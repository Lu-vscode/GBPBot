"""签到插件。

在群聊中发送 `/sign` 进行每日签到：每人每天在本群可签到一次（以本地
时区的自然日为准，0 点后重置），签到记录使用 localstore 长期存储在
本地且按群隔离（群号 -> 成员 QQ 号 -> 记录）；签到成功可获得决斗
分数奖励（默认 1 分，SIGN_SCORE 可配置）。

与决斗插件的交互通过跨插件服务实现（见 _shared/services.py）：加载时
用 require("duel") 声明依赖，确保分数服务提供方已加载并完成注册；
签到时经服务注册中心取用分数服务，为签到者增加决斗分数。
"""

import json
from datetime import datetime, timezone
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

from src.plugins._shared.services import DuelScoreService, require_service

__plugin_meta__ = PluginMetadata(
    name="签到",
    description="群聊每日签到，每人每天一次，签到成功可获得决斗分数奖励",
    usage="/sign：在本群签到（每人每天一次），签到成功可获得决斗分数奖励",
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 每次签到获得的决斗分数默认值
_DEFAULT_SCORE_GAIN = 1

# 文档中直接出现的文案对应的默认值，键与 SIGN_TEXT_* 配置一一对应
# （配置项名 = "sign_text_" + 键）
_TEXT_DEFAULTS = {
    "success": "签到成功！决斗分数+{score}。",
    "duplicate": "今天已经签到过了，明天再来吧。",
}

# 每条文案允许使用的占位符，启动时校验配置的文案与占位符是否匹配
_TEXT_FIELDS = {
    "success": ("score",),
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
    """签到成功时的提示文案，占位符 {score} 获得的决斗分数。"""

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


def _normalize_record(user_id: Any, record: Any) -> tuple[int, dict[str, Any]] | None:
    """校验签到记录中的单条成员记录，无效时返回 None。"""
    if not isinstance(user_id, str) or not user_id.isdigit():
        return None
    if not isinstance(record, dict):
        return None
    date = record.get("date")
    count = record.get("count")
    if not isinstance(date, str):
        return None
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    return int(user_id), {
        "name": str(record.get("name") or ""),
        "date": date,
        "count": count,
    }


def _normalize_group(
    group_id: Any, group_data: Any
) -> tuple[int, dict[int, dict[str, Any]]] | None:
    """校验签到记录中的单个群记录，返回 (群号, 群内成员记录)。"""
    if not isinstance(group_id, str) or not group_id.isdigit():
        return None
    if not isinstance(group_data, dict):
        return None
    members = [
        item
        for item in (
            _normalize_record(user_id, record) for user_id, record in group_data.items()
        )
        if item is not None
    ]
    if not members:
        return None
    return int(group_id), dict(members)


def _load_signs() -> dict[int, dict[int, dict[str, Any]]]:
    """从本地文件读取签到记录，文件不存在或损坏时返回空记录。"""
    if not _SIGN_FILE.exists():
        return {}
    try:
        raw = json.loads(_SIGN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"读取签到记录失败，本次将视为无签到记录：{exc}")
        return {}
    if not isinstance(raw, dict):
        logger.warning("签到记录格式异常（应为 JSON 对象），本次将视为无签到记录")
        return {}
    normalized = [
        item
        for item in (
            _normalize_group(group_id, group_data)
            for group_id, group_data in raw.items()
        )
        if item is not None
    ]
    return dict(normalized)


def _save_signs() -> None:
    """将签到记录写入本地文件，写入失败时仅记录错误。"""
    try:
        _SIGN_FILE.write_text(
            json.dumps(_signs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.error(f"写入签到记录失败，本次修改未保存：{exc}")


# 签到记录：群号 -> 成员 QQ 号 -> 记录（name 最近一次签到的显示名、
# date 最近一次签到的日期（本地时区 YYYY-MM-DD）、count 累计签到次数）
_signs = _load_signs()
if _signs:
    _member_count = sum(len(members) for members in _signs.values())
    logger.info(f"已加载签到记录，共 {len(_signs)} 个群、{_member_count} 名成员")

# 加载时依赖声明：签到奖励由决斗插件的分数服务发放（见 _shared/services.py）。
# require 保证决斗插件先完成加载、分数服务已完成注册；服务缺失时插件
# 加载失败并给出明确日志
require("duel")
_score_service = require_service(DuelScoreService)


def _current_date() -> str:
    """返回当前日期（本地时区，YYYY-MM-DD），用于判断当天是否已签到。

    以本地时区的自然日为准，0 点后视为新的一天。
    """
    return datetime.now(timezone.utc).astimezone().date().isoformat()


def _sender_display_name(event: GroupMessageEvent) -> str:
    """返回发送者在群内的显示名（群昵称优先，否则 QQ 昵称）。"""
    card = (event.sender.card or "").strip()
    nickname = (event.sender.nickname or "").strip()
    return card or nickname or f"QQ {event.user_id}"


def _try_sign(group_id: int, user_id: int, name: str, date: str) -> bool:
    """尝试为成员签到（在同步段调用，避免并发事件重复签到）。

    当天尚未签到时：更新签到记录并落盘、经分数服务发放决斗分数奖励，
    返回 True；当天已签到时：不改变任何状态，返回 False。
    """
    group = _signs.setdefault(group_id, {})
    record = group.get(user_id)
    if record is not None and record["date"] == date:
        return False
    count = 1 if record is None else int(record["count"]) + 1
    group[user_id] = {"name": name, "date": date, "count": count}
    _save_signs()
    _score_service.add_score(group_id, user_id, name, _score_gain)
    logger.info(
        f"群 {group_id} 成员 {user_id}（{name}）签到成功"
        f"（累计 {count} 次），决斗分数+{_score_gain}"
    )
    return True


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
    """处理 /sign 命令：每日签到，成功时发放决斗分数奖励。"""
    name = _sender_display_name(event)
    # 同步段：判定并更新记录、发放奖励，避免并发事件在判定与更新之间插入
    if not _try_sign(event.group_id, event.user_id, name, _current_date()):
        logger.debug(f"群 {event.group_id} 成员 {event.user_id} 今天已签到，跳过")
        await _send_text(
            bot, event.group_id, _text("duplicate"), reply_to=event.message_id
        )
        return
    await _send_text(
        bot,
        event.group_id,
        _text("success", score=_score_gain),
        reply_to=event.message_id,
    )


logger.info(f"签到已启用，每次签到决斗分数+{_score_gain}")
