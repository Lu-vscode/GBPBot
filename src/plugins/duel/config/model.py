"""猜拳决斗插件的配置模型与数值归一。

配置项在 Config 中声明，可在 `.env.{environment}` 文件中通过 `DUEL_*`
系列变量设置；缺失、为空或无法解析时回退默认行为。环境变量中的纯数字
会被 nonebot 全局配置 JSON 解码为数值或布尔值，本模块的解析层已做归一
兼容，避免变量留空或填写错误（如填成 "abc"）导致 pydantic 校验失败、
插件加载失败。

解析后的模块级常量（时长、点数上限、限频、概率函数）见 values.py 与
accept_expr.py；文案见 texts.py。
"""

import math
from collections.abc import Callable
from typing import Any

from nonebot import get_plugin_config, logger
from pydantic import BaseModel, ValidationInfo, field_validator


def _warn_unparsable_number(value: Any, info: ValidationInfo) -> None:
    """记录数值配置无法解析为有效数字的告警。"""
    name = (info.field_name or "").upper()
    logger.warning(
        f"猜拳决斗配置 {name}={value!r} 无法解析为数字，已视为未配置并使用默认值"
    )


def _parse_number(
    value: Any, info: ValidationInfo, parse: Callable[[str], int | float]
) -> int | float | None:
    """把字符串形式的数值配置解析为目标类型；空串视为未配置且不告警。"""
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return None
        try:
            number = parse(text_value)
        except ValueError:
            number = None
    else:
        number = None
    if number is None:
        _warn_unparsable_number(value, info)
    return number


def _number_or_none(
    value: Any, info: ValidationInfo, parse: Callable[[str], int | float]
) -> Any:
    """把数值配置解析为指定类型；空字符串或无法解析时视为未配置。

    整数配置只接受整值，浮点配置拒绝 NaN/Infinity。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # 环境变量中的纯数字经 nonebot 全局配置 JSON 解码后直接可用
        number: int | float | None = value
    else:
        number = _parse_number(value, info, parse)
    if number is None:
        return None
    if isinstance(number, float):
        if not math.isfinite(number):
            # JSON 可解码出 Infinity/NaN，不是可用的配置数值
            _warn_unparsable_number(value, info)
            return None
        if parse is not float and not number.is_integer():
            # 小数不能用于整数配置（如 DUEL_HOURLY_LIMIT=20.5）
            _warn_unparsable_number(value, info)
            return None
    return float(number) if parse is float else int(number)


class Config(BaseModel):
    """猜拳决斗插件配置。

    可在 `.env.{environment}` 文件中通过 `DUEL_*` 系列变量配置，
    缺失或为空时使用默认行为（超时 10 分钟、点数上限 100、每小时
    最多发起 20 次决斗、接受概率 1/点数、内置文案）；群昵称最大显示
    长度见共享配置 NICKNAME_MAX_LENGTH（_shared/config/nickname.py），机器人
    名单见 BOT_LIST 共享配置。
    """

    duel_duration: float | None = None
    """决斗的存在时间上限（分钟），超过后自动超时结束，默认 10。"""

    duel_max_multiplier: int | None = None
    """决斗点数的上限（正整数），默认 100。"""

    duel_hourly_limit: int | None = None
    """决斗发起的每小时次数上限（正整数，全部群聊合计、全局生效），默认 20。"""

    duel_bot_accept_func: str | None = None
    """机器人接受决斗的概率函数表达式（变量 x 为点数），默认 1/x。

    也可直接填写纯数字表示恒定概率（如 0 恒不接受、0.5 恒 50%）。
    """

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
        "duel_bot_accept_func",
        "duel_text_self",
        "duel_text_bot",
        "duel_text_accepted",
        "duel_text_challenged",
        "duel_text_rejected",
        "duel_text_not_challenged",
        "duel_text_win",
        "duel_text_draw",
        "duel_text_timeout",
        mode="before",
    )
    @classmethod
    def scalar_as_text(cls, value: Any) -> Any:
        """把 JSON 解码产生的数值/布尔值转回字符串，空字符串视为未配置。

        nonebot 全局配置会把纯数字或布尔值的环境变量 JSON 解码成
        int/float/bool（如 DUEL_BOT_ACCEPT_FUNC=0 变成整数 0），字符串
        配置直接透传会校验失败、插件加载失败；这里统一转回字符串
        （"0" 是合法的恒定概率表达式，即机器人恒定不接受）。
        """
        if isinstance(value, (int, float)):
            # 布尔值也是 int 的子类，一并转为 "True"/"False"
            value = str(value)
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("duel_duration", mode="before")
    @classmethod
    def duration_as_number(cls, value: Any, info: ValidationInfo) -> Any:
        """解析超时时间：空字符串或无法解析为数字时视为未配置。"""
        return _number_or_none(value, info, float)

    @field_validator(
        "duel_max_multiplier",
        "duel_hourly_limit",
        mode="before",
    )
    @classmethod
    def int_as_number(cls, value: Any, info: ValidationInfo) -> Any:
        """解析整数配置：空字符串或无法解析为整数时视为未配置。"""
        return _number_or_none(value, info, int)


plugin_config = get_plugin_config(Config)
