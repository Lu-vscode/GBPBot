"""道具「坚如磐石」（编号 723）：灰色垃圾、Buff型。

使用后获得持续 1 小时的「玉璋护盾」状态：获得黑色诅咒道具时，诅咒的
详细信息消息照常发送，但诅咒被护盾挡住——黑色诅咒道具失去（诅咒本就
不写入库存）、效果不执行，同时减少一层护盾。护盾可叠加多层，各层
独立计时，消耗时优先消耗最早到期的一层；状态到期静默消失。

本模块经框架的黑色诅咒拦截钩子（add_curse_interceptor）实现：在诅咒
效果执行前消耗一层护盾并发送提示消息，阻止本次自动使用。
"""

from nonebot import logger

from src.plugins.item._framework import (
    ItemDefinition,
    ItemState,
    ItemType,
    ItemUseContext,
    Quality,
    add_curse_interceptor,
    add_state,
    list_states,
    register_item,
    remove_state,
)

# 道具编号、状态键前缀、状态显示名与状态持续时长（秒）
_ITEM_ID = "723"
_STATE_KEY_BASE = f"{_ITEM_ID}.shield"
_STATE_KEY_PREFIX = f"{_STATE_KEY_BASE}."
_SHIELD_NAME = "玉璋护盾"
_DURATION_SECONDS = 3600.0


def _active_states(group_id: int, user_id: int) -> list[ItemState]:
    """返回成员持有的全部有效「玉璋护盾」状态（按到期时间升序）。

    expires_at 为 None（异常数据）的条目排在最前，优先被消耗。
    """
    states = [
        state
        for state in list_states(group_id, user_id)
        if state.key.startswith(_STATE_KEY_PREFIX)
    ]
    states.sort(key=lambda state: state.expires_at or 0.0)
    return states


def _next_state_key(group_id: int, user_id: int) -> str:
    """为新的一层护盾生成状态键（在现存有效状态的序号中取最小可用值）。

    已过期但未清理的状态可能占用同序号键；add_state 添加同键状态时
    会覆盖旧条目，序号仍可复用。
    """
    used: set[int] = set()
    for state in _active_states(group_id, user_id):
        suffix = state.key[len(_STATE_KEY_PREFIX) :]
        if suffix.isdigit():
            used.add(int(suffix))
    index = 1
    while index in used:
        index += 1
    return f"{_STATE_KEY_PREFIX}{index}"


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：登记一层持续 1 小时的「玉璋护盾」并发送提示。"""
    add_state(
        context.group_id,
        context.user_id,
        state=ItemState(
            key=_next_state_key(context.group_id, context.user_id),
            item_id=_ITEM_ID,
        ),
        duration_seconds=_DURATION_SECONDS,
    )
    layers = len(_active_states(context.group_id, context.user_id))
    await context.send(
        f"{context.user_name} 使用了 {context.item.label}，"
        f"获得了一层“{_SHIELD_NAME}”。\n\n"
        f"1 小时内，获得黑色诅咒时护盾会抵消该诅咒。\n"
        f"当前共有 {layers} 层“{_SHIELD_NAME}”。"
    )


async def _intercept_curse(context: ItemUseContext) -> bool:
    """黑色诅咒拦截：消耗一层护盾并抵消本次诅咒，返回是否拦截。

    在诅咒的详细信息消息发送后、诅咒效果执行前调用：持有护盾时消耗
    最早到期的一层、发送抵消提示并返回 True；未持有护盾时返回 False，
    诅咒按正常流程自动使用。
    """
    states = _active_states(context.group_id, context.user_id)
    if not states:
        return False
    remove_state(context.group_id, context.user_id, states[0].key)
    remaining = len(states) - 1
    logger.info(
        f"群 {context.group_id} 成员 {context.user_id} 的"
        f"「{_SHIELD_NAME}」抵消了黑色诅咒 {context.item.label}，"
        f"剩余 {remaining} 层"
    )
    if remaining:
        layers_text = f"“{_SHIELD_NAME}”减少一层，剩余 {remaining} 层。"
    else:
        layers_text = f"“{_SHIELD_NAME}”已经耗尽。"
    await context.send(
        f"{context.user_name} 的“{_SHIELD_NAME}”生效，"
        f"挡住了黑色诅咒 {context.item.label}！\n"
        f"黑色诅咒失去了效果。\n{layers_text}"
    )
    return True


add_curse_interceptor(_intercept_curse)

register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="坚如磐石",
        quality=Quality.GRAY,
        types=(ItemType.BUFF,),
        description="关键抵挡！",
        effect=(
            "使用后获得持续1小时的“玉璋护盾”状态：抵消一次黑色诅咒。\n"
            "“玉璋护盾”状态可以叠加多层，各自独立计时；"
            "获得黑色诅咒时消耗一层护盾并抵消该诅咒。"
        ),
        condition="无",
        timing="任意",
        note="岩石的重量，令人安心",
        handle_use=_handle_use,
        state_labels=((_STATE_KEY_BASE, _SHIELD_NAME),),
    )
)
