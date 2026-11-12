"""使用指令：/item.use 使用自己拥有的一件道具。"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.rule import is_type

from src.plugins.item.commands.texts import USAGE_USE
from src.plugins.item.core import (
    ItemUseContext,
    get_item,
    get_item_count,
    is_item_id,
    run_item_use,
)
from src.plugins.item.messaging import parse_use_targets, send_text, sender_name
from src.plugins.item.storage import group_lock

item_use_cmd = on_command("item.use", rule=is_type(GroupMessageEvent))


@item_use_cmd.handle()
async def handle_item_use(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.use 命令：使用自己拥有的一件道具。"""
    tokens = args.extract_plain_text().split()
    if not tokens or not is_item_id(tokens[0]):
        await send_text(bot, event.group_id, USAGE_USE, reply_to=event.message_id)
        return
    item_id = tokens[0]
    definition = get_item(item_id)
    if definition is None:
        await send_text(
            bot, event.group_id, f"道具 {item_id} 不存在。", reply_to=event.message_id
        )
        return
    use_args = tuple(tokens[1:])
    if use_args and not definition.accepts_args:
        await send_text(bot, event.group_id, USAGE_USE, reply_to=event.message_id)
        return
    # 群锁：校验拥有并消耗，避免并发指令重复使用同一件道具
    async with group_lock(event.group_id):
        if await get_item_count(event.group_id, event.user_id, item_id) < 1:
            await send_text(
                bot,
                event.group_id,
                f"你未拥有道具 {definition.label}。",
                reply_to=event.message_id,
            )
            return
        targets = parse_use_targets(args, event)
        context = ItemUseContext(
            bot=bot,
            group_id=event.group_id,
            user_id=event.user_id,
            user_name=sender_name(event),
            item=definition,
            args=use_args,
            reply_to=event.message_id,
            target_id=targets[0] if targets else None,
            target_ids=targets,
        )
        await run_item_use(context)
