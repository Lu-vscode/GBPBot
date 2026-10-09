"""猜拳决斗插件。

在群聊中发起猜拳决斗：`/duel @成员 [点数]` 发起（点数默认 1，上限由
DUEL_MAX_MULTIPLIER 配置，默认 100），被 @ 的成员可通过
`/duel.accept @成员` 接受或 `/duel.reject @成员` 拒绝；接受后双方发送
QQ"包剪锤"表情，机器人根据双方手势判定胜负：胜者分数 +点数、负者分数
-点数，平局分数不变；各群分数相互独立，分数数据使用 localstore 长期
存储在本地，并且作为"决斗分数服务"经跨插件服务注册中心（见
_shared/services.py）供其它插件增减分数与查询高/低分榜第一名；
`/duel.rank` 可查看本群分数排行榜（以合并转发发送），`/duel.score`
可查看自己在本群的决斗分数，`/duel.status` 可查看自己在本群的决斗
状态。除分数服务外，还对外提供决斗状态服务
（DuelStateService）、决斗事件服务（DuelEventService：在发起、接受、
拒绝、出拳、结算、超时、取消等环节发布事件，供道具等插件联动，
settling 事件的处理器可接管结算），机器人接受概率函数服务
（DuelBotAcceptService：供道具等插件校验并改写概率函数表达式）、
决斗挑衅服务（DuelProvokeService：供道具等插件强制对方接受决斗，
并由机器人代替对方发送猜拳表情）与决斗点数缩放服务
（DuelMultiplierService：供道具等插件缩放进行中决斗的点数，缩放
到 0 时取消决斗并发布取消事件）；
机器人名单（BOT_LIST）由 _shared/config.py
的共享配置提供，供各插件共享；群聊消息发送、群昵称获取与截断等通用
逻辑复用 _shared/onebot.py 的共享工具。

@ 机器人自己时机器人按接受概率函数（DUEL_BOT_ACCEPT_FUNC，默认 1/点数）
掷骰决定是否接受决斗，接受后发送猜拳表情；决斗状态保存在内存中，存在
时间超过 DUEL_DURATION（分钟，默认 10）后自动超时结束；超时提示保留
1 分钟，仅在机器人于该群发言后延迟 1 秒跟随发出，未能跟随则静默丢弃。
"""

import asyncio
import inspect
import json
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nonebot import get_bots, get_plugin_config, logger, on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    Message,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import is_type
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_localstore import get_plugin_data_file
from pydantic import BaseModel, field_validator

from src.plugins._shared.config import get_bot_list
from src.plugins._shared.onebot import (
    member_display_name,
    send_group_forward,
    send_group_text,
    sender_display_name,
    truncate_name,
)
from src.plugins._shared.services import (
    DuelBotAcceptService,
    DuelEvent,
    DuelEventHandler,
    DuelEventKind,
    DuelEventService,
    DuelMultiplierService,
    DuelProvokeService,
    DuelScaleOutcome,
    DuelScoreService,
    DuelSnapshot,
    DuelStateService,
    register_service,
)

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

# 决斗超时时间的默认值（分钟）、排行榜的默认最大显示条数、
# bot 消息中群昵称的最大显示长度（字符数）、决斗点数上限
_DEFAULT_DURATION_MINUTES = 10.0
_DEFAULT_RANK_LIMIT = 10
_DEFAULT_NICKNAME_MAX_LENGTH = 12
_DEFAULT_MAX_MULTIPLIER = 100

# 决斗超时的检查间隔（秒）：超时提示最多延迟该间隔
_TIMEOUT_CHECK_INTERVAL_SECONDS = 30

# 决斗超时提示的延迟发送：超时后在队列保留该时长（秒），期间机器人向同群
# 发送其它消息时在其后延迟该时长（秒）发出，保留期内未能跟随则静默丢弃
_TIMEOUT_MESSAGE_LINGER_SECONDS = 60.0
_TIMEOUT_MESSAGE_DELAY_SECONDS = 1.0

# QQ"包剪锤"表情的结果值（NapCat 等协议端直接透传 QQ 的 resultId）：
# 1 剪刀、2 石头、3 布，即按"剪刀石头布"顺序编号，与常见的
# "1 石头、2 剪刀、3 布"写法相反，胜负判定以实测值为准
_GESTURE_CHOICES = frozenset({1, 2, 3})
_GESTURE_TEXT_TO_VALUE = {"1": 1, "2": 2, "3": 3}

# 胜负判定：(挑战者手势, 接受者手势) 在该集合中表示挑战者胜，
# 其余非平局组合为接受者胜（剪刀胜布、石头胜剪刀、布胜石头）
_WINNING_GESTURES = frozenset({(1, 3), (2, 1), (3, 2)})

# "包剪锤"表情的 QQ 表情 ID：部分协议端可能以 face 段上报，作兼容处理
_RPS_FACE_ID = 359

# 文档中直接出现的文案对应的默认值，键与 DUEL_TEXT_* 配置一一对应
# （配置项名 = "duel_text_" + 键）
_TEXT_DEFAULTS = {
    "self": "不能向自己发起决斗。",
    "bot": "不能向BOT发起决斗。",
    "accepted": (
        "{accepter} 已接受 {challenger} 的决斗邀请（×{multiplier}）。"
        "\n请双方发送猜拳表情。"
    ),
    "challenged": "{challenger} 向 {opponent} 发起决斗（×{multiplier}）。",
    "rejected": "{rejecter} 已拒绝 {challenger} 的决斗邀请（×{multiplier}）。",
    "not_challenged": "{challenger} 未向你发起决斗。",
    "win": (
        "{winner} 在与 {loser} 的决斗（×{multiplier}）中获胜。"
        "{winner} 的分数+{multiplier}，{loser} 的分数-{multiplier}。决斗结束。"
    ),
    "draw": (
        "{player_a} 与 {player_b} 的决斗（×{multiplier}）平局。双方分数不变。决斗结束。"
    ),
    "timeout": "{player_a} 与 {player_b} 的决斗（×{multiplier}）超时结束。",
}

# 每条文案允许使用的占位符，启动时校验配置的文案与占位符是否匹配
_TEXT_FIELDS = {
    "self": (),
    "bot": (),
    "accepted": ("accepter", "challenger", "multiplier"),
    "challenged": ("challenger", "opponent", "multiplier"),
    "rejected": ("rejecter", "challenger", "multiplier"),
    "not_challenged": ("challenger",),
    "win": ("winner", "loser", "multiplier"),
    "draw": ("player_a", "player_b", "multiplier"),
    "timeout": ("player_a", "player_b", "multiplier"),
}

# 文档未定义的边界情况文案（用法与错误提示），不支持配置
_USAGE_ACCEPT = "请 @ 发起决斗的群成员，用法：/duel.accept @群成员"
_USAGE_REJECT = "请 @ 发起决斗的群成员，用法：/duel.reject @群成员"
_USAGE_RANK = (
    "参数无法识别，用法：/duel.rank (high|h (<条数>)) (low|l (<条数>))，"
    "条数需为正整数且默认为 10"
)
_USAGE_SCORE = "参数无法识别，用法：/duel.score"
_USAGE_STATUS = "参数无法识别，用法：/duel.status"
_STATUS_ALL_DENIED = "查看本群全部决斗状态仅超级用户可用。"
_PAIR_ACTIVE = "你们之间已有一场进行中的决斗，请等待该决斗结束或超时后再发起。"
_PROVOKED_ALREADY_STARTED = "你与 {challenger} 的决斗已经开始了，无需再接受。"
_PROVOKED_CANNOT_REJECT = "你与 {challenger} 的决斗已经开始了，无法再拒绝。"
_RANK_TITLE_HIGH = "决斗高分榜"
_RANK_TITLE_LOW = "决斗低分榜"
_RANK_EMPTY = "（暂无）"
# 排行榜合并转发中节点显示的发送者名称
_RANK_NODE_NAME = "决斗分数排行榜"


