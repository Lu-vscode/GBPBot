"""实名群群昵称检查插件。

检查实名群内每条群消息发送者的群昵称（群名片）是否符合配置的格式，
不符合时按具体原因发送对应的"伪@"提醒（纯文本 "@群昵称 " 前缀，
不会真正 @ 对方），并按配置限制提醒频率。
"""

import re
import time
from collections import deque

from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.plugin import PluginMetadata
from pydantic import BaseModel

__plugin_meta__ = PluginMetadata(
    name="实名群昵称检查",
    description="检查实名群成员的群昵称是否符合规定格式，不符合时发送伪@提醒",
    usage="配置实名群群号后自动生效，检查群内每条消息发送者的群昵称",
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 提醒文案配置缺失时使用的兜底文案
_FALLBACK_REMIND_TEXT = "你的群昵称格式不符合规定。"

# 提醒原因分析用的分段校验（基于 年级-院系-姓名-生源地简称 的结构）：
# 年级段以两位半角数字开头、姓名段以至少两个汉字开头、生源地段以省份简称开头
_GRADE_PATTERN = re.compile(r"[0-9]{2}")
_NAME_PATTERN = re.compile(r"[\u4e00-\u9fa5]{2}")
# 车牌号省份简称字符集（用于分析生源地部分的提醒原因）
_PROVINCE_ABBR = (
    "京津冀晋蒙辽吉黑沪苏浙皖闽赣鲁豫鄂湘粤桂琼渝川蜀黔贵云滇藏陕秦甘陇青宁新台港澳"
)
# 昵称由四个部分组成：年级-院系-姓名-生源地简称
_NICKNAME_PART_COUNT = 4


class Config(BaseModel):
    """实名群昵称检查插件配置。

    可在 `.env.{environment}` 文件中通过 `REAL_NAME_*` 系列变量配置。
    群号列表与昵称格式为必填项，缺失时插件报错停止工作；
    提醒文案缺失或为空时使用兜底文案；提醒频率缺失时使用内置默认值。
    """

    real_name_group_ids: list[int] | None = None
    """【必填】需要检查群昵称的实名群群号列表，缺失或为空时插件报错停止工作。"""

    real_name_nickname_pattern: str | None = None
    """【必填】群昵称需符合的正则表达式，使用整串匹配，缺失时插件报错停止工作。"""

    real_name_remind_text_not_set: str | None = None
    """未设置群昵称（群名片为空）时的提醒文案，缺失或为空时使用兜底文案。"""

    real_name_remind_text_missing_part: str | None = None
    """群昵称缺少必要分段时的提醒文案，缺失或为空时使用兜底文案。"""

    real_name_remind_text_extra_part: str | None = None
    """群昵称分段多于四部分时的提醒文案，缺失或为空时使用兜底文案。"""

    real_name_remind_text_grade: str | None = None
    """年级部分不符合要求时的提醒文案，缺失或为空时使用兜底文案。"""

    real_name_remind_text_department: str | None = None
    """院系部分不符合要求时的提醒文案，缺失或为空时使用兜底文案。"""

    real_name_remind_text_name: str | None = None
    """姓名部分不符合要求时的提醒文案，缺失或为空时使用兜底文案。"""

    real_name_remind_text_origin: str | None = None
    """生源地部分不符合要求时的提醒文案，缺失或为空时使用兜底文案。"""

    real_name_remind_text_generic: str | None = None
    """其他不符合格式情况下的通用提醒文案，缺失或为空时使用兜底文案。"""

    real_name_remind_member_cooldown: int = 600
    """同一成员两次提醒的最小间隔（秒）。"""

    real_name_remind_group_max: int = 5
    """同一群聊在统计窗口内最多提醒的次数。"""

    real_name_remind_group_window: int = 60
    """群聊提醒频率的统计窗口（秒）。"""


plugin_config = get_plugin_config(Config)

# 群号列表与昵称格式为必填配置，缺失或为空时直接报错，插件停止工作
group_ids = plugin_config.real_name_group_ids or []
pattern = plugin_config.real_name_nickname_pattern or ""
if not group_ids or not pattern:
    missing_keys = [
        key
        for key, value in (
            ("REAL_NAME_GROUP_IDS", group_ids),
            ("REAL_NAME_NICKNAME_PATTERN", pattern),
        )
        if not value
    ]
    raise RuntimeError(
        f"实名群昵称检查插件缺少必填配置（缺失或为空）：{'、'.join(missing_keys)}"
    )

_nickname_regex = re.compile(pattern)

logger.info(f"实名群昵称检查已启用，检查群：{group_ids}")

# 各不合规原因对应的提醒文案，可分别通过 REAL_NAME_REMIND_TEXT_* 配置，
# 缺失或为空的文案使用兜底文案
_remind_texts = {
    "not_set": plugin_config.real_name_remind_text_not_set or _FALLBACK_REMIND_TEXT,
    "missing_part": (
        plugin_config.real_name_remind_text_missing_part or _FALLBACK_REMIND_TEXT
    ),
    "extra_part": (
        plugin_config.real_name_remind_text_extra_part or _FALLBACK_REMIND_TEXT
    ),
    "grade": plugin_config.real_name_remind_text_grade or _FALLBACK_REMIND_TEXT,
    "department": (
        plugin_config.real_name_remind_text_department or _FALLBACK_REMIND_TEXT
    ),
    "name": plugin_config.real_name_remind_text_name or _FALLBACK_REMIND_TEXT,
    "origin": plugin_config.real_name_remind_text_origin or _FALLBACK_REMIND_TEXT,
    "generic": plugin_config.real_name_remind_text_generic or _FALLBACK_REMIND_TEXT,
}


def _diagnose(display_name: str) -> str:
    """分析昵称不符合格式的原因，返回对应原因标识。

    分析基于标准结构（年级-院系-姓名-生源地简称）；
    若自定义了其他正则，分析结果可能不适用，此时返回 generic。
    """
    parts = [part.strip() for part in display_name.split("-")]
    if len(parts) != _NICKNAME_PART_COUNT:
        return "missing_part" if len(parts) < _NICKNAME_PART_COUNT else "extra_part"
    if _GRADE_PATTERN.match(parts[0]) is None:
        return "grade"
    if not parts[1]:
        return "department"
    if _NAME_PATTERN.match(parts[2]) is None:
        return "name"
    if not parts[3] or parts[3][0] not in _PROVINCE_ABBR:
        return "origin"
    return "generic"


# 提醒限流记录：成员维度记录上次提醒时间，群维度记录窗口内的提醒时间队列
_member_last_remind: dict[tuple[int, int], float] = {}
_group_remind_history: dict[int, deque[float]] = {}

# 优先级设为 0 且不阻断事件传播：保证每条群消息都会先被检查，
# 又不会影响其他插件（命令、默认回复等）继续处理消息
real_name_group_check = on_message(priority=0, block=False)


def _take_remind_quota(group_id: int, user_id: int, now: float) -> bool:
    """尝试占用一次提醒额度，成功时记录本次提醒并返回 True。

    需同时满足两个限制：同一成员在冷却时间内不重复提醒，
    同一群聊在统计窗口内的提醒次数不超过上限。
    """
    last = _member_last_remind.get((group_id, user_id))
    if last is not None and now - last < plugin_config.real_name_remind_member_cooldown:
        return False

    window = plugin_config.real_name_remind_group_window
    history = _group_remind_history.setdefault(group_id, deque())
    while history and now - history[0] >= window:
        history.popleft()
    if len(history) >= plugin_config.real_name_remind_group_max:
        return False

    history.append(now)
    _member_last_remind[(group_id, user_id)] = now
    return True


@real_name_group_check.handle()
async def handle_real_name_group_check(event: GroupMessageEvent) -> None:
    """检查发言者的群昵称，不符合格式时发送伪@提醒。"""
    if event.anonymous is not None:
        # 匿名消息没有群昵称，跳过
        return
    if event.group_id not in group_ids:
        return

    # 群名片为空时，成员在群内显示的是 QQ 昵称，同样按显示名检查
    card = (event.sender.card or "").strip()
    display_name = card or (event.sender.nickname or "").strip()
    if not display_name:
        return
    if _nickname_regex.fullmatch(display_name):
        return

    # 群名片为空时原因即为未设置群昵称，否则进一步分析昵称结构
    reason = _diagnose(display_name) if card else "not_set"

    now = time.monotonic()
    if not _take_remind_quota(event.group_id, event.user_id, now):
        logger.debug(
            f"昵称提醒触发限流，跳过：群 {event.group_id} 成员 {event.user_id}"
        )
        return

    # 用文本消息段构造伪@，确保昵称中的 "[CQ:...]" 等内容不会被解析成真实消息段
    remind_text = _remind_texts[reason]
    message = MessageSegment.text(f"@{display_name} {remind_text}")
    try:
        await real_name_group_check.send(message)
    except (ActionFailed, NetworkError) as exc:
        logger.warning(f"发送实名群昵称提醒失败：{exc}")
        return
    logger.info(
        f"已提醒成员 {event.user_id}（{display_name}）修改群昵称（原因：{reason}）"
    )
