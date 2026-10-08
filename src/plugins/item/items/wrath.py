"""道具「怒」组（编号 094~096）：由《唐雎不辱使命》"天子之怒"、"布衣之怒"、
"庸夫之怒"的典故改编的道具组。

- 094 庸夫之怒（灰色垃圾、消耗型）：使用后自己的决斗分数-80、决斗高分榜
  第一名分数-1（以使用前的第一名为准，自己即第一名时共-81）；群内没有
  正分成员（高分榜为空）时不可用。
- 095 天子之怒（紫色史诗、复合型）：使用 /item.use 095 @成员 将其决斗分数
  设为-1；仅自身位列决斗高分榜第一名、对方决斗分数非负时可用。道具信息
  显示的耐久为 5/1000000，实际耐久 5/5：每使用一次对已耗损副本扣 1 点
  耐久（归零时道具消失），没有耗损副本时开始耗损一件全新副本；未使用过
  （耐久 5）的副本可正常交换/合成，已耗损的副本不可。
- 096 布衣之怒（白色普通、可选型）：在决斗中使用 /item.use 096 @成员 发起
  刺杀（自己的决斗分数为 0、对方位列决斗高分榜第一名、双方存在进行中的
  决斗，决斗是自己/对方发起的均可）；决斗出拳环节结束时接管结算：自己
  获胜则双方分数设为-1，平局或落败则自己分数设为-1。对方拒绝、决斗超时
  或决斗被取消时不介入（道具已消耗、不退还），由决斗按原始逻辑处理。

本模块经决斗插件的事件服务订阅决斗生命周期事件（见 _shared/services.py）：
096 在 settling 事件中设置 claimed 接管结算（决斗跳过默认结算与播报），
并在 timeout/rejected/canceled 事件中清理等待中的刺杀登记。
"""

import random

from nonebot import get_bots, logger
from nonebot.adapters.onebot.v11 import Bot

from src.plugins._shared.onebot import member_display_name, send_group_text
from src.plugins._shared.services import (
    DuelEvent,
    DuelEventKind,
    DuelEventService,
    DuelSnapshot,
    DuelStateService,
    require_service,
)
from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemType,
    ItemUseContext,
    Quality,
    add_item,
    add_state,
    add_user_score,
    get_highest_score_user,
    get_item_count,
    get_state,
    get_user_score,
    register_item,
    remove_item,
    remove_state,
)

# 本组道具编号
_ID_FOOL = "094"
_ID_KING = "095"
_ID_COMMONER = "096"

# 消息中群昵称的最大显示长度（字符数，与主模块保持一致）
_NICKNAME_MAX_LENGTH = 12

# 「天子之怒」的耐久参数与状态键：显示耐久 5/1000000，实际总耐久 5
_DURABILITY_TOTAL = 5
_DURABILITY_DISPLAY = f"{_DURABILITY_TOTAL}/1000000"
_DURABILITY_STATE_KEY = f"{_ID_KING}.durability"

# 决斗状态与事件服务（由决斗插件提供；_framework 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_state_service = require_service(DuelStateService)
_event_service = require_service(DuelEventService)

# 「庸夫之怒」的分数变化量
_FOOL_SELF_COST = 80
_FOOL_FIRST_COST = 1

# 「布衣之怒」获刺成功时的天象文案（随机选一）
_OMENS = ("彗星袭月", "白虹贯日", "苍鹰击于殿上")


def _can_use_fool(context: ItemUseContext) -> str | None:
    """庸夫之怒的使用前校验：群内存在决斗高分榜第一名；返回错误文案时不消耗。"""
    if get_highest_score_user(context.group_id) is None:
        return "当前没有位列决斗高分榜第一名的成员。"
    return None


