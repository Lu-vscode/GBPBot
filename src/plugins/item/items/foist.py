"""道具「强塞」（编号 127）：灰色垃圾、可选型。

使用 /item.use 127 @使用对象 <道具编号> 把自己拥有的一件品质不高于
紫色史诗的道具强行塞给对方：自己失去该道具、使用对象获得该道具。
「强塞」本身不能被强塞，也不能塞给机器人或自己。

耐久型道具的耗损副本同样可以强塞：转移时把最早开始耗损的副本（即
详情中显示剩余耐久的那一件）连同剩余耐久一并转给使用对象，见
_durability.move_damaged_instance。
"""

from nonebot import logger

from src.plugins._shared.config import NICKNAME_MAX_LENGTH, get_bot_list
from src.plugins._shared.onebot import member_display_name
from src.plugins.item._framework import (
    ItemDefinition,
    ItemType,
    ItemUseContext,
    Quality,
    add_item,
    get_item,
    get_item_count,
    is_item_id,
    quality_label,
    register_item,
    remove_item,
)
from src.plugins.item.items._durability import (
    durable_state_key,
    move_damaged_instance,
)

# 道具编号与可强塞的品质上限
_ITEM_ID = "127"
_MAX_QUALITY = Quality.PURPLE

# 消息中群昵称的最大显示长度（字符数，共享配置 NICKNAME_MAX_LENGTH）
_NICKNAME_MAX_LENGTH = NICKNAME_MAX_LENGTH

# 用法与错误提示文案
_USAGE = f"请 @ 一位群成员并提供道具编号，用法：/item.use {_ITEM_ID} @群成员 <道具编号>"


def _target_error(context: ItemUseContext, target_id: int) -> str | None:
    """校验被 @ 的成员：不能是自己、也不能是机器人。"""
    if target_id == context.user_id:
        return f"不能对自己使用 {context.item.label}。"
    if str(target_id) == context.bot.self_id or target_id in get_bot_list():
        return f"不能向机器人使用 {context.item.label}。"
    return None


def _item_error(context: ItemUseContext, item_id: str) -> str | None:
    """校验要强塞的道具：存在、自己拥有、可强塞的品质；返回错误文案或 None。"""
    if item_id == _ITEM_ID:
        return f"不能强塞 {context.item.label} 本身。"
    target = get_item(item_id)
    if target is None:
        return f"道具 {item_id} 不存在。"
    if get_item_count(context.group_id, context.user_id, item_id) < 1:
        return f"你未拥有道具 {target.label}。"
    if target.quality is Quality.BLACK_CURSE:
        return "黑色诅咒无法强塞。"
    if target.quality > _MAX_QUALITY:
        return (
            f"{context.item.label} 只能强塞品质不高于"
            f"{quality_label(_MAX_QUALITY)}的道具，无法强塞 {target.label}。"
        )
    return None


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：@ 对象与要强塞的道具；返回错误文案时本次不消耗。"""
    target_id = context.target_id
    if target_id is None or len(context.args) != 1 or not is_item_id(context.args[0]):
        return _USAGE
    error = _target_error(context, target_id)
    if error is not None:
        return error
    return _item_error(context, context.args[0])


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：把自己的一件道具（含耗损副本的剩余耐久）转移给使用对象。"""
    target_id = context.target_id
    if target_id is None or len(context.args) != 1:
        return  # 不可达：can_use 已校验参数与目标
    target = get_item(context.args[0])
    if target is None:  # 不可达：can_use 已校验目标道具存在
        return
    if not remove_item(context.group_id, context.user_id, target.item_id):
        # 不可达：校验、消耗与效果同处同步段，库存不会在此期间改变
        logger.error(
            f"强塞：道具 {target.label} 未能从库存移除，{context.item.label} 已退还"
            f"（群 {context.group_id}，成员 {context.user_id}）"
        )
        add_item(context.group_id, context.user_id, _ITEM_ID)
        await context.send(f"使用失败：道具状态异常，{context.item.label} 已退还。")
        return
    add_item(context.group_id, target_id, target.item_id)
    remaining = None
    state_key = durable_state_key(target.item_id)
    if state_key is not None:
        remaining = move_damaged_instance(
            context.group_id,
            context.user_id,
            target_id,
            item_id=target.item_id,
            state_key=state_key,
        )
    target_name = await member_display_name(
        context.bot, context.group_id, target_id, _NICKNAME_MAX_LENGTH
    )
    lines = [
        f"{context.user_name} 使用了 {context.item.label}，"
        f"把 {target.label} 强行塞给了 {target_name}：白送的东西，不要也得要。"
    ]
    if remaining is not None:
        lines.append(f"{target_name} 获得的副本剩余耐久 {remaining}。")
    await context.send("\n".join(lines))


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="强塞",
        quality=Quality.GRAY,
        types=(ItemType.OPTIONAL,),
        description="强行把自己的道具塞给别人",
        effect=(
            f'使用"/item.use {_ITEM_ID} @使用对象 <道具编号>"使自己失去指定的'
            f"品质不高于{quality_label(_MAX_QUALITY)}的道具，使用对象获得该道具。"
        ),
        condition="无",
        timing="任意",
        note="为什么会有人白送都不要？",
        accepts_args=True,
        can_use=_can_use,
        handle_use=_handle_use,
    )
)
