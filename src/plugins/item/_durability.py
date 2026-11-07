"""耐久型道具的公共逻辑：每件库存代表一个副本，使用时开始耗损而不扣库存。

库存中的每件道具代表一个副本，未使用过的副本是"全新"的，可用于交换与
合成。使用一件道具时不扣减库存数量，而是优先对已耗损副本扣 1 点耐久
（耐久归零时该副本从库存移除）；没有耗损副本时把一件全新副本记为"耗损
中"，其剩余耐久保存在该成员的持久状态 `<道具编号>.durability` 中（附加
数据 remaining 为各副本剩余耐久列表，按开始耗损的先后排列）。

道具模块提供总耐久（与展示用总耐久）、校验与效果即可获得完整行为块：

    _BEHAVIOR = durable_behavior(total=3, check=_can_use, use=_handle_use)

行为块不含具体道具，可在同组道具间共享（如保险 132~136）；消耗、可交易
数量与详情耐久均按被使用/查看的道具编号自动定位状态。其它道具转移耗损
副本时调用 move_damaged_instance 一并转移剩余耐久（如「强塞」127）。
示例见 flee.py（886，总耐久 3）、wrath.py（095，总耐久 4、展示
4/1000000）与 insurance.py（132~136，总耐久 3）。

本模块与 _framework.py 同属道具插件内部，仅由 items/ 下的道具模块导入。
"""

from collections.abc import Awaitable, Callable
from functools import partial

from nonebot import logger

from src.plugins.item._framework import (
    ItemBehavior,
    ItemState,
    ItemUseContext,
    add_state,
    get_item_count,
    get_state,
    remove_item,
    remove_state,
)

# 耗损状态的键后缀：道具的耗损状态键统一为 "<道具编号>.durability"
_STATE_KEY_SUFFIX = ".durability"

# 状态附加数据中保存副本剩余耐久列表的键
_INSTANCES_KEY = "remaining"


def _state_key(item_id: str) -> str:
    """返回道具的耗损状态键（约定：<道具编号>.durability）。"""
    return f"{item_id}{_STATE_KEY_SUFFIX}"


async def _remaining_durabilities(
    group_id: int, user_id: int, item_id: str
) -> list[int]:
    """返回成员身上处于耗损中的该道具副本的剩余耐久列表。

    全新（未使用过）的副本不在列表中；列表中的每一项为某个副本扣除
    使用次数后的剩余耐久（1~总耐久-1），按开始耗损的先后排列。
    """
    state = await get_state(group_id, user_id, _state_key(item_id))
    if state is None:
        return []
    raw = state.data.get(_INSTANCES_KEY)
    if not isinstance(raw, list):
        return []
    return [
        value
        for value in raw
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    ]


async def _store(
    group_id: int,
    user_id: int,
    *,
    item_id: str,
    values: list[int],
) -> None:
    """保存耗损中的副本耐久列表；列表为空时移除状态。"""
    if values:
        await add_state(
            group_id,
            user_id,
            state=ItemState(
                key=_state_key(item_id),
                item_id=item_id,
                data={_INSTANCES_KEY: [int(value) for value in values]},
            ),
        )
        return
    await remove_state(group_id, user_id, _state_key(item_id))


async def _consume(context: ItemUseContext, *, total: int) -> bool:
    """消耗一件副本（ItemBehavior.consume 的实现），返回是否成功消耗。

    优先对已耗损副本扣 1 点耐久（归零时该副本从库存移除）；没有耗损
    副本时开始耗损一件全新副本（库存数量不变，耐久记录进入状态）。
    """
    group_id = context.group_id
    user_id = context.user_id
    item_id = context.item.item_id
    values = await _remaining_durabilities(group_id, user_id, item_id)
    if values:
        values[0] -= 1
        if values[0] <= 0:
            # 该副本耐久归零：道具消失（从库存移除，状态随列表清空清理）
            values.pop(0)
            if not await remove_item(group_id, user_id, item_id):
                logger.error(
                    f"{context.item.name}：耐久归零的副本未能从库存移除"
                    f"（群 {group_id}，成员 {user_id}）"
                )
        await _store(group_id, user_id, item_id=item_id, values=values)
        return True
    if await get_item_count(group_id, user_id, item_id) < 1:
        # 调用方已在群锁内校验持有道具，走到这里说明库存与校验不一致
        return False
    await _store(group_id, user_id, item_id=item_id, values=[total - 1])
    return True


async def _transferable_count(group_id: int, user_id: int, item_id: str) -> int:
    """返回可用于交换/合成的副本数量（ItemBehavior.transferable 的实现）。

    只有全新副本可交易：库存数量扣除处于耗损中的副本数。
    """
    return max(
        0,
        await get_item_count(group_id, user_id, item_id)
        - len(await _remaining_durabilities(group_id, user_id, item_id)),
    )


async def _durability_text(
    group_id: int,
    user_id: int,
    item_id: str,
    *,
    total: int,
    display_total: int | None = None,
) -> str:
    """详情显示的耐久（ItemBehavior.durability_text 的实现）。

    存在耗损副本时显示第一个（最早开始耗损）副本的剩余耐久，否则显示
    满耐久。display_total 为耐久条目的分母（展示用总耐久），默认与总
    耐久一致；供展示总耐久与实际总耐久不同的道具使用（如 4/1000000）。
    """
    values = await _remaining_durabilities(group_id, user_id, item_id)
    shown_total = total if display_total is None else display_total
    if values:
        return f"{values[0]}/{shown_total}"
    return f"{total}/{shown_total}"


async def move_damaged_instance(
    group_id: int,
    from_user: int,
    to_user: int,
    item_id: str,
) -> int | None:
    """把一件耗损中的副本从一名成员转移给另一名成员，返回其剩余耐久。

    取最早开始耗损的副本（即详情中显示的那一件）；来源成员没有耗损
    副本时不作修改并返回 None（此时转移的是全新副本）。调用方应在完成
    库存转移后调用；接收方的耗损记录追加在其列表末尾。
    """
    values = await _remaining_durabilities(group_id, from_user, item_id)
    if not values:
        return None
    remaining = values.pop(0)
    await _store(group_id, from_user, item_id=item_id, values=values)
    await _store(
        group_id,
        to_user,
        item_id=item_id,
        values=[*await _remaining_durabilities(group_id, to_user, item_id), remaining],
    )
    return remaining


def durable_behavior(
    *,
    total: int,
    display_total: int | None = None,
    check: Callable[[ItemUseContext], Awaitable[str | None]] | None = None,
    use: Callable[[ItemUseContext], Awaitable[None]] | None = None,
) -> ItemBehavior:
    """返回耐久型道具的行为块：消耗、可交易数量与详情耐久由本模块提供。

    check / use 为道具自身的校验与效果（不提供时按框架默认：校验通过、
    无使用效果）。行为块不含具体道具（按使用上下文或查看的道具编号定位
    耐久状态），因此同组道具可共用一个实例。

    总耐久须为正整数（< 1 视为开发错误并抛出 ValueError，由 items 加载器
    记录 warning 并忽略该道具）。
    """
    if total < 1:
        message = f"耐久型道具的总耐久必须为正整数，收到：{total}"
        raise ValueError(message)
    return ItemBehavior(
        check=check,
        use=use,
        consume=partial(_consume, total=total),
        transferable=_transferable_count,
        durability_text=partial(
            _durability_text, total=total, display_total=display_total
        ),
    )
