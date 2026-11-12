"""决斗插件对外提供的服务契约（子包入口）。

按关注点拆分：

- score.py：分数增减、批量写入与分数改变拦截；
- state.py：进行中决斗的只读快照与状态查询；
- event.py：生命周期事件、事件订阅与结算接管；
- gesture.py：单组/整局手势判定规则与所需手势数量；
- action.py：机器人接受概率校验、挑衅、点数缩放、取消与认输。

消费方（道具、签到等插件）可只导入所需契约；本包入口汇总导出全部
决斗契约，便于一次导入多个符号。
"""

from src.plugins._shared.services.duel.action import (
    DuelBotAcceptService,
    DuelCancelService,
    DuelForfeitService,
    DuelMultiplierService,
    DuelProvokeService,
    DuelScaleOutcome,
)
from src.plugins._shared.services.duel.event import (
    DuelEvent,
    DuelEventHandler,
    DuelEventKind,
    DuelEventService,
    DuelSettleService,
    SettleHandler,
)
from src.plugins._shared.services.duel.gesture import (
    DuelGestureContext,
    DuelGestureJudge,
    DuelGesturePairContext,
    DuelGesturePairOutcome,
    DuelGesturePairRule,
    DuelGesturePairVerdict,
    DuelGestureQuotaProvider,
    DuelGestureService,
    DuelGestureSide,
    DuelGestureVerdict,
)
from src.plugins._shared.services.duel.score import (
    DuelScoreInterceptService,
    DuelScoreService,
    ScoreInterceptor,
)
from src.plugins._shared.services.duel.state import DuelSnapshot, DuelStateService

__all__ = [
    "DuelBotAcceptService",
    "DuelCancelService",
    "DuelEvent",
    "DuelEventHandler",
    "DuelEventKind",
    "DuelEventService",
    "DuelForfeitService",
    "DuelGestureContext",
    "DuelGestureJudge",
    "DuelGesturePairContext",
    "DuelGesturePairOutcome",
    "DuelGesturePairRule",
    "DuelGesturePairVerdict",
    "DuelGestureQuotaProvider",
    "DuelGestureService",
    "DuelGestureSide",
    "DuelGestureVerdict",
    "DuelMultiplierService",
    "DuelProvokeService",
    "DuelScaleOutcome",
    "DuelScoreInterceptService",
    "DuelScoreService",
    "DuelSettleService",
    "DuelSnapshot",
    "DuelStateService",
    "ScoreInterceptor",
    "SettleHandler",
]
