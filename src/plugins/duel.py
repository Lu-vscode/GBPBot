"""猜拳决斗插件。

在群聊中发起猜拳决斗：`/duel @成员` 发起，被 @ 的成员可通过
`/duel.accept @成员` 接受或 `/duel.reject @成员` 拒绝；接受后双方发送
QQ"包剪锤"表情，机器人根据双方手势判定胜负：胜者积分 +1、负者积分 -1，
平局积分不变；各群积分相互独立，积分数据使用 localstore 长期存储在本
地；`/duel.rank` 可查看本群积分排行榜。

@ 机器人自己时机器人自动接受决斗并发送猜拳表情；决斗状态保存在内存中，
存在时间超过 DUEL_DURATION（分钟，默认 10）后自动超时结束。
"""

import json
import time
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
from nonebot.plugin import PluginMetadata
from nonebot.rule import is_type
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_localstore import get_plugin_data_file
from pydantic import BaseModel, field_validator

__plugin_meta__ = PluginMetadata(
    name="猜拳决斗",
    description="在群聊中发起猜拳决斗，由机器人判定胜负并按群记录积分，支持积分排行榜",
    usage=(
        "/duel @成员：向群成员发起决斗\n"
        "/duel.accept @成员：接受对方的决斗邀请\n"
        "/duel.reject @成员：拒绝对方的决斗邀请\n"
        "/duel.rank (high|h (<条数>)) (low|l (<条数>))：查看本群积分排行榜"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 决斗超时时间的默认值（分钟）、排行榜的默认最大显示条数、
# bot 消息中群昵称的最大显示长度（字符数）
_DEFAULT_DURATION_MINUTES = 10.0
_DEFAULT_RANK_LIMIT = 10
_DEFAULT_NICKNAME_MAX_LENGTH = 12

# 决斗超时的检查间隔（秒）：超时提示最多延迟该间隔
_TIMEOUT_CHECK_INTERVAL_SECONDS = 30

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
    "accepted": "{accepter} 已接受 {challenger} 的决斗邀请。\n请双方发送猜拳表情。",
    "challenged": "{challenger} 向 {opponent} 发起决斗。",
    "rejected": "{rejecter} 已拒绝 {challenger} 的决斗邀请。",
    "not_challenged": "{challenger} 未向你发起决斗。",
    "win": (
        "{winner} 在与 {loser} 的决斗中获胜。"
        "{winner} 的积分+1，{loser} 的积分-1。决斗结束。"
    ),
    "draw": "{player_a} 与 {player_b} 的决斗平局。双方积分不变。决斗结束。",
    "timeout": "{player_a} 与 {player_b} 的决斗超时结束。",
}

# 每条文案允许使用的占位符，启动时校验配置的文案与占位符是否匹配
_TEXT_FIELDS = {
    "self": (),
    "bot": (),
    "accepted": ("accepter", "challenger"),
    "challenged": ("challenger", "opponent"),
    "rejected": ("rejecter", "challenger"),
    "not_challenged": ("challenger",),
    "win": ("winner", "loser"),
    "draw": ("player_a", "player_b"),
    "timeout": ("player_a", "player_b"),
}

# 文档未定义的边界情况文案（用法与错误提示），不支持配置
_USAGE_DUEL = "请 @ 要发起决斗的群成员，用法：/duel @群成员"
_USAGE_ACCEPT = "请 @ 发起决斗的群成员，用法：/duel.accept @群成员"
_USAGE_REJECT = "请 @ 发起决斗的群成员，用法：/duel.reject @群成员"
_USAGE_RANK = (
    "参数无法识别，用法：/duel.rank (high|h (<条数>)) (low|l (<条数>))，"
    "条数需为正整数且默认为 10"
)
_PAIR_ACTIVE = "你们之间已有一场进行中的决斗，请等待该决斗结束或超时后再发起。"
_RANK_TITLE_HIGH = "决斗积分高分榜"
_RANK_TITLE_LOW = "决斗积分低分榜"
_RANK_EMPTY = "（暂无）"


class Config(BaseModel):
    """猜拳决斗插件配置。

    可在 `.env.{environment}` 文件中通过 `DUEL_*` 系列变量配置，
    缺失或为空时使用默认行为（无机器人名单、超时 10 分钟、
    群昵称最长 12 字符、内置文案）。
    """

    duel_bot_list: Any = None
    """不参与决斗的机器人名单：其它机器人 QQ 号列表（所有群通用），默认为空。"""

    duel_duration: float | None = None
    """决斗的存在时间上限（分钟），超过后自动超时结束，默认 10。"""

    duel_nickname_max_length: int | None = None
    """bot 消息中群昵称的最大显示长度（字符数），超过时截断并以"…"结尾，默认 12。"""

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
        "duel_bot_list", "duel_duration", "duel_nickname_max_length", mode="before"
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


def _as_qq(value: Any) -> int | None:
    """把配置项转换为 QQ 号，无效时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _normalize_bot_list(value: Any) -> frozenset[int]:
    """校验并返回决斗机器人名单（所有群通用的机器人 QQ 号集合）。

    配置应为机器人 QQ 号列表（JSON 数组）；兼容旧版按群配置格式的
    JSON 对象（键为群号、值为机器人 QQ 号列表），此时各群名单会合并。
    """
    if value is None:
        return frozenset()
    if isinstance(value, dict):
        # 旧版按群配置格式：各群名单合并为全局名单
        members = [
            qq for bots in value.values() if isinstance(bots, list) for qq in bots
        ]
        if members:
            logger.warning(
                "猜拳决斗配置 DUEL_BOT_LIST 使用了旧版按群配置格式，"
                "已将各群的机器人合并为全局名单，建议改为机器人 QQ 号列表"
            )
        value = members
    if not isinstance(value, list):
        logger.warning(
            "猜拳决斗配置 DUEL_BOT_LIST 格式无效（应为机器人 QQ 号列表），已视为空名单"
        )
        return frozenset()
    return frozenset(qq for item in value if (qq := _as_qq(item)) is not None)


# 决斗机器人名单：不参与决斗（挑战时提示不能向 BOT 发起）的机器人 QQ 号，所有群通用
_bot_list = _normalize_bot_list(plugin_config.duel_bot_list)


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


def _text(key: str, **kwargs: str) -> str:
    """取出文案并填充占位符。"""
    return _texts[key].format(**kwargs)


# 积分数据存储文件（localstore 插件数据目录）
_SCORE_FILE = get_plugin_data_file("scores.json")


def _normalize_score_record(
    user_id: Any, record: Any
) -> tuple[int, dict[str, Any]] | None:
    """校验积分数据中的单条成员记录，无效时返回 None。"""
    if not isinstance(user_id, str) or not user_id.isdigit():
        return None
    if not isinstance(record, dict):
        return None
    score = record.get("score")
    if isinstance(score, bool) or not isinstance(score, int):
        return None
    return int(user_id), {"name": str(record.get("name") or ""), "score": score}


def _is_legacy_group_data(group_data: Any) -> bool:
    """判断群数据是否为旧版全局积分格式（值为成员记录而非群映射）。"""
    return isinstance(group_data, dict) and (
        "score" in group_data or "name" in group_data
    )


def _normalize_group_record(
    group_id: Any, group_data: Any
) -> tuple[int, dict[int, dict[str, Any]]] | None:
    """校验积分数据中的单个群记录，返回 (群号, 群内成员积分数据)。"""
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
    """从本地文件中读取积分数据，文件不存在或损坏时返回空数据。"""
    if not _SCORE_FILE.exists():
        return {}
    try:
        raw = json.loads(_SCORE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"读取决斗积分数据失败，本次将视为无积分数据：{exc}")
        return {}
    if not isinstance(raw, dict):
        logger.warning("决斗积分数据格式异常（应为 JSON 对象），本次将视为无积分数据")
        return {}
    if any(_is_legacy_group_data(group_data) for group_data in raw.values()):
        # 旧版全局格式的记录（键为成员 QQ 号，值含 score/name）：
        # 不含群号，按群隔离后无法归属，直接忽略
        logger.warning(
            "决斗积分数据中存在旧版全局格式的记录（不含群号），按群隔离后无法归属，本次已忽略"
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
    """将积分数据写入本地文件，写入失败时仅记录错误。"""
    try:
        _SCORE_FILE.write_text(
            json.dumps(_scores, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.error(f"写取决斗积分数据失败，本次修改未保存：{exc}")


# 积分数据：群号 -> {成员 QQ 号 -> {"name": 最近使用的群昵称, "score": 积分}}
_scores = _load_scores()
if _scores:
    _member_count = sum(len(members) for members in _scores.values())
    logger.info(f"已加载决斗积分数据，共 {len(_scores)} 个群、{_member_count} 名成员")


def _update_score(group_id: int, user_id: int, name: str, delta: int) -> None:
    """更新指定群内成员的积分与昵称并写入本地文件。"""
    group = _scores.setdefault(group_id, {})
    record = group.setdefault(user_id, {"name": name, "score": 0})
    record["name"] = name
    record["score"] = int(record["score"]) + delta
    _save_scores()


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

    created_at: float
    """决斗创建时间（time.monotonic，用于超时判断）。"""

    accepted: bool = False
    """对方是否已接受（等待接受时为 False，机器人自动接受时为 True）。"""

    gestures: dict[int, int] = field(default_factory=dict)
    """已发送的猜拳手势：成员 QQ 号 -> 手势结果（1 剪刀、2 石头、3 布）。"""


# 进行中的决斗，按创建顺序排列（同一成员多场决斗时优先满足旧的）
_duels: list[_Duel] = []


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
    """截断过长的群昵称：被截断的部分以"…"代替，总长不超过配置上限。"""
    if len(name) <= _nickname_max_length:
        return name
    return name[: _nickname_max_length - 1] + "…"


def _sender_display_name(event: GroupMessageEvent) -> str:
    """返回发送者在群内的显示名（群昵称优先，否则 QQ 昵称），过长时截断。"""
    card = (event.sender.card or "").strip()
    nickname = (event.sender.nickname or "").strip()
    name = card or nickname
    if not name:
        return f"QQ {event.user_id}"
    return _truncate_name(name)


async def _member_display_name(bot: Bot, group_id: int, user_id: int) -> str:
    """获取群成员的显示名（群昵称优先，过长时截断），查询失败时回退为 QQ 号。"""
    try:
        member = await bot.call_api(
            "get_group_member_info",
            group_id=group_id,
            user_id=user_id,
            no_cache=True,
        )
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"获取群 {group_id} 成员 {user_id} 的资料失败：{exc}")
        return f"QQ {user_id}"
    card = str((member or {}).get("card") or "").strip()
    nickname = str((member or {}).get("nickname") or "").strip()
    name = card or nickname
    if not name:
        return f"QQ {user_id}"
    return _truncate_name(name)


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


async def _send_text(
    bot: Bot, group_id: int, text: str, reply_to: int | None = None
) -> None:
    """向群聊发送文本消息，作为纯文本段构造以避免昵称等被解析为 CQ 码。"""
    segments: list[MessageSegment] = []
    if reply_to is not None:
        segments.append(MessageSegment.reply(reply_to))
    segments.append(MessageSegment.text(text))
    try:
        await bot.call_api(
            "send_group_msg", group_id=group_id, message=Message(segments)
        )
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"发送决斗消息失败（群 {group_id}）：{exc}")


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


async def _resolve_duel(bot: Bot, duel: _Duel) -> None:
    """结算双方均已出手的决斗：更新积分并发送结果。

    调用前该决斗必须已从决斗列表中认领移除，保证只结算一次。
    """
    result = _decide_winner(duel)
    if result is None:
        logger.info(
            f"群 {duel.group_id} 中 {duel.challenger_id} 与 "
            f"{duel.opponent_id} 的决斗平局结束"
        )
        await _send_text(
            bot,
            duel.group_id,
            _text(
                "draw",
                player_a=duel.challenger_name,
                player_b=duel.opponent_name,
            ),
        )
        return
    winner_id, winner_name, loser_id, loser_name = result
    _update_score(duel.group_id, winner_id, winner_name, 1)
    _update_score(duel.group_id, loser_id, loser_name, -1)
    logger.info(
        f"群 {duel.group_id} 的决斗中 {winner_id}（{winner_name}）获胜，"
        f"{loser_id}（{loser_name}）落败"
    )
    await _send_text(
        bot, duel.group_id, _text("win", winner=winner_name, loser=loser_name)
    )


async def _send_bot_rps(bot: Bot, duel: _Duel) -> None:
    """机器人自动接受决斗后发送猜拳表情，并记录机器人自己的手势。

    发送猜拳表情无法指定结果、发送接口也不返回结果，需在发送后通过
    get_msg 查询该消息读取实际结果；查询失败时等待协议端上报该消息。
    """
    message_id = await _send_rps(bot, duel.group_id)
    if message_id is None:
        return
    gesture = await _fetch_rps_result(bot, message_id)
    if gesture is None:
        logger.warning(
            f"未能获取机器人猜拳表情（消息 {message_id}）的结果，等待协议端上报该消息"
        )
        return
    # 以下为同步段：记录手势、检查就绪并认领决斗
    if not _record_gesture(duel, duel.opponent_id, gesture):
        return
    if not _duel_ready(duel):
        return
    if not _remove_duel(duel):
        return
    await _resolve_duel(bot, duel)


duel_cmd = on_command("duel", rule=is_type(GroupMessageEvent))


@duel_cmd.handle()
async def handle_duel(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /duel 命令：向群成员发起决斗。"""
    target = _parse_duel_target(args, event)
    if target is None:
        await _send_text(bot, event.group_id, _USAGE_DUEL, reply_to=event.message_id)
        return
    if target == event.user_id:
        await _send_text(bot, event.group_id, _text("self"), reply_to=event.message_id)
        return
    if target in _bot_list:
        await _send_text(bot, event.group_id, _text("bot"), reply_to=event.message_id)
        return

    challenger_name = _sender_display_name(event)
    opponent_name = await _member_display_name(bot, event.group_id, target)
    # 以下为同步段：复查并登记，避免并发指令（如快速重复发送）为
    # 同一对成员创建出多场决斗
    if _find_pair_duel(event.group_id, event.user_id, target) is not None:
        await _send_text(bot, event.group_id, _PAIR_ACTIVE, reply_to=event.message_id)
        return
    bot_id = int(bot.self_id)
    duel = _Duel(
        group_id=event.group_id,
        bot_self_id=bot_id,
        challenger_id=event.user_id,
        challenger_name=challenger_name,
        opponent_id=target,
        opponent_name=opponent_name,
        created_at=time.monotonic(),
        accepted=target == bot_id,
    )
    # 先保存决斗状态再发送文案：发送可能因频率限制排队，期间对方
    # 提前发送的猜拳表情也必须能被记录
    _duels.append(duel)
    logger.info(
        f"群 {event.group_id} 成员 {event.user_id}（{challenger_name}）"
        f"向 {target}（{opponent_name}）发起决斗"
    )

    if duel.accepted:
        # @ 的是机器人自己：自动接受，并由机器人发出猜拳表情
        await _send_text(
            bot,
            event.group_id,
            _text("accepted", accepter=opponent_name, challenger=challenger_name),
        )
        await _send_bot_rps(bot, duel)
    else:
        await _send_text(
            bot,
            event.group_id,
            _text("challenged", challenger=challenger_name, opponent=opponent_name),
        )


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
        _text("accepted", accepter=accepter_name, challenger=duel.challenger_name),
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
        _text("rejected", rejecter=rejecter_name, challenger=duel.challenger_name),
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
    """返回指定群的排行榜条目 (QQ 号, 昵称, 积分)，昵称过长时截断。

    高分榜为积分为正的成员按积分从高到低排列；低分榜为积分为负的成员
    按积分绝对值从高到低排列；积分相同时按 QQ 号升序排列。
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
    """处理 /duel.rank 命令：查看本群积分排行榜。"""
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
    await _send_text(bot, event.group_id, "\n\n".join(sections))


# 优先级 0 且不阻断事件传播：检测群成员的猜拳表情并更新决斗状态，
# 不影响其它插件（命令、默认回复等）继续处理消息
duel_gesture = on_message(priority=0, block=False, rule=_is_rps_message)


@duel_gesture.handle()
async def handle_duel_gesture(bot: Bot, event: GroupMessageEvent) -> None:
    """群成员（包括机器人自己）发送猜拳表情时更新决斗状态并结算。"""
    gesture = _extract_gesture(event.get_message())
    if gesture is None:
        return
    # 同步段：记录手势、检查就绪并认领决斗，避免并发事件重复结算
    duel = _take_gesture(event.group_id, event.user_id, gesture)
    if duel is None:
        return
    logger.debug(
        f"已记录成员 {event.user_id} 在群 {event.group_id} 决斗中的手势 {gesture}"
    )
    if not _duel_ready(duel):
        return
    if not _remove_duel(duel):
        return
    await _resolve_duel(bot, duel)


async def _check_duel_timeouts() -> None:
    """定时检查并结束超时的决斗。

    机器人不在线时保留决斗状态，等下次检查（机器人上线后）再通知。
    """
    now = time.monotonic()
    bots = get_bots()
    # 同步段：收集待通知的超时决斗并完成认领（遍历快照，认领会修改列表）
    expired: list[tuple[Bot, _Duel]] = []
    for duel in _duels[:]:
        if now - duel.created_at < _duration_seconds:
            continue
        bot = bots.get(str(duel.bot_self_id))
        if not isinstance(bot, Bot):
            continue
        if not _remove_duel(duel):
            continue
        expired.append((bot, duel))
    # 异步段：逐个发送超时提示
    for bot, duel in expired:
        logger.info(
            f"群 {duel.group_id} 中 {duel.challenger_id} 与 "
            f"{duel.opponent_id} 的决斗超时结束"
        )
        await _send_text(
            bot,
            duel.group_id,
            _text(
                "timeout",
                player_a=duel.challenger_name,
                player_b=duel.opponent_name,
            ),
        )


logger.info(
    f"猜拳决斗已启用，决斗超时时间为 {_duration_minutes:g} 分钟，"
    f"群昵称最大显示长度为 {_nickname_max_length} 字符"
)
if _bot_list:
    logger.info(f"猜拳决斗机器人名单已配置（所有群通用）：{sorted(_bot_list)}")

scheduler.add_job(
    _check_duel_timeouts,
    "interval",
    seconds=_TIMEOUT_CHECK_INTERVAL_SECONDS,
    id="duel_check_timeouts",
    replace_existing=True,
    misfire_grace_time=30,
)
