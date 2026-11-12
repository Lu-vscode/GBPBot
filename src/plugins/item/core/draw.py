"""随机抽取道具：品质权重与品质池抽取。

抽取品质的权重由配置注入（见 config/resolve.py 的 DRAW_WEIGHTS 与
item/__init__.py 的注入），抽到品质后再从该品质的道具池中等概率抽取。
"""

import random

from nonebot import logger

from src.plugins.item.core.definition import ItemDefinition, Quality, quality_label
from src.plugins.item.core.registry import _ITEMS

# 抽取品质的权重（由插件主模块解析 ITEM_DRAW_* 配置后注入）：品质 -> 权重
_draw_weights: dict[Quality, float] = {}


def set_draw_weights(weights: dict[Quality, float]) -> None:
    """设置抽取品质的权重（主模块解析配置后调用）。"""
    _draw_weights.clear()
    _draw_weights.update(weights)


def _draw_quality() -> Quality | None:
    """按权重抽取品质；权重未设置或浮点误差导致未命中时返回 None。"""
    roll = random.random()
    for quality, weight in _draw_weights.items():
        roll -= weight
        if roll < 0:
            return quality
    return None


def draw_item_of_quality(quality: Quality) -> ItemDefinition | None:
    """从指定品质的道具池中等概率抽取一件，池为空时返回 None。"""
    pool = [item for item in _ITEMS.values() if item.quality == quality]
    if not pool:
        logger.warning(
            f"抽取失败：{quality_label(quality)}（品质 {quality.value}）的道具池为空"
        )
        return None
    return random.choice(pool)


def draw_random_item() -> ItemDefinition | None:
    """随机抽取一件道具：先按配置权重抽品质、再从该品质池等概率抽取。

    权重未设置（异常情况）或品质池为空时返回 None。
    """
    quality = _draw_quality()
    if quality is None:
        logger.warning("道具抽取未命中任何品质（抽取权重未设置），本次未抽到道具")
        return None
    return draw_item_of_quality(quality)