async def _handle_fool(context: ItemUseContext) -> None:
    """庸夫之怒：自己 -80，使用前确定的高分榜第一名 -1（可能为同一人）。"""
    first_id = get_highest_score_user(context.group_id)
    if first_id is None:
        # 不可达：can_use 与消耗同处同步段，第一名不会在期间消失
        logger.error(
            f"庸夫之怒：消耗后群 {context.group_id} 没有决斗高分榜第一名，道具已退还"
        )
        add_item(context.group_id, context.user_id, context.item.item_id)
        await context.send("使用失败：当前没有位列决斗高分榜第一名的成员，道具已退还。")
        return
    if first_id == context.user_id:
        first_name = context.user_name
    else:
        first_name = await member_display_name(
            context.bot, context.group_id, first_id, _NICKNAME_MAX_LENGTH
        )
    context.add_score(-_FOOL_SELF_COST)
    add_user_score(context.group_id, first_id, first_name, -_FOOL_FIRST_COST)
    await context.send(
        f"{context.user_name} 以头抢地，{first_name} 跄。\n\n"
        f"{context.user_name} 决斗分数-{_FOOL_SELF_COST}，"
        f"{first_name} 决斗分数-{_FOOL_FIRST_COST}."
    )


def _durability_instances(group_id: int, user_id: int) -> list[int]:
    """返回成员身上处于耗损中的「天子之怒」副本的剩余耐久列表。

    全新（未使用过）的副本不在列表中；列表中的每一项为某个副本扣除
    使用次数后的剩余耐久（1~4），按开始耗损的先后排列。
    """
    state = get_state(group_id, user_id, _DURABILITY_STATE_KEY)
    if state is None:
        return []
    raw = state.data.get("remaining")
    if not isinstance(raw, list):
        return []
    return [
        value
        for value in raw
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    ]


def _store_durability_instances(
    group_id: int, user_id: int, instances: list[int]
) -> None:
    """保存耗损中的副本耐久列表；列表为空时移除状态。"""
    if instances:
        add_state(
            group_id,
            user_id,
            state=ItemState(
                key=_DURABILITY_STATE_KEY,
                item_id=_ID_KING,
                data={"remaining": [int(value) for value in instances]},
            ),
        )
        return
    remove_state(group_id, user_id, _DURABILITY_STATE_KEY)


def _transferable_count_kings(group_id: int, user_id: int) -> int:
    """返回可用于交换/合成的「天子之怒」数量（全新副本，耗损副本不计入）。"""
    return max(
        0,
        get_item_count(group_id, user_id, _ID_KING)
        - len(_durability_instances(group_id, user_id)),
    )


def _detail_durability_king(group_id: int, user_id: int) -> str:
    """「天子之怒」详情显示的耐久：存在耗损副本时显示第一个（最早开始
    耗损）副本的剩余耐久，否则显示满耐久。"""
    instances = _durability_instances(group_id, user_id)
    if instances:
        return f"{instances[0]}/1000000"
    return _DURABILITY_DISPLAY


def _handle_consume_king(context: ItemUseContext) -> bool:
    """消耗一件「天子之怒」：优先对已耗损副本扣 1 点耐久（归零时道具
    消失），没有耗损副本时开始耗损一件全新副本（库存数量不变，耐久
    记录进入状态）。返回是否成功消耗。
    """
    instances = _durability_instances(context.group_id, context.user_id)
    if instances:
        instances[0] -= 1
        if instances[0] <= 0:
            # 该副本耐久归零：道具消失（从库存移除，状态随列表清空清理）
            instances.pop(0)
            if not remove_item(context.group_id, context.user_id, _ID_KING):
                logger.error(
                    f"天子之怒：耐久归零的副本未能从库存移除"
                    f"（群 {context.group_id}，成员 {context.user_id}）"
                )
        _store_durability_instances(context.group_id, context.user_id, instances)
        return True
    if get_item_count(context.group_id, context.user_id, _ID_KING) < 1:
        # 不可达：主模块已在同步段校验持有道具
        return False
    _store_durability_instances(
        context.group_id, context.user_id, [_DURABILITY_TOTAL - 1]
    )
    return True


def _can_use_king(context: ItemUseContext) -> str | None:
    """天子之怒的使用前校验：@ 参数、高分榜条件与目标分数；返回错误文案时不消耗。"""
    if context.args or context.target_id is None:
        return f"请 @ 一位群成员，用法：/item.use {_ID_KING} @群成员"
    if context.target_id == context.user_id:
        return f"不能对自己使用 {context.item.label}。"
    if get_highest_score_user(context.group_id) != context.user_id:
        return f"只有位列决斗高分榜第一名的成员才能使用 {context.item.label}。"
    if get_user_score(context.group_id, context.target_id) < 0:
        return f"{context.item.label} 只能诛杀决斗分数非负的成员。"
    return None


