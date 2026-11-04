"""道具「+1」（编号 411）：蓝色稀有、永久型。

使用后获得 1 层永久状态「+1」：每持有 1 层，每日签到获得的决斗分数
额外 +1（层数登记在状态附加数据中，永久有效、不因时间流逝而减少）。

签到分数加成经跨插件服务提供给签到插件（见 _shared/services.py 的
SignScoreBonusService）：本模块只负责按状态层数登记加成提供者，签到
时由框架汇总（见 _framework.sign_bonus）。
"""

from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemType,
    ItemUseContext,
    Quality,
    add_state,
    get_state,
    register_item,
    set_sign_bonus_provider,
)

# 道具编号与状态键
_ITEM_ID = "411"
_STATE_KEY = f"{_ITEM_ID}.plus_one"

# 状态附加数据中的层数字段
_LAYERS_KEY = "layers"


def _layers_of(state: ItemState | None) -> int:
    """返回「+1」状态的层数（状态不存在或数据异常时视为 0 层）。"""
    if state is None:
        return 0
    value = state.data.get(_LAYERS_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：获得 1 层永久状态「+1」并发送提示。"""
    state = get_state(context.group_id, context.user_id, _STATE_KEY)
    layers = _layers_of(state) + 1
    add_state(
        context.group_id,
        context.user_id,
        state=ItemState(key=_STATE_KEY, item_id=_ITEM_ID, data={_LAYERS_KEY: layers}),
    )
    if state is None:
        await context.send(
            f"{context.user_name} 使用了 {context.item.label}，"
            f"获得永久状态“+1”（当前 1 层）。\n"
            f"每日签到获得的决斗分数+1。"
        )
        return
    await context.send(
        f"{context.user_name} 又使用了 {context.item.label}，"
        f"“+1”的层数增加到 {layers} 层。\n"
        f"每日签到获得的决斗分数+{layers}。"
    )


def _sign_bonus(group_id: int, user_id: int) -> int:
    """签到分数加成：每层「+1」为签到获得的决斗分数+1。"""
    return _layers_of(get_state(group_id, user_id, _STATE_KEY))


set_sign_bonus_provider(_ITEM_ID, _sign_bonus)

register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="+1",
        quality=Quality.BLUE,
        types=(ItemType.PERMANENT,),
        description="永久+1",
        effect="使用后获得1层永久状态“+1”：每日签到获得的决斗分数+[层数]。",
        condition="无",
        timing="任意",
        note="等我启动",
        handle_use=_handle_use,
        state_labels=((_STATE_KEY, "+1"),),
    )
)
