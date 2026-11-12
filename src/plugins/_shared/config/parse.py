"""配置值的通用转换。

各共享配置项都允许以宽松的形式书写（整数、数字字符串、JSON 数组字符串
等），本模块把原始配置值规范为 QQ 号、正整数与群号集合，无效值以
None 或空集合返回，由调用方决定记录警告还是使用默认值。
"""

import json
from typing import Any


def as_positive_int(value: Any) -> int | None:
    """把配置项转换为正整数，无效时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


def as_qq(value: Any) -> int | None:
    """把配置项转换为 QQ 号，无效时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_group_list(value: Any) -> frozenset[int] | None:
    """把配置项解析为群号集合，格式无效时返回 None。

    兼容已解码的列表与 JSON 数组字符串；缺失（None）与空白字符串
    视为空集合。
    """
    if value is None:
        return frozenset()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return frozenset()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, list):
        return None
    return frozenset(
        group_id for item in value if (group_id := as_qq(item)) is not None
    )
