"""决斗分数存储与分数服务。

分数数据按群隔离保存在数据库（`gbp_duel_score` 表，见 models.py），
主键为 (群号, 成员 QQ 号)，昵称为最近一次写入时的显示名。每次写入都是
单事务：先经分数改变拦截器链改写实际生效的变化量，再以 upsert 写入并
读回末态分数。写入时昵称统一按 NICKNAME_MAX_LENGTH 截断，与消息展示一致。

本模块同时实现对外提供的决斗分数服务（DuelScoreService）：其它插件
经服务注册中心调用它增减分数、查询分数与高/低分榜第一名；分数写入前
会先经分数改变拦截器链改写实际生效的变化量（决斗分数拦截服务
DuelScoreInterceptService，供道具等插件抵消分数改变）。两个服务实例
score_service、intercept_service 由插件主模块统一注册。
"""

from collections.abc import Sequence

from nonebot import logger
from nonebot_plugin_orm import get_session
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.plugins._shared.onebot import truncate_name
from src.plugins._shared.services import (
    DuelScoreInterceptService,
    DuelScoreService,
    ScoreInterceptor,
)
from src.plugins.duel.models import DuelScore

# 分数改变拦截器链：由 _DuelScoreInterceptService 维护，写入前依次应用
# （每个拦截器接收上一步的结果并返回改写后的变化量）
_interceptors: list[ScoreInterceptor] = []
_INTERCEPTOR_ERRORS = (Exception,)


class _DuelScoreInterceptService(DuelScoreInterceptService):
    """决斗分数拦截服务的实现：管理分数改变拦截器链。"""

    def add_interceptor(self, interceptor: ScoreInterceptor) -> None:
        """注册分数改变拦截器（同一拦截器重复注册会被忽略）。"""
        if interceptor not in _interceptors:
            _interceptors.append(interceptor)


# 拦截服务实例（由插件主模块统一注册）
intercept_service = _DuelScoreInterceptService()


async def _apply_interceptors(
    group_id: int, user_id: int, name: str, delta: int
) -> int:
    """应用拦截器链：依次改写实际生效的变化量。

    单个拦截器抛出的异常只记录错误日志，本次改写不生效（沿用上一步
    的结果），不影响分数写入与其它拦截器。
    """
    for interceptor in tuple(_interceptors):
        try:
            delta = await interceptor(group_id, user_id, name, delta)
        except _INTERCEPTOR_ERRORS as exc:
            logger.error(f"分数改变拦截器处理时出错：{exc}")
    return delta


def _score_insert(group_id: int, user_id: int, name: str, delta: int):
    """构造分数 upsert：变化量在数据库端累加，昵称取本次传入值。"""
    display_name = truncate_name(name)
    statement = sqlite_insert(DuelScore).values(
        group_id=group_id,
        user_id=user_id,
        name=display_name,
        score=delta,
    )
    return statement.on_conflict_do_update(
        index_elements=[DuelScore.group_id, DuelScore.user_id],
        set_={"name": display_name, "score": DuelScore.score + delta},
    )


async def _apply_score(
    session: AsyncSession, group_id: int, user_id: int, name: str, delta: int
) -> int:
    """在一个事务内写入一条分数变化，返回更新后的分数。

    变化量先经拦截器链改写实际生效值（如「无懈可击」把一次改变
    改为 0 完成抵消）。
    """
    delta = await _apply_interceptors(group_id, user_id, name, delta)
    await session.execute(_score_insert(group_id, user_id, name, delta))
    score = await session.scalar(
        select(DuelScore.score).where(
            DuelScore.group_id == group_id,
            DuelScore.user_id == user_id,
        )
    )
    return 0 if score is None else int(score)


async def update_score(group_id: int, user_id: int, name: str, delta: int) -> int:
    """更新指定群内成员的分数与昵称（昵称按显示规范截断），返回更新后的分数。

    写入前先经拦截器链改写实际生效的变化量（如「无懈可击」把一次
    改变改为 0 完成抵消）。
    """
    async with get_session() as session, session.begin():
        return await _apply_score(session, group_id, user_id, name, delta)


