"""OneBot v11 群聊消息与成员信息共享工具。

供各插件复用的通用发送/显示辅助函数（与具体插件业务无关）：
- 群聊文本消息发送（纯文本段构造，避免文案被解析为 CQ 码）
- 群聊合并转发消息发送（节点内容为文本消息段数组）
- 群成员显示名获取与截断（群昵称优先，过长时截断）

本模块属于以 "_" 开头的共享代码包，不会被 NoneBot 当作插件加载；
插件中通过 `from src.plugins._shared.onebot import ...` 导入。
"""

from collections.abc import Sequence

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError


def truncate_name(name: str, max_length: int) -> str:
    """截断过长的群昵称：被截断的部分以"…"代替，总长不超过上限。"""
    if len(name) <= max_length:
        return name
    return name[: max_length - 1] + "…"


def sender_display_name(event: GroupMessageEvent, max_length: int) -> str:
    """返回发送者在群内的显示名（群昵称优先，否则 QQ 昵称），过长时截断。"""
    card = (event.sender.card or "").strip()
    nickname = (event.sender.nickname or "").strip()
    name = card or nickname
    if not name:
        return f"QQ {event.user_id}"
    return truncate_name(name, max_length)


async def member_display_name(
    bot: Bot, group_id: int, user_id: int, max_length: int
) -> str:
    """获取群成员的显示名（群昵称优先，过长时截断），查询失败时回退为 QQ 号。"""
    try:
        member = await bot.call_api(
            "get_group_member_info",
            group_id=group_id,
            user_id=user_id,
            no_cache=True,
        )
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"获取群 {group_id} 成员 {user_id} 的资料失败：{exc}")
        return f"QQ {user_id}"
    card = str((member or {}).get("card") or "").strip()
    nickname = str((member or {}).get("nickname") or "").strip()
    name = card or nickname
    if not name:
        return f"QQ {user_id}"
    return truncate_name(name, max_length)


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
