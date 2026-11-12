"""决斗状态查询指令：`/duel.status (all|a)`。

默认显示自己在本群进行中决斗的状态（等待接受、等待自己出拳、等待对方
出拳等）；超级用户可附加 all/a 参数查看本群全部进行中的决斗（未在用户
手册中提及）。剩余超时时间按 DUEL_DURATION 计算并向上取整到分钟。
"""

import math
import time

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.rule import is_type

from src.plugins.duel.config import (
    DURATION_SECONDS,
    STATUS_ALL_DENIED,
    USAGE_STATUS,
)
from src.plugins.duel.core import (
    Duel,
    completed_gestures,
    group_duels,
    required_gestures,
)
from src.plugins.duel.messaging import send_text


def _remaining_text(duel: Duel, now: float) -> str:
    """返回决斗的剩余超时描述，如"距超时约 10 分钟"。"""
    remaining = max(0, math.ceil((duel.created_at + DURATION_SECONDS - now) / 60))
    return f"距超时约 {remaining} 分钟"


def _gesture_state_text(*, sent: int, quota: int, by_bot: bool) -> str:
    """生成成员自己视角下的出拳状态描述。

    by_bot 为 True 表示该成员的手势由机器人代替发送（被挑衅）；
    配额为 1 时沿用原有的简明文案，大于 1 时附上进度（已发送个数）。
    """
    if by_bot:
        if sent == 0:
            state = (
                f"你的猜拳表情将由 Bot 代替发送（共 {quota} 个）"
                if quota > 1
                else "你的猜拳表情将由 Bot 代替发送"
            )
        elif sent < quota:
            state = f"Bot 已代替你发送 {sent}/{quota} 个猜拳表情"
        else:
            state = "Bot 已代替你发送猜拳表情，等待对方发送猜拳表情"
        return state
    if sent == 0:
        return (
            f"等待你发送猜拳表情（共需发送 {quota} 个）"
            if quota > 1
            else "等待你发送猜拳表情"
        )
    if sent < quota:
        return f"你已发送 {sent}/{quota} 个猜拳表情，等待你继续发送"
    if quota > 1:
        return f"你已发送 {sent}/{quota} 个猜拳表情，等待对方发送猜拳表情"
    return "你已出拳，等待对方发送猜拳表情"


async def _status_lines(group_id: int, user_id: int) -> list[str]:
    """生成成员在本群进行中决斗的状态描述行（按创建顺序）。"""
    now = time.monotonic()
    lines: list[str] = []
    for duel in group_duels(group_id):
        if user_id not in (duel.challenger_id, duel.opponent_id):
            continue
        suffix = _remaining_text(duel, now)
        if not duel.accepted:
            if user_id == duel.challenger_id:
                lines.append(
                    f"你向 {duel.opponent_name} 发起的决斗"
                    f"（×{duel.multiplier}）：等待对方接受，{suffix}。"
                )
            else:
                lines.append(
                    f"{duel.challenger_name} 向你发起的决斗"
                    f"（×{duel.multiplier}）：等待你接受"
                    f"（可使用 /duel.accept 接受），{suffix}。"
                )
            continue
        other_id = (
            duel.opponent_id if user_id == duel.challenger_id else duel.challenger_id
        )
        other_name = (
            duel.opponent_name
            if user_id == duel.challenger_id
            else duel.challenger_name
        )
        state = _gesture_state_text(
            sent=len(duel.gestures.get(user_id, [])),
            quota=await required_gestures(group_id, user_id, other_id),
            by_bot=duel.provoked_by_bot and user_id == duel.opponent_id,
        )
        lines.append(
            f"你与 {other_name} 的决斗（×{duel.multiplier}）：{state}，{suffix}。"
        )
    return lines


async def _all_status_lines(group_id: int) -> list[str]:
    """生成本群全部进行中决斗的状态描述行（按创建顺序）。"""
    now = time.monotonic()
    lines: list[str] = []
    for duel in group_duels(group_id):
        suffix = _remaining_text(duel, now)
        if not duel.accepted:
            lines.append(
                f"{duel.challenger_name} 向 {duel.opponent_name} 发起的决斗"
                f"（×{duel.multiplier}）：等待对方接受，{suffix}。"
            )
            continue
        if duel.provoked_by_bot and not await completed_gestures(
            duel, duel.opponent_id
        ):
            # 被挑衅一方的手势由机器人代替（代替发送前仅短暂经过）
            state = f"等待 Bot 代替 {duel.opponent_name} 发送猜拳表情"
        else:
            players = (
                (duel.challenger_id, duel.challenger_name),
                (duel.opponent_id, duel.opponent_name),
            )
            pending = [
                (uid, name)
                for uid, name in players
                if not await completed_gestures(duel, uid)
            ]
            if len(pending) == 1:
                pending_id, pending_name = pending[0]
                other_id = (
                    duel.opponent_id
                    if pending_id == duel.challenger_id
                    else duel.challenger_id
                )
                sent = len(duel.gestures.get(pending_id, []))
                quota = await required_gestures(group_id, pending_id, other_id)
                progress = f"（已发送 {sent}/{quota}）" if quota > 1 else ""
                state = f"等待 {pending_name} 发送猜拳表情{progress}"
            else:
                # 双方都未出拳（双方均已出拳时会立即结算，不会留在列表中）
                state = "等待双方发送猜拳表情"
        lines.append(
            f"{duel.challenger_name} 与 {duel.opponent_name} 的决斗"
            f"（×{duel.multiplier}）：{state}，{suffix}。"
        )
    return lines


def _parse_status_args(tokens: list[str]) -> str | None:
    """解析 /duel.status 命令参数，返回 "self"、"all" 或 None（无法识别）。"""
    if not tokens:
        return "self"
    if len(tokens) == 1 and tokens[0] in {"all", "a"}:
        return "all"
    return None


duel_status_cmd = on_command("duel.status", rule=is_type(GroupMessageEvent))


@duel_status_cmd.handle()
async def handle_duel_status(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel.status 命令：显示自己或（超级用户）本群全部的决斗状态。"""
    scope = _parse_status_args(args.extract_plain_text().lower().split())
    if scope is None:
        await send_text(bot, event.group_id, USAGE_STATUS, reply_to=event.message_id)
        return
    if scope == "all":
        if not await SUPERUSER(bot, event):
            await send_text(
                bot, event.group_id, STATUS_ALL_DENIED, reply_to=event.message_id
            )
            return
        lines = await _all_status_lines(event.group_id)
        empty_text = "本群没有进行中的决斗。"
        header = f"本群有 {len(lines)} 场进行中的决斗："
    else:
        lines = await _status_lines(event.group_id, event.user_id)
        empty_text = "你在本群没有进行中的决斗。"
        header = f"你在本群有 {len(lines)} 场进行中的决斗："
    if not lines:
        await send_text(bot, event.group_id, empty_text)
        return
    numbered = [f"{index}. {line}" for index, line in enumerate(lines, start=1)]
    await send_text(bot, event.group_id, "\n".join([header, *numbered]))
