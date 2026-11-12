"""猜拳决斗插件的数值配置解析结果。

配置项见 model.py；本模块把原始配置归一为插件运行期使用的模块级常量
（决斗时长、点数上限、发起限频）。缺失或无效时使用默认值并记录日志，
不会因配置错误导致插件加载失败。
"""

from nonebot import logger

from src.plugins.duel.config.model import plugin_config

# 决斗超时时间的默认值（分钟）、决斗点数上限、决斗发起的每小时次数上限（全局）
_DEFAULT_DURATION_MINUTES = 10.0
_DEFAULT_MAX_MULTIPLIER = 100
_DEFAULT_HOURLY_LIMIT = 20


def _positive_or_default(
    value: float | None, default: float, name: str, requirement: str
) -> float:
    """返回配置的正数值；未配置或非正数时记 warning 并使用默认值。"""
    if value is None:
        return default
    if value <= 0:
        logger.warning(
            f"猜拳决斗配置 {name}={value:g} 无效（{requirement}），"
            f"已使用默认值 {default:g}"
        )
        return default
    return float(value)


# 决斗超时时间（分钟）与超时判定用的秒数
DURATION_MINUTES = _positive_or_default(
    plugin_config.duel_duration,
    _DEFAULT_DURATION_MINUTES,
    "DUEL_DURATION",
    "需为正数，单位分钟",
)
DURATION_SECONDS = DURATION_MINUTES * 60.0

# 决斗点数的上限
MAX_MULTIPLIER = int(
    _positive_or_default(
        plugin_config.duel_max_multiplier,
        _DEFAULT_MAX_MULTIPLIER,
        "DUEL_MAX_MULTIPLIER",
        "需为正整数",
    )
)

# 决斗发起的每小时次数上限（全局，全部群聊合计）
HOURLY_LIMIT = int(
    _positive_or_default(
        plugin_config.duel_hourly_limit,
        _DEFAULT_HOURLY_LIMIT,
        "DUEL_HOURLY_LIMIT",
        "需为正整数",
    )
)
