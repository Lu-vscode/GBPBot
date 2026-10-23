"""道具「篡天」（编号 002）：金色传说、可选型。

使用以下指令之一改写 Bot 配置（均写入当前环境的配置文件
.env.{environment}，随后重启 Bot 使其生效）：

1. /item.use 002 DUEL_BOT_ACCEPT_FUNC <表达式>：修改 Bot 接受决斗的
   概率函数（表达式支持 math 库，x 指代决斗点数，值须为 0~1）；
2. /item.use 002 BOT_LIST @使用对象：把成员的 QQ 号加入机器人名单；
3. /item.use 002 NICKNAME_GUARD_DISABLED_GROUPS：禁用本群的群昵称保护。

参数不合法（表达式有误、目标不合适或配置已生效等）时提示问题所在
且不消耗道具；成功时消耗道具、改写配置并在数秒后重启进程（由守护
进程如 systemd 自动拉起）。篡改会在下次更新部署时被覆盖。
"""

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values
from nonebot import get_driver, logger

from src.plugins._shared.config import (
    get_bot_list,
    get_nickname_guard_disabled_groups,
)
from src.plugins._shared.services import DuelBotAcceptService, require_service
from src.plugins.item._framework import (
    ItemDefinition,
    ItemType,
    ItemUseContext,
    Quality,
    add_item,
    register_item,
)

# 道具编号与三种篡改目标的配置项
_ITEM_ID = "002"
_ACCEPT_FUNC_KEY = "DUEL_BOT_ACCEPT_FUNC"
_BOT_LIST_KEY = "BOT_LIST"
_NICKNAME_GUARD_KEY = "NICKNAME_GUARD_DISABLED_GROUPS"

# 重启延迟（秒）：确保成功消息送达后再退出进程
_RESTART_DELAY_SECONDS = 3.0

# 目标配置行的匹配（整行替换；兼容 export 前缀与行首空白）
_ENV_LINE_PATTERN = r"^[ \t]*(?:export[ \t]+)?{key}[ \t]*=.*$"

# 配置文件读取与写入的异常集合（无权限、编码错误等）
_ENV_FILE_ERRORS = (OSError, UnicodeDecodeError)

_USAGE = (
    f"请提供篡改目标，用法：/item.use {_ITEM_ID} {_ACCEPT_FUNC_KEY} <表达式>、"
    f"/item.use {_ITEM_ID} {_BOT_LIST_KEY} @使用对象 或 "
    f"/item.use {_ITEM_ID} {_NICKNAME_GUARD_KEY}"
)
_USAGE_ACCEPT_FUNC = (
    f"请提供概率函数表达式，用法：/item.use {_ITEM_ID} {_ACCEPT_FUNC_KEY} <表达式>"
    "（表达式用 x 指代决斗点数，支持四则运算、幂运算与 math 库的常量、"
    "函数，如 1/x、math.sin(x)）"
)
_USAGE_BOT_LIST = (
    f"请 @ 一位要加入 Bot 名单的成员，用法："
    f"/item.use {_ITEM_ID} {_BOT_LIST_KEY} @使用对象"
)
_USAGE_NICKNAME_GUARD = (
    f"该指令不带额外参数，用法：/item.use {_ITEM_ID} {_NICKNAME_GUARD_KEY}"
)
_ENV_ACCESS_ERROR = "环境配置文件访问失败，本次未消耗道具。"
_ENV_WRITE_ERROR = "天意未能被篡改：写入配置文件失败，道具已退还。"

# 表达式校验服务（由决斗插件提供；_framework 已 require("duel") 保证
# 服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_accept_service = require_service(DuelBotAcceptService)


@dataclass(frozen=True)
class _Change:
    """一次配置改写：目标配置项、写入值、自检期望值与成功描述。"""

    key: str
    """配置项名称（写入时位于等号左侧）。"""

    value: str
    """配置项的值文本（写入时位于等号右侧，字符串值已加引号转义）。"""

    expected: str | tuple[int, ...]
    """写入后 dotenv 解析出的期望值：字符串原样比较，元组按 JSON 数组比较。"""

    summary: str
    """成功后的动作描述（拼入成功播报）。"""


@dataclass(frozen=True)
class _EnvUpdate:
    """一次通过预检的配置改写结果（供 can_use 与 handle_use 复用）。"""

    path: Path
    """当前环境的配置文件路径。"""

    text: str
    """改写后的完整文件文本。"""

    summary: str
    """成功后的动作描述。"""