async def _handle_king(context: ItemUseContext) -> None:
    """天子之怒：把被 @ 成员的决斗分数设为 -1。"""
    target_id = context.target_id
    if target_id is None:
        # 不可达：can_use 已校验参数与目标存在
        return
    target_name = await member_display_name(
        context.bot, context.group_id, target_id, _NICKNAME_MAX_LENGTH
    )
    old = get_user_score(context.group_id, target_id)
    add_user_score(context.group_id, target_id, target_name, -1 - old)
    await context.send(
        f"{context.user_name} 诛杀了 {target_name}。\n{target_name} 的决斗分数变为-1。"
    )


# 等待接管结算的刺杀：(群号, 使用者 QQ 号, 刺杀对象 QQ 号)
_assassinations: set[tuple[int, int, int]] = set()


def _find_duel(group_id: int, user_id: int, target_id: int) -> DuelSnapshot | None:
    """查找使用者与目标成员进行中的决斗快照（不限发起方向与是否已接受）。"""
    for snapshot in _state_service.list_duels(group_id):
        if {snapshot.challenger_id, snapshot.opponent_id} == {user_id, target_id}:
            return snapshot
    return None


def _assassination_error(context: ItemUseContext, target_id: int) -> str | None:
    """布衣之怒的刺杀条件校验：自身分数为 0、目标位列高分榜第一名、
    双方存在进行中的决斗且未重复发起；不满足时返回错误文案。"""
    if get_user_score(context.group_id, context.user_id) != 0:
        return f"只有决斗分数为 0 的成员才能使用 {context.item.label}。"
    if get_highest_score_user(context.group_id) != target_id:
        return f"只有位列决斗高分榜第一名的成员才能被 {context.item.label} 刺杀。"
    if _find_duel(context.group_id, context.user_id, target_id) is None:
        return "你与该成员没有进行中的决斗。"
    if (context.group_id, context.user_id, target_id) in _assassinations:
        return "你已在该决斗中发起过刺杀。"
    return None


def _can_use_commoner(context: ItemUseContext) -> str | None:
    """布衣之怒的使用前校验：@ 参数与刺杀条件；返回错误文案时不消耗。"""
    target_id = context.target_id
    if context.args or target_id is None:
        return f"请 @ 一位群成员，用法：/item.use {_ID_COMMONER} @群成员"
    if target_id == context.user_id:
        return f"不能对自己使用 {context.item.label}。"
    return _assassination_error(context, target_id)


async def _handle_commoner(context: ItemUseContext) -> None:
    """布衣之怒：登记刺杀（决斗出拳环节结束时接管结算）并发送播报。"""
    target_id = context.target_id
    if target_id is None:
        # 不可达：can_use 已校验参数与目标存在
        return
    # 同步段登记刺杀，防止并发指令在同一场决斗中重复发起
    _assassinations.add((context.group_id, context.user_id, target_id))
    await context.send(f"{context.user_name} 挺剑而起。")


def _set_score(group_id: int, user_id: int, name: str, score: int) -> None:
    """把成员的决斗分数设为指定值。"""
    current = get_user_score(group_id, user_id)
    if current != score:
        add_user_score(group_id, user_id, name, score - current)


def _match_assassination(
    group_id: int, player_a: int, player_b: int
) -> tuple[int, int, int] | None:
    """查找与指定决斗匹配的刺杀登记（参与者无序匹配）；不取出。"""
    return next(
        (
            entry
            for entry in _assassinations
            if entry[0] == group_id and {entry[1], entry[2]} == {player_a, player_b}
        ),
        None,
    )


def _drop_assassinations(group_id: int, player_a: int, player_b: int) -> None:
    """清理与指定决斗匹配的刺杀登记（决斗被拒绝或超时结束时）。"""
    match = _match_assassination(group_id, player_a, player_b)
    if match is not None:
        _assassinations.discard(match)


async def _send_duel_text(event: DuelEvent, text: str) -> None:
    """向决斗所在群发送道具结算消息（机器人离线或发送失败仅记录日志）。"""
    bot = get_bots().get(str(event.bot_self_id))
    if not isinstance(bot, Bot):
        logger.warning(
            f"刺杀结算消息未发送：机器人 {event.bot_self_id} 不在线"
            f"（群 {event.group_id}）"
        )
        return
    await send_group_text(bot, event.group_id, text, label="道具消息")


