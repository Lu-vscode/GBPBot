"""猜拳决斗插件。

在群聊中发起猜拳决斗：`/duel @成员 [点数]` 发起（点数默认 1，上限由
DUEL_MAX_MULTIPLIER 配置，默认 100），被 @ 的成员可通过
`/duel.accept @成员` 接受或 `/duel.reject @成员` 拒绝；接受后双方发送
QQ"包剪锤"表情，机器人根据双方手势判定胜负：胜者分数 +点数、负者分数
-点数，平局分数不变；成员所需的猜拳表情数量默认为 1，可由道具经
手势服务提高，胜负由 core/judgement.py 的判定引擎统一判定（整局判定
效果 → 单组判定规则 → 基础猜拳规则）；各群分数相互独立，分数数据使用
nonebot-plugin-orm 长期存储在本地，并且作为"决斗分数服务"经跨插件
服务注册中心（见 _shared/services/）供其它插件增减分数与查询高/低分
榜第一名，同时提供决斗分数拦截服务（DuelScoreInterceptService：供道具
等插件在分数改变生效前拦截并改写变化量）；
`/duel.rank` 可查看本群分数排行榜（以合并转发发送），`/duel.score`
可查看自己在本群的决斗分数（超级用户可附加 sum/s 参数查看本群全部
群成员的分数总和，未在用户手册中提及），`/duel.status` 可查看自己
在本群的决斗状态。除分数服务外，还对外提供决斗状态服务
（DuelStateService）、决斗事件服务（DuelEventService：在发起、接受、
拒绝、出拳、结算、超时、取消等环节发布事件，供道具等插件联动，
settling 事件的处理器可接管结算），机器人接受概率函数服务
（DuelBotAcceptService：供道具等插件校验并改写概率函数表达式）、
决斗挑衅服务（DuelProvokeService：供道具等插件强制对方接受决斗，
并由机器人代替对方发送猜拳表情）、决斗点数缩放服务
（DuelMultiplierService：供道具等插件缩放进行中决斗的点数，缩放
到 0 时取消决斗并发布取消事件）与决斗取消服务
（DuelCancelService：供道具等插件主动取消进行中的决斗并发布
取消事件）、决斗认输服务（DuelForfeitService：供道具等插件让进行中
的决斗跳过出拳阶段、按指定一方落败直接结算）与决斗手势服务
（DuelGestureService：供道具等插件注册单组手势判定规则与整局判定
效果，可同时提高成员在一场决斗中需要发送的猜拳表情数量，如
“特殊手势”与“强运”效果）；
机器人名单（BOT_LIST）由 _shared/config/
的共享配置提供，供各插件共享；群聊消息发送、群昵称获取与截断等通用
逻辑复用 _shared/onebot/ 的共享工具。

决斗发起全局限频：全部群聊合计每小时最多发起 DUEL_HOURLY_LIMIT 次
决斗（默认 20，见 limit.py）；@ 机器人自己时机器人按接受概率函数
（DUEL_BOT_ACCEPT_FUNC，默认 1/点数）掷骰决定是否接受决斗，接受后
发送猜拳表情；决斗状态保存在内存中，存在时间超过 DUEL_DURATION
（分钟，默认 10）后自动超时结束；超时提示保留 1 分钟，仅在机器人于
该群发言后延迟 1 秒跟随发出，未能跟随则静默丢弃。

本插件为包子包结构（由原单文件 duel.py 拆分而来），本模块只负责插件
元数据、跨插件服务注册与启动日志：

- config/：配置模型与解析后的数值（model.py、values.py）、可配置文案
  与 text()（texts.py）、机器人接受概率表达式的沙箱校验
  （accept_expr.py）；
- core/：决斗值对象与内存存储（duel.py、store.py）、猜拳表情解析
  （gestures.py）、三层判定引擎与效果注册（judgement.py）；
- flow/：结算与结果播报（settle.py）、机器人出手（botplay.py）；
- messaging/：群消息与合并转发发送（send.py）、超时提示延迟跟发
  （deferred.py）；
- services/：对外提供的跨插件服务实现（事件、分数、状态、手势、动作、
  接受概率），服务实例由本模块统一注册；
- commands/：六个群聊指令的参数解析与处理器（args.py、challenge.py、
  response.py、rank.py、score.py、status.py）；
- runtime.py：猜拳表情消息处理器与超时检查定时任务；
- limit.py、models.py：发起限频（全局滑动窗口）与分数表模型。
"""

