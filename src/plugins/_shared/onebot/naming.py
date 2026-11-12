"""群成员显示名：群昵称优先，过长时截断。

截断长度默认为共享配置 NICKNAME_MAX_LENGTH（见 _shared/config/nickname.py），
调用方一般不传长度；仅在需要与共享配置不同的长度时显式传入。
"""

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError

from src.plugins._shared.config.nickname import NICKNAME_MAX_LENGTH


def truncate_name(name: str, max_length: int | None = None) -> str:
    """截断过长的群昵称：被截断的部分以"…"代替，总长不超过上限。

    max_length 为 None 时使用共享配置 NICKNAME_MAX_LENGTH。
    """
    limit = NICKNAME_MAX_LENGTH if max_length is None else max_length
    if len(name) <= limit:
        return name
    return name[: limit - 1] + "…"


def sender_display_name(event: GroupMessageEvent, max_length: int | None = None) -> str:
    """返回发送者在群内的显示名（群昵称优先，否则 QQ 昵称），过长时截断。

    max_length 为 None 时使用共享配置 NICKNAME_MAX_LENGTH。
    """
    card = (event.sender.card or "").strip()
    nickname = (event.sender.nickname or "").strip()
    name = card or nickname
    if not name:
        return f"QQ {event.user_id}"
    return truncate_name(name, max_length)


async def member_display_name(
    bot: Bot, group_id: int, user_id: int, max_length: int | None = None
) -> str:
    """获取群成员的显示名（群昵称优先，过长时截断），查询失败时回退为 QQ 号。

    max_length 为 None 时使用共享配置 NICKNAME_MAX_LENGTH。
    """
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
