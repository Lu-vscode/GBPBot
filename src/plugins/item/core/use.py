"""道具使用流程与相关钩子。

run_item_use 执行一次完整的使用：使用条件校验 → 消耗（默认扣一件库存，
耐久型等道具自定义）→ 道具效果 → 使用后钩子。黑色诅咒的获得流程
（acquire_curse）先发送完整信息，再询问诅咒拦截器，未被拦截时自动使用。
"""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from nonebot import logger

from src.plugins._shared.onebot import send_group_forward
from src.plugins.item.core.context import ItemUseContext
from src.plugins.item.core.definition import ItemDefinition, Quality, quality_label
from src.plugins.item.core.inventory import remove_item

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot


async def run_item_use(context: ItemUseContext, *, consume: bool = True) -> bool:
    """执行一次道具使用：使用条件校验（未通过时不消耗也不执行效果）、
    可选地消耗道具（默认为消耗一件库存，道具模块可自定义消耗方式）、
    调用道具模块的使用效果。

    返回是否成功执行（使用条件未通过或消耗失败时返回 False，失败文案
    已由本函数发送）。供道具模块以其他成员的身份复用 /item.use 的完整
    使用流程（如「夺心魄」代替目标成员使用其道具）。

    道具效果执行成功后依次调用使用后钩子（见 add_item_use_hook），
    供「复得之轮」等道具在成员使用其它道具后附加效果。
    """
    behavior = context.item.behavior
    if behavior is not None and behavior.check is not None:
        error = await behavior.check(context)
        if error:
            await context.send(error)
            return False
    if consume:
        if behavior is not None and behavior.consume is not None:
            consumed = await behavior.consume(context)
        else:
            consumed = await remove_item(
                context.group_id, context.user_id, context.item.item_id
            )
        if not consumed:
            # 调用方应在群锁内调用本函数；走到这里说明库存数据与校验结果
            # 不一致（如被外部改写），按失败处理并提示
            logger.error(
                f"道具 {context.item.label} 消耗失败（群 {context.group_id}，"
                f"成员 {context.user_id}）：库存或状态不一致"
            )
            await context.send("道具使用失败：道具状态异常，本次未消耗。")
            return False
    if behavior is not None and behavior.use is not None:
        await behavior.use(context)
    await run_item_use_hooks(context)
    return True


# 道具使用后的附加效果钩子：在道具效果成功执行后按注册顺序调用（如
# 「复得之轮」在成员使用其它道具后按概率发放道具）
ItemUseHook = Callable[["ItemUseContext"], Awaitable[None]]

# 已注册的使用后钩子（按注册顺序）
_item_use_hooks: list[ItemUseHook] = []

# 钩子处理异常（单个钩子出错时不影响本次使用结果与其它钩子）
_ITEM_USE_HOOK_ERRORS = (Exception,)


def add_item_use_hook(hook: ItemUseHook) -> None:
    """注册道具使用后的附加效果钩子（同一钩子重复注册会被忽略）。

    钩子在道具自身的校验、消耗与效果全部成功后按注册顺序调用；拿到
    的是本次使用的完整上下文（道具、使用者、所在群与参数）。钩子自行
    判断是否生效（如按使用者身上的状态、道具品质或编号），抛出的异常
    只记录错误日志，不影响道具模块已发送的消息与其它钩子。
    """
    if hook not in _item_use_hooks:
        _item_use_hooks.append(hook)


async def run_item_use_hooks(context: ItemUseContext) -> None:
    """依次调用道具使用后的附加效果钩子（由 run_item_use 调用）。"""
    for hook in _item_use_hooks:
        try:
            await hook(context)
        except _ITEM_USE_HOOK_ERRORS as exc:
            logger.error(
                f"道具使用后钩子执行出错（{context.item.label}，"
                f"群 {context.group_id}，成员 {context.user_id}）：{exc}"
            )


# 黑色诅咒自动使用拦截器：在诅咒的详细信息消息发送后、效果执行前
# 依次调用；返回 True 表示本次自动使用被拦截（诅咒失去、效果不执行）
CurseInterceptor = Callable[["ItemUseContext"], Awaitable[bool]]

