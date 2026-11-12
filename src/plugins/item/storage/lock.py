"""按群号串行化的互斥锁。

需要"先校验、后修改"的流程（道具使用、交换、合成）应包在同一把群锁内，
取代原先"校验与消耗同处同步段"的假设。同一群的操作互斥，不同群互不
影响。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# 按群号缓存的锁
_group_locks: dict[int, asyncio.Lock] = {}


@asynccontextmanager
async def group_lock(group_id: int) -> AsyncIterator[None]:
    """按群号串行化一段异步流程（同一群的操作互斥，不同群互不影响）。"""
    lock = _group_locks.get(group_id)
    if lock is None:
        lock = _group_locks[group_id] = asyncio.Lock()
    async with lock:
        yield
