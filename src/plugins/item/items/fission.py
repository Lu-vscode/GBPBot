"""道具「分裂」组（编号 115~119、126，名称如「分裂（白）」）：白色普通至
金色传说、消耗型，以及灰色垃圾的「分裂（灰）」。

使用后消耗一定量的决斗分数，获得 3 件低一个品质的随机道具；
品质越高消耗的分数越多。分裂（灰）例外：不消耗分数，获得 2 件随机
黑色诅咒道具；黑色诅咒不写入库存，获得后自动使用：

- 126 分裂（灰）（灰色垃圾）：获得 2 件随机黑色诅咒道具。
- 115 分裂（白）（白色普通）：消耗 5 决斗分数，获得 3 件灰色垃圾道具。
- 116 分裂（绿）（绿色精良）：消耗 15 决斗分数，获得 3 件白色普通道具。
- 117 分裂（蓝）（蓝色稀有）：消耗 50 决斗分数，获得 3 件绿色精良道具。
- 118 分裂（紫）（紫色史诗）：消耗 150 决斗分数，获得 3 件蓝色稀有道具。
- 119 分裂（金）（金色传说）：消耗 500 决斗分数，获得 3 件紫色史诗道具。
"""

from nonebot import logger

from src.plugins._shared.services import GrantedItem, ItemService, get_service
from src.plugins.item._framework import (
    ItemDefinition,
    ItemType,
    ItemUseContext,
    Quality,
    add_items,
    draw_item_of_quality,
    get_user_score,
    quality_label,
    register_item,
)

# 本组道具：编号、名称、品质、消耗的决斗分数、产出数量、介绍、备注
_FISSION_ITEMS = (
    ("126", "分裂（灰）", Quality.GRAY, 0, 2, "获得一堆诅咒", "自讨苦吃"),
    ("115", "分裂（白）", Quality.WHITE, 5, 3, "碎成一堆垃圾", "意义不明"),
    ("116", "分裂（绿）", Quality.GREEN, 15, 3, "获得几个普通道具", "防止道具过少"),
    ("117", "分裂（蓝）", Quality.BLUE, 50, 3, "滋养一丛绿草", "生机勃勃"),
    ("118", "分裂（紫）", Quality.PURPLE, 150, 3, "消耗大量决斗分数", "抑制通货膨胀"),
    ("119", "分裂（金）", Quality.GOLD, 500, 3, "消耗海量决斗分数", "王者专属"),
)

# 各编号消耗的决斗分数与产出道具数量：编号 -> 分数 / 数量
_COSTS = {item_id: cost for item_id, _, _, cost, _, _, _ in _FISSION_ITEMS}
_PRODUCE_COUNTS = {item_id: count for item_id, _, _, _, count, _, _ in _FISSION_ITEMS}


def _produce_quality(quality: Quality) -> Quality:
    """返回分裂产出道具的品质（比自身品质低一级）。"""
    return Quality(quality.value - 1)


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：有分数消耗的道具需决斗分数足够；返回错误文案时本次不消耗。"""
    cost = _COSTS[context.item.item_id]
    if not cost:
        return None
    score = get_user_score(context.group_id, context.user_id)
    if score < cost:
        return (
            f"使用 {context.item.label} 需要消耗 {cost} 决斗分数，"
            f"你当前只有 {score} 分。"
        )
    return None


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：消耗决斗分数，获得低一个品质的随机道具；
    产出的黑色诅咒不写入库存，改由道具服务自动使用。"""
    cost = _COSTS[context.item.item_id]
    produce_quality = _produce_quality(context.item.quality)
    if cost:
        context.add_score(-cost)
    produced: list[ItemDefinition] = []
    curses: list[ItemDefinition] = []
    gained_ids: list[str] = []
    for _ in range(_PRODUCE_COUNTS[context.item.item_id]):
        item = draw_item_of_quality(produce_quality)
        if item is None:
            # 不可达：黑色诅咒至紫色品质的道具池恒非空，且注册表运行期不变
            logger.error(
                f"分裂抽取失败：{quality_label(produce_quality)}的道具池为空"
                f"（道具 {context.item.item_id}）"
            )
            break
        if item.quality is Quality.BLACK_CURSE:
            # 黑色诅咒不写入库存：收集后统一按获得流程自动使用
            curses.append(item)
        else:
            gained_ids.append(item.item_id)
        produced.append(item)
    add_items(context.group_id, context.user_id, gained_ids)
    # 按抽取顺序聚合同一件道具的显示文本（label 含编号，可作唯一键）
    counts: dict[str, int] = {}
    for item in produced:
        counts[item.label] = counts.get(item.label, 0) + 1
    gained_text = "、".join(f"{label}×{count}" for label, count in counts.items())
    cost_text = f"消耗{cost}决斗分数，" if cost else ""
    await context.send(
        f"你使用了 {context.item.label}，{cost_text}"
        f"分裂出{len(produced)}件{quality_label(produce_quality)}道具：{gained_text}。"
    )
    if curses:
        await _auto_use_curses(context, curses)


async def _auto_use_curses(
    context: ItemUseContext, curses: list[ItemDefinition]
) -> None:
    """让分裂出的黑色诅咒走标准获得流程自动使用（信息播报与效果
    由道具服务完成，见 _shared/services.py 的 ItemService）。"""
    service = get_service(ItemService)
    if service is None:
        # 不可达：主模块在插件加载期注册道具服务，事件处理时必然已注册
        logger.error(
            f"分裂出的黑色诅咒无法自动使用（群 {context.group_id}，"
            f"成员 {context.user_id}）：道具服务未注册"
        )
        return
    for curse in curses:
        await service.handle_acquisition(
            context.bot,
            context.group_id,
            context.user_id,
            context.user_name,
            GrantedItem(item_id=curse.item_id, item_name=curse.name),
        )


def _register_fission_items() -> None:
    """注册本组的六件「分裂」道具。"""
    for item_id, name, quality, cost, count, description, note in _FISSION_ITEMS:
        produce_quality = _produce_quality(quality)
        cost_text = f"消耗{cost}决斗分数，" if cost else ""
        register_item(
            ItemDefinition(
                item_id=item_id,
                name=name,
                quality=quality,
                types=(ItemType.CONSUMABLE,),
                description=description,
                effect=(
                    f"使用后{cost_text}"
                    f"获得{count}件随机{quality_label(produce_quality)}道具。"
                ),
                condition=f"决斗分数>={cost}" if cost else "无",
                timing="任意",
                note=note,
                can_use=_can_use,
                handle_use=_handle_use,
            )
        )


_register_fission_items()
