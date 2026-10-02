"""帮助插件。

用法：/help

读取同目录下的纯文本用户手册（help_manual.txt），按分隔行拆分为
多个节点，以合并转发的聊天记录形式发送。
"""

from pathlib import Path
from typing import Any

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="帮助",
    description="以合并转发的聊天记录形式发送 GBPBot 用户手册",
    usage="/help：把用户手册的内容以合并转发形式发给你",
    type="application",
    supported_adapters={"~onebot.v11"},
)

help_cmd = on_command("help")

# 纯文本用户手册：以一行 10 个等号分隔为多个合并转发节点
_MANUAL_PATH = Path(__file__).parent / "help_manual.txt"
_NODE_SEPARATOR = "=========="

# 合并转发节点中显示的发送者名称
_NODE_NAME = "GBPBot 用户手册"

# 用户手册读取失败时回复的兜底提示
_FALLBACK_REPLY = "请参阅 GBPBot 用户手册：https://github.com/Lu-vscode/GBPBot"


def _load_manual_nodes(self_id: str) -> list[dict[str, Any]]:
    """读取用户手册并按分隔行拆分为合并转发节点。

    节点内容用文本消息段构造，避免协议端把含 "[CQ:...]" 的文本
    解析成消息段。
    """
    text = _MANUAL_PATH.read_text(encoding="utf-8")
    return [
        {
            "type": "node",
            "data": {
                "name": _NODE_NAME,
                "uin": self_id,
                "content": [{"type": "text", "data": {"text": section.strip()}}],
            },
        }
        for section in text.split(_NODE_SEPARATOR)
        if section.strip()
    ]


@help_cmd.handle()
async def handle_help(bot: Bot, event: MessageEvent) -> None:
    try:
        nodes = _load_manual_nodes(bot.self_id)
    except OSError as exc:
        logger.error(f"读取用户手册失败，回复兜底提示：{exc}")
        await help_cmd.finish(_FALLBACK_REPLY)

    try:
        if isinstance(event, GroupMessageEvent):
            await bot.call_api(
                "send_group_forward_msg", group_id=event.group_id, messages=nodes
            )
        else:
            await bot.call_api(
                "send_private_forward_msg", user_id=event.user_id, messages=nodes
            )
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"发送用户手册失败：{exc}")

    await help_cmd.finish()
