"""决斗流程：胜负判定、结算与机器人出手。

胜负判定按手势组合（见 _gestures.WINNING_GESTURES）进行；结算流程
发布 settling 事件（订阅方可改写点数与胜负、设置默认结算消息前缀，
或接管结算），按最终点数更新双方分数并播报结果，随后发布 settled
事件；机器人代打（机器人掷骰接受决斗后出手，或被挑衅时代替对方出手）
发送猜拳表情并记录为对方手势，随后检查双方是否均已出手并认领结算。
"""

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from src.plugins._shared.services import DuelEvent, DuelEventKind
from src.plugins.duel._config import text
from src.plugins.duel._events import fire_duel_event, make_duel_event
from src.plugins.duel._gestures import WINNING_GESTURES
from src.plugins.duel._messages import fetch_rps_result, send_rps, send_text
from src.plugins.duel._scores import update_score
from src.plugins.duel._state import Duel, duel_ready, record_gesture, remove_duel


def _decide_winner(duel: Duel) -> tuple[int, str, int, str] | None:
    """判定胜负，返回 (胜者 QQ 号, 胜者群昵称, 负者 QQ 号, 负者群昵称)。

    平局时返回 None；调用前须保证双方均已出手。
    """
    challenger_gesture = duel.gestures[duel.challenger_id]
    opponent_gesture = duel.gestures[duel.opponent_id]
    if challenger_gesture == opponent_gesture:
        return None
    if (challenger_gesture, opponent_gesture) in WINNING_GESTURES:
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
    判定；处理器可设置 claimed 接管本次结算，此时跳过默认的分数结算与
    结果播报、也不使用前缀，由处理器自行完成），结算与播报后发布 settled
    事件（接管结算时在接管处理器完成结算后发布）。
    """
    result = _decide_winner(duel)
    event = make_duel_event(
        duel,
        DuelEventKind.SETTLING,
        winner_id=result[0] if result is not None else None,
        loser_id=result[2] if result is not None else None,
        challenger_gesture=duel.gestures[duel.challenger_id],
        opponent_gesture=duel.gestures[duel.opponent_id],
    )
    await fire_duel_event(event)
    final = _final_result(duel, result, event)
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
    """机器人发送猜拳表情并记录为对方（接受方）的手势，随后检查结算。

    用于两种场景：机器人掷骰接受决斗后出手（@机器人的决斗），以及
    被挑衅的决斗中代替对方出手。发送猜拳表情无法指定结果、发送接口
    也不返回结果，需在发送后通过 get_msg 查询该消息读取实际结果；
    查询失败时本次不记录手势（机器人自己参与的决斗仍可在协议端
    上报该消息时补记手势）。
    """
    message_id = await send_rps(bot, duel.group_id)
    if message_id is None:
        return
    gesture = await fetch_rps_result(bot, message_id)
    if gesture is None:
        logger.warning(
            f"未能获取机器人猜拳表情（消息 {message_id}）的结果，本次未记录手势"
        )
        return
    # 记录手势（同步段，避免并发事件重复记录），随后发布事件、
    # 检查就绪并认领结算（认领有对象身份保护，不会重复结算）
    if not record_gesture(duel, duel.opponent_id, gesture):
        return
    await fire_duel_event(
        make_duel_event(
            duel, DuelEventKind.GESTURE, actor_id=duel.opponent_id, gesture=gesture
        )
    )
    if not duel_ready(duel):
        return
    if not remove_duel(duel):
        return
    await resolve_duel(bot, duel)
