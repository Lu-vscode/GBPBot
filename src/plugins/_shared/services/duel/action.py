"""决斗动作服务：接受概率、挑衅、点数缩放、取消与认输。

均由猜拳决斗插件提供；这些服务只影响进行中的决斗，不改变分数服务与
事件服务的契约。
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from src.plugins._shared.services.duel.state import DuelSnapshot

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot


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


@runtime_checkable
class DuelForfeitService(Protocol):
    """决斗认输服务（由猜拳决斗插件提供）。"""

    async def forfeit_duel(
        self,
        bot: "Bot",
        group_id: int,
        loser_id: int,
        winner_id: int,
        prefix: str | None = None,
    ) -> DuelSnapshot | None:
        """让双方之间进行中的决斗跳过出拳阶段，按指定一方落败直接结算。

        仅对已接受、尚未结算的决斗生效：该决斗从进行中移除后立即进入
        正常结算流程——发布 settling/settled 事件、按点数更新双方分数并
        播报默认结算消息，胜负不再经过手势判定（判定规则与所需手势数量
        不参与）。双方之间没有符合条件的决斗时不做任何事并返回 None。

        参数:
            bot: 发送结算消息的机器人实例。
            group_id: 决斗所在群号。
            loser_id: 判负一方的成员 QQ 号。
            winner_id: 判胜一方的成员 QQ 号（本场决斗的另一方）。
            prefix: 拼在默认结算消息前的说明文案（如道具效果）；结算被
                接管（见 DuelSettleService）时与判定的说明一样不被使用。

        返回:
            被认输结算的决斗的只读快照；没有符合条件的决斗时返回 None。
        """
        ...
