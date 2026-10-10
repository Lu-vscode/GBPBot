"""决斗分数存储与分数服务。

分数数据按群隔离，使用 localstore 长期存储在本地（scores.json），
数据结构为：群号 -> {成员 QQ 号 -> {"name": 最近使用的群昵称,
"score": 分数}}。文件不存在或损坏时视为空数据；旧版全局格式（不含
群号）的记录会被忽略。写入时昵称统一按 NICKNAME_MAX_LENGTH 截断，
与消息展示一致。

本模块同时实现对外提供的决斗分数服务（DuelScoreService）：其它插件
经服务注册中心调用它增减分数、查询分数与高/低分榜第一名；服务实例
score_service 由插件主模块统一注册。
"""

import json
from typing import Any

from nonebot import logger
from nonebot_plugin_localstore import get_plugin_data_file

from src.plugins._shared.onebot import truncate_name
from src.plugins._shared.services import DuelScoreService
from src.plugins.duel._config import NICKNAME_MAX_LENGTH

# 分数数据存储文件（localstore 插件数据目录）
_SCORE_FILE = get_plugin_data_file("scores.json")


def _normalize_score_record(
    user_id: Any, record: Any
) -> tuple[int, dict[str, Any]] | None:
    """校验分数数据中的单条成员记录，无效时返回 None。"""
    if not isinstance(user_id, str) or not user_id.isdigit():
        return None
    if not isinstance(record, dict):
        return None
    score = record.get("score")
    if isinstance(score, bool) or not isinstance(score, int):
        return None
    return int(user_id), {"name": str(record.get("name") or ""), "score": score}


def _is_legacy_group_data(group_data: Any) -> bool:
    """判断群数据是否为旧版全局分数格式（值为成员记录而非群映射）。"""
    return isinstance(group_data, dict) and (
        "score" in group_data or "name" in group_data
    )


def _normalize_group_record(
    group_id: Any, group_data: Any
) -> tuple[int, dict[int, dict[str, Any]]] | None:
    """校验分数数据中的单个群记录，返回 (群号, 群内成员分数数据)。"""
    if not isinstance(group_id, str) or not group_id.isdigit():
        return None
    if not isinstance(group_data, dict):
        return None
    members = [
        item
        for item in (
            _normalize_score_record(user_id, record)
            for user_id, record in group_data.items()
        )
        if item is not None
    ]
    if not members:
        return None
    return int(group_id), dict(members)


def _load_scores() -> dict[int, dict[int, dict[str, Any]]]:
    """从本地文件中读取分数数据，文件不存在或损坏时返回空数据。"""
    if not _SCORE_FILE.exists():
        return {}
    try:
        raw = json.loads(_SCORE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"读取决斗分数数据失败，本次将视为无分数数据：{exc}")
        return {}
    if not isinstance(raw, dict):
        logger.warning("决斗分数数据格式异常（应为 JSON 对象），本次将视为无分数数据")
        return {}
    if any(_is_legacy_group_data(group_data) for group_data in raw.values()):
        # 旧版全局格式的记录（键为成员 QQ 号，值含 score/name）：
        # 不含群号，按群隔离后无法归属，直接忽略
        logger.warning(
            "决斗分数数据中存在旧版全局格式的记录（不含群号），按群隔离后无法归属，本次已忽略"
        )
    normalized = [
        item
        for item in (
            _normalize_group_record(group_id, group_data)
            for group_id, group_data in raw.items()
        )
        if item is not None
    ]
    return dict(normalized)


def _save_scores() -> None:
    """将分数数据写入本地文件，写入失败时仅记录错误。"""
    try:
        _SCORE_FILE.write_text(
            json.dumps(_scores, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.error(f"写取决斗分数数据失败，本次修改未保存：{exc}")


# 分数数据：群号 -> {成员 QQ 号 -> {"name": 最近使用的群昵称, "score": 分数}}
_scores = _load_scores()
if _scores:
    _member_count = sum(len(members) for members in _scores.values())
    logger.info(f"已加载决斗分数数据，共 {len(_scores)} 个群、{_member_count} 名成员")


def update_score(group_id: int, user_id: int, name: str, delta: int) -> None:
    """更新指定群内成员的分数与昵称（昵称按显示规范截断）并写入本地文件。"""
    group = _scores.setdefault(group_id, {})
    record = group.setdefault(user_id, {"name": name, "score": 0})
    record["name"] = truncate_name(name, NICKNAME_MAX_LENGTH)
    record["score"] = int(record["score"]) + delta
    _save_scores()


def rank_entries(group_id: int, *, positive: bool) -> list[tuple[int, str, int]]:
    """返回指定群的排行榜条目 (QQ 号, 昵称, 分数)，昵称过长时截断。

    高分榜为分数为正的成员按分数从高到低排列；低分榜为分数为负的成员
    按分数绝对值从高到低排列；分数相同时按 QQ 号升序排列。
    """
    entries = [
        (user_id, truncate_name(str(record["name"]), NICKNAME_MAX_LENGTH), score)
        for user_id, record in _scores.get(group_id, {}).items()
        if (score := int(record["score"])) and (score > 0) == positive
    ]
    if positive:
        entries.sort(key=lambda entry: (-entry[2], entry[0]))
    else:
        entries.sort(key=lambda entry: (-abs(entry[2]), entry[0]))
    return entries


class _DuelScoreService(DuelScoreService):
    """决斗分数服务的实现：其它插件经服务注册中心调用它增减分数。"""

    def add_score(self, group_id: int, user_id: int, name: str, delta: int) -> int:
        """更新成员分数（名字按显示规范截断），返回更新后的分数。"""
        update_score(group_id, user_id, name, delta)
        return int(_scores[group_id][user_id]["score"])

    def get_score(self, group_id: int, user_id: int) -> int:
        """查询成员在本群的分数，无记录时返回 0。"""
        record = _scores.get(group_id, {}).get(user_id)
        if record is None:
            return 0
        return int(record["score"])

    def lowest_member(self, group_id: int) -> int | None:
        """返回本群决斗低分榜第一名成员 QQ 号，没有负分成员时返回 None。

        判定与 /duel.rank 的低分榜一致（复用同一排列）。
        """
        entries = rank_entries(group_id, positive=False)
        return entries[0][0] if entries else None

    def highest_member(self, group_id: int) -> int | None:
        """返回本群决斗高分榜第一名成员 QQ 号，没有正分成员时返回 None。

        判定与 /duel.rank 的高分榜一致（复用同一排列）。
        """
        entries = rank_entries(group_id, positive=True)
        return entries[0][0] if entries else None

    def rank_entries(self, group_id: int) -> list[tuple[int, str, int]]:
        """返回本群决斗分数总榜条目：高分榜（正分降序）与反转的低分榜
        （负分降序）拼接，分数相同时按 QQ 号升序排列。
        """
        negative = rank_entries(group_id, positive=False)
        # 低分榜原为绝对值降序（分数升序），反转为分数降序后再拼接
        negative.sort(key=lambda entry: (-entry[2], entry[0]))
        return rank_entries(group_id, positive=True) + negative


score_service = _DuelScoreService()
