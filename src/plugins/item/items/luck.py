"""道具「强运」（编号 712）：蓝色稀有、Buff型。

使用后获得持续 5 小时的“强运”状态：决斗中需要发送三个猜拳表情，
至少一个表情获胜则胜利、否则至少一个表情平局则平局、否则失败。

- 通过决斗手势配额服务把自己的所需表情数量提高为 3（双方都持有
  “强运”时各自同样发送 3 个，按接收顺序一一对应判定三轮）；
- 单方持有时每个表情与对方手势单独按猜拳规则与手势状态触发规则
  （见 gesture 模块的 micro_result）判定出胜/平/负，再聚合为最终
  结果（至少一个胜则胜、否则至少一个平则平、否则负）；双方都持有
  时按接收顺序一一对应判定后统计胜次：仅一方至少获胜一次时该方
  获胜，否则平局。判定结果经 settling 事件改写胜负；
- 结算文案在默认结算消息前拼接“（<拥有者> 使用了 712 强运）”，
  双方强运对拼时拼接“（双方强运效果对拼，一一对应判定三轮）”
  （其它道具已设置的前缀保持不变，本道具的文案追加在其后）。
"""

from src.plugins._shared.services import (
    DuelEvent,
    DuelEventKind,
    DuelEventService,
    DuelGestureQuotaService,
    require_service,
)
from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemType,
    ItemUseContext,
    Quality,
    add_state,
    get_state,
    register_item,
)
from src.plugins.item.items import gesture

# 道具编号、状态键与道具显示名
_ITEM_ID = "712"
_STATE_KEY = f"{_ITEM_ID}.luck"
_ITEM_LABEL = f"{_ITEM_ID} 强运"

# “强运”状态的持续时长（秒）：5 小时
_DURATION_SECONDS = 5 * 3600.0

# 持有“强运”时决斗中需要发送的猜拳表情数量
_GESTURE_COUNT = 3

# 决斗事件与手势配额服务（由决斗插件提供；_framework 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_event_service = require_service(DuelEventService)
_quota_service = require_service(DuelGestureQuotaService)


def _has_luck(group_id: int, user_id: int) -> bool:
    """判断成员是否持有有效的“强运”状态。"""
    return get_state(group_id, user_id, _STATE_KEY) is not None


