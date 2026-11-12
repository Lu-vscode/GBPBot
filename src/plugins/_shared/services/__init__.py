"""跨插件服务注册中心与共享服务契约（包入口）。

NoneBot 的插件之间不应互相导入模块（提前导入会导致插件无法由框架
正常加载，见 PluginLoader 对已导入模块的检查）；本包提供一套解耦
的跨插件交互方式：提供方插件把自身能力注册为"服务"，消费方插件按
契约定向获取并调用。

约定：
1. 服务契约（@runtime_checkable 的 Protocol，仅含方法）与注册、获取
   接口都定义在本共享包中，交互双方共同依赖契约，不互相导入。
2. 提供方插件在加载时注册服务（注册早于任何事件处理）：
   `register_service(DuelScoreService, _DuelScoreService())`
3. 消费方插件在加载时先用 `nonebot.plugin.require` 声明对提供方插件
   的依赖（确保其已加载、服务已注册），再获取服务：
   `require("duel")`
   `service = get_service(DuelScoreService)`      # 可选依赖
   `service = require_service(DuelScoreService)`  # 必需依赖
4. 本模块所在包以 "_" 开头，不会被 NoneBot 当作插件加载；插件中通过
   `from src.plugins._shared.services import ...` 导入（插件模块名形如
   src.plugins.xxx，与运行时命名一致）。本包入口汇总导出全部契约，
   便于一次导入多个符号；只关心单个契约时可从下列子模块精确导入。

本包的组成：

- registry.py：服务注册与获取（register_service / get_service /
  require_service）；
- duel/：决斗插件对外提供的服务契约（score.py 分数与分数拦截、state.py
  决斗状态快照、event.py 生命周期事件与结算接管、gesture.py 手势判定与
  所需手势数量、action.py 接受概率/挑衅/点数缩放/取消/认输）；
- item.py：道具插件对外提供的道具发放服务；
- sign.py：签到分数加成服务（由道具插件提供、签到插件调用）。
"""

from src.plugins._shared.services.duel import (
    DuelBotAcceptService,
    DuelCancelService,
    DuelEvent,
    DuelEventHandler,
    DuelEventKind,
    DuelEventService,
    DuelForfeitService,
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
    DuelMultiplierService,
    DuelProvokeService,
    DuelScaleOutcome,
    DuelScoreInterceptService,
    DuelScoreService,
    DuelSettleService,
    DuelSnapshot,
    DuelStateService,
    ScoreInterceptor,
    SettleHandler,
)
from src.plugins._shared.services.item import GrantedItem, ItemService
from src.plugins._shared.services.registry import (
    get_service,
    register_service,
    require_service,
)
from src.plugins._shared.services.sign import SignScoreBonusService

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
    "GrantedItem",
    "ItemService",
    "ScoreInterceptor",
    "SettleHandler",
    "SignScoreBonusService",
    "get_service",
    "register_service",
    "require_service",
]
