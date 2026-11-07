"""道具库存与状态的数据库读写原语（仅由道具插件包内使用）。

本模块把 `gbp_item_inventory` / `gbp_item_state` 两张表的读写收敛到少数
几个函数，供 _framework.py 组合成对外 API：

- 每个函数自行开启事务（`async with get_session() as session, session.begin()`），
  调用方无需再开事务；函数返回时事务已提交。
- 复合操作（批量扣减、双方交换、整批新增状态）都在单个事务内完成，
  任一步不满足条件时整体回滚，不会留下"扣了一半"的库存。
- `group_lock(group_id)` 提供按群号串行化：需要"先校验、后修改"的流程
  （道具使用、交换、合成）应包在同一把群锁内，取代原先"校验与消耗
  同处同步段"的假设。

状态值对象 `ItemState` 定义在本模块，并经 _framework 对外导出（道具模块
从 _framework 导入它）。
"""

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from nonebot_plugin_orm import get_session
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.plugins.item._models import ItemInventory, ItemStateRow

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult


@dataclass
class ItemState:
    """一个成员身上的道具状态（道具模块经状态 API 读写）。"""

    key: str
    """状态键（同一成员同键状态重复添加时覆盖）。"""

    item_id: str
    """产生该状态的道具编号。"""

    expires_at: float | None = None
    """过期时间（Unix 时间戳）；None 表示永久。作为 add_state 的状态
    模板构造时可省略（由框架按持续时长填充）。"""

    data: dict[str, Any] = field(default_factory=dict)
    """附加数据（道具模块自定义，须可 JSON 序列化）。"""


# 按群号缓存的锁：需要"先校验、后修改"的流程在此串行化
_group_locks: dict[int, asyncio.Lock] = {}


@asynccontextmanager
async def group_lock(group_id: int) -> AsyncIterator[None]:
    """按群号串行化一段异步流程（同一群的操作互斥，不同群互不影响）。"""
    lock = _group_locks.get(group_id)
    if lock is None:
        lock = _group_locks[group_id] = asyncio.Lock()
    async with lock:
        yield


def _as_state(row: ItemStateRow) -> ItemState:
    """把数据库行转换为状态值对象。"""
    return ItemState(
        key=row.state_key,
        item_id=row.item_id,
        expires_at=None if row.expires_at is None else float(row.expires_at),
        data=dict(row.data or {}),
    )


def _active(state: ItemState, now: float) -> bool:
    """判断状态在指定时刻是否仍然有效。"""
    return state.expires_at is None or state.expires_at > now


# ===== 库存 =====


async def load_item_counts(group_id: int, user_id: int) -> dict[str, int]:
    """读取成员在指定群的全部道具数量（无记录时为空字典）。"""
    statement = select(ItemInventory.item_id, ItemInventory.count).where(
        ItemInventory.group_id == group_id,
        ItemInventory.user_id == user_id,
    )
    async with get_session() as session:
        rows = (await session.execute(statement)).all()
    return {str(item_id): int(count) for item_id, count in rows}


async def add_to_inventory(
    group_id: int, user_id: int, counts: Sequence[tuple[str, int]]
) -> None:
    """按 (道具编号, 增加数量) 序列增加库存，全部变化在同一个事务内完成。"""
    if not counts:
        return
    async with get_session() as session, session.begin():
        for item_id, count in counts:
            if count <= 0:
                continue
            statement = sqlite_insert(ItemInventory).values(
                group_id=group_id, user_id=user_id, item_id=item_id, count=count
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        ItemInventory.group_id,
                        ItemInventory.user_id,
                        ItemInventory.item_id,
                    ],
                    set_={"count": ItemInventory.count + count},
                )
            )


async def set_item_count(group_id: int, user_id: int, item_id: str, count: int) -> None:
    """把成员拥有的某件道具数量设为指定值（不大于 0 时删除条目）。"""
    async with get_session() as session, session.begin():
        if count <= 0:
            await session.execute(
                delete(ItemInventory).where(
                    ItemInventory.group_id == group_id,
                    ItemInventory.user_id == user_id,
                    ItemInventory.item_id == item_id,
                )
            )
            return
        statement = sqlite_insert(ItemInventory).values(
            group_id=group_id, user_id=user_id, item_id=item_id, count=count
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    ItemInventory.group_id,
                    ItemInventory.user_id,
                    ItemInventory.item_id,
                ],
                set_={"count": count},
            )
        )


