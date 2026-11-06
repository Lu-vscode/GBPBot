"""决斗流程：胜负判定、结算与机器人出手。

胜负判定集中在 _judgement（整局判定效果 → 单组判定规则 → 基础猜拳
规则），本模块只负责取判定结果并完成结算：发布 settling 事件（订阅方
可修改点数、追加默认结算消息前缀，或设置 claimed 接管结算），随后按
优先级询问结算接管者（经 DuelSettleService 注册，第一个返回 True 的
完成结算并跳过默认结算），否则按最终点数更新双方分数并播报结果，随后
发布 settled 事件。胜负只能经 DuelGestureService 注册的判定规则或结算
接管改变：settling 事件处理器对胜负字段的改写会被还原并记录错误日志。

机器人代打（机器人掷骰接受决斗后出手，或被挑衅时代替对方出手）循环
发送猜拳表情并记录为对方手势直至满足所需数量，随后检查双方是否均已
完成出手并认领结算。
"""

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from src.plugins._shared.services import DuelEventKind, DuelGestureSide
from src.plugins.duel._config import text
from src.plugins.duel._events import fire_duel_event, make_duel_event, run_settlers
from src.plugins.duel._judgement import judge
from src.plugins.duel._messages import fetch_rps_result, send_rps, send_text
from src.plugins.duel._scores import update_score
from src.plugins.duel._state import (
    Duel,
    completed_gestures,
    duel_ready,
    duels,
    record_gesture,
    remove_duel,
)


def _result_of(duel: Duel, winner_id: int | None) -> tuple[int, str, int, str] | None:
    """把判定出的胜者映射为 (胜者 QQ 号, 胜者群昵称, 负者, 负者名)。

    平局（winner_id 为 None）时返回 None。
    """
    if winner_id is None:
        return None
    if winner_id == duel.challenger_id:
        return (
            duel.challenger_id,
            duel.challenger_name,
            duel.opponent_id,
            duel.opponent_name,
        )
    return (
        duel.opponent_id,
        duel.opponent_name,
        duel.challenger_id,
        duel.challenger_name,
    )