async def update_scores(group_id: int, entries: Sequence[tuple[int, str, int]]) -> None:
    """批量更新多名成员的分数与昵称，全部变化在同一个事务内写入。

    各条变化按顺序应用并依次经过拦截器链，语义等价于逐个调用
    update_score；空序列不写库。
    """
    if not entries:
        return
    async with get_session() as session, session.begin():
        for user_id, name, delta in entries:
            await _apply_score(session, group_id, user_id, name, delta)


async def rank_entries(group_id: int, *, positive: bool) -> list[tuple[int, str, int]]:
    """返回指定群的排行榜条目 (QQ 号, 昵称, 分数)，昵称过长时截断。

    高分榜为分数为正的成员按分数从高到低排列；低分榜为分数为负的成员
    按分数从低到高排列（即绝对值从高到低）；分数相同时按 QQ 号升序排列。
    """
    statement = select(DuelScore.user_id, DuelScore.name, DuelScore.score).where(
        DuelScore.group_id == group_id
    )
    if positive:
        statement = statement.where(DuelScore.score > 0).order_by(
            DuelScore.score.desc(), DuelScore.user_id
        )
    else:
        statement = statement.where(DuelScore.score < 0).order_by(
            DuelScore.score, DuelScore.user_id
        )
    async with get_session() as session:
        rows = (await session.execute(statement)).all()
    return [
        (int(user_id), truncate_name(str(name)), int(score))
        for user_id, name, score in rows
    ]


async def total_score(group_id: int) -> int:
    """返回指定群全部成员决斗分数的总和（无分数记录时为 0）。

    没有分数记录的成员视为 0 分，因此有记录成员的分数之和即为本群
    全部群成员的分数总和。
    """
    statement = select(func.coalesce(func.sum(DuelScore.score), 0)).where(
        DuelScore.group_id == group_id
    )
    async with get_session() as session:
        total = await session.scalar(statement)
    return 0 if total is None else int(total)


class _DuelScoreService(DuelScoreService):
    """决斗分数服务的实现：其它插件经服务注册中心调用它增减分数。"""

    async def add_score(
        self, group_id: int, user_id: int, name: str, delta: int
    ) -> int:
        """更新成员分数（名字按显示规范截断），返回更新后的分数。"""
        return await update_score(group_id, user_id, name, delta)

    async def get_score(self, group_id: int, user_id: int) -> int:
        """查询成员在本群的分数，无记录时返回 0。"""
        statement = select(DuelScore.score).where(
            DuelScore.group_id == group_id,
            DuelScore.user_id == user_id,
        )
        async with get_session() as session:
            score = await session.scalar(statement)
        return 0 if score is None else int(score)

    async def add_scores(
        self, group_id: int, entries: Sequence[tuple[int, str, int]]
    ) -> None:
        """批量更新成员分数（名字按显示规范截断），同一个事务内写入。"""
        await update_scores(group_id, entries)

    async def lowest_member(self, group_id: int) -> int | None:
        """返回本群决斗低分榜第一名成员 QQ 号，没有负分成员时返回 None。

        判定与 /duel.rank 的低分榜一致（复用同一排列）。
        """
        entries = await rank_entries(group_id, positive=False)
        return entries[0][0] if entries else None

    async def highest_member(self, group_id: int) -> int | None:
        """返回本群决斗高分榜第一名成员 QQ 号，没有正分成员时返回 None。

        判定与 /duel.rank 的高分榜一致（复用同一排列）。
        """
        entries = await rank_entries(group_id, positive=True)
        return entries[0][0] if entries else None

    async def rank_entries(self, group_id: int) -> list[tuple[int, str, int]]:
        """返回本群决斗分数总榜条目：高分榜（正分降序）与反转的低分榜
        （负分降序）拼接，分数相同时按 QQ 号升序排列。
        """
        negative = await rank_entries(group_id, positive=False)
        # 低分榜原为分数升序，反转为分数降序后再拼接
        negative.sort(key=lambda entry: (-entry[2], entry[0]))
        return await rank_entries(group_id, positive=True) + negative


score_service = _DuelScoreService()
