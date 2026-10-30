"""决斗流程：胜负判定、结算与机器人出手。

胜负判定按手势组合（见 _gestures.WINNING_GESTURES）进行；一方需要
多个手势时（如道具效果，数量见 _state.required_gestures）按逐手势
与对方手势判定的聚合结果判定：至少一个手势胜则该方胜、否则至少
一个手势平则平局、否则该方负（双方都需要多个手势时以发起方为
聚合方、另一方取第一个手势）。结算流程发布 settling 事件（订阅方
可改写点数与胜负、设置默认结算消息前缀，或按旧机制设置 claimed
接管），随后按优先级询问结算接管者（经 DuelSettleService 注册，
第一个返回 True 的完成结算并跳过默认结算），否则按最终点数更新
双方分数并播报结果，随后发布 settled 事件；机器人代打（机器人
掷骰接受决斗后出手，或被挑衅时代替对方出手）循环发送猜拳表情并
记录为对方手势直至满足所需数量，随后检查双方是否均已完成出手并
认领结算。
"""

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from src.plugins._shared.services import DuelEvent, DuelEventKind
from src.plugins.duel._config import text
from src.plugins.duel._events import fire_duel_event, make_duel_event, run_settlers
from src.plugins.duel._gestures import WINNING_GESTURES
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


def _gesture_outcome(gesture: int, other: int) -> int:
    """单组手势的判定结果：1 胜、0 平、-1 负。"""
    if gesture == other:
        return 0
    if (gesture, other) in WINNING_GESTURES:
        return 1
    return -1


def _aggregate_outcome(gestures: list[int], other: int) -> int:
    """聚合多手势方对对方单个手势的逐手势判定结果。

    至少一个手势胜则返回 1；否则至少一个手势平则返回 0；否则返回 -1。
    """
    outcomes = [_gesture_outcome(gesture, other) for gesture in gestures]
    if 1 in outcomes:
        return 1
    if 0 in outcomes:
        return 0
    return -1


def _decide_winner(duel: Duel) -> tuple[int, str, int, str] | None:
    """判定胜负，返回 (胜者 QQ 号, 胜者群昵称, 负者 QQ 号, 负者群昵称)。

    平局时返回 None；调用前须保证双方均已出手。一方需要多个手势时
    按逐手势聚合判定（见 _aggregate_outcome），另一方取第一个手势；
    双方都需要多个手势时同样以发起方为聚合方。
    """
    challenger_gestures = duel.gestures[duel.challenger_id]
    opponent_gestures = duel.gestures[duel.opponent_id]
    if len(challenger_gestures) > 1:
        outcome = _aggregate_outcome(challenger_gestures, opponent_gestures[0])
    elif len(opponent_gestures) > 1:
        outcome = -_aggregate_outcome(opponent_gestures, challenger_gestures[0])
    else:
        outcome = _gesture_outcome(challenger_gestures[0], opponent_gestures[0])
    if outcome == 0:
        return None
    if outcome > 0:
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


def _final_result(
    duel: Duel, result: tuple[int, str, int, str] | None, event: DuelEvent
) -> tuple[int, str, int, str] | None:
    """结合 settling 事件的改写确定最终胜负，非法改写回退原判定。

    返回 (胜者 QQ 号, 胜者群昵称, 负者 QQ 号, 负者群昵称)；平局返回 None。
    """
    winner_id = event.winner_id
    loser_id = event.loser_id
    if winner_id is None and loser_id is None:
        return None
    participants = {
        duel.challenger_id: duel.challenger_name,
        duel.opponent_id: duel.opponent_name,
    }
    if (
        winner_id is None
        or loser_id is None
        or winner_id == loser_id
        or winner_id not in participants
        or loser_id not in participants
    ):
        logger.warning(
            f"settling 事件改写的胜负（{winner_id} 胜 {loser_id}）不是本场决斗"
            f"的合法参与者组合，已回退为原判定"
        )
        return result
    return winner_id, participants[winner_id], loser_id, participants[loser_id]


async def resolve_duel(bot: Bot, duel: Duel) -> None:
    """结算双方均已出手的决斗：更新分数并发送结果。

    调用前该决斗必须已从决斗列表中认领移除，保证只结算一次；
    结算前发布 settling 事件（处理器可改写点数与胜负、读取双方手势，并可
    设置前缀文本在默认结算消息前拼接；点数负数按 0 处理、非法改写回退原
    判定），随后把胜负校正为最终合法判定并按优先级询问结算接管者（经
    DuelSettleService 注册，第一个返回 True 的接管本场结算；接管与直接在
    settling 事件中设置 claimed 的效果相同：跳过默认的分数结算与结果播报、
    也不使用前缀，由接管者自行完成），结算与播报后发布 settled 事件
    （接管结算时在接管者完成结算后发布，事件的 claimed 为 True，供订阅方
    判断默认的分数结算是否已执行）。
    """
    result = _decide_winner(duel)
    event = make_duel_event(
        duel,
        DuelEventKind.SETTLING,
        winner_id=result[0] if result is not None else None,
        loser_id=result[2] if result is not None else None,
        challenger_gestures=tuple(duel.gestures[duel.challenger_id]),
        opponent_gestures=tuple(duel.gestures[duel.opponent_id]),
    )
    await fire_duel_event(event)
    final = _final_result(duel, result, event)
    if not event.claimed:
        # 把改写后的胜负校正为最终合法判定，供结算接管者读取；无接管者
        # 返回 True 时继续按默认逻辑结算
        event.winner_id = final[0] if final is not None else None
        event.loser_id = final[2] if final is not None else None
        if await run_settlers(event):
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
    # settling 事件处理器可设置在默认结算消息前拼接的前缀（如道具效果
    # 说明）；结算被接管时已跳过默认播报，前缀自然不被使用
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
