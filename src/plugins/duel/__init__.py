"""猜拳决斗插件。

在群聊中发起猜拳决斗：`/duel @成员 [点数]` 发起（点数默认 1，上限由
DUEL_MAX_MULTIPLIER 配置，默认 100），被 @ 的成员可通过
`/duel.accept @成员` 接受或 `/duel.reject @成员` 拒绝；接受后双方发送
QQ"包剪锤"表情，机器人根据双方手势判定胜负：胜者分数 +点数、负者分数
-点数，平局分数不变；各群分数相互独立，分数数据使用 localstore 长期
存储在本地，并且作为"决斗分数服务"经跨插件服务注册中心（见
_shared/services.py）供其它插件增减分数与查询高/低分榜第一名；
`/duel.rank` 可查看本群分数排行榜（以合并转发发送），`/duel.score`
可查看自己在本群的决斗分数，`/duel.status` 可查看自己在本群的决斗
状态。除分数服务外，还对外提供决斗状态服务
（DuelStateService）、决斗事件服务（DuelEventService：在发起、接受、
拒绝、出拳、结算、超时、取消等环节发布事件，供道具等插件联动，
settling 事件的处理器可接管结算），机器人接受概率函数服务
（DuelBotAcceptService：供道具等插件校验并改写概率函数表达式）、
决斗挑衅服务（DuelProvokeService：供道具等插件强制对方接受决斗，
并由机器人代替对方发送猜拳表情）与决斗点数缩放服务
（DuelMultiplierService：供道具等插件缩放进行中决斗的点数，缩放
到 0 时取消决斗并发布取消事件）；
机器人名单（BOT_LIST）由 _shared/config.py
的共享配置提供，供各插件共享；群聊消息发送、群昵称获取与截断等通用
逻辑复用 _shared/onebot.py 的共享工具。

决斗发起全局限频：全部群聊合计每小时最多发起 DUEL_HOURLY_LIMIT 次
决斗（默认 20，见 _limit.py）；@ 机器人自己时机器人按接受概率函数
（DUEL_BOT_ACCEPT_FUNC，默认 1/点数）掷骰决定是否接受决斗，接受后
发送猜拳表情；决斗状态保存在内存中，存在时间超过 DUEL_DURATION
（分钟，默认 10）后自动超时结束；超时提示保留 1 分钟，仅在机器人于
该群发言后延迟 1 秒跟随发出，未能跟随则静默丢弃。

本插件为包结构（由原单文件 duel.py 拆分而来）：配置与文案见
_config.py，手势解析见 _gestures.py，分数存储与分数服务见
_scores.py，决斗状态与状态服务见 _state.py，事件分发与事件服务见
_events.py，消息发送与超时提示延迟发送见 _messages.py，结算与机器人
出手见 _flow.py，对外服务实现见 _services.py，发起限频见 _limit.py。
"""

import math
import random
import time

from nonebot import get_bots, logger, on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import is_type
from nonebot_plugin_apscheduler import scheduler

from src.plugins._shared.config import get_bot_list
from src.plugins._shared.onebot import (
    member_display_name,
    sender_display_name,
    truncate_name,
)
from src.plugins._shared.services import (
    DuelBotAcceptService,
    DuelEvent,
    DuelEventKind,
    DuelEventService,
    DuelMultiplierService,
    DuelProvokeService,
    DuelScoreService,
    DuelStateService,
    register_service,
)
from src.plugins.duel._config import (
    BOT_ACCEPT_PROBABILITY,
    DURATION_MINUTES,
    DURATION_SECONDS,
    HOURLY_LIMIT,
    LIMIT_REACHED,
    MAX_MULTIPLIER,
    NICKNAME_MAX_LENGTH,
    PAIR_ACTIVE,
    PROVOKED_ALREADY_STARTED,
    PROVOKED_CANNOT_REJECT,
    RANK_EMPTY,
    RANK_NODE_NAME,
    RANK_TITLE_HIGH,
    RANK_TITLE_LOW,
    STATUS_ALL_DENIED,
    USAGE_ACCEPT,
    USAGE_DUEL,
    USAGE_RANK,
    USAGE_REJECT,
    USAGE_SCORE,
    USAGE_STATUS,
    text,
)
from src.plugins.duel._events import event_service, fire_duel_event, make_duel_event
from src.plugins.duel._flow import resolve_duel, send_bot_rps
from src.plugins.duel._gestures import extract_gesture, is_rps_message
from src.plugins.duel._limit import (
    limit_reached,
    record_initiation,
    retry_after_seconds,
)
from src.plugins.duel._messages import (
    defer_timeout,
    drop_expired_timeouts,
    send_forward,
    send_text,
)
from src.plugins.duel._scores import rank_entries, score_service
from src.plugins.duel._services import (
    bot_accept_service,
    multiplier_service,
    provoke_service,
)
from src.plugins.duel._state import (
    Duel,
    duel_ready,
    duels,
    find_pair_duel,
    find_pending_duel,
    find_provoked_duel,
    remove_duel,
    state_service,
    take_gesture,
)

