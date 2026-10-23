"""道具「狂区之怒」（编号 679）：灰色垃圾、可选型。

使用 /item.use 679 @使用对象 使其获得黑色诅咒道具「！？区区？！」
（编号 678）并按诅咒获得流程自动使用：获得持续 1 天的“区”状态。
使用条件为自己拥有“区”状态；可以对自己使用，不能对机器人使用。

678 的获得流程经框架的 acquire_curse 执行：先发送黑色诅咒的完整信息，
再询问黑色诅咒拦截器（如「坚如磐石」的护盾）；被拦截时对方失去该诅咒、
效果不执行，本次使用不退还本道具。
"""

from nonebot import logger

from src.plugins._shared.config import NICKNAME_MAX_LENGTH, get_bot_list
from src.plugins._shared.onebot import member_display_name
from src.plugins.item._framework import (
    ItemDefinition,
    ItemType,
    ItemUseContext,
    Quality,
    acquire_curse,
    add_item,
    get_item,
    get_state,
    register_item,
)

# 道具编号
_ITEM_ID = "679"

# 被转赠的黑色诅咒道具编号（定义见 worm.py）
_CURSE_ID = "678"

# “区”状态的状态键（由「！？区区？！」678 产生，见 worm.py）
_CURSE_STATE_KEY = "curse.678"

# 消息中群昵称的最大显示长度（字符数，共享配置 NICKNAME_MAX_LENGTH）
_NICKNAME_MAX_LENGTH = NICKNAME_MAX_LENGTH

# 参数不合法的提示文案（使用对象须以 @ 形式给出）
_USAGE = f"请 @ 一位群成员，用法：/item.use {_ITEM_ID} @群成员"


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：@ 使用对象、自身的“区”状态与目标；返回错误文案时本次不消耗。"""
    target_id = context.target_id
    if context.args or target_id is None:
        return _USAGE
    if get_state(context.group_id, context.user_id, _CURSE_STATE_KEY) is None:
        return f"只有拥有“区”状态的成员才能使用 {context.item.label}。"
    if target_id in get_bot_list() or context.bot.self_id == str(target_id):
        return f"不能对机器人使用 {context.item.label}。"
    return None


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：让被 @ 的成员获得 678 并按诅咒获得流程自动使用。"""
    target_id = context.target_id
    if target_id is None:
        # 不可达：can_use 已校验参数与目标存在
        return
    curse = get_item(_CURSE_ID)
    if curse is None:
        # 不可达：678 与本道具由同一道具包注册；注册缺失时退还本道具
        logger.error(
            f"狂区之怒：道具 {_CURSE_ID} 未注册，已退还"
            f"（群 {context.group_id}，成员 {context.user_id}）"
        )
        add_item(context.group_id, context.user_id, context.item.item_id)
        await context.send(f"使用失败：道具 {_CURSE_ID} 不存在，道具已退还。")
        return
    if target_id == context.user_id:
        target_name = context.user_name
    else:
        target_name = await member_display_name(
            context.bot, context.group_id, target_id, _NICKNAME_MAX_LENGTH
        )
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}。\n"
        f"{target_name} 的周围响起了扭曲的蠕动与阴暗的爬行声……"
    )
    await acquire_curse(context.bot, context.group_id, target_id, target_name, curse)


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="狂区之怒",
        quality=Quality.GRAY,
        types=(ItemType.OPTIONAL,),
        description="损人不利己",
        effect=f'使用"/item.use {_ITEM_ID} @使用对象"使其获得"678 ！？区区？！"',
        condition="自己拥有“区”状态",
        timing="任意",
        note="扭曲的蠕动，阴暗的爬行！",
        accepts_args=True,
        can_use=_can_use,
        handle_use=_handle_use,
    )
)