class Config(BaseModel):
    """猜拳决斗插件配置。

    可在 `.env.{environment}` 文件中通过 `DUEL_*` 系列变量配置，
    缺失或为空时使用默认行为（超时 10 分钟、群昵称最长 12 字符、
    点数上限 100、接受概率 1/点数、内置文案）；机器人名单见
    BOT_LIST 共享配置（_shared/config.py）。
    """

    duel_duration: float | None = None
    """决斗的存在时间上限（分钟），超过后自动超时结束，默认 10。"""

    duel_nickname_max_length: int | None = None
    """bot 消息中群昵称的最大显示长度（字符数），超过时截断并以"…"结尾，默认 12。"""

    duel_max_multiplier: int | None = None
    """决斗点数的上限（正整数），默认 100。"""

    duel_bot_accept_func: str | None = None
    """机器人接受决斗的概率函数表达式（变量 x 为点数），默认 1/x。"""

    duel_text_self: str | None = None
    """向自己发起决斗时的提示文案。"""

    duel_text_bot: str | None = None
    """向决斗机器人名单中的机器人发起决斗时的提示文案。"""

    duel_text_accepted: str | None = None
    """接受决斗邀请的提示文案，占位符 {accepter}、{challenger}。"""

    duel_text_challenged: str | None = None
    """发起决斗等待对方接受时的提示文案，占位符 {challenger}、{opponent}。"""

    duel_text_rejected: str | None = None
    """拒绝决斗邀请的提示文案，占位符 {rejecter}、{challenger}。"""

    duel_text_not_challenged: str | None = None
    """对方未向自己发起决斗时的提示文案，占位符 {challenger}。"""

    duel_text_win: str | None = None
    """决出胜负时的提示文案，占位符 {winner}、{loser}。"""

    duel_text_draw: str | None = None
    """平局时的提示文案，占位符 {player_a}、{player_b}。"""

    duel_text_timeout: str | None = None
    """决斗超时结束时的提示文案，占位符 {player_a}、{player_b}。"""

    @field_validator(
        "duel_duration",
        "duel_nickname_max_length",
        "duel_max_multiplier",
        "duel_bot_accept_func",
        mode="before",
    )
    @classmethod
    def blank_as_none(cls, value: Any) -> Any:
        """将空字符串视为未配置，避免变量留空导致插件加载失败。"""
        if isinstance(value, str) and not value.strip():
            return None
        return value


plugin_config = get_plugin_config(Config)


def _duration_minutes_or_default(value: float | None) -> float:
    """返回决斗超时时间（分钟），未配置或非正数时使用默认值。"""
    if value is None:
        return _DEFAULT_DURATION_MINUTES
    if value <= 0:
        logger.warning(
            f"猜拳决斗配置 DUEL_DURATION={value} 无效（需为正数，单位分钟），"
            f"已使用默认值 {_DEFAULT_DURATION_MINUTES} 分钟"
        )
        return _DEFAULT_DURATION_MINUTES
    return float(value)


def _nickname_max_length_or_default(value: int | None) -> int:
    """返回 bot 消息中群昵称的最大显示长度，未配置或非正数时使用默认值。"""
    if value is None:
        return _DEFAULT_NICKNAME_MAX_LENGTH
    if value <= 0:
        logger.warning(
            f"猜拳决斗配置 DUEL_NICKNAME_MAX_LENGTH={value} 无效（需为正整数），"
            f"已使用默认值 {_DEFAULT_NICKNAME_MAX_LENGTH}"
        )
        return _DEFAULT_NICKNAME_MAX_LENGTH
    return value


_duration_minutes = _duration_minutes_or_default(plugin_config.duel_duration)
# 决斗超时时间（秒）
_duration_seconds = _duration_minutes * 60.0

# bot 消息中群昵称的最大显示长度（字符数）
_nickname_max_length = _nickname_max_length_or_default(
    plugin_config.duel_nickname_max_length
)


def _max_multiplier_or_default(value: int | None) -> int:
    """返回决斗点数的上限，未配置或非正数时使用默认值。"""
    if value is None:
        return _DEFAULT_MAX_MULTIPLIER
    if value <= 0:
        logger.warning(
            f"猜拳决斗配置 DUEL_MAX_MULTIPLIER={value} 无效（需为正整数），"
            f"已使用默认值 {_DEFAULT_MAX_MULTIPLIER}"
        )
        return _DEFAULT_MAX_MULTIPLIER
    return value


_max_multiplier = _max_multiplier_or_default(plugin_config.duel_max_multiplier)


def _default_bot_accept_probability(multiplier: int) -> float:
    """默认的机器人接受决斗概率函数：接受概率为 1/点数。"""
    return 1 / multiplier


# 表达式求值阶段的异常捕获集合（编译阶段另行捕获 SyntaxError/ValueError）
_ACCEPT_EXPR_ERRORS = (
    ArithmeticError,
    AttributeError,
    NameError,
    TypeError,
    ValueError,
)


def _accept_expr_error(expr: str) -> str | None:
    """校验接受概率表达式在全部可选点数上都能求值为 0~1 的浮点数。

    表达式为 Python 表达式（变量 x 为决斗点数，在受限命名空间中求值），
    在点数取值范围 1..上限 内逐点检查；返回面向用户的错误文案，合法时
    返回 None。
    """
    try:
        code = compile(expr, "<DUEL_BOT_ACCEPT_FUNC>", "eval")
    except SyntaxError as exc:
        location = f"（第 {exc.offset} 个字符附近）" if exc.offset else ""
        return f"表达式无法解析：{exc.msg}{location}"
    except ValueError as exc:
        return f"表达式无法解析：{exc}"
    for x in range(1, _max_multiplier + 1):
        try:
            probability = float(eval(code, {"__builtins__": {}}, {"x": x}))
        except _ACCEPT_EXPR_ERRORS as exc:
            return f"表达式在点数 {x} 时求值失败：{exc}"
        if not 0.0 <= probability <= 1.0:
            return f"表达式在点数 {x} 时的值为 {probability}，不是 0~1 之间的概率"
    return None


def _bot_accept_func_or_default(expr: str | None) -> Callable[[int], float]:
    """解析机器人接受决斗的概率函数，无效时报错并使用默认函数。

    配置为 Python 表达式（变量 x 为点数）；当表达式无法求值，或在点数
    取值范围 1..上限 内存在小于 0 或大于 1 的概率时，记录错误并回退
    默认函数 1/点数。
    """
    text = (expr or "").strip()
    if not text:
        return _default_bot_accept_probability
    error = _accept_expr_error(text)
    if error is not None:
        logger.error(
            f"猜拳决斗配置 DUEL_BOT_ACCEPT_FUNC={text!r} 校验未通过"
            f"（{error}），已使用默认函数 1/点数"
        )
        return _default_bot_accept_probability
    code = compile(text, "<DUEL_BOT_ACCEPT_FUNC>", "eval")

    def accept_probability(multiplier: int) -> float:
        return float(eval(code, {"__builtins__": {}}, {"x": multiplier}))

    logger.info(f"机器人接受决斗的概率函数已配置：{text}")
    return accept_probability


# 机器人接受决斗的概率函数：点数 -> 接受概率
_bot_accept_probability = _bot_accept_func_or_default(
    plugin_config.duel_bot_accept_func
)

# /duel 的用法提示（点数上限可配置，需在解析配置后构建）
_USAGE_DUEL = (
    "请 @ 要发起决斗的群成员，用法：/duel @群成员 [数值]（如 /duel 2 @群成员），"
    f"数值需为不超过 {_max_multiplier} 的正整数，默认为 1"
)


# 机器人名单（共享配置 BOT_LIST，见 _shared/config.py）：
# 不参与决斗（挑战时提示不能向 BOT 发起）的机器人 QQ 号，所有群通用
_bot_list = get_bot_list()


def _text_or_default(key: str, configured: str | None) -> str:
    """校验并返回配置的文案，缺失或占位符格式无效时使用默认文案。"""
    default = _TEXT_DEFAULTS[key]
    text = (configured or "").strip()
    if not text:
        return default
    try:
        text.format(**dict.fromkeys(_TEXT_FIELDS[key], "占位"))
    except (IndexError, KeyError, ValueError) as exc:
        logger.warning(
            f"猜拳决斗配置 DUEL_TEXT_{key.upper()} 的占位符格式无效"
            f"（{exc}），已使用默认文案"
        )
        return default
    return text


