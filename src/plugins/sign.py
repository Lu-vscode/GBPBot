"""签到插件。

在群聊中发送 `/sign` 进行每日签到：每人每天在本群可签到一次（以本地
时区的自然日为准，0 点后重置），签到记录使用 localstore 长期存储在
本地且按群隔离（群号 -> 成员 QQ 号 -> 逐条签到记录，每条含签到日期
与获得的道具编号；旧格式的汇总记录在加载时自动迁移、仅保留最新
一条）；签到成功可获得决斗分数奖励（默认 1 分，SIGN_SCORE 可配置）
与一件随机道具。

与决斗、道具插件的交互通过跨插件服务实现（见 _shared/services.py）：
加载时用 require("duel")、require("item") 声明依赖，确保分数与道具
服务提供方已加载并完成注册；签到时经服务注册中心取用分数服务增加
决斗分数、取用道具服务发放随机道具（黑色诅咒由道具服务在签到文案
之后完成信息播报与自动使用）。
"""

import json
from datetime import UTC, datetime
from typing import Any

from nonebot import get_plugin_config, logger, on_command
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.plugin import PluginMetadata, require
from nonebot.rule import is_type
from nonebot_plugin_localstore import get_plugin_data_file
from pydantic import BaseModel, ValidationInfo, field_validator

from src.plugins._shared.services import (
    DuelScoreService,
    GrantedItem,
    ItemService,
    require_service,
)

__plugin_meta__ = PluginMetadata(
    name="签到",
    description="群聊每日签到，每人每天一次，签到成功可获得决斗分数奖励与随机道具",
    usage="/sign：在本群签到（每人每天一次），签到成功可获得决斗分数奖励与随机道具",
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 每次签到获得的决斗分数默认值
_DEFAULT_SCORE_GAIN = 1

# 文档中直接出现的文案对应的默认值，键与 SIGN_TEXT_* 配置一一对应
# （配置项名 = "sign_text_" + 键）
_TEXT_DEFAULTS = {
    "success": "签到成功！决斗分数+{score}，获得道具 {item}。",
    "success_no_item": "签到成功！决斗分数+{score}，道具抽取失败。",
    "duplicate": "今天已经签到过了，明天再来吧。",
}

# 每条文案允许使用的占位符，启动时校验配置的文案与占位符是否匹配
_TEXT_FIELDS = {
    "success": ("score", "item"),
    "success_no_item": ("score",),
    "duplicate": (),
}


class Config(BaseModel):
    """签到插件配置。

    可在 `.env.{environment}` 文件中通过 `SIGN_*` 系列变量配置，
    缺失或为空时使用默认行为（每次签到 1 分、内置文案）。
    """

    sign_score: int | None = None
    """每次签到获得的决斗分数（正整数），默认 1。"""

    sign_text_success: str | None = None
    """签到成功且获得道具时的提示文案，占位符 {score} 获得的决斗分数、
    {item} 获得的道具（形如"111 +1"）。"""

    sign_text_success_no_item: str | None = None
    """签到成功但道具抽取失败时的提示文案，占位符 {score} 获得的决斗分数。"""

    sign_text_duplicate: str | None = None
    """当天已签到时重复签到的提示文案。"""

    @field_validator("sign_score", mode="before")
    @classmethod
    def invalid_int_as_none(cls, value: Any, info: ValidationInfo) -> Any:
        """将空字符串或无法解析为整数的值视为未配置，避免插件加载失败。"""
        if isinstance(value, str):
            if not value.strip():
                return None
            try:
                return int(value)
            except ValueError:
                logger.warning(
                    f"签到配置 {info.field_name}={value!r} 无法解析为整数，"
                    f"已视为未配置并使用默认值"
                )
                return None
        return value


plugin_config = get_plugin_config(Config)


def _score_gain_or_default(value: int | None) -> int:
    """返回每次签到获得的决斗分数，未配置或非正数时使用默认值。"""
    if value is None:
        return _DEFAULT_SCORE_GAIN
    if value < 1:
        logger.warning(
            f"签到配置 SIGN_SCORE={value} 无效（需为正整数），"
            f"已使用默认值 {_DEFAULT_SCORE_GAIN}"
        )
        return _DEFAULT_SCORE_GAIN
    return value


# 每次签到获得的决斗分数
_score_gain = _score_gain_or_default(plugin_config.sign_score)


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
            f"签到配置 SIGN_TEXT_{key.upper()} 的占位符格式无效"
            f"（{exc}），已使用默认文案"
        )
        return default
    return text