async def take_from_inventory(
    group_id: int, user_id: int, entries: Sequence[tuple[str, int]]
) -> bool:
    """按 (道具编号, 扣减数量) 序列扣减库存，返回是否扣减成功。

    任一编号数量不足时整体不修改并返回 False；扣减到 0 的条目直接删除。
    同一编号在序列中重复出现时，各项数量均按库存总量校验，按顺序扣减。
    """
    if not entries:
        return True
    async with get_session() as session, session.begin():
        statement = select(ItemInventory.item_id, ItemInventory.count).where(
            ItemInventory.group_id == group_id,
            ItemInventory.user_id == user_id,
        )
        rows = (await session.execute(statement)).all()
        counts = {str(item_id): int(count) for item_id, count in rows}
        required: dict[str, int] = {}
        for item_id, count in entries:
            if count <= 0:
                continue
            required[item_id] = required.get(item_id, 0) + count
        if any(counts.get(item_id, 0) < count for item_id, count in required.items()):
            return False
        for item_id, count in required.items():
            remaining = counts[item_id] - count
            if remaining <= 0:
                await session.execute(
                    delete(ItemInventory).where(
                        ItemInventory.group_id == group_id,
                        ItemInventory.user_id == user_id,
                        ItemInventory.item_id == item_id,
                    )
                )
            else:
                await session.execute(
                    update(ItemInventory)
                    .where(
                        ItemInventory.group_id == group_id,
                        ItemInventory.user_id == user_id,
                        ItemInventory.item_id == item_id,
                    )
                    .values(count=remaining)
                )
    return True


async def swap_items(
    group_id: int,
    initiator_id: int,
    initiator_item_id: str,
    target_id: int,
    target_item_id: str,
) -> bool:
    """把双方各一件道具互换，返回是否交换成功（任一方不足时整体不修改）。"""
    async with get_session() as session, session.begin():
        initiator = await session.scalar(
            select(ItemInventory.count).where(
                ItemInventory.group_id == group_id,
                ItemInventory.user_id == initiator_id,
                ItemInventory.item_id == initiator_item_id,
            )
        )
        target = await session.scalar(
            select(ItemInventory.count).where(
                ItemInventory.group_id == group_id,
                ItemInventory.user_id == target_id,
                ItemInventory.item_id == target_item_id,
            )
        )
        if not initiator or not target:
            return False
        await _decrease_one(
            session, group_id, initiator_id, initiator_item_id, int(initiator)
        )
        await _decrease_one(session, group_id, target_id, target_item_id, int(target))
        await _increase_one(session, group_id, initiator_id, target_item_id)
        await _increase_one(session, group_id, target_id, initiator_item_id)
    return True


async def _decrease_one(
    session: AsyncSession, group_id: int, user_id: int, item_id: str, current: int
) -> None:
    """把一件道具的数量减 1（减到 0 时删除条目）。"""
    if current <= 1:
        await session.execute(
            delete(ItemInventory).where(
                ItemInventory.group_id == group_id,
                ItemInventory.user_id == user_id,
                ItemInventory.item_id == item_id,
            )
        )
        return
    await session.execute(
        update(ItemInventory)
        .where(
            ItemInventory.group_id == group_id,
            ItemInventory.user_id == user_id,
            ItemInventory.item_id == item_id,
        )
        .values(count=current - 1)
    )


async def _increase_one(
    session: AsyncSession, group_id: int, user_id: int, item_id: str
) -> None:
    """把一件道具的数量加 1（已存在则累加）。"""
    statement = sqlite_insert(ItemInventory).values(
        group_id=group_id, user_id=user_id, item_id=item_id, count=1
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[
                ItemInventory.group_id,
                ItemInventory.user_id,
                ItemInventory.item_id,
            ],
            set_={"count": ItemInventory.count + 1},
        )
    )


# ===== 状态 =====


async def load_states(group_id: int, user_id: int) -> list[ItemState]:
    """读取成员身上的全部状态条目（含已过期但尚未清理的条目）。"""
    statement = select(ItemStateRow).where(
        ItemStateRow.group_id == group_id,
        ItemStateRow.user_id == user_id,
    )
    async with get_session() as session:
        rows = (await session.execute(statement)).scalars().all()
    return [_as_state(row) for row in rows]


