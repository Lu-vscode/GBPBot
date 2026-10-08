"""道具「复制」组（编号 100~105，名称如「复制（金）」）：金色传说至
灰色垃圾、可选型。

使用 /item.use <复制道具编号> <需要复制的道具编号> 复制一件自己拥有的、
品质不高于该复制道具品质的道具；由备注可知复制道具自身也允许（先消耗
一件再复制一件，数量不变）。六件道具的差异仅在品质与名称（颜色后缀）：
编号越大品质越低、可复制的品质上限越低。
"""

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
)

# 本组道具：编号 -> 品质与名称（名称带品质颜色后缀；品质同时是可复制的上限）
_COPY_ITEMS = (
    ("100", Quality.GOLD, "复制（金）"),
    ("101", Quality.PURPLE, "复制（紫）"),
    ("102", Quality.BLUE, "复制（蓝）"),
    ("103", Quality.GREEN, "复制（绿）"),
    ("104", Quality.WHITE, "复制（白）"),
    ("105", Quality.GRAY, "复制（灰）"),
)


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：参数与要复制的目标道具；返回错误文案时本次不消耗。"""
    if len(context.args) != 1 or not is_item_id(context.args[0]):
        return (
            f"请提供需要复制的道具编号，用法：/item.use {context.item.item_id} "
            "<需要复制的道具编号>"
        )
    target_id = context.args[0]
    target = get_item(target_id)
    if target is None:
        return f"道具 {target_id} 不存在。"
    if get_item_count(context.group_id, context.user_id, target_id) < 1:
        return f"你未拥有道具 {target.label}。"
    if target.quality > context.item.quality:
        return (
            f"{context.item.label} 只能复制品质不高于"
            f"{quality_label(context.item.quality)}的道具，无法复制 {target.label}。"
        )
    return None


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：把一件目标道具复制进使用者库存。"""
    target = get_item(context.args[0])
    if target is None:  # 不可达：can_use 已校验目标道具存在
        return
    add_item(context.group_id, context.user_id, target.item_id)
    await context.send(f"你使用了 {context.item.label}，成功复制出 {target.label}。")


def _register_copy_items() -> None:
    """注册本组的六件「复制」道具。"""
    for item_id, quality, name in _COPY_ITEMS:
        register_item(
            ItemDefinition(
                item_id=item_id,
                name=name,
                quality=quality,
                types=(ItemType.OPTIONAL,),
                description="复制道具。",
                effect=(
                    f'使用指令"/item.use {item_id} <需要复制的道具编号>"'
                    f"复制一个自己拥有的品质不高于{quality_label(quality)}的道具。"
                ),
                condition="无",
                timing="任意",
                note=f"如果你足够无聊，可以尝试复制道具{item_id}",
                accepts_args=True,
                can_use=_can_use,
                handle_use=_handle_use,
            )
        )


_register_copy_items()
