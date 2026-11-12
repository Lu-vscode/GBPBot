"""进行中决斗的内存存储与状态原语。

进行中的决斗保存在内存中（duels 列表，按创建顺序排列）；本模块提供
查询、只手势记录与移除等原语，供包内各模块与对外服务使用。记录手势的
“检查—修改”在模块级锁内完成（所需手势数量需读取数据库），用于防并发
事件重复记录；移除决斗按对象身份判断，不受锁影响（重复结算已被身份
检查挡住）。

决斗中成员所需的猜拳表情数量默认为 1，可由整局判定效果（如道具的
“强运”效果）提高：效果经决斗手势服务（DuelGestureService）注册判定与
所需数量，所需数量在记录手势与就绪判定时动态计算（见 judgement.py）。
"""

import asyncio
import time

from src.plugins._shared.services import DuelSnapshot
from src.plugins.duel.config import DURATION_SECONDS
from src.plugins.duel.core.duel import Duel
from src.plugins.duel.core.judgement import required_gestures

# 进行中的决斗，按创建顺序排列（同一成员多场决斗时优先满足旧的）
duels: list[Duel] = []

# 记录手势时的互斥锁：结合“读取手势配额（需 await 数据库）再追加手势”
# 的检查—修改过程，避免并发事件记录到超出配额的数量
_state_lock = asyncio.Lock()


def _opponent_of(duel: Duel, user_id: int) -> int:
    """返回决斗中相对于指定成员的另一方 QQ 号（调用方保证其在决斗中）。"""
    return duel.opponent_id if user_id == duel.challenger_id else duel.challenger_id


async def completed_gestures(duel: Duel, user_id: int) -> bool:
    """判断成员是否已完成出手（已发送全部所需的猜拳表情）。

    所需数量按当前动态配额计算（见 required_gestures）。
    """
    quota = await required_gestures(duel.group_id, user_id, _opponent_of(duel, user_id))
    return len(duel.gestures.get(user_id, [])) >= quota


async def snapshot(duel: Duel) -> DuelSnapshot:
    """构造一场决斗的只读快照。"""
    now = time.monotonic()
    completed: set[int] = set()
    for user_id in (duel.challenger_id, duel.opponent_id):
        if await completed_gestures(duel, user_id):
            completed.add(user_id)
    return DuelSnapshot(
        group_id=duel.group_id,
        challenger_id=duel.challenger_id,
        challenger_name=duel.challenger_name,
        opponent_id=duel.opponent_id,
        opponent_name=duel.opponent_name,
        multiplier=duel.multiplier,
        accepted=duel.accepted,
        gesture_users=frozenset(completed),
        remaining_seconds=max(0.0, duel.created_at + DURATION_SECONDS - now),
    )


def add_duel(duel: Duel) -> None:
    """把新发起的决斗追加到决斗列表（由发起指令在同步段调用）。"""
    duels.append(duel)


def group_duels(group_id: int) -> list[Duel]:
    """返回指定群内全部进行中的决斗（按创建顺序）。"""
    return [duel for duel in duels if duel.group_id == group_id]


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


async def take_gesture(group_id: int, user_id: int, gesture: int) -> Duel | None:
    """把手势记录到等待该成员出手的最旧一场决斗，返回该决斗。

    记录在模块级锁内完成，避免并发事件重复记录。成员已发送完全部
    所需猜拳表情时跳过该决斗（见 completed_gestures）。
    """
    async with _state_lock:
        for duel in duels:
            if (
                duel.group_id == group_id
                and duel.accepted
                and user_id in (duel.challenger_id, duel.opponent_id)
                and not await completed_gestures(duel, user_id)
            ):
                if duel.provoked_by_bot and user_id == duel.opponent_id:
                    # 被挑衅决斗中对方的手势由机器人代替，对方发送的猜拳表情无效
                    continue
                if duel.bot_rps_active and user_id == duel.opponent_id:
                    # 机器人代发期间：该消息由代发流程回查结果后统一记录，
                    # 消息事件不重复记录（见 flow.botplay.send_bot_rps）
                    continue
                duel.gestures.setdefault(user_id, []).append(gesture)
                return duel
    return None


async def record_gesture(duel: Duel, user_id: int, gesture: int) -> bool:
    """记录指定决斗中成员的手势（在模块级锁内），返回是否记录成功。

    决斗已被移除（判定完成或超时）或成员已发送完全部所需猜拳表情时
    返回 False（见 completed_gestures）。
    """
    async with _state_lock:
        if not any(item is duel for item in duels):
            return False
        if user_id not in (duel.challenger_id, duel.opponent_id):
            return False
        if await completed_gestures(duel, user_id):
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


async def duel_ready(duel: Duel) -> bool:
    """判断决斗双方是否都已发送完全部所需的猜拳表情。"""
    return await completed_gestures(
        duel, duel.challenger_id
    ) and await completed_gestures(duel, duel.opponent_id)
