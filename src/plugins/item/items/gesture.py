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
刷新持续时长）。判定经决斗手势服务（DuelGestureService）注册的单组
规则完成（不订阅 settling 事件、不改写胜负）：单方状态触发时该组
手势判该方胜（无论基础猜拳规则如何），双方状态同时触发（抵消）或
都不触发时交回基础规则；结算文案随判定结果给出——获胜方靠状态才
获胜时插入「（<拥有者> 使用了 <道具>）」，双方状态抵消导致平局时
插入「（<拥有者1> 的 <道具1> 与 <拥有者2> 的 <道具2> 抵消了）」。

单组规则对任意一组手势生效（包括「强运」等道具逐组复用的判定），
本模块不再向其它道具模块导出判定接口。
"""

from dataclasses import dataclass

from src.plugins._shared.services import (
    DuelGesturePairContext,
    DuelGesturePairVerdict,
    DuelGestureService,
    require_service,
)
from src.plugins.item._framework import (
    ItemBehavior,
    ItemDefinition,
    ItemKind,
    ItemState,
    ItemUseContext,
    Quality,
    add_state,
    list_states,
    register_item,
)

# 手势状态的持续时长（秒）：1 小时
_DURATION_SECONDS = 3600.0

# 猜拳手势值（与决斗插件的判定一致）：1 布、2 剪刀、3 石头
_PAPER = 1
_SCISSORS = 2
_ROCK = 3

# 判定规则的优先级与显示名：优先级取本组道具的品质值
_RULE_PRIORITY = Quality.GREEN
_RULE_NAME = "121~124 特殊手势"

# 决斗手势服务（由决斗插件提供；_framework 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_gesture_service = require_service(DuelGestureService)


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
    """触发状态的手势值（1 布、2 剪刀、3 石头）；None 表示「破平」
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


def _triggered_meta(
    meta: _GestureItem | None, gesture: int, *, draw: bool
) -> _GestureItem | None:
    """返回因本次手势而触发的状态信息（未持有状态或未触发时为 None）。

    石头/剪刀/布状态在出了对应手势时触发；破平状态在双方手势相同
    （draw 为 True）时触发。
    """
    if meta is None:
        return None
    if meta.gesture is None:
        return meta if draw else None
    return meta if meta.gesture == gesture else None


def _used_prefix(name: str, label: str) -> str:
    """生成"（<拥有者> 使用了 <道具>）"前缀。"""
    return f"（{name} 使用了 {label}）\n"


def _canceled_prefix(name_a: str, label_a: str, name_b: str, label_b: str) -> str:
    """生成"（<拥有者1> 的 <道具1> 与 <拥有者2> 的 <道具2> 抵消了）"前缀。"""
    return f"（{name_a} 的 {label_a} 与 {name_b} 的 {label_b} 抵消了）\n"


def _pair_rule(context: DuelGesturePairContext) -> DuelGesturePairVerdict | None:
    """单组手势判定规则：按双方的手势状态判定一组手势的胜负与文案。

    单方状态触发时该组手势判该方胜（无论基础猜拳规则如何）；双方都
    触发且基础判定分出胜负时交回基础规则，都不触发时同样交回基础规则
    （返回 None）。结算文案：获胜方靠状态才获胜（基础判定中该方不会
    获胜）时插入「（<拥有者> 使用了 <道具>）」，双方状态抵消且基础判定
    为平局时插入抵消文案。
    """
    actor_meta = _held_gesture_meta(context.group_id, context.actor_id)
    opponent_meta = _held_gesture_meta(context.group_id, context.opponent_id)
    draw = context.actor_gesture == context.opponent_gesture
    actor_state = _triggered_meta(actor_meta, context.actor_gesture, draw=draw)
    opponent_state = _triggered_meta(opponent_meta, context.opponent_gesture, draw=draw)
    if actor_state is not None and opponent_state is not None:
        if not draw:
            # 双方状态抵消但基础规则仍能分出胜负：保持基础判定
            return None
        return DuelGesturePairVerdict(
            outcome=0,
            detail=_canceled_prefix(
                context.actor_name,
                actor_state.label,
                context.opponent_name,
                opponent_state.label,
            ),
        )
    if actor_state is not None:
        detail = (
            None
            if context.base_outcome == 1
            else _used_prefix(context.actor_name, actor_state.label)
        )
        return DuelGesturePairVerdict(outcome=1, detail=detail)
    if opponent_state is not None:
        detail = (
            None
            if context.base_outcome == -1
            else _used_prefix(context.opponent_name, opponent_state.label)
        )
        return DuelGesturePairVerdict(outcome=-1, detail=detail)
    return None


_gesture_service.register_pair_rule(_RULE_PRIORITY, _RULE_NAME, _pair_rule)


def _register_gesture_items() -> None:
    """注册本组的四件手势道具。"""
    for meta in _GESTURE_ITEMS:
        register_item(
            ItemDefinition(
                item_id=meta.item_id,
                name=meta.name,
                quality=Quality.GREEN,
                kind=ItemKind.BUFF,
                description=meta.description,
                effect=meta.effect,
                condition=meta.condition,
                note=meta.note,
                labels=((meta.state_key, meta.state_label),),
                behavior=ItemBehavior(
                    check=_can_use,
                    use=_handle_use,
                ),
            )
        )


_register_gesture_items()
