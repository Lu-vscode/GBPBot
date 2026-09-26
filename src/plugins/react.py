"""为消息贴表情的插件。

用法：/react <表情>
"""

import re
from typing import Any, Optional

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="贴表情",
    description="为触发指令的消息贴上指定的表情",
    usage="/react <表情>：给这条消息贴上对应表情，引用其它消息时贴给被引用的消息",
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 常见 Unicode 表情字符范围
_EMOJI_CHARS = (
    "\u2600-\u27bf"  # 杂项符号与装饰符号
    "\u2b00-\u2bff"  # 杂项符号与箭头
    "\U0001f000-\U0001f0ff"  # 麻将、扑克牌、多米诺骨牌
    "\U0001f1e6-\U0001f1ff"  # 区域指示符（国旗）
    "\U0001f300-\U0001f5ff"  # 杂项符号和象形文字
    "\U0001f600-\U0001f64f"  # 表情符号
    "\U0001f680-\U0001f6ff"  # 交通和地图符号
    "\U0001f700-\U0001f77f"  # 炼金术符号
    "\U0001f780-\U0001f7ff"  # 几何图形扩展
    "\U0001f800-\U0001f8ff"  # 补充箭头符号 C
    "\U0001f900-\U0001f9ff"  # 补充符号和象形文字
    "\U0001fa00-\U0001faff"  # 符号和象形文字扩展
)
# 变体选择符、零宽连接符、键帽组合符、肤色修饰符
_EMOJI_JOINERS = "\u200d\ufe0f\u20e3\U0001f3fb-\U0001f3ff"
_FLAG_CHARS = "\U0001f1e6-\U0001f1ff"

# 匹配单个表情簇：国旗为两个区域指示符，其余表情后跟随 "零宽连接符 + 表情"
# （如家庭表情）或变体选择符、键帽组合符、肤色修饰符
_EMOJI_CLUSTER = re.compile(
    f"(?:[{_FLAG_CHARS}]{{2}}|[{_EMOJI_CHARS}](?:[\u200d][{_EMOJI_CHARS}]|[{_EMOJI_JOINERS}])*)"
)

react = on_command("react")


def _extract_emoji(message: Message) -> Optional[str]:
    """从命令参数中提取表情，提取不到时返回 None。

    QQ 原生表情段返回其数字 ID，Unicode 表情返回第一个表情簇。
    """
    # 优先取 QQ 原生表情段（face / mface）
    for segment in message:
        if segment.type == "face":
            face_id = segment.data.get("id")
            if face_id:
                return str(face_id)
        elif segment.type == "mface":
            emoji_id = segment.data.get("emoji_id")
            if emoji_id:
                return str(emoji_id)

    # 其次从纯文本中提取第一个 Unicode 表情
    match = _EMOJI_CLUSTER.search(message.extract_plain_text())
    return match.group() if match else None


def _to_emoji_id(emoji: str) -> str:
    """转换为贴表情接口所需的 emoji_id。

    系统表情的数字 ID 直接透传；Unicode 表情需转换为码点的十进制值
    （dec 值），如 "😄" (U+1F604) 对应 "128516"，否则部分协议端会把
    其误判为系统表情而静默失败。
    """
    if emoji.isascii() and emoji.isdigit():
        return emoji
    return str(ord(emoji[0]))


def _find_reply_id(message: Message) -> Optional[int]:
    """从消息段中提取引用消息的 ID，提取不到时返回 None。"""
    for segment in message:
        if segment.type != "reply":
            continue
        reply_id = str(segment.data.get("id", "")).strip()
        if reply_id.isascii() and reply_id.isdigit():
            return int(reply_id)
    return None


def _find_reply_id_in_raw(raw: Any) -> Optional[int]:
    """从 get_msg 返回的原始消息（段数组或 CQ 码字符串）中提取引用 ID。"""
    if isinstance(raw, str):
        return _find_reply_id(Message(raw))
    if isinstance(raw, list):
        segments = [MessageSegment(**seg) for seg in raw if isinstance(seg, dict)]
        return _find_reply_id(Message(segments))
    return None


async def _resolve_target_message_id(bot: Bot, event: MessageEvent) -> int:
    """获取贴表情的目标消息 ID。

    触发指令的消息若引用了其它消息，则以被引用的消息为目标，
    否则以触发指令的消息本身为目标。协议端解析引用依赖被引用
    消息已写入本地数据，可能失败导致事件中缺少引用段，此时
    重新获取该消息再解析一次。
    """
    reply_id = _find_reply_id(event.message)
    if reply_id is not None:
        return reply_id

    try:
        info = await bot.call_api("get_msg", message_id=event.message_id)
        reply_id = _find_reply_id_in_raw((info or {}).get("message"))
    except ActionFailed as exc:
        logger.debug(f"重新获取消息以解析引用失败：{exc}")
        return event.message_id

    if reply_id is None:
        logger.debug("未解析出引用消息，贴表情目标为触发消息本身")
        return event.message_id
    return reply_id


@react.handle()
async def handle_react(
    bot: Bot, event: MessageEvent, args: Message = CommandArg()
) -> None:
    emoji = _extract_emoji(args)
    if emoji is None:
        # 参数为空或不含表情：不响应
        await react.finish()

    target_id = await _resolve_target_message_id(bot, event)
    try:
        await bot.call_api(
            "set_msg_emoji_like",
            message_id=target_id,
            emoji_id=_to_emoji_id(emoji),
        )
    except ActionFailed as exc:
        logger.warning(f"贴表情失败：{exc}")

    await react.finish()
