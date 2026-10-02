"""消息发送频率限制插件。

统一限制机器人发送消息的频率（默认每分钟最多 9 条、每秒最多 1 条、
每小时最多 100 条），由两层机制协作实现：

- 发送节流（Bot.call_api 钩子）：拦截所有发送类接口，为每次发送排定发送时间，
  保证任意两条消息的间隔不小于每秒限额、滑动 60 秒与 3600 秒窗口内的普通消息
  条数分别不超过每分钟与每小时限额；超出限额的发送会排队等待空位，而不是被丢弃。
- 事件阻断（rate_limit_gate）：每分钟或每小时限额用尽后，以项目最小优先级拦截
  消息事件并停止事件传播，使所有会发送消息的插件（实名群提醒、命令等）停止运行。
  此时仅当消息指向机器人时才回复忙碌提示：群聊 @机器人 或以命令前缀
  （COMMAND_START）开头、私聊任意消息，引用机器人消息不算。忙碌提示每个
  群聊/私聊每分钟至多一条，不占用发送限额。

【后续开发注意】
1. 新增会发送消息的插件无需任何适配：只要响应器优先级大于
   RATE_LIMIT_GATE_PRIORITY（使用默认优先级即可满足），额度用尽时就会一并
   被阻断；消息经由 bot.send / bot.call_api 发送即可被节流，请勿绕过它们直接
   调用协议端接口，否则不会被计入频率限制。
2. 必须在额度用尽时也运行的响应器（如日志、审计类插件），应将其优先级设为
   小于 RATE_LIMIT_GATE_PRIORITY 的值（比本插件更先运行），并注意其发送仍会
   受发送节流的约束。
3. 事件阻断目前只覆盖消息事件；面向其他事件类型（notice、request 等）发送消息
   的插件在额度用尽时仍会运行，但其发送同样会被节流。如需一并阻断，请扩展
   本插件的匹配类型。
4. 修改 RATE_LIMIT_GATE_PRIORITY 时必须保证它是项目中所有响应器优先级的最小值，
   否则部分插件的运行将不会被阻断。
"""

import asyncio
import time
from collections import deque
from contextvars import ContextVar
from typing import Any, NamedTuple

from nonebot import get_driver, get_plugin_config, logger, on_message
from nonebot.adapters import Bot as BaseBot
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.matcher import Matcher
from nonebot.plugin import PluginMetadata
from pydantic import BaseModel, ValidationInfo, field_validator