# 已注册的拦截器（按注册顺序）
_curse_interceptors: list[CurseInterceptor] = []

# 拦截器处理异常（单个拦截器出错时视为未拦截，不影响诅咒执行）
_CURSE_INTERCEPTOR_ERRORS = (Exception,)


def add_curse_interceptor(interceptor: CurseInterceptor) -> None:
    """注册黑色诅咒自动使用拦截器（同一拦截器重复注册会被忽略）。

    黑色诅咒道具获得后自动使用前（详细信息消息已发送）按注册顺序
    询问拦截器：第一个返回 True 的拦截器抵消本次自动使用（诅咒道具
    失去、诅咒效果不执行），其余拦截器不再被调用；全部返回 False 时
    按正常流程自动使用。拦截器可在此时消耗自己的状态并发送提示消息；
    单个拦截器抛出的异常只记录错误日志，视为未拦截。
    """
    if interceptor not in _curse_interceptors:
        _curse_interceptors.append(interceptor)


async def run_curse_interceptors(context: ItemUseContext) -> bool:
    """依次询问黑色诅咒自动使用拦截器，返回本次自动使用是否被拦截。

    供诅咒获得流程（见 acquire_curse）在诅咒的详细信息消息发送后、
    执行诅咒效果前调用。
    """
    for interceptor in _curse_interceptors:
        try:
            if await interceptor(context):
                return True
        except _CURSE_INTERCEPTOR_ERRORS as exc:
            logger.error(
                f"黑色诅咒拦截器执行出错（{context.item.label}，"
                f"群 {context.group_id}，成员 {context.user_id}）：{exc}"
            )
    return False


async def detail_text(definition: ItemDefinition, group_id: int, user_id: int) -> str:
    """生成道具的完整信息文本（用于详情与黑色诅咒获得消息）。

    耐久条目默认使用定义中的静态文本；行为块提供 durability_text 钩子
    时改用其动态结果（按查看者所在群与 QQ 号计算）。使用条件与使用
    时机未声明时分别显示“无”与“任意”。
    """
    durability = definition.durability
    behavior = definition.behavior
    if behavior is not None and behavior.durability_text is not None:
        durability = await behavior.durability_text(
            group_id, user_id, definition.item_id
        )
    lines = [
        f"编号：{definition.item_id}",
        f"名称：{definition.name}",
        f"品质：{quality_label(definition.quality)}",
        f"类型：{definition.resolved_kind.value}",
        f"介绍：{definition.description}",
        f"功能：{definition.effect}",
        f"使用条件：{definition.condition or '无'}",
        f"使用时机：{definition.timing or '任意'}",
        f"耐久：{durability}",
    ]
    if definition.note:
        lines.append(f"备注：{definition.note}")
    return "\n".join(lines)


async def acquire_curse(
    bot: "Bot",
    group_id: int,
    user_id: int,
    user_name: str,
    definition: ItemDefinition,
) -> None:
    """让成员获得一件黑色诅咒并完成完整的获得流程。

    诅咒不写入库存：先以合并转发发送完整信息（节点名"道具详情"），
    再询问黑色诅咒拦截器（如「坚如磐石」的护盾）；未被拦截时按诅咒
    自身的 use 钩子自动使用（不消耗库存）。

    签到、合成与道具效果转赠诅咒统一经本函数处理，保证信息播报、
    拦截与自动使用行为一致。definition 不是黑色诅咒时抛出 ValueError
    （开发错误，正常流程不会出现）。
    """
    if definition.quality is not Quality.BLACK_CURSE:
        message = f"道具 {definition.label} 不是黑色诅咒，不能按诅咒获得流程处理"
        raise ValueError(message)
    await send_group_forward(
        bot,
        group_id,
        [
            f"你获得了黑色诅咒道具：{definition.label}！\n"
            f"{await detail_text(definition, group_id, user_id)}"
        ],
        node_name="道具详情",
        label="诅咒道具信息",
    )
    context = ItemUseContext(
        bot=bot,
        group_id=group_id,
        user_id=user_id,
        user_name=user_name,
        item=definition,
        args=(),
        reply_to=None,
    )
    if await run_curse_interceptors(context):
        return
    await run_item_use(context, consume=False)
