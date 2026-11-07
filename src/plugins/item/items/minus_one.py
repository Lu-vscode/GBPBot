"""道具「-1」（编号 110）：黑色诅咒、诅咒型。

获得后不进入库存，由主模块先发送完整信息、随后自动使用：决斗分数 -1。
"""

from src.plugins.item._framework import (
    ItemBehavior,
    ItemDefinition,
    ItemUseContext,
    Quality,
    register_item,
)


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：决斗分数 -1。"""
    await context.add_score(-1)
    await context.send("黑色诅咒生效，你的决斗分数-1。")


register_item(
    ItemDefinition(
        item_id="110",
        name="-1",
        quality=Quality.BLACK_CURSE,
        description="-1",
        effect="决斗分数-1。",
        timing="自动使用",
        note="-1",
        behavior=ItemBehavior(
            use=_handle_use,
        ),
    )
)
