"""合成的公共逻辑：合成概率、结果抽取与材料校验。

/item.craft 指令与「保险」道具组（items/insurance.py）共用本模块，保证
两处的合成产物、概率与材料校验完全一致。合成概率由主模块解析
ITEM_CRAFT_* 配置后经 set_craft_odds 注入，未注入时使用默认概率
（见 DEFAULT_CRAFT_ODDS）。

品质运算见 shift_quality：灰色垃圾的"低一级"为黑色诅咒，紫色史诗的
"高两级"改为获得两件金色传说（见 draw_craft_results）。
"""

import random

from src.plugins.item._framework import (
    ItemDefinition,
    Quality,
    draw_item_of_quality,
    get_item_count,
    transferable_count,
)

# 合成概率的默认值：品质低一级 / 高一级 / 高两级
DEFAULT_CRAFT_ODDS = (0.24, 0.75, 0.01)

# 合成结果文案的前缀：抽到低一级品质即"合成失败"
FAILED_PREFIX = "合成失败"
SUCCESS_PREFIX = "合成成功"
GREAT_SUCCESS_PREFIX = "合成大成功"

# 当前生效的合成概率（低一级、高一级、高两级）
_craft_odds: list[float] = list(DEFAULT_CRAFT_ODDS)


def set_craft_odds(odds: tuple[float, float, float]) -> None:
    """设置合成概率（主模块解析 ITEM_CRAFT_* 配置后调用）。"""
    _craft_odds[:] = odds


def shift_quality(quality: Quality, delta: int) -> Quality:
    """按档位升降品质，并夹取到合法品质范围。

    库存中出现黑色诅咒等异常数据时，品质运算不得越界（否则构造
    Quality 会抛 ValueError）；夹取后仍保持原有合成语义（如灰色
    “低一级”得到黑色诅咒）。
    """
    value = max(
        Quality.BLACK_CURSE.value,
        min(Quality.GOLD.value, quality.value + delta),
    )
    return Quality(value)


def _draw_one(prefix: str, quality: Quality) -> tuple[str, list[ItemDefinition]] | None:
    """从指定品质的池中抽一件作为合成结果；池为空时返回 None。"""
    item = draw_item_of_quality(quality)
    if item is None:
        return None
    return prefix, [item]


def draw_craft_results(quality: Quality) -> tuple[str, list[ItemDefinition]] | None:
    """抽取合成结果（结果文案前缀与获得的道具列表）；目标品质的道具池
    为空时返回 None。

    结果前缀按抽取档位为"合成失败"（低一级）、"合成成功"（高一级）与
    "合成大成功"（高两级）；紫色史诗"高两级"超出最高品质时改为获得
    两件金色传说。
    """
    down, up = _craft_odds[0], _craft_odds[1]
    roll = random.random()
    if roll < down:
        return _draw_one(FAILED_PREFIX, shift_quality(quality, -1))
    if roll < down + up:
        return _draw_one(SUCCESS_PREFIX, shift_quality(quality, 1))
    if quality is Quality.PURPLE:
        first = draw_item_of_quality(Quality.GOLD)
        second = draw_item_of_quality(Quality.GOLD)
        if first is None or second is None:
            return None
        return GREAT_SUCCESS_PREFIX, [first, second]
    return _draw_one(GREAT_SUCCESS_PREFIX, shift_quality(quality, 2))


async def craft_error(
    group_id: int, user_id: int, first: ItemDefinition, second: ItemDefinition
) -> str | None:
    """校验两件合成材料（品质相同、非金色、数量足够且可交易），
    返回错误文案或 None。"""
    if first.quality != second.quality:
        return f"{first.label} 与 {second.label} 的品质不同，无法合成。"
    if first.quality is Quality.GOLD:
        return "金色传说道具无法合成。"
    if first.quality is Quality.BLACK_CURSE:
        return "黑色诅咒无法作为合成材料。"
    needed = 2 if first.item_id == second.item_id else 1
    if first.item_id == second.item_id:
        need_text = f"{first.label}×2"
    else:
        need_text = f"{first.label}、{second.label} 各一件"
    if (
        await get_item_count(group_id, user_id, first.item_id) < needed
        or await get_item_count(group_id, user_id, second.item_id) < needed
    ):
        return f"合成需要 {need_text}，你的数量不足。"
    for definition in (first, second):
        if await transferable_count(group_id, user_id, definition.item_id) < needed:
            return f"合成需要 {need_text}，但 {definition.label} 目前无法用于合成。"
    return None