def _env_file_path() -> Path:
    """返回当前环境配置文件（`.env.{environment}`）的路径。"""
    return Path(f".env.{get_driver().config.environment}")


def _read_env_text(path: Path) -> str:
    """读取配置文件文本；文件不存在时视为空（后续将创建）。"""
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _quote_value(expr: str) -> str:
    """把表达式渲染为配置值（双引号包裹并转义反斜杠与双引号）。"""
    escaped = expr.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _json_list(values: tuple[int, ...]) -> str:
    """把群号或 QQ 号列表渲染为配置值（紧凑 JSON 数组）。"""
    return json.dumps(sorted(values), separators=(",", ":"))


def _line_pattern(key: str) -> re.Pattern[str]:
    """返回匹配目标配置整行的正则（兼容 export 前缀与行首空白）。"""
    return re.compile(_ENV_LINE_PATTERN.format(key=re.escape(key)), re.MULTILINE)


def _render_env_text(original: str, change: _Change) -> str:
    """把配置改写渲染进文件文本：已有配置行时整行替换，否则末尾追加。"""
    new_line = f"{change.key}={change.value}"
    pattern = _line_pattern(change.key)
    if pattern.search(original):
        return pattern.sub(lambda _match: new_line, original)
    if original and not original.endswith("\n"):
        original += "\n"
    return f"{original}{new_line}\n"


def _env_text_ok(text: str, change: _Change) -> bool:
    """用 python-dotenv 解析渲染结果，确认目标配置项的取值符合期望。"""
    try:
        values = dotenv_values(stream=StringIO(text))
    except ValueError:
        return False
    raw = values.get(change.key)
    if raw is None:
        return False
    if isinstance(change.expected, str):
        return raw == change.expected
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return parsed == list(change.expected)


def _accept_func_change(context: ItemUseContext) -> _Change | str:
    """校验概率函数表达式并生成 DUEL_BOT_ACCEPT_FUNC 的改写。"""
    if context.target_ids:
        return _USAGE_ACCEPT_FUNC
    expr = " ".join(context.args[1:])
    if not expr:
        return _USAGE_ACCEPT_FUNC
    error = _accept_service.validate_accept_func(expr)
    if error is not None:
        return error
    return _Change(
        key=_ACCEPT_FUNC_KEY,
        value=_quote_value(expr),
        expected=expr,
        summary=f"已将 Bot 接受决斗的概率函数篡改为：{expr}",
    )


def _bot_list_change(context: ItemUseContext) -> _Change | str:
    """校验 @ 目标并生成 BOT_LIST 的改写（保留现有名单）。"""
    if len(context.args) != 1 or len(context.target_ids) != 1:
        return _USAGE_BOT_LIST
    target = context.target_ids[0]
    if str(target) == context.bot.self_id:
        return "不能将本 Bot 自己加入 Bot 名单。"
    members = get_bot_list()
    if target in members:
        return f"{target} 已在 Bot 名单中，无需重复添加。"
    new_members = tuple(sorted({*members, target}))
    return _Change(
        key=_BOT_LIST_KEY,
        value=_json_list(new_members),
        expected=new_members,
        summary=f"已将 {target} 加入 Bot 名单。",
    )


def _nickname_guard_change(context: ItemUseContext) -> _Change | str:
    """校验本群状态并生成 NICKNAME_GUARD_DISABLED_GROUPS 的改写。"""
    if len(context.args) != 1 or context.target_ids:
        return _USAGE_NICKNAME_GUARD
    disabled = get_nickname_guard_disabled_groups()
    if context.group_id in disabled:
        return "本群的群昵称保护已处于禁用状态，无需重复禁用。"
    groups = tuple(sorted({*disabled, context.group_id}))
    return _Change(
        key=_NICKNAME_GUARD_KEY,
        value=_json_list(groups),
        expected=groups,
        summary=f"已禁用本群（{context.group_id}）的群昵称保护。",
    )


def _change_for(context: ItemUseContext) -> _Change | str:
    """按篡改目标生成本次要写入的配置改写；参数不合法时返回错误文案。"""
    if not context.args:
        return _USAGE
    command = context.args[0]
    if command == _ACCEPT_FUNC_KEY:
        return _accept_func_change(context)
    if command == _BOT_LIST_KEY:
        return _bot_list_change(context)
    if command == _NICKNAME_GUARD_KEY:
        return _nickname_guard_change(context)
    return f"无法识别的篡改目标「{command}」。{_USAGE}"


