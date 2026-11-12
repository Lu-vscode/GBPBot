"""共享配置 NICKNAME_GUARD_DISABLED_GROUPS：不启用群昵称保护的群号列表。

群昵称保护插件在加载时读取该配置；篡天道具（002）禁用本群前经本模块
读取现有名单并合并写入，避免覆盖已禁用的其它群。
"""

from functools import lru_cache

from nonebot import get_plugin_config, logger

from src.plugins._shared.config.model import Config
from src.plugins._shared.config.parse import parse_group_list


@lru_cache(maxsize=1)
def get_nickname_guard_disabled_groups() -> frozenset[int]:
    """返回不启用群昵称保护的群号（共享配置 NICKNAME_GUARD_DISABLED_GROUPS）。

    群昵称保护插件在加载时读取同一配置；篡天道具（002）禁用本群前
    经本函数读取现有名单并合并写入，避免覆盖已禁用的其它群。首次调用
    时读取配置并缓存；缺失或为空时返回空集合，格式无效时记录 warning
    并视为空。
    """
    raw = get_plugin_config(Config).nickname_guard_disabled_groups
    groups = parse_group_list(raw)
    if groups is None:
        logger.warning(
            "配置 NICKNAME_GUARD_DISABLED_GROUPS 格式无效（应为群号列表），已视为空"
        )
        return frozenset()
    return groups
