"""道具「？！区区？！」（编号 678）：黑色诅咒、诅咒型。

获得后不进入库存，由主模块先发送完整信息、随后自动使用：获得持续
1 天的「区」状态。状态有效期间，Bot 发送的群消息中涉及该成员显示名
的部分全部替换为"区宝宝"。

替换经 Bot.on_calling_api 钩子完成：对群发送接口（send_msg /
send_group_msg / send_group_forward_msg）的各文本段做名称替换。
成员显示名在登记状态时快照保存（原名与常规截断形式，见
_name_variants）；状态持续期间成员修改群昵称属于未覆盖的边界情况。
"""

import re
from typing import Any

from nonebot.adapters import Bot as BaseBot
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment

from src.plugins._shared.onebot import truncate_name
from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemType,
    ItemUseContext,
    Quality,
    add_state,
    list_state_holders,
    register_item,
)

# 道具编号与状态键
_ITEM_ID = "678"
_STATE_KEY = f"curse.{_ITEM_ID}"

# 「区」状态的持续时长（秒）：1 天
_DURATION_SECONDS = 86400.0

# 显示名替换的目标文案
_REPLACEMENT = "区宝宝"

# 消息中群昵称的常规最大显示长度（字符数，与主模块保持一致）
_NICKNAME_MAX_LENGTH = 12

# 需要按「区」状态改写文本的发送接口：群消息与群合并转发；
# 私聊消息不涉及群昵称，无需处理
_REWRITE_APIS = frozenset({"send_msg", "send_group_msg", "send_group_forward_msg"})


def _name_variants(name: str) -> list[str]:
    """返回成员显示名的替换候选：原名与常规截断形式（去重）。

    Bot 消息中既可能出现截断后的显示名（大多数插件），也可能出现
    完整原名（如签到消息），两种形式都作为候选保存。
    """
    stripped = name.strip()
    if not stripped:
        return []
    return list(
        dict.fromkeys((stripped, truncate_name(stripped, _NICKNAME_MAX_LENGTH)))
    )


def _as_group_id(value: Any) -> int | None:
    """把发送参数中的群号转换为 int，缺失或无效时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _group_names(group_id: int) -> list[str]:
    """返回群内全部「区」状态持有者的显示名候选（长名在前，去重）。"""
    names: set[str] = set()
    for _, state in list_state_holders(group_id, _STATE_KEY):
        raw = state.data.get("names")
        if not isinstance(raw, list):
            continue
        names.update(name for name in raw if isinstance(name, str) and name)
    return sorted(names, key=len, reverse=True)


def _rewrite_text(text: str, names: list[str]) -> str:
    """把文本中出现的「区」状态持有者显示名替换为"区宝宝"。

    候选名按长名在前排列，经正则一次扫描替换：替换结果不会再被
    其它候选名匹配。names 为空（无候选或均已为空名）时原样返回。
    """
    if not names:
        return text
    pattern = "|".join(re.escape(name) for name in names)
    return re.sub(pattern, _REPLACEMENT, text)


def _rewritten_message(message: Message, names: list[str]) -> Message | None:
    """返回文本段替换后的新消息；没有发生替换时返回 None。"""
    segments: list[MessageSegment] = []
    changed = False
    for segment in message:
        text = segment.data.get("text")
        if segment.type == "text" and isinstance(text, str):
            replaced = _rewrite_text(text, names)
            if replaced != text:
                segments.append(MessageSegment.text(replaced))
                changed = True
                continue
        segments.append(segment)
    if not changed:
        return None
    return Message(segments)


def _rewrite_send_data(data: dict[str, Any]) -> None:
    """改写一次群发送调用参数中的文本（send_msg / send_group_msg）。

    message 参数为 Message 时以替换后的新消息覆盖、为纯文本字符串
    时直接覆盖；参数缺失、私聊调用或未发生替换时不改动。
    """
    group_id = _as_group_id(data.get("group_id"))
    if group_id is None:
        return
    names = _group_names(group_id)
    if not names:
        return
    message = data.get("message")
    if isinstance(message, Message):
        rewritten = _rewritten_message(message, names)
        if rewritten is not None:
            data["message"] = rewritten
    elif isinstance(message, str):
        replaced = _rewrite_text(message, names)
        if replaced != message:
            data["message"] = replaced


def _rewrite_content_segments(content: list[Any], names: list[str]) -> None:
    """就地改写合并转发节点内容（消息段列表）中的文本段。"""
    for segment in content:
        if not isinstance(segment, dict) or segment.get("type") != "text":
            continue
        segment_data = segment.get("data")
        if not isinstance(segment_data, dict):
            continue
        text = segment_data.get("text")
        if isinstance(text, str):
            segment_data["text"] = _rewrite_text(text, names)


def _rewrite_forward_data(data: dict[str, Any]) -> None:
    """改写一次群合并转发调用参数中各节点的文本段。

    节点结构为本次调用新构造（见 _shared/onebot.py 的
    send_group_forward 与帮助插件），就地改写不会有副作用；
    结构异常时跳过对应节点。
    """
    group_id = _as_group_id(data.get("group_id"))
    if group_id is None:
        return
    names = _group_names(group_id)
    if not names:
        return
    messages = data.get("messages")
    if not isinstance(messages, list):
        return
    for node in messages:
        if not isinstance(node, dict):
            continue
        node_data = node.get("data")
        if not isinstance(node_data, dict):
            continue
        content = node_data.get("content")
        if isinstance(content, str):
            node_data["content"] = _rewrite_text(content, names)
        elif isinstance(content, list):
            _rewrite_content_segments(content, names)


@Bot.on_calling_api
async def _rewrite_outgoing(_bot: BaseBot, api: str, data: dict[str, Any]) -> None:
    """发送群消息前把文本中「区」状态持有者的显示名替换为"区宝宝"。

    钩子为异步签名，实际只做同步的文本替换：拦截全部群发送接口的
    参数字典并就地改写，对其它接口与调用无影响。
    """
    if api not in _REWRITE_APIS:
        return
    if api == "send_group_forward_msg":
        _rewrite_forward_data(data)
    else:
        _rewrite_send_data(data)


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：登记持续 1 天的「区」状态并发送提示。

    先登记状态再发送提示：提示消息经发送钩子时称呼即被替换为
    "区宝宝"。
    """
    add_state(
        context.group_id,
        context.user_id,
        state=ItemState(
            key=_STATE_KEY,
            item_id=_ITEM_ID,
            data={"names": _name_variants(context.user_name)},
        ),
        duration_seconds=_DURATION_SECONDS,
    )
    await context.send(
        f"{context.user_name} 变成了“区”！\n1天之内，Bot 会一直称呼 TA 为“区宝宝”。"
    )


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="？！区区？！",
        quality=Quality.BLACK_CURSE,
        types=(ItemType.CURSE,),
        description="你是一个可爱的区宝宝🥰",
        effect="使用后获得持续1天的“区”状态：Bot称呼你的名字变成“区宝宝”。",
        condition="无",
        timing="自动使用",
        note="？！区区？！",
        handle_use=_handle_use,
    )
)
