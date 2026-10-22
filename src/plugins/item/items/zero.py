"""道具「归零者」（编号 000）：金色传说、永久型。

使用后将自己和随机 10 名决斗分数非零的群成员（不含自己，不够 10 名
时取全部）的决斗分数归零，清空自己的全部道具与状态，并获得永久状态
「归零」与「新生」：

- 「归零」：使用 /item.000.zero 将自己的决斗分数设为 0，冷却 1 天；
- 「新生」：使用 /item.000.rebirth 将自己的决斗分数取绝对值，冷却 1 周。

两个状态在群内唯一：再次有成员使用 000 时，状态由旧持有者转移至新
使用者（旧持有者随后收到失去提示）。不具有对应状态的成员使用两个
指令时静默失败；持有但处于冷却中时提示剩余时间（照常消耗冷却）。

清空状态只移除有效状态：已过期待清理的状态留给定时清理分发到期
效果（如其它诅咒状态的到期结算），不会被本道具提前丢弃。
"""

import math
import random
import time

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.rule import is_type

from src.plugins._shared.config import NICKNAME_MAX_LENGTH
from src.plugins._shared.onebot import send_group_text, sender_display_name
from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemType,
    ItemUseContext,
    Quality,
    add_state,
    add_states,
    add_user_score,
    add_user_scores,
    get_rank_entries,
    get_state,
    get_user_score,
    list_state_holders,
    list_states,
    list_user_items,
    register_item,
    remove_items,
    remove_states,
    state_label,
)

# 道具编号、状态键与状态数据中的使用者昵称快照键
_ITEM_ID = "000"
_ZERO_STATE_KEY = f"{_ITEM_ID}.zero"
_REBIRTH_STATE_KEY = f"{_ITEM_ID}.rebirth"
_OWN_STATE_KEYS = (_ZERO_STATE_KEY, _REBIRTH_STATE_KEY)

# 「归零」与「新生」的冷却时长（秒）：1 天 / 1 周
_ZERO_COOLDOWN_SECONDS = 86400.0
_REBIRTH_COOLDOWN_SECONDS = 604800.0

# 使用 000 时随使用者一同归零的其它成员人数上限
_OTHER_LIMIT = 10

# 消息中群昵称的最大显示长度（字符数，共享配置 NICKNAME_MAX_LENGTH）
_NICKNAME_MAX_LENGTH = NICKNAME_MAX_LENGTH

# 清空状态文案中的状态显示名由产生状态的道具在 state_labels 中声明（见
# _framework.state_label），未声明的回退为道具名称


def _set_score(group_id: int, user_id: int, name: str, score: int) -> None:
    """把成员的决斗分数设为指定值（无变化时不重复写盘）。"""
    current = get_user_score(group_id, user_id)
    if current != score:
        add_user_score(group_id, user_id, name, score - current)


def _state_label(state: ItemState) -> str:
    """返回状态在文案中的显示名（见 _framework.state_label）。"""
    return state_label(state)


def _new_state(key: str, user_name: str) -> ItemState:
    """构造「归零」/「新生」状态（登记时的昵称快照，冷却记录为空）。"""
    return ItemState(key=key, item_id=_ITEM_ID, data={"user_name": user_name})


def _cooldown_remaining(state: ItemState, *, now: float) -> float | None:
    """返回状态的剩余冷却秒数；没有冷却记录或冷却已过时返回 None。"""
    ready_at = state.data.get("ready_at")
    if isinstance(ready_at, bool) or not isinstance(ready_at, (int, float)):
        return None
    remaining = float(ready_at) - now
    return remaining if remaining > 0 else None


def _format_remaining(seconds: float) -> str:
    """把剩余冷却秒数转为简洁的中文时长（如"1天2小时"、"3小时5分钟"）。"""
    minutes = max(1, math.ceil(seconds / 60))
    days, remainder = divmod(minutes, 1440)
    hours, minute = divmod(remainder, 60)
    if days:
        return f"{days}天{hours}小时" if hours else f"{days}天"
    if hours:
        return f"{hours}小时{minute}分钟" if minute else f"{hours}小时"
    return f"{minute}分钟"


