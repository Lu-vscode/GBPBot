"""道具「加倍」组（编号 120、200、400）：由决斗点数加倍衍生的道具组。

- 120 减半（灰色垃圾、可选型）：使用 /item.use 120 @成员 使自己与对方的
  决斗点数整除2（向下取整）；点数归零时取消该场决斗并发送提醒。
- 200 加倍（白色普通、可选型）：使用 /item.use 200 @成员 使自己与对方的
  决斗点数×2；该场决斗自己未使用过 200 或 400 时可用。
- 400 超级加倍（绿色精良、可选型）：同上，决斗点数×4。

效果经决斗插件的点数缩放服务（见 _shared/services.py）直接缩放进行中的
决斗（不限发起方向与是否已被接受）；倍加类道具的「该场决斗已使用」登记
在决斗结束时（结算、超时、被拒绝或被取消）经决斗事件服务清理。
"""

from nonebot import logger

from src.plugins._shared.services import (
    DuelEvent,
    DuelEventKind,
    DuelEventService,
    DuelMultiplierService,
    DuelSnapshot,
    DuelStateService,
    require_service,
)
from src.plugins.item._framework import (
    ItemDefinition,
    ItemType,
    ItemUseContext,
    Quality,
    add_item,
    register_item,
)

# 本组道具编号
_ID_HALVE = "120"

# 倍加组道具：编号、名称、品质、倍数、使用条件
_MULTIPLY_ITEMS = (
    ("200", "加倍", Quality.WHITE, 2, "该场决斗自己未使用过200或400"),
    ("400", "超级加倍", Quality.GREEN, 4, "该场决斗自己未使用过400或200"),
)

# 各编号的缩放参数与消息中的缩放描述：编号 -> (分子, 分母, 描述)
_SCALES = {
    "120": (1, 2, "整除2"),
    "200": (2, 1, "×2"),
    "400": (4, 1, "×4"),
}

# 决斗状态、点数缩放与事件服务（由决斗插件提供；_framework 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_state_service = require_service(DuelStateService)
_multiplier_service = require_service(DuelMultiplierService)
_event_service = require_service(DuelEventService)

# 用法与错误提示文案
_NO_DUEL = "你们之间没有进行中的决斗。"
_ALREADY_USED = "你在该场决斗中已使用过200或400。"
_SCALE_FAILED = "使用失败：该决斗已不在进行中，道具已退还。"

# 该场决斗中已使用过倍加类道具的登记：(群号, 使用者 QQ 号, 对手 QQ 号)
_multiply_usages: set[tuple[int, int, int]] = set()


def _find_duel(group_id: int, user_id: int, target_id: int) -> DuelSnapshot | None:
    """查找双方之间进行中的决斗快照（不限发起方向与是否已接受）。"""
    for snapshot in _state_service.list_duels(group_id):
        if {snapshot.challenger_id, snapshot.opponent_id} == {user_id, target_id}:
            return snapshot
    return None


def _usage_error(context: ItemUseContext) -> str | None:
    """使用前共用校验：@ 参数与双方之间进行中的决斗；
    返回错误文案时本次不消耗。"""
    if context.args or context.target_id is None:
        return f"请 @ 一位群成员，用法：/item.use {context.item.item_id} @群成员"
    if context.target_id == context.user_id:
        return f"不能对自己使用 {context.item.label}。"
    if _find_duel(context.group_id, context.user_id, context.target_id) is None:
        return _NO_DUEL
    return None


def _can_use_multiply(context: ItemUseContext) -> str | None:
    """倍加类道具的使用前校验：共用校验与「该场决斗未使用过」；
    返回错误文案时本次不消耗。"""
    error = _usage_error(context)
    if error is not None:
        return error
    target_id = context.target_id
    if (
        target_id is not None
        and (
            context.group_id,
            context.user_id,
            target_id,
        )
        in _multiply_usages
    ):
        return _ALREADY_USED
    return None


