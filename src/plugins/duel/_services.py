"""决斗对外服务的实现（机器人接受、挑衅与点数缩放）。

本模块实现三个跨插件服务契约（见 _shared/services.py），服务实例
（bot_accept_service、provoke_service、multiplier_service）由插件主
模块统一注册到服务注册中心：
- DuelBotAcceptService：供道具等插件校验机器人接受概率函数表达式；
- DuelProvokeService：供道具等插件强制对方接受决斗，并由机器人代替
  对方发送猜拳表情；
- DuelMultiplierService：供道具等插件缩放进行中决斗的点数，缩放到 0
  时取消决斗并发布取消事件。
"""

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from src.plugins._shared.services import (
    DuelBotAcceptService,
    DuelEventKind,
    DuelMultiplierService,
    DuelProvokeService,
    DuelScaleOutcome,
    DuelSnapshot,
)
from src.plugins.duel._config import accept_expr_error
from src.plugins.duel._events import fire_duel_event, make_duel_event
from src.plugins.duel._flow import send_bot_rps
from src.plugins.duel._state import (
    find_pair_duel,
    find_pending_duel,
    find_provoked_duel,
    remove_duel,
    snapshot,
)


class _DuelBotAcceptService(DuelBotAcceptService):
    """机器人接受概率函数服务的实现：供道具等插件校验表达式。"""

    def validate_accept_func(self, expr: str) -> str | None:
        """校验概率函数表达式；额外禁止连续下划线（防外部输入逃逸）。"""
        text = expr.strip()
        if not text:
            return "表达式不能为空。"
        if "__" in text:
            return "表达式不允许包含连续的下划线“__”。"
        return accept_expr_error(text)


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
        return snapshot(duel)

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


# 服务实例（由插件主模块统一注册）
bot_accept_service = _DuelBotAcceptService()
provoke_service = _DuelProvokeService()
multiplier_service = _DuelMultiplierService()
