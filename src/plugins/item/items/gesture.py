"""道具「特殊手势」组（编号 121~124）：由猜拳手势强化的道具组。

- 121 原始力量（绿色精良、Buff型）：使用后获得持续 1 小时的「石头」
  状态：决斗中出了石头即胜利。
- 122 剪刀手（绿色精良、Buff型）：使用后获得持续 1 小时的「剪刀」
  状态：决斗中出了剪刀即胜利。
- 123 以柔克刚（绿色精良、Buff型）：使用后获得持续 1 小时的「布」
  状态：决斗中出了布即胜利。
- 124 平胜（绿色精良、Buff型）：使用后获得持续 1 小时的「破平」
  状态：决斗平局即胜利。

四件道具的状态互斥（持有任一其它手势状态时不可使用，使用同种道具
刷新持续时长）。决斗双方出拳环节结束时经决斗事件服务在 settling
事件中判定：单方状态触发时该方获胜（改写决斗判定），双方状态同时
触发（抵消）或都不触发时按正常规则；结算文案按触发情况在默认结算
消息前拼接——获胜方靠状态才获胜时插入「（<拥有者> 使用了 <道具>）」，
双方状态抵消导致平局时插入「（<拥有者1> 的 <道具1> 与 <拥有者2>
的 <道具2> 抵消了）」，其它道具接管结算时不插入。
"""

from dataclasses import dataclass

from src.plugins._shared.services import (
    DuelEvent,
    DuelEventKind,
    DuelEventService,
    require_service,
)
from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemType,
    ItemUseContext,
    Quality,
    add_state,
    list_states,
    register_item,
)

# 手势状态的持续时长（秒）：1 小时
_DURATION_SECONDS = 3600.0

# 猜拳手势值（与决斗插件的判定一致）：1 剪刀、2 石头、3 布
_SCISSORS = 1
_ROCK = 2
_PAPER = 3

# 正常规则的胜负判定：(挑战者手势, 接受者手势) 在此集合中表示挑战者胜
_WINNING_GESTURES = frozenset({(1, 3), (2, 1), (3, 2)})

# 决斗事件服务（由决斗插件提供；_framework 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_event_service = require_service(DuelEventService)


@dataclass(frozen=True)
class _GestureItem:
    """本组一件道具的静态信息。"""

    item_id: str
    """道具编号。"""

    name: str
    """道具名称。"""

    state_label: str
    """状态的显示名（石头/剪刀/布/破平）。"""

    gesture: int | None
    """触发状态的手势值（1 剪刀、2 石头、3 布）；None 表示「破平」
    状态（平局时触发）。"""

    description: str
    """介绍。"""

    effect: str
    """功能（展示文本）。"""

    condition: str
    """使用条件（展示文本）。"""

    note: str
    """备注。"""

    @property
    def label(self) -> str:
        """道具的显示名（"<编号> <名称>"）。"""
        return f"{self.item_id} {self.name}"

    @property
    def state_key(self) -> str:
        """该道具产生的状态的键。"""
        return f"{self.item_id}.gesture"


# 本组道具的数据表（按编号升序）
_GESTURE_ITEMS = (
    _GestureItem(
        item_id="121",
        name="原始力量",
        state_label="石头",
        gesture=_ROCK,
        description="拳头就是力量",
        effect=("使用后获得持续1小时的“石头”状态：决斗中，如果你出了石头，则你胜利。"),
        condition="没有“剪刀”“布”“破平”状态",
        note="拳头不能战胜一切",
    ),
    _GestureItem(
        item_id="122",
        name="剪刀手",
        state_label="剪刀",
        gesture=_SCISSORS,
        description="吃我一剪！",
        effect=("使用后获得持续1小时的“剪刀”状态：决斗中，如果你出了剪刀，则你胜利。"),
        condition="没有“石头”“布”“破平”状态",
        note="别剪到自己",
    ),
    _GestureItem(
        item_id="123",
        name="以柔克刚",
        state_label="布",
        gesture=_PAPER,
        description="包住对手",
        effect=("使用后获得持续1小时的“布”状态：决斗时，如果你出了布，则你胜利。"),
        condition="没有“石头”“剪刀”“破平”状态",
        note="总有东西是包不住的",
    ),
    _GestureItem(
        item_id="124",
        name="平胜",
        state_label="破平",
        gesture=None,
        description="平者，胜也。",
        effect=("使用后获得持续1小时的“破平”状态：决斗时，如果平局，则你胜利。"),
        condition="没有“石头”“剪刀”“布”状态",
        note="别再平+0了",
    ),
)

# 道具编号 -> 静态信息
_META_BY_ID = {meta.item_id: meta for meta in _GESTURE_ITEMS}


