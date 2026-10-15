"""道具「Not Found」（编号 404）：黑色诅咒、诅咒型。

获得后不进入库存，由主模块先发送完整信息、随后自动使用：获得持续
1 小时的「404」状态。状态有效期间该成员无法发起决斗，也不会被他人
发起决斗——发起流程在 challenge 事件中被本模块阻断（设置 block_reason
为"404 Not Found"，由决斗插件发送提示并取消决斗创建，见
_shared/services.py 的决斗事件服务）。

状态到期后静默解除：过期状态由定时清理直接删除，不发送任何提示
消息（减少 Bot 不必要的主动消息），决斗功能随之恢复。
"""

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

# 道具编号与状态键
_ITEM_ID = "404"
_STATE_KEY = f"{_ITEM_ID}.not_found"

# 「404」状态的持续时长（秒）：1 小时
_DURATION_SECONDS = 3600.0

# 阻断决斗发起时发送的提示文案（自身发起与被发起时相同）
_BLOCK_REASON = "404 Not Found"

# 决斗事件服务（由决斗插件提供；_framework 已 require("duel")
# 保证服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_event_service = require_service(DuelEventService)


def _has_state(group_id: int, user_id: int) -> bool:
    """判断成员是否持有有效的「404」状态（已过期等待清理的不计入）。"""
    return get_state(group_id, user_id, _STATE_KEY) is not None


async def _on_duel_event(event: DuelEvent) -> None:
    """决斗事件处理：发起流程校验通过后，任一参与方持有有效的「404」
    状态时阻断本次发起（设置 block_reason，决斗不会被创建）。"""
    if event.kind is not DuelEventKind.CHALLENGE:
        return
    if _has_state(event.group_id, event.challenger_id) or _has_state(
        event.group_id, event.opponent_id
    ):
        event.block_reason = _BLOCK_REASON


_event_service.subscribe(_on_duel_event)


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：先登记持续 1 小时的「404」状态，再发送生效提示。

    先登记状态再发送提示：提示经发送排队期间状态已生效，期间发起的
    决斗也能被正常阻断。
    """
    add_state(
        context.group_id,
        context.user_id,
        state=ItemState(key=_STATE_KEY, item_id=_ITEM_ID),
        duration_seconds=_DURATION_SECONDS,
    )
    await context.send(
        f"@{context.user_name}\n"
        f"{_BLOCK_REASON}\n"
        "你请求的页面似乎不存在，1 小时内无法发起决斗，"
        "也没有人能向你发起决斗。"
    )


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="Not Found",
        quality=Quality.BLACK_CURSE,
        types=(ItemType.CURSE,),
        description="404 Not Found",
        effect="使用后获得持续1小时的“404”状态，无法发起/被发起决斗。",
        condition="无",
        timing="自动使用",
        note="404 Not Found",
        handle_use=_handle_use,
    )
)
