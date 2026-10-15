"""道具「五谷丰登」（编号 125）：白色普通、消耗型。

使用后所有决斗分数非零的群成员决斗分数 +1（含分数非零的使用者本人，
分数为 0 或没有记录的成员不受影响）；效果按决斗分数总榜（即全部非零
分数的成员）逐人加 1 分。
"""

from src.plugins.item._framework import (
    ItemDefinition,
    ItemType,
    ItemUseContext,
    Quality,
    add_user_score,
    get_rank_entries,
    register_item,
)


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：所有决斗分数非零的群成员决斗分数 +1。"""
    entries = get_rank_entries(context.group_id)
    if entries:
        for user_id, user_name, _ in entries:
            add_user_score(context.group_id, user_id, user_name, 1)
        await context.send(
            f"{context.user_name} 使用了 {context.item.label}，风调雨顺，国泰民安！\n"
            f"群内 {len(entries)} 名决斗分数非零的成员各 +1 分。"
        )
        return
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}，"
        "可惜群内没有决斗分数非零的成员，颗粒无收。"
    )


register_item(
    ItemDefinition(
        item_id="125",
        name="五谷丰登",
        quality=Quality.WHITE,
        types=(ItemType.CONSUMABLE,),
        description="五谷丰登",
        effect="使用后所有决斗分数非零的群成员决斗分数+1。",
        condition="无",
        timing="任意",
        note="给三国杀差评谢谢喵",
        handle_use=_handle_use,
    )
)
