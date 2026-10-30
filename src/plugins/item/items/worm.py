"""道具组「区」（677 区从天降、678 ！？区区？！、679 狂区之怒）。

“区”是“蛆”的谐音。三件道具共用同一份「区」状态：678 是黑色诅咒，
获得后不进入库存，由主模块先发送完整信息、随后自动使用，获得持续
1 天的「区」状态。状态有效期间，Bot 发送的群消息中涉及该成员显示名
的部分全部替换为"区宝宝"。

677 使用后自己立即获得 678，随后每分钟使一名（最多三名）决斗分数
非零的其它群成员依次获得 678；679 使用 /item.use 679 @成员 使对方
获得 678，条件是使用者自己持有「区」状态。本组道具发放的 678 一律经
框架的 acquire_curse 走完整的诅咒获得流程（信息播报、拦截、自动使用）。

显示名替换经 Bot.on_calling_api 钩子完成：对群发送接口（send_msg /
send_group_msg / send_group_forward_msg）的各文本段做名称替换。
成员显示名在登记状态时快照保存（原名与常规截断形式，见
_name_variants）；状态持续期间成员修改群昵称属于未覆盖的边界情况。
"""

import random
import re
import time
from dataclasses import dataclass
from typing import Any

from nonebot import get_bots, logger
from nonebot.adapters import Bot as BaseBot
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot_plugin_apscheduler import scheduler

from src.plugins._shared.config import NICKNAME_MAX_LENGTH, get_bot_list
from src.plugins._shared.onebot import member_display_name, truncate_name
from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemType,
    ItemUseContext,
    Quality,
    acquire_curse,
    add_item,
    add_state,
    get_item,
    get_rank_entries,
    get_state,
    list_state_holders,
    register_item,
    state_version,
)

# 道具编号
_ADVENT_ID = "677"
_WORM_ID = "678"
_WRATH_ID = "679"

# 「区」状态键（由 678 产生）与状态持续时长（秒）：1 天
_STATE_KEY = f"curse.{_WORM_ID}"
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

# 区从天降的发放节奏：最多三名其它成员，每分钟发放一份「区」诅咒
_GRANT_COUNT = 3
_GRANT_INTERVAL_SECONDS = 60.0

# 区从天降待发放队列的检查间隔（秒）与机器人离线时的放弃时限（秒）
_GRANT_CHECK_INTERVAL_SECONDS = 10
_GRANT_GIVE_UP_SECONDS = 600.0


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


async def _handle_worm_use(context: ItemUseContext) -> None:
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
            item_id=_WORM_ID,
            data={"names": _name_variants(context.user_name)},
        ),
        duration_seconds=_DURATION_SECONDS,
    )


register_item(
    ItemDefinition(
        item_id=_WORM_ID,
        name="！？区区？！",
        quality=Quality.BLACK_CURSE,
        types=(ItemType.CURSE,),
        description="你是一个可爱的区宝宝🥰",
        effect="使用后获得持续1天的“区”状态：Bot称呼你的名字变成“区宝宝”。",
        condition="无",
        timing="自动使用",
        note="？！区区？！",
        handle_use=_handle_worm_use,
        state_labels=((_STATE_KEY, "区"),),
    )
)


def _pick_grant_targets(group_id: int, user_id: int) -> list[int]:
    """随机选取最多三名决斗分数非零的其它群成员（不含自己与机器人）。

    候选取自决斗分数总榜（0 分或没有分数记录的成员不在榜上）；候选
    不足三名时按实际人数返回，没有候选时返回空列表。
    """
    bots = get_bot_list()
    candidates = [
        entry_user
        for entry_user, _name, _score in get_rank_entries(group_id)
        if entry_user != user_id and entry_user not in bots
    ]
    return random.sample(candidates, min(_GRANT_COUNT, len(candidates)))


@dataclass
class _PendingGrant:
    """一份等待发放的「区」诅咒（仅存内存，Bot 重启后丢失）。"""

    group_id: int
    """发放所在群号。"""

    target_id: int
    """获得诅咒的成员 QQ 号。"""

    due_at: float
    """计划发放时刻（time.monotonic）。"""

    deadline: float
    """放弃发放的时限（time.monotonic）：期满仍未发出则丢弃。"""


# 等待发放的「区」诅咒（按计划时刻先后入列）
_pending_grants: list[_PendingGrant] = []

# 发放流程是否正在执行（单元素列表：上一轮未结束时跳过本轮检查）
_delivering = [False]


def _schedule_grants(group_id: int, target_ids: list[int]) -> None:
    """把「区」诅咒按第 1、2、3 分钟排入待发放队列。"""
    now = time.monotonic()
    for index, target_id in enumerate(target_ids, start=1):
        due_at = now + _GRANT_INTERVAL_SECONDS * index
        _pending_grants.append(
            _PendingGrant(
                group_id=group_id,
                target_id=target_id,
                due_at=due_at,
                deadline=due_at + _GRANT_GIVE_UP_SECONDS,
            )
        )


def _online_bot() -> Bot | None:
    """返回当前在线的机器人实例，没有可用实例时返回 None。"""
    bot = next(iter(get_bots().values()), None)
    return bot if isinstance(bot, Bot) else None


