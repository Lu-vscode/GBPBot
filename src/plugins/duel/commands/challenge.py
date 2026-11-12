"""发起决斗指令：`/duel @成员 [点数]`。

发起流程分三段：

1. 解析参数与预检（用法错误、@ 对象的合法性、双方已有进行中决斗、本小时
   发起次数已达上限）；
2. 发布 challenge 事件，订阅方（道具等插件）可通过设置 block_reason 阻止
   本次发起；
3. 同步段复查并登记本次发起，创建决斗；被 @ 的是机器人自己时按接受概率
   函数（DUEL_BOT_ACCEPT_FUNC）掷骰决定是否接受，接受后由机器人出拳。

先保存决斗状态再发送文案：发送可能因频率限制排队，期间对方提前发送的
猜拳表情也必须能被记录。
"""

import math
import random
import time

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.rule import is_type

from src.plugins._shared.config import get_bot_list
from src.plugins._shared.services import DuelEvent, DuelEventKind
from src.plugins.duel.commands.args import member_name, parse_duel_target, sender_name
from src.plugins.duel.config import (
    BOT_ACCEPT_PROBABILITY,
    HOURLY_LIMIT,
    LIMIT_REACHED,
    MAX_MULTIPLIER,
    PAIR_ACTIVE,
    USAGE_DUEL,
    text,
)
from src.plugins.duel.core import Duel, add_duel, find_pair_duel
from src.plugins.duel.flow.botplay import send_bot_rps
from src.plugins.duel.limit import (
    limit_reached,
    record_initiation,
    retry_after_seconds,
)
from src.plugins.duel.messaging import send_text
from src.plugins.duel.services.events import fire_duel_event, make_duel_event

# 机器人名单（共享配置 BOT_LIST，见 _shared/config/）：
# 不参与决斗（挑战时提示不能向 BOT 发起）的机器人 QQ 号，所有群通用
_BOT_LIST = get_bot_list()


def _parse_multiplier(args: Message) -> int | None:
    """从命令参数中解析决斗点数，未提供时返回 1，无效时返回 None。"""
    tokens = args.extract_plain_text().split()
    if not tokens:
        return 1
    if len(tokens) > 1 or not tokens[0].isdigit():
        return None
    value = int(tokens[0])
    return value if 1 <= value <= MAX_MULTIPLIER else None


def _target_error(target: int, user_id: int) -> str | None:
    """校验决斗的被 @ 对象是否合法（不能是自己或名单机器人），
    返回错误文案或 None（合法）。"""
    if target == user_id:
        return text("self")
    if target in _BOT_LIST:
        return text("bot")
    return None


def _limit_reached_message() -> str:
    """构造发起次数达到上限的提示文案（含预计等待的分钟数）。"""
    minutes = max(1, math.ceil(retry_after_seconds() / 60))
    return LIMIT_REACHED.format(limit=HOURLY_LIMIT, minutes=minutes)


def _precheck_block_reason(event: GroupMessageEvent, target: int) -> str | None:
    """发起流程的同步段预检，返回阻断本次发起的提示文案或 None。

    双方之间已有进行中的决斗、或本小时的发起次数已达上限时返回对应
    提示文案（不消费名额、不发布发起事件）。
    """
    if find_pair_duel(event.group_id, event.user_id, target) is not None:
        return PAIR_ACTIVE
    if limit_reached():
        return _limit_reached_message()
    return None


def _start_block_reason(event: GroupMessageEvent, target: int) -> str | None:
    """发起文案前的同步段复查与登记，返回阻断文案或 None（登记成功）。

    复查双方之间是否已有进行中的决斗，并登记本次发起（全局每小时次数
    上限）；复查、登记与后续创建决斗之间没有 await，避免并发指令（如
    快速重复发送）或 challenge 事件处理器的等待期间为同一对成员创建
    出多场决斗、或突破每小时发起次数上限。
    """
    if find_pair_duel(event.group_id, event.user_id, target) is not None:
        return PAIR_ACTIVE
    if not record_initiation():
        return _limit_reached_message()
    return None


duel_cmd = on_command("duel", rule=is_type(GroupMessageEvent))


