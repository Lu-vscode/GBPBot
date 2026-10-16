"""道具「隔断」（编号 711）：灰色垃圾、复合型。

使用 /item.use 711 @成员（可以对自己使用）使自己与被 @ 成员获得持续
1 小时的、指向彼此的「隔断」状态。状态有效期间持有者只能向隔断对象
发起决斗，也只能被隔断对象发起决斗；发起不满足该条件时，发起流程
在 challenge 事件中被本模块阻断（设置 block_reason，由决斗插件发送
提示并取消决斗创建，见 _shared/services.py 的决斗事件服务）。

对自己使用（@自己）会使自己获得指向自己的状态：向自己发起决斗会被
决斗模块自动拒绝，因此实际效果是 1 小时内无法发起决斗、也不会被
任何人发起决斗。状态到期后静默解除：过期状态由定时清理直接删除，
不发送任何提示消息（减少 Bot 不必要的主动消息），决斗功能随之恢复。
"""

from src.plugins._shared.config import get_bot_list
from src.plugins._shared.onebot import member_display_name, truncate_name
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
    get_state,
    register_item,
)

# 道具编号、状态键与状态持续时长（秒）：1 小时
_ITEM_ID = "711"
_STATE_KEY = f"{_ITEM_ID}.partition"
_DURATION_SECONDS = 3600.0

# 消息中群昵称的最大显示长度（字符数，与主模块保持一致）
_NICKNAME_MAX_LENGTH = 12

# 用法与错误提示文案
_USAGE = f"请 @ 一位群成员，用法：/item.use {_ITEM_ID} @群成员"
_SELF_STATE = "你已处于「隔断」状态。"
_TARGET_STATE = "对方已处于「隔断」状态。"

# 决斗事件服务（由决斗插件提供；_framework 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_event_service = require_service(DuelEventService)


def _state_target(state: ItemState) -> int | None:
    """从状态附加数据中读取隔断对象的 QQ 号（缺失或无效时返回 None）。"""
    value = state.data.get("target")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _state_target_name(state: ItemState, target_id: int) -> str:
    """从状态附加数据中读取隔断对象的显示名（缺失时回退为 QQ 号，并截断）。"""
    raw = str(state.data.get("target_name") or "").strip()
    if not raw:
        return f"QQ {target_id}"
    return truncate_name(raw, _NICKNAME_MAX_LENGTH)


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：@ 一位成员（可以 @ 自己）且双方均未处于「隔断」
    状态；返回错误文案时本次不消耗。"""
    target_id = context.target_id
    if context.args or target_id is None:
        return _USAGE
    if target_id in get_bot_list() or context.bot.self_id == str(target_id):
        return f"不能对机器人使用 {context.item.label}。"
    if get_state(context.group_id, context.user_id, _STATE_KEY) is not None:
        return _SELF_STATE
    if target_id != context.user_id and (
        get_state(context.group_id, target_id, _STATE_KEY) is not None
    ):
        return _TARGET_STATE
    return None


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：先为自己与被 @ 成员登记指向彼此的「隔断」状态，再发送
    生效提示（提示经发送排队期间状态已生效，期间发起的决斗也能被正常
    阻断）。可以对自己使用：只登记指向自己的状态。"""
    target_id = context.target_id
    if target_id is None:
        # 不可达：can_use 已校验参数与目标存在
        return
    if target_id == context.user_id:
        target_name = context.user_name
    else:
        target_name = await member_display_name(
            context.bot, context.group_id, target_id, _NICKNAME_MAX_LENGTH
        )
    add_state(
        context.group_id,
        context.user_id,
        state=ItemState(
            key=_STATE_KEY,
            item_id=_ITEM_ID,
            data={"target": target_id, "target_name": target_name},
        ),
        duration_seconds=_DURATION_SECONDS,
    )
    if target_id == context.user_id:
        await context.send(
            f"{context.user_name} 使用了 {context.item.label}，"
            f"{context.user_name} 周围升起一道指向自己的「隔断」。\n"
            "1 小时内，无法向任何人发起决斗，也不会被任何人发起决斗。"
        )
        return
    add_state(
        context.group_id,
        target_id,
        state=ItemState(
            key=_STATE_KEY,
            item_id=_ITEM_ID,
            data={"target": context.user_id, "target_name": context.user_name},
        ),
        duration_seconds=_DURATION_SECONDS,
    )
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}，"
        f"{context.user_name} 与 {target_name} 之间升起一道「隔断」。\n"
        "1 小时内，两人只能与彼此决斗。"
    )


def _challenger_block_text(event: DuelEvent, state: ItemState) -> str | None:
    """发起者持有「隔断」状态时的阻断文案；状态指向本次目标时无阻断。"""
    target = _state_target(state)
    if target is None or target == event.opponent_id:
        # 无目标数据（异常情况）视为无隔断，不影响决斗
        return None
    if target == event.challenger_id:
        return "你处于指向自己的「隔断」状态，无法发起决斗。"
    return f"你处于「隔断」状态，只能向 {_state_target_name(state, target)} 发起决斗。"


def _opponent_block_text(event: DuelEvent, state: ItemState) -> str | None:
    """被挑战者持有「隔断」状态时的阻断文案；状态指向发起者时无阻断。"""
    target = _state_target(state)
    if target is None or target == event.challenger_id:
        # 无目标数据（异常情况）视为无隔断，不影响决斗
        return None
    if target == event.opponent_id:
        return f"{event.opponent_name} 处于指向自己的「隔断」状态，没人能向其发起决斗。"
    name = _state_target_name(state, target)
    return f"{event.opponent_name} 处于「隔断」状态，只能被 {name} 发起决斗。"


async def _on_duel_event(event: DuelEvent) -> None:
    """决斗事件处理：发起流程校验通过后，任一参与方的「隔断」状态与
    本次发起不匹配时阻断本次发起（设置 block_reason，决斗不会被创建；
    双方都不匹配时提示文案合并展示）。"""
    if event.kind is not DuelEventKind.CHALLENGE:
        return
    lines: list[str] = []
    challenger_state = get_state(event.group_id, event.challenger_id, _STATE_KEY)
    if challenger_state is not None:
        text = _challenger_block_text(event, challenger_state)
        if text is not None:
            lines.append(text)
    opponent_state = get_state(event.group_id, event.opponent_id, _STATE_KEY)
    if opponent_state is not None:
        text = _opponent_block_text(event, opponent_state)
        if text is not None:
            lines.append(text)
    if lines:
        event.block_reason = "\n".join(lines)


_event_service.subscribe(_on_duel_event)


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="隔断",
        quality=Quality.GRAY,
        types=(ItemType.COMPOSITE,),
        description="我听见了「隔断」的回响",
        effect=(
            f'使用"/item.use {_ITEM_ID} @成员"使自己与被 @ 成员获得持续'
            " 1 小时的指向彼此的「隔断」状态。\n"
            "「隔断」状态：只能向隔断对象发起决斗，也只能被隔断对象"
            "发起决斗。"
        ),
        condition="自己和使用对象不具有「隔断」状态",
        timing="任意",
        note="可以对自己使用",
        accepts_args=True,
        can_use=_can_use,
        handle_use=_handle_use,
    )
)
