"""交换指令：发起交换（/item.exchange）与接受、拒绝交换。

交换请求的登记与过期见 exchange/queue.py；本模块负责参数校验、双方道具
复查（持有且可交易）、互换库存与结果播报。
"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.rule import is_type

from src.plugins._shared.config import get_bot_list
from src.plugins.item.core import (
    ItemDefinition,
    exchange_items,
    get_item,
    get_item_count,
    is_item_id,
    transferable_count,
)
from src.plugins.item.exchange.queue import (
    EXCHANGE_DURATION_SECONDS,
    PendingExchange,
    append_exchange,
    current_time,
    expire_exchanges,
    find_pair_exchange,
    find_pending_exchange,
    remove_exchange,
)
from src.plugins.item.messaging import (
    ITEM_ARG_COUNT,
    item_label,
    member_name,
    parse_target,
    send_text,
    sender_name,
)
from src.plugins.item.storage import group_lock

# 各指令的用法与错误提示文案（成功播报由各处自由编写，不入 env 配置）
USAGE_EXCHANGE = (
    "请 @ 群成员并提供两个道具编号，"
    "用法：/item.exchange @群成员 <自己的道具编号> <对方的道具编号>"
)
USAGE_EXCHANGE_ACCEPT = "请 @ 发起交换的群成员，用法：/item.exchange.accept @群成员"
USAGE_EXCHANGE_REJECT = "请 @ 发起交换的群成员，用法：/item.exchange.reject @群成员"
EXCHANGE_PAIR_ACTIVE = (
    "你们之间已有一笔进行中的交换，请等待其完成、被拒绝或过期后再发起。"
)

# 机器人名单（共享配置 BOT_LIST，见 _shared/config/）：不能交换的对象
_BOT_LIST = get_bot_list()


async def _start_exchange(
    bot: Bot,
    event: GroupMessageEvent,
    target: int,
    first: ItemDefinition,
    second: ItemDefinition,
) -> PendingExchange | str:
    """复查并登记一笔交换，返回交换对象或错误文案（发送由调用方完成）。

    先复查发起方道具（拥有且可交易），再获取对方显示名并复查对方道具
    （拥有且可交易）与双方之间进行中的交换；全部通过后在群锁内登记，
    避免并发指令重复发起。
    """
    if await get_item_count(event.group_id, event.user_id, first.item_id) < 1:
        return f"你未拥有道具 {first.label}。"
    if await transferable_count(event.group_id, event.user_id, first.item_id) < 1:
        return f"你的道具 {first.label} 目前无法用于交换。"
    target_name = await member_name(bot, event.group_id, target)
    if await get_item_count(event.group_id, target, second.item_id) < 1:
        return f"{target_name} 未拥有道具 {second.label}。"
    if await transferable_count(event.group_id, target, second.item_id) < 1:
        return f"{target_name} 的道具 {second.label} 目前无法用于交换。"
    if find_pair_exchange(event.group_id, event.user_id, target) is not None:
        return EXCHANGE_PAIR_ACTIVE
    exchange = PendingExchange(
        group_id=event.group_id,
        initiator_id=event.user_id,
        initiator_name=sender_name(event),
        target_id=target,
        target_name=target_name,
        initiator_item_id=first.item_id,
        target_item_id=second.item_id,
        created_at=current_time(),
    )
    append_exchange(exchange)
    return exchange


item_exchange_cmd = on_command("item.exchange", rule=is_type(GroupMessageEvent))


@item_exchange_cmd.handle()
async def handle_item_exchange(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.exchange 命令：向群成员发起道具交换。"""
    expire_exchanges(current_time())
    target = parse_target(args)
    tokens = args.extract_plain_text().split()
    if (
        target is None
        or len(tokens) != ITEM_ARG_COUNT
        or not all(is_item_id(token) for token in tokens)
    ):
        await send_text(bot, event.group_id, USAGE_EXCHANGE, reply_to=event.message_id)
        return
    item_id_a, item_id_b = tokens
    if target == event.user_id:
        await send_text(
            bot, event.group_id, "不能与自己交换道具。", reply_to=event.message_id
        )
        return
    if target == int(event.self_id) or target in _BOT_LIST:
        await send_text(
            bot, event.group_id, "不能与机器人交换道具。", reply_to=event.message_id
        )
        return
    definition_a = get_item(item_id_a)
    definition_b = get_item(item_id_b)
    if definition_a is None or definition_b is None:
        missing_id = item_id_a if definition_a is None else item_id_b
        await send_text(
            bot,
            event.group_id,
            f"道具 {missing_id} 不存在。",
            reply_to=event.message_id,
        )
        return
    # 同步段：复查双方道具与进行中的交换并登记，避免并发指令重复发起
    async with group_lock(event.group_id):
        result = await _start_exchange(bot, event, target, definition_a, definition_b)
        if isinstance(result, str):
            await send_text(bot, event.group_id, result, reply_to=event.message_id)
            return
    minutes = int(EXCHANGE_DURATION_SECONDS // 60)
    await send_text(
        bot,
        event.group_id,
        f"{result.initiator_name} 想用 {definition_a.label} 交换 "
        f"{result.target_name} 的 {definition_b.label}。"
        f"{result.target_name} 可在 {minutes} 分钟内使用 "
        f"/item.exchange.accept @{result.initiator_name} 接受，"
        f"或 /item.exchange.reject @{result.initiator_name} 拒绝。",
    )


item_exchange_accept_cmd = on_command(
    "item.exchange.accept", rule=is_type(GroupMessageEvent)
)


@item_exchange_accept_cmd.handle()
async def handle_item_exchange_accept(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.exchange.accept 命令：接受对方的道具交换。"""
    expire_exchanges(current_time())
    target = parse_target(args)
    if target is None:
        await send_text(
            bot, event.group_id, USAGE_EXCHANGE_ACCEPT, reply_to=event.message_id
        )
        return
    exchange = find_pending_exchange(
        event.group_id, initiator=target, target=event.user_id
    )
    if exchange is None:
        initiator_name = await member_name(bot, event.group_id, target)
        await send_text(
            bot,
            event.group_id,
            f"{initiator_name} 未向你发起交换。",
            reply_to=event.message_id,
        )
        return
    # 群锁：复查双方道具并完成互换，避免并发指令重复处理
    async with group_lock(event.group_id):
        if (
            await get_item_count(
                event.group_id, exchange.initiator_id, exchange.initiator_item_id
            )
            < 1
        ):
            # 发起方道具已失去：交换取消并告知
            remove_exchange(exchange)
            await send_text(
                bot,
                event.group_id,
                f"交换已取消：{exchange.initiator_name} 已不再拥有道具 "
                f"{item_label(exchange.initiator_item_id)}。",
            )
            return
        if (
            await transferable_count(
                event.group_id, exchange.initiator_id, exchange.initiator_item_id
            )
            < 1
        ):
            # 发起方道具已不可交易（如耐久耗损）：交换取消并告知
            remove_exchange(exchange)
            await send_text(
                bot,
                event.group_id,
                f"交换已取消：{exchange.initiator_name} 的道具 "
                f"{item_label(exchange.initiator_item_id)} 目前无法用于交换。",
            )
            return
        if (
            await get_item_count(event.group_id, event.user_id, exchange.target_item_id)
            < 1
        ):
            # 接受方道具已失去：保留交换（可再次尝试接受或拒绝）
            await send_text(
                bot,
                event.group_id,
                f"你已不再拥有道具 {item_label(exchange.target_item_id)}，"
                "交换无法完成。",
                reply_to=event.message_id,
            )
            return
        if (
            await transferable_count(
                event.group_id, event.user_id, exchange.target_item_id
            )
            < 1
        ):
            # 接受方道具已不可交易：保留交换（可再次尝试接受或拒绝）
            await send_text(
                bot,
                event.group_id,
                f"你的道具 {item_label(exchange.target_item_id)} 目前无法用于交换，"
                "交换无法完成。",
                reply_to=event.message_id,
            )
            return
        await exchange_items(
            event.group_id,
            exchange.initiator_id,
            exchange.initiator_item_id,
            exchange.target_id,
            exchange.target_item_id,
        )
        remove_exchange(exchange)
    await send_text(
        bot,
        event.group_id,
        f"交换完成：{exchange.initiator_name} 用 "
        f"{item_label(exchange.initiator_item_id)} 交换了 {exchange.target_name} 的 "
        f"{item_label(exchange.target_item_id)}。",
    )


item_exchange_reject_cmd = on_command(
    "item.exchange.reject", rule=is_type(GroupMessageEvent)
)


@item_exchange_reject_cmd.handle()
async def handle_item_exchange_reject(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.exchange.reject 命令：拒绝对方的道具交换。"""
    expire_exchanges(current_time())
    target = parse_target(args)
    if target is None:
        await send_text(
            bot, event.group_id, USAGE_EXCHANGE_REJECT, reply_to=event.message_id
        )
        return
    exchange = find_pending_exchange(
        event.group_id, initiator=target, target=event.user_id
    )
    if exchange is None:
        initiator_name = await member_name(bot, event.group_id, target)
        await send_text(
            bot,
            event.group_id,
            f"{initiator_name} 未向你发起交换。",
            reply_to=event.message_id,
        )
        return
    remove_exchange(exchange)
    await send_text(
        bot,
        event.group_id,
        f"{exchange.target_name} 已拒绝 {exchange.initiator_name} 的交换请求。",
    )
