"""道具「篡天」（编号 002）：金色传说、可选型。

使用 /item.use 002 <表达式> 篡改 Bot 接受决斗的概率函数
（DUEL_BOT_ACCEPT_FUNC）：表达式须为合法的 Python 表达式（用 x 指代
决斗点数），且在全部可选点数上都能求值为 0~1 之间的浮点数。不合法时
提示问题所在且不消耗道具；合法时改写当前环境的配置文件
（.env.{environment}）中的对应配置项并重启 Bot 使其生效（进程退出后
由守护进程如 systemd 自动拉起）。篡改会在下次更新部署时被覆盖。
"""

import asyncio
import os
import re
import sys
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values
from nonebot import get_driver, logger

from src.plugins._shared.services import DuelBotAcceptService, require_service
from src.plugins.item._framework import (
    ItemDefinition,
    ItemType,
    ItemUseContext,
    Quality,
    add_item,
    register_item,
)

# 道具编号与目标配置项
_ITEM_ID = "002"
_ENV_KEY = "DUEL_BOT_ACCEPT_FUNC"

# 重启延迟（秒）：确保成功消息送达后再退出进程
_RESTART_DELAY_SECONDS = 3.0

# 目标配置行的匹配（整行替换；兼容 export 前缀与行首空白）
_ACCEPT_FUNC_LINE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?DUEL_BOT_ACCEPT_FUNC[ \t]*=.*$",
    re.MULTILINE,
)

# 配置文件读取与写入的异常集合（无权限、编码错误等）
_ENV_FILE_ERRORS = (OSError, UnicodeDecodeError)

_USAGE = (
    f"请提供概率函数表达式，用法：/item.use {_ITEM_ID} <表达式>"
    "（表达式为 Python 表达式，用 x 指代决斗点数，如 1/x）"
)
_ENV_ACCESS_ERROR = "环境配置文件访问失败，本次未消耗道具。"
_ENV_WRITE_ERROR = "天意未能被篡改：写入配置文件失败，道具已退还。"

# 表达式校验服务（由决斗插件提供；_framework 已 require("duel") 保证
# 服务已注册；服务缺失时本道具模块加载失败并被加载器忽略）
_accept_service = require_service(DuelBotAcceptService)


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


def _render_env_text(original: str, expr: str) -> str:
    """把表达式渲染进配置文件文本：已有配置行时整行替换，否则末尾追加。"""
    new_line = f"{_ENV_KEY}={_quote_value(expr)}"
    if _ACCEPT_FUNC_LINE.search(original):
        return _ACCEPT_FUNC_LINE.sub(lambda _match: new_line, original)
    if original and not original.endswith("\n"):
        original += "\n"
    return f"{original}{new_line}\n"


def _env_text_ok(text: str, expr: str) -> bool:
    """用 python-dotenv 解析渲染结果，确认目标配置项的取值等于表达式。"""
    try:
        values = dotenv_values(stream=StringIO(text))
    except ValueError:
        return False
    return values.get(_ENV_KEY) == expr


def _prepare_update(context: ItemUseContext) -> tuple[Path, str] | str:
    """预检本次使用并把表达式渲染进配置文件文本。

    返回 (配置文件路径, 渲染后的完整文本)；预检失败时返回错误文案
    （此时不消耗道具）。可失败的操作（表达式校验、配置文件读取与
    渲染自检）都在这里完成，供同步的 can_use 与使用效果复用。
    """
    expr = " ".join(context.args)
    if not expr:
        return _USAGE
    error = _accept_service.validate_accept_func(expr)
    if error is not None:
        return error
    path = _env_file_path()
    if not path.parent.is_dir():
        logger.error(f"篡天：配置文件所在目录不存在（{path.resolve()}）")
        return _ENV_ACCESS_ERROR
    try:
        original = _read_env_text(path)
    except _ENV_FILE_ERRORS as exc:
        logger.error(f"篡天：读取配置文件失败（{path.resolve()}）：{exc!r}")
        return _ENV_ACCESS_ERROR
    new_text = _render_env_text(original, expr)
    if not _env_text_ok(new_text, expr):
        logger.error(f"篡天：渲染后的配置文本自检未通过（{path.resolve()}）")
        return _ENV_ACCESS_ERROR
    return path, new_text


def _can_use(context: ItemUseContext) -> str | None:
    """使用前校验：表达式与配置文件预检；返回错误文案时本次不消耗。"""
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
    """使用效果：把表达式写入配置文件并重启 Bot 使其生效。"""
    prepared = _prepare_update(context)
    if isinstance(prepared, str):
        # 不可达：can_use 已预检通过（配置文件在两次读取之间被外部改动时兜底）
        logger.error(f"篡天：消耗后的预检未通过（{prepared}）")
        add_item(context.group_id, context.user_id, context.item.item_id)
        await context.send(_ENV_WRITE_ERROR)
        return
    path, new_text = prepared
    try:
        _write_env_text(path, new_text)
    except _ENV_FILE_ERRORS as exc:
        logger.error(f"篡天：写入配置文件失败（{path.resolve()}）：{exc!r}")
        add_item(context.group_id, context.user_id, context.item.item_id)
        await context.send(_ENV_WRITE_ERROR)
        return
    expr = " ".join(context.args)
    await context.send(
        f"你使用了 {context.item.label}，已将 Bot 接受决斗的概率函数篡改为：{expr}\n"
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
            f'使用指令"/item.use {_ITEM_ID} <表达式>"修改Bot接受决斗的概率函数。'
            "<表达式>须为合法的Python表达式，用x指代决斗点数，"
            "在可选点数范围内表达式的值须为大于等于0小于等于1的浮点数。"
        ),
        condition="无",
        timing="任意",
        note="篡改会在下次更新时被天意的大手覆盖。",
        accepts_args=True,
        can_use=_can_use,
        handle_use=_handle_use,
    )
)