__plugin_meta__ = PluginMetadata(
    name="猜拳决斗",
    description="在群聊中发起猜拳决斗，由机器人判定胜负并按群记录分数，支持分数排行榜与分数查询",
    usage=(
        "/duel @成员 [点数]：向群成员发起决斗（点数默认为 1）\n"
        "/duel.accept @成员：接受对方的决斗邀请\n"
        "/duel.reject @成员：拒绝对方的决斗邀请\n"
        "/duel.rank (high|h (<条数>)) (low|l (<条数>))：查看本群分数排行榜"
        "（合并转发发送）\n"
        "/duel.score：查看自己在本群的决斗分数\n"
        "/duel.status：查看自己在本群的决斗状态"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 排行榜的默认最大显示条数
_DEFAULT_RANK_LIMIT = 10

# 决斗超时的检查间隔（秒）：超时提示最多延迟该间隔
_TIMEOUT_CHECK_INTERVAL_SECONDS = 30

# 机器人名单（共享配置 BOT_LIST，见 _shared/config.py）：
# 不参与决斗（挑战时提示不能向 BOT 发起）的机器人 QQ 号，所有群通用
_bot_list = get_bot_list()

# 将决斗能力注册为跨插件服务（契约见 _shared/services.py），
# 供签到、道具等插件使用
register_service(DuelScoreService, score_service)
register_service(DuelStateService, state_service)
register_service(DuelEventService, event_service)
register_service(DuelBotAcceptService, bot_accept_service)
register_service(DuelProvokeService, provoke_service)
register_service(DuelMultiplierService, multiplier_service)


def _truncate_name(name: str) -> str:
    """按配置上限截断群昵称（共享工具对接层）。"""
    return truncate_name(name, NICKNAME_MAX_LENGTH)


def _sender_display_name(event: GroupMessageEvent) -> str:
    """返回发送者在群内的显示名（群昵称优先，过长时截断）。"""
    return sender_display_name(event, NICKNAME_MAX_LENGTH)


async def _member_display_name(bot: Bot, group_id: int, user_id: int) -> str:
    """获取群成员的显示名（群昵称优先，过长时截断）。"""
    return await member_display_name(bot, group_id, user_id, NICKNAME_MAX_LENGTH)


def _parse_multiplier(args: Message) -> int | None:
    """从命令参数中解析决斗点数，未提供时返回 1，无效时返回 None。"""
    tokens = args.extract_plain_text().split()
    if not tokens:
        return 1
    if len(tokens) > 1 or not tokens[0].isdigit():
        return None
    value = int(tokens[0])
    return value if 1 <= value <= MAX_MULTIPLIER else None


def _parse_target(args: Message) -> int | None:
    """从命令参数中解析被 @ 的成员 QQ 号，无有效 @ 段时返回 None。"""
    for segment in args:
        if segment.type != "at":
            continue
        qq = str(segment.data.get("qq", ""))
        if qq.isdigit():
            return int(qq)
    return None


def _parse_duel_target(args: Message, event: GroupMessageEvent) -> int | None:
    """解析 /duel 的被 @ 对象，消息末尾 @机器人 被适配器剥离时返回机器人自己。

    OneBot v11 适配器会把消息末尾的 @机器人（其后可跟一个纯空白文本段）
    当作呼叫机器人处理并连同尾随空白一起删除（见适配器 _check_at_me），
    此时参数中已找不到该 @ 段，需要根据剥离前的原始消息判断：若原始消息
    末尾正是 @机器人（可跟一个纯空白文本段），则发起对象为机器人自己。
    """
    target = _parse_target(args)
    if target is not None:
        return target
    if not event.to_me:
        return None
    original = event.original_message
    if not original:
        return None
    last = original[-1]
    if (
        last.type == "text"
        and not str(last.data.get("text", "")).strip()
        and len(original) > 1
    ):
        # 与适配器一致：末尾是纯空白文本段时向前看一段
        last = original[-2]
    if last.type == "at" and str(last.data.get("qq", "")) == str(event.self_id):
        return int(event.self_id)
    return None


def _target_error(target: int, user_id: int) -> str | None:
    """校验决斗的被 @ 对象是否合法（不能是自己或名单机器人），
    返回错误文案或 None（合法）。"""
    if target == user_id:
        return text("self")
    if target in _bot_list:
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
    target = _parse_duel_target(args, event)
    multiplier = _parse_multiplier(args)
    if target is None or multiplier is None:
        await send_text(bot, event.group_id, USAGE_DUEL, reply_to=event.message_id)
        return
    error = _target_error(target, event.user_id)
    if error is not None:
        await send_text(bot, event.group_id, error, reply_to=event.message_id)
        return

    challenger_name = _sender_display_name(event)
    opponent_name = await _member_display_name(bot, event.group_id, target)
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
    duels.append(duel)
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


duel_accept_cmd = on_command("duel.accept", rule=is_type(GroupMessageEvent))


@duel_accept_cmd.handle()
async def handle_duel_accept(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel.accept 命令：接受对方的决斗邀请。"""
    target = _parse_target(args)
    if target is None:
        await send_text(bot, event.group_id, USAGE_ACCEPT, reply_to=event.message_id)
        return
    # 同步段查找并标记接受：发送文案可能因频率限制排队，
    # 期间双方提前发送的猜拳表情也必须能按已接受的决斗记录
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
        target_name = await _member_display_name(bot, event.group_id, target)
        await send_text(
            bot,
            event.group_id,
            text("not_challenged", challenger=target_name),
            reply_to=event.message_id,
        )
        return
    duel.accepted = True
    accepter_name = _sender_display_name(event)
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
    target = _parse_target(args)
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
        target_name = await _member_display_name(bot, event.group_id, target)
        await send_text(
            bot,
            event.group_id,
            text("not_challenged", challenger=target_name),
            reply_to=event.message_id,
        )
        return
    remove_duel(duel)
    rejecter_name = _sender_display_name(event)
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


def _parse_rank_args(tokens: list[str]) -> tuple[bool, int, bool, int] | None:
    """解析排行榜参数，返回 (显示高分榜, 高分榜条数, 显示低分榜, 低分榜条数)。

    参数无法识别或条数不是正整数时返回 None。
    """
    show_high = False
    high_limit = _DEFAULT_RANK_LIMIT
    show_low = False
    low_limit = _DEFAULT_RANK_LIMIT
    index = 0
    while index < len(tokens):
        token = tokens[index].lower()
        if token in {"high", "h"}:
            show_high = True
            if index + 1 < len(tokens) and tokens[index + 1].isdigit():
                high_limit = int(tokens[index + 1])
                index += 1
        elif token in {"low", "l"}:
            show_low = True
            if index + 1 < len(tokens) and tokens[index + 1].isdigit():
                low_limit = int(tokens[index + 1])
                index += 1
        else:
            return None
        index += 1
    if high_limit < 1 or low_limit < 1:
        return None
    return show_high, high_limit, show_low, low_limit


def _rank_section(title: str, entries: list[tuple[int, str, int]], limit: int) -> str:
    """生成一个排行榜的文本：标题与最多 limit 条条目。"""
    lines = [f"{title}（前 {limit} 名）"]
    visible = entries[:limit]
    if not visible:
        lines.append(RANK_EMPTY)
        return "\n".join(lines)
    lines.extend(
        f"{index}. {name} {score} 分"
        for index, (_, name, score) in enumerate(visible, start=1)
    )
    return "\n".join(lines)


duel_rank_cmd = on_command("duel.rank", rule=is_type(GroupMessageEvent))


@duel_rank_cmd.handle()
async def handle_duel_rank(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel.rank 命令：查看本群分数排行榜。"""
    parsed = _parse_rank_args(args.extract_plain_text().lower().split())
    if parsed is None:
        await send_text(bot, event.group_id, USAGE_RANK, reply_to=event.message_id)
        return
    show_high, high_limit, show_low, low_limit = parsed
    if not show_high and not show_low:
        # 未指定榜单时默认同时显示高分榜与低分榜
        show_high = True
        show_low = True
    sections: list[str] = []
    if show_high:
        sections.append(
            _rank_section(
                RANK_TITLE_HIGH,
                rank_entries(event.group_id, positive=True),
                high_limit,
            )
        )
    if show_low:
        sections.append(
            _rank_section(
                RANK_TITLE_LOW,
                rank_entries(event.group_id, positive=False),
                low_limit,
            )
        )
    await send_forward(
        bot, event.group_id, sections, node_name=RANK_NODE_NAME, label="决斗排行榜"
    )


duel_score_cmd = on_command("duel.score", rule=is_type(GroupMessageEvent))


@duel_score_cmd.handle()
async def handle_duel_score(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel.score 命令：查看自己在本群的决斗分数（无记录时为 0）。"""
    if args.extract_plain_text().split():
        await send_text(bot, event.group_id, USAGE_SCORE, reply_to=event.message_id)
        return
    score = score_service.get_score(event.group_id, event.user_id)
    name = _sender_display_name(event)
    await send_text(
        bot,
        event.group_id,
        f"{name} 的决斗分数为 {score} 分。",
        reply_to=event.message_id,
    )


def _remaining_text(duel: Duel, now: float) -> str:
    """返回决斗的剩余超时描述，如"距超时约 10 分钟"。"""
    remaining = max(0, math.ceil((duel.created_at + DURATION_SECONDS - now) / 60))
    return f"距超时约 {remaining} 分钟"


def _status_lines(group_id: int, user_id: int) -> list[str]:
    """生成成员在本群进行中决斗的状态描述行（按创建顺序）。"""
    now = time.monotonic()
    lines: list[str] = []
    for duel in duels:
        if duel.group_id != group_id:
            continue
        if user_id not in (duel.challenger_id, duel.opponent_id):
            continue
        suffix = _remaining_text(duel, now)
        if not duel.accepted:
            if user_id == duel.challenger_id:
                lines.append(
                    f"你向 {duel.opponent_name} 发起的决斗"
                    f"（×{duel.multiplier}）：等待对方接受，{suffix}。"
                )
            else:
                lines.append(
                    f"{duel.challenger_name} 向你发起的决斗"
                    f"（×{duel.multiplier}）：等待你接受"
                    f"（可使用 /duel.accept 接受），{suffix}。"
                )
            continue
        other_name = (
            duel.opponent_name
            if user_id == duel.challenger_id
            else duel.challenger_name
        )
        if duel.provoked_by_bot and user_id == duel.opponent_id:
            # 被挑衅的一方：手势由机器人代替，自己发送的猜拳表情无效
            if user_id not in duel.gestures:
                state = "你的猜拳表情将由 Bot 代替发送"
            else:
                state = "Bot 已代替你发送猜拳表情，等待对方发送猜拳表情"
        elif user_id not in duel.gestures:
            state = "等待你发送猜拳表情"
        else:
            state = "你已出拳，等待对方发送猜拳表情"
        lines.append(
            f"你与 {other_name} 的决斗（×{duel.multiplier}）：{state}，{suffix}。"
        )
    return lines


def _all_status_lines(group_id: int) -> list[str]:
    """生成本群全部进行中决斗的状态描述行（按创建顺序）。"""
    now = time.monotonic()
    lines: list[str] = []
    for duel in duels:
        if duel.group_id != group_id:
            continue
        suffix = _remaining_text(duel, now)
        if not duel.accepted:
            lines.append(
                f"{duel.challenger_name} 向 {duel.opponent_name} 发起的决斗"
                f"（×{duel.multiplier}）：等待对方接受，{suffix}。"
            )
            continue
        if duel.provoked_by_bot and duel.opponent_id not in duel.gestures:
            # 被挑衅一方的手势由机器人代替（代替发送前仅短暂经过）
            state = f"等待 Bot 代替 {duel.opponent_name} 发送猜拳表情"
        else:
            players = (
                (duel.challenger_id, duel.challenger_name),
                (duel.opponent_id, duel.opponent_name),
            )
            pending_names = [name for uid, name in players if uid not in duel.gestures]
            if len(pending_names) == 1:
                state = f"等待 {pending_names[0]} 发送猜拳表情"
            else:
                # 双方都未出拳（双方均已出拳时会立即结算，不会留在列表中）
                state = "等待双方发送猜拳表情"
        lines.append(
            f"{duel.challenger_name} 与 {duel.opponent_name} 的决斗"
            f"（×{duel.multiplier}）：{state}，{suffix}。"
        )
    return lines


def _parse_status_args(tokens: list[str]) -> str | None:
    """解析 /duel.status 命令参数，返回 "self"、"all" 或 None（无法识别）。"""
    if not tokens:
        return "self"
    if len(tokens) == 1 and tokens[0] in {"all", "a"}:
        return "all"
    return None


duel_status_cmd = on_command("duel.status", rule=is_type(GroupMessageEvent))


@duel_status_cmd.handle()
async def handle_duel_status(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel.status 命令：显示自己或（超级用户）本群全部的决斗状态。"""
    scope = _parse_status_args(args.extract_plain_text().lower().split())
    if scope is None:
        await send_text(bot, event.group_id, USAGE_STATUS, reply_to=event.message_id)
        return
    if scope == "all":
        if not await SUPERUSER(bot, event):
            await send_text(
                bot, event.group_id, STATUS_ALL_DENIED, reply_to=event.message_id
            )
            return
        lines = _all_status_lines(event.group_id)
        empty_text = "本群没有进行中的决斗。"
        header = f"本群有 {len(lines)} 场进行中的决斗："
    else:
        lines = _status_lines(event.group_id, event.user_id)
        empty_text = "你在本群没有进行中的决斗。"
        header = f"你在本群有 {len(lines)} 场进行中的决斗："
    if not lines:
        await send_text(bot, event.group_id, empty_text)
        return
    numbered = [f"{index}. {line}" for index, line in enumerate(lines, start=1)]
    await send_text(bot, event.group_id, "\n".join([header, *numbered]))


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
    duel = take_gesture(event.group_id, event.user_id, gesture)
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
    if not duel_ready(duel):
        return
    if not remove_duel(duel):
        return
    await resolve_duel(bot, duel)


async def _check_duel_timeouts() -> None:
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


logger.info(
    f"猜拳决斗已启用，决斗超时时间为 {DURATION_MINUTES:g} 分钟，"
    f"群昵称最大显示长度为 {NICKNAME_MAX_LENGTH} 字符，"
    f"点数上限为 {MAX_MULTIPLIER}，"
    f"发起限频为每小时最多 {HOURLY_LIMIT} 次（全部群聊合计）"
)
if _bot_list:
    logger.info(
        f"机器人名单已配置（共享配置 BOT_LIST，所有群通用）：{sorted(_bot_list)}"
    )

scheduler.add_job(
    _check_duel_timeouts,
    "interval",
    seconds=_TIMEOUT_CHECK_INTERVAL_SECONDS,
    id="duel_check_timeouts",
    replace_existing=True,
    misfire_grace_time=30,
)
