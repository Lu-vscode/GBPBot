"""实名群群昵称检查插件。

检查实名群内每条群消息发送者的群昵称（群名片）是否符合配置的格式，
不符合时按具体原因发送对应的"伪@"提醒（纯文本 "@群昵称 " 前缀，
不会真正 @ 对方），并按配置限制提醒频率。
群主、管理员或超级用户可通过 /exempt @成员 理由 将成员加入免验证名单，
名单使用 localstore 长期存储在本地且按群隔离，名单内成员不再被检查。
"""

import json
import re
import time
from collections import deque
from datetime import datetime, timezone

from nonebot import get_plugin_config, logger, on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot_plugin_localstore import get_plugin_data_file
from pydantic import BaseModel

__plugin_meta__ = PluginMetadata(
    name="实名群昵称检查",
    description="检查实名群成员的群昵称是否符合规定格式，不符合时发送伪@提醒",
    usage=(
        "配置实名群群号后自动生效，检查群内每条消息发送者的群昵称；"
        "/exempt @成员 理由：将成员加入免验证名单（群主、管理员或超级用户可用）"
    ),
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


# 免验证名单存储文件（localstore 插件数据目录），按群号隔离
_EXEMPTION_FILE = get_plugin_data_file("exemptions.json")


def _load_exemptions() -> dict[int, dict[int, dict[str, str]]]:
    """从本地存储读取免验证名单，文件不存在或损坏时返回空名单。"""
    if not _EXEMPTION_FILE.exists():
        return {}
    try:
        raw = json.loads(_EXEMPTION_FILE.read_text(encoding="utf-8"))
        return {
            int(group_id): {
                int(user_id): {str(key): str(value) for key, value in record.items()}
                for user_id, record in members.items()
            }
            for group_id, members in raw.items()
        }
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        logger.warning(f"读取免验证名单失败，本次将视为空名单：{exc}")
        return {}


def _save_exemptions() -> None:
    """将免验证名单写入本地存储，写入失败时仅记录错误。"""
    try:
        _EXEMPTION_FILE.write_text(
            json.dumps(_exemptions, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.error(f"写入免验证名单失败，本次修改未保存：{exc}")


# 免验证名单：群号 -> 成员 QQ 号 -> 记录（理由、操作人、加入时间）
_exemptions = _load_exemptions()
if _exemptions:
    total = sum(len(members) for members in _exemptions.values())
    logger.info(f"已加载免验证名单，共 {total} 名成员")


def _add_exemption(group_id: int, user_id: int, reason: str, operator_id: int) -> None:
    """将成员加入免验证名单并写入本地存储。"""
    _exemptions.setdefault(group_id, {})[user_id] = {
        "reason": reason,
        "operator": str(operator_id),
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save_exemptions()


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
    # 不在检查范围的群、或在免验证名单中的成员，跳过检查
    if event.group_id not in group_ids or event.user_id in _exemptions.get(
        event.group_id, {}
    ):
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


# /exempt 命令：仅超级用户、群管理员与群主可用
exempt_cmd = on_command(
    "exempt",
    permission=SUPERUSER | GROUP_ADMIN | GROUP_OWNER,
)


def _parse_exempt_args(args: Message) -> tuple[int | None, str]:
    """从命令参数中解析目标成员 QQ 号与理由。

    以第一个 @ 段作为目标成员，其之前的内容会被忽略；@全体成员或没有
    @ 段时目标为 None，理由为目标成员之后的部分，未填写时为空字符串。
    """
    for index, segment in enumerate(args):
        if segment.type != "at":
            continue
        qq = str(segment.data.get("qq", ""))
        if not qq.isdigit():
            return None, ""
        reason = "".join(str(part) for part in args[index + 1 :] if part.is_text())
        return int(qq), reason.strip()
    return None, ""


@exempt_cmd.handle()
async def handle_exempt(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """处理 /exempt 命令：将成员加入本群免验证名单。"""
    if event.group_id not in group_ids:
        await exempt_cmd.finish("本群不在实名群检查范围内，无需添加免验证。")

    target, reason = _parse_exempt_args(args)
    if target is None:
        await exempt_cmd.finish(
            "请 @ 要加入免验证名单的群成员后重试"
            "（格式：/exempt @群成员 理由，不支持 @全体成员）。"
        )
    if target == int(bot.self_id):
        await exempt_cmd.finish("不能将机器人自身加入免验证名单。")
    if not reason:
        await exempt_cmd.finish(
            "请补充将该成员加入免验证名单的理由，格式：/exempt @群成员 理由"
        )

    _add_exemption(event.group_id, target, reason, event.user_id)
    logger.info(
        f"已将成员 {target} 加入群 {event.group_id} 的免验证名单"
        f"（理由：{reason}，操作人：{event.user_id}）"
    )
    await exempt_cmd.finish(f"已将 QQ 号 {target} 添加到免验证名单，理由：{reason}")
