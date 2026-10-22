"""道具「！？区区？！」（编号 678）：黑色诅咒、诅咒型。

获得后不进入库存，由主模块先发送完整信息、随后自动使用：获得持续
1 天的「区」状态。状态有效期间，Bot 发送的群消息中涉及该成员显示名
的部分全部替换为"区宝宝"。

替换经 Bot.on_calling_api 钩子完成：对群发送接口（send_msg /
send_group_msg / send_group_forward_msg）的各文本段做名称替换。
成员显示名在登记状态时快照保存（原名与常规截断形式，见
_name_variants）；状态持续期间成员修改群昵称属于未覆盖的边界情况。
"""

import re
import time
from typing import Any

from nonebot.adapters import Bot as BaseBot
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment

from src.plugins._shared.config import NICKNAME_MAX_LENGTH
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
    state_version,
)

# 道具编号与状态键
_ITEM_ID = "678"
_STATE_KEY = f"curse.{_ITEM_ID}"

# 「区」状态的持续时长（秒）：1 天
_DURATION_SECONDS = 86400.0

# 显示名替换的目标文案
_REPLACEMENT = "区宝宝"

# 消息中群昵称的常规最大显示长度（字符数，共享配置 NICKNAME_MAX_LENGTH）
_NICKNAME_MAX_LENGTH = NICKNAME_MAX_LENGTH

# 需要按「区」状态改写文本的发送接口：群消息与群合并转发；
# 私聊消息不涉及群昵称，无需处理
_REWRITE_APIS = frozenset({"send_msg", "send_group_msg", "send_group_forward_msg"})

# 群昵称替换表的缓存：群号 -> (状态变更序号, 缓存到期时刻, 预编译正则)；
# 无持有者时正则为 None（发送热路径直接返回）。状态未变化也没有状态
# 到期时直接复用，无需遍历群内全部成员状态
_group_patterns: dict[int, tuple[int, float, re.Pattern[str] | None]] = {}

# 缓存条目上限：群数很多时整体清空重建，避免无限增长
_GROUP_CACHE_LIMIT = 256


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


def _collect_group_names(group_id: int) -> tuple[list[str], float]:
    """返回群内全部「区」状态持有者的显示名候选（长名在前，去重）与
    这些状态中最早的过期时刻（没有到期状态时为无穷大）。"""
    names: set[str] = set()
    deadline = float("inf")
    for _, state in list_state_holders(group_id, _STATE_KEY):
        if state.expires_at is not None:
            deadline = min(deadline, state.expires_at)
        raw = state.data.get("names")
        if not isinstance(raw, list):
            continue
        names.update(name for name in raw if isinstance(name, str) and name)
    return sorted(names, key=len, reverse=True), deadline


def _group_pattern(group_id: int) -> re.Pattern[str] | None:
    """返回群内「区」状态持有者显示名的替换正则，无持有者时返回 None。

    结果按状态变更序号与最早过期时刻缓存：状态未变化且没有状态过期
    时直接复用上次编译的正则，发送热路径不再遍历群内全部成员状态。
    状态新增/移除/清理后序号变化（见 _framework.state_version），
    缓存自动失效重建。
    """
    now = time.time()
    version = state_version()
    cached = _group_patterns.get(group_id)
    if cached is not None and cached[0] == version and now < cached[1]:
        return cached[2]
    names, deadline = _collect_group_names(group_id)
    pattern = re.compile("|".join(re.escape(name) for name in names)) if names else None
    if len(_group_patterns) >= _GROUP_CACHE_LIMIT:
        _group_patterns.clear()
    _group_patterns[group_id] = (version, deadline, pattern)
    return pattern


def _rewrite_text(text: str, pattern: re.Pattern[str] | None) -> str:
    """把文本中出现的「区」状态持有者显示名替换为"区宝宝"。

    正则由 _group_pattern 提供（候选名长名在前，经一次扫描替换：
    替换结果不会再被其它候选名匹配）；pattern 为 None 时原样返回。
    """
    if pattern is None:
        return text
    return pattern.sub(_REPLACEMENT, text)


def _rewritten_message(
    message: Message, pattern: re.Pattern[str] | None
) -> Message | None:
    """返回文本段替换后的新消息；没有发生替换时返回 None。"""
    segments: list[MessageSegment] = []
    changed = False
    for segment in message:
        text = segment.data.get("text")
        if segment.type == "text" and isinstance(text, str):
            replaced = _rewrite_text(text, pattern)
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
    pattern = _group_pattern(group_id)
    if pattern is None:
        return
    message = data.get("message")
    if isinstance(message, Message):
        rewritten = _rewritten_message(message, pattern)
        if rewritten is not None:
            data["message"] = rewritten
    elif isinstance(message, str):
        replaced = _rewrite_text(message, pattern)
        if replaced != message:
            data["message"] = replaced


def _rewrite_content_segments(
    content: list[Any], pattern: re.Pattern[str] | None
) -> None:
    """就地改写合并转发节点内容（消息段列表）中的文本段。"""
    for segment in content:
        if not isinstance(segment, dict) or segment.get("type") != "text":
            continue
        segment_data = segment.get("data")
        if not isinstance(segment_data, dict):
            continue
        text = segment_data.get("text")
        if isinstance(text, str):
            segment_data["text"] = _rewrite_text(text, pattern)


def _rewrite_forward_data(data: dict[str, Any]) -> None:
    """改写一次群合并转发调用参数中各节点的文本段。

    节点结构为本次调用新构造（见 _shared/onebot.py 的
    send_group_forward 与帮助插件），就地改写不会有副作用；
    结构异常时跳过对应节点。
    """
    group_id = _as_group_id(data.get("group_id"))
    if group_id is None:
        return
    pattern = _group_pattern(group_id)
    if pattern is None:
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
            node_data["content"] = _rewrite_text(content, pattern)
        elif isinstance(content, list):
            _rewrite_content_segments(content, pattern)


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
    """使用效果：发送提示后登记持续 1 天的「区」状态。

    先发送提示再登记状态：发送钩子在提示消息发出前查询状态，此时
    状态尚未登记，提示中的称呼保持成员原名；提示发出后「区」状态
    即刻生效。
    """
    await context.send(
        f"{context.user_name} 变成了“区”！\n1天之内，Bot 会一直称呼 TA 为“区宝宝”。"
    )
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


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="！？区区？！",
        quality=Quality.BLACK_CURSE,
        types=(ItemType.CURSE,),
        description="你是一个可爱的区宝宝🥰",
        effect="使用后获得持续1天的“区”状态：Bot称呼你的名字变成“区宝宝”。",
        condition="无",
        timing="自动使用",
        note="？！区区？！",
        handle_use=_handle_use,
        state_labels=((_STATE_KEY, "区"),),
    )
)
