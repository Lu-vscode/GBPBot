"""消息发送频率限制插件。

统一限制机器人发送消息的频率，限额分全局与单个会话（群聊/私聊）两层：默认全局
每秒 1 条、每分钟 20 条、每小时 300 条，单个群聊/私聊每秒 1 条、每分钟 9 条、
每小时 100 条，均可通过 RATE_LIMIT_* 环境变量修改。一次发送需同时满足两层限额，
由两层机制协作实现：

- 发送节流（Bot.call_api 钩子）：拦截所有发送类接口，为每次发送排定发送时间，
  保证任意两条消息的间隔不小于每秒限额（两层取较大者）、发送后全局与所在会话的
  滑动 60 秒与 3600 秒窗口内的普通消息条数分别不超过各自的每分钟与每小时限额；
  超出限额的发送会排队等待空位，而不是被丢弃。忙碌提示不计入任何窗口，只与相邻
  发送保持最小间隔，不会排在尚未到点的普通消息之后（限额用尽时普通消息可能被排
  到近一小时之后）。
- 事件阻断（rate_limit_gate）：全局或事件所在会话的每分钟或每小时限额用尽后，
  以项目最小优先级拦截消息事件并停止事件传播，使所有会发送消息的插件（实名群
  提醒、命令等）停止运行。此时仅当消息指向机器人时才回复忙碌提示：群聊 @机器人
  或以命令前缀（COMMAND_START）开头、私聊任意消息，引用机器人消息不算。忙碌
  提示每个群聊/私聊每分钟至多一条，不占用发送限额。

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
5. 会话限额按发送类接口参数中的 group_id / user_id 归属：群消息计入所在群聊、
   私聊消息计入该用户。两者都不带的接口（如部分通用合并转发接口）只受全局限额
   约束；新增此类接口时需一并确定其会话归属，否则会话限额对其不生效。
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
    description="统一限制机器人发送消息的频率（全局与单个群聊/私聊两层），达到上限时阻断其他插件并回复忙碌提示",
    usage="自动生效，无触发指令；可通过 RATE_LIMIT_* 环境变量配置",
    type="application",
    supported_adapters={"~onebot.v11"},
)


class _Limits(NamedTuple):
    """一层限额：每秒条数、最小发送间隔（秒）、每分钟与每小时条数上限。"""

    max_per_second: int
    min_interval: float
    max_per_minute: int
    max_per_hour: int


# 发送频率限制的默认值（配置缺失、为空或无效时使用）：全局与单个会话两层
_DEFAULT_GLOBAL_LIMITS = _Limits(
    max_per_second=1, min_interval=1.0, max_per_minute=20, max_per_hour=300
)
_DEFAULT_SESSION_LIMITS = _Limits(
    max_per_second=1, min_interval=1.0, max_per_minute=9, max_per_hour=100
)
_DEFAULT_BUSY_MESSAGE = "Bot忙，请稍后再试。"

_MINUTE_SECONDS = 60.0
_SECOND_SECONDS = 1.0
_HOUR_SECONDS = 3600.0


class Config(BaseModel):
    """消息发送频率限制插件配置。

    可在 `.env.{environment}` 文件中通过 `RATE_LIMIT_*` 系列变量配置，缺失、为空、
    无法解析为整数或小于 1 时使用内置默认值。限额分全局与单个会话（群聊/私聊）两层，
    一次发送需同时满足两层限额；默认值如下：

    - 全局：每秒 1 条、每分钟 20 条、每小时 300 条
    - 单个群聊/私聊：每秒 1 条、每分钟 9 条、每小时 100 条
    """

    rate_limit_global_max_per_second: int | None = None
    """全局每秒允许发送的最大消息数（决定任意两条消息的最小发送间隔），默认 1。"""

    rate_limit_global_max_per_minute: int | None = None
    """全局滑动 60 秒窗口内允许发送的最大消息数，默认 20。"""

    rate_limit_global_max_per_hour: int | None = None
    """全局滑动 3600 秒窗口内允许发送的最大消息数，默认 300。"""

    rate_limit_session_max_per_second: int | None = None
    """单个群聊/私聊每秒允许发送的最大消息数，默认 1。"""

    rate_limit_session_max_per_minute: int | None = None
    """单个群聊/私聊滑动 60 秒窗口内允许发送的最大消息数，默认 9。"""

    rate_limit_session_max_per_hour: int | None = None
    """单个群聊/私聊滑动 3600 秒窗口内允许发送的最大消息数，默认 100。"""

    rate_limit_busy_message: str | None = None
    """发送频率达到上限后回复的忙碌提示文案，默认"Bot忙，请稍后再试。"。"""

    @field_validator(
        "rate_limit_global_max_per_second",
        "rate_limit_global_max_per_minute",
        "rate_limit_global_max_per_hour",
        "rate_limit_session_max_per_second",
        "rate_limit_session_max_per_minute",
        "rate_limit_session_max_per_hour",
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


def _build_limits(
    per_second: int | None,
    per_minute: int | None,
    per_hour: int | None,
    name_prefix: str,
    defaults: _Limits,
) -> _Limits:
    """按配置构建一层限额，非法值回退默认值。

    最小发送间隔由每秒限额换算得到（如每秒 2 条即间隔 0.5 秒）。
    """
    max_per_second = _positive_int_or_default(
        per_second, defaults.max_per_second, f"{name_prefix}_MAX_PER_SECOND"
    )
    max_per_minute = _positive_int_or_default(
        per_minute, defaults.max_per_minute, f"{name_prefix}_MAX_PER_MINUTE"
    )
    max_per_hour = _positive_int_or_default(
        per_hour, defaults.max_per_hour, f"{name_prefix}_MAX_PER_HOUR"
    )
    return _Limits(
        max_per_second=max_per_second,
        min_interval=_SECOND_SECONDS / max_per_second,
        max_per_minute=max_per_minute,
        max_per_hour=max_per_hour,
    )


# 全局限额与单个群聊/私聊限额：一次发送需同时满足两层
_global_limits = _build_limits(
    plugin_config.rate_limit_global_max_per_second,
    plugin_config.rate_limit_global_max_per_minute,
    plugin_config.rate_limit_global_max_per_hour,
    "RATE_LIMIT_GLOBAL",
    _DEFAULT_GLOBAL_LIMITS,
)
_session_limits = _build_limits(
    plugin_config.rate_limit_session_max_per_second,
    plugin_config.rate_limit_session_max_per_minute,
    plugin_config.rate_limit_session_max_per_hour,
    "RATE_LIMIT_SESSION",
    _DEFAULT_SESSION_LIMITS,
)

_busy_message = (
    plugin_config.rate_limit_busy_message or ""
).strip() or _DEFAULT_BUSY_MESSAGE

# 命令前缀集合（COMMAND_START）：用于判断群聊消息是否以指令开头
_command_start = set(get_driver().config.command_start)

logger.info(
    f"消息发送频率限制已启用：全局每秒 {_global_limits.max_per_second} 条、"
    f"每分钟 {_global_limits.max_per_minute} 条、"
    f"每小时 {_global_limits.max_per_hour} 条；"
    f"单个群聊/私聊每秒 {_session_limits.max_per_second} 条、"
    f"每分钟 {_session_limits.max_per_minute} 条、"
    f"每小时 {_session_limits.max_per_hour} 条"
)


class _ScheduledSend(NamedTuple):
    """一次已登记的发送：排定时间（time.monotonic）与是否为忙碌提示。"""

    at: float
    is_busy: bool


# 全局发送排定表：按排定时间升序记录最长窗口（1 小时）内已发出与排队中的全部
# 发送，移出窗口的条目会被清理。所有发送（含忙碌提示）都经它排定，保证间隔。
# 忙碌提示可能插入到已排队但尚未到点的普通消息之前，插入后仍保持升序。
_global_schedule: deque[_ScheduledSend] = deque()

# 会话发送排定表：每个群聊/私聊一份，键为 group_{群号} 或 user_{QQ 号}，
# 结构与全局排定表相同（按排定时间升序），清空后随清理一并移除
_session_schedules: dict[str, deque[_ScheduledSend]] = {}

# 发送排定与登记的互斥锁：保证并发发送的间隔计算与限额判断不互相干扰
_send_lock = asyncio.Lock()

# 忙碌提示最近一次发送时间（time.monotonic），按会话（群聊/私聊）记录：
# 键为 group_{群号} 或 user_{QQ 号}，键不存在表示该会话本进程尚未收到过忙碌提示
_busy_last_sent: dict[str, float] = {}

# 标记"当前上下文正在发送忙碌提示"：忙碌提示不计入发送限额，不受每分钟与
# 每小时限额阻塞（但仍遵守与相邻发送的最小间隔，且不会排在尚未到点的普通
# 消息之后）
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


def _purge_expired(schedule: deque[_ScheduledSend], now: float) -> None:
    """清理单个排定表中已移出最长窗口（1 小时）的条目。"""
    while schedule and now - schedule[0].at >= _HOUR_SECONDS:
        schedule.popleft()


def _purge_all(now: float) -> None:
    """清理全局与会话排定表中已移出最长窗口的条目，并丢弃清空后的会话。

    顺带移除已无条目的会话，避免排定表随会话数量持续增长。
    """
    _purge_expired(_global_schedule, now)
    for key in list(_session_schedules):
        schedule = _session_schedules[key]
        _purge_expired(schedule, now)
        if not schedule:
            del _session_schedules[key]


def _committed_within(
    schedule: deque[_ScheduledSend], reference: float, window: float
) -> list[_ScheduledSend]:
    """返回排定表中排定时间晚于 reference - window 的普通消息（不含忙碌提示）。

    用于统计滑动窗口内已消耗或已排队的限额；排定表按排定时间升序，
    返回列表的首项即窗口内最早的一条。
    """
    return [
        entry
        for entry in schedule
        if not entry.is_busy and entry.at > reference - window
    ]


def _limits_exhausted(
    schedule: deque[_ScheduledSend], now: float, limits: _Limits
) -> bool:
    """判断该层限额在 now 时刻是否已用尽（每分钟或每小时任一满额）。"""
    return (
        len(_committed_within(schedule, now, _MINUTE_SECONDS)) >= limits.max_per_minute
        or len(_committed_within(schedule, now, _HOUR_SECONDS)) >= limits.max_per_hour
    )


def _insert_scheduled(
    schedule: deque[_ScheduledSend], at: float, *, is_busy: bool
) -> None:
    """按排定时间升序把一次发送登记到排定表。

    普通消息总排在全部队尾之后，追加即可；忙碌提示可能插入到已排队但尚未
    到点的普通消息之前，需要找到正确位置，窗口统计与过期清理依赖升序。
    """
    entry = _ScheduledSend(at=at, is_busy=is_busy)
    index = len(schedule)
    while index > 0 and schedule[index - 1].at > at:
        index -= 1
    schedule.insert(index, entry)


def _earliest_normal(
    schedule: deque[_ScheduledSend], start: float, limits: _Limits
) -> float:
    """计算不早于 start 的最早发送时间，使普通消息满足该层限额。

    排定时间需满足：与排定表中最后一条发送的间隔不小于最小间隔；排定后的
    滑动 60 秒与 3600 秒窗口内条数分别不超过每分钟与每小时限额。不满足时按
    "排队等待"向后顺延（顺延到窗口内最早一条移出窗口的时刻后重查），而不是
    丢弃。

    把排定时间往后顺延不会让任何一项约束变紧，因此调用方可安全地对两层各自
    算出的时间取较大值。
    """
    scheduled = start
    while True:
        if schedule:
            scheduled = max(scheduled, schedule[-1].at + limits.min_interval)
        minute_committed = _committed_within(schedule, scheduled, _MINUTE_SECONDS)
        if len(minute_committed) >= limits.max_per_minute:
            # 每分钟窗口已满：顺延到窗口内最早一条普通消息移出窗口后重查
            scheduled = minute_committed[0].at + _MINUTE_SECONDS
            continue
        hour_committed = _committed_within(schedule, scheduled, _HOUR_SECONDS)
        if len(hour_committed) >= limits.max_per_hour:
            # 每小时窗口已满：同理顺延到最早一条移出小时窗口
            scheduled = hour_committed[0].at + _HOUR_SECONDS
            continue
        return scheduled


def _earliest_busy(
    schedule: deque[_ScheduledSend], start: float, limits: _Limits
) -> float:
    """计算不早于 start 的最早发送时间，使忙碌提示与相邻发送保持最小间隔。

    忙碌提示不计入每分钟与每小时窗口，只保证与排定表中每条发送的间隔不小于
    最小间隔，从而可以插到尚未到点的普通消息之前：限额用尽时普通消息可能被
    排到近一小时之后，若忙碌提示也追加到队尾，就会被一并拖后而失去提示意义。
    """
    scheduled = start
    for entry in schedule:
        if entry.at >= scheduled:
            if entry.at - scheduled >= limits.min_interval:
                break
            scheduled = entry.at + limits.min_interval
        elif scheduled - entry.at < limits.min_interval:
            scheduled = entry.at + limits.min_interval
    return scheduled


def _reserve_send_time(now: float, session_key: str | None, *, is_busy: bool) -> float:
    """为一次发送排定时间并登记，返回排定的 monotonic 发送时间。

    需在 `_send_lock` 内调用。发送需同时满足全局与所在会话两层限额：分别算出
    两层的最早发送时间后取较晚的一个登记，session_key 为 None（接口参数中取
    不到群号与 QQ 号）时只受全局限额约束。
    """
    _purge_all(now)
    session_schedule = (
        _session_schedules.setdefault(session_key, deque())
        if session_key is not None
        else None
    )
    if is_busy:
        scheduled = _earliest_busy(_global_schedule, now, _global_limits)
        if session_schedule is not None:
            scheduled = _earliest_busy(session_schedule, scheduled, _session_limits)
    else:
        scheduled = _earliest_normal(_global_schedule, now, _global_limits)
        if session_schedule is not None:
            scheduled = _earliest_normal(session_schedule, scheduled, _session_limits)
    _insert_scheduled(_global_schedule, scheduled, is_busy=is_busy)
    if session_schedule is not None:
        _insert_scheduled(session_schedule, scheduled, is_busy=is_busy)
    return scheduled


def _api_session_key(data: dict[str, Any]) -> str | None:
    """从发送类 API 的参数中取出会话标识，取不到时返回 None（只受全局限额约束）。

    群消息带 group_id、私聊消息带 user_id；个别接口（如通用合并转发接口）可能
    两者都不带。
    """
    group_id = data.get("group_id")
    if group_id is not None:
        return f"group_{group_id}"
    user_id = data.get("user_id")
    if user_id is not None:
        return f"user_{user_id}"
    return None


# 发送节流：所有经由 bot.call_api 的发送类调用都会先经过本钩子，按频率限制
# 排定发送时间；钩子内的等待会阻塞本次 API 调用，直到到达排定时间。
@Bot.on_calling_api
async def _handle_calling_api(_bot: BaseBot, api: str, _data: dict[str, Any]) -> None:
    """发送节流入口：为发送类 API 排定发送时间并等待。"""
    if api not in _SEND_APIS:
        return
    is_busy = _sending_busy.get()
    session_key = _api_session_key(_data)
    async with _send_lock:
        scheduled = _reserve_send_time(time.monotonic(), session_key, is_busy=is_busy)
    delay = scheduled - time.monotonic()
    if delay > 0:
        logger.debug(f"发送频率限制：{api} 排队 {delay:.2f} 秒后发送")
        await asyncio.sleep(delay)


# ===== 事件阻断（rate_limit_gate）=====
#
# 【重要】RATE_LIMIT_GATE_PRIORITY 必须是项目中所有响应器优先级的最小值
# （数值最小、最先运行）：全局或事件所在会话的每分钟或每小时限额用尽时，本响应器
# 会停止事件传播，使事件不再被更低优先级（数值更大）的任何响应器处理，从而阻断
# 所有会发送消息的插件。开发注意事项详见本模块开头的说明。
RATE_LIMIT_GATE_PRIORITY = -1000

rate_limit_gate = on_message(priority=RATE_LIMIT_GATE_PRIORITY, block=False)


def _session_key(event: MessageEvent) -> str:
    """返回事件所在会话的标识：群聊按群隔离、私聊按用户隔离。

    同一群聊内的不同成员共用同一份额度与同一条忙碌提示额度。
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
    """全局或所在会话的限额用尽时阻断事件传播，并按会话节流回复忙碌提示。"""
    now = time.monotonic()
    _purge_all(now)
    session_key = _session_key(event)
    session_schedule = _session_schedules.get(session_key)
    exhausted = _limits_exhausted(_global_schedule, now, _global_limits) or (
        session_schedule is not None
        and _limits_exhausted(session_schedule, now, _session_limits)
    )
    if not exhausted:
        return

    # 限额已用尽：停止事件传播，后续优先级的插件都不会运行
    matcher.stop_propagation()
    logger.debug(f"发送频率已达上限，已阻断事件传播：{event.get_session_id()}")

    # 仅当消息指向机器人时才回复忙碌提示：群聊 @机器人 或以命令前缀开头、
    # 私聊任意消息；群聊中的普通聊天与引用机器人消息只阻断、不回复
    if not _directed_to_bot(event):
        return

    # 忙碌提示每个群聊/私聊每分钟至多一条且不计入限额
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
