"""决斗手势判定：单组规则、整局效果与所需手势数量（由猜拳决斗插件提供）。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

DuelGestureQuotaProvider = Callable[[int, int, int], Awaitable[int]]
"""决斗手势配额提供者：接收 (群号, 成员 QQ 号, 对手 QQ 号)，返回该成员
在该场决斗中需要发送的猜拳表情数量（正整数）。"""


@dataclass(frozen=True)
class DuelGesturePairContext:
    """一组手势判定的只读上下文（经 DuelGesturePairRule 交给规则）。"""

    group_id: int
    """决斗所在群号。"""

    actor_id: int
    """判定主体（结果是相对该成员的视角）的成员 QQ 号。"""

    actor_name: str
    """判定主体的群昵称（提供方已按显示规范截断）。"""

    actor_gesture: int
    """判定主体的手势（1 布、2 剪刀、3 石头）。"""

    opponent_id: int
    """对手的成员 QQ 号。"""

    opponent_name: str
    """对手的群昵称（提供方已按显示规范截断）。"""

    opponent_gesture: int
    """对手的手势（1 布、2 剪刀、3 石头）。"""

    base_outcome: int
    """基础猜拳规则的判定结果（相对判定主体）：1 胜、0 平、-1 负；
    供规则仅在“自己的效果改变了基础判定”时给出结算说明。"""


@dataclass(frozen=True)
class DuelGesturePairVerdict:
    """一组手势的判定结果。"""

    outcome: int
    """相对判定主体的结果：1 胜、0 平、-1 负。"""

    detail: str | None = None
    """结算消息前缀说明（如道具效果），仅当该组手势是整局的最终判定
    依据时由决斗插件采用；None 表示无说明。"""


DuelGesturePairRule = Callable[
    [DuelGesturePairContext], Awaitable[DuelGesturePairVerdict | None]
]
"""单组手势判定规则：返回 None 表示本规则不裁决该组手势（交基础规则或
其它规则）。规则为异步函数，可读取持久化状态；须保持快捷、只读不写
并避免发送消息。"""

DuelGesturePairOutcome = Callable[
    [int, int, int, int], Awaitable[DuelGesturePairVerdict]
]
"""按已注册规则判定一组手势：接收 (判定主体 QQ, 其手势, 对手 QQ, 其手势)，
返回判定结果。"""


@dataclass(frozen=True)
class DuelGestureSide:
    """决斗一方的参与者与其手势记录。"""

    user_id: int
    """成员 QQ 号。"""

    name: str
    """该成员的群昵称（提供方已按显示规范截断）。"""

    gestures: tuple[int, ...]
    """该成员本次决斗的手势（1 布、2 剪刀、3 石头），按发送顺序。"""


@dataclass(frozen=True)
class DuelGestureContext:
    """整局判定的只读上下文（经 DuelGestureJudge 交给手势效果）。"""

    group_id: int
    """决斗所在群号。"""

    challenger: DuelGestureSide
    """发起决斗的一方（含其手势）。"""

    opponent: DuelGestureSide
    """接受决斗的一方（含其手势）。"""

    pair_outcome: DuelGesturePairOutcome
    """按已注册的单组手势规则判定一组手势（含其它道具的单组效果修正
    与基础规则），供多手势效果逐组复用单组判定。"""


@dataclass(frozen=True)
class DuelGestureVerdict:
    """整局判定结果。"""

    winner_id: int | None
    """胜者 QQ 号（为本场决斗的参与方）；None 表示平局。"""

    detail: str | None = None
    """结算消息前缀说明（如道具效果），None 表示无。"""


DuelGestureJudge = Callable[[DuelGestureContext], Awaitable[DuelGestureVerdict | None]]
"""整局判定规则（手势效果）：返回 None 表示本效果不裁决本场胜负。
规则为异步函数，可读取持久化状态；须保持快捷、只读不写并避免发送消息。"""


@runtime_checkable
class DuelGestureService(Protocol):
    """决斗手势服务（由猜拳决斗插件提供）。"""

    def register_pair_rule(
        self, priority: int, name: str, rule: DuelGesturePairRule
    ) -> None:
        """注册单组手势判定规则（同一规则重复注册会被忽略）。

        规则在基础猜拳规则之上叠加：决斗判定一组手势时按优先级从高到
        低询问全部规则，取得出结果（非 None）的最高优先级者；多条规则
        给出不一致的结果时记录警告日志。未注册任何规则或规则全部放弃
        时按基础猜拳规则判定。

        参数:
            priority: 优先级，数值越大越优先（建议使用道具品质值）。
            name: 规则来源的显示名（如“121 原始力量”），用于日志。
            rule: 单组手势判定规则。
        """
        ...

    def register_effect(
        self,
        priority: int,
        name: str,
        judge: DuelGestureJudge,
        *,
        required: DuelGestureQuotaProvider | None = None,
    ) -> None:
        """注册整局判定效果（同一效果重复注册会被忽略）。

        整局判定在单组判定之上先询问已注册的效果：决斗判定胜负时按优先
        级从高到低询问全部效果，取得出结果（非 None）的最高优先级者，并
        采用其结果与结算说明；多条效果给出不一致的结果时记录警告日志。
        全部效果放弃时按单组判定与基础规则结算。

        多手势与整局判定应成对注册（同一效果同时提供 required 与 judge）：
        提高所需手势数量的效果必须能裁决多手势的胜负，未提供 required 的
        效果不改变所需手势数量。

        参数:
            priority: 优先级，数值越大越优先（建议使用道具品质值）。
            name: 效果来源的显示名（如“712 强运”），用于日志。
            judge: 整局判定规则。
            required: 所需手势数量提供者；None 表示不改变所需数量。
        """
        ...