def _remove_former_holders(group_id: int, user_id: int) -> list[tuple[int, str]]:
    """移除群内其它成员身上的「归零」与「新生」状态，返回 (QQ 号, 群昵称)。

    昵称取状态登记时保存的快照（缺失时回退为 QQ 号）；两个状态键的
    持有者正常为同一人，按成员合并去重后统一移除。
    """
    former: dict[int, str] = {}
    for key in _OWN_STATE_KEYS:
        for holder_id, state in list_state_holders(group_id, key):
            if holder_id == user_id:
                # 不可达：使用者自己的状态已在清空步骤移除
                continue
            name = str(state.data.get("user_name") or "").strip()
            former[holder_id] = name or f"QQ {holder_id}"
    for holder_id in former:
        remove_states(group_id, holder_id, list(_OWN_STATE_KEYS))
    return list(former.items())


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：清空自己的道具与状态、抽选成员归零分数并转移状态。

    全程无 await 的同步段完成全部数据变更（清空、转移、登记与结算），
    最后依次发送常规消息与状态转移消息。
    """
    group_id = context.group_id
    user_id = context.user_id
    user_name = context.user_name

    # 同步段一：清空使用者的道具与全部有效状态，并快照文案信息
    lost_items = list_user_items(group_id, user_id)
    remove_items(
        group_id,
        user_id,
        [(definition.item_id, count) for definition, count in lost_items],
    )
    lost_states = list_states(group_id, user_id)
    remove_states(group_id, user_id, [state.key for state in lost_states])
    lost_state_labels = [
        _state_label(state) for state in lost_states if state.key not in _OWN_STATE_KEYS
    ]

    # 同步段二：把「归零」与「新生」从旧持有者手中收回（状态转移）
    former_holders = _remove_former_holders(group_id, user_id)

    # 同步段三：自己获得「归零」与「新生」（冷却记录为空，即时可用）
    add_states(
        group_id,
        user_id,
        states=[_new_state(key, user_name) for key in _OWN_STATE_KEYS],
    )

    # 同步段四：自己与随机抽选的其它成员决斗分数归零（批量写盘一次）
    candidates = [entry for entry in get_rank_entries(group_id) if entry[0] != user_id]
    picked = (
        random.sample(candidates, _OTHER_LIMIT)
        if len(candidates) > _OTHER_LIMIT
        else candidates
    )
    targets = [(user_id, user_name), *((entry[0], entry[1]) for entry in picked)]
    score_changes: list[tuple[int, str, int]] = []
    for target_id, target_name in targets:
        current = get_user_score(group_id, target_id)
        if current:
            score_changes.append((target_id, target_name, -current))
    add_user_scores(group_id, score_changes)

    # 常规使用消息（引用触发指令），列表为空的段落不显示
    names = "、".join([user_name, *(name for _, name, _ in picked)])
    lines = ["归零开始了。", f"{names} 的决斗分数归零。"]
    if lost_items:
        item_ids = "、".join(definition.item_id for definition, _ in lost_items)
        lines.append(f"{user_name} 失去了道具 {item_ids}。")
    if lost_state_labels:
        lines.append(f"{user_name} 失去了状态 {'、'.join(lost_state_labels)}。")
    lines.append(f"归零者 {user_name} 获得了“归零”与“新生”")
    await context.send("\n".join(lines))

    # 状态发生转移时在常规消息后补发失去提示（不附加引用）
    for _, former_name in former_holders:
        await send_group_text(
            context.bot,
            group_id,
            f"{former_name} 失去了“归零”与“新生”",
            label="道具消息",
        )


zero_cmd = on_command(f"item.{_ITEM_ID}.zero", rule=is_type(GroupMessageEvent))


@zero_cmd.handle()
async def handle_item_zero(bot: Bot, event: GroupMessageEvent) -> None:
    """处理 /item.000.zero 命令：将自己的决斗分数设为 0（冷却 1 天）。"""
    state = get_state(event.group_id, event.user_id, _ZERO_STATE_KEY)
    if state is None:
        # 不具有“归零”状态：静默失败
        return
    name = sender_display_name(event, _NICKNAME_MAX_LENGTH)
    now = time.time()
    remaining = _cooldown_remaining(state, now=now)
    if remaining is not None:
        await send_group_text(
            bot,
            event.group_id,
            f"你的“归零”还在冷却中，还需 {_format_remaining(remaining)} 才能再次使用。",
            event.message_id,
            label="道具消息",
        )
        return
    # 同步段：结算分数并按当前时刻刷新冷却记录
    current = get_user_score(event.group_id, event.user_id)
    _set_score(event.group_id, event.user_id, name, 0)
    add_state(
        event.group_id,
        event.user_id,
        state=ItemState(
            key=_ZERO_STATE_KEY,
            item_id=_ITEM_ID,
            data={"user_name": name, "ready_at": now + _ZERO_COOLDOWN_SECONDS},
        ),
    )
    if current == 0:
        text = f"{name} 的决斗分数已经为 0，没有变化。"
    else:
        text = f"{name} 的决斗分数归零。"
    await send_group_text(bot, event.group_id, text, event.message_id, label="道具消息")


rebirth_cmd = on_command(f"item.{_ITEM_ID}.rebirth", rule=is_type(GroupMessageEvent))


@rebirth_cmd.handle()
async def handle_item_rebirth(bot: Bot, event: GroupMessageEvent) -> None:
    """处理 /item.000.rebirth 命令：将自己的决斗分数取绝对值（冷却 1 周）。"""
    state = get_state(event.group_id, event.user_id, _REBIRTH_STATE_KEY)
    if state is None:
        # 不具有“新生”状态：静默失败
        return
    name = sender_display_name(event, _NICKNAME_MAX_LENGTH)
    now = time.time()
    remaining = _cooldown_remaining(state, now=now)
    if remaining is not None:
        await send_group_text(
            bot,
            event.group_id,
            f"你的“新生”还在冷却中，还需 {_format_remaining(remaining)} 才能再次使用。",
            event.message_id,
            label="道具消息",
        )
        return
    # 同步段：结算分数并按当前时刻刷新冷却记录
    current = get_user_score(event.group_id, event.user_id)
    reborn = abs(current)
    _set_score(event.group_id, event.user_id, name, reborn)
    add_state(
        event.group_id,
        event.user_id,
        state=ItemState(
            key=_REBIRTH_STATE_KEY,
            item_id=_ITEM_ID,
            data={"user_name": name, "ready_at": now + _REBIRTH_COOLDOWN_SECONDS},
        ),
    )
    if reborn == current:
        text = f"{name} 的决斗分数没有变化。"
    else:
        text = f"{name} 的决斗分数从 {current} 变为 {reborn}。"
    await send_group_text(bot, event.group_id, text, event.message_id, label="道具消息")


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="归零者",
        quality=Quality.GOLD,
        types=(ItemType.PERMANENT,),
        description="让宇宙由零维重获新生",
        effect=(
            "使用后将自己和随机10名决斗分数非零的群成员的决斗分数设为0，"
            "清空自己的道具，清空自己的状态，获得永久状态“归零”和“新生”。\n"
            '“归零”：可以使用"/item.000.zero"将自己的决斗分数设为0，冷却为1天。\n'
            '“新生”：可以使用"/item.000.rebirth"将自己的决斗分数取绝对值，'
            "冷却为1周。"
        ),
        condition="无",
        timing="任意",
        note="“归零”和“新生”状态唯一",
        handle_use=_handle_use,
    )
)
