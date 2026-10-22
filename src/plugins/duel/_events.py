"""决斗事件分发与事件服务。

订阅方（道具等插件）经 DuelEventService 增删事件处理器（实例
event_service 由插件主模块统一注册）；决斗流程在发起、接受、拒绝、
出拳、结算、超时、取消等环节构造 DuelEvent 并经 fire_duel_event
发布。处理器按注册顺序逐个调用（异步处理器被等待），单个处理器抛出
的异常只记录错误日志，不影响决斗流程与其它处理器。

结算接管者（如接管结算的道具）经 DuelSettleService 注册：settling
事件的全部处理器完成后，决斗流程经 run_settlers 按优先级从高到低
询问接管者，第一个返回 True 的接管本场结算。
"""

import inspect
from typing import Any

from nonebot import logger

from src.plugins._shared.services import (
    DuelEvent,
    DuelEventHandler,
    DuelEventKind,
    DuelEventService,
    DuelSettleService,
    SettleHandler,
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

# 结算接管者：(优先级, 处理器)；询问时按优先级降序、同优先级按注册
# 顺序（sorted 稳定）
_settlers: list[tuple[int, SettleHandler]] = []


class _DuelSettleService(DuelSettleService):
    """决斗结算接管服务的实现：注册按优先级询问的接管者。"""

    def register_settler(self, priority: int, handler: SettleHandler) -> None:
        """注册结算接管者（同一处理器重复注册会被忽略）。"""
        if any(registered is handler for _, registered in _settlers):
            return
        _settlers.append((priority, handler))


# 结算接管服务实例（由插件主模块统一注册）
settle_service = _DuelSettleService()


async def run_settlers(event: DuelEvent) -> bool:
    """按优先级从高到低询问结算接管者，返回是否已由接管者完成结算。

    同优先级的接管者按注册顺序询问；接管者返回 True 表示已接管并完成
    分数改变与播报，本次询问随即结束。接管者抛出的异常按已接管处理
    （记录错误日志并返回 True），避免已产生的分数改变与默认结算或其它
    接管者叠加。供决斗流程在 settling 事件分发完毕、最终胜负确定后调用。
    """
    for _, handler in sorted(_settlers, key=lambda entry: -entry[0]):
        try:
            result = handler(event)
            if inspect.isawaitable(result):
                result = await result
        except _EVENT_HANDLER_ERRORS as exc:
            logger.error(f"结算接管者处理 settling 事件时出错，按已接管处理：{exc}")
            return True
        if result:
            return True
    return False


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
