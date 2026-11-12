"""猜拳决斗插件的文案目录。

可配置文案在 model.py 的 Config 中声明（`DUEL_TEXT_*`），默认值与允许的
占位符在本模块维护；启动时校验配置文案的占位符格式，无效时回退默认
文案。用法与边界情况提示（不支持配置）也在本模块集中定义。全部文案
统一经 text() 取出。
"""

from typing import Any

from nonebot import logger

from src.plugins.duel.config.model import plugin_config
from src.plugins.duel.config.values import MAX_MULTIPLIER

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
SCORE_SUM_DENIED = "查看本群决斗分数总和仅超级用户可用。"
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
