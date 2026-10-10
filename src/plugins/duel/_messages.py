"""决斗消息发送。

发送决斗相关的群聊消息（文本、合并转发、猜拳表情），并实现决斗超时
提示的"跟随发送"机制：超时提示不在超时时立即发出，而是先在队列保留
一段时间（_TIMEOUT_MESSAGE_LINGER_SECONDS），期间机器人向同群发送
其它消息时在其后延迟（_TIMEOUT_MESSAGE_DELAY_SECONDS）跟随发出，
保留期内未能跟随则静默丢弃。本模块所有发送成功（含猜拳表情）都会
触发该群保留中提示的调度释放。
"""

import asyncio
import time
from dataclasses import dataclass

from nonebot import get_bots, logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError

from src.plugins._shared.onebot import send_group_forward, send_group_text
from src.plugins.duel._gestures import message_to_gesture

# 决斗超时提示的延迟发送：超时后在队列保留该时长（秒），期间机器人向同群
# 发送其它消息时在其后延迟该时长（秒）发出，保留期内未能跟随则静默丢弃
_TIMEOUT_MESSAGE_LINGER_SECONDS = 60.0
_TIMEOUT_MESSAGE_DELAY_SECONDS = 1.0


@dataclass
class _DeferredTimeoutMessage:
    """已超时决斗的提示：先保留，跟随机器人在该群的其它消息延迟发出。"""

    bot_self_id: str
    """发送该提示的机器人 QQ 号。"""

    group_id: int
    """提示所在群号。"""

    text: str
    """超时提示文案。"""

    deadline: float
    """保留截止时间（time.monotonic），超过后静默丢弃。"""


# 保留中的决斗超时提示（超时后不立即发送，等待跟随机器人其它消息）
_deferred_timeouts: list[_DeferredTimeoutMessage] = []

# 延迟发送的任务集合：持有引用避免任务被垃圾回收，任务结束后自动移出
_deferred_tasks: set[asyncio.Task[None]] = set()


def drop_expired_timeouts(now: float) -> None:
    """静默丢弃超过保留时间的决斗超时提示。"""
    kept: list[_DeferredTimeoutMessage] = []
    for message in _deferred_timeouts:
        if message.deadline <= now:
            logger.debug(
                f"群 {message.group_id} 的决斗超时提示超过保留时间，已静默丢弃"
            )
        else:
            kept.append(message)
    _deferred_timeouts[:] = kept


def defer_timeout(*, bot_self_id: int, group_id: int, text: str, now: float) -> None:
    """把决斗超时提示转入延迟发送队列（保留期内等待跟随机器人消息）。"""
    _deferred_timeouts.append(
        _DeferredTimeoutMessage(
            bot_self_id=str(bot_self_id),
            group_id=group_id,
            text=text,
            deadline=now + _TIMEOUT_MESSAGE_LINGER_SECONDS,
        )
    )


def _release_timeouts(group_id: int) -> None:
    """机器人向群聊发送消息后，调度保留期内的超时提示延迟发出。

    需在发送成功后调用；提示按群匹配（跟随同群的其它消息），
    超过保留时间的提示会被静默丢弃。
    """
    now = time.monotonic()
    drop_expired_timeouts(now)
    claimed: list[_DeferredTimeoutMessage] = []
    kept: list[_DeferredTimeoutMessage] = []
    for message in _deferred_timeouts:
        if message.group_id == group_id:
            claimed.append(message)
        else:
            kept.append(message)
    if not claimed:
        return
    _deferred_timeouts[:] = kept
    task = asyncio.create_task(_send_deferred_timeouts(claimed))
    _deferred_tasks.add(task)
    task.add_done_callback(_deferred_tasks.discard)


async def _send_deferred_timeouts(messages: list[_DeferredTimeoutMessage]) -> None:
    """延迟一段时间后发送已认领的决斗超时提示。"""
    await asyncio.sleep(_TIMEOUT_MESSAGE_DELAY_SECONDS)
    bots = get_bots()
    for message in messages:
        bot = bots.get(message.bot_self_id)
        if not isinstance(bot, Bot):
            logger.debug(
                f"机器人 {message.bot_self_id} 离线，"
                f"群 {message.group_id} 的决斗超时提示未发出"
            )
            continue
        logger.debug(f"群 {message.group_id} 的决斗超时提示跟随机器人消息发出")
        await send_text(bot, message.group_id, message.text)


async def send_text(
    bot: Bot, group_id: int, text: str, reply_to: int | None = None
) -> None:
    """向群聊发送决斗文本消息，成功后调度跟发本群保留中的超时提示。"""
    success = await send_group_text(bot, group_id, text, reply_to, label="决斗消息")
    if success:
        _release_timeouts(group_id)


async def send_forward(
    bot: Bot,
    group_id: int,
    texts: list[str],
    *,
    node_name: str,
    label: str,
) -> None:
    """以合并转发的聊天记录形式发送多条文本（每条文本一个节点），
    成功后调度跟发本群保留中的超时提示。"""
    success = await send_group_forward(
        bot, group_id, texts, node_name=node_name, label=label
    )
    if success:
        _release_timeouts(group_id)


async def send_rps(bot: Bot, group_id: int) -> int | None:
    """发送猜拳表情，返回消息 ID，发送失败时返回 None。"""
    try:
        data = await bot.call_api(
            "send_group_msg",
            group_id=group_id,
            message=Message([MessageSegment.rps()]),
        )
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"发送猜拳表情失败（群 {group_id}）：{exc}")
        return None
    _release_timeouts(group_id)
    message_id = (data or {}).get("message_id")
    return message_id if isinstance(message_id, int) else None


async def fetch_rps_result(bot: Bot, message_id: int) -> int | None:
    """查询消息的猜拳结果（发送猜拳表情无法指定结果，需发送后回查）。"""
    try:
        data = await bot.call_api("get_msg", message_id=message_id)
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"查询猜拳表情结果失败（消息 {message_id}）：{exc}")
        return None
    return message_to_gesture((data or {}).get("message"))
