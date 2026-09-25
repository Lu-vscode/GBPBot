"""消息发送频率限制插件。

统一限制机器人发送消息的频率（默认每分钟最多 9 条、每秒最多 1 条），
由两层机制协作实现：

- 发送节流（Bot.call_api 钩子）：拦截所有发送类接口，为每次发送排定发送时间，
  保证任意两条消息的间隔不小于每秒限额、滑动 60 秒窗口内的普通消息条数不超过
  每分钟限额；超出限额的发送会排队等待空位，而不是被丢弃。
- 事件阻断（rate_limit_gate）：每分钟限额用尽后，以项目最小优先级拦截消息事件
  并停止事件传播，使所有会发送消息的插件（实名群提醒、命令、默认回复等）停止
  运行，并按分钟节流回复一条忙碌提示（不占用发送限额）。

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

from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters import Bot as BaseBot
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.matcher import Matcher
from nonebot.plugin import PluginMetadata
from pydantic import BaseModel, field_validator

__plugin_meta__ = PluginMetadata(
    name="消息发送频率限制",
    description="统一限制机器人发送消息的频率，达到每分钟上限时阻断其他插件并回复忙碌提示",
    usage="自动生效，无触发指令；可通过 RATE_LIMIT_* 环境变量配置",
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 发送频率限制的默认值（配置缺失、为空或无效时使用）
_DEFAULT_MAX_PER_MINUTE = 9
_DEFAULT_MAX_PER_SECOND = 1
_DEFAULT_BUSY_MESSAGE = "Bot忙，请稍后再试。"

_MINUTE_SECONDS = 60.0
_SECOND_SECONDS = 1.0


class Config(BaseModel):
    """消息发送频率限制插件配置。

    可在 `.env.{environment}` 文件中通过 `RATE_LIMIT_*` 系列变量配置，
    缺失、为空或小于 1 时使用内置默认值（每分钟 9 条、每秒 1 条和内置文案）。
    """

    rate_limit_max_per_minute: int | None = None
    """滑动 60 秒窗口内允许发送的最大消息数，默认 9。"""

    rate_limit_max_per_second: int | None = None
    """每秒允许发送的最大消息数（决定相邻两条消息的最小发送间隔），默认 1。"""

    rate_limit_busy_message: str | None = None
    """发送频率达到上限后回复的忙碌提示文案，默认"Bot忙，请稍后再试。"。"""

    @field_validator(
        "rate_limit_max_per_minute", "rate_limit_max_per_second", mode="before"
    )
    @classmethod
    def blank_int_as_none(cls, value: Any) -> Any:
        """将空字符串视为未配置，避免变量留空导致插件加载失败。"""
        if isinstance(value, str) and not value.strip():
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
_busy_message = (
    plugin_config.rate_limit_busy_message or ""
).strip() or _DEFAULT_BUSY_MESSAGE

# 相邻两条消息的最小发送间隔（秒），由每秒限额换算得到
_min_interval = _SECOND_SECONDS / _max_per_second

logger.info(
    f"消息发送频率限制已启用：每分钟最多 {_max_per_minute} 条、"
    f"每秒最多 {_max_per_second} 条"
)


class _ScheduledSend(NamedTuple):
    """一次已登记的发送：排定时间（time.monotonic）与是否为忙碌提示。"""

    at: float
    is_busy: bool


# 发送排定表：按排定时间升序记录滑动 60 秒窗口内已发出与排队中的全部发送，
# 超窗条目会被清理。所有发送（含忙碌提示）都经它排定，保证发送间隔。
_send_schedule: deque[_ScheduledSend] = deque()

# 发送排定与登记的互斥锁：保证并发发送的间隔计算与限额判断不互相干扰
_send_lock = asyncio.Lock()

# 忙碌提示最近一次发送时间（time.monotonic），键不存在表示本进程尚未发送过
_busy_last_sent: dict[str, float] = {}

# 标记"当前上下文正在发送忙碌提示"：忙碌提示不计入发送限额，不受每分钟限额
# 阻塞（但仍遵守发送间隔）
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
    """清理发送排定表中已移出滑动 60 秒窗口的条目。"""
    while _send_schedule and now - _send_schedule[0].at >= _MINUTE_SECONDS:
        _send_schedule.popleft()


def _reserve_send_time(now: float, *, is_busy: bool) -> float:
    """为一次发送排定时间并登记，返回排定的 monotonic 发送时间。

    需在 `_send_lock` 内调用。排定时间满足两个约束：与上一条发送的间隔不小于
    每秒限额换算的最小间隔；普通消息（非忙碌提示）排定后的滑动 60 秒窗口内
    条数不超过每分钟限额。不满足时按"排队等待"向后顺延，而不是丢弃。
    """
    _purge_expired(now)
    scheduled = now
    while True:
        if _send_schedule:
            scheduled = max(scheduled, _send_schedule[-1].at + _min_interval)
        if is_busy:
            break
        committed = [
            entry
            for entry in _send_schedule
            if not entry.is_busy and entry.at > scheduled - _MINUTE_SECONDS
        ]
        if len(committed) < _max_per_minute:
            break
        # 窗口已满：顺延到窗口内最早一条普通消息移出窗口之后再重新检查
        scheduled = committed[0].at + _MINUTE_SECONDS
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
# （数值最小、最先运行）：每分钟限额用尽时，本响应器会停止事件传播，使事件
# 不再被更低优先级（数值更大）的任何响应器处理，从而阻断所有会发送消息的
# 插件。开发注意事项详见本模块开头的说明。
RATE_LIMIT_GATE_PRIORITY = -1000

rate_limit_gate = on_message(priority=RATE_LIMIT_GATE_PRIORITY, block=False)


@rate_limit_gate.handle()
async def handle_rate_limit_gate(matcher: Matcher, event: MessageEvent) -> None:
    """每分钟限额用尽时阻断事件传播，并按分钟节流回复忙碌提示。"""
    now = time.monotonic()
    _purge_expired(now)
    if sum(1 for entry in _send_schedule if not entry.is_busy) < _max_per_minute:
        return

    # 限额已用尽：停止事件传播，后续优先级的插件都不会运行
    matcher.stop_propagation()
    logger.debug(f"发送频率已达上限，已阻断事件传播：{event.get_session_id()}")

    last_busy_at = _busy_last_sent.get("at")
    if last_busy_at is not None and now - last_busy_at < _MINUTE_SECONDS:
        return

    # 忙碌提示每分钟至多发送一条且不计入限额；先记录时间再发送，避免并发重复
    _busy_last_sent["at"] = now
    token = _sending_busy.set(True)
    try:
        await rate_limit_gate.send(MessageSegment.text(_busy_message))
        logger.info(f"发送频率已达上限，已回复忙碌提示：{_busy_message}")
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"发送忙碌提示失败：{exc}")
    finally:
        _sending_busy.reset(token)
