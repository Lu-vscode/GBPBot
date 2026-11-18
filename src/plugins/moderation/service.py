"""内容审核服务的实现（契约见 _shared/services/moderation.py）。

审核经 nonebot-plugin-anywhere-llm 调用大模型完成，并按插件配置做结果
缓存（相同文本与场景不重复请求）与并发限流。审核不携带任何对话上下文
（关闭该插件的历史、时间与天气注入），同一文本无论何时送审都得到同一
份提示词，结果可缓存、可复现。
"""

import asyncio
import hashlib
import time
from dataclasses import dataclass

from nonebot import logger, require

require("nonebot_plugin_anywhere_llm")

from nonebot_plugin_anywhere_llm import LLMService

from src.plugins._shared.services import (
    ModerationResult,
    ModerationUnavailableError,
    TextModerationService,
)
from src.plugins.moderation.config import MAX_OUTPUT_TOKENS, SETTINGS, Settings
from src.plugins.moderation.parsing import parse_result
from src.plugins.moderation.prompt import SYSTEM_PROMPT, build_user_content

PREVIEW_LENGTH = 60
"""拦截日志中保留的文本长度（超出部分截断）。"""


@dataclass(frozen=True)
class _CacheEntry:
    """一条审核缓存：过期时刻（time.monotonic 时间轴）与结果。"""

    expires_at: float
    result: ModerationResult


def _build_llm(settings: Settings) -> LLMService:
    """构造只用于审核的 LLMService 实例。

    nonebot-plugin-anywhere-llm 的接口地址与密钥来自其自身的
    OPENAI_BASE_URL、OPENAI_API_KEY 配置，这里只覆盖与审核相关的部分：
    关闭历史（审核不需要上下文，也不会写库）、关闭时间与天气注入，并把
    系统提示词换成审核标准。当前版本的 provider 不透传 temperature，
    因此审核的稳定性只靠提示词约束，不依赖采样参数。
    """
    llm = LLMService()
    llm.config.messages.history.max_length = 0
    llm.config.messages.injections.time = 0
    llm.config.messages.injections.weather = 0
    llm.config.messages.system_prompt.template = SYSTEM_PROMPT
    llm.config.params.model = settings.model
    llm.config.params.max_tokens = MAX_OUTPUT_TOKENS
    return llm


def _prepare(text: str, max_chars: int) -> str:
    """规范化送审文本：去首尾空白，超长时截断。"""
    return text.strip()[:max_chars]


def _cache_key(content: str, scene: str) -> str:
    """按场景与正文生成缓存键（正文可能很长，用摘要作键）。"""
    return hashlib.sha256(f"{scene}\x00{content}".encode()).hexdigest()


def _preview(content: str) -> str:
    """返回用于日志的文本预览（超长时截断并加省略号）。"""
    if len(content) <= PREVIEW_LENGTH:
        return content.replace("\n", " ")
    return content[:PREVIEW_LENGTH].replace("\n", " ") + "…"


class _TextModerationService(TextModerationService):
    """纯文本内容审核服务的实现。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._llm = _build_llm(settings)
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        self._cache: dict[str, _CacheEntry] = {}

    async def check_text(self, text: str, *, scene: str = "") -> ModerationResult:
        """审核一段纯文本，返回是否放行（空文本直接放行，不请求模型）。"""
        content = _prepare(text, self._settings.max_chars)
        if not content:
            return ModerationResult(allowed=True)
        key = _cache_key(content, scene)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        result = await self._audit(content, scene)
        self._cache_put(key, result)
        return result

    async def _audit(self, content: str, scene: str) -> ModerationResult:
        """请求模型并解析结果，拦截时记录日志。"""
        raw = await self._ask(build_user_content(content, scene))
        result = parse_result(raw)
        if not result.allowed:
            logger.info(
                f"内容审核拦截（类别：{'、'.join(result.categories) or '未给出'}，"
                f"理由：{result.reason or '未给出'}）：{_preview(content)}"
            )
        return result

    async def _ask(self, user_content: str) -> str:
        """请求模型返回原始输出。

        异常:
            ModerationUnavailableError: 超时、接口异常或返回为空。
        """
        timeout = self._settings.timeout
        try:
            async with self._semaphore:
                raw = await asyncio.wait_for(
                    self._llm.generate(user_content), timeout=timeout
                )
        except TimeoutError as exc:
            message = (
                f"内容审核超时（{timeout} 秒，含排队等待；"
                f"MODERATION_TIMEOUT 或 MODERATION_CONCURRENCY 可调）"
            )
            raise ModerationUnavailableError(message) from exc
        except TypeError as exc:
            # 该插件的 provider 在接口异常时抛出字符串而非异常对象，最终
            # 表现为 TypeError；同时兼容内部实现变化带来的类型错误
            logger.debug(f"内容审核接口调用异常：{exc!r}")
            message = "内容审核接口调用失败（模型服务异常或未配置密钥）"
            raise ModerationUnavailableError(message) from exc
        if not isinstance(raw, str) or not raw.strip():
            message = "内容审核接口返回为空"
            raise ModerationUnavailableError(message)
        return raw

    def _cache_get(self, key: str) -> ModerationResult | None:
        """读取缓存，过期或未命中时返回 None；命中时刷新其最近使用顺序。"""
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._cache.pop(key, None)
            return None
        self._cache[key] = self._cache.pop(key)
        return entry.result

    def _cache_put(self, key: str, result: ModerationResult) -> None:
        """写入缓存；缓存已满时先丢弃最久未使用的条目。"""
        if self._settings.cache_ttl <= 0:
            return
        if key not in self._cache and len(self._cache) >= self._settings.cache_size:
            oldest = next(iter(self._cache), None)
            if oldest is not None:
                self._cache.pop(oldest, None)
        expires_at = time.monotonic() + self._settings.cache_ttl
        self._cache[key] = _CacheEntry(expires_at=expires_at, result=result)


moderation_service = _TextModerationService(SETTINGS)
"""审核服务实例：插件加载时注册到跨插件服务注册中心。"""
