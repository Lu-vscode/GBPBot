"""道具库存的数据库读写原语。

每个函数自行开启事务（`async with get_session() as session, session.begin()`），
调用方无需再开事务；函数返回时事务已提交。复合操作（批量扣减、双方交换）
都在单个事务内完成，任一步不满足条件时整体回滚，不会留下"扣了一半"的
库存。
"""

from collections.abc import Sequence

from nonebot_plugin_orm import get_session
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.plugins.item.storage.models import ItemInventory


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
