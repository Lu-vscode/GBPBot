"""决斗消息发送。

发送决斗相关的群聊消息（文本、合并转发、猜拳表情）；每次发送成功后调度
跟发本群保留中的超时提示（见 deferred.py）。
"""

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError

from src.plugins._shared.onebot import send_group_forward, send_group_text
from src.plugins.duel.core import message_to_gesture
from src.plugins.duel.messaging.deferred import release_timeouts


async def send_text(
    bot: Bot, group_id: int, text: str, reply_to: int | None = None
) -> None:
    """向群聊发送决斗文本消息，成功后调度跟发本群保留中的超时提示。"""
    success = await send_group_text(bot, group_id, text, reply_to, label="决斗消息")
    if success:
        release_timeouts(group_id)


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
        release_timeouts(group_id)


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
    release_timeouts(group_id)
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
