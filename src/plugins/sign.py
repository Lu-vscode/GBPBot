"""签到插件。

在群聊中发送 `/sign` 进行每日签到：每人每天在本群可签到一次（以本地
时区的自然日为准，0 点后重置）。签到记录落在数据库表 `gbp_sign_record`，
以 `(群号, 成员 QQ 号, 签到日期)` 唯一约束保证当天只有一条记录（并发
签到由插入冲突判定，不依赖进程内同步段）；签到成功可获得决斗分数奖励
（默认 1 分，SIGN_SCORE 可配置）与一件随机道具。

与决斗、道具插件的交互通过跨插件服务实现（见 _shared/services.py）：
加载时用 require("duel")、require("item") 声明依赖，确保分数与道具
服务提供方已加载并完成注册；签到时经服务注册中心取用分数服务增加
决斗分数（基础分数与道具加成之和）、取用道具服务发放随机道具（黑色
诅咒由道具服务在签到文案之后完成信息播报与自动使用）。
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

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
from pydantic import BaseModel, ValidationInfo, field_validator

require("nonebot_plugin_orm")

from nonebot_plugin_orm import Model, get_session
from sqlalchemy import Integer, String, UniqueConstraint, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Mapped, mapped_column

from src.plugins._shared.services import (
    DuelScoreService,
    GrantedItem,
    ItemService,
    SignScoreBonusService,
    require_service,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

__plugin_meta__ = PluginMetadata(
    name="签到",
    description="群聊每日签到，每人每天一次，签到成功可获得决斗分数奖励与随机道具",
    usage="/sign：在本群签到（每人每天一次），签到成功可获得决斗分数奖励与随机道具",
    type="application",
    supported_adapters={"~onebot.v11"},
)


class SignRecord(Model):
    """成员在单个群内的一次签到记录（每人每天最多一条）。"""

    __tablename__ = "gbp_sign_record"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", "date", name="uq_gbp_sign_record_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    """自增主键。"""

    group_id: Mapped[int] = mapped_column(Integer)
    """群号。"""

    user_id: Mapped[int] = mapped_column(Integer)
    """成员 QQ 号。"""

    date: Mapped[str] = mapped_column(String(10))
    """签到日期（本地时区 YYYY-MM-DD）。"""

    item_id: Mapped[str] = mapped_column(String(3), default="", server_default="")
    """本次签到获得的道具编号（抽取失败为空字符串）。"""


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


# 加载时依赖声明：签到奖励由决斗插件的分数服务发放、道具由道具插件的
# 道具服务发放（见 _shared/services.py）。require 保证提供方插件先完成
# 加载、服务已完成注册；服务缺失时插件加载失败并给出明确日志
require("duel")
require("item")
_score_service = require_service(DuelScoreService)
_item_service = require_service(ItemService)
_score_bonus_service = require_service(SignScoreBonusService)


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


# 奖励发放阶段的异常（单个步骤失败时只记日志，不回滚已写入的签到记录）
_REWARD_ERRORS = (Exception,)


async def _insert_record(group_id: int, user_id: int, date: str) -> bool:
    """写入当天签到记录，返回是否写入成功（当天已签到时返回 False）。

    唯一约束（群号、成员 QQ 号、日期）保证同日只有一条记录，并发签到
    时只有一个事件能插入成功，其余按"今天已签到"处理，不依赖进程内
    同步段。记录先以空道具编号写入，奖励发放后回填。
    """
    statement = (
        sqlite_insert(SignRecord)
        .values(group_id=group_id, user_id=user_id, date=date, item_id="")
        .on_conflict_do_nothing(
            index_elements=[SignRecord.group_id, SignRecord.user_id, SignRecord.date]
        )
    )
    async with get_session() as session, session.begin():
        result = await session.execute(statement)
    return bool(cast("CursorResult[Any]", result).rowcount)


async def _set_record_item(
    group_id: int, user_id: int, date: str, item_id: str
) -> None:
    """把当天签到记录的道具编号回填为实际获得的道具编号。"""
    async with get_session() as session, session.begin():
        await session.execute(
            update(SignRecord)
            .where(
                SignRecord.group_id == group_id,
                SignRecord.user_id == user_id,
                SignRecord.date == date,
            )
            .values(item_id=item_id)
        )


async def _count_records(group_id: int, user_id: int) -> int:
    """返回成员在本群的累计签到次数。"""
    statement = (
        select(func.count())
        .select_from(SignRecord)
        .where(
            SignRecord.group_id == group_id,
            SignRecord.user_id == user_id,
        )
    )
    async with get_session() as session:
        return int(await session.scalar(statement) or 0)


async def _try_sign(
    group_id: int, user_id: int, name: str, date: str
) -> tuple[bool, GrantedItem | None, int]:
    """尝试为成员签到，返回 (是否签到成功, 道具发放结果, 获得的决斗分数)。

    先写入当天签到记录（唯一约束判定是否重复签到）；写入成功后向分数
    服务发放决斗分数（基础分数与道具加成之和，加成见
    SignScoreBonusService）、经道具服务发放随机道具并回填本次记录的
    道具编号。奖励发放中途出错时保留"当天已签到但未发奖"的状态（比
    重复领奖安全）并只记录错误日志；当天已签到时不做任何修改，返回
    (False, None, 0)。
    """
    if not await _insert_record(group_id, user_id, date):
        return False, None, 0
    gain = _score_gain
    try:
        gain += await _score_bonus_service.extra_score(group_id, user_id)
    except _REWARD_ERRORS as exc:
        logger.error(f"签到分数加成的服务调用出错，本次按基础分数发放：{exc!r}")
    awarded = 0
    granted: GrantedItem | None = None
    try:
        await _score_service.add_score(group_id, user_id, name, gain)
        awarded = gain
        granted = await _item_service.grant_random(group_id, user_id, name)
        if granted is not None:
            await _set_record_item(group_id, user_id, date, granted.item_id)
    except _REWARD_ERRORS as exc:
        logger.error(
            f"群 {group_id} 成员 {user_id}（{name}）的签到奖励发放失败，"
            f"当天记录已写入且不补发：{exc!r}"
        )
    try:
        total = await _count_records(group_id, user_id)
    except _REWARD_ERRORS as exc:
        logger.error(f"读取累计签到次数失败（群 {group_id}，成员 {user_id}）：{exc!r}")
        total = 0
    if granted is None:
        item_text = "，道具抽取失败"
    else:
        item_text = f"，获得道具 {granted.item_id} {granted.item_name}"
    logger.info(
        f"群 {group_id} 成员 {user_id}（{name}）签到成功"
        f"（累计 {total} 次），决斗分数+{awarded}{item_text}"
    )
    return True, granted, awarded


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
    # 当天唯一性由记录的唯一约束保证：并发签到只有一个事件能写入记录
    signed, granted, gain = await _try_sign(
        event.group_id, event.user_id, name, _current_date()
    )
    if not signed:
        logger.debug(f"群 {event.group_id} 成员 {event.user_id} 今天已签到，跳过")
        await _send_text(
            bot, event.group_id, _text("duplicate"), reply_to=event.message_id
        )
        return
    if granted is None:
        text = _text("success_no_item", score=gain)
    else:
        text = _text(
            "success", score=gain, item=f"{granted.item_id} {granted.item_name}"
        )
    await _send_text(bot, event.group_id, text, reply_to=event.message_id)
    if granted is not None:
        # 先发送签到文案，再由道具服务处理黑色诅咒的信息播报与自动使用
        await _item_service.handle_acquisition(
            bot, event.group_id, event.user_id, name, granted
        )


logger.info(f"签到已启用，每次签到决斗分数+{_score_gain}并随机获得一件道具")