def _held_gesture_meta(group_id: int, user_id: int) -> _GestureItem | None:
    """返回成员身上本组道具产生的有效状态的信息，无此类状态时返回 None。

    四件道具的状态互斥，正常情况下至多持有一个；数据异常同时存在
    多个时取第一个。
    """
    for state in list_states(group_id, user_id):
        meta = _META_BY_ID.get(state.item_id)
        if meta is not None:
            return meta
    return None


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：不持有本组其它手势状态（同种状态可再次使用以刷新
    持续时长）；返回错误文案时本次不消耗。"""
    held = _held_gesture_meta(context.group_id, context.user_id)
    if held is None or held.item_id == context.item.item_id:
        return None
    return f"你已处于“{held.state_label}”状态，无法使用 {context.item.label}。"


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：获得持续 1 小时的手势状态并发送提示。"""
    meta = _META_BY_ID[context.item.item_id]
    add_state(
        context.group_id,
        context.user_id,
        state=ItemState(key=meta.state_key, item_id=meta.item_id),
        duration_seconds=_DURATION_SECONDS,
    )
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}，"
        f"获得了持续1小时的“{meta.state_label}”状态。"
    )


def _normal_winner(
    challenger_gesture: int,
    opponent_gesture: int,
    challenger_id: int,
    opponent_id: int,
) -> int | None:
    """按正常规则判定胜负，返回胜者 QQ 号（平局时返回 None）。"""
    if challenger_gesture == opponent_gesture:
        return None
    if (challenger_gesture, opponent_gesture) in _WINNING_GESTURES:
        return challenger_id
    return opponent_id


def _triggered(meta: _GestureItem | None, gesture: int, *, draw: bool) -> bool:
    """判断一方的状态效果是否触发（无状态时返回 False）。

    石头/剪刀/布状态在出了对应手势时触发；破平状态在正常规则平局
    （双方手势相同）时触发。
    """
    if meta is None:
        return False
    if meta.gesture is None:
        return draw
    return meta.gesture == gesture


def _used_prefix(name: str, label: str) -> str:
    """生成"（<拥有者> 使用了 <道具>）"前缀。"""
    return f"（{name} 使用了 {label}）\n"


def _canceled_prefix(name_a: str, label_a: str, name_b: str, label_b: str) -> str:
    """生成"（<拥有者1> 的 <道具1> 与 <拥有者2> 的 <道具2> 抵消了）"前缀。"""
    return f"（{name_a} 的 {label_a} 与 {name_b} 的 {label_b} 抵消了）\n"


async def _on_duel_event(event: DuelEvent) -> None:
    """决斗事件处理：双方出拳环节结束时按手势状态改写胜负判定，
    并在默认结算消息前拼接状态效果文案。"""
    if event.kind is not DuelEventKind.SETTLING:
        return
    challenger_gesture = event.challenger_gesture
    opponent_gesture = event.opponent_gesture
    if challenger_gesture is None or opponent_gesture is None:
        # 不可达：settling 事件在双方出拳后发布，手势必然已记录
        return
    meta_challenger = _held_gesture_meta(event.group_id, event.challenger_id)
    meta_opponent = _held_gesture_meta(event.group_id, event.opponent_id)
    normal_winner = _normal_winner(
        challenger_gesture,
        opponent_gesture,
        event.challenger_id,
        event.opponent_id,
    )
    normal_draw = normal_winner is None
    challenger_triggered = _triggered(
        meta_challenger, challenger_gesture, draw=normal_draw
    )
    opponent_triggered = _triggered(meta_opponent, opponent_gesture, draw=normal_draw)
    # 单方状态触发时该方获胜（无论正常判定如何）；双方都触发（抵消）
    # 或都不触发时保持正常判定
    if challenger_triggered and not opponent_triggered:
        event.winner_id = event.challenger_id
        event.loser_id = event.opponent_id
    elif opponent_triggered and not challenger_triggered:
        event.winner_id = event.opponent_id
        event.loser_id = event.challenger_id
    # 结算文案：获胜方靠状态才获胜（正常判定中该方不会获胜）时插入
    # "（<拥有者> 使用了 <道具>）"；双方状态抵消（都触发）且抵消导致
    # 平局时插入抵消文案；其余情况（状态未改变判定等）不插入
    label_challenger = meta_challenger.label if meta_challenger is not None else ""
    label_opponent = meta_opponent.label if meta_opponent is not None else ""
    if challenger_triggered and opponent_triggered:
        if normal_draw:
            event.result_prefix = _canceled_prefix(
                event.challenger_name,
                label_challenger,
                event.opponent_name,
                label_opponent,
            )
    elif challenger_triggered and normal_winner != event.challenger_id:
        event.result_prefix = _used_prefix(event.challenger_name, label_challenger)
    elif opponent_triggered and normal_winner != event.opponent_id:
        event.result_prefix = _used_prefix(event.opponent_name, label_opponent)


_event_service.subscribe(_on_duel_event)


def _register_gesture_items() -> None:
    """注册本组的四件手势道具。"""
    for meta in _GESTURE_ITEMS:
        register_item(
            ItemDefinition(
                item_id=meta.item_id,
                name=meta.name,
                quality=Quality.GREEN,
                types=(ItemType.BUFF,),
                description=meta.description,
                effect=meta.effect,
                condition=meta.condition,
                timing="任意",
                note=meta.note,
                can_use=_can_use,
                handle_use=_handle_use,
            )
        )


_register_gesture_items()
