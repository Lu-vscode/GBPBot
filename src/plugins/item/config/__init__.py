"""道具插件配置（包入口）：解析后的配置值的统一导入点。

- model.py：Config 模型与 plugin_config（`ITEM_*` 环境变量）；
- resolve.py：解析与校验，并导出解析后的配置值。

包内其它模块统一从本包导入配置，不直接依赖具体子模块。
"""

from src.plugins.item.config.model import Config, plugin_config
from src.plugins.item.config.resolve import (
    CRAFT_ODDS,
    DRAW_WEIGHTS,
    LIST_FORWARD_THRESHOLD,
    TEST_GROUPS,
)

__all__ = [
    "CRAFT_ODDS",
    "DRAW_WEIGHTS",
    "LIST_FORWARD_THRESHOLD",
    "TEST_GROUPS",
    "Config",
    "plugin_config",
]
