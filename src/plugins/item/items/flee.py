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
from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemType,
    ItemUseContext,
    Quality,
    add_item,
    add_state,
    get_item_count,
    get_state,
    register_item,
    remove_item,
    remove_state,
)

# 道具编号、耐久参数与状态键
_ITEM_ID = "886"
_DURABILITY_TOTAL = 3
_DURABILITY_STATE_KEY = f"{_ITEM_ID}.durability"

# 决斗状态与取消服务（由决斗插件提供；_framework 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_state_service = require_service(DuelStateService)
_cancel_service = require_service(DuelCancelService)

# 用法与错误提示文案
_USAGE = f"请 @ 一位群成员，用法：/item.use {_ITEM_ID} @群成员"
_NO_DUEL = "你们之间没有进行中的决斗。"
_CANCEL_FAILED = "使用失败：该决斗已不在进行中，道具已退还。"


def _find_duel(group_id: int, user_id: int, target_id: int) -> DuelSnapshot | None:
    """查找双方之间进行中的决斗快照（不限发起方向与是否已接受）。"""
    for snapshot in _state_service.list_duels(group_id):
        if {snapshot.challenger_id, snapshot.opponent_id} == {user_id, target_id}:
            return snapshot
    return None


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：@ 一位成员且双方之间存在进行中的决斗；返回错误文案时本次不消耗。"""
    if context.args or context.target_id is None:
        return _USAGE
    if context.target_id == context.user_id:
        return f"不能对自己使用 {context.item.label}。"
    if _find_duel(context.group_id, context.user_id, context.target_id) is None:
        return _NO_DUEL
    return None


def _durability_instances(group_id: int, user_id: int) -> list[int]:
    """返回成员身上处于耗损中的「逃之夭夭」副本的剩余耐久列表。

    全新（未使用过）的副本不在列表中；列表中的每一项为某个副本扣除
    使用次数后的剩余耐久（1~2），按开始耗损的先后排列。
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
                item_id=_ITEM_ID,
                data={"remaining": [int(value) for value in instances]},
            ),
        )
        return
    remove_state(group_id, user_id, _DURABILITY_STATE_KEY)


def _transferable_count(group_id: int, user_id: int) -> int:
    """返回可用于交换/合成的「逃之夭夭」数量（全新副本，耗损副本不计入）。"""
    return max(
        0,
        get_item_count(group_id, user_id, _ITEM_ID)
        - len(_durability_instances(group_id, user_id)),
    )


def _detail_durability(group_id: int, user_id: int) -> str:
    """详情显示的耐久：存在耗损副本时显示第一个（最早开始耗损）副本的
    剩余耐久，否则显示满耐久。"""
    instances = _durability_instances(group_id, user_id)
    if instances:
        return f"{instances[0]}/{_DURABILITY_TOTAL}"
    return f"{_DURABILITY_TOTAL}/{_DURABILITY_TOTAL}"


def _handle_consume(context: ItemUseContext) -> bool:
    """消耗一件「逃之夭夭」：优先对已耗损副本扣 1 点耐久（归零时道具
    消失），没有耗损副本时开始耗损一件全新副本（库存数量不变，耐久
    记录进入状态）。返回是否成功消耗。
    """
    instances = _durability_instances(context.group_id, context.user_id)
    if instances:
        instances[0] -= 1
        if instances[0] <= 0:
            # 该副本耐久归零：道具消失（从库存移除，状态随列表清空清理）
            instances.pop(0)
            if not remove_item(context.group_id, context.user_id, _ITEM_ID):
                logger.error(
                    f"逃之夭夭：耐久归零的副本未能从库存移除"
                    f"（群 {context.group_id}，成员 {context.user_id}）"
                )
        _store_durability_instances(context.group_id, context.user_id, instances)
        return True
    if get_item_count(context.group_id, context.user_id, _ITEM_ID) < 1:
        # 不可达：主模块已在同步段校验持有道具
        return False
    _store_durability_instances(
        context.group_id, context.user_id, [_DURABILITY_TOTAL - 1]
    )
    return True


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
        # 不可达：can_use 与消耗同处同步段，决斗状态不会在期间改变
        logger.error(
            f"逃之夭夭：消耗后决斗取消未成功，道具已退还"
            f"（群 {context.group_id}，成员 {context.user_id}）"
        )
        add_item(context.group_id, context.user_id, _ITEM_ID)
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
        types=(ItemType.COMPOSITE,),
        description="三十六计，走为上计",
        effect=f'使用"/item.use {_ITEM_ID} @成员"取消自己与对方的决斗。',
        condition="无",
        timing="决斗发起后结束前",
        durability=f"{_DURABILITY_TOTAL}/{_DURABILITY_TOTAL}",
        detail_durability=_detail_durability,
        note="强大的对策道具",
        accepts_args=True,
        can_use=_can_use,
        handle_consume=_handle_consume,
        handle_use=_handle_use,
        transferable_count=_transferable_count,
    )
)