def _quota_provider(group_id: int, user_id: int, _opponent_id: int) -> int:
    """手势配额提供者：持有“强运”时需发送 3 个猜拳表情。

    双方都持有“强运”时各自同样发送 3 个并按接收顺序一一对应判定
    （见 _clash_result），所需数量与对手是否持有没有关系。
    """
    if _has_luck(group_id, user_id):
        return _GESTURE_COUNT
    return 1


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：获得持续 5 小时的“强运”状态并发送提示。"""
    add_state(
        context.group_id,
        context.user_id,
        state=ItemState(key=_STATE_KEY, item_id=_ITEM_ID),
        duration_seconds=_DURATION_SECONDS,
    )
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}，"
        f"获得了持续5小时的“强运”状态。"
    )


def _used_prefix(name: str) -> str:
    """生成“（<拥有者> 使用了 712 强运）”前缀。"""
    return f"（{name} 使用了 {_ITEM_LABEL}）\n"


# 双方强运效果对拼时的结算文案前缀（各 3 个手势按接收顺序一一对应判定）
_CLASH_PREFIX = "（双方强运效果对拼，一一对应判定三轮）\n"


def _first_gesture(gestures: tuple[int, ...]) -> int:
    """返回手势序列的第一个手势（调用方保证序列非空）。"""
    return gestures[0]


def _clash_result(
    group_id: int,
    challenger_id: int,
    challenger_gestures: tuple[int, ...],
    opponent_id: int,
    opponent_gestures: tuple[int, ...],
) -> int:
    """双方强运对拼的判定：手势按接收顺序一一对应逐轮判定后统计胜次。

    返回 1（挑战者胜）、0（平局）、-1（挑战者负）：仅一方至少获胜
    一次时该方获胜；双方都至少获胜一次、或双方均未获胜时为平局。
    每轮用 gesture.micro_result 判定（含手势状态触发规则）；手势数量
    不一致（动态时序异常）时按较短一方截断配对。
    """
    outcomes = [
        gesture.micro_result(
            group_id,
            challenger_id,
            challenger_gesture,
            opponent_id,
            opponent_gesture,
        )
        for challenger_gesture, opponent_gesture in zip(
            challenger_gestures, opponent_gestures, strict=False
        )
    ]
    challenger_won = any(result > 0 for result in outcomes)
    opponent_won = any(result < 0 for result in outcomes)
    if challenger_won == opponent_won:
        return 0
    return 1 if challenger_won else -1


def _aggregate_multi_result(
    group_id: int,
    multi_id: int,
    multi_gestures: tuple[int, ...],
    single_id: int,
    single_gesture: int,
) -> int:
    """逐表情聚合强运方的判定：1 强运方胜、0 平局、-1 强运方负。

    每个表情与对方手势单独用 gesture.micro_result 判定出胜/平/负，
    再聚合：至少一个胜则胜；否则至少一个平则平；否则负。
    """
    results = [
        gesture.micro_result(
            group_id, multi_id, actor_gesture, single_id, single_gesture
        )
        for actor_gesture in multi_gestures
    ]
    if 1 in results:
        return 1
    if 0 in results:
        return 0
    return -1


def _apply_result(event: DuelEvent, result: int, first_id: int, second_id: int) -> None:
    """按判定结果改写事件的胜负。

    result 为 1 时 first_id 胜、-1 时 second_id 胜、0 时为平局
    （胜者与负者均置空）。
    """
    if result > 0:
        event.winner_id = first_id
        event.loser_id = second_id
    elif result < 0:
        event.winner_id = second_id
        event.loser_id = first_id
    else:
        event.winner_id = None
        event.loser_id = None


async def _on_duel_event(event: DuelEvent) -> None:
    """决斗事件处理：双方出拳环节结束时按“强运”判定并改写胜负，
    并在默认结算消息前拼接效果文案。

    双方都持有“强运”时按接收顺序一一对应判定（见 _clash_result）；
    仅一方持有时逐表情聚合判定（至少一个胜、否则至少一个平、否则
    负）；双方都未持有时不做任何事（由 gesture 模块按手势状态处理）。
    """
    if event.kind is not DuelEventKind.SETTLING:
        return
    challenger_gestures = event.challenger_gestures
    opponent_gestures = event.opponent_gestures
    if not challenger_gestures or not opponent_gestures:
        # 不可达：settling 事件在双方完成出手后发布
        return
    challenger_luck = _has_luck(event.group_id, event.challenger_id)
    opponent_luck = _has_luck(event.group_id, event.opponent_id)
    if challenger_luck and opponent_luck:
        # 双方强运对拼：各 3 个手势按接收顺序一一对应判定后统计胜次
        result = _clash_result(
            event.group_id,
            event.challenger_id,
            challenger_gestures,
            event.opponent_id,
            opponent_gestures,
        )
        _apply_result(event, result, event.challenger_id, event.opponent_id)
        event.result_prefix = (event.result_prefix or "") + _CLASH_PREFIX
        return
    if len(challenger_gestures) > 1:
        multi_id = event.challenger_id
        multi_name = event.challenger_name
        multi_gestures = challenger_gestures
        single_id = event.opponent_id
        single_gesture = _first_gesture(opponent_gestures)
    elif len(opponent_gestures) > 1:
        multi_id = event.opponent_id
        multi_name = event.opponent_name
        multi_gestures = opponent_gestures
        single_id = event.challenger_id
        single_gesture = _first_gesture(challenger_gestures)
    else:
        return
    if not _has_luck(event.group_id, multi_id):
        # 多手势方未持有强运（动态时序异常，如状态在出拳后到期）：
        # 不改写判定，由决斗插件按逐手势聚合的默认判定结算
        return
    result = _aggregate_multi_result(
        event.group_id, multi_id, multi_gestures, single_id, single_gesture
    )
    _apply_result(event, result, multi_id, single_id)
    event.result_prefix = (event.result_prefix or "") + _used_prefix(multi_name)


_quota_service.add_quota_provider(_quota_provider)
_event_service.subscribe(_on_duel_event)

register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="强运",
        quality=Quality.BLUE,
        types=(ItemType.BUFF,),
        description="我听见了「强运」的回响",
        effect=(
            "使用后获得持续5小时的“强运”状态：决斗中你需发送3个猜拳表情，"
            "只要有1个表情获胜则你胜利；否则只要有1个表情平局则平局；"
            "否则你失败。"
        ),
        condition="无",
        timing="任意",
        note="运气不够，次数来凑",
        handle_use=_handle_use,
    )
)