__plugin_meta__ = PluginMetadata(
    name="消息发送频率限制",
    description="统一限制机器人发送消息的频率，达到上限时阻断其他插件并回复忙碌提示",
    usage="自动生效，无触发指令；可通过 RATE_LIMIT_* 环境变量配置",
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 发送频率限制的默认值（配置缺失、为空或无效时使用）
_DEFAULT_MAX_PER_MINUTE = 9
_DEFAULT_MAX_PER_SECOND = 1
_DEFAULT_MAX_PER_HOUR = 100
_DEFAULT_BUSY_MESSAGE = "Bot忙，请稍后再试。"

_MINUTE_SECONDS = 60.0
_SECOND_SECONDS = 1.0
_HOUR_SECONDS = 3600.0


class Config(BaseModel):
    """消息发送频率限制插件配置。

    可在 `.env.{environment}` 文件中通过 `RATE_LIMIT_*` 系列变量配置，
    缺失、为空、无法解析为整数或小于 1 时使用内置默认值
    （每分钟 9 条、每秒 1 条、每小时 100 条和内置文案）。
    """

    rate_limit_max_per_minute: int | None = None
    """滑动 60 秒窗口内允许发送的最大消息数，默认 9。"""

    rate_limit_max_per_second: int | None = None
    """每秒允许发送的最大消息数（决定相邻两条消息的最小发送间隔），默认 1。"""

    rate_limit_max_per_hour: int | None = None
    """滑动 3600 秒窗口内允许发送的最大消息数，默认 100。"""

    rate_limit_busy_message: str | None = None
    """发送频率达到上限后回复的忙碌提示文案，默认"Bot忙，请稍后再试。"。"""

    @field_validator(
        "rate_limit_max_per_minute",
        "rate_limit_max_per_second",
        "rate_limit_max_per_hour",
        mode="before",
    )
    @classmethod
    def invalid_int_as_none(cls, value: Any, info: ValidationInfo) -> Any:
        """将空字符串或无法解析为整数的值视为未配置。

        避免变量留空或填写错误（如填成 "abc"）导致 pydantic 校验失败、
        插件加载失败（nonebot 仅记录日志），频率限制静默失效。
        """
        if isinstance(value, str):
            if not value.strip():
                return None
            try:
                return int(value)
            except ValueError:
                logger.warning(
                    f"消息发送频率限制配置 {info.field_name}={value!r} "
                    f"无法解析为整数，已视为未配置并使用默认值"
                )
                return None
        return value


plugin_config = get_plugin_config(Config)


def _positive_int_or_default(value: int | None, default: int, name: str) -> int:
    """返回配置的正整数，缺失（None）或小于 1 时返回默认值并提示。"""
    if value is None:
        return default
    if value < 1:
        logger.warning(
            f"消息发送频率限制配置 {name}={value} 无效（需为正整数），"
            f"已使用默认值 {default}"
        )
        return default
    return value


_max_per_minute = _positive_int_or_default(
    plugin_config.rate_limit_max_per_minute,
    _DEFAULT_MAX_PER_MINUTE,
    "RATE_LIMIT_MAX_PER_MINUTE",
)
_max_per_second = _positive_int_or_default(
    plugin_config.rate_limit_max_per_second,
    _DEFAULT_MAX_PER_SECOND,
    "RATE_LIMIT_MAX_PER_SECOND",
)
_max_per_hour = _positive_int_or_default(
    plugin_config.rate_limit_max_per_hour,
    _DEFAULT_MAX_PER_HOUR,
    "RATE_LIMIT_MAX_PER_HOUR",
)
_busy_message = (
    plugin_config.rate_limit_busy_message or ""
).strip() or _DEFAULT_BUSY_MESSAGE

# 命令前缀集合（COMMAND_START）：用于判断群聊消息是否以指令开头
_command_start = set(get_driver().config.command_start)

# 相邻两条消息的最小发送间隔（秒），由每秒限额换算得到
_min_interval = _SECOND_SECONDS / _max_per_second

logger.info(
    f"消息发送频率限制已启用：每分钟最多 {_max_per_minute} 条、"
    f"每秒最多 {_max_per_second} 条、每小时最多 {_max_per_hour} 条"
)


class _ScheduledSend(NamedTuple):
    """一次已登记的发送：排定时间（time.monotonic）与是否为忙碌提示。"""

    at: float
    is_busy: bool


# 发送排定表：按排定时间升序记录最长窗口（1 小时）内已发出与排队中的全部
# 发送，移出窗口的条目会被清理。所有发送（含忙碌提示）都经它排定，保证间隔。
_send_schedule: deque[_ScheduledSend] = deque()

# 发送排定与登记的互斥锁：保证并发发送的间隔计算与限额判断不互相干扰
_send_lock = asyncio.Lock()

# 忙碌提示最近一次发送时间（time.monotonic），按会话（群聊/私聊）记录：
# 键为 group_{群号} 或 user_{QQ 号}，键不存在表示该会话本进程尚未收到过忙碌提示
_busy_last_sent: dict[str, float] = {}

# 标记"当前上下文正在发送忙碌提示"：忙碌提示不计入发送限额，不受每分钟与
# 每小时限额阻塞（但仍遵守发送间隔）
_sending_busy: ContextVar[bool] = ContextVar("rate_limit_sending_busy", default=False)

# OneBot v11 中会发出消息的 API；其他接口（如贴表情、上传文件）不受频率限制。
# 若后续使用协议端扩展的发送类接口，请将接口名一并加入本集合，否则不会被节流。
_SEND_APIS = frozenset(
    {
        "send_msg",
        "send_private_msg",
        "send_group_msg",
        "send_private_forward_msg",
        "send_group_forward_msg",
        "send_forward_msg",
    }
)


def _purge_expired(now: float) -> None:
    """清理发送排定表中已移出最长窗口（1 小时）的条目。"""
    while _send_schedule and now - _send_schedule[0].at >= _HOUR_SECONDS:
        _send_schedule.popleft()


def _committed_within(reference: float, window: float) -> list[_ScheduledSend]:
    """返回排定时间晚于 reference - window 的普通消息（不含忙碌提示）。

    用于统计滑动窗口内已消耗或已排队的限额；发送排定表按排定时间升序，
    返回列表的首项即窗口内最早的一条。
    """
    return [
        entry
        for entry in _send_schedule
        if not entry.is_busy and entry.at > reference - window
    ]


def _reserve_send_time(now: float, *, is_busy: bool) -> float:
    """为一次发送排定时间并登记，返回排定的 monotonic 发送时间。

    需在 `_send_lock` 内调用。排定时间满足三个约束：与上一条发送的间隔不小
    于每秒限额换算的最小间隔；普通消息（非忙碌提示）排定后的滑动 60 秒与
    3600 秒窗口内条数分别不超过每分钟与每小时限额。不满足时按"排队等待"
    向后顺延，而不是丢弃。
    """
    _purge_expired(now)
    scheduled = now
    while True:
        if _send_schedule:
            scheduled = max(scheduled, _send_schedule[-1].at + _min_interval)
        if is_busy:
            break
        minute_committed = _committed_within(scheduled, _MINUTE_SECONDS)
        if len(minute_committed) >= _max_per_minute:
            # 每分钟窗口已满：顺延到窗口内最早一条普通消息移出窗口后重查
            scheduled = minute_committed[0].at + _MINUTE_SECONDS
            continue
        hour_committed = _committed_within(scheduled, _HOUR_SECONDS)
        if len(hour_committed) >= _max_per_hour:
            # 每小时窗口已满：同理顺延到最早一条移出小时窗口
            scheduled = hour_committed[0].at + _HOUR_SECONDS
            continue
        break
    _send_schedule.append(_ScheduledSend(at=scheduled, is_busy=is_busy))
    return scheduled


# 发送节流：所有经由 bot.call_api 的发送类调用都会先经过本钩子，按频率限制
# 排定发送时间；钩子内的等待会阻塞本次 API 调用，直到到达排定时间。
@Bot.on_calling_api
async def _handle_calling_api(_bot: BaseBot, api: str, _data: dict[str, Any]) -> None:
    """发送节流入口：为发送类 API 排定发送时间并等待。"""
    if api not in _SEND_APIS:
        return
    is_busy = _sending_busy.get()
    async with _send_lock:
        scheduled = _reserve_send_time(time.monotonic(), is_busy=is_busy)
    delay = scheduled - time.monotonic()
    if delay > 0:
        logger.debug(f"发送频率限制：{api} 排队 {delay:.2f} 秒后发送")
        await asyncio.sleep(delay)


# ===== 事件阻断（rate_limit_gate）=====
#
# 【重要】RATE_LIMIT_GATE_PRIORITY 必须是项目中所有响应器优先级的最小值
# （数值最小、最先运行）：每分钟或每小时限额用尽时，本响应器会停止事件传播，
# 使事件不再被更低优先级（数值更大）的任何响应器处理，从而阻断所有会发送
# 消息的插件。开发注意事项详见本模块开头的说明。
RATE_LIMIT_GATE_PRIORITY = -1000

rate_limit_gate = on_message(priority=RATE_LIMIT_GATE_PRIORITY, block=False)


def _session_key(event: MessageEvent) -> str:
    """返回事件所在会话的标识：群聊按群隔离、私聊按用户隔离。

    同一群聊内的不同成员共用同一条忙碌提示额度。
    """
    if isinstance(event, GroupMessageEvent):
        return f"group_{event.group_id}"
    return f"user_{event.user_id}"


def _directed_to_bot(event: MessageEvent) -> bool:
    """判断消息是否指向机器人，以此决定是否回复忙碌提示。

    私聊消息始终视为与机器人对话；群聊消息仅当包含 @机器人 或以命令前缀
    （COMMAND_START）开头时才视为对话。注意：不能直接用 event.is_tome()——
    引用机器人消息会被适配器一并标记为 to_me；@机器人 也不能看处理后的消息，
    因为适配器会移除消息首尾的 @机器人 段，故这里检查 original_message。
    引用机器人消息本身既不含 @机器人、也不以命令前缀开头，不会收到忙碌提示。
    """
    if isinstance(event, PrivateMessageEvent):
        return True
    if not isinstance(event, GroupMessageEvent):
        return False
    if any(
        segment.type == "at" and str(segment.data.get("qq")) == str(event.self_id)
        for segment in event.original_message
    ):
        return True
    text = event.get_plaintext().lstrip()
    return any(text.startswith(prefix) for prefix in _command_start)


@rate_limit_gate.handle()
async def handle_rate_limit_gate(matcher: Matcher, event: MessageEvent) -> None:
    """每分钟或每小时限额用尽时阻断事件传播，并按会话节流回复忙碌提示。"""
    now = time.monotonic()
    _purge_expired(now)
    minute_exhausted = len(_committed_within(now, _MINUTE_SECONDS)) >= _max_per_minute
    hour_exhausted = len(_committed_within(now, _HOUR_SECONDS)) >= _max_per_hour
    if not minute_exhausted and not hour_exhausted:
        return

    # 限额已用尽：停止事件传播，后续优先级的插件都不会运行
    matcher.stop_propagation()
    logger.debug(f"发送频率已达上限，已阻断事件传播：{event.get_session_id()}")

    # 仅当消息指向机器人时才回复忙碌提示：群聊 @机器人 或以命令前缀开头、
    # 私聊任意消息；群聊中的普通聊天与引用机器人消息只阻断、不回复
    if not _directed_to_bot(event):
        return

    # 忙碌提示每个群聊/私聊每分钟至多一条且不计入限额
    session_key = _session_key(event)
    # 顺带清理已过期的记录，避免字典随会话数量持续增长
    expired_keys = [
        key
        for key, sent_at in _busy_last_sent.items()
        if now - sent_at >= _MINUTE_SECONDS
    ]
    for expired_key in expired_keys:
        del _busy_last_sent[expired_key]
    last_busy_at = _busy_last_sent.get(session_key)
    if last_busy_at is not None and now - last_busy_at < _MINUTE_SECONDS:
        return

    # 先记录时间再发送，避免同一会话的并发事件重复回复
    _busy_last_sent[session_key] = now
    token = _sending_busy.set(True)
    try:
        await rate_limit_gate.send(MessageSegment.text(_busy_message))
        logger.info(f"发送频率已达上限，已回复忙碌提示：{_busy_message}")
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"发送忙碌提示失败：{exc}")
    finally:
        _sending_busy.reset(token)