# 全部决斗文案：文案键 -> 最终使用的文案（配置或默认值）
_texts = {
    key: _text_or_default(key, getattr(plugin_config, f"duel_text_{key}"))
    for key in _TEXT_DEFAULTS
}


def _text(key: str, **kwargs: Any) -> str:
    """取出文案并填充占位符。"""
    return _texts[key].format(**kwargs)


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


def _update_score(group_id: int, user_id: int, name: str, delta: int) -> None:
    """更新指定群内成员的分数与昵称并写入本地文件。"""
    group = _scores.setdefault(group_id, {})
    record = group.setdefault(user_id, {"name": name, "score": 0})
    record["name"] = name
    record["score"] = int(record["score"]) + delta
    _save_scores()


class _DuelScoreService(DuelScoreService):
    """决斗分数服务的实现：其它插件经服务注册中心调用它增减分数。"""

    def add_score(self, group_id: int, user_id: int, name: str, delta: int) -> int:
        """更新成员分数（名字按显示规范截断），返回更新后的分数。"""
        _update_score(group_id, user_id, _truncate_name(name), delta)
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
        entries = _rank_entries(group_id, positive=False)
        return entries[0][0] if entries else None

    def highest_member(self, group_id: int) -> int | None:
        """返回本群决斗高分榜第一名成员 QQ 号，没有正分成员时返回 None。

        判定与 /duel.rank 的高分榜一致（复用同一排列）。
        """
        entries = _rank_entries(group_id, positive=True)
        return entries[0][0] if entries else None

    def rank_entries(self, group_id: int) -> list[tuple[int, str, int]]:
        """返回本群决斗分数总榜条目：高分榜（正分降序）与反转的低分榜
        （负分降序）拼接，分数相同时按 QQ 号升序排列。
        """
        negative = _rank_entries(group_id, positive=False)
        # 低分榜原为绝对值降序（分数升序），反转为分数降序后再拼接
        negative.sort(key=lambda entry: (-entry[2], entry[0]))
        return _rank_entries(group_id, positive=True) + negative


@dataclass(eq=False)
class _Duel:
    """一场进行中的决斗，保存在内存中（相等判断请使用对象身份）。"""

    group_id: int
    """决斗所在群号。"""

    bot_self_id: int
    """处理该决斗的机器人 QQ 号。"""

    challenger_id: int
    """发起决斗的成员 QQ 号。"""

    challenger_name: str
    """发起决斗一方的群昵称（发起时记录，过长时已按上限截断）。"""

    opponent_id: int
    """接受决斗一方的成员 QQ 号（被 @ 的成员或机器人自己）。"""

    opponent_name: str
    """接受决斗一方的群昵称（发起时记录，过长时已按上限截断）。"""

    multiplier: int
    """决斗点数：胜者分数 +点数、败者分数 -点数。"""

    created_at: float
    """决斗创建时间（time.monotonic，用于超时判断）。"""

    accepted: bool = False
    """对方是否已接受（等待接受时为 False，机器人已掷骰接受时为 True）。"""

    provoked_by_bot: bool = False
    """是否被挑衅：该决斗中对方的手势由机器人代替（对方发送的猜拳表情无效）。"""

    gestures: dict[int, int] = field(default_factory=dict)
    """已发送的猜拳手势：成员 QQ 号 -> 手势结果（1 剪刀、2 石头、3 布）。"""


# 进行中的决斗，按创建顺序排列（同一成员多场决斗时优先满足旧的）
_duels: list[_Duel] = []


def _duel_snapshot(duel: _Duel) -> DuelSnapshot:
    """构造一场决斗的只读快照。"""
    now = time.monotonic()
    return DuelSnapshot(
        group_id=duel.group_id,
        challenger_id=duel.challenger_id,
        challenger_name=duel.challenger_name,
        opponent_id=duel.opponent_id,
        opponent_name=duel.opponent_name,
        multiplier=duel.multiplier,
        accepted=duel.accepted,
        gesture_users=frozenset(duel.gestures),
        remaining_seconds=max(0.0, duel.created_at + _duration_seconds - now),
    )


class _DuelStateService(DuelStateService):
    """决斗状态查询服务的实现：其它插件经服务注册中心查看进行中决斗。"""

    def list_duels(self, group_id: int) -> list[DuelSnapshot]:
        """返回指定群内全部进行中决斗的只读快照（按创建顺序）。"""
        return [_duel_snapshot(duel) for duel in _duels if duel.group_id == group_id]


# 决斗事件处理器与捕获集合：订阅方经 DuelEventService 增删处理器，
# 发布时按注册顺序逐个调用；单个处理器抛出的异常只记录错误日志，
# 不影响决斗流程与其它的处理器
_EVENT_HANDLER_ERRORS = (Exception,)
_event_handlers: list[DuelEventHandler] = []


class _DuelEventService(DuelEventService):
    """决斗事件服务的实现：订阅/注销决斗生命周期事件处理器。"""

    def subscribe(self, handler: DuelEventHandler) -> None:
        """注册事件处理器（同一处理器重复注册会被忽略）。"""
        if handler not in _event_handlers:
            _event_handlers.append(handler)

    def unsubscribe(self, handler: DuelEventHandler) -> None:
        """注销事件处理器（未注册时不做任何事）。"""
        if handler in _event_handlers:
            _event_handlers.remove(handler)


class _DuelBotAcceptService(DuelBotAcceptService):
    """机器人接受概率函数服务的实现：供道具等插件校验表达式。"""

    def validate_accept_func(self, expr: str) -> str | None:
        """校验概率函数表达式；额外禁止连续下划线（防外部输入逃逸）。"""
        text = expr.strip()
        if not text:
            return "表达式不能为空。"
        if "__" in text:
            return "表达式不允许包含连续的下划线“__”。"
        return _accept_expr_error(text)


class _DuelProvokeService(DuelProvokeService):
    """决斗挑衅服务的实现：供道具等插件强制对方接受决斗。"""

    async def accept_by_provoke(
        self, group_id: int, challenger_id: int, opponent_id: int
    ) -> DuelSnapshot | None:
        """强制对方接受决斗并标记对方手势由机器人代替，返回决斗快照。"""
        duel = _find_pending_duel(group_id, challenger_id, opponent_id)
        if duel is None:
            return None
        # 同步段完成状态标记：此后对方发送的猜拳表情被忽略、手势由机器人代替
        duel.accepted = True
        duel.provoked_by_bot = True
        logger.info(
            f"群 {group_id} 成员 {challenger_id} 挑衅 {opponent_id}，"
            f"决斗已强制接受（点数 {duel.multiplier}），对方手势将由机器人代替"
        )
        await _fire_duel_event(
            _make_duel_event(duel, DuelEventKind.ACCEPTED, actor_id=duel.opponent_id)
        )
        return _duel_snapshot(duel)

    async def send_provoked_gesture(
        self, bot: Bot, group_id: int, challenger_id: int, opponent_id: int
    ) -> None:
        """由机器人代替被挑衅决斗的对方发送猜拳表情并记录为对方手势。"""
        duel = _find_provoked_duel(group_id, challenger_id, opponent_id)
        if duel is None:
            logger.warning(
                f"群 {group_id} 待机器人代替出手的被挑衅决斗（{challenger_id} 与 "
                f"{opponent_id}）已不在进行中，未发送猜拳表情"
            )
            return
        await _send_bot_rps(bot, duel)


