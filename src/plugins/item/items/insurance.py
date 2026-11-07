"""道具「保险」组（编号 132~136）：灰色垃圾至紫色史诗各一件、复合型
（可选型与耐久型的复合）。

使用 /item.use <保险编号> <道具1编号> <道具2编号> 合成两件与保险同品质的
道具：合成产物与概率和默认合成（/item.craft）完全相同，两者共用
_craft.py 的抽取与材料校验。合成失败（抽到低一级品质）时，重新获得
消耗的两件材料、失败的产物照常保留；合成成功与大成功不返还材料。
保险道具自身不能作为本组的合成材料（否则耐久消耗与材料移除会对同一
副本重复结算，见 _can_use）。

耐久 3/3：每使用一次对已耗损副本扣 1 点耐久（归零时道具消失），没有
耗损副本时开始耗损一件全新副本（库存数量不变，耐久记录进入状态）；
未使用过的副本可正常交换/合成，已耗损的副本不可。
"""

from nonebot import logger

from src.plugins.item._craft import FAILED_PREFIX, craft_error, draw_craft_results
from src.plugins.item._durability import durable_behavior
from src.plugins.item._framework import (
    ItemDefinition,
    ItemKind,
    ItemUseContext,
    Quality,
    acquire_curse,
    add_items,
    get_item,
    is_item_id,
    quality_label,
    register_item,
    remove_items,
)

# 本组道具编号 -> 品质（编号按设计文档顺序由低到高分配：132 灰 ~ 136 紫）
_INSURANCE_QUALITIES: dict[str, Quality] = {
    "132": Quality.GRAY,
    "133": Quality.WHITE,
    "134": Quality.GREEN,
    "135": Quality.BLUE,
    "136": Quality.PURPLE,
}

# 名称中品质对应的颜色后缀
_QUALITY_COLORS = {
    Quality.GRAY: "灰",
    Quality.WHITE: "白",
    Quality.GREEN: "绿",
    Quality.BLUE: "蓝",
    Quality.PURPLE: "紫",
}

# 每件保险的总耐久与合成材料数量（两件）
_DURABILITY_TOTAL = 3
_ARG_COUNT = 2


def _usage(item_id: str) -> str:
    """返回使用指令的用法提示。"""
    return f"请提供两个道具编号，用法：/item.use {item_id} <道具编号1> <道具编号2>"


def _materials(args: tuple[str, ...]) -> tuple[ItemDefinition, ItemDefinition] | None:
    """按参数取两件合成材料的定义（数量不符或编号未注册时返回 None）。"""
    if len(args) != _ARG_COUNT:
        return None
    first = get_item(args[0])
    second = get_item(args[1])
    if first is None or second is None:
        return None
    return first, second


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：两个道具编号、与本保险同品质、数量足够且可交易；
    返回错误文案时本次不消耗。"""
    args = context.args
    if len(args) != _ARG_COUNT or not all(is_item_id(arg) for arg in args):
        return _usage(context.item.item_id)
    first = get_item(args[0])
    second = get_item(args[1])
    if first is None or second is None:
        missing = args[0] if first is None else args[1]
        return f"道具 {missing} 不存在。"
    if context.item.item_id in (first.item_id, second.item_id):
        return f"{context.item.label} 不能作为合成材料。"
    if (
        first.quality is not context.item.quality
        or second.quality is not context.item.quality
    ):
        return (
            f"{context.item.label} 只能合成两件"
            f"{quality_label(context.item.quality)}道具，"
            f"{first.label}、{second.label} 不符合要求。"
        )
    return craft_error(context.group_id, context.user_id, first, second)


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：按默认合成的产物与概率合成两件材料，失败时返还材料。"""
    materials = _materials(context.args)
    if materials is None:
        # 不可达：can_use 已校验参数与编号存在
        logger.error(
            f"保险：合成材料参数异常（群 {context.group_id}，"
            f"成员 {context.user_id}）：{context.args}"
        )
        await context.send("使用失败：合成材料无法识别。")
        return
    first, second = materials
    drawn = draw_craft_results(context.item.quality)
    if drawn is None:
        # 不可达：灰色至紫色的道具池均非空
        logger.error(
            "保险：合成结果抽取失败，合成材料未消耗"
            f"（群 {context.group_id}，成员 {context.user_id}）"
        )
        await context.send("使用失败：目标品质的道具池为空，合成材料未消耗。")
        return
    prefix, results = drawn
    # 同步段：消耗材料并登记产物（黑色诅咒不写入库存，走诅咒获得流程）
    remove_items(
        context.group_id,
        context.user_id,
        [(first.item_id, 1), (second.item_id, 1)],
    )
    add_items(
        context.group_id,
        context.user_id,
        [
            result.item_id
            for result in results
            if result.quality is not Quality.BLACK_CURSE
        ],
    )
    result_labels = "、".join(result.label for result in results)
    material_labels = f"{first.label}、{second.label}"
    if prefix == FAILED_PREFIX:
        # 合成失败：返还两件材料，失败的产物保留
        add_items(context.group_id, context.user_id, [first.item_id, second.item_id])
        await context.send(
            f"{context.user_name} 使用了 {context.item.label}，{prefix}，"
            f"获得了 {result_labels}。\n"
            f"{context.item.name}生效：消耗的 {material_labels} 已返还。"
        )
    else:
        await context.send(
            f"{context.user_name} 使用了 {context.item.label}，{prefix}，"
            f"消耗了 {material_labels}，获得了 {result_labels}。"
        )
    for result in results:
        if result.quality is Quality.BLACK_CURSE:
            await acquire_curse(
                context.bot,
                context.group_id,
                context.user_id,
                context.user_name,
                result,
            )


# 本组五件道具的耐久行为完全相同（消耗、可交易数量与详情耐久），
# 共用同一个行为块；校验与效果按道具自身属性（品质）区分
_BEHAVIOR = durable_behavior(
    total=_DURABILITY_TOTAL,
    check=_can_use,
    use=_handle_use,
)

for _item_id, _quality in _INSURANCE_QUALITIES.items():
    register_item(
        ItemDefinition(
            item_id=_item_id,
            name=f"保险（{_QUALITY_COLORS[_quality]}）",
            quality=_quality,
            kind=ItemKind.COMPOSITE,
            description="让合成不再提心吊胆",
            effect=(
                f'使用"/item.use {_item_id} <道具1编号> <道具2编号>"'
                f"合成两件{quality_label(_quality)}道具，合成产物和概率与默认"
                "合成相同。若合成失败，重新获得合成消耗的两个道具，"
                "合成产物不回收。"
            ),
            note="保险不是免费的",
            durability=f"{_DURABILITY_TOTAL}/{_DURABILITY_TOTAL}",
            accepts_args=True,
            behavior=_BEHAVIOR,
        )
    )
