"""猜拳决斗插件配置（包入口）：数值、文案与概率函数的统一导入点。

- model.py：Config 模型与数值归一（`DUEL_*` 环境变量）；
- values.py：解析后的数值常量（时长、点数上限、发起限频）；
- texts.py：可配置文案的默认值与校验、用法与边界提示、text()；
- accept_expr.py：机器人接受概率表达式的沙箱校验与求值。

包内其它模块统一从本包导入配置，不直接依赖具体子模块。
"""

from src.plugins.duel.config.accept_expr import (
    BOT_ACCEPT_PROBABILITY,
    accept_expr_error,
)
from src.plugins.duel.config.texts import (
    LIMIT_REACHED,
    PAIR_ACTIVE,
    PROVOKED_ALREADY_STARTED,
    PROVOKED_CANNOT_REJECT,
    RANK_EMPTY,
    RANK_NODE_NAME,
    RANK_TITLE_HIGH,
    RANK_TITLE_LOW,
    SCORE_SUM_DENIED,
    STATUS_ALL_DENIED,
    USAGE_ACCEPT,
    USAGE_DUEL,
    USAGE_RANK,
    USAGE_REJECT,
    USAGE_SCORE,
    USAGE_STATUS,
    text,
)
from src.plugins.duel.config.values import (
    DURATION_MINUTES,
    DURATION_SECONDS,
    HOURLY_LIMIT,
    MAX_MULTIPLIER,
)

__all__ = [
    "BOT_ACCEPT_PROBABILITY",
    "DURATION_MINUTES",
    "DURATION_SECONDS",
    "HOURLY_LIMIT",
    "LIMIT_REACHED",
    "MAX_MULTIPLIER",
    "PAIR_ACTIVE",
    "PROVOKED_ALREADY_STARTED",
    "PROVOKED_CANNOT_REJECT",
    "RANK_EMPTY",
    "RANK_NODE_NAME",
    "RANK_TITLE_HIGH",
    "RANK_TITLE_LOW",
    "SCORE_SUM_DENIED",
    "STATUS_ALL_DENIED",
    "USAGE_ACCEPT",
    "USAGE_DUEL",
    "USAGE_RANK",
    "USAGE_REJECT",
    "USAGE_SCORE",
    "USAGE_STATUS",
    "accept_expr_error",
    "text",
]
