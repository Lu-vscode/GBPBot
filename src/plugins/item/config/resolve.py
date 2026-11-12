"""道具配置的解析与校验。

各 `ITEM_*` 配置在此解析为可直接使用的值（DRAW_WEIGHTS、CRAFT_ODDS、
TEST_GROUPS、LIST_FORWARD_THRESHOLD）：缺失或为空时使用默认值，无法解析
时记录日志并回退到默认值，概率总和不等于 1 时归一化。
"""

import math
from typing import Any

from nonebot import logger

from src.plugins.item.config.model import plugin_config
from src.plugins.item.core.craft import DEFAULT_CRAFT_ODDS
from src.plugins.item.core.definition import Quality

# 抽取品质的默认概率（键与 ITEM_DRAW_* 配置一一对应）
_DEFAULT_DRAW = {
    Quality.BLACK_CURSE: 0.055,
    Quality.GRAY: 0.550,
    Quality.WHITE: 0.250,
    Quality.GREEN: 0.100,
    Quality.BLUE: 0.030,
    Quality.PURPLE: 0.010,
    Quality.GOLD: 0.005,
}

# 品质对应的 ITEM_DRAW_* 配置后缀
_DRAW_KEYS = {
    Quality.BLACK_CURSE: "black",
    Quality.GRAY: "gray",
    Quality.WHITE: "white",
    Quality.GREEN: "green",
    Quality.BLUE: "blue",
    Quality.PURPLE: "purple",
    Quality.GOLD: "gold",
}

# 概率比较的容差（默认值浮点相加可能有微小误差，不做无谓归一化）
_PROBABILITY_TOLERANCE = 1e-9

# 道具清单改用合并转发的默认种类数阈值（配置项 ITEM_LIST_FORWARD_THRESHOLD）
_DEFAULT_LIST_FORWARD_THRESHOLD = 10


def _parse_probability(value: Any, default: float, name: str) -> float:
    """解析单个概率配置。

    缺失或为空时使用默认值；无法解析为有限数字时报错并使用默认值；
    负数视为 0。
    """
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        number = float(text)
    except ValueError:
        logger.error(
            f"道具配置 {name}={value!r} 无法解析为数字，已使用默认值 {default}"
        )
        return default
    if not math.isfinite(number):
        logger.error(f"道具配置 {name}={value!r} 不是有限数字，已使用默认值 {default}")
        return default
    if number < 0:
        logger.warning(f"道具配置 {name}={value!r} 为负数，已视为 0")
        return 0.0
    return number


def _resolve_draw_weights() -> dict[Quality, float]:
    """解析抽取品质的权重：总概率不为 1 时归一化，总概率为 0 时用默认值。"""
    weights = {
        quality: _parse_probability(
            getattr(plugin_config, f"item_draw_{key}"),
            _DEFAULT_DRAW[quality],
            f"ITEM_DRAW_{key.upper()}",
        )
        for quality, key in _DRAW_KEYS.items()
    }
    total = sum(weights.values())
    if total <= 0:
        logger.error("道具配置 ITEM_DRAW_* 的总概率不大于 0，已使用默认抽取概率")
        return dict(_DEFAULT_DRAW)
    if not math.isclose(
        total, 1.0, rel_tol=_PROBABILITY_TOLERANCE, abs_tol=_PROBABILITY_TOLERANCE
    ):
        logger.warning(f"道具配置 ITEM_DRAW_* 的总概率为 {total}（不为 1），已归一化")
        return {quality: weight / total for quality, weight in weights.items()}
    return weights


def _resolve_craft_odds() -> tuple[float, float, float]:
    """解析合成概率（低一级、高一级、高两级）：总概率不为 1 时归一化。"""
    default_down, default_up, default_up2 = DEFAULT_CRAFT_ODDS
    down = _parse_probability(
        plugin_config.item_craft_down, default_down, "ITEM_CRAFT_DOWN"
    )
    up = _parse_probability(plugin_config.item_craft_up, default_up, "ITEM_CRAFT_UP")
    up2 = _parse_probability(
        plugin_config.item_craft_up2, default_up2, "ITEM_CRAFT_UP2"
    )
    total = down + up + up2
    if total <= 0:
        logger.error("道具配置 ITEM_CRAFT_* 的总概率不大于 0，已使用默认合成概率")
        return DEFAULT_CRAFT_ODDS
    if not math.isclose(
        total, 1.0, rel_tol=_PROBABILITY_TOLERANCE, abs_tol=_PROBABILITY_TOLERANCE
    ):
        logger.warning(f"道具配置 ITEM_CRAFT_* 的总概率为 {total}（不为 1），已归一化")
        return down / total, up / total, up2 / total
    return down, up, up2


def _as_group_id(value: Any) -> int | None:
    """把配置项转换为群号，无效时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _resolve_test_groups() -> frozenset[int]:
    """解析测试群群号列表（JSON 数组），无效时视为空（功能禁用）。"""
    value = plugin_config.item_test_groups
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        logger.warning("道具配置 ITEM_TEST_GROUPS 格式无效（应为群号列表），已视为空")
        return frozenset()
    return frozenset(
        group_id for item in value if (group_id := _as_group_id(item)) is not None
    )


def _resolve_list_forward_threshold() -> int:
    """解析道具清单改用合并转发的种类数阈值（正整数）。

    缺失或为空时使用默认值；无法解析为整数或不为正数时报错并使用默认值。
    """
    value = plugin_config.item_list_forward_threshold
    if value is None:
        return _DEFAULT_LIST_FORWARD_THRESHOLD
    text = str(value).strip()
    if not text:
        return _DEFAULT_LIST_FORWARD_THRESHOLD
    try:
        number = int(text)
    except ValueError:
        number = 0
    if number > 0:
        return number
    logger.error(
        f"道具配置 ITEM_LIST_FORWARD_THRESHOLD={value!r} 不是正整数，"
        f"已使用默认值 {_DEFAULT_LIST_FORWARD_THRESHOLD}"
    )
    return _DEFAULT_LIST_FORWARD_THRESHOLD


# 抽取品质的权重（经 core/draw.py 注入抽取逻辑）
DRAW_WEIGHTS: dict[Quality, float] = _resolve_draw_weights()

# 合成概率（低一级、高一级、高两级，经 core/craft.py 注入合成逻辑）
CRAFT_ODDS: tuple[float, float, float] = _resolve_craft_odds()

# 测试群群号：仅这些群中的超级用户可使用 /item.get 获取测试道具
TEST_GROUPS: frozenset[int] = _resolve_test_groups()

# 道具清单改用合并转发的种类数阈值（默认 10）
LIST_FORWARD_THRESHOLD: int = _resolve_list_forward_threshold()
