"""交换请求的内存队列：登记、查找、过期与移除。

交换请求只存内存（进程重启即失效），有效期见 EXCHANGE_DURATION_SECONDS；
过期请求在每次交换指令与定时清理时静默删除。业务语义（复查道具、互换
库存与播报）见 exchange/commands.py。
"""

import time
from dataclasses import dataclass

from nonebot import logger

# 交换请求的有效期（秒）：期间不能向同一人重复发起；过期静默删除
EXCHANGE_DURATION_SECONDS = 600.0


@dataclass
class PendingExchange:
    """一笔等待对方处理的交换请求（仅存内存，有效期 10 分钟）。"""

    group_id: int
    """交换所在群号。"""

    initiator_id: int
    """发起方的成员 QQ 号。"""

    initiator_name: str
    """发起方的群昵称（已截断）。"""

    target_id: int
    """被发起方的成员 QQ 号。"""

    target_name: str
    """被发起方的群昵称（已截断）。"""

    initiator_item_id: str
    """发起方提供的道具编号。"""

    target_item_id: str
    """发起方希望获得（对方拥有）的道具编号。"""

    created_at: float
    """创建时间（time.monotonic，用于过期判断）。"""


# 等待处理的交换请求（按创建顺序）
_exchanges: list[PendingExchange] = []


def append_exchange(exchange: PendingExchange) -> None:
    """把新发起的交换请求追加到队列末尾（由发起指令在群锁内调用）。"""
    _exchanges.append(exchange)


def expire_exchanges(now: float) -> None:
    """静默丢弃超过有效期的交换请求。"""
    kept: list[PendingExchange] = []
    for exchange in _exchanges:
        if now - exchange.created_at >= EXCHANGE_DURATION_SECONDS:
            logger.debug(
                f"群 {exchange.group_id} 中 {exchange.initiator_id} 向 "
                f"{exchange.target_id} 的交换请求已过期，已静默删除"
            )
        else:
            kept.append(exchange)
    _exchanges[:] = kept


def find_pair_exchange(
    group_id: int, user_a: int, user_b: int
) -> PendingExchange | None:
    """查找双方之间（不限方向）未完成的交换请求。"""
    for exchange in _exchanges:
        if exchange.group_id != group_id:
            continue
        if {exchange.initiator_id, exchange.target_id} == {user_a, user_b}:
            return exchange
    return None


def find_pending_exchange(
    group_id: int, initiator: int, target: int
) -> PendingExchange | None:
    """查找指定成员向对方发起、等待处理的交换请求。"""
    for exchange in _exchanges:
        if (
            exchange.group_id == group_id
            and exchange.initiator_id == initiator
            and exchange.target_id == target
        ):
            return exchange
    return None


def remove_exchange(exchange: PendingExchange) -> bool:
    """从交换队列中移除指定请求（按对象身份判断）。"""
    for index, item in enumerate(_exchanges):
        if item is exchange:
            del _exchanges[index]
            return True
    return False


def current_time() -> float:
    """返回交换请求使用的单调时钟读数（与 created_at 同一时钟）。"""
    return time.monotonic()
