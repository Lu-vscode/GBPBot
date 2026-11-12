"""决斗插件的运行时：猜拳消息处理与超时检查。

- duel_gesture：优先级 0 且不阻断事件传播的消息处理器，记录群成员（包括
  机器人自己）的猜拳表情，双方就绪后认领并结算；
- check_duel_timeouts：定时检查并结束超时的决斗，超时提示不立即发出，
  而是转入延迟跟发队列（见 messaging/deferred.py）。

导入本模块即注册猜拳消息处理器与定时任务；插件主模块导入本模块完成注册。
"""

import time

from nonebot import get_bots, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot_plugin_apscheduler import scheduler

from src.plugins._shared.services import DuelEventKind
from src.plugins.duel.config import DURATION_SECONDS, text
from src.plugins.duel.core import (
    duel_ready,
    duels,
    extract_gesture,
    is_rps_message,
    remove_duel,
    take_gesture,
)
from src.plugins.duel.flow.settle import resolve_duel
from src.plugins.duel.messaging import defer_timeout, drop_expired_timeouts
from src.plugins.duel.services.events import fire_duel_event, make_duel_event

# 决斗超时的检查间隔（秒）：超时提示最多延迟该间隔
_TIMEOUT_CHECK_INTERVAL_SECONDS = 30

# 优先级 0 且不阻断事件传播：检测群成员的猜拳表情并更新决斗状态，
# 不影响其它插件（命令、默认回复等）继续处理消息
duel_gesture = on_message(priority=0, block=False, rule=is_rps_message)


@duel_gesture.handle()
async def handle_duel_gesture(bot: Bot, event: GroupMessageEvent) -> None:
    """群成员（包括机器人自己）发送猜拳表情时更新决斗状态并结算。"""
    gesture = extract_gesture(event.get_message())
    if gesture is None:
        return
    # 同步段：记录手势（避免并发事件重复记录），随后发布事件、
    # 检查就绪并认领结算（认领有对象身份保护，不会重复结算）
    duel = await take_gesture(event.group_id, event.user_id, gesture)
    if duel is None:
        return
    logger.debug(
        f"已记录成员 {event.user_id} 在群 {event.group_id} 决斗中的手势 {gesture}"
    )
    await fire_duel_event(
        make_duel_event(
            duel, DuelEventKind.GESTURE, actor_id=event.user_id, gesture=gesture
        )
    )
    if not await duel_ready(duel):
        return
    if not remove_duel(duel):
        return
    await resolve_duel(bot, duel)


async def check_duel_timeouts() -> None:
    """定时检查并结束超时的决斗。

    机器人不在线时保留决斗状态，等下次检查（机器人上线后）再通知。
    超时提示不立即发出：先保留 1 分钟，期间机器人向该群发送其它消息
    时在其后延迟 1 秒发出，保留期内未能跟随则静默丢弃。
    """
    now = time.monotonic()
    bots = get_bots()
    # 清理超过保留时间的旧提示，遍历快照收集待通知的超时决斗并完成
    # 认领（认领有对象身份保护，事件发布的等待期间不会重复认领），
    # 超时提示转入延迟发送队列
    drop_expired_timeouts(now)
    for duel in duels[:]:
        if now - duel.created_at < DURATION_SECONDS:
            continue
        bot = bots.get(str(duel.bot_self_id))
        if not isinstance(bot, Bot):
            continue
        if not remove_duel(duel):
            continue
        logger.info(
            f"群 {duel.group_id} 中 {duel.challenger_id} 与 "
            f"{duel.opponent_id} 的决斗超时结束"
        )
        defer_timeout(
            bot_self_id=duel.bot_self_id,
            group_id=duel.group_id,
            text=text(
                "timeout",
                player_a=duel.challenger_name,
                player_b=duel.opponent_name,
                multiplier=duel.multiplier,
            ),
            now=now,
        )
        await fire_duel_event(make_duel_event(duel, DuelEventKind.TIMEOUT))


scheduler.add_job(
    check_duel_timeouts,
    "interval",
    seconds=_TIMEOUT_CHECK_INTERVAL_SECONDS,
    id="duel_check_timeouts",
    replace_existing=True,
    misfire_grace_time=30,
)
