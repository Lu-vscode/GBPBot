"""道具「逃之夭夭」（编号 886）：蓝色稀有、复合型（可选型与耐久型）。

使用 /item.use 886 @成员 取消自己与被 @ 成员之间进行中的决斗（不限
发起方向与是否已被接受）；取消经决斗取消服务完成，该决斗不会结算，
与其相关的道具登记（如布衣之怒的刺杀、倍加类道具的使用记录）随取消
事件一并清理。双方之间没有进行中的决斗时不可用。耐久 3/3：每使用
一次对已耗损副本扣 1 点耐久（归零时道具消失），没有耗损副本时开始
耗损一件全新副本；未使用过的副本可正常交换/合成，已耗损的副本不可。
"""

from nonebot import logger

from src.plugins._shared.services import (
    DuelCancelService,
    DuelSnapshot,
    DuelStateService,
    require_service,
)
from src.plugins.item.core import (
    ItemDefinition,
    ItemKind,
    ItemUseContext,
    Quality,
    add_item,
    durable_behavior,
    register_item,
)

# 道具编号与总耐久
_ITEM_ID = "886"
_DURABILITY_TOTAL = 3

# 决斗状态与取消服务（由决斗插件提供；core/scores.py 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_state_service = require_service(DuelStateService)
_cancel_service = require_service(DuelCancelService)

# 用法与错误提示文案
_USAGE = f"请 @ 一位群成员，用法：/item.use {_ITEM_ID} @群成员"
_NO_DUEL = "你们之间没有进行中的决斗。"
_CANCEL_FAILED = "使用失败：该决斗已不在进行中，道具已退还。"


async def _find_duel(
    group_id: int, user_id: int, target_id: int
) -> DuelSnapshot | None:
    """查找双方之间进行中的决斗快照（不限发起方向与是否已接受）。"""
    for snapshot in await _state_service.list_duels(group_id):
        if {snapshot.challenger_id, snapshot.opponent_id} == {user_id, target_id}:
            return snapshot
    return None


async def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：@ 一位成员且双方之间存在进行中的决斗；返回错误文案时本次不消耗。"""
    if context.args or context.target_id is None:
        return _USAGE
    if context.target_id == context.user_id:
        return f"不能对自己使用 {context.item.label}。"
    if await _find_duel(context.group_id, context.user_id, context.target_id) is None:
        return _NO_DUEL
    return None


def _target_name(snapshot: DuelSnapshot, user_id: int) -> str:
    """从决斗快照中取使用者的对手（对方成员）的群昵称。"""
    if snapshot.challenger_id == user_id:
        return snapshot.opponent_name
    return snapshot.challenger_name


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：经决斗取消服务取消自己与被 @ 成员之间进行中的决斗。"""
    target_id = context.target_id
    if target_id is None:
        # 不可达：can_use 已校验参数与目标存在
        return
    snapshot = await _cancel_service.cancel_duel(
        context.group_id, context.user_id, target_id
    )
    if snapshot is None:
        # 不可达：check 与消耗在同一把群锁内，决斗状态不会在期间改变
        logger.error(
            f"逃之夭夭：消耗后决斗取消未成功，道具已退还"
            f"（群 {context.group_id}，成员 {context.user_id}）"
        )
        await add_item(context.group_id, context.user_id, _ITEM_ID)
        await context.send(_CANCEL_FAILED)
        return
    target_name = _target_name(snapshot, context.user_id)
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}，溜之大吉。\n"
        f"{context.user_name} 与 {target_name} 的决斗"
        f"（×{snapshot.multiplier}）取消了。"
    )


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="逃之夭夭",
        quality=Quality.BLUE,
        kind=ItemKind.COMPOSITE,
        description="三十六计，走为上计",
        effect=f'使用"/item.use {_ITEM_ID} @成员"取消自己与对方的决斗。',
        timing="决斗发起后结束前",
        note="逃跑并不可耻",
        durability=f"{_DURABILITY_TOTAL}/{_DURABILITY_TOTAL}",
        accepts_args=True,
        behavior=durable_behavior(
            total=_DURABILITY_TOTAL,
            check=_can_use,
            use=_handle_use,
        ),
    )
)
