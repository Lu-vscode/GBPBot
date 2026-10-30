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

from collections.abc import Awaitable, Callable, Sequence
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

    def add_scores(
        self, group_id: int, entries: Sequence[tuple[int, str, int]]
    ) -> None:
        """批量更新多名成员的决斗分数并只写入一次本地存储。

        语义等价于按顺序逐个调用 add_score，但只落盘一次；各成员的
        变化量仍会依次经过分数改变拦截器链。用于「五谷丰登」等一次
        影响全群成员的道具效果。

        参数:
            group_id: 群号。
            entries: (成员 QQ 号, 显示名, 分数变化量) 序列。
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

    def rank_entries(self, group_id: int) -> list[tuple[int, str, int]]:
        """返回指定群决斗分数总榜的全部条目 (QQ 号, 昵称, 分数)。

        总榜为决斗高分榜与反转的决斗低分榜的拼接：全部非零分数的
        成员按分数从高到低排列，分数相同时按 QQ 号升序排列；0 分或
        没有分数记录的成员不在榜上。

        参数:
            group_id: 群号。
        """
        ...


ScoreInterceptor = Callable[[int, int, str, int], int]
"""分数改变拦截器：接收 (群号, 成员 QQ 号, 显示名, 待生效的变化量)，
返回实际生效的变化量（返回 0 表示本次改变被抵消、分数不变）。"""


@runtime_checkable
class DuelScoreInterceptService(Protocol):
    """决斗分数拦截服务（由猜拳决斗插件提供）。"""

    def add_interceptor(self, interceptor: ScoreInterceptor) -> None:
        """注册分数改变拦截器（同一拦截器重复注册会被忽略）。

        拦截器在分数写入前的同步段内按注册顺序依次调用：每个拦截器
        接收上一个拦截器改写后的变化量，返回自己改写后的结果（返回
        0 表示本次改变被抵消、分数不变）；全部决斗分数写入（决斗
        结算、道具效果等）都会经过该拦截点。拦截器应保持快捷；单个
        拦截器抛出的异常只记录错误日志、本次改写不生效，不影响分数
        写入与其它拦截器。

        参数:
            interceptor: 分数改变拦截器。
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
    """已完成出手（发送了全部所需的猜拳表情）的成员 QQ 号集合。"""

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
    """记录任一方一个猜拳手势后触发（需要多个手势的决斗每记录一个
    发布一次）。"""

    SETTLING = "settling"
    """判定完成、结算分数前触发；处理器可修改点数与胜负、读取双方手势
    （challenger_gestures/opponent_gestures，按发送顺序）、设置默认结算
    消息前缀（result_prefix，结算被接管时不使用）。"""

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
    """challenge 事件中由处理器设置的非空文案表示取消本次发起。"""

    claimed: bool = False
    """settling 事件中由处理器设为 True 表示接管本次结算：决斗跳过默认的
    分数结算与结果播报，由处理器自行完成结算；settled 事件仍会在接管
    处理器完成结算后发布。settled 事件中本字段表示本场结算是否已被接管
    （接管时默认的分数结算没有执行，分数可能被接管者自行改写）。"""

    result_prefix: str | None = None
    """settling 事件中由处理器设置的前缀文本（如道具效果说明），默认结算
    消息（胜负/平局播报）会将其拼接在消息最前；结算被接管（claimed）时
    跳过了默认播报，前缀不被使用。"""

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


DuelGestureQuotaProvider = Callable[[int, int, int], int]
"""决斗手势配额提供者：接收 (群号, 成员 QQ 号, 对手 QQ 号)，返回该成员
在该场决斗中需要发送的猜拳表情数量（正整数）。"""


@runtime_checkable
class DuelGestureQuotaService(Protocol):
    """决斗手势配额服务（由猜拳决斗插件提供）。"""

    def add_quota_provider(self, provider: DuelGestureQuotaProvider) -> None:
        """注册手势配额提供者（同一提供者重复注册会被忽略）。

        提供者用于决定一场决斗中成员需要发送几个猜拳表情（如道具
        效果要求发送多个表情聚合胜负）：默认所有成员需要 1 个，注册
        提供者后按提供者返回的数量提高（多个提供者取最大值，至少为
        1）。所需数量在记录手势、就绪判定与机器人代发时动态计算；
        返回非正整数的提供者会被忽略并记录警告，单个提供者抛出的异常
        只记录日志、其结果被忽略；提供者应保持快捷。

        参数:
            provider: 手势配额提供者。
        """
        ...


@runtime_checkable
class DuelBotAcceptService(Protocol):
    """机器人接受决斗概率函数服务（由猜拳决斗插件提供）。"""

    def validate_accept_func(self, expr: str) -> str | None:
        """校验机器人接受决斗的概率函数表达式。

        表达式为 Python 表达式（变量 x 为决斗点数），支持四则运算、
        幂运算（指数受限）与 math 库的常用常量、函数（如 math.sin(x)、
        math.pi）；须能编译并在整个可选点数范围（1~点数上限）内以浮点数
        求值且结果在 0~1 之间。校验对表达式长度、语法规模与总耗时设有
        上限，且不允许包含连续下划线 "__"（防沙盒逃逸）。校验规则与
        决斗插件加载配置时的校验一致（管理员在配置文件中直接配置同样
        受此约束）。

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
        """由机器人代替被挑衅决斗的对方发送全部所需猜拳表情并记录
        为对方的手势。

        应在 accept_by_provoke 成功、挑衅提示消息发送后调用：机器人
        逐条发送猜拳表情并将实际结果记录为对方的手势（所需数量由
        决斗中的手势配额决定，胜负结算影响对方的分数，不影响机器人）；
        该决斗已不在进行中时停止发送、不做任何事。

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


@runtime_checkable
class DuelCancelService(Protocol):
    """决斗取消服务（由猜拳决斗插件提供）。"""

    async def cancel_duel(
        self, group_id: int, user_a: int, user_b: int
    ) -> DuelSnapshot | None:
        """取消双方之间进行中的决斗。

        决斗不限发起方向与是否已被接受；从进行中移除并发布
        canceled 事件（订阅方可据此清理与该决斗相关的登记），此后
        该决斗不再结算；未找到符合条件的决斗时不做任何事并返回
        None。

        参数:
            group_id: 决斗所在群号。
            user_a: 决斗一方的成员 QQ 号。
            user_b: 决斗另一方的成员 QQ 号。

        返回:
            被取消决斗的只读快照；双方之间没有进行中的决斗时返回
            None。
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
