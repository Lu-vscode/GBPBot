"""道具「魂迁」（编号 720）：蓝色稀有、可选型。

使用 /item.use 720 @成员 发动魂迁：使用者决斗分数非负、双方之间存在
进行中的决斗、本场决斗中没有人使用过魂迁时可用。出拳环节结束时接管
结算（跳过决斗默认的分数结算与结果播报）：使用者获胜则双方交换决斗
分数，平局或落败则双方分数不变。

接管结算的优先级高于「布衣之怒」（096）：本模块经决斗插件的结算接管
服务按蓝色稀有的品质值注册结算接管者（096 按白色普通注册），决斗流程
按优先级从高到低询问，720 先接管；同场决斗两件道具都登记时 096 的刺杀
作废（道具已消耗、不退还），其登记在 settled 事件中清理。决斗被拒绝、
超时或被取消时同样静默清理本场决斗的魂迁登记（道具已消耗、不退还）。
"""

from nonebot import get_bots, logger
from nonebot.adapters.onebot.v11 import Bot

from src.plugins._shared.onebot import send_group_text
from src.plugins._shared.services import (
    DuelEvent,
    DuelEventKind,
    DuelEventService,
    DuelSettleService,
    DuelSnapshot,
    DuelStateService,
    require_service,
)
from src.plugins.item._framework import (
    ItemDefinition,
    ItemType,
    ItemUseContext,
    Quality,
    add_item,
    add_user_score,
    get_user_score,
    register_item,
)

# 道具编号与用法提示文案
_ITEM_ID = "720"
_USAGE = f"请 @ 一位群成员，用法：/item.use {_ITEM_ID} @群成员"
_NO_DUEL = "你与该成员没有进行中的决斗。"
_ALREADY_USED = "本场决斗中已有人使用过魂迁。"
_REFUND_TEXT = "使用失败：该决斗已不在进行中，道具已退还。"

# 决斗状态、事件与结算接管服务（由决斗插件提供；_framework 已
# require("duel") 保证服务已注册；服务缺失时本道具模块加载失败并被
# 加载器忽略）
_state_service = require_service(DuelStateService)
_event_service = require_service(DuelEventService)
_settle_service = require_service(DuelSettleService)

# 等待接管结算的魂迁：(群号, 使用者 QQ 号, 对象 QQ 号)
_switches: set[tuple[int, int, int]] = set()


def _find_duel(group_id: int, user_id: int, target_id: int) -> DuelSnapshot | None:
    """查找使用者与目标成员进行中的决斗快照（不限发起方向与是否已接受）。"""
    for snapshot in _state_service.list_duels(group_id):
        if {snapshot.challenger_id, snapshot.opponent_id} == {user_id, target_id}:
            return snapshot
    return None


def _match_switch(
    group_id: int, player_a: int, player_b: int
) -> tuple[int, int, int] | None:
    """查找与指定决斗匹配的魂迁登记（参与者无序匹配）；不取出。"""
    return next(
        (
            entry
            for entry in _switches
            if entry[0] == group_id and {entry[1], entry[2]} == {player_a, player_b}
        ),
        None,
    )


def _drop_switches(group_id: int, player_a: int, player_b: int) -> None:
    """清理与指定决斗匹配的魂迁登记（决斗被拒绝、超时或取消时）。"""
    match = _match_switch(group_id, player_a, player_b)
    if match is not None:
        _switches.discard(match)


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：@ 参数、决斗进行中、分数非负与本场未使用过魂迁；
    返回错误文案时本次不消耗。"""
    target_id = context.target_id
    if context.args or target_id is None:
        return _USAGE
    if target_id == context.user_id:
        return f"不能对自己使用 {context.item.label}。"
    if _find_duel(context.group_id, context.user_id, target_id) is None:
        return _NO_DUEL
    if get_user_score(context.group_id, context.user_id) < 0:
        return f"只有决斗分数非负的成员才能使用 {context.item.label}。"
    if _match_switch(context.group_id, context.user_id, target_id) is not None:
        return _ALREADY_USED
    return None


def _target_name(snapshot: DuelSnapshot, user_id: int) -> str:
    """从决斗快照中取使用者的对手（对方成员）的群昵称。"""
    if snapshot.challenger_id == user_id:
        return snapshot.opponent_name
    return snapshot.challenger_name


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：登记魂迁（出拳环节结束时接管结算）并发送播报。"""
    target_id = context.target_id
    if target_id is None:
        # 不可达：can_use 已校验参数与目标存在
        return
    snapshot = _find_duel(context.group_id, context.user_id, target_id)
    if snapshot is None:
        # 不可达：can_use 与消耗同处同步段，决斗状态不会在期间改变
        logger.error(
            f"魂迁：消耗后未找到进行中的决斗，道具已退还"
            f"（群 {context.group_id}，成员 {context.user_id}）"
        )
        add_item(context.group_id, context.user_id, _ITEM_ID)
        await context.send(_REFUND_TEXT)
        return
    # 同步段登记，防止并发指令在同一场决斗中重复使用
    _switches.add((context.group_id, context.user_id, target_id))
    target_name = _target_name(snapshot, context.user_id)
    await context.send(
        f"{context.user_name} 听见了「魂迁」的回响。\n"
        f"出拳结束后，若 {context.user_name} 获胜，"
        f"将与 {target_name} 互换决斗分数。"
    )


