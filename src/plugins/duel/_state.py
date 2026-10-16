"""决斗状态与状态服务。

进行中的决斗保存在内存中（duels 列表，按创建顺序排列）；本模块实现
对外的决斗状态查询服务（DuelStateService，实例 state_service 由插件
主模块统一注册），其它插件经服务注册中心查看某个群内进行中决斗的
只读快照。状态变更函数（记录/认领手势、移除决斗）供包内其它模块在
同步段（无 await）调用，用于防并发事件重复记录、重复结算。

决斗中成员所需的猜拳表情数量默认为 1，可由手势配额提供者（如道具
的“强运”效果）提高：提供者经决斗手势配额服务
（DuelGestureQuotaService）注册，所需数量在记录手势与就绪判定时
动态计算，多个提供者取最大值；被挑衅决斗中由机器人代替出手的一方
同样按该数量记录手势。
"""

import time
from dataclasses import dataclass, field

from nonebot import logger

from src.plugins._shared.services import (
    DuelGestureQuotaProvider,
    DuelSnapshot,
    DuelStateService,
)
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

    bot_rps_active: bool = False
    """机器人代发猜拳（_flow.send_bot_rps）是否执行中：代发期间对方
    （机器人自己）的消息事件不记录手势，由代发流程回查结果后统一
    记录，避免同一消息被重复记录。"""

    gestures: dict[int, list[int]] = field(default_factory=dict)
    """已发送的猜拳手势：成员 QQ 号 -> 手势结果列表（1 布、2 剪刀、
    3 石头），按发送顺序逐条记录。"""


# 进行中的决斗，按创建顺序排列（同一成员多场决斗时优先满足旧的）
duels: list[Duel] = []

# 手势配额提供者与捕获集合：提供者经决斗手势配额服务
# （DuelGestureQuotaService）注册，用于提高成员在一场决斗中需要发送
# 的猜拳表情数量；单个提供者抛出的异常只记录错误日志、其结果被忽略
_QUOTA_PROVIDER_ERRORS = (Exception,)
_quota_providers: list[DuelGestureQuotaProvider] = []


def add_gesture_quota_provider(provider: DuelGestureQuotaProvider) -> None:
    """注册手势配额提供者（同一提供者重复注册会被忽略）。"""
    if provider not in _quota_providers:
        _quota_providers.append(provider)


def required_gestures(group_id: int, user_id: int, opponent_id: int) -> int:
    """返回成员在该场决斗中需要发送的猜拳表情数量（至少 1）。

    默认需要 1 个，注册提供者后按提供者返回的数量提高（多个提供者
    取最大值）；返回非正整数的提供者被忽略并记录警告，单个提供者
    抛出的异常只记录错误日志、其结果被忽略。
    """
    required = 1
    for provider in tuple(_quota_providers):
        try:
            value = provider(group_id, user_id, opponent_id)
        except _QUOTA_PROVIDER_ERRORS as exc:
            logger.error(f"手势配额提供者计算所需数量时出错：{exc}")
            continue
        if not isinstance(value, int) or value < 1:
            logger.warning(f"手势配额提供者返回了无效的数量 {value!r}，已忽略")
            continue
        required = max(required, value)
    return required


def _opponent_of(duel: Duel, user_id: int) -> int:
    """返回决斗中相对于指定成员的另一方 QQ 号（调用方保证其在决斗中）。"""
    return duel.opponent_id if user_id == duel.challenger_id else duel.challenger_id


def completed_gestures(duel: Duel, user_id: int) -> bool:
    """判断成员是否已完成出手（已发送全部所需的猜拳表情）。

    所需数量按当前动态配额计算（见 required_gestures）。
    """
    return len(duel.gestures.get(user_id, [])) >= required_gestures(
        duel.group_id, user_id, _opponent_of(duel, user_id)
    )


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
        gesture_users=frozenset(
            user_id
            for user_id in (duel.challenger_id, duel.opponent_id)
            if completed_gestures(duel, user_id)
        ),
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

    需在同步段（无 await）中调用，用于防并发事件重复记录。成员已
    发送完全部所需猜拳表情时跳过该决斗（见 completed_gestures）。
    """
    for duel in duels:
        if (
            duel.group_id == group_id
            and duel.accepted
            and user_id in (duel.challenger_id, duel.opponent_id)
            and not completed_gestures(duel, user_id)
        ):
            if duel.provoked_by_bot and user_id == duel.opponent_id:
                # 被挑衅决斗中对方的手势由机器人代替，对方发送的猜拳表情无效
                continue
            if duel.bot_rps_active and user_id == duel.opponent_id:
                # 机器人代发期间：该消息由代发流程回查结果后统一记录，
                # 消息事件不重复记录（见 _flow.send_bot_rps）
                continue
            duel.gestures.setdefault(user_id, []).append(gesture)
            return duel
    return None


def record_gesture(duel: Duel, user_id: int, gesture: int) -> bool:
    """记录指定决斗中成员的手势（同步段），返回是否记录成功。

    决斗已被移除（判定完成或超时）或成员已发送完全部所需猜拳表情时
    返回 False（见 completed_gestures）。
    """
    if not any(item is duel for item in duels):
        return False
    if user_id not in (duel.challenger_id, duel.opponent_id):
        return False
    if completed_gestures(duel, user_id):
        return False
    duel.gestures.setdefault(user_id, []).append(gesture)
    return True


def remove_duel(duel: Duel) -> bool:
    """从决斗列表中移除指定决斗（按对象身份判断），用于认领结算。"""
    for index, item in enumerate(duels):
        if item is duel:
            del duels[index]
            return True
    return False


def duel_ready(duel: Duel) -> bool:
    """判断决斗双方是否都已发送完全部所需的猜拳表情。"""
    return completed_gestures(duel, duel.challenger_id) and completed_gestures(
        duel, duel.opponent_id
    )
