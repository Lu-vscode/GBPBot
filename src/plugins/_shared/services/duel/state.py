"""决斗状态查询：进行中决斗的只读快照（由猜拳决斗插件提供）。"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class DuelSnapshot:
    """一场进行中决斗的只读快照（由 DuelStateService 提供）。"""

    group_id: int
    """决斗所在群号。"""

    challenger_id: int
    """发起决斗的成员 QQ 号。"""

    challenger_name: str
    """发起决斗一方的群昵称（提供方已按显示规范截断）。"""

    opponent_id: int
    """接受决斗一方的成员 QQ 号（被 @ 的成员或机器人自己）。"""

    opponent_name: str
    """接受决斗一方的群昵称（提供方已按显示规范截断）。"""

    multiplier: int
    """决斗点数。"""

    accepted: bool
    """对方是否已接受。"""

    gesture_users: frozenset[int]
    """已完成出手（发送了全部所需的猜拳表情）的成员 QQ 号集合。"""

    remaining_seconds: float
    """距超时结束的剩余秒数（不小于 0）。"""


@runtime_checkable
class DuelStateService(Protocol):
    """决斗状态查询服务（由猜拳决斗插件提供）。"""

    async def list_duels(self, group_id: int) -> list[DuelSnapshot]:
        """返回指定群内全部进行中决斗的快照（按创建顺序）。"""
        ...
