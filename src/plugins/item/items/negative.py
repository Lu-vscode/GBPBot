"""道具「负号」组（编号 044、106、107、444）：由给决斗分数加负号衍生的道具。

- 044 自刎归天（灰色垃圾、消耗型）：使自己的决斗分数变为原始分数的负绝对值。
- 106 反转（紫色史诗、消耗型）：使自己的决斗分数变为相反数。
- 107 涅槃（蓝色稀有、消耗型）：50% 的概率使自己的决斗分数变为原始分数的
  绝对值，失败无副作用；仅当自己位列决斗低分榜第一名时可用。
- 444 阎王（蓝色稀有、可选型）：使用 /item.use 444 @成员 使该成员的决斗
  分数变为其原始分数的负绝对值；仅当自己位列决斗低分榜第一名时可用。
"""

import random

from src.plugins._shared.onebot import member_display_name
from src.plugins.item._framework import (
    ItemDefinition,
    ItemType,
    ItemUseContext,
    Quality,
    add_user_score,
    get_lowest_score_user,
    get_user_score,
    register_item,
)

# 消息中群昵称的最大显示长度（字符数，与主模块保持一致）
_NICKNAME_MAX_LENGTH = 12

# 涅槃的生效概率
_NIRVANA_CHANCE = 0.5


def _lowest_only_error(context: ItemUseContext) -> str | None:
    """校验使用者位列决斗低分榜第一名，不满足时返回错误文案。"""
    if get_lowest_score_user(context.group_id) == context.user_id:
        return None
    return f"只有位列决斗低分榜第一名的成员才能使用 {context.item.label}。"


async def _handle_suicide(context: ItemUseContext) -> None:
    """自刎归天：使自己的决斗分数变为原始分数的负绝对值。"""
    old = get_user_score(context.group_id, context.user_id)
    new = -abs(old)
    if new == old:
        await context.send(f"你使用了 {context.item.label}，你的决斗分数没有变化。")
        return
    context.add_score(new - old)
    await context.send(
        f"你使用了 {context.item.label}，你的决斗分数从 {old} 变为 {new}。"
    )


async def _handle_reverse(context: ItemUseContext) -> None:
    """反转：使自己的决斗分数变为相反数。"""
    old = get_user_score(context.group_id, context.user_id)
    if old == 0:
        await context.send(
            f"你使用了 {context.item.label}，但你的决斗分数为 0，什么也没有发生。"
        )
        return
    context.add_score(-2 * old)
    await context.send(
        f"你使用了 {context.item.label}，你的决斗分数从 {old} 反转为 {-old}。"
    )


def _can_use_nirvana(context: ItemUseContext) -> str | None:
    """涅槃的使用前校验：仅位列决斗低分榜第一名时可用。"""
    return _lowest_only_error(context)


async def _handle_nirvana(context: ItemUseContext) -> None:
    """涅槃：50% 的概率使自己的决斗分数变为原始分数的绝对值。"""
    old = get_user_score(context.group_id, context.user_id)
    if random.random() >= _NIRVANA_CHANCE:
        await context.send(
            f"你使用了 {context.item.label}，涅槃失败。亡者不亡，你的决斗分数没有变化。"
        )
        return
    context.add_score(abs(old) - old)
    await context.send(
        f"你使用了 {context.item.label}，涅槃重生！"
        f"你的决斗分数从 {old} 变为 {abs(old)}。"
    )


def _can_use_yama(context: ItemUseContext) -> str | None:
    """阎王的使用前校验：参数与低分榜条件；返回错误文案时本次不消耗。"""
    if context.args or context.target_id is None:
        return f"请 @ 一位群成员，用法：/item.use {context.item.item_id} @群成员"
    if context.target_id == context.user_id:
        return f"不能对自己使用 {context.item.label}。"
    return _lowest_only_error(context)


async def _handle_yama(context: ItemUseContext) -> None:
    """阎王：使被 @ 成员的决斗分数变为其原始分数的负绝对值。"""
    if context.target_id is None:
        # 不可达：can_use 已校验目标存在
        return
    target_id = context.target_id
    target_name = await member_display_name(
        context.bot, context.group_id, target_id, _NICKNAME_MAX_LENGTH
    )
    old = get_user_score(context.group_id, target_id)
    new = -abs(old)
    if new == old:
        await context.send(
            f"你使用了 {context.item.label}，{target_name} 的决斗分数没有变化。"
        )
        return
    add_user_score(context.group_id, target_id, target_name, new - old)
    await context.send(
        f"你使用了 {context.item.label}，{target_name} 的决斗分数从 {old} 变为 {new}。"
    )


register_item(
    ItemDefinition(
        item_id="044",
        name="自刎归天",
        quality=Quality.GRAY,
        types=(ItemType.CONSUMABLE,),
        description="死是凉爽的夏夜。",
        effect="使用后使自己的决斗分数变为原始分数的负绝对值。",
        condition="无",
        timing="任意",
        note="你死了，我们吃什么。",
        handle_use=_handle_suicide,
    )
)

register_item(
    ItemDefinition(
        item_id="106",
        name="反转",
        quality=Quality.PURPLE,
        types=(ItemType.CONSUMABLE,),
        description="反转自己的决斗分数。",
        effect="使用后使自己的决斗分数变为相反数。",
        condition="无",
        timing="任意",
        note="一念神魔",
        handle_use=_handle_reverse,
    )
)

register_item(
    ItemDefinition(
        item_id="107",
        name="涅槃",
        quality=Quality.BLUE,
        types=(ItemType.CONSUMABLE,),
        description="涅槃重生",
        effect="使用后有50%的概率使自己的决斗分数变为原始分数的绝对值。",
        condition="自身位列决斗低分榜第一名",
        timing="任意",
        note="亡者不亡",
        can_use=_can_use_nirvana,
        handle_use=_handle_nirvana,
    )
)

register_item(
    ItemDefinition(
        item_id="444",
        name="阎王",
        quality=Quality.BLUE,
        types=(ItemType.OPTIONAL,),
        description="我从地狱归来。",
        effect=(
            '使用指令"/item.use 444 @成员"将该成员的决斗分数变为其原始分数的负绝对值。'
        ),
        condition="自身位列决斗低分榜第一名",
        timing="任意",
        note="小心线下真人快打。",
        accepts_args=True,
        can_use=_can_use_yama,
        handle_use=_handle_yama,
    )
)
