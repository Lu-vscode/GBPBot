"""道具「复得之轮」（编号 325）：金色传说、复合型。

使用 /item.use 325 <n> 获得持续 n 天的「复得」状态：状态有效期间，
每天 8:00~23:00 每次使用非黑色诅咒的非复制道具（复制道具编号见
copy.py）时，有 1/2-arctan(7ln((n-1)/14))/π 的概率获得一件随机道具
（抽取概率与签到相同，抽到黑色诅咒时按诅咒获得流程播报并自动使用），
否则获得一件随机黑色诅咒道具；每次触发后冷却 1 小时，冷却期间使用
道具既不触发效果也不发送提示。

触发经框架的道具使用后钩子（add_item_use_hook）实现：道具效果成功
执行后由本模块判断使用者是否持有状态、是否处于生效时段与冷却中，因此
道具模块之间无需互相引用。状态附加数据保存天数（days）与冷却截止
时刻（ready_at），冷却记录经 set_state_data 刷新，不改变状态的过期
时刻；状态到期由定时清理静默删除，不发送提示。
"""

import math
import random
import time
from datetime import UTC, datetime

from nonebot import logger

from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemType,
    ItemUseContext,
    Quality,
    acquire_curse,
    add_item,
    add_item_use_hook,
    add_state,
    draw_item_of_quality,
    draw_random_item,
    get_state,
    quality_label,
    register_item,
    set_state_data,
)
from src.plugins.item.items.copy import COPY_ITEM_IDS

# 道具编号与状态键
_ITEM_ID = "325"
_STATE_KEY = f"{_ITEM_ID}.regain"

# 状态附加数据中的键：持续天数与冷却截止时刻（Unix 时间戳）
_DAYS_KEY = "days"
_READY_AT_KEY = "ready_at"

# 每次触发后的冷却时长（秒）
_COOLDOWN_SECONDS = 3600.0

# 每日生效窗口（本地时区的小时，含起始小时、不含结束小时）
_ACTIVE_START_HOUR = 8
_ACTIVE_END_HOUR = 23

# 一天的秒数与天数上限（与 C 的 signed int 上限相同）
_SECONDS_PER_DAY = 86400.0
_MAX_DAYS = 2**31 - 1

# 概率公式 1/2-arctan(7ln((n-1)/14))/π 中的常量
_LOG_FACTOR = 7.0
_MIDPOINT_DAYS = 14.0


def _success_chance(days: int) -> float:
    """返回一次触发获得随机道具（而非黑色诅咒）的概率。

    公式为 1/2-arctan(7ln((n-1)/14))/π：n=1 时 ln0 取 -∞，概率为 1，
    先行返回以免对数无定义；n=14 附近为 1/2，n 越大几率越小。
    """
    if days <= 1:
        return 1.0
    chance = (
        0.5 - math.atan(_LOG_FACTOR * math.log((days - 1) / _MIDPOINT_DAYS)) / math.pi
    )
    return min(1.0, max(0.0, chance))


def _chance_text(chance: float) -> str:
    """把概率渲染为百分比文案（整数百分比不保留小数部分）。"""
    return f"{chance * 100:.1f}".removesuffix(".0")


def _active_now() -> bool:
    """判断当前时刻是否处于每日生效窗口（本地时区 8:00~23:00）。"""
    return _ACTIVE_START_HOUR <= datetime.now(UTC).astimezone().hour < _ACTIVE_END_HOUR


def _parse_days(text: str) -> int | None:
    """解析天数参数（正整数，上限与 C 的 signed int 相同），无效时返回 None。"""
    if not text.isascii() or not text.isdigit():
        return None
    try:
        days = int(text)
    except ValueError:
        # 超长数字串超出 Python 的整数转换位数限制
        return None
    return days if 1 <= days <= _MAX_DAYS else None


def _usage(context: ItemUseContext) -> str:
    """返回使用指令的用法提示。"""
    return f"请提供天数，用法：/item.use {context.item.item_id} <天数>"


def _state_days(state: ItemState) -> int | None:
    """返回状态附加数据中的天数，缺失或无效时返回 None。"""
    days = state.data.get(_DAYS_KEY)
    if isinstance(days, bool) or not isinstance(days, int):
        return None
    return days if 1 <= days <= _MAX_DAYS else None


