"""决斗的动作类跨插件服务：挑衅、点数缩放、取消与认输。

- DuelProvokeService：供道具等插件强制对方接受决斗，并由机器人代替对方
  发送猜拳表情；
- DuelMultiplierService：供道具等插件缩放进行中决斗的点数，缩放到 0 时
  取消决斗并发布取消事件；
- DuelCancelService：供道具等插件主动取消进行中的决斗并发布取消事件；
- DuelForfeitService：供道具等插件让进行中的决斗跳过出拳阶段、按指定
  一方落败直接结算（如道具「投降」）。

四个服务实例 provoke_service、multiplier_service、cancel_service、
forfeit_service 由插件主模块统一注册。
"""

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from src.plugins._shared.services import (
    DuelCancelService,
    DuelEventKind,
    DuelForfeitService,
    DuelMultiplierService,
    DuelProvokeService,
    DuelScaleOutcome,
    DuelSnapshot,
)
from src.plugins.duel.core.store import (
    find_pair_duel,
    find_pending_duel,
    find_provoked_duel,
    remove_duel,
    snapshot,
)
from src.plugins.duel.flow.botplay import send_bot_rps
from src.plugins.duel.flow.settle import resolve_duel
from src.plugins.duel.services.events import fire_duel_event, make_duel_event


class _DuelProvokeService(DuelProvokeService):
    """决斗挑衅服务的实现：供道具等插件强制对方接受决斗。"""

    async def accept_by_provoke(
        self, group_id: int, challenger_id: int, opponent_id: int
    ) -> DuelSnapshot | None:
        """强制对方接受决斗并标记对方手势由机器人代替，返回决斗快照。"""
        duel = find_pending_duel(group_id, challenger_id, opponent_id)
        if duel is None:
            return None
        # 同步段完成状态标记：此后对方发送的猜拳表情被忽略、手势由机器人代替
        duel.accepted = True
        duel.provoked_by_bot = True
        logger.info(
            f"群 {group_id} 成员 {challenger_id} 挑衅 {opponent_id}，"
            f"决斗已强制接受（点数 {duel.multiplier}），对方手势将由机器人代替"
        )
        await fire_duel_event(
            make_duel_event(duel, DuelEventKind.ACCEPTED, actor_id=duel.opponent_id)
        )
        return await snapshot(duel)

    async def send_provoked_gesture(
        self, bot: Bot, group_id: int, challenger_id: int, opponent_id: int
    ) -> None:
        """由机器人代替被挑衅决斗的对方发送猜拳表情并记录为对方手势。"""
        duel = find_provoked_duel(group_id, challenger_id, opponent_id)
        if duel is None:
            logger.warning(
                f"群 {group_id} 待机器人代替出手的被挑衅决斗（{challenger_id} 与 "
                f"{opponent_id}）已不在进行中，未发送猜拳表情"
            )
            return
        await send_bot_rps(bot, duel)


class _DuelMultiplierService(DuelMultiplierService):
    """决斗点数缩放服务的实现：供道具等插件缩放进行中决斗的点数。"""

    async def scale_multiplier(
        self,
        group_id: int,
        user_a: int,
        user_b: int,
        numerator: int,
        denominator: int,
    ) -> DuelScaleOutcome | None:
        """按比例缩放双方之间进行中决斗的点数；缩放到 0 时取消决斗。"""
        if numerator < 1 or denominator < 1:
            message = (
                f"缩放比例的分子与分母必须为正整数，收到 {numerator}/{denominator}"
            )
            raise ValueError(message)
        duel = find_pair_duel(group_id, user_a, user_b)
        if duel is None:
            return None
        old = duel.multiplier
        new = old * numerator // denominator
        if new <= 0:
            # 同步段完成取消（从决斗列表移除），再发布取消事件
            remove_duel(duel)
            logger.info(
                f"群 {group_id} 中 {duel.challenger_id} 与 {duel.opponent_id} "
                f"的决斗点数 {old} 被缩放为 0，决斗已取消"
            )
            await fire_duel_event(
                make_duel_event(duel, DuelEventKind.CANCELED, multiplier=0)
            )
            return DuelScaleOutcome(multiplier=0, canceled=True)
        duel.multiplier = new
        logger.info(
            f"群 {group_id} 中 {duel.challenger_id} 与 {duel.opponent_id} "
            f"的决斗点数由 {old} 变为 {new}（比例 {numerator}/{denominator}）"
        )
        return DuelScaleOutcome(multiplier=new, canceled=False)


class _DuelCancelService(DuelCancelService):
    """决斗取消服务的实现：供道具等插件主动取消进行中的决斗。"""

    async def cancel_duel(
        self, group_id: int, user_a: int, user_b: int
    ) -> DuelSnapshot | None:
        """取消双方之间进行中的决斗；在同步段移除后发布取消事件。"""
        duel = find_pair_duel(group_id, user_a, user_b)
        if duel is None:
            return None
        # 同步段完成取消（从决斗列表移除），再发布取消事件
        remove_duel(duel)
        logger.info(
            f"群 {group_id} 中 {duel.challenger_id} 与 {duel.opponent_id} "
            f"的决斗（点数 {duel.multiplier}）被取消"
        )
        await fire_duel_event(make_duel_event(duel, DuelEventKind.CANCELED))
        return await snapshot(duel)


class _DuelForfeitService(DuelForfeitService):
    """决斗认输服务的实现：供道具等插件跳过出拳阶段、按指定一方落败结算。"""

    async def forfeit_duel(
        self,
        bot: Bot,
        group_id: int,
        loser_id: int,
        winner_id: int,
        prefix: str | None = None,
    ) -> DuelSnapshot | None:
        """按指定一方落败完成进行中决斗的结算；无符合条件的决斗时返回 None。"""
        if loser_id == winner_id:
            return None
        duel = find_pair_duel(group_id, loser_id, winner_id)
        if duel is None or not duel.accepted:
            return None
        snap = await snapshot(duel)
        # 同步段完成认领（从决斗列表移除），保证只结算一次；随后按调用方
        # 指定的胜负结算，不再经过手势判定
        if not remove_duel(duel):
            return None
        logger.info(
            f"群 {group_id} 中 {duel.challenger_id} 与 {duel.opponent_id} "
            f"的决斗（点数 {duel.multiplier}）跳过出拳阶段认输结算，"
            f"{loser_id} 落败"
        )
        await resolve_duel(bot, duel, forced_winner_id=winner_id, result_prefix=prefix)
        return snap


# 服务实例（由插件主模块统一注册）
provoke_service = _DuelProvokeService()
multiplier_service = _DuelMultiplierService()
cancel_service = _DuelCancelService()
forfeit_service = _DuelForfeitService()
