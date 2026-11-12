"""机器人代替一方出拳（掷骰接受决斗与挑衅代发）。

机器人循环发送猜拳表情并记录为对方手势直至满足所需数量，随后检查双方
是否均已完成出手并认领结算。两种场景共用本流程：机器人掷骰接受决斗后
出手（@机器人的决斗），以及被挑衅时代替对方出手。
"""

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from src.plugins._shared.services import DuelEventKind
from src.plugins.duel.core import (
    Duel,
    completed_gestures,
    duel_ready,
    duels,
    record_gesture,
    remove_duel,
)
from src.plugins.duel.flow.settle import resolve_duel
from src.plugins.duel.messaging import fetch_rps_result, send_rps
from src.plugins.duel.services.events import fire_duel_event, make_duel_event


async def send_bot_rps(bot: Bot, duel: Duel) -> None:
    """机器人循环发送猜拳表情并记录为对方（接受方）的手势，随后检查结算。

    对方需要多个手势时（见 core.store.completed_gestures）循环发送直至
    记录完全部所需数量。发送猜拳表情无法指定结果、发送接口也不返回结果，
    需在发送后通过 get_msg 查询该消息读取实际结果；查询失败时本次不记录
    手势（机器人自己参与的决斗仍可在协议端上报该消息时补记手势）。
    代发期间机器人自己的消息事件不记录手势（见 core.store.take_gesture），
    避免与回查结果重复记录。
    """
    duel.bot_rps_active = True
    try:
        while not await completed_gestures(duel, duel.opponent_id):
            if not any(item is duel for item in duels):
                # 决斗已被移除（取消或超时），停止发送
                return
            message_id = await send_rps(bot, duel.group_id)
            if message_id is None:
                return
            gesture = await fetch_rps_result(bot, message_id)
            if gesture is None:
                logger.warning(
                    f"未能获取机器人猜拳表情（消息 {message_id}）的结果，本次未记录手势"
                )
                return
            # 记录手势（同步段，避免并发事件重复记录），随后发布事件；
            # 记录失败（决斗已被移除）时停止发送
            if not await record_gesture(duel, duel.opponent_id, gesture):
                return
            await fire_duel_event(
                make_duel_event(
                    duel,
                    DuelEventKind.GESTURE,
                    actor_id=duel.opponent_id,
                    gesture=gesture,
                )
            )
    finally:
        duel.bot_rps_active = False
    if not await duel_ready(duel):
        return
    if not remove_duel(duel):
        return
    await resolve_duel(bot, duel)
