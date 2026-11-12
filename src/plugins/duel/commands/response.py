"""响应决斗邀请的指令：`/duel.accept @成员` 与 `/duel.reject @成员`。

接受前发布 accepting 事件，订阅方（道具等插件）可通过设置 block_reason
阻止本次接受（如道具的「怯战」状态），决斗保持等待对方接受的状态。

两个指令都在同步段完成“查找并标记/移除”决斗，再发送文案：发送可能因
频率限制排队，期间双方提前发送的猜拳表情也必须能按已接受（或已拒绝、
已开始）的决斗处理。被挑衅（强制接受）的决斗不能再被接受或拒绝，分别
提示已开始与不能拒绝。
"""

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.rule import is_type

from src.plugins._shared.services import DuelEventKind
from src.plugins.duel.commands.args import member_name, parse_target, sender_name
from src.plugins.duel.config import (
    PROVOKED_ALREADY_STARTED,
    PROVOKED_CANNOT_REJECT,
    USAGE_ACCEPT,
    USAGE_REJECT,
    text,
)
from src.plugins.duel.core import find_pending_duel, find_provoked_duel, remove_duel
from src.plugins.duel.messaging import send_text
from src.plugins.duel.services.events import fire_duel_event, make_duel_event


async def _not_challenged_text(bot: Bot, event: GroupMessageEvent, target: int) -> str:
    """构造“对方没有向你发起决斗”的提示文案（含对方显示名）。"""
    target_name = await member_name(bot, event.group_id, target)
    return text("not_challenged", challenger=target_name)


duel_accept_cmd = on_command("duel.accept", rule=is_type(GroupMessageEvent))


@duel_accept_cmd.handle()
async def handle_duel_accept(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel.accept 命令：接受对方的决斗邀请。"""
    target = parse_target(args)
    if target is None:
        await send_text(bot, event.group_id, USAGE_ACCEPT, reply_to=event.message_id)
        return
    # 查找对方发起、等待自己接受的决斗
    duel = find_pending_duel(event.group_id, challenger=target, opponent=event.user_id)
    if duel is None:
        provoked = find_provoked_duel(
            event.group_id, challenger=target, opponent=event.user_id
        )
        if provoked is not None:
            await send_text(
                bot,
                event.group_id,
                PROVOKED_ALREADY_STARTED.format(challenger=provoked.challenger_name),
                reply_to=event.message_id,
            )
            return
        await send_text(
            bot,
            event.group_id,
            await _not_challenged_text(bot, event, target),
            reply_to=event.message_id,
        )
        return
    # accepting 事件：订阅方可以通过设置 block_reason 阻止本次接受
    accepting_event = make_duel_event(
        duel, DuelEventKind.ACCEPTING, actor_id=event.user_id
    )
    await fire_duel_event(accepting_event)
    if accepting_event.block_reason:
        await send_text(
            bot,
            event.group_id,
            accepting_event.block_reason,
            reply_to=event.message_id,
        )
        return
    # 同步段标记接受：发送文案可能因频率限制排队，
    # 期间双方提前发送的猜拳表情也必须能按已接受的决斗记录
    duel.accepted = True
    accepter_name = sender_name(event)
    logger.info(f"群 {event.group_id} 成员 {event.user_id} 接受 {target} 的决斗邀请")
    await send_text(
        bot,
        event.group_id,
        text(
            "accepted",
            accepter=accepter_name,
            challenger=duel.challenger_name,
            multiplier=duel.multiplier,
        ),
    )
    await fire_duel_event(
        make_duel_event(duel, DuelEventKind.ACCEPTED, actor_id=event.user_id)
    )


duel_reject_cmd = on_command("duel.reject", rule=is_type(GroupMessageEvent))


@duel_reject_cmd.handle()
async def handle_duel_reject(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel.reject 命令：拒绝对方的决斗邀请。"""
    target = parse_target(args)
    if target is None:
        await send_text(bot, event.group_id, USAGE_REJECT, reply_to=event.message_id)
        return
    duel = find_pending_duel(event.group_id, challenger=target, opponent=event.user_id)
    if duel is None:
        provoked = find_provoked_duel(
            event.group_id, challenger=target, opponent=event.user_id
        )
        if provoked is not None:
            await send_text(
                bot,
                event.group_id,
                PROVOKED_CANNOT_REJECT.format(challenger=provoked.challenger_name),
                reply_to=event.message_id,
            )
            return
        await send_text(
            bot,
            event.group_id,
            await _not_challenged_text(bot, event, target),
            reply_to=event.message_id,
        )
        return
    remove_duel(duel)
    rejecter_name = sender_name(event)
    logger.info(f"群 {event.group_id} 成员 {event.user_id} 拒绝 {target} 的决斗邀请")
    await send_text(
        bot,
        event.group_id,
        text(
            "rejected",
            rejecter=rejecter_name,
            challenger=duel.challenger_name,
            multiplier=duel.multiplier,
        ),
    )
    await fire_duel_event(
        make_duel_event(duel, DuelEventKind.REJECTED, actor_id=event.user_id)
    )