class _DuelMultiplierService(DuelMultiplierService):
    """决斗点数缩放服务的实现：供道具等插件缩放进行中决斗的点数。"""

    async def scale_multiplier(
        self,
        group_id: int,
        user_a: int,
        user_b: int,
        numerator: int,
        denominator: int,
    ) -> DuelScaleOutcome | None:
        """按比例缩放双方之间进行中决斗的点数；缩放到 0 时取消决斗。"""
        if numerator < 1 or denominator < 1:
            message = (
                f"缩放比例的分子与分母必须为正整数，收到 {numerator}/{denominator}"
            )
            raise ValueError(message)
        duel = _find_pair_duel(group_id, user_a, user_b)
        if duel is None:
            return None
        old = duel.multiplier
        new = old * numerator // denominator
        if new <= 0:
            # 同步段完成取消（从决斗列表移除），再发布取消事件
            _remove_duel(duel)
            logger.info(
                f"群 {group_id} 中 {duel.challenger_id} 与 {duel.opponent_id} "
                f"的决斗点数 {old} 被缩放为 0，决斗已取消"
            )
            await _fire_duel_event(
                _make_duel_event(duel, DuelEventKind.CANCELED, multiplier=0)
            )
            return DuelScaleOutcome(multiplier=0, canceled=True)
        duel.multiplier = new
        logger.info(
            f"群 {group_id} 中 {duel.challenger_id} 与 {duel.opponent_id} "
            f"的决斗点数由 {old} 变为 {new}（比例 {numerator}/{denominator}）"
        )
        return DuelScaleOutcome(multiplier=new, canceled=False)


def _make_duel_event(duel: _Duel, kind: DuelEventKind, **extra: Any) -> DuelEvent:
    """以决斗的参与者信息构造事件，extra 中给出的字段会覆盖默认值。"""
    fields: dict[str, Any] = {
        "kind": kind,
        "group_id": duel.group_id,
        "bot_self_id": duel.bot_self_id,
        "challenger_id": duel.challenger_id,
        "challenger_name": duel.challenger_name,
        "opponent_id": duel.opponent_id,
        "opponent_name": duel.opponent_name,
        "multiplier": duel.multiplier,
    }
    fields.update(extra)
    return DuelEvent(**fields)


async def _call_event_handler(handler: DuelEventHandler, event: DuelEvent) -> None:
    """调用单个事件处理器；失败只记录错误日志，不影响决斗流程与其它处理器。"""
    try:
        result = handler(event)
        if inspect.isawaitable(result):
            await result
    except _EVENT_HANDLER_ERRORS as exc:
        logger.error(f"决斗事件处理器处理 {event.kind.value} 事件时出错：{exc}")


async def _fire_duel_event(event: DuelEvent) -> None:
    """把事件按注册顺序派发给全部处理器（单个处理器失败只记录日志）。

    派发使用注册列表的快照，处理器在派发期间的注册或注销不影响本次派发。
    """
    for handler in tuple(_event_handlers):
        await _call_event_handler(handler, event)


# 将决斗能力注册为跨插件服务（契约见 _shared/services.py），
# 供签到、道具等插件使用
register_service(DuelScoreService, _DuelScoreService())
register_service(DuelStateService, _DuelStateService())
register_service(DuelEventService, _DuelEventService())
register_service(DuelBotAcceptService, _DuelBotAcceptService())
register_service(DuelProvokeService, _DuelProvokeService())
register_service(DuelMultiplierService, _DuelMultiplierService())


@dataclass
class _DeferredTimeoutMessage:
    """已超时决斗的提示：先保留，跟随机器人在该群的其它消息延迟发出。"""

    bot_self_id: str
    """发送该提示的机器人 QQ 号。"""

    group_id: int
    """提示所在群号。"""

    text: str
    """超时提示文案。"""

    deadline: float
    """保留截止时间（time.monotonic），超过后静默丢弃。"""


# 保留中的决斗超时提示（超时后不立即发送，等待跟随机器人其它消息）
_deferred_timeouts: list[_DeferredTimeoutMessage] = []

# 延迟发送的任务集合：持有引用避免任务被垃圾回收，任务结束后自动移出
_deferred_tasks: set[asyncio.Task[None]] = set()


