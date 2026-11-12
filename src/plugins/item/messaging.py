"""道具消息的发送与命令参数解析：指令与交换共用的小工具。

本模块只处理与消息、参数相关的杂务（群昵称截断、@ 解析、道具显示名、
文本发送），不含业务语义；业务逻辑见 commands/ 与 exchange/。
"""

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message

from src.plugins._shared.onebot import (
    member_display_name,
    send_group_text,
    sender_display_name,
)
from src.plugins.item.core.registry import get_item

# 合成与交换指令需要提供的道具编号数量
ITEM_ARG_COUNT = 2


async def send_text(
    bot: Bot, group_id: int, text: str, reply_to: int | None = None
) -> None:
    """向群聊发送道具文本消息（发送失败仅记录日志）。"""
    await send_group_text(bot, group_id, text, reply_to, label="道具消息")


def item_label(item_id: str) -> str:
    """返回道具的显示名（"<编号> <名称>"）；编号未注册时回退为编号本身。"""
    definition = get_item(item_id)
    if definition is None:
        return item_id
    return definition.label


def parse_targets(args: Message) -> tuple[int, ...]:
    """从命令参数中解析全部被 @ 的成员 QQ 号（按出现顺序）。"""
    targets: list[int] = []
    for segment in args:
        if segment.type != "at":
            continue
        qq = str(segment.data.get("qq", ""))
        if qq.isdigit():
            targets.append(int(qq))
    return tuple(targets)


def _ends_with_at_self(message: Message, self_id: str) -> bool:
    """消息末尾（忽略其后的纯空白文本段）是否为 @机器人。"""
    for segment in reversed(message):
        if segment.type == "text" and not str(segment.data.get("text", "")).strip():
            continue
        return segment.type == "at" and str(segment.data.get("qq", "")) == self_id
    return False


def parse_use_targets(args: Message, event: GroupMessageEvent) -> tuple[int, ...]:
    """解析 /item.use 的全部 @ 使用对象，补回被适配器剥离的末尾 @机器人。

    OneBot v11 适配器会把消息末尾的 @机器人（其后可跟一个纯空白文本段）
    当作呼叫机器人处理，连同尾随空白一起删除（见适配器 _check_at_me），
    此时该 @ 段已不在命令参数中。若原始消息末尾是 @机器人、而参数末尾
    没有对应的 @ 段，说明它被剥离了：把机器人自己补为最后一个使用对象
    （消息开头的 @机器人 属于呼叫、本来就会被剥离，不在此补回）。
    是否允许以机器人为使用对象由各道具自行校验。
    """
    targets = parse_targets(args)
    self_id = str(event.self_id)
    if not _ends_with_at_self(event.original_message, self_id):
        return targets
    if _ends_with_at_self(args, self_id):
        # 未被剥离（如消息开头已 @机器人，适配器不再检查末尾），已在参数中
        return targets
    return (*targets, int(event.self_id))


def parse_target(args: Message) -> int | None:
    """从命令参数中解析被 @ 的成员 QQ 号，无有效 @ 段时返回 None。"""
    targets = parse_targets(args)
    return targets[0] if targets else None


def sender_name(event: GroupMessageEvent) -> str:
    """返回发送者的群昵称（超过共享配置的最大长度时截断）。"""
    return sender_display_name(event)


async def member_name(bot: Bot, group_id: int, user_id: int) -> str:
    """返回指定成员的群昵称（超过共享配置的最大长度时截断）。"""
    return await member_display_name(bot, group_id, user_id)
