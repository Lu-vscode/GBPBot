"""道具状态的数据库读写原语与状态值对象。

状态值对象 ItemState 定义在本模块，并经 core（道具模块统一导入点）对外
导出。每个函数自行开启事务；整批新增与过期清理都在单个事务内完成，
避免并发清理重复分发到期效果。
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from nonebot_plugin_orm import get_session
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.plugins.item.storage.models import ItemStateRow

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
