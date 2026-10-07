"""道具「挑衅」（编号 108）：绿色精良、可选型。

使用 /item.use 108 @成员 强迫自己发起的决斗中尚未接受的对方接受决斗，
并由 Bot 代替对方发送猜拳表情：该决斗中对方自己发送的猜拳表情无效，
由 Bot 发送的表情作为对方的手势参与结算（影响对方的分数）。
仅当目标是自己发起的待接受决斗的对方时可用，使用失败不消耗道具。
"""

from nonebot import logger

from src.plugins._shared.services import (
    DuelProvokeService,
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

# 道具编号
_ITEM_ID = "108"

# 决斗状态与挑衅服务（由决斗插件提供；_framework 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_state_service = require_service(DuelStateService)
_provoke_service = require_service(DuelProvokeService)

_USAGE = f"请 @ 一位群成员，用法：/item.use {_ITEM_ID} @群成员"
_NO_PENDING_DUEL = "你没有向该成员发起等待对方接受的决斗。"
_PROVOKE_FAILED = "挑衅失败：该决斗已不在进行中，道具已退还。"


def _find_pending_target(context: ItemUseContext) -> DuelSnapshot | None:
    """查找使用者向目标成员发起、等待对方接受的决斗快照。"""
    for snapshot in _state_service.list_duels(context.group_id):
        if (
            snapshot.challenger_id == context.user_id
            and snapshot.opponent_id == context.target_id
            and not snapshot.accepted
        ):
            return snapshot
    return None


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：@ 参数与待接受的决斗；返回错误文案时本次不消耗。"""
    if context.args or context.target_id is None:
        return _USAGE
    if context.target_id == context.user_id:
        return f"不能对自己使用 {context.item.label}。"
    if _find_pending_target(context) is None:
        return _NO_PENDING_DUEL
    return None


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：强制对方接受决斗并让 Bot 代替对方发送猜拳表情。"""
    target_id = context.target_id
    if target_id is None:
        # 不可达：can_use 已校验参数与目标存在
        return
    snapshot = await _provoke_service.accept_by_provoke(
        context.group_id, context.user_id, target_id
    )
    if snapshot is None:
        # 不可达：can_use 与消耗同处同步段，决斗状态不会在期间改变
        logger.error(
            f"挑衅：消耗后的强制接受未成功（群 {context.group_id}，"
            f"{context.user_id} 与 {target_id}）"
        )
        add_item(context.group_id, context.user_id, context.item.item_id)
        await context.send(_PROVOKE_FAILED)
        return
    opponent_name = snapshot.opponent_name
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}，{opponent_name} 被激怒，"
        f"接受了决斗邀请（×{snapshot.multiplier}）。\n"
        f"{opponent_name} 的猜拳表情将由 Bot 代替发送。"
    )
    await _provoke_service.send_provoked_gesture(
        context.bot, context.group_id, context.user_id, target_id
    )


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="挑衅",
        quality=Quality.GREEN,
        types=(ItemType.OPTIONAL,),
        description="竖子安敢一战",
        effect=(
            f'使用指令"/item.use {_ITEM_ID} @成员"强迫对方接受决斗，'
            "并由Bot代替对方发送猜拳表情。"
        ),
        condition="无",
        timing="自己发起决斗后，对方接受决斗前",
        note="挑衅并不能保证你胜利",
        accepts_args=True,
        can_use=_can_use,
        handle_use=_handle_use,
    )
)
