"""决斗指令（包入口）：导入各指令模块即完成命令注册。

- args.py：被 @ 成员的解析与群昵称显示名；
- challenge.py：/duel 发起决斗（含点数解析、发起限频与 challenge 事件）；
- response.py：/duel.accept 与 /duel.reject 响应邀请；
- rank.py：/duel.rank 本群分数排行榜；
- score.py：/duel.score 分数查询；
- status.py：/duel.status 决斗状态查询。

各指令模块在导入时创建 matcher；插件主模块导入本包即完成全部注册。
"""

from src.plugins.duel.commands import (
    args,
    challenge,
    rank,
    response,
    score,
    status,
)

__all__ = ["args", "challenge", "rank", "response", "score", "status"]
