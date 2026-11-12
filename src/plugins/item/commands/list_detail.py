"""查看类指令：/item.list 查看持有道具，/item.detail 查看道具详情。"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.rule import is_type

from src.plugins._shared.onebot import send_group_forward
from src.plugins.item.commands.texts import USAGE_DETAIL, USAGE_LIST
from src.plugins.item.config import LIST_FORWARD_THRESHOLD
from src.plugins.item.core import (
    detail_text,
    get_item,
    get_item_count,
    is_item_id,
    list_user_items,
    quality_label,
)
from src.plugins.item.messaging import send_text, sender_name

item_list_cmd = on_command("item.list", rule=is_type(GroupMessageEvent))


@item_list_cmd.handle()
async def handle_item_list(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.list 命令：查看自己拥有的道具（按品质由高到低）。

    清单以"<群昵称> 的道具："开头；种类数超过 LIST_FORWARD_THRESHOLD
    （配置项 ITEM_LIST_FORWARD_THRESHOLD）时整份清单以合并转发（单节点）
    发送且不引用触发消息，不超过时仍按普通文本引用触发消息回复。
    """
    if args.extract_plain_text().strip():
        await send_text(bot, event.group_id, USAGE_LIST, reply_to=event.message_id)
        return
    entries = await list_user_items(event.group_id, event.user_id)
    if not entries:
        await send_text(
            bot,
            event.group_id,
            "你还没有任何道具，签到可以获得随机道具。",
            reply_to=event.message_id,
        )
        return
    lines = [f"{sender_name(event)} 的道具："]
    lines.extend(
        f"{definition.label}【{quality_label(definition.quality)}】×{count}"
        for definition, count in entries
    )
    if len(entries) > LIST_FORWARD_THRESHOLD:
        await send_group_forward(
            bot,
            event.group_id,
            ["\n".join(lines)],
            node_name="道具清单",
            label="道具清单",
        )
        return
    await send_text(bot, event.group_id, "\n".join(lines), reply_to=event.message_id)


item_detail_cmd = on_command(
    "item.detail", rule=is_type(GroupMessageEvent), aliases={"item.info"}
)


@item_detail_cmd.handle()
async def handle_item_detail(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /item.detail 命令：查看自己拥有道具的详细信息（合并转发）。"""
    tokens = args.extract_plain_text().split()
    if len(tokens) != 1 or not is_item_id(tokens[0]):
        await send_text(bot, event.group_id, USAGE_DETAIL, reply_to=event.message_id)
        return
    item_id = tokens[0]
    definition = get_item(item_id)
    if definition is None:
        await send_text(
            bot, event.group_id, f"道具 {item_id} 不存在。", reply_to=event.message_id
        )
        return
    if await get_item_count(event.group_id, event.user_id, item_id) < 1:
        await send_text(
            bot,
            event.group_id,
            f"你未拥有道具 {definition.label}。",
            reply_to=event.message_id,
        )
        return
    await send_group_forward(
        bot,
        event.group_id,
        [await detail_text(definition, event.group_id, event.user_id)],
        node_name="道具详情",
        label="道具详情",
    )
