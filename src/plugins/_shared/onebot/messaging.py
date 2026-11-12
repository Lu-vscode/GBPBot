"""群聊消息发送：文本消息与合并转发消息。

发送均以消息段构造，避免文案被协议端解析为 CQ 码；失败只记录日志，
不影响调用方流程。
"""

from collections.abc import Sequence

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError


async def send_group_text(
    bot: Bot,
    group_id: int,
    text: str,
    reply_to: int | None = None,
    *,
    label: str = "消息",
) -> bool:
    """向群聊发送文本消息，返回是否发送成功。

    作为纯文本段构造以避免昵称等被解析为 CQ 码；reply_to 非空时附带引用。
    label 仅用于失败日志（如"决斗消息"）。
    """
    segments: list[MessageSegment] = []
    if reply_to is not None:
        segments.append(MessageSegment.reply(reply_to))
    segments.append(MessageSegment.text(text))
    try:
        await bot.call_api(
            "send_group_msg", group_id=group_id, message=Message(segments)
        )
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"发送{label}失败（群 {group_id}）：{exc}")
        return False
    return True


async def send_group_forward(
    bot: Bot,
    group_id: int,
    texts: Sequence[str],
    *,
    node_name: str,
    label: str = "消息",
) -> bool:
    """以合并转发的聊天记录形式向群聊发送多段文本，返回是否发送成功。

    texts 的每一项为合并转发中的一条记录；节点内容用文本消息段构造，
    避免协议端把含 "[CQ:...]" 的文本解析成消息段；节点发送者固定为机器人。
    label 仅用于失败日志（如"道具详情"）。
    """
    nodes = [
        {
            "type": "node",
            "data": {
                "name": node_name,
                "uin": bot.self_id,
                "content": [{"type": "text", "data": {"text": text}}],
            },
        }
        for text in texts
    ]
    try:
        await bot.call_api("send_group_forward_msg", group_id=group_id, messages=nodes)
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"发送{label}失败（群 {group_id}）：{exc}")
        return False
    return True
