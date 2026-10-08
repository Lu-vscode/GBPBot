"""道具「分裂」组（编号 115~119，名称如「分裂（白）」）：白色普通至
金色传说、消耗型。

使用后消耗一定量的决斗分数，获得 3 件低一个品质的随机道具；
品质越高消耗的分数越多：

- 115 分裂（白）（白色普通）：消耗 5 决斗分数，获得 3 件灰色垃圾道具。
- 116 分裂（绿）（绿色精良）：消耗 15 决斗分数，获得 3 件白色普通道具。
- 117 分裂（蓝）（蓝色稀有）：消耗 50 决斗分数，获得 3 件绿色精良道具。
- 118 分裂（紫）（紫色史诗）：消耗 150 决斗分数，获得 3 件蓝色稀有道具。
- 119 分裂（金）（金色传说）：消耗 500 决斗分数，获得 3 件紫色史诗道具。
"""

from nonebot import logger

from src.plugins.item._framework import (
    ItemDefinition,
    ItemType,
    ItemUseContext,
    Quality,
    add_item,
    draw_item_of_quality,
    get_user_score,
    quality_label,
    register_item,
)

# 每次使用获得道具的数量
_PRODUCE_COUNT = 3

# 本组道具：编号、名称、品质、消耗的决斗分数、介绍、备注
_FISSION_ITEMS = (
    ("115", "分裂（白）", Quality.WHITE, 5, "碎成一堆垃圾", "意义不明"),
    ("116", "分裂（绿）", Quality.GREEN, 15, "获得几个普通道具", "防止道具过少"),
    ("117", "分裂（蓝）", Quality.BLUE, 50, "滋养一丛绿草", "生机勃勃"),
    ("118", "分裂（紫）", Quality.PURPLE, 150, "消耗大量决斗分数", "抑制通货膨胀"),
    ("119", "分裂（金）", Quality.GOLD, 500, "消耗海量决斗分数", "王者专属"),
)

# 各编号消耗的决斗分数：编号 -> 分数
_COSTS = {item_id: cost for item_id, _, _, cost, _, _ in _FISSION_ITEMS}


def _produce_quality(quality: Quality) -> Quality:
    """返回分裂产出道具的品质（比自身品质低一级）。"""
    return Quality(quality.value - 1)


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：决斗分数足够；返回错误文案时本次不消耗。"""
    cost = _COSTS[context.item.item_id]
    score = get_user_score(context.group_id, context.user_id)
    if score < cost:
        return (
            f"使用 {context.item.label} 需要消耗 {cost} 决斗分数，"
            f"你当前只有 {score} 分。"
        )
    return None


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：消耗决斗分数，获得 3 件低一个品质的随机道具。"""
    cost = _COSTS[context.item.item_id]
    produce_quality = _produce_quality(context.item.quality)
    context.add_score(-cost)
    gained: list[ItemDefinition] = []
    for _ in range(_PRODUCE_COUNT):
        item = draw_item_of_quality(produce_quality)
        if item is None:
            # 不可达：灰色至紫色品质的道具池恒非空，且注册表运行期不变
            logger.error(
                f"分裂抽取失败：{quality_label(produce_quality)}的道具池为空"
                f"（道具 {context.item.item_id}）"
            )
            break
        add_item(context.group_id, context.user_id, item.item_id)
        gained.append(item)
    # 按抽取顺序聚合同一件道具的显示文本（label 含编号，可作唯一键）
    counts: dict[str, int] = {}
    for item in gained:
        counts[item.label] = counts.get(item.label, 0) + 1
    gained_text = "、".join(f"{label}×{count}" for label, count in counts.items())
    await context.send(
        f"你使用了 {context.item.label}，消耗{cost}决斗分数，"
        f"分裂出{len(gained)}件{quality_label(produce_quality)}道具：{gained_text}。"
    )


def _register_fission_items() -> None:
    """注册本组的五件「分裂」道具。"""
    for item_id, name, quality, cost, description, note in _FISSION_ITEMS:
        produce_quality = _produce_quality(quality)
        register_item(
            ItemDefinition(
                item_id=item_id,
                name=name,
                quality=quality,
                types=(ItemType.CONSUMABLE,),
                description=description,
                effect=(
                    f"使用后消耗{cost}决斗分数，"
                    f"获得{_PRODUCE_COUNT}件随机{quality_label(produce_quality)}道具。"
                ),
                condition=f"决斗分数>={cost}",
                timing="任意",
                note=note,
                can_use=_can_use,
                handle_use=_handle_use,
            )
        )


_register_fission_items()
