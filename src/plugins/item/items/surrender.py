"""道具「投降」（编号 128）：灰色垃圾、复合型（可选型与 Buff 型）。

使用 /item.use 128 @成员 向双方之间进行中的决斗投降：该决斗跳过出拳
阶段，直接按使用者落败进入结算（分数加减、结算播报与 settled 事件等与
正常出拳结束的决斗一致，胜负不再经过手势判定）。认输经决斗认输服务
（DuelForfeitService）完成，使用者的投降说明与「怯战」说明拼在默认结算
消息前；结算被其它道具接管时由接管方自行播报。

使用后使用者获得持续 1 天的「怯战」状态：状态有效期间无法发起决斗
（发起流程在 challenge 事件中被本模块阻断），也无法接受决斗（接受流程
在 accepting 事件中被本模块阻断）；仍可被他人发起决斗，也可以拒绝决斗。
状态到期后静默解除：过期状态由定时清理直接删除，不发送任何提示消息
（减少 Bot 不必要的主动消息），决斗功能随之恢复。

仅当双方之间的决斗已进入出拳阶段（对方已接受）时可用：没有决斗、决斗
尚未被接受、@ 自己或附带其它参数时使用失败，不消耗道具。
"""

from nonebot import logger

from src.plugins._shared.services import (
    DuelEvent,
    DuelEventKind,
    DuelEventService,
    DuelForfeitService,
    DuelSnapshot,
    DuelStateService,
    require_service,
)
from src.plugins.item.core import (
    ItemBehavior,
    ItemDefinition,
    ItemKind,
    ItemState,
    ItemUseContext,
    Quality,
    add_item,
    add_state,
    get_state,
    register_item,
)

# 道具编号、状态键与状态持续时长（秒）：1 天
_ITEM_ID = "128"
_STATE_KEY = f"{_ITEM_ID}.cowardice"
_DURATION_SECONDS = 86400.0

# 决斗状态、认输与事件服务（由决斗插件提供；core/scores.py 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_state_service = require_service(DuelStateService)
_forfeit_service = require_service(DuelForfeitService)
_event_service = require_service(DuelEventService)

# 用法与错误提示文案
_USAGE = f"请 @ 一位群成员，用法：/item.use {_ITEM_ID} @群成员"
_NO_DUEL = "你们之间没有进行中的决斗。"
_NOT_STARTED = "你们之间的决斗还没有开始，现在投降也没用。"
_FORFEIT_FAILED = "使用失败：该决斗已不在进行中，道具已退还。"

# 「怯战」状态阻断决斗时发送的提示文案
_BLOCK_CHALLENGE = "你处于「怯战」状态，无法发起决斗。"
_BLOCK_ACCEPT = "你处于「怯战」状态，无法接受决斗（可以拒绝）。"


async def _has_state(group_id: int, user_id: int) -> bool:
    """判断成员是否持有有效的「怯战」状态（已过期等待清理的不计入）。"""
    return await get_state(group_id, user_id, _STATE_KEY) is not None


async def _on_duel_event(event: DuelEvent) -> None:
    """决斗事件处理：持有「怯战」状态的成员不能发起决斗（challenge），
    也不能接受他人的决斗邀请（accepting，决斗保持等待接受）；被他人
    发起决斗与拒绝决斗不受影响。"""
    if event.kind is DuelEventKind.CHALLENGE:
        if await _has_state(event.group_id, event.challenger_id):
            event.block_reason = _BLOCK_CHALLENGE
        return
    if event.kind is DuelEventKind.ACCEPTING and await _has_state(
        event.group_id, event.opponent_id
    ):
        event.block_reason = _BLOCK_ACCEPT


_event_service.subscribe(_on_duel_event)


async def _find_duel(
    group_id: int, user_id: int, target_id: int
) -> DuelSnapshot | None:
    """查找双方之间进行中的决斗快照（不限发起方向与是否已接受）。"""
    for snapshot in await _state_service.list_duels(group_id):
        if {snapshot.challenger_id, snapshot.opponent_id} == {user_id, target_id}:
            return snapshot
    return None


def _opponent_name(snapshot: DuelSnapshot, user_id: int) -> str:
    """从决斗快照中取使用者的对手（对方成员）的群昵称。"""
    if snapshot.challenger_id == user_id:
        return snapshot.opponent_name
    return snapshot.challenger_name


async def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：@ 一位成员且双方之间的决斗已进入出拳阶段；
    返回错误文案时本次不消耗。"""
    target_id = context.target_id
    if context.args or target_id is None:
        return _USAGE
    if target_id == context.user_id:
        return f"不能对自己使用 {context.item.label}。"
    snapshot = await _find_duel(context.group_id, context.user_id, target_id)
    if snapshot is None:
        return _NO_DUEL
    if not snapshot.accepted:
        return _NOT_STARTED
    return None


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：经决斗认输服务让进行中的决斗跳过出拳阶段、按自己落败
    结算，结算成功后获得持续 1 天的「怯战」状态。"""
    target_id = context.target_id
    if target_id is None:
        # 不可达：can_use 已校验参数与目标存在
        return
    snapshot = await _find_duel(context.group_id, context.user_id, target_id)
    if snapshot is None:
        # 校验与消耗之间对方的手势可能已使决斗结算：退还道具
        logger.error(
            f"投降：消耗后未找到进行中的决斗，道具已退还"
            f"（群 {context.group_id}，成员 {context.user_id}）"
        )
        await add_item(context.group_id, context.user_id, _ITEM_ID)
        await context.send(_FORFEIT_FAILED)
        return
    target_name = _opponent_name(snapshot, context.user_id)
    settled = await _forfeit_service.forfeit_duel(
        context.bot,
        context.group_id,
        context.user_id,
        target_id,
        prefix=(
            f"{context.user_name} 使用了 {context.item.label}，"
            f"向 {target_name} 举起了白旗。\n"
            f"此后 1 天内，{context.user_name} 将处于「怯战」状态："
            "无法发起决斗，也无法接受决斗。\n"
        ),
    )
    if settled is None:
        # 认输前对方的手势恰好使决斗结算：退还道具
        logger.error(
            f"投降：消耗后认输结算未成功，道具已退还"
            f"（群 {context.group_id}，成员 {context.user_id} -> {target_id}）"
        )
        await add_item(context.group_id, context.user_id, _ITEM_ID)
        await context.send(_FORFEIT_FAILED)
        return
    # 结算成功后才登记「怯战」状态：结算失败时道具已退还，不留状态
    await add_state(
        context.group_id,
        context.user_id,
        state=ItemState(key=_STATE_KEY, item_id=_ITEM_ID),
        duration_seconds=_DURATION_SECONDS,
    )


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="投降",
        quality=Quality.GRAY,
        kind=ItemKind.COMPOSITE,
        description="向决斗对象投降",
        effect=(
            f'使用"/item.use {_ITEM_ID} @使用对象"使自己与对方的决斗跳过'
            "出拳阶段，按照自己失败进入结算阶段。\n"
            "使用后获得持续 1 天的「怯战」状态：无法发起决斗，无法接受决斗。"
        ),
        timing="决斗出拳阶段",
        note="识时务者为俊杰",
        labels=((_STATE_KEY, "怯战"),),
        accepts_args=True,
        behavior=ItemBehavior(
            check=_can_use,
            use=_handle_use,
        ),
    )
)
