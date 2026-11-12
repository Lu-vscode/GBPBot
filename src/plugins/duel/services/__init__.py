"""决斗的跨插件服务实现（包入口）。

本包按服务族拆分实现，契约见 _shared/services/：

- events.py：决斗事件分发（DuelEventService）与结算接管（DuelSettleService）；
- scores.py：分数存储与分数服务（DuelScoreService）、分数拦截服务
  （DuelScoreInterceptService）；
- state.py：决斗状态查询服务（DuelStateService）；
- gestures.py：决斗手势服务（DuelGestureService，注册判定规则与配额）；
- actions.py：挑衅、点数缩放、取消与认输服务；
- bot_accept.py：机器人接受概率函数服务（DuelBotAcceptService）。

本包不在 __init__ 中汇总导入：服务实现之间存在“流程 → 事件服务”的
单向依赖，逐模块导入可避免包级循环导入；服务实例由插件主模块统一
注册到注册中心。
"""
