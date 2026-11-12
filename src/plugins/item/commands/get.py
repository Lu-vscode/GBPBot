"""测试获取指令：/item.get 供测试群中的超级用户获取随机道具。"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.permission import SUPERUSER
from nonebot.rule import is_type

from src.plugins.item.config import TEST_GROUPS
from src.plugins.item.messaging import send_text, sender_name
from src.plugins.item.services import item_service

# 仅测试群中的超级用户可用（条件不符时不作响应）
item_get_cmd = on_command("item.get", rule=is_type(GroupMessageEvent))


@item_get_cmd.handle()
async def handle_item_get(bot: Bot, event: GroupMessageEvent) -> None:
    """处理 /item.get 命令：测试群中的超级用户获取一件随机道具。

    仅测试群（ITEM_TEST_GROUPS）中的超级用户可用，每次获取一件、无次数
    限制，抽取概率与签到一致；群号或权限不符时不作响应，也不干预事件
    传播。
    """
    if event.group_id not in TEST_GROUPS or not await SUPERUSER(bot, event):
        return
    name = sender_name(event)
    granted = await item_service.grant_random(event.group_id, event.user_id, name)
    if granted is None:
        await send_text(
            bot, event.group_id, "道具抽取失败。", reply_to=event.message_id
        )
        return
    await send_text(
        bot,
        event.group_id,
        f"获得道具 {granted.item_id} {granted.item_name}。",
        reply_to=event.message_id,
    )
    # 与签到一致：黑色诅咒由道具服务在获取文案之后完成播报与自动使用
    await item_service.handle_acquisition(
        bot, event.group_id, event.user_id, name, granted
    )
