"""决斗指令的公共参数解析与成员显示名。

被 @ 成员从命令参数中解析；消息末尾 @机器人 被 OneBot v11 适配器剥离的
情形由 parse_duel_target 兜底。显示名统一按显示名工具的默认长度（共享配置
NICKNAME_MAX_LENGTH）截断，保证各指令的展示一致。
"""

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message

from src.plugins._shared.onebot import member_display_name, sender_display_name


def parse_target(args: Message) -> int | None:
    """从命令参数中解析被 @ 的成员 QQ 号，无有效 @ 段时返回 None。"""
    for segment in args:
        if segment.type != "at":
            continue
        qq = str(segment.data.get("qq", ""))
        if qq.isdigit():
            return int(qq)
    return None


def parse_duel_target(args: Message, event: GroupMessageEvent) -> int | None:
    """解析 /duel 的被 @ 对象，消息末尾 @机器人 被适配器剥离时返回机器人自己。

    OneBot v11 适配器会把消息末尾的 @机器人（其后可跟一个纯空白文本段）
    当作呼叫机器人处理并连同尾随空白一起删除（见适配器 _check_at_me），
    此时参数中已找不到该 @ 段，需要根据剥离前的原始消息判断：若原始消息
    末尾正是 @机器人（可跟一个纯空白文本段），则发起对象为机器人自己。
    """
    target = parse_target(args)
    if target is not None:
        return target
    if not event.to_me:
        return None
    original = event.original_message
    if not original:
        return None
    last = original[-1]
    if (
        last.type == "text"
        and not str(last.data.get("text", "")).strip()
        and len(original) > 1
    ):
        # 与适配器一致：末尾是纯空白文本段时向前看一段
        last = original[-2]
    if last.type == "at" and str(last.data.get("qq", "")) == str(event.self_id):
        return int(event.self_id)
    return None


def sender_name(event: GroupMessageEvent) -> str:
    """返回发送者在群内的显示名（群昵称优先，过长时截断）。"""
    return sender_display_name(event)


async def member_name(bot: Bot, group_id: int, user_id: int) -> str:
    """获取群成员的显示名（群昵称优先，过长时截断）。"""
    return await member_display_name(bot, group_id, user_id)