def _prepare_update(context: ItemUseContext) -> _EnvUpdate | str:
    """预检本次使用并生成改写后的配置文件文本。

    返回改写结果；预检失败时返回错误文案（此时不消耗道具）。可失败
    的操作（参数与可行性校验、配置文件读取与渲染自检）都在这里完成，
    供同步的 can_use 与使用效果复用。
    """
    change = _change_for(context)
    if isinstance(change, str):
        return change
    path = _env_file_path()
    if not path.parent.is_dir():
        logger.error(f"篡天：配置文件所在目录不存在（{path.resolve()}）")
        return _ENV_ACCESS_ERROR
    try:
        original = _read_env_text(path)
    except _ENV_FILE_ERRORS as exc:
        logger.error(f"篡天：读取配置文件失败（{path.resolve()}）：{exc!r}")
        return _ENV_ACCESS_ERROR
    new_text = _render_env_text(original, change)
    if not _env_text_ok(new_text, change):
        logger.error(f"篡天：渲染后的配置文本自检未通过（{path.resolve()}）")
        return _ENV_ACCESS_ERROR
    return _EnvUpdate(path=path, text=new_text, summary=change.summary)


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：参数、可行性与配置文件预检；返回错误文案时不消耗。"""
    result = _prepare_update(context)
    if isinstance(result, str):
        return result
    return None


def _write_env_text(path: Path, text: str) -> None:
    """原子写入配置文件：先写临时文件再替换，失败时清理临时文件。"""
    temp = path.with_name(f"{path.name}.tmp")
    try:
        temp.write_text(text, encoding="utf-8", newline="\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _schedule_restart() -> None:
    """调度重启：延迟数秒（确保消息送达）后退出进程，由守护进程拉起。

    服务器上 Bot 以 systemd 服务运行（Restart=always），进程退出后会被
    自动拉起；开发环境直接运行进程时，重启需手动完成。
    """
    asyncio.get_running_loop().call_later(_RESTART_DELAY_SECONDS, _exit_process)


def _exit_process() -> None:
    """立即退出进程（数据均已即时落盘，无需优雅收尾）。"""
    logger.info("篡天：Bot 正在重启……")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


async def _handle_use(context: ItemUseContext) -> None:
    """使用效果：把配置改写写入文件并重启 Bot 使其生效。"""
    prepared = _prepare_update(context)
    if isinstance(prepared, str):
        # 不可达：can_use 已预检通过（配置文件在两次读取之间被外部改动时兜底）
        logger.error(f"篡天：消耗后的预检未通过（{prepared}）")
        add_item(context.group_id, context.user_id, context.item.item_id)
        await context.send(_ENV_WRITE_ERROR)
        return
    try:
        _write_env_text(prepared.path, prepared.text)
    except _ENV_FILE_ERRORS as exc:
        logger.error(f"篡天：写入配置文件失败（{prepared.path.resolve()}）：{exc!r}")
        add_item(context.group_id, context.user_id, context.item.item_id)
        await context.send(_ENV_WRITE_ERROR)
        return
    await context.send(
        f"你使用了 {context.item.label}，{prepared.summary}\n"
        "天意已被改写，Bot 将在数秒后重启使其生效。"
    )
    _schedule_restart()


register_item(
    ItemDefinition(
        item_id=_ITEM_ID,
        name="篡天",
        quality=Quality.GOLD,
        types=(ItemType.OPTIONAL,),
        description="篡改Bot设定",
        effect=(
            "可以使用一次以下指令之一。\n"
            f'1."/item.use {_ITEM_ID} {_ACCEPT_FUNC_KEY} <表达式>"：'
            "修改Bot接受决斗的概率函数。<表达式>须为合法的Python表达式，"
            "允许使用math库，用x指代决斗点数，在可选点数范围内表达式的值"
            "须为大于等于0小于等于1的浮点数。\n"
            f'2."/item.use {_ITEM_ID} {_BOT_LIST_KEY} @使用对象"：'
            "将使用对象的QQ号加入Bot名单，使其被视为Bot，很多功能被限制。\n"
            f'3."/item.use {_ITEM_ID} {_NICKNAME_GUARD_KEY}"：'
            "禁用本群的群昵称保护功能。"
        ),
        condition="无",
        timing="任意",
        note="篡改会在下次更新时被天意的大手覆盖。",
        accepts_args=True,
        can_use=_can_use,
        handle_use=_handle_use,
    )
)
