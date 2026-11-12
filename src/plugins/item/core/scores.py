"""决斗分数服务的桥接（供道具效果增减分数或查询排行榜）。

加载时经 require 声明依赖决斗插件，并取得决斗分数服务（见
_shared/services/）：道具效果可增减分数、查询分数、查询高/低分榜第一名
与总榜条目。
"""

from collections.abc import Sequence

from nonebot.plugin import require

from src.plugins._shared.services import DuelScoreService, require_service

# 加载时依赖声明：道具效果可经决斗分数服务增减分数或查询低分榜第一名
# （见 _shared/services/）。require 保证决斗插件先完成加载、分数服务已
# 注册；服务缺失时插件加载失败并给出明确日志
require("duel")
_score_service = require_service(DuelScoreService)


async def get_user_score(group_id: int, user_id: int) -> int:
    """查询成员在指定群的决斗分数（供无使用上下文的场景调用）。"""
    return await _score_service.get_score(group_id, user_id)


async def get_lowest_score_user(group_id: int) -> int | None:
    """查询指定群决斗低分榜第一名的成员 QQ 号（无负分成员时返回 None）。

    低分榜的判定与决斗插件的 /duel.rank 低分榜一致；供道具模块校验
    "位列决斗低分榜第一名"等使用条件。
    """
    return await _score_service.lowest_member(group_id)


async def get_highest_score_user(group_id: int) -> int | None:
    """查询指定群决斗高分榜第一名的成员 QQ 号（无正分成员时返回 None）。

    高分榜的判定与决斗插件的 /duel.rank 高分榜一致；供道具模块校验
    "位列决斗高分榜第一名"等使用条件。
    """
    return await _score_service.highest_member(group_id)


async def get_rank_entries(group_id: int) -> list[tuple[int, str, int]]:
    """查询指定群决斗分数总榜的全部条目 (QQ 号, 昵称, 分数)。

    总榜为决斗高分榜与反转的决斗低分榜的拼接：全部非零分数的成员
    按分数从高到低排列、同分按 QQ 号升序排列；0 分或没有分数记录的
    成员不在榜上；供道具模块遍历全部非零分数成员（如全体加 1 分、
    随机抽选）或按榜上相邻位置处理（如交换分数）。
    """
    return await _score_service.rank_entries(group_id)


async def add_user_score(
    group_id: int, user_id: int, user_name: str, delta: int
) -> int:
    """经决斗分数服务为成员增减决斗分数，返回更新后的分数。

    供状态到期回调等没有 ItemUseContext 的场景使用。
    """
    return await _score_service.add_score(group_id, user_id, user_name, delta)


async def add_user_scores(
    group_id: int, entries: Sequence[tuple[int, str, int]]
) -> None:
    """批量增减多名成员的决斗分数，同一个事务内写入。

    entries 为 (成员 QQ 号, 显示名, 分数变化量) 序列，按顺序应用；
    各成员的变化量仍会依次经过分数改变拦截器链。供「五谷丰登」等
    一次影响全群成员的道具效果使用。
    """
    if entries:
        await _score_service.add_scores(group_id, entries)
