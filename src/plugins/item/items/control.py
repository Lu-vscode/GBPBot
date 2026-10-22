"""道具「夺心魄」（编号 714）：紫色史诗、可选型。

使用 /item.use 714 @成员 <道具编号> [其它参数] 代替被 @ 的成员使用其拥有
的、品质不高于蓝色稀有的道具：效果等同于被 @ 的成员自己执行了
"/item.use <道具编号> [其它参数]" 指令，消耗的是被 @ 成员的该道具。
道具编号之后的第一个 @ 成员作为被使用道具自身的 @ 参数（如 108 挑衅的
目标），其余参数按原样交给被使用道具。

不能对自己使用；缺 @、缺编号、编号无效、品质高于蓝色稀有、对方未拥有
道具、被使用道具不接受参数或被使用道具自身的使用条件不满足时本次使用
失败，不消耗本道具。
"""

from dataclasses import dataclass

from nonebot import logger

from src.plugins._shared.config import NICKNAME_MAX_LENGTH
from src.plugins._shared.onebot import member_display_name
from src.plugins.item._framework import (
    ItemDefinition,
    ItemType,
    ItemUseContext,
    Quality,
    add_item,
    get_item,
    get_item_count,
    is_item_id,
    quality_label,
    register_item,
    run_item_use,
)

# 道具编号
_ITEM_ID = "714"

# 可被驱使的道具的最高品质
_MAX_QUALITY = Quality.BLUE

# 消息中群昵称的最大显示长度（字符数，共享配置 NICKNAME_MAX_LENGTH）
_NICKNAME_MAX_LENGTH = NICKNAME_MAX_LENGTH

# 用法与错误提示文案
_USAGE = (
    "请 @ 一位群成员并提供道具编号，"
    f"用法：/item.use {_ITEM_ID} @群成员 <道具编号> [其它参数]"
)
_SELF = f"不能对自己使用 {_ITEM_ID} 夺心魄。"
_STALE_FAILED = "使用失败：道具状态异常，本次未消耗，夺心魄已退还。"


@dataclass(frozen=True)
class _InnerUse:
    """一次代替使用的解析结果（尚未获取目标成员的显示名）。"""

    user_id: int
    """被代替使用的成员 QQ 号。"""

    item: ItemDefinition
    """被使用道具的定义。"""

    args: tuple[str, ...]
    """被使用道具的其它参数。"""

    target_id: int | None
    """被使用道具自身的 @ 目标（道具编号之后的第一个 @，未 @ 时为 None）。"""


def _unusable_error(
    context: ItemUseContext,
    target_id: int,
    definition: ItemDefinition,
    use_args: tuple[str, ...],
) -> str | None:
    """校验被使用道具能否被驱使：品质上限、对方持有情况与参数要求；
    返回错误文案或 None。"""
    if definition.quality > _MAX_QUALITY:
        return (
            f"无法驱使对方使用 {definition.label}：只能驱使对方使用品质不高于"
            f"{quality_label(_MAX_QUALITY)}的道具。"
        )
    if get_item_count(context.group_id, target_id, definition.item_id) < 1:
        return f"对方未拥有道具 {definition.label}。"
    if use_args and not definition.accepts_args:
        return f"{definition.label} 不接受其它参数。"
    return None


def _parse_inner_use(context: ItemUseContext) -> _InnerUse | str:
    """解析被代替使用的成员与道具，返回解析结果或错误文案。

    同步校验 @ 参数、道具编号与使用要求；被使用道具自身的使用条件
    需要目标成员的显示名，在使用效果中以其身份复核。
    """
    target_id = context.target_id
    if target_id is None or not context.args or not is_item_id(context.args[0]):
        return _USAGE
    if target_id == context.user_id:
        return _SELF
    item_id = context.args[0]
    definition = get_item(item_id)
    if definition is None:
        return f"道具 {item_id} 不存在。"
    use_args = context.args[1:]
    error = _unusable_error(context, target_id, definition, use_args)
    if error is not None:
        return error
    return _InnerUse(
        user_id=target_id,
        item=definition,
        args=use_args,
        target_id=context.target_ids[1] if len(context.target_ids) > 1 else None,
    )


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：@ 成员与被使用道具及其参数；返回错误文案时本次不消耗。"""
    parsed = _parse_inner_use(context)
    if isinstance(parsed, str):
        return parsed
    return None


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：以被 @ 成员的身份使用其道具（消耗其该道具并按原样
    执行使用效果）。"""
    parsed = _parse_inner_use(context)
    if isinstance(parsed, str):
        # 不可达：can_use 与消耗同处同步段，道具状态不会在期间改变
        logger.error(
            f"夺心魄：消耗后的参数解析未通过（{parsed}）"
            f"（群 {context.group_id}，成员 {context.user_id}）"
        )
        add_item(context.group_id, context.user_id, _ITEM_ID)
        await context.send(_STALE_FAILED)
        return
    target_name = await member_display_name(
        context.bot, context.group_id, parsed.user_id, _NICKNAME_MAX_LENGTH
    )
    inner = ItemUseContext(
        bot=context.bot,
        group_id=context.group_id,
        user_id=parsed.user_id,
        user_name=target_name,
        item=parsed.item,
        args=parsed.args,
        reply_to=context.reply_to,
        target_id=parsed.target_id,
        target_ids=() if parsed.target_id is None else (parsed.target_id,),
    )
    if inner.item.can_use is not None:
        error = inner.item.can_use(inner)
        if error:
            # 获取显示名期间被使用道具的状态已变化：退还本道具
            logger.error(
                f"夺心魄：获取显示名后被使用道具已不可用（{error}）"
                f"（群 {context.group_id}，成员 {context.user_id} ->"
                f" {parsed.user_id}）"
            )
            add_item(context.group_id, context.user_id, _ITEM_ID)
            await context.send(
                f"{target_name} 无法使用 {inner.item.label}：{error}\n"
                f"{context.item.label} 已退还。"
            )
            return
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}，"
        f"{target_name} 的心魄被夺，身不由己地使用了 {inner.item.label}。"
    )
    if not await run_item_use(inner):
        # 不可达：can_use 与消耗同处同步段，道具状态不会在期间改变
        add_item(context.group_id, context.user_id, _ITEM_ID)
        await context.send(_STALE_FAILED)


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="夺心魄",
        quality=Quality.PURPLE,
        types=(ItemType.OPTIONAL,),
        description="我听见了「夺心魄」的回响",
        effect=(
            f'使用指令"/item.use {_ITEM_ID} @使用对象 <道具编号> [其它参数]"'
            "代替使用对象使用品质不高于蓝色稀有的道具，效果等同于使用对象"
            '使用了"/item.use <道具编号> [其它参数]"指令。'
        ),
        condition="无",
        timing="任意",
        note="我是燕知春，我即是极道王。",
        accepts_args=True,
        can_use=_can_use,
        handle_use=_handle_use,
    )
)