async def fetch_state(
    group_id: int, user_id: int, key: str, *, now: float | None = None
) -> ItemState | None:
    """读取成员身上指定键的有效状态，未找到或已过期时返回 None。"""
    moment = time.time() if now is None else now
    statement = select(ItemStateRow).where(
        ItemStateRow.group_id == group_id,
        ItemStateRow.user_id == user_id,
        ItemStateRow.state_key == key,
    )
    async with get_session() as session:
        row = (await session.execute(statement)).scalar_one_or_none()
    if row is None:
        return None
    state = _as_state(row)
    return state if _active(state, moment) else None


async def upsert_states(
    group_id: int,
    user_id: int,
    states: Sequence[ItemState],
    expires_at: float | None,
) -> None:
    """按状态键覆盖写入多条状态（整批共用同一个过期时刻）。"""
    if not states:
        return
    async with get_session() as session, session.begin():
        for state in states:
            statement = sqlite_insert(ItemStateRow).values(
                group_id=group_id,
                user_id=user_id,
                state_key=state.key,
                item_id=state.item_id,
                expires_at=expires_at,
                data=dict(state.data),
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        ItemStateRow.group_id,
                        ItemStateRow.user_id,
                        ItemStateRow.state_key,
                    ],
                    set_={
                        "item_id": state.item_id,
                        "expires_at": expires_at,
                        "data": dict(state.data),
                    },
                )
            )


async def delete_states(group_id: int, user_id: int, keys: Sequence[str]) -> int:
    """删除成员身上指定键的状态，返回实际删除的条目数。"""
    if not keys:
        return 0
    async with get_session() as session, session.begin():
        result = await session.execute(
            delete(ItemStateRow).where(
                ItemStateRow.group_id == group_id,
                ItemStateRow.user_id == user_id,
                ItemStateRow.state_key.in_(list(keys)),
            )
        )
    return int(cast("CursorResult[Any]", result).rowcount or 0)


async def update_state_data(
    group_id: int,
    user_id: int,
    key: str,
    data: dict[str, Any],
    *,
    now: float | None = None,
) -> bool:
    """覆盖指定键有效状态的附加数据，返回是否更新（状态不存在或已过期
    时不做任何事并返回 False）。"""
    moment = time.time() if now is None else now
    async with get_session() as session, session.begin():
        row = await session.scalar(
            select(ItemStateRow).where(
                ItemStateRow.group_id == group_id,
                ItemStateRow.user_id == user_id,
                ItemStateRow.state_key == key,
            )
        )
        if row is None:
            return False
        state = _as_state(row)
        if not _active(state, moment):
            return False
        await session.execute(
            update(ItemStateRow)
            .where(
                ItemStateRow.group_id == group_id,
                ItemStateRow.user_id == user_id,
                ItemStateRow.state_key == key,
            )
            .values(data=dict(data))
        )
    return True


async def select_state_holders(
    group_id: int, key: str, *, now: float | None = None
) -> list[tuple[int, ItemState]]:
    """返回群内持有指定键有效状态的成员 (QQ 号, 状态)，按 QQ 号升序。"""
    moment = time.time() if now is None else now
    statement = (
        select(ItemStateRow)
        .where(
            ItemStateRow.group_id == group_id,
            ItemStateRow.state_key == key,
        )
        .order_by(ItemStateRow.user_id)
    )
    async with get_session() as session:
        rows = (await session.execute(statement)).scalars().all()
    holders: list[tuple[int, ItemState]] = []
    for row in rows:
        state = _as_state(row)
        if _active(state, moment):
            holders.append((int(row.user_id), state))
    return holders


async def take_expired_states(
    *, now: float | None = None
) -> list[tuple[int, int, ItemState]]:
    """删除全部已过期状态并返回 (群号, QQ 号, 状态) 列表。

    删除与读取在同一个事务内完成，避免并发清理重复分发到期效果。
    """
    moment = time.time() if now is None else now
    statement = select(ItemStateRow).where(
        ItemStateRow.expires_at.is_not(None),
        ItemStateRow.expires_at <= moment,
    )
    async with get_session() as session, session.begin():
        rows = (await session.execute(statement)).scalars().all()
        if not rows:
            return []
        await session.execute(
            delete(ItemStateRow).where(
                ItemStateRow.expires_at.is_not(None),
                ItemStateRow.expires_at <= moment,
            )
        )
        return [(int(row.group_id), int(row.user_id), _as_state(row)) for row in rows]