# 全部签到文案：文案键 -> 最终使用的文案（配置或默认值）
_texts = {
    key: _text_or_default(key, getattr(plugin_config, f"sign_text_{key}"))
    for key in _TEXT_DEFAULTS
}


def _text(key: str, **kwargs: Any) -> str:
    """取出文案并填充占位符。"""
    return _texts[key].format(**kwargs)


# 签到记录存储文件（localstore 插件数据目录）
_SIGN_FILE = get_plugin_data_file("sign_ins.json")


def _normalize_entry(entry: Any) -> dict[str, str] | None:
    """校验签到记录中的单条记录（{date, item}），无效时返回 None。"""
    if not isinstance(entry, dict):
        return None
    date = entry.get("date")
    if not isinstance(date, str):
        return None
    return {"date": date, "item": str(entry.get("item") or "")}


def _normalize_member(
    user_id: Any, member_data: Any
) -> tuple[int, list[dict[str, str]]] | None:
    """校验签到记录中的单个成员记录，返回 (QQ 号, 逐条签到记录)。

    新格式为逐条记录的数组；旧格式（name/date/count/item 的汇总对象）
    迁移为仅保留最新的一条记录。无效时返回 None。
    """
    if not isinstance(user_id, str) or not user_id.isdigit():
        return None
    if isinstance(member_data, dict):
        entry = _normalize_entry(member_data)
        if entry is None:
            return None
        return int(user_id), [entry]
    if not isinstance(member_data, list):
        return None
    entries = [
        entry
        for entry in (_normalize_entry(raw) for raw in member_data)
        if entry is not None
    ]
    if not entries:
        return None
    return int(user_id), entries


def _normalize_group(
    group_id: Any, group_data: Any
) -> tuple[int, dict[int, list[dict[str, str]]]] | None:
    """校验签到记录中的单个群记录，返回 (群号, 群内成员记录)。"""
    if not isinstance(group_id, str) or not group_id.isdigit():
        return None
    if not isinstance(group_data, dict):
        return None
    members = [
        member
        for member in (
            _normalize_member(user_id, member_data)
            for user_id, member_data in group_data.items()
        )
        if member is not None
    ]
    if not members:
        return None
    return int(group_id), dict(members)


def _load_signs() -> tuple[dict[int, dict[int, list[dict[str, str]]]], bool]:
    """从本地文件读取签到记录，文件不存在或损坏时返回 (空记录, False)。

    返回的第二个元素表示原始数据是否含有旧格式（成员记录为 name/date/
    count/item 汇总对象）的记录；为 True 时应把仅保留每人最新一条的
    规范化结果写回文件。
    """
    if not _SIGN_FILE.exists():
        return {}, False
    try:
        raw = json.loads(_SIGN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"读取签到记录失败，本次将视为无签到记录：{exc}")
        return {}, False
    if not isinstance(raw, dict):
        logger.warning("签到记录格式异常（应为 JSON 对象），本次将视为无签到记录")
        return {}, False
    legacy = any(
        isinstance(member_data, dict)
        for group_data in raw.values()
        if isinstance(group_data, dict)
        for member_data in group_data.values()
    )
    normalized = [
        item
        for item in (
            _normalize_group(group_id, group_data)
            for group_id, group_data in raw.items()
        )
        if item is not None
    ]
    return dict(normalized), legacy


