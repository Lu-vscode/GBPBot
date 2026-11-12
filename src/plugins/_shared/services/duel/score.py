"""决斗分数服务与分数改变拦截（由猜拳决斗插件提供）。"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class DuelScoreService(Protocol):
    """决斗分数服务（由猜拳决斗插件提供）。"""

    async def add_score(
        self, group_id: int, user_id: int, name: str, delta: int
    ) -> int:
        """更新指定群内成员的决斗分数并写入数据库，返回更新后的分数。

        参数:
            group_id: 群号。
            user_id: 成员 QQ 号。
            name: 成员在群内的显示名（提供方按自身的显示规范处理）。
            delta: 分数变化量，可为负数。
        """
        ...

    async def get_score(self, group_id: int, user_id: int) -> int:
        """查询指定群内成员的决斗分数，无记录时返回 0。

        参数:
            group_id: 群号。
            user_id: 成员 QQ 号。
        """
        ...

    async def add_scores(
        self, group_id: int, entries: Sequence[tuple[int, str, int]]
    ) -> None:
        """批量更新多名成员的决斗分数，全部变化在同一个事务内写入。

        语义等价于按顺序逐个调用 add_score，但只写一次数据库；各成员的
        变化量仍会依次经过分数改变拦截器链。用于「五谷丰登」等一次
        影响全群成员的道具效果。

        参数:
            group_id: 群号。
            entries: (成员 QQ 号, 显示名, 分数变化量) 序列。
        """
        ...

    async def lowest_member(self, group_id: int) -> int | None:
        """返回指定群决斗低分榜第一名成员的 QQ 号，没有负分成员时返回 None。

        低分榜的判定与 /duel.rank 的低分榜一致：分数为负的成员按
        分数从低到高排列（即绝对值从大到小），分数相同时按 QQ 号
        升序排列；只有排在最前的成员是第一名。

        参数:
            group_id: 群号。
        """
        ...

    async def highest_member(self, group_id: int) -> int | None:
        """返回指定群决斗高分榜第一名成员的 QQ 号，没有正分成员时返回 None。

        高分榜的判定与 /duel.rank 的高分榜一致：分数为正的成员按
        分数从高到低排列，分数相同时按 QQ 号升序排列；只有排在最前
        的成员是第一名。

        参数:
            group_id: 群号。
        """
        ...

    async def rank_entries(self, group_id: int) -> list[tuple[int, str, int]]:
        """返回指定群决斗分数总榜的全部条目 (QQ 号, 昵称, 分数)。

        总榜为决斗高分榜与反转的决斗低分榜的拼接：全部非零分数的
        成员按分数从高到低排列，分数相同时按 QQ 号升序排列；0 分或
        没有分数记录的成员不在榜上。

        参数:
            group_id: 群号。
        """
        ...


ScoreInterceptor = Callable[[int, int, str, int], Awaitable[int]]
"""分数改变拦截器：接收 (群号, 成员 QQ 号, 显示名, 待生效的变化量)，
返回实际生效的变化量（返回 0 表示本次改变被抵消、分数不变）。"""


@runtime_checkable
class DuelScoreInterceptService(Protocol):
    """决斗分数拦截服务（由猜拳决斗插件提供）。"""

    def add_interceptor(self, interceptor: ScoreInterceptor) -> None:
        """注册分数改变拦截器（同一拦截器重复注册会被忽略）。

        拦截器在分数写入前按注册顺序依次等待：每个拦截器接收上一个
        拦截器改写后的变化量，返回自己改写后的结果（返回 0 表示本次
        改变被抵消、分数不变）；全部决斗分数写入（决斗结算、道具效果
        等）都会经过该拦截点。拦截器为异步回调，应保持快捷；单个
        拦截器抛出的异常只记录错误日志、本次改写不生效，不影响分数
        写入与其它拦截器。

        参数:
            interceptor: 分数改变拦截器。
        """
        ...
