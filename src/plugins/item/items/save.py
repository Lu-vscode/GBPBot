"""道具「储蓄罐」（编号 184）：白色普通、Buff型。

使用后获得持续 1 天的“储蓄”状态（储蓄分数初始为 0，登记在状态附加数据
中）：决斗获胜结算完成后储蓄分数增加该场决斗的点数；决斗平局不变化；
决斗失败结算完成后决斗分数减少“储蓄分数”并失去“储蓄”状态。使用
/item.184.draw 取出储蓄：决斗分数增加“储蓄分数”并失去“储蓄”状态。

决斗胜平负与点数按 settled 事件中的最终结算结果判定（含其它道具改写后
的胜负与点数）；结算被道具接管（事件的 claimed）时默认的分数结算没有
执行，本道具不介入，状态与储蓄分数保持不变。状态到期后静默消失，不改变
决斗分数、也不发送提示。

本模块经决斗插件的事件服务订阅决斗生命周期事件（见 _shared/services.py）。
"""

from nonebot import get_bots, logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.rule import is_type

from src.plugins._shared.config import NICKNAME_MAX_LENGTH
from src.plugins._shared.onebot import send_group_text, sender_display_name
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
    get_state,
    get_user_score,
    register_item,
    remove_state,
    set_state_data,
)

# 道具编号、状态键与状态持续时长（秒）：1 天
_ITEM_ID = "184"
_STATE_KEY = f"{_ITEM_ID}.save"
_DURATION_SECONDS = 86400.0

# 状态附加数据中的储蓄分数字段
_SCORE_KEY = "score"

# 消息中群昵称的最大显示长度（字符数，共享配置 NICKNAME_MAX_LENGTH）
_NICKNAME_MAX_LENGTH = NICKNAME_MAX_LENGTH

# 决斗事件服务（由决斗插件提供；_framework 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_event_service = require_service(DuelEventService)


