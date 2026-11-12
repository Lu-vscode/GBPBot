"""道具状态 API（道具模块使用）。

在 storage/states.py 的读写原语之上组合出业务语义：按时长新增状态、查询
有效状态、覆盖附加数据、批量移除、按状态键查询持有者与过期清理。状态
集合每次变化都会递增变更序号（见 core/registry.py 的 state_version），供
道具模块缓存由状态派生的数据。
"""

import time
from collections.abc import Sequence
from typing import Any

from src.plugins.item.core.context import ItemStateExpireContext
from src.plugins.item.core.registry import bump_state_version
from src.plugins.item.storage.states import (
    ItemState,
    delete_states,
    fetch_state,
    load_states,
    select_state_holders,
    take_expired_states,
    update_state_data,
    upsert_states,
)


def _state_active(state: ItemState, now: float) -> bool:
    """判断状态在指定时刻是否仍然有效。"""
    return state.expires_at is None or state.expires_at > now


async def add_state(
    group_id: int,
    user_id: int,
    *,
    state: ItemState,
    duration_seconds: float | None = None,
) -> None:
    """为成员新增一件道具状态（同键旧状态被覆盖）。

    state 提供状态键、来源道具与附加数据（expires_at 由本函数按下述
    时长计算，传入值不使用）；duration_seconds 为 None 时状态永久
    有效，否则自当前时刻起持续指定秒数。过期状态不在此处清理，
    统一由定时清理分发到期效果（见 purge_expired_states）。

    一次新增多件状态请用 add_states。
    """
    await add_states(
        group_id, user_id, states=[state], duration_seconds=duration_seconds
    )


async def add_states(
    group_id: int,
    user_id: int,
    *,
    states: Sequence[ItemState],
    duration_seconds: float | None = None,
) -> None:
    """批量新增道具状态（同键旧状态按顺序覆盖）。

    时长语义与 add_state 一致（整批共用一个过期时刻）；空序列不写库。
    """
    if not states:
        return
    expires_at = None if duration_seconds is None else time.time() + duration_seconds
    await upsert_states(group_id, user_id, states, expires_at)
    bump_state_version()


async def get_state(group_id: int, user_id: int, key: str) -> ItemState | None:
    """查询成员身上指定键的有效状态，未找到或已过期时返回 None。

    已过期的状态不在此处删除：等待定时清理统一分发到期效果
    （见 purge_expired_states）。
    """
    return await fetch_state(group_id, user_id, key)


async def remove_state(group_id: int, user_id: int, key: str) -> bool:
    """移除成员身上指定键的状态，返回是否存在该状态。"""
    return await remove_states(group_id, user_id, [key]) > 0


async def set_state_data(
    group_id: int, user_id: int, key: str, data: dict[str, Any]
) -> bool:
    """覆盖成员身上指定键有效状态的附加数据（过期时刻保持不变）。

    供道具模块在状态有效期内更新附加记录（如刷新冷却时刻）；状态不
    存在或已过期时不做任何事并返回 False。
    """
    updated = await update_state_data(group_id, user_id, key, data)
    if updated:
        bump_state_version()
    return updated


async def remove_states(group_id: int, user_id: int, keys: Sequence[str]) -> int:
    """批量移除指定键的状态，返回实际移除的状态数。"""
    removed = await delete_states(group_id, user_id, keys)
    if removed:
        bump_state_version()
    return removed


async def list_states(group_id: int, user_id: int) -> list[ItemState]:
    """返回成员身上的全部有效状态（不做清理，过期条目等待定时清理）。"""
    now = time.time()
    states = await load_states(group_id, user_id)
    return [state for state in states if _state_active(state, now)]


async def list_state_holders(group_id: int, key: str) -> list[tuple[int, ItemState]]:
    """返回群内持有指定键有效状态的成员 (QQ 号, 状态)，按 QQ 号升序。

    供道具模块在无使用上下文的场景查询状态持有者（如发送消息时按
    持有者显示名改写文本）；已过期但未清理的状态不计入。
    """
    return await select_state_holders(group_id, key)


async def purge_expired_states() -> list[ItemStateExpireContext]:
    """清理全部已过期的状态，返回到期条目列表（无清理时不写库）。

    返回的条目由定时清理按来源道具分发到期回调（见
    ItemBehavior.expire）；未提供回调或处理失败时
    状态仍已被删除。
    """
    expired = [
        ItemStateExpireContext(group_id=group_id, user_id=user_id, state=state)
        for group_id, user_id, state in await take_expired_states()
    ]
    if expired:
        bump_state_version()
    return expired
