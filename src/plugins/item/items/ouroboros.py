"""道具「衔尾蛇」（编号 314）：金色传说、Buff型。

使用后获得持续 1 天的「衔尾蛇」状态：决斗胜利/失败结算完成后（含
布衣之怒等道具接管的结算），与决斗分数总榜的前一名/后一名交换分数；
自己是第一名/最后一名时胜利/失败后与最后一名/第一名交换分数（环回）。

决斗分数总榜为决斗高分榜与反转的决斗低分榜的拼接：全部非零分数的
成员按分数从高到低排列、同分按 QQ 号升序排列。交换沿环进行：向交换
方向跳过与自己同分的成员，越过榜首/榜尾时环回继续查找，找不到不同分
的成员（榜为空或其余成员全部同分）时本次不交换。0 分或没有分数记录
的成员视为处于高分榜与反转低分榜中间的特殊位置：向前的邻近成员为
正分榜末尾、向后为反转低分榜开头（对应方向没有成员时同样环回），
交换后对方的分数变为 0。

本模块经决斗插件的事件服务订阅决斗生命周期事件：在 settled 事件中
按结算后的总榜为持有状态的胜者与负者执行交换。
"""

from nonebot import get_bots, logger
from nonebot.adapters.onebot.v11 import Bot

from src.plugins._shared.onebot import send_group_text
from src.plugins._shared.services import (
    DuelEvent,
    DuelEventKind,
    DuelEventService,
    require_service,
)
from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemType,
    ItemUseContext,
    Quality,
    add_state,
    add_user_score,
    get_rank_entries,
    get_state,
    get_user_score,
    register_item,
)

# 道具编号、状态键与状态持续时长（秒）
_ITEM_ID = "314"
_STATE_KEY = f"{_ITEM_ID}.ouroboros"
_DURATION_SECONDS = 86400.0

# 决斗事件服务（由决斗插件提供；_framework 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_event_service = require_service(DuelEventService)


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：登记持续 1 天的衔尾蛇状态并发送提示。"""
    add_state(
        context.group_id,
        context.user_id,
        state=ItemState(key=_STATE_KEY, item_id=_ITEM_ID),
        duration_seconds=_DURATION_SECONDS,
    )
    await context.send(
        f"{context.user_name} 的衔尾蛇开始衔住自己的尾巴。\n\n"
        "1 天内，决斗胜利后与总榜前一名交换分数，失败后与总榜后一名交换分数。"
    )


def _find_exchange_target(
    entries: list[tuple[int, str, int]], user_id: int, *, forward: bool
) -> tuple[int, str, int] | None:
    """在总榜中沿交换方向环回查找第一个与自己不同分的成员。

    forward 为 True 时向前（分数更高的方向，胜利后使用）、False 时
    向后（分数更低的方向，失败后使用）；途中跳过与自己同分的成员，
    越过榜首/榜尾时环回。找不到不同分的成员时返回 None。
    """
    count = len(entries)
    if count == 0:
        return None
    position = next(
        (index for index, entry in enumerate(entries) if entry[0] == user_id),
        None,
    )
    if position is not None:
        own_score = entries[position][2]
        start = position - 1 if forward else position + 1
    else:
        # 不在榜上（0 分或没有分数记录）：处于高分榜与反转低分榜中间
        # 的特殊位置，向前的邻近位置为正分榜末尾、向后为反转低分榜开头
        own_score = 0
        positive_count = sum(1 for entry in entries if entry[2] > 0)
        start = positive_count - 1 if forward else positive_count
    step = -1 if forward else 1
    for offset in range(count):
        entry = entries[(start + step * offset) % count]
        if entry[2] != own_score:
            return entry
    return None


def _exchange_after_settled(
    group_id: int, user_id: int, user_name: str, *, forward: bool
) -> str | None:
    """结算完成后为持有衔尾蛇状态的成员执行一次分数交换。

    未持有状态或没有可交换的成员时返回 None；否则交换双方分数并
    返回结算文案。
    """
    if get_state(group_id, user_id, _STATE_KEY) is None:
        return None
    target = _find_exchange_target(get_rank_entries(group_id), user_id, forward=forward)
    if target is None:
        return None
    target_id, target_name, _ = target
    own_score = get_user_score(group_id, user_id)
    target_score = get_user_score(group_id, target_id)
    add_user_score(group_id, target_id, target_name, own_score - target_score)
    add_user_score(group_id, user_id, user_name, target_score - own_score)
    return (
        f"{user_name} 的衔尾蛇在排行榜中游走，"
        f"与 {target_name} 交换了决斗分数。\n\n"
        f"{user_name} 决斗分数变为{target_score}，"
        f"{target_name} 决斗分数变为{own_score}。"
    )


async def _send_duel_text(event: DuelEvent, text: str) -> None:
    """向决斗所在群发送道具结算消息（机器人离线或发送失败仅记录日志）。"""
    bot = get_bots().get(str(event.bot_self_id))
    if not isinstance(bot, Bot):
        logger.warning(
            f"衔尾蛇交换消息未发送：机器人 {event.bot_self_id} 不在线"
            f"（群 {event.group_id}）"
        )
        return
    await send_group_text(bot, event.group_id, text, label="道具消息")


async def _on_duel_event(event: DuelEvent) -> None:
    """决斗事件处理：结算完成后（含被道具接管的结算），持有衔尾蛇
    状态的胜者与负者分别与总榜前一名/后一名交换分数。"""
    if event.kind is not DuelEventKind.SETTLED or event.draw:
        return
    if event.winner_id is None or event.loser_id is None:
        return
    winner_name = event.winner_name or f"QQ {event.winner_id}"
    loser_name = event.loser_name or f"QQ {event.loser_id}"
    # 先处理胜者（向前），再处理负者（向后）：两者都持有状态时
    # 依次交换，后者基于前者交换后的榜单
    for user_id, user_name, forward in (
        (event.winner_id, winner_name, True),
        (event.loser_id, loser_name, False),
    ):
        text = _exchange_after_settled(
            event.group_id, user_id, user_name, forward=forward
        )
        if text is not None:
            await _send_duel_text(event, text)


_event_service.subscribe(_on_duel_event)

register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="衔尾蛇",
        quality=Quality.GOLD,
        types=(ItemType.BUFF,),
        description="游走在排行榜中的衔尾蛇",
        effect=(
            "使用后获得持续1天的“衔尾蛇”状态：决斗胜利/失败结算完成后，"
            "与决斗分数总榜的前一名/后一名交换分数。如果自己是第一名/最后一名，"
            "胜利/失败后与最后一名/第一名交换分数。"
        ),
        condition="无",
        timing="任意",
        note="周而复始，始而复终。",
        handle_use=_handle_use,
    )
)