from nonebot import logger
from nonebot.plugin import PluginMetadata

from src.plugins._shared.config import NICKNAME_MAX_LENGTH, get_bot_list
from src.plugins._shared.services import (
    DuelBotAcceptService,
    DuelCancelService,
    DuelEventService,
    DuelForfeitService,
    DuelGestureService,
    DuelMultiplierService,
    DuelProvokeService,
    DuelScoreInterceptService,
    DuelScoreService,
    DuelSettleService,
    DuelStateService,
    register_service,
)
from src.plugins.duel import commands, models, runtime
from src.plugins.duel.config import DURATION_MINUTES, HOURLY_LIMIT, MAX_MULTIPLIER
from src.plugins.duel.services.actions import (
    cancel_service,
    forfeit_service,
    multiplier_service,
    provoke_service,
)
from src.plugins.duel.services.bot_accept import bot_accept_service
from src.plugins.duel.services.events import event_service, settle_service
from src.plugins.duel.services.gestures import gesture_service
from src.plugins.duel.services.scores import intercept_service, score_service
from src.plugins.duel.services.state import state_service

__plugin_meta__ = PluginMetadata(
    name="猜拳决斗",
    description="在群聊中发起猜拳决斗，由机器人判定胜负并按群记录分数，支持分数排行榜与分数查询",
    usage=(
        "/duel @成员 [点数]：向群成员发起决斗（点数默认为 1）\n"
        "/duel.accept @成员：接受对方的决斗邀请\n"
        "/duel.reject @成员：拒绝对方的决斗邀请\n"
        "/duel.rank (high|h (<条数>)) (low|l (<条数>))：查看本群分数排行榜"
        "（合并转发发送）\n"
        "/duel.score：查看自己在本群的决斗分数\n"
        "/duel.status：查看自己在本群的决斗状态"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 将决斗能力注册为跨插件服务（契约见 _shared/services/），
# 供签到、道具等插件使用
register_service(DuelScoreService, score_service)
register_service(DuelScoreInterceptService, intercept_service)
register_service(DuelStateService, state_service)
register_service(DuelEventService, event_service)
register_service(DuelSettleService, settle_service)
register_service(DuelBotAcceptService, bot_accept_service)
register_service(DuelProvokeService, provoke_service)
register_service(DuelMultiplierService, multiplier_service)
register_service(DuelCancelService, cancel_service)
register_service(DuelForfeitService, forfeit_service)
register_service(DuelGestureService, gesture_service)

# 机器人名单（共享配置 BOT_LIST，见 _shared/config/bot_list.py）：
# 不参与决斗（挑战时提示不能向 BOT 发起）的机器人 QQ 号，所有群通用
_bot_list = get_bot_list()

logger.info(
    f"猜拳决斗已启用，决斗超时时间为 {DURATION_MINUTES:g} 分钟，"
    f"群昵称最大显示长度为 {NICKNAME_MAX_LENGTH} 字符，"
    f"点数上限为 {MAX_MULTIPLIER}，"
    f"发起限频为每小时最多 {HOURLY_LIMIT} 次（全部群聊合计）"
)
if _bot_list:
    logger.info(
        f"机器人名单已配置（共享配置 BOT_LIST，所有群通用）：{sorted(_bot_list)}"
    )

# commands 与 runtime 在导入时注册指令、猜拳消息处理器与定时任务；
# models 在导入时完成 ORM 模型注册
__all__ = ["commands", "models", "runtime"]
