"""决斗事件分发与事件服务。

订阅方（道具等插件）经 DuelEventService 增删事件处理器（实例
event_service 由插件主模块统一注册）；决斗流程在发起、接受、拒绝、
出拳、结算、超时、取消等环节构造 DuelEvent 并经 fire_duel_event
发布。处理器按注册顺序逐个调用（异步处理器被等待），单个处理器抛出
的异常只记录错误日志，不影响决斗流程与其它处理器。
"""

import inspect
from typing import Any

from nonebot import logger

from src.plugins._shared.services import (
    DuelEvent,
    DuelEventHandler,
    DuelEventKind,
    DuelEventService,
)
from src.plugins.duel._state import Duel

# 事件处理器与捕获集合：订阅方经 DuelEventService 增删处理器，
# 发布时按注册顺序逐个调用；单个处理器抛出的异常只记录错误日志，
# 不影响决斗流程与其它的处理器
_EVENT_HANDLER_ERRORS = (Exception,)
_event_handlers: list[DuelEventHandler] = []


class _DuelEventService(DuelEventService):
    """决斗事件服务的实现：订阅/注销决斗生命周期事件处理器。"""

    def subscribe(self, handler: DuelEventHandler) -> None:
        """注册事件处理器（同一处理器重复注册会被忽略）。"""
        if handler not in _event_handlers:
            _event_handlers.append(handler)

    def unsubscribe(self, handler: DuelEventHandler) -> None:
        """注销事件处理器（未注册时不做任何事）。"""
        if handler in _event_handlers:
            _event_handlers.remove(handler)


# 事件服务实例（由插件主模块统一注册）
event_service = _DuelEventService()


def make_duel_event(duel: Duel, kind: DuelEventKind, **extra: Any) -> DuelEvent:
    """以决斗的参与者信息构造事件，extra 中给出的字段会覆盖默认值。"""
    fields: dict[str, Any] = {
        "kind": kind,
        "group_id": duel.group_id,
        "bot_self_id": duel.bot_self_id,
        "challenger_id": duel.challenger_id,
        "challenger_name": duel.challenger_name,
        "opponent_id": duel.opponent_id,
        "opponent_name": duel.opponent_name,
        "multiplier": duel.multiplier,
    }
    fields.update(extra)
    return DuelEvent(**fields)


async def _call_event_handler(handler: DuelEventHandler, event: DuelEvent) -> None:
    """调用单个事件处理器；失败只记录错误日志，不影响决斗流程与其它处理器。"""
    try:
        result = handler(event)
        if inspect.isawaitable(result):
            await result
    except _EVENT_HANDLER_ERRORS as exc:
        logger.error(f"决斗事件处理器处理 {event.kind.value} 事件时出错：{exc}")


async def fire_duel_event(event: DuelEvent) -> None:
    """把事件按注册顺序派发给全部处理器（单个处理器失败只记录日志）。

    派发使用注册列表的快照，处理器在派发期间的注册或注销不影响本次派发。
    """
    for handler in tuple(_event_handlers):
        await _call_event_handler(handler, event)
