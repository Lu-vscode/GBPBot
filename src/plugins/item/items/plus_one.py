"""道具「+1」（编号 111）：灰色垃圾、消耗型。

使用后决斗分数 +1；作为最基础的道具，被开发用作道具框架的参考实现。
"""

from src.plugins.item._framework import (
    ItemDefinition,
    ItemType,
    ItemUseContext,
    Quality,
    register_item,
)


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：决斗分数 +1。"""
    context.add_score(1)
    await context.send(f"你使用了 {context.item.label}，决斗分数+1。")


register_item(
    ItemDefinition(
        item_id="111",
        name="+1",
        quality=Quality.GRAY,
        types=(ItemType.CONSUMABLE,),
        description="+1",
        effect="决斗分数+1。",
        condition="无",
        timing="任意",
        note="+1",
        handle_use=_handle_use,
    )
)