async def _on_duel_event(event: DuelEvent) -> None:
    """决斗事件处理：决斗被拒绝、超时或被取消时清理刺杀登记；
    出拳环节结束时由「布衣之怒」接管结算（设置 claimed 阻止决斗默认
    结算与播报）。"""
    if event.kind in (
        DuelEventKind.TIMEOUT,
        DuelEventKind.REJECTED,
        DuelEventKind.CANCELED,
    ):
        _drop_assassinations(event.group_id, event.challenger_id, event.opponent_id)
        return
    if event.kind is not DuelEventKind.SETTLING:
        return
    entry = _match_assassination(event.group_id, event.challenger_id, event.opponent_id)
    if entry is None:
        return
    _assassinations.discard(entry)
    _, user_id, target_id = entry
    if event.challenger_id == user_id:
        user_name = event.challenger_name
        target_name = event.opponent_name
    else:
        user_name = event.opponent_name
        target_name = event.challenger_name
    # 同步段完成分数改名与接管标记，再发送结算消息
    if event.winner_id == user_id:
        _set_score(event.group_id, user_id, user_name, -1)
        _set_score(event.group_id, target_id, target_name, -1)
        text = (
            f"{user_name} 刺 {target_name}，{random.choice(_OMENS)}。\n\n"
            f"{user_name} 和 {target_name} 的决斗分数变为-1。"
        )
    else:
        _set_score(event.group_id, user_id, user_name, -1)
        text = (
            f"{target_name} 环柱走，{user_name} 废。\n\n"
            f"{user_name} 决斗分数变为-1，{target_name} 决斗分数不变。"
        )
    event.claimed = True
    logger.info(f"群 {event.group_id} 中 {user_id} 对 {target_id} 的刺杀已接管决斗结算")
    await _send_duel_text(event, text)


_event_service.subscribe(_on_duel_event)

register_item(
    ItemDefinition(
        item_id=_ID_FOOL,
        name="庸夫之怒",
        quality=Quality.GRAY,
        types=(ItemType.CONSUMABLE,),
        description="庸夫之怒，免冠徒跣，以头抢地。",
        effect=(
            f"使用后自己的决斗分数-{_FOOL_SELF_COST}，"
            f"决斗高分榜第一名分数-{_FOOL_FIRST_COST}。"
        ),
        condition="无",
        timing="任意",
        note="地动山摇",
        can_use=_can_use_fool,
        handle_use=_handle_fool,
    )
)

register_item(
    ItemDefinition(
        item_id=_ID_KING,
        name="天子之怒",
        quality=Quality.PURPLE,
        types=(ItemType.COMPOSITE,),
        description="天子之怒，伏尸百万，流血千里。",
        effect=f'使用"/item.use {_ID_KING} @成员"将其决斗分数设为-1。',
        condition="自身位列决斗高分榜第一名，对方决斗分数非负",
        timing="任意",
        durability=_DURABILITY_DISPLAY,
        detail_durability=_detail_durability_king,
        note="大清亡了",
        accepts_args=True,
        can_use=_can_use_king,
        handle_consume=_handle_consume_king,
        handle_use=_handle_king,
        transferable_count=_transferable_count_kings,
    )
)

register_item(
    ItemDefinition(
        item_id=_ID_COMMONER,
        name="布衣之怒",
        quality=Quality.WHITE,
        types=(ItemType.OPTIONAL,),
        description="若士必怒，伏尸二人，流血五步，天下缟素。",
        effect=(
            f'在决斗中使用"/item.use {_ID_COMMONER} @成员"发起刺杀。'
            "若决斗获胜，将自己和对方的决斗分数设为-1；"
            "若平手或落败，将自己的决斗分数设为-1，对方决斗分数不变；"
            "若决斗超时，双方分数不变。"
        ),
        condition="自己的决斗分数为0，对方位列决斗高分榜第一名",
        timing="决斗开始后结束前",
        note="怀怒未发，休祲降于天。",
        accepts_args=True,
        can_use=_can_use_commoner,
        handle_use=_handle_commoner,
    )
)
