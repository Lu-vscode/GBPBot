"""耐久型道具的公共逻辑：库存中的每件道具代表一个副本，未使用过的
副本是全新的；使用时不扣减库存数量，而是把一个副本记为"耗损中"并
保存其剩余耐久（进入成员的持久状态），剩余耐久归零时该副本从库存
移除。

道具模块提供道具编号、总耐久与状态键即可复用本模块的消耗、交换
数量复核与详情耐久显示（见 consume / transferable_count /
detail_durability），示例见 wrath.py（095，总耐久 4）与 flee.py
（886，总耐久 3）。耐久型道具在导入期登记（见 register_durable），
其它道具转移副本时据此一并转移剩余耐久（见 move_damaged_instance，
如「强塞」127）。
"""

from nonebot import logger

from src.plugins.item._framework import (
    ItemState,
    ItemUseContext,
    add_state,
    get_item_count,
    get_state,
    remove_item,
    remove_state,
)

# 状态附加数据中保存副本剩余耐久列表的键
_INSTANCES_KEY = "remaining"

# 已登记的耐久型道具：道具编号 -> 耗损状态键（由各耐久道具模块登记）
_DURABLE_STATE_KEYS: dict[str, str] = {}


def register_durable(item_id: str, state_key: str) -> None:
    """登记一件耐久型道具及其耗损状态键（由该道具模块在导入期调用）。

    供其它道具（如「强塞」）判断道具是否为耐久型，并在转移副本时
    一并转移剩余耐久。
    """
    _DURABLE_STATE_KEYS[item_id] = state_key


def durable_state_key(item_id: str) -> str | None:
    """返回耐久型道具的耗损状态键，非耐久型道具返回 None。"""
    return _DURABLE_STATE_KEYS.get(item_id)


def instances(group_id: int, user_id: int, state_key: str) -> list[int]:
    """返回成员身上处于耗损中的副本的剩余耐久列表。

    全新（未使用过）的副本不在列表中；列表中的每一项为某个副本扣除
    使用次数后的剩余耐久（1~总耐久-1），按开始耗损的先后排列。
    """
    state = get_state(group_id, user_id, state_key)
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


def _store(
    group_id: int,
    user_id: int,
    *,
    state_key: str,
    item_id: str,
    instances: list[int],
) -> None:
    """保存耗损中的副本耐久列表；列表为空时移除状态。"""
    if instances:
        add_state(
            group_id,
            user_id,
            state=ItemState(
                key=state_key,
                item_id=item_id,
                data={_INSTANCES_KEY: [int(value) for value in instances]},
            ),
        )
        return
    remove_state(group_id, user_id, state_key)


def consume(context: ItemUseContext, *, state_key: str, total: int) -> bool:
    """消耗一件耐久道具，返回是否成功消耗。

    优先对已耗损副本扣 1 点耐久（归零时道具消失），没有耗损副本时
    开始耗损一件全新副本（库存数量不变，耐久记录进入状态）。
    """
    group_id = context.group_id
    user_id = context.user_id
    item_id = context.item.item_id
    values = instances(group_id, user_id, state_key)
    if values:
        values[0] -= 1
        if values[0] <= 0:
            # 该副本耐久归零：道具消失（从库存移除，状态随列表清空清理）
            values.pop(0)
            if not remove_item(group_id, user_id, item_id):
                logger.error(
                    f"{context.item.name}：耐久归零的副本未能从库存移除"
                    f"（群 {group_id}，成员 {user_id}）"
                )
        _store(
            group_id,
            user_id,
            state_key=state_key,
            item_id=item_id,
            instances=values,
        )
        return True
    if get_item_count(group_id, user_id, item_id) < 1:
        # 不可达：主模块已在同步段校验持有道具
        return False
    _store(
        group_id,
        user_id,
        state_key=state_key,
        item_id=item_id,
        instances=[total - 1],
    )
    return True


def move_damaged_instance(
    group_id: int,
    from_user: int,
    to_user: int,
    *,
    item_id: str,
    state_key: str,
) -> int | None:
    """把一件耗损中的副本从一名成员转移给另一名成员，返回其剩余耐久。

    取最早开始耗损的副本（即详情中显示的那一件）；来源成员没有耗损
    副本时不作修改并返回 None（此时转移的是全新副本）。调用方应在完成
    库存转移后调用；接收方的耗损记录追加在其列表末尾。
    """
    values = instances(group_id, from_user, state_key)
    if not values:
        return None
    remaining = values.pop(0)
    _store(
        group_id,
        from_user,
        state_key=state_key,
        item_id=item_id,
        instances=values,
    )
    _store(
        group_id,
        to_user,
        state_key=state_key,
        item_id=item_id,
        instances=[*instances(group_id, to_user, state_key), remaining],
    )
    return remaining


def transferable_count(
    group_id: int, user_id: int, *, item_id: str, state_key: str
) -> int:
    """返回可用于交换/合成的副本数量（全新副本，耗损副本不计入）。"""
    return max(
        0,
        get_item_count(group_id, user_id, item_id)
        - len(instances(group_id, user_id, state_key)),
    )


def detail_durability(
    group_id: int,
    user_id: int,
    *,
    state_key: str,
    total: int,
    display_total: int | None = None,
) -> str:
    """详情显示的耐久：存在耗损副本时显示第一个（最早开始耗损）副本的
    剩余耐久，否则显示满耐久。

    display_total 为耐久条目的分母（展示用总耐久），默认与总耐久一致；
    供展示总耐久与实际总耐久不同的道具使用（如"4/1000000"）。
    """
    values = instances(group_id, user_id, state_key)
    shown_total = total if display_total is None else display_total
    if values:
        return f"{values[0]}/{shown_total}"
    return f"{total}/{shown_total}"
