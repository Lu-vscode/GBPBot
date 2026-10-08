"""跨插件服务注册中心。

NoneBot 的插件之间不应互相导入模块（提前导入会导致插件无法由框架
正常加载，见 PluginLoader 对已导入模块的检查）；本模块提供一套解耦
的跨插件交互方式：提供方插件把自身能力注册为"服务"，消费方插件按
契约定向获取并调用。

约定：
1. 服务契约（@runtime_checkable 的 Protocol，仅含方法）与注册、获取
   接口都定义在本共享模块中，交互双方共同依赖契约，不互相导入。
2. 提供方插件在加载时注册服务（注册早于任何事件处理）：
   `register_service(DuelScoreService, _DuelScoreService())`
3. 消费方插件在加载时先用 `nonebot.plugin.require` 声明对提供方插件
   的依赖（确保其已加载、服务已注册），再获取服务：
   `require("duel")`
   `service = get_service(DuelScoreService)`      # 可选依赖
   `service = require_service(DuelScoreService)`  # 必需依赖
4. 本模块所在包以 "_" 开头，不会被 NoneBot 当作插件加载；插件中通过
   `from src.plugins._shared.services import ...` 导入（插件模块名形如
   src.plugins.xxx，与运行时命名一致）。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot


@runtime_checkable
class DuelScoreService(Protocol):
    """决斗分数服务（由猜拳决斗插件提供）。"""

    def add_score(self, group_id: int, user_id: int, name: str, delta: int) -> int:
        """更新指定群内成员的决斗分数并写入本地存储，返回更新后的分数。

        参数:
            group_id: 群号。
            user_id: 成员 QQ 号。
            name: 成员在群内的显示名（提供方按自身的显示规范处理）。
            delta: 分数变化量，可为负数。
        """
        ...

    def get_score(self, group_id: int, user_id: int) -> int:
        """查询指定群内成员的决斗分数，无记录时返回 0。

        参数:
            group_id: 群号。
            user_id: 成员 QQ 号。
        """
        ...

    def lowest_member(self, group_id: int) -> int | None:
        """返回指定群决斗低分榜第一名成员的 QQ 号，没有负分成员时返回 None。

        低分榜的判定与 /duel.rank 的低分榜一致：分数为负的成员按
        分数从低到高排列（即绝对值从大到小），分数相同时按 QQ 号
        升序排列；只有排在最前的成员是第一名。

        参数:
            group_id: 群号。
        """
        ...

    def highest_member(self, group_id: int) -> int | None:
        """返回指定群决斗高分榜第一名成员的 QQ 号，没有正分成员时返回 None。

        高分榜的判定与 /duel.rank 的高分榜一致：分数为正的成员按
        分数从高到低排列，分数相同时按 QQ 号升序排列；只有排在最前
        的成员是第一名。

        参数:
            group_id: 群号。
        """
        ...


@dataclass(frozen=True)
class DuelSnapshot:
    """一场进行中决斗的只读快照（由 DuelStateService 提供）。"""

    group_id: int
    """决斗所在群号。"""

    challenger_id: int
    """发起决斗的成员 QQ 号。"""

    challenger_name: str
    """发起决斗一方的群昵称（提供方已按显示规范截断）。"""

    opponent_id: int
    """接受决斗一方的成员 QQ 号（被 @ 的成员或机器人自己）。"""

    opponent_name: str
    """接受决斗一方的群昵称（提供方已按显示规范截断）。"""

    multiplier: int
    """决斗点数。"""

    accepted: bool
    """对方是否已接受。"""

    gesture_users: frozenset[int]
    """已发送猜拳表情的成员 QQ 号集合。"""

    remaining_seconds: float
    """距超时结束的剩余秒数（不小于 0）。"""


@runtime_checkable
class DuelStateService(Protocol):
    """决斗状态查询服务（由猜拳决斗插件提供）。"""

    def list_duels(self, group_id: int) -> list[DuelSnapshot]:
        """返回指定群内全部进行中决斗的快照（按创建顺序）。"""
        ...


class DuelEventKind(StrEnum):
    """决斗生命周期事件类型。"""

    CHALLENGE = "challenge"
    """发起决斗校验通过后、登记决斗前触发；处理器可设置 block_reason 取消发起。"""

    CREATED = "created"
    """决斗登记后、发起播报发送后触发。"""

    ACCEPTED = "accepted"
    """对方接受决斗后触发（含机器人掷骰接受）。"""

    REJECTED = "rejected"
    """对方拒绝决斗后触发（含机器人掷骰拒绝）。"""

    GESTURE = "gesture"
    """记录任一方猜拳手势后触发。"""

    SETTLING = "settling"
    """判定完成、结算分数前触发；处理器可修改点数与胜负。"""

    SETTLED = "settled"
    """分数结算与结果播报完成后触发。"""

    TIMEOUT = "timeout"
    """决斗超时认领移除后触发。"""

    CANCELED = "canceled"
    """决斗点数归零被取消后触发（决斗已从进行中移除，不会再结算）。"""


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
    """gesture 事件的手势值（1 剪刀、2 石头、3 布）。"""

    block_reason: str | None = None
    """challenge 事件中由处理器设置的非空文案表示取消本次发起。"""

    claimed: bool = False
    """settling 事件中由处理器设为 True 表示接管本次结算：决斗跳过默认的
    分数结算与结果播报（含 settled 事件），由处理器自行完成结算。"""

    winner_id: int | None = None
    """settling/settled 事件的胜者 QQ 号（与 loser_id 同为 None 表示平局）。"""

    loser_id: int | None = None
    """settling/settled 事件的负者 QQ 号。"""

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


@runtime_checkable
class DuelBotAcceptService(Protocol):
    """机器人接受决斗概率函数服务（由猜拳决斗插件提供）。"""

    def validate_accept_func(self, expr: str) -> str | None:
        """校验机器人接受决斗的概率函数表达式。

        表达式为 Python 表达式（变量 x 为决斗点数），须能编译并在整个
        可选点数范围（1~点数上限）内以浮点数求值且结果在 0~1 之间，
        且不允许包含连续下划线 "__"（防沙盒逃逸；该限制面向外部
        输入，管理员在配置文件中直接配置不受此限制）。校验规则与
        决斗插件加载配置时的校验一致。

        参数:
            expr: 待校验的概率函数表达式。

        返回:
            面向用户的错误文案；表达式合法时返回 None。
        """
        ...


@runtime_checkable
class DuelProvokeService(Protocol):
    """决斗挑衅服务（由猜拳决斗插件提供）。"""

    async def accept_by_provoke(
        self, group_id: int, challenger_id: int, opponent_id: int
    ) -> DuelSnapshot | None:
        """强制对方接受决斗（挑衅），成功时返回被接受决斗的只读快照。

        查找 challenger_id 向 opponent_id 发起、对方尚未接受的决斗并
        强制接受：此后该决斗中对方自己发送的猜拳表情无效，对方的手势
        由机器人代替（调用方应发送挑衅提示后调用 send_provoked_gesture
        让机器人出手）；未找到符合条件的决斗时不做任何事并返回 None。

        参数:
            group_id: 决斗所在群号。
            challenger_id: 发起决斗的一方（挑衅者）的成员 QQ 号。
            opponent_id: 被挑衅的成员 QQ 号。
        """
        ...

    async def send_provoked_gesture(
        self, bot: "Bot", group_id: int, challenger_id: int, opponent_id: int
    ) -> None:
        """由机器人代替被挑衅决斗的对方发送猜拳表情并记录为对方的手势。

        应在 accept_by_provoke 成功、挑衅提示消息发送后调用：机器人
        发送猜拳表情并将实际结果记录为对方的手势（胜负结算影响对方的
        分数，不影响机器人）；该决斗已不在进行中时不做任何事。

        参数:
            bot: 发送猜拳表情的机器人实例。
            group_id: 决斗所在群号。
            challenger_id: 发起决斗的一方（挑衅者）的成员 QQ 号。
            opponent_id: 被挑衅的成员 QQ 号。
        """
        ...


@dataclass(frozen=True)
class DuelScaleOutcome:
    """一次进行中决斗点数缩放的结果（由 DuelMultiplierService 返回）。"""

    multiplier: int
    """缩放后的决斗点数（决斗被取消时为 0）。"""

    canceled: bool
    """决斗是否因点数被缩放到 0 而取消。"""


@runtime_checkable
class DuelMultiplierService(Protocol):
    """决斗点数缩放服务（由猜拳决斗插件提供）。"""

    async def scale_multiplier(
        self,
        group_id: int,
        user_a: int,
        user_b: int,
        numerator: int,
        denominator: int,
    ) -> DuelScaleOutcome | None:
        """将双方之间进行中的决斗点数按比例缩放。

        新点数为原点数乘 numerator 除以 denominator 后向下取整；新
        点数为 0 时取消该决斗（从进行中移除并发布 canceled 事件）；
        未找到符合条件的决斗时不做任何事并返回 None。决斗不限发起
        方向与是否已被接受。

        参数:
            group_id: 决斗所在群号。
            user_a: 决斗一方的成员 QQ 号。
            user_b: 决斗另一方的成员 QQ 号。
            numerator: 缩放比例的分子（正整数）。
            denominator: 缩放比例的分母（正整数）。

        返回:
            缩放结果（缩放后的点数与决斗是否被取消）；双方之间没有
            进行中的决斗时返回 None。
        """
        ...


@dataclass(frozen=True)
class GrantedItem:
    """一次道具发放的结果（由 ItemService.grant_random 返回）。"""

    item_id: str
    """道具编号（三位数字字符串）。"""

    item_name: str
    """道具名称。"""


@runtime_checkable
class ItemService(Protocol):
    """道具发放服务（由道具插件提供）。"""

    def grant_random(
        self, group_id: int, user_id: int, name: str
    ) -> GrantedItem | None:
        """为成员随机发放并登记一件道具，返回发放结果；抽取失败时返回 None。

        普通道具写入库存；黑色诅咒不写入库存，由调用方在其后调用
        handle_acquisition 完成信息播报与自动使用。

        参数:
            group_id: 群号。
            user_id: 成员 QQ 号。
            name: 成员在群内的显示名（提供方按自身显示规范处理）。
        """
        ...

    async def handle_acquisition(
        self,
        bot: "Bot",
        group_id: int,
        user_id: int,
        name: str,
        item: GrantedItem,
    ) -> None:
        """处理道具获得后的自动流程，应在调用方自己的提示消息之后调用。

        黑色诅咒会先发送完整信息再自动使用；其它道具为空操作。

        参数:
            bot: 发送道具消息的机器人实例。
            group_id: 群号。
            user_id: 成员 QQ 号。
            name: 成员在群内的显示名。
            item: grant_random 返回的道具发放结果。
        """
        ...


# 已注册的服务：契约类型 -> 提供方实现对象
_service_providers: dict[type[Any], Any] = {}


def register_service[T](interface: type[T], provider: T) -> None:
    """注册服务提供方。

    参数:
        interface: 服务契约（@runtime_checkable 的 Protocol 类型）。
        provider: 实现该契约的对象。

    异常:
        TypeError: 实现对象不符合契约（缺少契约要求的方法）。
        RuntimeError: 该契约已注册过服务（通常意味着插件被重复加载，
            属于需要暴露的错误，不做静默覆盖）。
    """
    if interface in _service_providers:
        message = f"服务 {interface.__name__} 已注册，不能重复注册"
        raise RuntimeError(message)
    if not isinstance(provider, interface):
        message = f"服务实现不符合契约 {interface.__name__} 的约定"
        raise TypeError(message)
    _service_providers[interface] = provider


def get_service[T](interface: type[T]) -> T | None:
    """获取已注册的服务提供方，未注册时返回 None（供可选依赖使用）。"""
    return _service_providers.get(interface)


def require_service[T](interface: type[T]) -> T:
    """获取已注册的服务提供方，未注册时抛出异常（供必需依赖使用）。

    异常:
        RuntimeError: 服务尚未注册（提供方插件未加载或未注册服务）。
    """
    service = get_service(interface)
    if service is None:
        message = f"服务 {interface.__name__} 尚未注册"
        raise RuntimeError(message)
    return service
