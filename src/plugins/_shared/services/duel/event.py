"""决斗生命周期事件与结算接管（由猜拳决斗插件提供）。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class DuelEventKind(StrEnum):
    """决斗生命周期事件类型。"""

    CHALLENGE = "challenge"
    """发起决斗校验通过后、登记决斗前触发；处理器可设置 block_reason 取消发起。"""

    CREATED = "created"
    """决斗登记后、发起播报发送后触发。"""

    ACCEPTING = "accepting"
    """对方接受决斗前触发；处理器可设置 block_reason 取消本次接受
    （决斗保持等待对方接受的状态，仍可拒绝）。"""

    ACCEPTED = "accepted"
    """对方接受决斗后触发（含机器人掷骰接受）。"""

    REJECTED = "rejected"
    """对方拒绝决斗后触发（含机器人掷骰拒绝）。"""

    GESTURE = "gesture"
    """记录任一方一个猜拳手势后触发（需要多个手势的决斗每记录一个
    发布一次）。"""

    SETTLING = "settling"
    """判定完成、结算分数前触发；处理器可修改点数、读取双方手势
    （challenger_gestures/opponent_gestures，按发送顺序）与判定结果
    （winner_id/loser_id 只读）、追加默认结算消息前缀（result_prefix，
    结算被接管时不使用）。胜负只能经手势服务（DuelGestureService）注册
    的判定规则或结算接管（DuelSettleService）改变，直接改写
    winner_id/loser_id 会被决斗插件还原并记录错误日志。"""

    SETTLED = "settled"
    """分数结算与结果播报完成后触发；结算被处理器接管时在接管处理器
    完成结算后触发（此时事件的 claimed 为 True）。"""

    TIMEOUT = "timeout"
    """决斗超时认领移除后触发。"""

    CANCELED = "canceled"
    """决斗被取消后触发（点数归零或经决斗取消服务主动取消；决斗已从
    进行中移除，不会再结算）。"""


@dataclass
class DuelEvent:
    """决斗生命周期事件（经 DuelEventService 发布给订阅者）。"""

    kind: DuelEventKind
    """事件类型。"""

    group_id: int
    """决斗所在群号。"""

    bot_self_id: int
    """处理该决斗的机器人 QQ 号。"""

    challenger_id: int
    """发起决斗的成员 QQ 号（可能为机器人自己）。"""

    challenger_name: str
    """发起决斗一方的群昵称。"""

    opponent_id: int
    """接受/被挑战一方的成员 QQ 号（可能为机器人自己）。"""

    opponent_name: str
    """接受/被挑战一方的群昵称。"""

    multiplier: int
    """决斗点数；settling 事件中处理器可修改（负数会被取 0 并记录警告）。"""

    actor_id: int | None = None
    """accepted/rejected/gesture 事件的动作发起者 QQ 号。"""

    gesture: int | None = None
    """gesture 事件的手势值（1 布、2 剪刀、3 石头）。"""

    challenger_gestures: tuple[int, ...] = ()
    """settling 事件的挑战者手势（1 布、2 剪刀、3 石头），按发送顺序；
    双方出拳后的结算前触发，此时两人全部所需手势必然已记录。"""

    opponent_gestures: tuple[int, ...] = ()
    """settling 事件的接受方手势（1 布、2 剪刀、3 石头），按发送顺序。"""

    block_reason: str | None = None
    """challenge/accepting 事件中由处理器设置的非空文案表示取消本次
    发起/接受（该文案由决斗插件发送）。"""

    claimed: bool = False
    """settling 事件中由处理器设为 True 表示接管本次结算：决斗跳过默认的
    分数结算与结果播报，由处理器自行完成结算；settled 事件仍会在接管
    处理器完成结算后发布。settled 事件中本字段表示本场结算是否已被接管
    （接管时默认的分数结算没有执行，分数可能被接管者自行改写）。"""

    result_prefix: str | None = None
    """结算消息前缀文本（如道具效果说明），默认结算消息（胜负/平局播报）
    会将其拼接在消息最前；判定规则（见 DuelGestureService）给出的说明是
    settling 事件中的初始值，处理器可在其后追加；结算被接管（claimed）时
    跳过了默认播报，前缀不被使用。"""

    winner_id: int | None = None
    """settling/settled 事件的胜者 QQ 号（与 loser_id 同为 None 表示平局）；
    settling 事件中为只读的判定结果，改写会被决斗插件还原并记录错误日志。"""

    loser_id: int | None = None
    """settling/settled 事件的负者 QQ 号（settling 事件中同样只读）。"""

    winner_name: str | None = None
    """settled 事件的胜者群昵称。"""

    loser_name: str | None = None
    """settled 事件的负者群昵称。"""

    draw: bool = False
    """settled 事件是否为平局。"""


DuelEventHandler = Callable[[DuelEvent], Awaitable[None] | None]
"""决斗事件处理器：同步或异步均可。

处理器在决斗流程内按注册顺序依次调用（异步处理器被等待），应保持快捷；
处理器抛出的异常会被记录错误日志，不影响决斗流程与其它处理器。
"""


@runtime_checkable
class DuelEventService(Protocol):
    """决斗事件服务（由猜拳决斗插件提供）。"""

    def subscribe(self, handler: DuelEventHandler) -> None:
        """注册事件处理器（同一处理器重复注册会被忽略）。"""
        ...

    def unsubscribe(self, handler: DuelEventHandler) -> None:
        """注销事件处理器（未注册时不做任何事）。"""
        ...


SettleHandler = Callable[[DuelEvent], Awaitable[bool] | bool]
"""结算接管者：在 settling 事件的全部处理器完成后按注册优先级调用。

返回 True 表示已接管本场结算并完成分数改变与播报，决斗跳过默认的
分数结算与结果播报；返回 False 表示本场不接管，且不应产生任何副作用。
接管者在改变分数后必须返回 True（播报失败只记录日志即可），避免同一场
结算被执行两次；接管者只读取事件中的最终胜负，不应再改写它。"""


@runtime_checkable
class DuelSettleService(Protocol):
    """决斗结算接管服务（由猜拳决斗插件提供）。"""

    def register_settler(self, priority: int, handler: SettleHandler) -> None:
        """注册结算接管者（同一处理器重复注册会被忽略）。

        决斗判定完成、settling 事件的全部处理器执行完毕后，按优先级
        从高到低依次询问接管者：第一个返回 True 的接管本场结算，其余
        接管者不再被调用；全部返回 False 时按默认逻辑结算。同优先级
        的接管者按注册顺序询问。接管者抛出的异常按已接管处理（记录
        错误日志），避免已产生的分数改变与默认结算或其它接管者叠加。

        参数:
            priority: 接管优先级，数值越大越优先（建议使用道具品质值；
                同品质道具的冲突由道具自行约定具体数值）。
            handler: 结算接管者。
        """
        ...
