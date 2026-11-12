"""共享配置 NICKNAME_MAX_LENGTH：群昵称的最大显示长度（字符数）。

决斗与道具插件共用该配置，避免两侧截断规则不一致；群昵称显示工具
（见 _shared/onebot/naming.py）以本配置作为默认截断长度。
"""

from nonebot import get_plugin_config, logger

from src.plugins._shared.config.model import Config
from src.plugins._shared.config.parse import as_positive_int

# 群昵称最大显示长度的默认值（字符数）
_DEFAULT_NICKNAME_MAX_LENGTH = 12


# 未配置或非正整数时记 warning 并使用默认值。
def _resolve_nickname_max_length() -> int:
    """读取并校验群昵称最大显示长度（共享配置 NICKNAME_MAX_LENGTH）。"""
    raw = get_plugin_config(Config).nickname_max_length
    value = as_positive_int(raw)
    if value is not None:
        return value
    if raw is not None:
        logger.warning(
            f"共享配置 NICKNAME_MAX_LENGTH={raw!r} 无效"
            f"（需为正整数），已使用默认值 {_DEFAULT_NICKNAME_MAX_LENGTH}"
        )
    return _DEFAULT_NICKNAME_MAX_LENGTH


NICKNAME_MAX_LENGTH: int = _resolve_nickname_max_length()
