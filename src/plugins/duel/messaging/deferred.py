"""决斗超时提示的延迟跟发队列。

超时提示不在超时时立即发出，而是先在队列保留一段时间
（_TIMEOUT_MESSAGE_LINGER_SECONDS），期间机器人向同群发送其它消息时在其
后延迟（_TIMEOUT_MESSAGE_DELAY_SECONDS）跟随发出，保留期内未能跟随则
静默丢弃。发送动作由 messaging.send 的成功发送触发（见 release_timeouts）。

延迟发送直接复用 _shared/onebot/ 的群文本发送，不经过 send.py 的包装，
避免跟发自身再次触发队列释放。
"""

import asyncio
import time
from dataclasses import dataclass

from nonebot import get_bots, logger
from nonebot.adapters.onebot.v11 import Bot

from src.plugins._shared.onebot import send_group_text

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


def release_timeouts(group_id: int) -> None:
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
        await send_group_text(
            bot, message.group_id, message.text, None, label="决斗消息"
        )
