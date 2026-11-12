"""合成指令：/item.craft 用两件同品质道具合成一件随机品质道具。"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.rule import is_type

from src.plugins.item.commands.texts import USAGE_CRAFT
from src.plugins.item.core import (
    Quality,
    acquire_curse,
    add_items,
    craft_error,
    draw_craft_results,
    get_item,
    is_item_id,
    remove_items,
)
from src.plugins.item.messaging import ITEM_ARG_COUNT, send_text, sender_name
from src.plugins.item.storage import group_lock

item_craft_cmd = on_command("item.craft", rule=is_type(GroupMessageEvent))


@item_craft_cmd.handle()
async def handle_item_craft(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.craft 命令：用两件同品质道具合成一件随机品质道具。"""
    tokens = args.extract_plain_text().split()
    if len(tokens) != ITEM_ARG_COUNT or not all(is_item_id(token) for token in tokens):
        await send_text(bot, event.group_id, USAGE_CRAFT, reply_to=event.message_id)
        return
    item_id_a, item_id_b = tokens
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
    # 群锁 + 单事务：复查数量、抽取结果、消耗材料并登记，避免并发指令重复消耗
    async with group_lock(event.group_id):
        error = await craft_error(
            event.group_id, event.user_id, definition_a, definition_b
        )
        if error is not None:
            await send_text(bot, event.group_id, error, reply_to=event.message_id)
            return
        drawn = draw_craft_results(definition_a.quality)
        if drawn is None:
            await send_text(
                bot,
                event.group_id,
                "合成失败：目标品质的道具池为空。",
                reply_to=event.message_id,
            )
            return
        prefix, results = drawn
        await remove_items(
            event.group_id,
            event.user_id,
            [(item_id_a, 1), (item_id_b, 1)],
        )
        await add_items(
            event.group_id,
            event.user_id,
            [
                result.item_id
                for result in results
                if result.quality != Quality.BLACK_CURSE
            ],
        )
    result_labels = "、".join(result.label for result in results)
    await send_text(
        bot,
        event.group_id,
        f"{prefix}，消耗了 {definition_a.label}、{definition_b.label}，"
        f"获得了 {result_labels}。",
    )
    user_name = sender_name(event)
    for result in results:
        if result.quality is Quality.BLACK_CURSE:
            await acquire_curse(bot, event.group_id, event.user_id, user_name, result)
