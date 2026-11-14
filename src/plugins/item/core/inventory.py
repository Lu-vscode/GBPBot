"""道具库存 API（道具模块使用）。

在 storage/inventory.py 的读写原语之上组合出业务语义：数量查询、可交易
数量（经 ItemBehavior.transferable 排除不可交易副本）、增加/移除/互换与
按品质排序的清单。
"""

from collections.abc import Mapping, Sequence

from nonebot import logger

from src.plugins.item.core.definition import ItemDefinition
from src.plugins.item.core.registry import get_item
from src.plugins.item.storage.inventory import (
    add_to_inventory,
    load_group_inventory,
    load_item_counts,
    replace_group_inventory,
    swap_items,
    take_from_inventory,
)


async def get_item_count(group_id: int, user_id: int, item_id: str) -> int:
    """返回成员在指定群拥有的道具数量（无记录时为 0）。"""
    counts = await load_item_counts(group_id, user_id)
    return counts.get(item_id, 0)


async def transferable_count(group_id: int, user_id: int, item_id: str) -> int:
    """返回成员可用于交换与合成的道具数量。

    道具模块可经 ItemBehavior.transferable 钩子排除不可交易的副本
    （如耐久耗损的副本）；未注册编号按库存数量处理。
    """
    definition = get_item(item_id)
    behavior = definition.behavior if definition is not None else None
    if behavior is not None and behavior.transferable is not None:
        return await behavior.transferable(group_id, user_id, item_id)
    return await get_item_count(group_id, user_id, item_id)


async def add_item(group_id: int, user_id: int, item_id: str, count: int = 1) -> None:
    """为成员增加道具。"""
    if count <= 0:
        return
    await add_to_inventory(group_id, user_id, [(item_id, count)])


async def add_items(group_id: int, user_id: int, item_ids: Sequence[str]) -> None:
    """批量增加道具（每件 +1，允许重复编号），只写一次数据库。"""
    if not item_ids:
        return
    await add_to_inventory(group_id, user_id, [(item_id, 1) for item_id in item_ids])


async def remove_item(
    group_id: int, user_id: int, item_id: str, count: int = 1
) -> bool:
    """为成员移除道具；数量不足时不做任何事并返回 False。"""
    return await remove_items(group_id, user_id, [(item_id, count)])


async def remove_items(
    group_id: int, user_id: int, entries: Sequence[tuple[str, int]]
) -> bool:
    """批量移除道具；任一数量不足时不做任何修改并返回 False。

    entries 为 (道具编号, 数量) 序列；同一编号在序列中重复出现时，
    各项数量均按库存总量校验，按顺序扣减。
    """
    if not entries:
        return True
    return await take_from_inventory(group_id, user_id, entries)


async def exchange_items(
    group_id: int,
    initiator_id: int,
    initiator_item_id: str,
    target_id: int,
    target_item_id: str,
) -> bool:
    """把双方各一件道具互换，返回是否交换成功。

    任一方库存不足时不做任何修改并返回 False；双方为同一成员的
    情况由调用方排除。
    """
    return await swap_items(
        group_id, initiator_id, initiator_item_id, target_id, target_item_id
    )


async def snapshot_group_inventory(group_id: int) -> dict[int, dict[str, int]]:
    """返回本群全部成员的库存快照（成员 QQ 号 -> {道具编号: 数量}）。

    没有库存记录的成员不出现在快照中；供需要按群整体记录库存的道具
    使用（如「二阶堂希罗」的“死亡回溯”）。
    """
    return await load_group_inventory(group_id)


async def restore_group_inventory(
    group_id: int, snapshot: Mapping[int, Mapping[str, int]]
) -> None:
    """把本群的全部库存整体恢复为快照（单个事务）。

    快照之后的库存变化全部被覆盖：快照中没有的成员库存被清空、数量
    被改回快照值。只处理库存数量：耐久型道具的耗损副本记录（状态）
    等不在此恢复。
    """
    await replace_group_inventory(group_id, snapshot)


async def list_user_items(
    group_id: int, user_id: int
) -> list[tuple[ItemDefinition, int]]:
    """返回成员拥有的全部道具（定义与数量），按品质由高到低、
    同品质按编号升序排列；库存中未注册的编号跳过并记录 warning。"""
    counts = await load_item_counts(group_id, user_id)
    result: list[tuple[ItemDefinition, int]] = []
    for item_id, count in counts.items():
        definition = get_item(item_id)
        if definition is None:
            logger.warning(f"库存中的道具编号 {item_id} 未注册（可能已移除），已跳过")
            continue
        result.append((definition, count))
    result.sort(key=lambda entry: (-entry[0].quality.value, entry[0].item_id))
    return result
