"""道具「补签卡」（编号 996）：白色普通、可选型。

使用 /item.use 996 YYYY-MM-DD 补上指定日期的签到并获得签到奖励；补签
日期不能早于 2026-10-14、不能晚于今天，已签到的日期不能补签：

- 签到记录与奖励由签到插件完成（见 _shared/services/ 的
  SignMakeUpService）：决斗分数按签到当刻的基础分数与道具加成计算，
  随机道具与签到共用同一套抽取与登记流程，本道具不复制这些判定；
- 使用条件校验在消耗前查询该日期是否已有签到记录，已签到时本次不
  消耗；使用阶段写入失败（记录在查询与写入之间被改写）时返还补签卡；
- 补签获得的黑色诅咒不在库存内，先发送本道具的文案，再经道具服务的
  获得流程播报详情并自动使用。
"""

import re
from datetime import UTC, date, datetime

from nonebot import logger

from src.plugins._shared.services import (
    GrantedItem,
    ItemService,
    SignMakeUpService,
    get_service,
)
from src.plugins.item.core import (
    ItemBehavior,
    ItemDefinition,
    ItemKind,
    ItemUseContext,
    Quality,
    add_item,
    register_item,
)

# 道具编号、补签日期的最早日期与参数格式（严格 YYYY-MM-DD）
_ITEM_ID = "996"
_EARLIEST_DATE = date(2026, 10, 14)
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


def _today() -> date:
    """返回当前本地时区的日期。"""
    return datetime.now(UTC).astimezone().date()


def _parse_date(text: str) -> date | None:
    """把参数解析为补签日期，格式不合法（非 YYYY-MM-DD）时返回 None。"""
    if not _DATE_PATTERN.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _usage(context: ItemUseContext) -> str:
    """返回使用指令的用法提示。"""
    return f"请提供补签日期，用法：/item.use {context.item.item_id} <YYYY-MM-DD>"


def _checked_date(context: ItemUseContext) -> tuple[date | None, str | None]:
    """校验并解析补签日期参数，返回 (补签日期, 错误文案)。

    参数个数、格式与允许区间（不早于最早补签日期、不晚于今天）都在
    这里校验；任一项不满足时日期为 None，错误文案可直接发送给成员。
    """
    if len(context.args) != 1:
        return None, _usage(context)
    target = _parse_date(context.args[0])
    if target is None:
        return None, f"补签日期格式不正确，{_usage(context)}"
    today = _today()
    if target > today:
        return None, f"补签日期不能晚于今天（{today.isoformat()}）。"
    if target < _EARLIEST_DATE:
        return None, f"补签日期不能早于 {_EARLIEST_DATE.isoformat()}。"
    return target, None


async def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：日期参数合法且在允许区间内，且该日期尚未签到。

    返回错误文案时本次不消耗补签卡（已签到的日期不能补签，也无需
    为此消耗道具）。
    """
    target, error = _checked_date(context)
    if target is None:
        return error
    service = get_service(SignMakeUpService)
    if service is None:
        return "补签服务不可用（签到插件未加载），本次未消耗。"
    if await service.has_record(context.group_id, context.user_id, target.isoformat()):
        return f"你在 {target.isoformat()} 已经签到过了，无需补签。"
    return None


async def _refund(context: ItemUseContext, reason: str) -> None:
    """返还本次已消耗的补签卡并提示补签失败。

    使用阶段的失败分支理论上不可达（校验已在消耗前完成），走到这里
    说明签到记录在校验与写入之间被改写；返还道具以免成员白白损失。
    """
    await add_item(context.group_id, context.user_id, _ITEM_ID)
    logger.warning(
        f"补签失败并返还补签卡（群 {context.group_id}，"
        f"成员 {context.user_id}）：{reason}"
    )
    await context.send(f"补签失败：{reason}，补签卡已返还。")


async def _handle_granted_item(context: ItemUseContext, granted: GrantedItem) -> None:
    """按道具获得流程处理补签获得的道具（黑色诅咒的播报与自动使用）。

    普通道具已由签到插件写入库存，这里只处理黑色诅咒；须在本道具的
    文案发送之后调用，保证消息顺序为补签文案 → 诅咒详情。
    """
    service = get_service(ItemService)
    if service is None:  # 不可达：主模块在插件加载期注册道具服务
        logger.error(
            f"补签获得的道具 {granted.item_id} 无法自动处理"
            f"（群 {context.group_id}，成员 {context.user_id}）：道具服务未注册"
        )
        return
    await service.handle_acquisition(
        context.bot, context.group_id, context.user_id, context.user_name, granted
    )


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：补上指定日期的签到，播报签到奖励并处理诅咒获得。"""
    target, _error = _checked_date(context)
    if target is None:  # 不可达：can_use 已校验参数
        return
    date_text = target.isoformat()
    service = get_service(SignMakeUpService)
    if service is None:  # 不可达：can_use 已校验服务可用
        await _refund(context, "补签服务不可用")
        return
    result = await service.make_up(
        context.group_id, context.user_id, context.user_name, date_text
    )
    if not result.signed:
        await _refund(context, f"{date_text} 已有签到记录")
        return
    if result.granted is None:
        gained_text = "道具抽取失败"
    else:
        gained_text = f"获得道具 {result.granted.item_id} {result.granted.item_name}"
    await context.send(
        f"{context.user_name} 使用 {context.item.label} 补上了 {date_text} 的签到："
        f"决斗分数+{result.score_gain}，{gained_text}。"
    )
    if result.granted is not None:
        await _handle_granted_item(context, result.granted)


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="补签卡",
        quality=Quality.WHITE,
        kind=ItemKind.OPTIONAL,
        description="补签一次",
        effect=(
            f'使用"/item.use {_ITEM_ID} YYYY-MM-DD"补上当天的签到并获得签到奖励，'
            f"补签日期不能早于{_EARLIEST_DATE.isoformat()}，不能晚于今天，"
            "已签到的日期不能补签。"
        ),
        note="停服补偿的常规赠品",
        accepts_args=True,
        behavior=ItemBehavior(
            check=_can_use,
            use=_handle_use,
        ),
    )
)
