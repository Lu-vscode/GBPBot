"""内容审核插件（主模块）。

经 nonebot-plugin-anywhere-llm 调用大模型（默认 qwen3.7-flash），按 QQ
平台的内容安全规范判定一段纯文本是否放行，并经跨插件服务注册中心
（见 _shared/services/）对外提供 TextModerationService，供其它插件在
发送、展示或落盘消息前调用。本插件不监听事件、不含用户指令，审核标准
与类别见 prompt.py。

本模块只负责插件元数据、配置注入、服务注册与启动日志：

- config.py：配置模型与解析结果（MODERATION_* 变量）；
- prompt.py：审核标准（违规类别）与系统提示词；
- parsing.py：解析模型输出的 JSON；
- service.py：审核服务实现（模型调用、超时、结果缓存与并发限流）。
"""

from nonebot import logger
from nonebot.plugin import PluginMetadata

from src.plugins._shared.services import TextModerationService, register_service
from src.plugins.moderation.config import SETTINGS
from src.plugins.moderation.service import moderation_service

__plugin_meta__ = PluginMetadata(
    name="内容审核",
    description="调用大模型按 QQ 内容规范审核纯文本，经跨插件服务提供给其它插件",
    usage=(
        '不提供用户指令；其它插件 require("moderation") 后经 '
        "TextModerationService.check_text 调用"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
)

register_service(TextModerationService, moderation_service)

logger.info(
    f"内容审核已启用，模型 {SETTINGS.model}，单次超时 {SETTINGS.timeout} 秒，"
    f"缓存 {SETTINGS.cache_size} 条 / {SETTINGS.cache_ttl} 秒，"
    f"并发上限 {SETTINGS.concurrency}"
)
