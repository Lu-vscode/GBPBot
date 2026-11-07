"""道具「强运」（编号 712）：蓝色稀有、Buff型。

使用后获得持续 3 小时的“强运”状态：决斗中需要发送三个猜拳表情，
至少一个表情获胜则胜利、否则至少一个表情平局则平局、否则失败。

效果经决斗手势服务（DuelGestureService）注册为整局判定效果，并在同一
注册中声明所需手势数量（3 个）：判定由决斗插件的判定引擎统一执行，本
模块不订阅 settling 事件、不改写胜负。

- 双方都持有“强运”时各自同样发送 3 个，按接收顺序一一对应判定后
  统计胜次：仅一方至少获胜一次时该方获胜，否则平局；
- 仅一方持有时逐表情与对方首手势判定后聚合（至少一个胜则胜、否则
  至少一个平则平、否则负），每组手势复用决斗插件的单组判定（含其它
  单组规则，如「特殊手势」的状态触发）；
- 结算文案在默认结算消息前拼接“（<拥有者> 使用了 712 强运）”，
  双方强运对拼时拼接“（双方强运效果对拼，一一对应判定三轮）”。
"""

from src.plugins._shared.services import (
    DuelGestureContext,
    DuelGestureService,
    DuelGestureSide,
    DuelGestureVerdict,
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
    get_state,
    register_item,
)

# 道具编号、状态键与道具显示名
_ITEM_ID = "712"
_STATE_KEY = f"{_ITEM_ID}.luck"
_ITEM_LABEL = f"{_ITEM_ID} 强运"

# “强运”状态的持续时长（秒）：3 小时
_DURATION_SECONDS = 3 * 3600.0

# 持有“强运”时决斗中需要发送的猜拳表情数量
_GESTURE_COUNT = 3

# 整局判定效果的优先级：取道具品质值
_EFFECT_PRIORITY = Quality.BLUE

# 决斗手势服务（由决斗插件提供；_framework 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_gesture_service = require_service(DuelGestureService)


def _has_luck(group_id: int, user_id: int) -> bool:
    """判断成员是否持有有效的“强运”状态。"""
    return get_state(group_id, user_id, _STATE_KEY) is not None


def _required_gestures(group_id: int, user_id: int, _opponent_id: int) -> int:
    """所需手势提供者：持有“强运”时需发送 3 个猜拳表情。

    双方都持有“强运”时各自同样发送 3 个并按接收顺序一一对应判定
    （见 _clash_outcome），所需数量与对手是否持有没有关系。
    """
    if _has_luck(group_id, user_id):
        return _GESTURE_COUNT
    return 1


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：获得持续 3 小时的“强运”状态并发送提示。"""
    add_state(
        context.group_id,
        context.user_id,
        state=ItemState(key=_STATE_KEY, item_id=_ITEM_ID),
        duration_seconds=_DURATION_SECONDS,
    )
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}，"
        f"获得了持续3小时的“强运”状态。"
    )


def _used_prefix(name: str) -> str:
    """生成“（<拥有者> 使用了 712 强运）”前缀。"""
    return f"（{name} 使用了 {_ITEM_LABEL}）\n"


# 双方强运效果对拼时的结算文案前缀（各 3 个手势按接收顺序一一对应判定）
_CLASH_PREFIX = "（双方强运效果对拼，一一对应判定三轮）\n"


def _winner_of(
    outcome: int, winner: DuelGestureSide, loser: DuelGestureSide
) -> int | None:
    """把 1（前者胜）/0（平局）/-1（后者胜）映射为胜者 QQ 号。"""
    if outcome > 0:
        return winner.user_id
    if outcome < 0:
        return loser.user_id
    return None


def _clash_outcome(
    context: DuelGestureContext,
    challenger: DuelGestureSide,
    opponent: DuelGestureSide,
) -> int:
    """双方强运对拼的判定：手势按接收顺序一一对应逐轮判定后统计胜次。

    返回 1（挑战者胜）、0（平局）、-1（挑战者负）：仅一方至少获胜
    一次时该方获胜；双方都至少获胜一次、或双方均未获胜时为平局。
    每轮复用决斗插件的单组判定（含单组规则与基础规则）；手势数量
    不一致（动态时序异常）时按较短一方截断配对。
    """
    outcomes = [
        context.pair_outcome(
            challenger.user_id,
            challenger_gesture,
            opponent.user_id,
            opponent_gesture,
        ).outcome
        for challenger_gesture, opponent_gesture in zip(
            challenger.gestures, opponent.gestures, strict=False
        )
    ]
    challenger_won = any(result > 0 for result in outcomes)
    opponent_won = any(result < 0 for result in outcomes)
    if challenger_won == opponent_won:
        return 0
    return 1 if challenger_won else -1


def _aggregate(
    context: DuelGestureContext, multi: DuelGestureSide, single: DuelGestureSide
) -> int:
    """逐表情聚合强运方的判定：1 强运方胜、0 平局、-1 强运方负。

    每个表情与对方首手势单独判定出胜/平/负，再聚合：至少一个胜则胜；
    否则至少一个平则平；否则负。
    """
    results = [
        context.pair_outcome(
            multi.user_id, gesture, single.user_id, single.gestures[0]
        ).outcome
        for gesture in multi.gestures
    ]
    if 1 in results:
        return 1
    if 0 in results:
        return 0
    return -1


def _judge(context: DuelGestureContext) -> DuelGestureVerdict | None:
    """整局判定效果：任一方持有“强运”时按强运规则裁决本场胜负。

    双方都持有“强运”时按接收顺序一一对应判定（见 _clash_outcome）；
    仅一方持有时逐表情聚合判定（至少一个胜、否则至少一个平、否则
    负）；双方都未持有、或发多个手势的一方未持有“强运”（动态时序
    异常，如状态在出拳后到期）时交回默认判定（返回 None）。
    """
    challenger = context.challenger
    opponent = context.opponent
    challenger_luck = _has_luck(context.group_id, challenger.user_id)
    opponent_luck = _has_luck(context.group_id, opponent.user_id)
    if challenger_luck and opponent_luck:
        outcome = _clash_outcome(context, challenger, opponent)
        return DuelGestureVerdict(
            winner_id=_winner_of(outcome, challenger, opponent),
            detail=_CLASH_PREFIX,
        )
    if len(challenger.gestures) > 1:
        multi, single = challenger, opponent
    elif len(opponent.gestures) > 1:
        multi, single = opponent, challenger
    else:
        return None
    if not _has_luck(context.group_id, multi.user_id):
        return None
    outcome = _aggregate(context, multi, single)
    return DuelGestureVerdict(
        winner_id=_winner_of(outcome, multi, single),
        detail=_used_prefix(multi.name),
    )


_gesture_service.register_effect(
    _EFFECT_PRIORITY, _ITEM_LABEL, _judge, required=_required_gestures
)

register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="强运",
        quality=Quality.BLUE,
        kind=ItemKind.BUFF,
        description="我听见了「强运」的回响",
        effect=(
            "使用后获得持续3小时的“强运”状态：决斗中你需发送3个猜拳表情，"
            "只要有1个表情获胜则你胜利；否则只要有1个表情平局则平局；"
            "否则你失败。"
        ),
        note="运气不够，次数来凑",
        behavior=ItemBehavior(
            use=_handle_use,
        ),
    )
)
