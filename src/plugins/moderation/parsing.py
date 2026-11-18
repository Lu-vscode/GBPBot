"""解析审核模型返回的 JSON。

模型偶尔会用 Markdown 代码块包裹结果或在 JSON 前后带上说明文字，因此
先剥离代码块围栏、截取第一个左花括号到最后一个右花括号之间的内容再解析；
无法得到可用的 allowed 字段时抛出 ModerationUnavailableError（由调用方
决定放行还是拦截），保证不会把解析失败当成"通过"静默放过。
"""

import json

from nonebot import logger

from src.plugins._shared.services import ModerationResult, ModerationUnavailableError
from src.plugins.moderation.prompt import CATEGORIES

MAX_REASON_LENGTH = 60
"""判定理由的最大保留字数（超出部分截断）。"""

_FENCE_PREFIXES = ("```json", "```JSON", "```")


def _strip_fence(raw: str) -> str:
    """剥离包裹整段输出的 Markdown 代码块围栏。"""
    text = raw.strip()
    for prefix in _FENCE_PREFIXES:
        if text.startswith(prefix):
            text = text.removeprefix(prefix)
            break
    return text.removesuffix("```").strip()


def _extract_object(raw: str) -> str:
    """截取输出中的 JSON 对象文本；找不到成对花括号时返回空字符串。"""
    text = _strip_fence(raw)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return ""
    return text[start : end + 1]


def _as_bool(value: object) -> bool | None:
    """把 JSON 中的 allowed 字段转换为布尔值，无法判断时返回 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "true":
            return True
        if text == "false":
            return False
    return None


def _as_categories(value: object) -> tuple[str, ...]:
    """归一化违规类别：只保留审核标准中的类别名，去重并保持原顺序。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    categories: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            continue
        name = entry.strip()
        if name not in CATEGORIES:
            if name:
                logger.debug(f"内容审核返回了未知类别 {name!r}，已忽略")
            continue
        if name not in categories:
            categories.append(name)
    return tuple(categories)


def _as_reason(value: object) -> str:
    """归一化判定理由（非字符串视为空）。"""
    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_REASON_LENGTH]


def parse_result(raw: str) -> ModerationResult:
    """把模型输出解析为审核结果。

    异常:
        ModerationUnavailableError: 输出为空、不是 JSON 对象或缺少可用的
            allowed 字段。
    """
    payload = _extract_object(raw)
    if not payload:
        message = "内容审核返回中找不到 JSON 对象"
        raise ModerationUnavailableError(message)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        message = f"内容审核返回的 JSON 无法解析：{exc.msg}"
        raise ModerationUnavailableError(message) from exc
    if not isinstance(data, dict):
        message = "内容审核返回的 JSON 不是对象"
        raise ModerationUnavailableError(message)
    allowed = _as_bool(data.get("allowed"))
    if allowed is None:
        message = "内容审核返回缺少 allowed 字段"
        raise ModerationUnavailableError(message)
    if allowed:
        return ModerationResult(allowed=True)
    categories = _as_categories(data.get("categories"))
    return ModerationResult(
        allowed=False,
        categories=categories,
        reason=_as_reason(data.get("reason")),
    )