async def _deliver_due_grants() -> None:
    """把到期的「区」诅咒发放给对应成员（由 _deliver_grants 调用）。

    机器人不在线时保留到期条目、等待下次检查重试；超过放弃时限的
    条目直接丢弃并记录日志。
    """
    now = time.monotonic()
    due = [grant for grant in _pending_grants if grant.due_at <= now]
    if not due:
        return
    curse = get_item(_WORM_ID)
    if curse is None:
        # 不可达：678 与本组道具由同一模块注册；注册缺失时只能丢弃
        logger.error("区从天降：道具 678 未注册，待发放的“区”诅咒已全部丢弃")
        _pending_grants.clear()
        return
    bot = _online_bot()
    if bot is None:
        kept = [
            grant
            for grant in _pending_grants
            if grant not in due or now < grant.deadline
        ]
        dropped = len(_pending_grants) - len(kept)
        if dropped:
            logger.warning(f"区从天降：机器人长时间不在线，已丢弃 {dropped} 份“区”诅咒")
            _pending_grants[:] = kept
        return
    _pending_grants[:] = [grant for grant in _pending_grants if grant not in due]
    for grant in due:
        name = await member_display_name(
            bot, grant.group_id, grant.target_id, _NICKNAME_MAX_LENGTH
        )
        await acquire_curse(bot, grant.group_id, grant.target_id, name, curse)


async def _deliver_grants() -> None:
    """定时检查待发放队列：本轮发放未结束时跳过重复执行。"""
    if _delivering[0] or not _pending_grants:
        return
    _delivering[0] = True
    try:
        await _deliver_due_grants()
    finally:
        _delivering[0] = False


async def _handle_advent_use(context: ItemUseContext) -> None:
    """使用效果：自己立即获得 678，随后每分钟让一名其它成员获得 678。"""
    curse = get_item(_WORM_ID)
    if curse is None:
        # 不可达：678 与 677 由同一模块注册；注册缺失时退还本道具
        logger.error(
            "区从天降：道具 678 未注册，已退还"
            f"（群 {context.group_id}，成员 {context.user_id}）"
        )
        add_item(context.group_id, context.user_id, context.item.item_id)
        await context.send("使用失败：道具 678 不存在，道具已退还。")
        return
    targets = _pick_grant_targets(context.group_id, context.user_id)
    _schedule_grants(context.group_id, targets)
    lines = [f"{context.user_name} 使用了 {context.item.label}。", "感染相生了……"]
    if targets:
        lines.append(
            f"接下来 {len(targets)} 分钟内，{len(targets)} 名决斗分数非零的"
            "群成员将依次被“区”感染。"
        )
    else:
        lines.append("本群没有其它决斗分数非零的成员，只有自己被“区”感染了。")
    await context.send("\n".join(lines))
    await acquire_curse(
        context.bot, context.group_id, context.user_id, context.user_name, curse
    )


register_item(
    ItemDefinition(
        item_id=_ADVENT_ID,
        name="区从天降",
        quality=Quality.WHITE,
        types=(ItemType.CONSUMABLE,),
        description="感染相生了",
        effect=(
            "使用后自己立即获得“678 ！？区区？！”，随后每分钟使一名"
            "（最多三名）决斗分数非零的其它群成员依次获得“678 ！？区区？！”。"
        ),
        condition="无",
        timing="任意",
        note="区从天降，愤怒狰狞",
        handle_use=_handle_advent_use,
    )
)


# 狂区之怒的参数提示文案（使用对象须以 @ 形式给出）
_USAGE_WRATH = f"请 @ 一位群成员，用法：/item.use {_WRATH_ID} @群成员"


def _can_use_wrath(context: ItemUseContext) -> str | None:
    """使用前校验：@ 使用对象、自身的“区”状态与目标；返回错误文案时本次不消耗。"""
    target_id = context.target_id
    if context.args or target_id is None:
        return _USAGE_WRATH
    if get_state(context.group_id, context.user_id, _STATE_KEY) is None:
        return f"只有拥有“区”状态的成员才能使用 {context.item.label}。"
    if target_id in get_bot_list() or context.bot.self_id == str(target_id):
        return f"不能对机器人使用 {context.item.label}。"
    return None


async def _handle_wrath_use(context: ItemUseContext) -> None:
    """使用效果：让被 @ 的成员获得 678 并按诅咒获得流程自动使用。"""
    target_id = context.target_id
    if target_id is None:
        # 不可达：can_use 已校验参数与目标存在
        return
    curse = get_item(_WORM_ID)
    if curse is None:
        # 不可达：678 与 679 由同一模块注册；注册缺失时退还本道具
        logger.error(
            "狂区之怒：道具 678 未注册，已退还"
            f"（群 {context.group_id}，成员 {context.user_id}）"
        )
        add_item(context.group_id, context.user_id, context.item.item_id)
        await context.send("使用失败：道具 678 不存在，道具已退还。")
        return
    if target_id == context.user_id:
        target_name = context.user_name
    else:
        target_name = await member_display_name(
            context.bot, context.group_id, target_id, _NICKNAME_MAX_LENGTH
        )
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}。\n"
        f"{target_name} 的周围响起了扭曲的蠕动与阴暗的爬行声……"
    )
    await acquire_curse(context.bot, context.group_id, target_id, target_name, curse)


register_item(
    ItemDefinition(
        item_id=_WRATH_ID,
        name="狂区之怒",
        quality=Quality.GRAY,
        types=(ItemType.OPTIONAL,),
        description="损人不利己",
        effect=f'使用"/item.use {_WRATH_ID} @使用对象"使其获得"678 ！？区区？！"',
        condition="自己拥有“区”状态",
        timing="任意",
        note="扭曲的蠕动，阴暗的爬行！",
        accepts_args=True,
        can_use=_can_use_wrath,
        handle_use=_handle_wrath_use,
    )
)


scheduler.add_job(
    _deliver_grants,
    "interval",
    seconds=_GRANT_CHECK_INTERVAL_SECONDS,
    id="item_worm_deliver_grants",
    replace_existing=True,
    misfire_grace_time=30,
)