def _save_signs() -> None:
    """将签到记录写入本地文件，写入失败时仅记录错误。"""
    try:
        _SIGN_FILE.write_text(
            json.dumps(_signs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.error(f"写入签到记录失败，本次修改未保存：{exc}")


# 签到记录：群号 -> 成员 QQ 号 -> 逐条签到记录列表，每条记录含 date
# 签到日期（本地时区 YYYY-MM-DD）与 item 获得的道具编号（抽取失败为 ""）
_signs, _migrated = _load_signs()
if _signs:
    _member_count = sum(len(members) for members in _signs.values())
    logger.info(f"已加载签到记录，共 {len(_signs)} 个群、{_member_count} 名成员")
if _migrated:
    # 旧格式（每人一条汇总记录）迁移后仅保留每人最新一条，立即写回磁盘
    logger.info("签到记录已由旧格式迁移，每个成员仅保留最新一条记录")
    _save_signs()

# 加载时依赖声明：签到奖励由决斗插件的分数服务发放、道具由道具插件的
# 道具服务发放（见 _shared/services.py）。require 保证提供方插件先完成
# 加载、服务已完成注册；服务缺失时插件加载失败并给出明确日志
require("duel")
require("item")
_score_service = require_service(DuelScoreService)
_item_service = require_service(ItemService)


def _current_date() -> str:
    """返回当前日期（本地时区，YYYY-MM-DD），用于判断当天是否已签到。

    以本地时区的自然日为准，0 点后视为新的一天。
    """
    return datetime.now(UTC).astimezone().date().isoformat()


def _sender_display_name(event: GroupMessageEvent) -> str:
    """返回发送者在群内的显示名（群昵称优先，否则 QQ 昵称）。"""
    card = (event.sender.card or "").strip()
    nickname = (event.sender.nickname or "").strip()
    return card or nickname or f"QQ {event.user_id}"


def _try_sign(
    group_id: int, user_id: int, name: str, date: str
) -> tuple[bool, GrantedItem | None]:
    """尝试为成员签到（在同步段调用，避免并发事件重复签到）。

    当天尚未签到时：追加一条签到记录、经分数服务发放决斗分数奖励、
    经道具服务发放随机道具（编号写入本次记录，抽取失败为 ""），最后
    落盘并返回 (True, 道具发放结果或 None)；当天已签到时：不改变任何
    状态，返回 (False, None)。
    """
    group = _signs.setdefault(group_id, {})
    records = group.setdefault(user_id, [])
    if any(record["date"] == date for record in records):
        return False, None
    records.append({"date": date, "item": ""})
    _score_service.add_score(group_id, user_id, name, _score_gain)
    granted = _item_service.grant_random(group_id, user_id, name)
    if granted is not None:
        records[-1]["item"] = granted.item_id
    _save_signs()
    if granted is None:
        item_text = "，道具抽取失败"
    else:
        item_text = f"，获得道具 {granted.item_id} {granted.item_name}"
    logger.info(
        f"群 {group_id} 成员 {user_id}（{name}）签到成功"
        f"（累计 {len(records)} 次），决斗分数+{_score_gain}{item_text}"
    )
    return True, granted


async def _send_text(
    bot: Bot, group_id: int, text: str, reply_to: int | None = None
) -> None:
    """向群聊发送文本消息，作为纯文本段构造以避免文案被解析为 CQ 码。"""
    segments: list[MessageSegment] = []
    if reply_to is not None:
        segments.append(MessageSegment.reply(reply_to))
    segments.append(MessageSegment.text(text))
    try:
        await bot.call_api(
            "send_group_msg", group_id=group_id, message=Message(segments)
        )
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"发送签到消息失败（群 {group_id}）：{exc}")


# /sign 命令：仅限群聊使用
sign_cmd = on_command("sign", rule=is_type(GroupMessageEvent))


@sign_cmd.handle()
async def handle_sign(bot: Bot, event: GroupMessageEvent) -> None:
    """处理 /sign 命令：每日签到，成功时发放决斗分数与随机道具。"""
    name = _sender_display_name(event)
    # 同步段：判定并更新记录、发放奖励与道具，避免并发事件在判定与更新之间插入
    signed, granted = _try_sign(event.group_id, event.user_id, name, _current_date())
    if not signed:
        logger.debug(f"群 {event.group_id} 成员 {event.user_id} 今天已签到，跳过")
        await _send_text(
            bot, event.group_id, _text("duplicate"), reply_to=event.message_id
        )
        return
    if granted is None:
        text = _text("success_no_item", score=_score_gain)
    else:
        text = _text(
            "success", score=_score_gain, item=f"{granted.item_id} {granted.item_name}"
        )
    await _send_text(bot, event.group_id, text, reply_to=event.message_id)
    if granted is not None:
        # 先发送签到文案，再由道具服务处理黑色诅咒的信息播报与自动使用
        await _item_service.handle_acquisition(
            bot, event.group_id, event.user_id, name, granted
        )


logger.info(f"签到已启用，每次签到决斗分数+{_score_gain}并随机获得一件道具")
