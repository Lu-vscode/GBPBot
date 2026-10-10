"""猜拳决斗插件配置与文案。

配置项在 Config 中声明，可在 `.env.{environment}` 文件中通过 `DUEL_*`
系列变量设置；缺失、为空或无法解析时回退默认行为（超时 10 分钟、
群昵称最长 12 字符、点数上限 100、每小时最多发起 20 次决斗、接受
概率 1/点数、内置文案）。环境变量中的纯数字会被 nonebot 全局配置
JSON 解码为数值或布尔值，解析层已做归一兼容。解析结果以本模块的
模块级常量为准（大写名，如 DURATION_SECONDS、MAX_MULTIPLIER、
HOURLY_LIMIT）；配置的文案统一经 text() 取出；本模块不依赖包内其它
模块，供各模块共享。
"""

import math
from collections.abc import Callable
from typing import Any

from nonebot import get_plugin_config, logger
from pydantic import BaseModel, ValidationInfo, field_validator

# 决斗超时时间的默认值（分钟）、bot 消息中群昵称的最大显示长度
# （字符数）、决斗点数上限、决斗发起的每小时次数上限（全局）
_DEFAULT_DURATION_MINUTES = 10.0
_DEFAULT_NICKNAME_MAX_LENGTH = 12
_DEFAULT_MAX_MULTIPLIER = 100
_DEFAULT_HOURLY_LIMIT = 20

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

    避免变量留空或填写错误（如填成 "abc"）导致 pydantic 校验失败、
    插件加载失败（nonebot 仅记录日志），决斗配置静默失效。环境变量
    中的纯数字会被 nonebot 全局配置 JSON 解码为 int/float（布尔值、
    数组等同理），这里统一归一：整数配置只接受整值，其余按无效处理。
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
    缺失或为空时使用默认行为（超时 10 分钟、群昵称最长 12 字符、
    点数上限 100、每小时最多发起 20 次决斗、接受概率 1/点数、内置
    文案）；机器人名单见 BOT_LIST 共享配置（_shared/config.py）。
    """

    duel_duration: float | None = None
    """决斗的存在时间上限（分钟），超过后自动超时结束，默认 10。"""

    duel_nickname_max_length: int | None = None
    """bot 消息中群昵称的最大显示长度（字符数），超过时截断并以"…"结尾，默认 12。"""

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
        "duel_nickname_max_length",
        "duel_max_multiplier",
        "duel_hourly_limit",
        mode="before",
    )
    @classmethod
    def int_as_number(cls, value: Any, info: ValidationInfo) -> Any:
        """解析整数配置：空字符串或无法解析为整数时视为未配置。"""
        return _number_or_none(value, info, int)


plugin_config = get_plugin_config(Config)


def _positive_or_default(
    value: float | None, default: float, name: str, requirement: str
) -> float:
    """返回配置的正数值；未配置或非正数时记 warning 并使用默认值。"""
    if value is None:
        return default
    if value <= 0:
        logger.warning(
            f"猜拳决斗配置 {name}={value:g} 无效（{requirement}），"
            f"已使用默认值 {default:g}"
        )
        return default
    return float(value)


# 决斗超时时间（分钟）与超时判定用的秒数
DURATION_MINUTES = _positive_or_default(
    plugin_config.duel_duration,
    _DEFAULT_DURATION_MINUTES,
    "DUEL_DURATION",
    "需为正数，单位分钟",
)
DURATION_SECONDS = DURATION_MINUTES * 60.0

# bot 消息中群昵称的最大显示长度（字符数）
NICKNAME_MAX_LENGTH = int(
    _positive_or_default(
        plugin_config.duel_nickname_max_length,
        _DEFAULT_NICKNAME_MAX_LENGTH,
        "DUEL_NICKNAME_MAX_LENGTH",
        "需为正整数",
    )
)

# 决斗点数的上限
MAX_MULTIPLIER = int(
    _positive_or_default(
        plugin_config.duel_max_multiplier,
        _DEFAULT_MAX_MULTIPLIER,
        "DUEL_MAX_MULTIPLIER",
        "需为正整数",
    )
)

# 决斗发起的每小时次数上限（全局，全部群聊合计）
HOURLY_LIMIT = int(
    _positive_or_default(
        plugin_config.duel_hourly_limit,
        _DEFAULT_HOURLY_LIMIT,
        "DUEL_HOURLY_LIMIT",
        "需为正整数",
    )
)


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


def accept_expr_error(expr: str) -> str | None:
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
    for x in range(1, MAX_MULTIPLIER + 1):
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
    text_value = (expr or "").strip()
    if not text_value:
        return _default_bot_accept_probability
    error = accept_expr_error(text_value)
    if error is not None:
        logger.error(
            f"猜拳决斗配置 DUEL_BOT_ACCEPT_FUNC={text_value!r} 校验未通过"
            f"（{error}），已使用默认函数 1/点数"
        )
        return _default_bot_accept_probability
    code = compile(text_value, "<DUEL_BOT_ACCEPT_FUNC>", "eval")

    def accept_probability(multiplier: int) -> float:
        return float(eval(code, {"__builtins__": {}}, {"x": multiplier}))

    logger.info(f"机器人接受决斗的概率函数已配置：{text_value}")
    return accept_probability


# 机器人接受决斗的概率函数：点数 -> 接受概率
BOT_ACCEPT_PROBABILITY = _bot_accept_func_or_default(plugin_config.duel_bot_accept_func)

# /duel 的用法提示（点数上限可配置，需在解析配置后构建）
USAGE_DUEL = (
    "请 @ 要发起决斗的群成员，用法：/duel @群成员 [数值]（如 /duel 2 @群成员），"
    f"数值需为不超过 {MAX_MULTIPLIER} 的正整数，默认为 1"
)

# 文档未定义的边界情况文案（用法与错误提示），不支持配置
USAGE_ACCEPT = "请 @ 发起决斗的群成员，用法：/duel.accept @群成员"
USAGE_REJECT = "请 @ 发起决斗的群成员，用法：/duel.reject @群成员"
USAGE_RANK = (
    "参数无法识别，用法：/duel.rank (high|h (<条数>)) (low|l (<条数>))，"
    "条数需为正整数且默认为 10"
)
USAGE_SCORE = "参数无法识别，用法：/duel.score"
USAGE_STATUS = "参数无法识别，用法：/duel.status"
STATUS_ALL_DENIED = "查看本群全部决斗状态仅超级用户可用。"
PAIR_ACTIVE = "你们之间已有一场进行中的决斗，请等待该决斗结束或超时后再发起。"
PROVOKED_ALREADY_STARTED = "你与 {challenger} 的决斗已经开始了，无需再接受。"
PROVOKED_CANNOT_REJECT = "你与 {challenger} 的决斗已经开始了，无法再拒绝。"
RANK_TITLE_HIGH = "决斗高分榜"
RANK_TITLE_LOW = "决斗低分榜"
RANK_EMPTY = "（暂无）"
# 排行榜合并转发中节点显示的发送者名称
RANK_NODE_NAME = "决斗分数排行榜"
# 决斗发起次数达到每小时上限时的提示文案，
# 占位符 {limit} 每小时上限次数、{minutes} 预计等待分钟数
LIMIT_REACHED = (
    "本小时的决斗发起次数已达上限（全部群聊合计每小时最多 {limit} 次），"
    "请约 {minutes} 分钟后重试。"
)


def _text_or_default(key: str, configured: str | None) -> str:
    """校验并返回配置的文案，缺失或占位符格式无效时使用默认文案。"""
    default = _TEXT_DEFAULTS[key]
    text_value = (configured or "").strip()
    if not text_value:
        return default
    try:
        text_value.format(**dict.fromkeys(_TEXT_FIELDS[key], "占位"))
    except (IndexError, KeyError, ValueError) as exc:
        logger.warning(
            f"猜拳决斗配置 DUEL_TEXT_{key.upper()} 的占位符格式无效"
            f"（{exc}），已使用默认文案"
        )
        return default
    return text_value


# 全部决斗文案：文案键 -> 最终使用的文案（配置或默认值）
_texts = {
    key: _text_or_default(key, getattr(plugin_config, f"duel_text_{key}"))
    for key in _TEXT_DEFAULTS
}


def text(key: str, **kwargs: Any) -> str:
    """取出文案并填充占位符。"""
    return _texts[key].format(**kwargs)