async def resolve_duel(bot: Bot, duel: Duel) -> None:
    """结算双方均已出手的决斗：更新分数并发送结果。

    调用前该决斗必须已从决斗列表中认领移除，保证只结算一次。胜负由
    _judgement.judge 判定（整局判定效果 → 单组判定规则 → 基础猜拳规则），
    判定给出的说明作为默认结算消息前缀；随后发布 settling 事件（处理器可
    改写点数、读取双方手势，或设置 claimed 接管结算；点数负数按 0 处理），
    并按优先级询问结算接管者（经 DuelSettleService 注册，第一个返回 True
    的接管本场结算；接管与在 settling 事件中设置 claimed 的效果相同：跳过
    默认的分数结算与结果播报、也不使用前缀，由接管者自行完成）。settling
    处理器对胜负字段的改写会被还原并记录错误日志：胜负只能经
    DuelGestureService 注册的判定规则（或结算接管）改变。结算与播报后发布
    settled 事件（接管结算时在接管者完成结算后发布，事件的 claimed 为 True，
    供订阅方判断默认的分数结算是否已执行）。
    """
    challenger_gestures = tuple(duel.gestures[duel.challenger_id])
    opponent_gestures = tuple(duel.gestures[duel.opponent_id])
    verdict = judge(
        duel.group_id,
        DuelGestureSide(
            user_id=duel.challenger_id,
            name=duel.challenger_name,
            gestures=challenger_gestures,
        ),
        DuelGestureSide(
            user_id=duel.opponent_id,
            name=duel.opponent_name,
            gestures=opponent_gestures,
        ),
    )
    final = _result_of(duel, verdict.winner_id)
    judged_winner = final[0] if final is not None else None
    judged_loser = final[2] if final is not None else None
    event = make_duel_event(
        duel,
        DuelEventKind.SETTLING,
        winner_id=judged_winner,
        loser_id=judged_loser,
        challenger_gestures=challenger_gestures,
        opponent_gestures=opponent_gestures,
        result_prefix=verdict.detail,
    )
    await fire_duel_event(event)
    if (event.winner_id, event.loser_id) != (judged_winner, judged_loser):
        # 胜负只读：settling 事件处理器不能改写胜负（需要通过判定规则或
        # 结算接管改变），改写会被还原并记录错误日志
        logger.error(
            f"群 {duel.group_id} 的 settling 事件处理器把胜负改写为 "
            f"{event.winner_id} 胜、{event.loser_id} 负（判定结果为 "
            f"{judged_winner} 胜），已还原；胜负只能经 DuelGestureService "
            "注册的判定规则或结算接管改变"
        )
        event.winner_id = judged_winner
        event.loser_id = judged_loser
    if not event.claimed and await run_settlers(event):
        event.claimed = True
    if event.claimed:
        logger.info(
            f"群 {duel.group_id} 中 {duel.challenger_id} 与 {duel.opponent_id} "
            "的决斗结算已被接管，跳过默认结算与播报"
        )
        await fire_duel_event(
            make_duel_event(
                duel,
                DuelEventKind.SETTLED,
                multiplier=event.multiplier,
                winner_id=final[0] if final is not None else None,
                loser_id=final[2] if final is not None else None,
                winner_name=final[1] if final is not None else None,
                loser_name=final[3] if final is not None else None,
                draw=final is None,
                claimed=True,
            )
        )
        return
    multiplier = event.multiplier
    if multiplier < 0:
        logger.warning(
            f"settling 事件把群 {duel.group_id} 决斗（原点数 {duel.multiplier}）"
            f"的点数改为负数 {multiplier}，已按 0 结算"
        )
        multiplier = 0
    # 判定效果的说明（或 settling 事件处理器设置的前缀）拼在默认结算
    # 消息前；结算被接管时已跳过默认播报，前缀自然不被使用
    prefix = event.result_prefix or ""
    if final is None:
        logger.info(
            f"群 {duel.group_id} 中 {duel.challenger_id} 与 "
            f"{duel.opponent_id} 的决斗平局结束"
        )
        await send_text(
            bot,
            duel.group_id,
            prefix
            + text(
                "draw",
                player_a=duel.challenger_name,
                player_b=duel.opponent_name,
                multiplier=multiplier,
            ),
        )
    else:
        winner_id, winner_name, loser_id, loser_name = final
        update_score(duel.group_id, winner_id, winner_name, multiplier)
        update_score(duel.group_id, loser_id, loser_name, -multiplier)
        logger.info(
            f"群 {duel.group_id} 的决斗中 {winner_id}（{winner_name}）获胜，"
            f"{loser_id}（{loser_name}）落败"
        )
        await send_text(
            bot,
            duel.group_id,
            prefix
            + text(
                "win",
                winner=winner_name,
                loser=loser_name,
                multiplier=multiplier,
            ),
        )
    await fire_duel_event(
        make_duel_event(
            duel,
            DuelEventKind.SETTLED,
            multiplier=multiplier,
            winner_id=final[0] if final is not None else None,
            loser_id=final[2] if final is not None else None,
            winner_name=final[1] if final is not None else None,
            loser_name=final[3] if final is not None else None,
            draw=final is None,
        )
    )


async def send_bot_rps(bot: Bot, duel: Duel) -> None:
    """机器人循环发送猜拳表情并记录为对方（接受方）的手势，随后检查结算。

    用于两种场景：机器人掷骰接受决斗后出手（@机器人的决斗），以及
    被挑衅的决斗中代替对方出手。对方需要多个手势时（见
    _state.completed_gestures）循环发送直至记录完全部所需数量。
    发送猜拳表情无法指定结果、发送接口也不返回结果，需在发送后通过
    get_msg 查询该消息读取实际结果；查询失败时本次不记录手势
    （机器人自己参与的决斗仍可在协议端上报该消息时补记手势）。
    代发期间机器人自己的消息事件不记录手势（见 _state.take_gesture），
    避免与回查结果重复记录。
    """
    duel.bot_rps_active = True
    try:
        while not completed_gestures(duel, duel.opponent_id):
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
            if not record_gesture(duel, duel.opponent_id, gesture):
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
    if not duel_ready(duel):
        return
    if not remove_duel(duel):
        return
    await resolve_duel(bot, duel)