def _exchange_scores(
    group_id: int, user_id: int, user_name: str, target_id: int, target_name: str
) -> tuple[int, int]:
    """交换双方的决斗分数，返回 (使用者原分数, 对象原分数)。"""
    user_score = get_user_score(group_id, user_id)
    target_score = get_user_score(group_id, target_id)
    add_user_score(group_id, target_id, target_name, user_score - target_score)
    add_user_score(group_id, user_id, user_name, target_score - user_score)
    return user_score, target_score


async def _send_duel_text(event: DuelEvent, text: str) -> None:
    """向决斗所在群发送道具结算消息（机器人离线或发送失败仅记录日志）。"""
    bot = get_bots().get(str(event.bot_self_id))
    if not isinstance(bot, Bot):
        logger.warning(
            f"魂迁结算消息未发送：机器人 {event.bot_self_id} 不在线"
            f"（群 {event.group_id}）"
        )
        return
    await send_group_text(bot, event.group_id, text, label="道具消息")


async def _settle(event: DuelEvent) -> bool:
    """结算接管：本场有魂迁登记时按最终胜负结算并返回是否接管。"""
    entry = _match_switch(event.group_id, event.challenger_id, event.opponent_id)
    if entry is None:
        return False
    _switches.discard(entry)
    _, user_id, target_id = entry
    if event.challenger_id == user_id:
        user_name = event.challenger_name
        target_name = event.opponent_name
    else:
        user_name = event.opponent_name
        target_name = event.challenger_name
    # 同步段完成分数交换，再发送结算消息
    if event.winner_id == user_id:
        user_score, target_score = _exchange_scores(
            event.group_id, user_id, user_name, target_id, target_name
        )
        text = (
            f"{user_name} 的魂迁应验，与 {target_name} 互换了魂灵。\n\n"
            f"{user_name} 决斗分数变为{target_score}，"
            f"{target_name} 决斗分数变为{user_score}。"
        )
    else:
        text = (
            f"{user_name} 的魂迁未能应验。\n\n"
            f"{user_name} 与 {target_name} 的决斗分数不变。"
        )
    logger.info(f"群 {event.group_id} 中 {user_id} 对 {target_id} 的魂迁已接管决斗结算")
    await _send_duel_text(event, text)
    return True


async def _on_duel_event(event: DuelEvent) -> None:
    """决斗事件处理：决斗结束时（结算、超时、被拒绝或被取消）
    清理魂迁登记，避免影响双方之后的新决斗。"""
    if event.kind in (
        DuelEventKind.SETTLED,
        DuelEventKind.TIMEOUT,
        DuelEventKind.REJECTED,
        DuelEventKind.CANCELED,
    ):
        _drop_switches(event.group_id, event.challenger_id, event.opponent_id)


_event_service.subscribe(_on_duel_event)
# 结算接管优先级使用品质值：720（蓝色稀有 3）高于 096（白色普通 1）
_settle_service.register_settler(Quality.BLUE.value, _settle)

register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="魂迁",
        quality=Quality.BLUE,
        types=(ItemType.OPTIONAL,),
        description="我听见了「魂迁」的回响",
        effect=(
            f'使用"/item.use {_ITEM_ID} @使用对象"发动魂迁，'
            "若决斗获胜则双方决斗分数互换，否则双方决斗分数不变。"
        ),
        condition="自身决斗分数非负，本场决斗没有人使用过720 魂迁",
        timing="决斗发起后结算前",
        note="我叫章晨则，是名律师。",
        accepts_args=True,
        can_use=_can_use,
        handle_use=_handle_use,
    )
)
