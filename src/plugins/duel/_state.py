"""决斗状态与状态服务。

进行中的决斗保存在内存中（duels 列表，按创建顺序排列）；本模块实现
对外的决斗状态查询服务（DuelStateService，实例 state_service 由插件
主模块统一注册），其它插件经服务注册中心查看某个群内进行中决斗的
只读快照。状态变更函数（记录/认领手势、移除决斗）供包内其它模块在
同步段（无 await）调用，用于防并发事件重复记录、重复结算。
"""

import time
from dataclasses import dataclass, field

from src.plugins._shared.services import DuelSnapshot, DuelStateService
from src.plugins.duel._config import DURATION_SECONDS


@dataclass(eq=False)
class Duel:
    """一场进行中的决斗，保存在内存中（相等判断请使用对象身份）。"""

    group_id: int
    """决斗所在群号。"""

    bot_self_id: int
    """处理该决斗的机器人 QQ 号。"""

    challenger_id: int
    """发起决斗的成员 QQ 号。"""

    challenger_name: str
    """发起决斗一方的群昵称（发起时记录，过长时已按上限截断）。"""

    opponent_id: int
    """接受决斗一方的成员 QQ 号（被 @ 的成员或机器人自己）。"""

    opponent_name: str
    """接受决斗一方的群昵称（发起时记录，过长时已按上限截断）。"""

    multiplier: int
    """决斗点数：胜者分数 +点数、败者分数 -点数。"""

    created_at: float
    """决斗创建时间（time.monotonic，用于超时判断）。"""

    accepted: bool = False
    """对方是否已接受（等待接受时为 False，机器人已掷骰接受时为 True）。"""

    provoked_by_bot: bool = False
    """是否被挑衅：该决斗中对方的手势由机器人代替（对方发送的猜拳表情无效）。"""

    gestures: dict[int, int] = field(default_factory=dict)
    """已发送的猜拳手势：成员 QQ 号 -> 手势结果（1 剪刀、2 石头、3 布）。"""


# 进行中的决斗，按创建顺序排列（同一成员多场决斗时优先满足旧的）
duels: list[Duel] = []


def snapshot(duel: Duel) -> DuelSnapshot:
    """构造一场决斗的只读快照。"""
    now = time.monotonic()
    return DuelSnapshot(
        group_id=duel.group_id,
        challenger_id=duel.challenger_id,
        challenger_name=duel.challenger_name,
        opponent_id=duel.opponent_id,
        opponent_name=duel.opponent_name,
        multiplier=duel.multiplier,
        accepted=duel.accepted,
        gesture_users=frozenset(duel.gestures),
        remaining_seconds=max(0.0, duel.created_at + DURATION_SECONDS - now),
    )


class _DuelStateService(DuelStateService):
    """决斗状态查询服务的实现：其它插件经服务注册中心查看进行中决斗。"""

    def list_duels(self, group_id: int) -> list[DuelSnapshot]:
        """返回指定群内全部进行中决斗的只读快照（按创建顺序）。"""
        return [snapshot(duel) for duel in duels if duel.group_id == group_id]


# 状态服务实例（由插件主模块统一注册）
state_service = _DuelStateService()


def find_pair_duel(group_id: int, user_a: int, user_b: int) -> Duel | None:
    """查找双方之间进行中的决斗（不限方向与是否已接受）。"""
    for duel in duels:
        if duel.group_id != group_id:
            continue
        if {duel.challenger_id, duel.opponent_id} == {user_a, user_b}:
            return duel
    return None


def find_pending_duel(group_id: int, challenger: int, opponent: int) -> Duel | None:
    """查找指定成员向对方发起、等待接受（或拒绝）的决斗。"""
    for duel in duels:
        if (
            duel.group_id == group_id
            and not duel.accepted
            and duel.challenger_id == challenger
            and duel.opponent_id == opponent
        ):
            return duel
    return None


def find_provoked_duel(group_id: int, challenger: int, opponent: int) -> Duel | None:
    """查找指定成员被挑衅（强制接受）的进行中决斗。"""
    for duel in duels:
        if (
            duel.group_id == group_id
            and duel.provoked_by_bot
            and duel.challenger_id == challenger
            and duel.opponent_id == opponent
        ):
            return duel
    return None


def take_gesture(group_id: int, user_id: int, gesture: int) -> Duel | None:
    """把手势记录到等待该成员出手的最旧一场决斗，返回该决斗。

    需在同步段（无 await）中调用，用于防并发事件重复记录。
    """
    for duel in duels:
        if (
            duel.group_id == group_id
            and duel.accepted
            and user_id in (duel.challenger_id, duel.opponent_id)
            and user_id not in duel.gestures
        ):
            if duel.provoked_by_bot and user_id == duel.opponent_id:
                # 被挑衅决斗中对方的手势由机器人代替，对方发送的猜拳表情无效
                continue
            duel.gestures[user_id] = gesture
            return duel
    return None


def record_gesture(duel: Duel, user_id: int, gesture: int) -> bool:
    """记录指定决斗中成员的手势（同步段），返回是否记录成功。

    决斗已被移除（判定完成或超时）或成员已出过手时返回 False。
    """
    if not any(item is duel for item in duels):
        return False
    if user_id not in (duel.challenger_id, duel.opponent_id):
        return False
    if user_id in duel.gestures:
        return False
    duel.gestures[user_id] = gesture
    return True


def remove_duel(duel: Duel) -> bool:
    """从决斗列表中移除指定决斗（按对象身份判断），用于认领结算。"""
    for index, item in enumerate(duels):
        if item is duel:
            del duels[index]
            return True
    return False


def duel_ready(duel: Duel) -> bool:
    """判断决斗双方是否都已发送猜拳表情。"""
    return duel.challenger_id in duel.gestures and duel.opponent_id in duel.gestures