def _target_name(snapshot: DuelSnapshot, user_id: int) -> str:
    """从决斗快照中取使用者的对手（对方成员）的群昵称。"""
    if snapshot.challenger_id == user_id:
        return snapshot.opponent_name
    return snapshot.challenger_name


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：经决斗点数缩放服务缩放本场决斗的点数；
    倍加类道具缩放成功后登记「该场决斗已使用」。"""
    target_id = context.target_id
    if target_id is None:
        # 不可达：can_use 已校验参数与目标存在
        return
    snapshot = _find_duel(context.group_id, context.user_id, target_id)
    if snapshot is None:
        # 不可达：can_use 与消耗同处同步段，决斗状态不会在期间改变
        await _refund(context)
        return
    numerator, denominator, scale_text = _SCALES[context.item.item_id]
    outcome = await _multiplier_service.scale_multiplier(
        context.group_id, context.user_id, target_id, numerator, denominator
    )
    if outcome is None:
        # 不可达：同上
        await _refund(context)
        return
    if context.item.item_id != _ID_HALVE:
        _multiply_usages.add((context.group_id, context.user_id, target_id))
    target_name = _target_name(snapshot, context.user_id)
    if outcome.canceled:
        await context.send(
            f"{context.user_name} 使用了 {context.item.label}，"
            f"与 {target_name} 的决斗点数归零，本场决斗已取消。"
        )
        return
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}，"
        f"与 {target_name} 的决斗点数{scale_text}"
        f"（×{snapshot.multiplier} 变为 ×{outcome.multiplier}）。"
    )


async def _refund(context: ItemUseContext) -> None:
    """缩放未生效时退还道具并提示本次使用失败（不可达分支的兜底）。"""
    logger.error(
        f"{context.item.label}：消耗后点数缩放未生效，道具已退还"
        f"（群 {context.group_id}，成员 {context.user_id}）"
    )
    add_item(context.group_id, context.user_id, context.item.item_id)
    await context.send(_SCALE_FAILED)


def _drop_usages(group_id: int, player_a: int, player_b: int) -> None:
    """清理与指定决斗匹配的倍加登记（决斗结束时调用）。"""
    stale = {
        entry
        for entry in _multiply_usages
        if entry[0] == group_id and {entry[1], entry[2]} == {player_a, player_b}
    }
    _multiply_usages.difference_update(stale)


async def _on_duel_event(event: DuelEvent) -> None:
    """决斗事件处理：决斗结束时（结算、超时、被拒绝或被取消）
    清理倍加登记，避免影响双方之后的新决斗。"""
    if event.kind in (
        DuelEventKind.SETTLING,
        DuelEventKind.TIMEOUT,
        DuelEventKind.REJECTED,
        DuelEventKind.CANCELED,
    ):
        _drop_usages(event.group_id, event.challenger_id, event.opponent_id)


_event_service.subscribe(_on_duel_event)


def _register_multiply_items() -> None:
    """注册本组的两件倍加类道具。"""
    for item_id, name, quality, factor, condition in _MULTIPLY_ITEMS:
        register_item(
            ItemDefinition(
                item_id=item_id,
                name=name,
                quality=quality,
                types=(ItemType.OPTIONAL,),
                description=f"{name}！",
                effect=(
                    f'使用"/item.use {item_id} @成员"使自己与对方的决斗点数×{factor}。'
                ),
                condition=condition,
                timing="决斗开始后结束前",
                note="祝你好运",
                accepts_args=True,
                can_use=_can_use_multiply,
                handle_use=_handle_use,
            )
        )


register_item(
    ItemDefinition(
        item_id=_ID_HALVE,
        name="减半",
        quality=Quality.GRAY,
        types=(ItemType.OPTIONAL,),
        description="懦夫专属",
        effect=f'使用"/item.use {_ID_HALVE} @成员"使自己与对方的决斗点数整除2。',
        condition="无",
        timing="决斗开始后结束前",
        note="没有使用次数限制",
        accepts_args=True,
        can_use=_usage_error,
        handle_use=_handle_use,
    )
)

_register_multiply_items()
