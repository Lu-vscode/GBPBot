"""内容审核插件的配置模型与解析结果。

可在 `.env.{environment}` 文件中通过 `MODERATION_*` 系列变量配置，缺失
或为空时使用默认值；模型服务的地址与密钥沿用
nonebot-plugin-anywhere-llm 的 `OPENAI_BASE_URL`、`OPENAI_API_KEY`。
plugin_config 在导入时读取一次，解析结果集中在 SETTINGS。
"""

from dataclasses import dataclass
from typing import Any

from nonebot import get_plugin_config, logger
from pydantic import BaseModel

DEFAULT_MODEL = "qwen3.7-flash"
"""默认审核模型。"""

DEFAULT_TIMEOUT = 8
"""单次审核的默认超时秒数。"""

DEFAULT_MAX_CHARS = 1000
"""单次送审文本的默认最大字数，超出部分截断。"""

DEFAULT_CACHE_TTL = 300
"""审核结果缓存的默认有效期秒数。"""

DEFAULT_CACHE_SIZE = 256
"""审核结果缓存的默认条目上限。"""

DEFAULT_CONCURRENCY = 4
"""同时进行的审核请求默认上限。"""

MAX_OUTPUT_TOKENS = 256
"""审核请求的最大输出 token 数（只输出一个短 JSON，无需更多）。"""


class Config(BaseModel):
    """内容审核插件配置。"""

    moderation_model: Any = None
    """审核使用的模型名，缺失或为空时默认 qwen3.7-flash。"""

    moderation_timeout: Any = None
    """单次审核的超时秒数（含排队等待时间，正整数），默认 8。"""

    moderation_max_chars: Any = None
    """单次送审文本的最大字数（正整数），超出部分截断，默认 1000。"""

    moderation_cache_ttl: Any = None
    """相同文本审核结果的缓存秒数（非负整数），0 表示不缓存，默认 300。"""

    moderation_cache_size: Any = None
    """审核结果缓存的条目上限（正整数），默认 256。"""

    moderation_concurrency: Any = None
    """同时进行的审核请求上限（正整数），默认 4。"""


plugin_config = get_plugin_config(Config)


@dataclass(frozen=True)
class Settings:
    """解析后的审核配置。"""

    model: str
    """审核使用的模型名。"""

    timeout: int
    """单次审核的超时秒数。"""

    max_chars: int
    """单次送审文本的最大字数。"""

    cache_ttl: int
    """审核结果缓存的秒数，0 表示不缓存。"""

    cache_size: int
    """审核结果缓存的条目上限。"""

    concurrency: int
    """同时进行的审核请求上限。"""


def _as_count(value: Any, *, allow_zero: bool) -> int | None:
    """把配置项转换为计数（正整数，allow_zero 时允许 0），无效时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            return None
        value = int(text)
    if not isinstance(value, int):
        return None
    if value == 0 and allow_zero:
        return 0
    return value if value > 0 else None


def _count_or_default(
    name: str, value: Any, default: int, *, allow_zero: bool = False
) -> int:
    """返回配置项对应的计数；无效时记录警告并使用默认值。"""
    number = _as_count(value, allow_zero=allow_zero)
    if number is not None:
        return number
    if value is not None and str(value).strip():
        range_text = "非负整数" if allow_zero else "正整数"
        logger.warning(
            f"内容审核配置 {name}={value!r} 无效（需为{range_text}），"
            f"已使用默认值 {default}"
        )
    return default


def _model_or_default(value: Any) -> str:
    """返回审核模型名；缺失或为空时使用默认模型。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_MODEL


SETTINGS = Settings(
    model=_model_or_default(plugin_config.moderation_model),
    timeout=_count_or_default(
        "MODERATION_TIMEOUT", plugin_config.moderation_timeout, DEFAULT_TIMEOUT
    ),
    max_chars=_count_or_default(
        "MODERATION_MAX_CHARS", plugin_config.moderation_max_chars, DEFAULT_MAX_CHARS
    ),
    cache_ttl=_count_or_default(
        "MODERATION_CACHE_TTL",
        plugin_config.moderation_cache_ttl,
        DEFAULT_CACHE_TTL,
        allow_zero=True,
    ),
    cache_size=_count_or_default(
        "MODERATION_CACHE_SIZE", plugin_config.moderation_cache_size, DEFAULT_CACHE_SIZE
    ),
    concurrency=_count_or_default(
        "MODERATION_CONCURRENCY",
        plugin_config.moderation_concurrency,
        DEFAULT_CONCURRENCY,
    ),
)