def _savings_of(state: ItemState | None) -> int:
    """返回状态的储蓄分数（状态不存在或数据异常时视为 0）。"""
    if state is None:
        return 0
    value = state.data.get(_SCORE_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：获得持续 1 天的“储蓄”状态并发送提示。

    已有“储蓄”状态时只刷新持续时间，储蓄分数保持不变（状态过期后
    由定时清理静默丢弃，此时重新使用等同于首次使用）。
    """
    existing = get_state(context.group_id, context.user_id, _STATE_KEY)
    savings = _savings_of(existing)
    add_state(
        context.group_id,
        context.user_id,
        state=ItemState(key=_STATE_KEY, item_id=_ITEM_ID, data={_SCORE_KEY: savings}),
        duration_seconds=_DURATION_SECONDS,
    )
    if existing is None:
        await context.send(
            f"{context.user_name} 使用了 {context.item.label}，"
            f"把储蓄罐摆在了决斗场边。\n"
            f"“储蓄”状态持续1天，储蓄分数为 {savings}。"
        )
        return
    await context.send(
        f"{context.user_name} 又往 {context.item.label} 里塞了一张纸。\n"
        f"“储蓄”状态的时间刷新为1天，储蓄分数仍为 {savings}。"
    )


def _add_savings(group_id: int, user_id: int, state: ItemState, amount: int) -> int:
    """把本场决斗的点数存入储蓄分数并落盘（不刷新状态时长），返回新储蓄分数。"""
    savings = _savings_of(state) + amount
    set_state_data(group_id, user_id, _STATE_KEY, {**state.data, _SCORE_KEY: savings})
    return savings


def _apply_score_change(group_id: int, user_id: int, name: str, delta: int) -> int:
    """为成员增减决斗分数，返回实际生效的变化量。

    变化量经分数改变拦截器链处理（如「无懈可击」可将其抵消为 0），
    文案按实际生效的变化量编写。
    """
    if delta == 0:
        return 0
    before = get_user_score(group_id, user_id)
    after = add_user_score(group_id, user_id, name, delta)
    return (after - before) if delta > 0 else (before - after)


async def _send_duel_text(event: DuelEvent, text: str) -> None:
    """向决斗所在群发送储蓄结算消息（机器人离线或发送失败仅记录日志）。"""
    bot = get_bots().get(str(event.bot_self_id))
    if not isinstance(bot, Bot):
        logger.warning(
            f"储蓄结算消息未发送：机器人 {event.bot_self_id} 不在线"
            f"（群 {event.group_id}）"
        )
        return
    await send_group_text(bot, event.group_id, text, label="道具消息")


async def _settle_winner(event: DuelEvent) -> None:
    """决斗获胜结算完成后：储蓄分数增加本场决斗的点数。"""
    if event.winner_id is None:
        # 不可达：非平局事件的胜负双方均由决斗流程填好
        return
    state = get_state(event.group_id, event.winner_id, _STATE_KEY)
    if state is None:
        return
    name = event.winner_name or f"QQ {event.winner_id}"
    savings = _add_savings(event.group_id, event.winner_id, state, event.multiplier)
    await _send_duel_text(
        event,
        f"{name} 决斗获胜，往储蓄罐里存了 {event.multiplier}，储蓄分数为 {savings}。",
    )


async def _settle_loser(event: DuelEvent) -> None:
    """决斗失败结算完成后：扣除储蓄分数并失去“储蓄”状态。"""
    if event.loser_id is None:
        # 不可达：非平局事件的胜负双方均由决斗流程填好
        return
    state = get_state(event.group_id, event.loser_id, _STATE_KEY)
    if state is None:
        return
    name = event.loser_name or f"QQ {event.loser_id}"
    savings = _savings_of(state)
    # 同步段：扣除分数并失去状态，再发送结算消息
    deducted = _apply_score_change(event.group_id, event.loser_id, name, -savings)
    remove_state(event.group_id, event.loser_id, _STATE_KEY)
    if deducted:
        detail = f"决斗分数 -{deducted}"
    elif savings:
        detail = "决斗分数没有变化"
    else:
        detail = "里面空空如也"
    await _send_duel_text(
        event, f"{name} 决斗失败，储蓄罐碎了，{detail}，失去了“储蓄”状态。"
    )


async def _on_duel_event(event: DuelEvent) -> None:
    """决斗事件处理：结算完成后按最终胜负结算双方持有的“储蓄”。

    平局不提醒、储蓄分数不变；结算被接管（claimed）时默认的分数结算
    没有执行，本次不介入；未结算（超时、取消）的决斗不发布 settled 事件，
    同样不介入。
    """
    if event.kind is not DuelEventKind.SETTLED or event.claimed or event.draw:
        return
    await _settle_winner(event)
    await _settle_loser(event)


_event_service.subscribe(_on_duel_event)

draw_cmd = on_command(f"item.{_ITEM_ID}.draw", rule=is_type(GroupMessageEvent))


@draw_cmd.handle()
async def handle_item_draw(bot: Bot, event: GroupMessageEvent) -> None:
    """处理 /item.184.draw 命令：取出储蓄分数并从决斗分数结算。

    仅限持有“储蓄”状态的成员使用，其它成员使用时静默失败。
    """
    state = get_state(event.group_id, event.user_id, _STATE_KEY)
    if state is None:
        # 不具有“储蓄”状态：静默失败
        return
    name = sender_display_name(event, _NICKNAME_MAX_LENGTH)
    savings = _savings_of(state)
    # 同步段：结算分数并移除状态
    gained = _apply_score_change(event.group_id, event.user_id, name, savings)
    remove_state(event.group_id, event.user_id, _STATE_KEY)
    if gained:
        detail = f"决斗分数 +{gained}"
    elif savings:
        detail = "决斗分数没有变化"
    else:
        detail = "里面空空如也"
    await send_group_text(
        bot,
        event.group_id,
        f"{name} 砸开了储蓄罐，{detail}，失去了“储蓄”状态。",
        event.message_id,
        label="道具消息",
    )


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="储蓄罐",
        quality=Quality.WHITE,
        types=(ItemType.BUFF,),
        description="老实决斗，很枯燥吧？",
        effect=(
            "使用后获得持续1天的“储蓄”状态：初始“储蓄分数”为0，"
            "每次决斗获胜“储蓄分数”增加该场决斗的点数，决斗平局不变化，"
            "决斗失败时决斗分数减少“储蓄分数”并失去“储蓄”状态。\n"
            f'使用"/item.{_ITEM_ID}.draw"使决斗分数增加“储蓄分数”'
            "并失去“储蓄”状态。"
        ),
        condition="无",
        timing="任意",
        note="真的是在“储蓄”吗？",
        handle_use=_handle_use,
        state_labels=((_STATE_KEY, "储蓄"),),
    )
)