def _parse_gesture(value: Any) -> int | None:
    """解析猜拳手势值，返回 1-3，无效时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value in _GESTURE_CHOICES else None
    if isinstance(value, str):
        return _GESTURE_TEXT_TO_VALUE.get(value.strip())
    return None


def _segment_gesture(segment_type: str, segment_data: dict[str, Any]) -> int | None:
    """从单个消息段中解析猜拳手势，非猜拳段或无效时返回 None。"""
    if segment_type == "rps":
        return _parse_gesture(segment_data.get("result"))
    if segment_type == "face" and str(segment_data.get("id")) == str(_RPS_FACE_ID):
        # 兼容部分协议端以 face 段上报"包剪锤"表情的情况（resultId 为结果）
        return _parse_gesture(segment_data.get("resultId"))
    return None


def _extract_gesture(message: Message) -> int | None:
    """从消息中提取第一个有效的手势。"""
    for segment in message:
        gesture = _segment_gesture(segment.type, segment.data)
        if gesture is not None:
            return gesture
    return None


def _message_to_gesture(message: Any) -> int | None:
    """从 get_msg 等接口返回的原始消息（字符串或消息段数组）中提取手势。"""
    if isinstance(message, Message):
        return _extract_gesture(message)
    if isinstance(message, str):
        return _extract_gesture(Message(message))
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict):
                continue
            data = segment.get("data")
            if not isinstance(data, dict):
                continue
            gesture = _segment_gesture(str(segment.get("type") or ""), data)
            if gesture is not None:
                return gesture
    return None


def _is_rps_message(event: Event) -> bool:
    """判断事件是否为群成员发送的猜拳表情消息（包括机器人自己）。"""
    return (
        isinstance(event, GroupMessageEvent)
        and _extract_gesture(event.get_message()) is not None
    )


def _truncate_name(name: str) -> str:
    """按配置上限截断群昵称（共享工具对接层）。"""
    return truncate_name(name, _nickname_max_length)


def _sender_display_name(event: GroupMessageEvent) -> str:
    """返回发送者在群内的显示名（群昵称优先，过长时截断）。"""
    return sender_display_name(event, _nickname_max_length)


async def _member_display_name(bot: Bot, group_id: int, user_id: int) -> str:
    """获取群成员的显示名（群昵称优先，过长时截断）。"""
    return await member_display_name(bot, group_id, user_id, _nickname_max_length)


def _parse_multiplier(args: Message) -> int | None:
    """从命令参数中解析决斗点数，未提供时返回 1，无效时返回 None。"""
    tokens = args.extract_plain_text().split()
    if not tokens:
        return 1
    if len(tokens) > 1 or not tokens[0].isdigit():
        return None
    value = int(tokens[0])
    return value if 1 <= value <= _max_multiplier else None


def _parse_target(args: Message) -> int | None:
    """从命令参数中解析被 @ 的成员 QQ 号，无有效 @ 段时返回 None。"""
    for segment in args:
        if segment.type != "at":
            continue
        qq = str(segment.data.get("qq", ""))
        if qq.isdigit():
            return int(qq)
    return None


def _parse_duel_target(args: Message, event: GroupMessageEvent) -> int | None:
    """解析 /duel 的被 @ 对象，消息末尾 @机器人 被适配器剥离时返回机器人自己。

    OneBot v11 适配器会把消息末尾的 @机器人（其后可跟一个纯空白文本段）
    当作呼叫机器人处理并连同尾随空白一起删除（见适配器 _check_at_me），
    此时参数中已找不到该 @ 段，需要根据剥离前的原始消息判断：若原始消息
    末尾正是 @机器人（可跟一个纯空白文本段），则发起对象为机器人自己。
    """
    target = _parse_target(args)
    if target is not None:
        return target
    if not event.to_me:
        return None
    original = event.original_message
    if not original:
        return None
    last = original[-1]
    if (
        last.type == "text"
        and not str(last.data.get("text", "")).strip()
        and len(original) > 1
    ):
        # 与适配器一致：末尾是纯空白文本段时向前看一段
        last = original[-2]
    if last.type == "at" and str(last.data.get("qq", "")) == str(event.self_id):
        return int(event.self_id)
    return None


def _drop_expired_deferred_timeouts(now: float) -> None:
    """静默丢弃超过保留时间的决斗超时提示。"""
    kept: list[_DeferredTimeoutMessage] = []
    for message in _deferred_timeouts:
        if message.deadline <= now:
            logger.debug(
                f"群 {message.group_id} 的决斗超时提示超过保留时间，已静默丢弃"
            )
        else:
            kept.append(message)
    _deferred_timeouts[:] = kept


def _release_deferred_timeouts(group_id: int) -> None:
    """机器人向群聊发送消息后，调度保留期内的超时提示延迟发出。

    需在发送成功后调用；提示按群匹配（跟随同群的其它消息），
    超过保留时间的提示会被静默丢弃。
    """
    now = time.monotonic()
    _drop_expired_deferred_timeouts(now)
    claimed: list[_DeferredTimeoutMessage] = []
    kept: list[_DeferredTimeoutMessage] = []
    for message in _deferred_timeouts:
        if message.group_id == group_id:
            claimed.append(message)
        else:
            kept.append(message)
    if not claimed:
        return
    _deferred_timeouts[:] = kept
    task = asyncio.create_task(_send_deferred_timeouts(claimed))
    _deferred_tasks.add(task)
    task.add_done_callback(_deferred_tasks.discard)


async def _send_deferred_timeouts(messages: list[_DeferredTimeoutMessage]) -> None:
    """延迟一段时间后发送已认领的决斗超时提示。"""
    await asyncio.sleep(_TIMEOUT_MESSAGE_DELAY_SECONDS)
    bots = get_bots()
    for message in messages:
        bot = bots.get(message.bot_self_id)
        if not isinstance(bot, Bot):
            logger.debug(
                f"机器人 {message.bot_self_id} 离线，"
                f"群 {message.group_id} 的决斗超时提示未发出"
            )
            continue
        logger.debug(f"群 {message.group_id} 的决斗超时提示跟随机器人消息发出")
        await _send_text(bot, message.group_id, message.text)


async def _send_text(
    bot: Bot, group_id: int, text: str, reply_to: int | None = None
) -> None:
    """向群聊发送决斗文本消息，成功后调度跟发本群保留中的超时提示。"""
    success = await send_group_text(bot, group_id, text, reply_to, label="决斗消息")
    if success:
        _release_deferred_timeouts(group_id)


async def _send_forward(
    bot: Bot,
    group_id: int,
    texts: list[str],
    *,
    node_name: str,
    label: str,
) -> None:
    """以合并转发的聊天记录形式发送多条文本（每条文本一个节点），
    成功后调度跟发本群保留中的超时提示。"""
    success = await send_group_forward(
        bot, group_id, texts, node_name=node_name, label=label
    )
    if success:
        _release_deferred_timeouts(group_id)


async def _send_rps(bot: Bot, group_id: int) -> int | None:
    """发送猜拳表情，返回消息 ID，发送失败时返回 None。"""
    try:
        data = await bot.call_api(
            "send_group_msg",
            group_id=group_id,
            message=Message([MessageSegment.rps()]),
        )
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"发送猜拳表情失败（群 {group_id}）：{exc}")
        return None
    _release_deferred_timeouts(group_id)
    message_id = (data or {}).get("message_id")
    return message_id if isinstance(message_id, int) else None


async def _fetch_rps_result(bot: Bot, message_id: int) -> int | None:
    """查询消息的猜拳结果（发送猜拳表情无法指定结果，需发送后回查）。"""
    try:
        data = await bot.call_api("get_msg", message_id=message_id)
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"查询猜拳表情结果失败（消息 {message_id}）：{exc}")
        return None
    return _message_to_gesture((data or {}).get("message"))


def _find_pair_duel(group_id: int, user_a: int, user_b: int) -> _Duel | None:
    """查找双方之间进行中的决斗（不限方向与是否已接受）。"""
    for duel in _duels:
        if duel.group_id != group_id:
            continue
        if {duel.challenger_id, duel.opponent_id} == {user_a, user_b}:
            return duel
    return None


def _find_pending_duel(group_id: int, challenger: int, opponent: int) -> _Duel | None:
    """查找指定成员向对方发起、等待接受（或拒绝）的决斗。"""
    for duel in _duels:
        if (
            duel.group_id == group_id
            and not duel.accepted
            and duel.challenger_id == challenger
            and duel.opponent_id == opponent
        ):
            return duel
    return None


def _find_provoked_duel(group_id: int, challenger: int, opponent: int) -> _Duel | None:
    """查找指定成员被挑衅（强制接受）的进行中决斗。"""
    for duel in _duels:
        if (
            duel.group_id == group_id
            and duel.provoked_by_bot
            and duel.challenger_id == challenger
            and duel.opponent_id == opponent
        ):
            return duel
    return None


def _take_gesture(group_id: int, user_id: int, gesture: int) -> _Duel | None:
    """把手势记录到等待该成员出手的最旧一场决斗，返回该决斗。

    需在同步段（无 await）中调用，用于防并发事件重复记录。
    """
    for duel in _duels:
        if (
            duel.group_id == group_id
            and duel.accepted
            and user_id in (duel.challenger_id, duel.opponent_id)
            and user_id not in duel.gestures
        ):
            if duel.provoked_by_bot and user_id == duel.opponent_id:
                # 被挑衅决斗中对方的手势由机器人代替，对方发送的猜拳表情无效
                continue
            duel.gestures[user_id] = gesture
            return duel
    return None


def _record_gesture(duel: _Duel, user_id: int, gesture: int) -> bool:
    """记录指定决斗中成员的手势（同步段），返回是否记录成功。

    决斗已被移除（判定完成或超时）或成员已出过手时返回 False。
    """
    if not any(item is duel for item in _duels):
        return False
    if user_id not in (duel.challenger_id, duel.opponent_id):
        return False
    if user_id in duel.gestures:
        return False
    duel.gestures[user_id] = gesture
    return True


def _remove_duel(duel: _Duel) -> bool:
    """从决斗列表中移除指定决斗（按对象身份判断），用于认领结算。"""
    for index, item in enumerate(_duels):
        if item is duel:
            del _duels[index]
            return True
    return False


def _duel_ready(duel: _Duel) -> bool:
    """判断决斗双方是否都已发送猜拳表情。"""
    return duel.challenger_id in duel.gestures and duel.opponent_id in duel.gestures


def _decide_winner(duel: _Duel) -> tuple[int, str, int, str] | None:
    """判定胜负，返回 (胜者 QQ 号, 胜者群昵称, 负者 QQ 号, 负者群昵称)。

    平局时返回 None；调用前须保证双方均已出手。
    """
    challenger_gesture = duel.gestures[duel.challenger_id]
    opponent_gesture = duel.gestures[duel.opponent_id]
    if challenger_gesture == opponent_gesture:
        return None
    if (challenger_gesture, opponent_gesture) in _WINNING_GESTURES:
        return (
            duel.challenger_id,
            duel.challenger_name,
            duel.opponent_id,
            duel.opponent_name,
        )
    return (
        duel.opponent_id,
        duel.opponent_name,
        duel.challenger_id,
        duel.challenger_name,
    )


def _final_result(
    duel: _Duel, result: tuple[int, str, int, str] | None, event: DuelEvent
) -> tuple[int, str, int, str] | None:
    """结合 settling 事件的改写确定最终胜负，非法改写回退原判定。

    返回 (胜者 QQ 号, 胜者群昵称, 负者 QQ 号, 负者群昵称)；平局返回 None。
    """
    winner_id = event.winner_id
    loser_id = event.loser_id
    if winner_id is None and loser_id is None:
        return None
    participants = {
        duel.challenger_id: duel.challenger_name,
        duel.opponent_id: duel.opponent_name,
    }
    if (
        winner_id is None
        or loser_id is None
        or winner_id == loser_id
        or winner_id not in participants
        or loser_id not in participants
    ):
        logger.warning(
            f"settling 事件改写的胜负（{winner_id} 胜 {loser_id}）不是本场决斗"
            f"的合法参与者组合，已回退为原判定"
        )
        return result
    return winner_id, participants[winner_id], loser_id, participants[loser_id]


async def _resolve_duel(bot: Bot, duel: _Duel) -> None:
    """结算双方均已出手的决斗：更新分数并发送结果。

    调用前该决斗必须已从决斗列表中认领移除，保证只结算一次；
    结算前发布 settling 事件（处理器可改写点数与胜负、读取双方手势，并可
    设置前缀文本在默认结算消息前拼接；点数负数按 0 处理、非法改写回退原
    判定；处理器可设置 claimed 接管本次结算，此时跳过默认的分数结算与
    结果播报、也不使用前缀，由处理器自行完成），结算与播报后发布 settled
    事件（接管结算时在接管处理器完成结算后发布）。
    """
    result = _decide_winner(duel)
    event = _make_duel_event(
        duel,
        DuelEventKind.SETTLING,
        winner_id=result[0] if result is not None else None,
        loser_id=result[2] if result is not None else None,
        challenger_gesture=duel.gestures[duel.challenger_id],
        opponent_gesture=duel.gestures[duel.opponent_id],
    )
    await _fire_duel_event(event)
    final = _final_result(duel, result, event)
    if event.claimed:
        logger.info(
            f"群 {duel.group_id} 中 {duel.challenger_id} 与 {duel.opponent_id} "
            "的决斗结算已被接管，跳过默认结算与播报"
        )
        await _fire_duel_event(
            _make_duel_event(
                duel,
                DuelEventKind.SETTLED,
                multiplier=event.multiplier,
                winner_id=final[0] if final is not None else None,
                loser_id=final[2] if final is not None else None,
                winner_name=final[1] if final is not None else None,
                loser_name=final[3] if final is not None else None,
                draw=final is None,
            )
        )
        return
    multiplier = event.multiplier
    if multiplier < 0:
        logger.warning(
            f"settling 事件把群 {duel.group_id} 决斗（原点数 {duel.multiplier}）"
            f"的点数改为负数 {multiplier}，已按 0 结算"
        )
        multiplier = 0
    # settling 事件处理器可设置在默认结算消息前拼接的前缀（如道具效果
    # 说明）；结算被接管时已跳过默认播报，前缀自然不被使用
    prefix = event.result_prefix or ""
    if final is None:
        logger.info(
            f"群 {duel.group_id} 中 {duel.challenger_id} 与 "
            f"{duel.opponent_id} 的决斗平局结束"
        )
        await _send_text(
            bot,
            duel.group_id,
            prefix
            + _text(
                "draw",
                player_a=duel.challenger_name,
                player_b=duel.opponent_name,
                multiplier=multiplier,
            ),
        )
    else:
        winner_id, winner_name, loser_id, loser_name = final
        _update_score(duel.group_id, winner_id, winner_name, multiplier)
        _update_score(duel.group_id, loser_id, loser_name, -multiplier)
        logger.info(
            f"群 {duel.group_id} 的决斗中 {winner_id}（{winner_name}）获胜，"
            f"{loser_id}（{loser_name}）落败"
        )
        await _send_text(
            bot,
            duel.group_id,
            prefix
            + _text(
                "win",
                winner=winner_name,
                loser=loser_name,
                multiplier=multiplier,
            ),
        )
    await _fire_duel_event(
        _make_duel_event(
            duel,
            DuelEventKind.SETTLED,
            multiplier=multiplier,
            winner_id=final[0] if final is not None else None,
            loser_id=final[2] if final is not None else None,
            winner_name=final[1] if final is not None else None,
            loser_name=final[3] if final is not None else None,
            draw=final is None,
        )
    )


async def _send_bot_rps(bot: Bot, duel: _Duel) -> None:
    """机器人发送猜拳表情并记录为对方（接受方）的手势，随后检查结算。

    用于两种场景：机器人掷骰接受决斗后出手（@机器人的决斗），以及
    被挑衅的决斗中代替对方出手。发送猜拳表情无法指定结果、发送接口
    也不返回结果，需在发送后通过 get_msg 查询该消息读取实际结果；
    查询失败时本次不记录手势（机器人自己参与的决斗仍可在协议端
    上报该消息时补记手势）。
    """
    message_id = await _send_rps(bot, duel.group_id)
    if message_id is None:
        return
    gesture = await _fetch_rps_result(bot, message_id)
    if gesture is None:
        logger.warning(
            f"未能获取机器人猜拳表情（消息 {message_id}）的结果，本次未记录手势"
        )
        return
    # 记录手势（同步段，避免并发事件重复记录），随后发布事件、
    # 检查就绪并认领结算（认领有对象身份保护，不会重复结算）
    if not _record_gesture(duel, duel.opponent_id, gesture):
        return
    await _fire_duel_event(
        _make_duel_event(
            duel, DuelEventKind.GESTURE, actor_id=duel.opponent_id, gesture=gesture
        )
    )
    if not _duel_ready(duel):
        return
    if not _remove_duel(duel):
        return
    await _resolve_duel(bot, duel)


def _target_error(target: int, user_id: int) -> str | None:
    """校验决斗的被 @ 对象是否合法（不能是自己或名单机器人），
    返回错误文案或 None（合法）。"""
    if target == user_id:
        return _text("self")
    if target in _bot_list:
        return _text("bot")
    return None


duel_cmd = on_command("duel", rule=is_type(GroupMessageEvent))


@duel_cmd.handle()
async def handle_duel(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel 命令：向群成员发起决斗（可指定点数）。"""
    target = _parse_duel_target(args, event)
    multiplier = _parse_multiplier(args)
    if target is None or multiplier is None:
        await _send_text(bot, event.group_id, _USAGE_DUEL, reply_to=event.message_id)
        return
    error = _target_error(target, event.user_id)
    if error is not None:
        await _send_text(bot, event.group_id, error, reply_to=event.message_id)
        return

    challenger_name = _sender_display_name(event)
    opponent_name = await _member_display_name(bot, event.group_id, target)
    bot_id = int(bot.self_id)
    # 同步段预检：双方之间已有进行中的决斗时直接提示（不发布发起事件）
    if _find_pair_duel(event.group_id, event.user_id, target) is not None:
        await _send_text(bot, event.group_id, _PAIR_ACTIVE, reply_to=event.message_id)
        return

    # challenge 事件：订阅方可以通过设置 block_reason 阻止本次发起
    challenge_event = DuelEvent(
        kind=DuelEventKind.CHALLENGE,
        group_id=event.group_id,
        bot_self_id=bot_id,
        challenger_id=event.user_id,
        challenger_name=challenger_name,
        opponent_id=target,
        opponent_name=opponent_name,
        multiplier=multiplier,
    )
    await _fire_duel_event(challenge_event)
    if challenge_event.block_reason:
        await _send_text(
            bot,
            event.group_id,
            challenge_event.block_reason,
            reply_to=event.message_id,
        )
        return

    # 以下为同步段：再次复查并登记，避免并发指令（如快速重复发送）或
    # challenge 事件处理器的等待期间为同一对成员创建出多场决斗
    if _find_pair_duel(event.group_id, event.user_id, target) is not None:
        await _send_text(bot, event.group_id, _PAIR_ACTIVE, reply_to=event.message_id)
        return
    duel = _Duel(
        group_id=event.group_id,
        bot_self_id=bot_id,
        challenger_id=event.user_id,
        challenger_name=challenger_name,
        opponent_id=target,
        opponent_name=opponent_name,
        multiplier=multiplier,
        created_at=time.monotonic(),
        accepted=False,
    )
    if target == bot_id:
        # @ 的是机器人自己：按接受概率函数掷骰决定是否接受，
        # 拒绝时不登记决斗状态
        accepts = random.random() < _bot_accept_probability(multiplier)
        if not accepts:
            logger.info(
                f"群 {event.group_id} 成员 {event.user_id}（{challenger_name}）"
                f"向机器人发起决斗（点数 {multiplier}），机器人拒绝"
            )
            await _send_text(
                bot,
                event.group_id,
                _text(
                    "rejected",
                    rejecter=opponent_name,
                    challenger=challenger_name,
                    multiplier=multiplier,
                ),
            )
            await _fire_duel_event(
                _make_duel_event(
                    duel, DuelEventKind.REJECTED, actor_id=duel.opponent_id
                )
            )
            return
        duel.accepted = True
    # 先保存决斗状态再发送文案：发送可能因频率限制排队，期间对方
    # 提前发送的猜拳表情也必须能被记录
    _duels.append(duel)
    logger.info(
        f"群 {event.group_id} 成员 {event.user_id}（{challenger_name}）"
        f"向 {target}（{opponent_name}）发起决斗（点数 {multiplier}）"
    )

    if duel.accepted:
        # @ 的是机器人自己：掷骰接受，并由机器人发出猜拳表情
        await _send_text(
            bot,
            event.group_id,
            _text(
                "accepted",
                accepter=opponent_name,
                challenger=challenger_name,
                multiplier=multiplier,
            ),
        )
        await _fire_duel_event(_make_duel_event(duel, DuelEventKind.CREATED))
        await _fire_duel_event(
            _make_duel_event(duel, DuelEventKind.ACCEPTED, actor_id=duel.opponent_id)
        )
        await _send_bot_rps(bot, duel)
    else:
        await _send_text(
            bot,
            event.group_id,
            _text(
                "challenged",
                challenger=challenger_name,
                opponent=opponent_name,
                multiplier=multiplier,
            ),
        )
        await _fire_duel_event(_make_duel_event(duel, DuelEventKind.CREATED))


