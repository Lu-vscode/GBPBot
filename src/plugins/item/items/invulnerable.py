"""道具「无懈可击」（编号 406）：绿色精良、Buff型。

使用后获得持续 1 小时的「无懈可击」状态：当拥有者的决斗分数改变时，
该改变被抵消并失去此状态；同一成员可同时拥有多份（各自独立计时，
每份抵消一次改变；状态到期静默消失，不发送提示消息）。

本模块经决斗插件的分数拦截服务（DuelScoreInterceptService）实现
拦截：持有状态的成员分数即将改变时抵消本次改变（把变化量改写为 0）
并消耗一份状态（优先消耗最早到期的一份）。全部决斗分数写入（决斗
结算、道具效果等）都经过该拦截点；拦截在同步段内执行，提示消息经
异步任务尽快发出。
"""

import asyncio

from nonebot import get_bots, logger
from nonebot.adapters.onebot.v11 import Bot

from src.plugins._shared.onebot import send_group_text
from src.plugins._shared.services import DuelScoreInterceptService, require_service
from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemType,
    ItemUseContext,
    Quality,
    add_state,
    list_states,
    register_item,
    remove_state,
)

# 道具编号、状态键前缀与状态持续时长（秒）
_ITEM_ID = "406"
_STATE_KEY_PREFIX = f"{_ITEM_ID}.invulnerable."
_DURATION_SECONDS = 3600.0

# 决斗分数拦截服务（由决斗插件提供；_framework 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_intercept_service = require_service(DuelScoreInterceptService)


def _active_states(group_id: int, user_id: int) -> list[ItemState]:
    """返回成员持有的全部有效「无懈可击」状态（按到期时间升序）。

    expires_at 为 None（异常数据）的条目排在最前，优先被消耗。
    """
    states = [
        state
        for state in list_states(group_id, user_id)
        if state.key.startswith(_STATE_KEY_PREFIX)
    ]
    states.sort(key=lambda state: state.expires_at or 0.0)
    return states


def _next_state_key(group_id: int, user_id: int) -> str:
    """为新的一份状态生成状态键（在现存有效状态的序号中取最小可用值）。

    已过期但未清理的状态可能占用同序号键；add_state 添加同键状态时
    会覆盖旧条目，序号仍可复用。
    """
    used: set[int] = set()
    for state in _active_states(group_id, user_id):
        suffix = state.key[len(_STATE_KEY_PREFIX) :]
        if suffix.isdigit():
            used.add(int(suffix))
    index = 1
    while index in used:
        index += 1
    return f"{_STATE_KEY_PREFIX}{index}"


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：登记一份持续 1 小时的「无懈可击」状态并发送提示。"""
    add_state(
        context.group_id,
        context.user_id,
        state=ItemState(
            key=_next_state_key(context.group_id, context.user_id),
            item_id=_ITEM_ID,
        ),
        duration_seconds=_DURATION_SECONDS,
    )
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}。\n\n"
        f"1 小时内，{context.user_name} 的决斗分数改变会被抵消一次。"
    )


# 抵消提示的异步发送任务集合（持有引用避免任务被垃圾回收，结束后自动移出）
_notice_tasks: set[asyncio.Task[None]] = set()


def _on_notice_task_done(task: asyncio.Task[None]) -> None:
    """抵消提示发送任务结束时移出任务集合，异常时记录日志避免静默失效。"""
    _notice_tasks.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning(f"「无懈可击」抵消提示的发送任务异常：{error!r}")


async def _send_notice(group_id: int, text: str) -> None:
    """发送抵消提示（机器人离线或发送失败仅记录日志）。"""
    bot = next(iter(get_bots().values()), None)
    if not isinstance(bot, Bot):
        logger.warning(f"「无懈可击」抵消提示未发送：机器人不在线（群 {group_id}）")
        return
    await send_group_text(bot, group_id, text, label="道具消息")


def _schedule_notice(group_id: int, text: str) -> None:
    """调度一条抵消提示尽快发出（拦截在同步段内，无法直接发送）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            f"「无懈可击」抵消提示未发送：当前没有运行中的事件循环（群 {group_id}）"
        )
        return
    task = loop.create_task(_send_notice(group_id, text))
    _notice_tasks.add(task)
    task.add_done_callback(_on_notice_task_done)


def _intercept_score_change(group_id: int, user_id: int, name: str, delta: int) -> int:
    """分数改变拦截：抵消一次变化并消耗一份「无懈可击」状态。

    在分数写入的同步段内调用：持有状态时消耗最早到期的一份、登记抵消
    提示，返回 0（分数不变）；未持有状态或变化量为 0 时原样返回。
    """
    if delta == 0:
        return delta
    states = _active_states(group_id, user_id)
    if not states:
        return delta
    remove_state(group_id, user_id, states[0].key)
    logger.info(
        f"群 {group_id} 成员 {user_id} 的「无懈可击」生效，"
        f"抵消了一次 {delta:+d} 的决斗分数改变"
    )
    display_name = name.strip() or f"QQ {user_id}"
    _schedule_notice(
        group_id,
        f"{display_name} 的无懈可击生效，抵消了一次决斗分数改变"
        f"（{delta:+d}）。\n{display_name} 的决斗分数不变。",
    )
    return 0


_intercept_service.add_interceptor(_intercept_score_change)

register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="无懈可击",
        quality=Quality.GREEN,
        types=(ItemType.BUFF,),
        description="抵消一次决斗分数改变",
        effect=(
            "使用后获得持续1小时的“无懈可击”状态。\n"
            "“无懈可击”状态：当拥有者的决斗分数改变时，抵消改变并失去此状态。"
        ),
        condition="无",
        timing="任意",
        note="注意使用时机",
        handle_use=_handle_use,
    )
)