def _in_cooldown(state: ItemState, now: float) -> bool:
    """判断状态是否处于触发后的冷却中。"""
    ready_at = state.data.get(_READY_AT_KEY)
    if isinstance(ready_at, bool) or not isinstance(ready_at, (int, float)):
        return False
    return float(ready_at) > now


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：不得处于「复得」状态，且天数参数合法。"""
    if get_state(context.group_id, context.user_id, _STATE_KEY) is not None:
        return f"你正处于“复得”状态，无法再次使用 {context.item.label}。"
    if len(context.args) != 1:
        return _usage(context)
    if _parse_days(context.args[0]) is None:
        return (
            f"天数必须为 1~{_MAX_DAYS} 的整数，"
            f"用法：/item.use {context.item.item_id} <天数>"
        )
    return None


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：登记持续 n 天的「复得」状态并发送提示。"""
    days = _parse_days(context.args[0]) if len(context.args) == 1 else None
    if days is None:  # 不可达：can_use 已校验参数
        return
    add_state(
        context.group_id,
        context.user_id,
        state=ItemState(key=_STATE_KEY, item_id=_ITEM_ID, data={_DAYS_KEY: days}),
        duration_seconds=days * _SECONDS_PER_DAY,
    )
    await context.send(
        f"{context.user_name} 转动了 {context.item.label}。\n"
        f"“复得”将持续 {days} 天：每天 8:00~23:00 使用非黑色诅咒的非复制道具时，"
        f"有 {_chance_text(_success_chance(days))}% 的概率获得一件随机道具，"
        "否则获得一件随机黑色诅咒道具；每次触发后冷却 1 小时。"
    )


async def _trigger(
    context: ItemUseContext, state: ItemState, days: int, now: float
) -> None:
    """触发一次「复得」效果：按概率发放道具并记录冷却截止时刻。"""
    if random.random() < _success_chance(days):
        item = draw_random_item()
    else:
        item = draw_item_of_quality(Quality.BLACK_CURSE)
    if item is None:
        logger.warning(
            f"复得效果抽取失败（群 {context.group_id}，成员 {context.user_id}），"
            "本次不发放道具也不进入冷却"
        )
        return
    set_state_data(
        context.group_id,
        context.user_id,
        _STATE_KEY,
        {**state.data, _READY_AT_KEY: now + _COOLDOWN_SECONDS},
    )
    logger.info(
        f"群 {context.group_id} 成员 {context.user_id}（{context.user_name}）"
        f"的“复得”生效：发放道具 {item.label}（{quality_label(item.quality)}）"
    )
    if item.quality is Quality.BLACK_CURSE:
        await context.send(
            f"{context.user_name} 的“复得”如约而至，"
            f"代价是黑色诅咒道具 {item.label} 找上了门。"
        )
        await acquire_curse(
            context.bot, context.group_id, context.user_id, context.user_name, item
        )
        return
    add_item(context.group_id, context.user_id, item.item_id)
    await context.send(
        f"{context.user_name} 的“复得”如约而至，"
        f"获得道具 {item.label}【{quality_label(item.quality)}】。"
    )


async def _on_item_used(context: ItemUseContext) -> None:
    """道具使用后的附加效果：持有「复得」状态时按概率触发一次效果。

    使用复得之轮本身不触发（那一次使用换来的是状态，而非效果）；
    黑色诅咒由自身的获得流程处理，复制道具按设计文档排除在触发条件
    之外，两者均不影响其它道具的触发。
    """
    if context.item.item_id == _ITEM_ID:
        return
    if context.item.quality is Quality.BLACK_CURSE:
        return
    if context.item.item_id in COPY_ITEM_IDS:
        return
    state = get_state(context.group_id, context.user_id, _STATE_KEY)
    if state is None or not _active_now():
        return
    days = _state_days(state)
    if days is None:
        logger.warning(
            f"“复得”状态缺少有效的天数记录（群 {context.group_id}，"
            f"成员 {context.user_id}），本次不触发效果"
        )
        return
    now = time.time()
    if _in_cooldown(state, now):
        return
    await _trigger(context, state, days, now)


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="复得之轮",
        quality=Quality.GOLD,
        types=(ItemType.COMPOSITE,),
        description="绝对的倒转，由此而生的数值，教会你爱的是……",
        effect=(
            f'使用指令"/item.use {_ITEM_ID} <n>"获得持续n天的“复得”状态：'
            "每天8:00到23:00每次使用非黑色诅咒的非复制道具有"
            "(1/2-arctan(7ln((n-1)/14))/π)的概率获得一个随机道具"
            "（概率与签到相同），否则获得一个随机黑色诅咒道具，冷却时间1小时。"
        ),
        condition="没有“复得”状态",
        timing="任意",
        note="每一份命运的馈赠，早已暗中标好了价格。",
        accepts_args=True,
        can_use=_can_use,
        handle_use=_handle_use,
        state_labels=((_STATE_KEY, "复得"),),
    )
)

add_item_use_hook(_on_item_used)