duel_accept_cmd = on_command("duel.accept", rule=is_type(GroupMessageEvent))


@duel_accept_cmd.handle()
async def handle_duel_accept(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel.accept 命令：接受对方的决斗邀请。"""
    target = _parse_target(args)
    if target is None:
        await _send_text(bot, event.group_id, _USAGE_ACCEPT, reply_to=event.message_id)
        return
    # 同步段查找并标记接受：发送文案可能因频率限制排队，
    # 期间双方提前发送的猜拳表情也必须能按已接受的决斗记录
    duel = _find_pending_duel(event.group_id, challenger=target, opponent=event.user_id)
    if duel is None:
        provoked = _find_provoked_duel(
            event.group_id, challenger=target, opponent=event.user_id
        )
        if provoked is not None:
            await _send_text(
                bot,
                event.group_id,
                _PROVOKED_ALREADY_STARTED.format(challenger=provoked.challenger_name),
                reply_to=event.message_id,
            )
            return
        target_name = await _member_display_name(bot, event.group_id, target)
        await _send_text(
            bot,
            event.group_id,
            _text("not_challenged", challenger=target_name),
            reply_to=event.message_id,
        )
        return
    duel.accepted = True
    accepter_name = _sender_display_name(event)
    logger.info(f"群 {event.group_id} 成员 {event.user_id} 接受 {target} 的决斗邀请")
    await _send_text(
        bot,
        event.group_id,
        _text(
            "accepted",
            accepter=accepter_name,
            challenger=duel.challenger_name,
            multiplier=duel.multiplier,
        ),
    )
    await _fire_duel_event(
        _make_duel_event(duel, DuelEventKind.ACCEPTED, actor_id=event.user_id)
    )


duel_reject_cmd = on_command("duel.reject", rule=is_type(GroupMessageEvent))


@duel_reject_cmd.handle()
async def handle_duel_reject(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel.reject 命令：拒绝对方的决斗邀请。"""
    target = _parse_target(args)
    if target is None:
        await _send_text(bot, event.group_id, _USAGE_REJECT, reply_to=event.message_id)
        return
    duel = _find_pending_duel(event.group_id, challenger=target, opponent=event.user_id)
    if duel is None:
        provoked = _find_provoked_duel(
            event.group_id, challenger=target, opponent=event.user_id
        )
        if provoked is not None:
            await _send_text(
                bot,
                event.group_id,
                _PROVOKED_CANNOT_REJECT.format(challenger=provoked.challenger_name),
                reply_to=event.message_id,
            )
            return
        target_name = await _member_display_name(bot, event.group_id, target)
        await _send_text(
            bot,
            event.group_id,
            _text("not_challenged", challenger=target_name),
            reply_to=event.message_id,
        )
        return
    _remove_duel(duel)
    rejecter_name = _sender_display_name(event)
    logger.info(f"群 {event.group_id} 成员 {event.user_id} 拒绝 {target} 的决斗邀请")
    await _send_text(
        bot,
        event.group_id,
        _text(
            "rejected",
            rejecter=rejecter_name,
            challenger=duel.challenger_name,
            multiplier=duel.multiplier,
        ),
    )
    await _fire_duel_event(
        _make_duel_event(duel, DuelEventKind.REJECTED, actor_id=event.user_id)
    )


def _parse_rank_args(tokens: list[str]) -> tuple[bool, int, bool, int] | None:
    """解析排行榜参数，返回 (显示高分榜, 高分榜条数, 显示低分榜, 低分榜条数)。

    参数无法识别或条数不是正整数时返回 None。
    """
    show_high = False
    high_limit = _DEFAULT_RANK_LIMIT
    show_low = False
    low_limit = _DEFAULT_RANK_LIMIT
    index = 0
    while index < len(tokens):
        token = tokens[index].lower()
        if token in {"high", "h"}:
            show_high = True
            if index + 1 < len(tokens) and tokens[index + 1].isdigit():
                high_limit = int(tokens[index + 1])
                index += 1
        elif token in {"low", "l"}:
            show_low = True
            if index + 1 < len(tokens) and tokens[index + 1].isdigit():
                low_limit = int(tokens[index + 1])
                index += 1
        else:
            return None
        index += 1
    if high_limit < 1 or low_limit < 1:
        return None
    return show_high, high_limit, show_low, low_limit


def _rank_entries(group_id: int, *, positive: bool) -> list[tuple[int, str, int]]:
    """返回指定群的排行榜条目 (QQ 号, 昵称, 分数)，昵称过长时截断。

    高分榜为分数为正的成员按分数从高到低排列；低分榜为分数为负的成员
    按分数绝对值从高到低排列；分数相同时按 QQ 号升序排列。
    """
    entries = [
        (user_id, _truncate_name(str(record["name"])), score)
        for user_id, record in _scores.get(group_id, {}).items()
        if (score := int(record["score"])) and (score > 0) == positive
    ]
    if positive:
        entries.sort(key=lambda entry: (-entry[2], entry[0]))
    else:
        entries.sort(key=lambda entry: (-abs(entry[2]), entry[0]))
    return entries


def _rank_section(title: str, entries: list[tuple[int, str, int]], limit: int) -> str:
    """生成一个排行榜的文本：标题与最多 limit 条条目。"""
    lines = [f"{title}（前 {limit} 名）"]
    visible = entries[:limit]
    if not visible:
        lines.append(_RANK_EMPTY)
        return "\n".join(lines)
    lines.extend(
        f"{index}. {name} {score} 分"
        for index, (_, name, score) in enumerate(visible, start=1)
    )
    return "\n".join(lines)


duel_rank_cmd = on_command("duel.rank", rule=is_type(GroupMessageEvent))


@duel_rank_cmd.handle()
async def handle_duel_rank(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel.rank 命令：查看本群分数排行榜。"""
    parsed = _parse_rank_args(args.extract_plain_text().lower().split())
    if parsed is None:
        await _send_text(bot, event.group_id, _USAGE_RANK, reply_to=event.message_id)
        return
    show_high, high_limit, show_low, low_limit = parsed
    if not show_high and not show_low:
        # 未指定榜单时默认同时显示高分榜与低分榜
        show_high = True
        show_low = True
    sections: list[str] = []
    if show_high:
        sections.append(
            _rank_section(
                _RANK_TITLE_HIGH,
                _rank_entries(event.group_id, positive=True),
                high_limit,
            )
        )
    if show_low:
        sections.append(
            _rank_section(
                _RANK_TITLE_LOW,
                _rank_entries(event.group_id, positive=False),
                low_limit,
            )
        )
    await _send_forward(
        bot, event.group_id, sections, node_name=_RANK_NODE_NAME, label="决斗排行榜"
    )


duel_score_cmd = on_command("duel.score", rule=is_type(GroupMessageEvent))


@duel_score_cmd.handle()
async def handle_duel_score(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel.score 命令：查看自己在本群的决斗分数（无记录时为 0）。"""
    if args.extract_plain_text().split():
        await _send_text(bot, event.group_id, _USAGE_SCORE, reply_to=event.message_id)
        return
    record = _scores.get(event.group_id, {}).get(event.user_id)
    score = int(record["score"]) if record is not None else 0
    name = _sender_display_name(event)
    await _send_text(
        bot,
        event.group_id,
        f"{name} 的决斗分数为 {score} 分。",
        reply_to=event.message_id,
    )


def _remaining_text(duel: _Duel, now: float) -> str:
    """返回决斗的剩余超时描述，如"距超时约 10 分钟"。"""
    remaining = max(0, math.ceil((duel.created_at + _duration_seconds - now) / 60))
    return f"距超时约 {remaining} 分钟"


def _status_lines(group_id: int, user_id: int) -> list[str]:
    """生成成员在本群进行中决斗的状态描述行（按创建顺序）。"""
    now = time.monotonic()
    lines: list[str] = []
    for duel in _duels:
        if duel.group_id != group_id:
            continue
        if user_id not in (duel.challenger_id, duel.opponent_id):
            continue
        suffix = _remaining_text(duel, now)
        if not duel.accepted:
            if user_id == duel.challenger_id:
                lines.append(
                    f"你向 {duel.opponent_name} 发起的决斗"
                    f"（×{duel.multiplier}）：等待对方接受，{suffix}。"
                )
            else:
                lines.append(
                    f"{duel.challenger_name} 向你发起的决斗"
                    f"（×{duel.multiplier}）：等待你接受"
                    f"（可使用 /duel.accept 接受），{suffix}。"
                )
            continue
        other_name = (
            duel.opponent_name
            if user_id == duel.challenger_id
            else duel.challenger_name
        )
        if duel.provoked_by_bot and user_id == duel.opponent_id:
            # 被挑衅的一方：手势由机器人代替，自己发送的猜拳表情无效
            if user_id not in duel.gestures:
                state = "你的猜拳表情将由 Bot 代替发送"
            else:
                state = "Bot 已代替你发送猜拳表情，等待对方发送猜拳表情"
        elif user_id not in duel.gestures:
            state = "等待你发送猜拳表情"
        else:
            state = "你已出拳，等待对方发送猜拳表情"
        lines.append(
            f"你与 {other_name} 的决斗（×{duel.multiplier}）：{state}，{suffix}。"
        )
    return lines


duel_status_cmd = on_command("duel.status", rule=is_type(GroupMessageEvent))


def _all_status_lines(group_id: int) -> list[str]:
    """生成本群全部进行中决斗的状态描述行（按创建顺序）。"""
    now = time.monotonic()
    lines: list[str] = []
    for duel in _duels:
        if duel.group_id != group_id:
            continue
        suffix = _remaining_text(duel, now)
        if not duel.accepted:
            lines.append(
                f"{duel.challenger_name} 向 {duel.opponent_name} 发起的决斗"
                f"（×{duel.multiplier}）：等待对方接受，{suffix}。"
            )
            continue
        if duel.provoked_by_bot and duel.opponent_id not in duel.gestures:
            # 被挑衅一方的手势由机器人代替（代替发送前仅短暂经过）
            state = f"等待 Bot 代替 {duel.opponent_name} 发送猜拳表情"
        else:
            players = (
                (duel.challenger_id, duel.challenger_name),
                (duel.opponent_id, duel.opponent_name),
            )
            pending_names = [name for uid, name in players if uid not in duel.gestures]
            if len(pending_names) == 1:
                state = f"等待 {pending_names[0]} 发送猜拳表情"
            else:
                # 双方都未出拳（双方均已出拳时会立即结算，不会留在列表中）
                state = "等待双方发送猜拳表情"
        lines.append(
            f"{duel.challenger_name} 与 {duel.opponent_name} 的决斗"
            f"（×{duel.multiplier}）：{state}，{suffix}。"
        )
    return lines


def _parse_status_args(tokens: list[str]) -> str | None:
    """解析 /duel.status 命令参数，返回 "self"、"all" 或 None（无法识别）。"""
    if not tokens:
        return "self"
    if len(tokens) == 1 and tokens[0] in {"all", "a"}:
        return "all"
    return None


@duel_status_cmd.handle()
async def handle_duel_status(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel.status 命令：显示自己或（超级用户）本群全部的决斗状态。"""
    scope = _parse_status_args(args.extract_plain_text().lower().split())
    if scope is None:
        await _send_text(bot, event.group_id, _USAGE_STATUS, reply_to=event.message_id)
        return
    if scope == "all":
        if not await SUPERUSER(bot, event):
            await _send_text(
                bot, event.group_id, _STATUS_ALL_DENIED, reply_to=event.message_id
            )
            return
        lines = _all_status_lines(event.group_id)
        empty_text = "本群没有进行中的决斗。"
        header = f"本群有 {len(lines)} 场进行中的决斗："
    else:
        lines = _status_lines(event.group_id, event.user_id)
        empty_text = "你在本群没有进行中的决斗。"
        header = f"你在本群有 {len(lines)} 场进行中的决斗："
    if not lines:
        await _send_text(bot, event.group_id, empty_text)
        return
    numbered = [f"{index}. {line}" for index, line in enumerate(lines, start=1)]
    await _send_text(bot, event.group_id, "\n".join([header, *numbered]))


# 优先级 0 且不阻断事件传播：检测群成员的猜拳表情并更新决斗状态，
# 不影响其它插件（命令、默认回复等）继续处理消息
duel_gesture = on_message(priority=0, block=False, rule=_is_rps_message)


@duel_gesture.handle()
async def handle_duel_gesture(bot: Bot, event: GroupMessageEvent) -> None:
    """群成员（包括机器人自己）发送猜拳表情时更新决斗状态并结算。"""
    gesture = _extract_gesture(event.get_message())
    if gesture is None:
        return
    # 同步段：记录手势（避免并发事件重复记录），随后发布事件、
    # 检查就绪并认领结算（认领有对象身份保护，不会重复结算）
    duel = _take_gesture(event.group_id, event.user_id, gesture)
    if duel is None:
        return
    logger.debug(
        f"已记录成员 {event.user_id} 在群 {event.group_id} 决斗中的手势 {gesture}"
    )
    await _fire_duel_event(
        _make_duel_event(
            duel, DuelEventKind.GESTURE, actor_id=event.user_id, gesture=gesture
        )
    )
    if not _duel_ready(duel):
        return
    if not _remove_duel(duel):
        return
    await _resolve_duel(bot, duel)


async def _check_duel_timeouts() -> None:
    """定时检查并结束超时的决斗。

    机器人不在线时保留决斗状态，等下次检查（机器人上线后）再通知。
    超时提示不立即发出：先保留 1 分钟，期间机器人向该群发送其它消息
    时在其后延迟 1 秒发出，保留期内未能跟随则静默丢弃。
    """
    now = time.monotonic()
    bots = get_bots()
    # 清理超过保留时间的旧提示，遍历快照收集待通知的超时决斗并完成
    # 认领（认领有对象身份保护，事件发布的等待期间不会重复认领），
    # 超时提示转入延迟发送队列
    _drop_expired_deferred_timeouts(now)
    for duel in _duels[:]:
        if now - duel.created_at < _duration_seconds:
            continue
        bot = bots.get(str(duel.bot_self_id))
        if not isinstance(bot, Bot):
            continue
        if not _remove_duel(duel):
            continue
        logger.info(
            f"群 {duel.group_id} 中 {duel.challenger_id} 与 "
            f"{duel.opponent_id} 的决斗超时结束"
        )
        _deferred_timeouts.append(
            _DeferredTimeoutMessage(
                bot_self_id=str(duel.bot_self_id),
                group_id=duel.group_id,
                text=_text(
                    "timeout",
                    player_a=duel.challenger_name,
                    player_b=duel.opponent_name,
                    multiplier=duel.multiplier,
                ),
                deadline=now + _TIMEOUT_MESSAGE_LINGER_SECONDS,
            )
        )
        await _fire_duel_event(_make_duel_event(duel, DuelEventKind.TIMEOUT))


logger.info(
    f"猜拳决斗已启用，决斗超时时间为 {_duration_minutes:g} 分钟，"
    f"群昵称最大显示长度为 {_nickname_max_length} 字符，"
    f"点数上限为 {_max_multiplier}"
)
if _bot_list:
    logger.info(
        f"机器人名单已配置（共享配置 BOT_LIST，所有群通用）：{sorted(_bot_list)}"
    )

scheduler.add_job(
    _check_duel_timeouts,
    "interval",
    seconds=_TIMEOUT_CHECK_INTERVAL_SECONDS,
    id="duel_check_timeouts",
    replace_existing=True,
    misfire_grace_time=30,
)
