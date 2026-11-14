"""道具「复得之轮」（编号 325）：金色传说、复合型。

使用 /item.use 325 <n> 获得持续 n 天的「复得」状态：状态有效期间，
每天前 7 次使用非黑色诅咒的非复制道具（带 ItemTag.COPY 标签的道具）
时，每次都有 1/2-arctan(7ln((n-1)/14))/π 的概率获得一件随机道具
（抽取概率与签到相同，抽到黑色诅咒时按诅咒获得流程播报并自动使用），
否则获得一件随机黑色诅咒道具；当天第 8 次起的使用不再触发效果，
也不发送提示。

触发经框架的道具使用后钩子（add_item_use_hook）实现：道具效果成功
执行后由本模块判断使用者是否持有状态、当天触发次数是否用尽，因此
道具模块之间无需互相引用；“复制道具”经框架的标签查询（has_tag）
识别，不从具体道具模块导入编号集合。状态附加数据保存天数（days）与
当天的触发记录（day 为本地日期、uses 为当天已触发次数），记录经
set_state_data 刷新，不改变状态的过期时刻；跨天时按当天尚未触发处理，
状态到期由定时清理静默删除，不发送提示。旧格式记录只带冷却截止时刻，
按当天尚未触发处理，并在下次触发时覆盖为新格式。
"""

import math
import random
from datetime import UTC, datetime

from nonebot import logger

from src.plugins.item.core import (
    ItemBehavior,
    ItemDefinition,
    ItemKind,
    ItemState,
    ItemTag,
    ItemUseContext,
    Quality,
    acquire_curse,
    add_item,
    add_item_use_hook,
    add_state,
    draw_item_of_quality,
    draw_random_item,
    get_state,
    has_tag,
    quality_label,
    register_item,
    set_state_data,
)

# 道具编号与状态键
_ITEM_ID = "325"
_STATE_KEY = f"{_ITEM_ID}.regain"

# 状态附加数据中的键：持续天数、当天日期（本地时区）与当天已触发次数
_DAYS_KEY = "days"
_DAY_KEY = "day"
_USES_KEY = "uses"

# 每天可触发的次数
_DAILY_LIMIT = 7

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


def _today() -> str:
    """返回当前本地时区的日期文本（用作当天触发记录的日期）。"""
    return datetime.now(UTC).astimezone().date().isoformat()


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


def _used_today(state: ItemState) -> int:
    """返回状态记录中当天的触发次数。

    记录日期不是今天（含旧格式只带冷却截止时刻、缺少日期与次数）时
    按当天尚未触发处理；新记录会在下次触发时写入。
    """
    if state.data.get(_DAY_KEY) != _today():
        return 0
    uses = state.data.get(_USES_KEY)
    if isinstance(uses, bool) or not isinstance(uses, int):
        return 0
    return min(max(uses, 0), _DAILY_LIMIT)


async def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：不得处于「复得」状态，且天数参数合法。"""
    if await get_state(context.group_id, context.user_id, _STATE_KEY) is not None:
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
    await add_state(
        context.group_id,
        context.user_id,
        state=ItemState(key=_STATE_KEY, item_id=_ITEM_ID, data={_DAYS_KEY: days}),
        duration_seconds=days * _SECONDS_PER_DAY,
    )
    await context.send(
        f"{context.user_name} 转动了 {context.item.label}。\n"
        f"“复得”将持续 {days} 天：每天前 {_DAILY_LIMIT} 次使用"
        "非黑色诅咒的非复制道具时，"
        f"每次都有 {_chance_text(_success_chance(days))}% 的概率获得一件随机道具，"
        "否则获得一件随机黑色诅咒道具。"
    )


async def _trigger(context: ItemUseContext, days: int, used_today: int) -> None:
    """触发一次「复得」效果：记下当天这次使用，再按概率发放道具。"""
    await set_state_data(
        context.group_id,
        context.user_id,
        _STATE_KEY,
        {_DAYS_KEY: days, _DAY_KEY: _today(), _USES_KEY: used_today + 1},
    )
    if random.random() < _success_chance(days):
        item = draw_random_item()
    else:
        item = draw_item_of_quality(Quality.BLACK_CURSE)
    if item is None:
        logger.warning(
            f"复得效果抽取失败（群 {context.group_id}，成员 {context.user_id}），"
            "本次不发放道具，当天次数已记入"
        )
        return
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
    await add_item(context.group_id, context.user_id, item.item_id)
    await context.send(
        f"{context.user_name} 的“复得”如约而至，"
        f"获得道具 {item.label}【{quality_label(item.quality)}】。"
    )


async def _on_item_used(context: ItemUseContext) -> None:
    """道具使用后的附加效果：持有「复得」状态时按概率触发一次效果。

    使用复得之轮本身不触发（那一次使用换来的是状态，而非效果）；
    黑色诅咒由自身的获得流程处理，复制道具按设计文档排除在触发条件
    之外（标签见 ItemTag.COPY），两者均不影响其它道具的触发。
    """
    if context.item.item_id == _ITEM_ID:
        return
    if context.item.quality is Quality.BLACK_CURSE:
        return
    if has_tag(context.item.item_id, ItemTag.COPY):
        return
    state = await get_state(context.group_id, context.user_id, _STATE_KEY)
    if state is None:
        return
    days = _state_days(state)
    if days is None:
        logger.warning(
            f"“复得”状态缺少有效的天数记录（群 {context.group_id}，"
            f"成员 {context.user_id}），本次不触发效果"
        )
        return
    used_today = _used_today(state)
    if used_today >= _DAILY_LIMIT:
        return
    await _trigger(context, days, used_today)


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="复得之轮",
        quality=Quality.GOLD,
        kind=ItemKind.COMPOSITE,
        description="绝对的倒转，由此而生的数值，教会你爱的是……",
        effect=(
            f'使用指令"/item.use {_ITEM_ID} <n>"获得持续n天的“复得”状态：'
            "每天前7次使用非黑色诅咒的非复制道具时，每次都有"
            "(1/2-arctan(7ln((n-1)/14))/π)的概率获得一个随机道具"
            "（概率与签到相同），否则获得一个随机黑色诅咒道具。"
        ),
        condition="没有“复得”状态",
        note="每一份命运的馈赠，早已暗中标好了价格。",
        accepts_args=True,
        labels=((_STATE_KEY, "复得"),),
        behavior=ItemBehavior(
            check=_can_use,
            use=_handle_use,
        ),
    )
)

add_item_use_hook(_on_item_used)