@duel_cmd.handle()
async def handle_duel(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel 命令：向群成员发起决斗（可指定点数）。"""
    target = parse_duel_target(args, event)
    multiplier = _parse_multiplier(args)
    if target is None or multiplier is None:
        await send_text(bot, event.group_id, USAGE_DUEL, reply_to=event.message_id)
        return
    error = _target_error(target, event.user_id)
    if error is not None:
        await send_text(bot, event.group_id, error, reply_to=event.message_id)
        return

    challenger_name = sender_name(event)
    opponent_name = await member_name(bot, event.group_id, target)
    bot_id = int(bot.self_id)
    # 同步段预检：双方之间已有进行中的决斗、或本小时的发起次数已达上限
    # 时直接提示（不发布发起事件、不消费名额）
    block_reason = _precheck_block_reason(event, target)
    if block_reason is not None:
        await send_text(bot, event.group_id, block_reason, reply_to=event.message_id)
        return

    # challenge 事件：订阅方可以通过设置 block_reason 阻止本次发起
    challenge_event = DuelEvent(
        kind=DuelEventKind.CHALLENGE,
        group_id=event.group_id,
        bot_self_id=bot_id,
        challenger_id=event.user_id,
        challenger_name=challenger_name,
        opponent_id=target,
        opponent_name=opponent_name,
        multiplier=multiplier,
    )
    await fire_duel_event(challenge_event)
    if challenge_event.block_reason:
        await send_text(
            bot,
            event.group_id,
            challenge_event.block_reason,
            reply_to=event.message_id,
        )
        return

    # 以下为同步段：复查双方与发起次数上限，并登记本次发起，避免并发指令
    # （如快速重复发送）或 challenge 事件处理器的等待期间为同一对成员
    # 创建出多场决斗、或突破每小时发起次数上限
    block_reason = _start_block_reason(event, target)
    if block_reason is not None:
        await send_text(bot, event.group_id, block_reason, reply_to=event.message_id)
        return
    duel = Duel(
        group_id=event.group_id,
        bot_self_id=bot_id,
        challenger_id=event.user_id,
        challenger_name=challenger_name,
        opponent_id=target,
        opponent_name=opponent_name,
        multiplier=multiplier,
        created_at=time.monotonic(),
        accepted=False,
    )
    if target == bot_id:
        # @ 的是机器人自己：按接受概率函数掷骰决定是否接受，
        # 拒绝时不登记决斗状态
        accepts = random.random() < BOT_ACCEPT_PROBABILITY(multiplier)
        if not accepts:
            logger.info(
                f"群 {event.group_id} 成员 {event.user_id}（{challenger_name}）"
                f"向机器人发起决斗（点数 {multiplier}），机器人拒绝"
            )
            await send_text(
                bot,
                event.group_id,
                text(
                    "rejected",
                    rejecter=opponent_name,
                    challenger=challenger_name,
                    multiplier=multiplier,
                ),
            )
            await fire_duel_event(
                make_duel_event(duel, DuelEventKind.REJECTED, actor_id=duel.opponent_id)
            )
            return
        duel.accepted = True
    # 先保存决斗状态再发送文案：发送可能因频率限制排队，期间对方
    # 提前发送的猜拳表情也必须能被记录
    add_duel(duel)
    logger.info(
        f"群 {event.group_id} 成员 {event.user_id}（{challenger_name}）"
        f"向 {target}（{opponent_name}）发起决斗（点数 {multiplier}）"
    )

    if duel.accepted:
        # @ 的是机器人自己：掷骰接受，并由机器人发出猜拳表情
        await send_text(
            bot,
            event.group_id,
            text(
                "accepted",
                accepter=opponent_name,
                challenger=challenger_name,
                multiplier=multiplier,
            ),
        )
        await fire_duel_event(make_duel_event(duel, DuelEventKind.CREATED))
        await fire_duel_event(
            make_duel_event(duel, DuelEventKind.ACCEPTED, actor_id=duel.opponent_id)
        )
        await send_bot_rps(bot, duel)
    else:
        await send_text(
            bot,
            event.group_id,
            text(
                "challenged",
                challenger=challenger_name,
                opponent=opponent_name,
                multiplier=multiplier,
            ),
        )
        await fire_duel_event(make_duel_event(duel, DuelEventKind.CREATED))
