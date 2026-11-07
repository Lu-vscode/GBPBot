"""数据安全编辑的公共管线（数据库版）。

修改类操作固定经过：机器人服务运行检查（运行中拒绝）-> 交互确认 ->
数据库快照备份 -> 单事务写入；任何一步失败都不会留下部分修改。本模块
不导入任何插件代码，表名与列名与插件模型保持一致（各数据模块内注明
对应的插件源码），表结构变更时必须同步维护。
"""

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 数据库文件相对数据目录的路径（nonebot-plugin-orm 的插件数据目录）
DB_REL = Path("nonebot_plugin_orm") / "db.sqlite3"

# 表结构迁移命令（缺少数据库或表时提示使用者先执行）
_MIGRATE_HINT = "python -m scripts.data.orm upgrade head"

# 备份目录名（位于数据目录下，与插件的插件名子目录互不冲突）
_BACKUP_DIR_NAME = "_backup"

# 备份集保留数量；超出后自动清理最旧的备份集
_BACKUP_KEEP = 20

# 备份集目录名：本地时间戳（同一秒内的多次操作追加 -2、-3 等序号）
_BACKUP_SET_PATTERN = re.compile(r"^\d{8}-\d{6}(-\d+)?$")

# systemctl is-active 对不存在的服务单元返回的退出码
_SYSTEMCTL_NO_SUCH_UNIT = 4


class SafetyError(Exception):
    """安全门禁或数据校验不通过，已拒绝执行（CLI 退出码 2）。"""


class OperationCancelledError(Exception):
    """用户在确认环节取消操作（CLI 退出码 3）。"""


@dataclass
class Context:
    """一次 CLI 调用的全局参数。"""

    data_dir: Path
    """数据目录（localstore 根，如 /opt/gbpbot/data）。"""

    service_name: str
    """机器人 systemd 服务名（默认 gbpbot）。"""

    assume_yes: bool
    """是否跳过交互确认（-y）。"""

    dry_run: bool
    """是否只预览变更、不写入磁盘。"""


@dataclass
class DbChangePlan:
    """一次待执行的数据修改（由数据模块构造后交给 execute_change）。"""

    label: str
    """数据表的展示名（用于输出与报错）。"""

    changes: list[str]
    """变更摘要行（为空表示数据与目标值一致，无需修改）。"""

    apply: Callable[[sqlite3.Connection], None]
    """在写事务内执行的修改语句（抛异常时整体回滚）。"""


def default_data_dir() -> Path:
    """默认数据目录：仓库根/data（由脚本位置推导，与 cwd 无关）。"""
    return Path(__file__).resolve().parents[3] / "data"


def backup_root(data_dir: Path) -> Path:
    """数据目录下的备份根目录路径。"""
    return data_dir / _BACKUP_DIR_NAME


def is_digits(value: Any) -> bool:
    """判断是否为十进制数字字符串（群号/QQ 号）。"""
    return isinstance(value, str) and value.isdigit()


def db_path(data_dir: Path) -> Path:
    """数据库文件路径（数据目录下的 nonebot_plugin_orm/db.sqlite3）。"""
    return data_dir / DB_REL


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """判断数据库中存在指定表。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def connect_db(
    ctx: Context, tables: Sequence[str], *, db: Path | None = None
) -> sqlite3.Connection:
    """打开数据库并确认所需的表存在，返回以列名访问的连接（调用方负责关闭）。

    db 为 None 时使用数据目录下的默认数据库路径（迁移脚本可指定其它库）。
    """
    path = db_path(ctx.data_dir) if db is None else db
    if not path.is_file():
        message = (
            f"数据库文件不存在：{path}\n"
            f"  请先在项目根目录执行表结构迁移：{_MIGRATE_HINT}"
        )
        raise SafetyError(message)
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        missing = [name for name in tables if not _table_exists(conn, name)]
    except sqlite3.Error as exc:
        message = f"打开数据库失败（{path}）：{exc}"
        raise SafetyError(message) from exc
    if missing:
        conn.close()
        message = (
            f"数据库缺少表：{'、'.join(missing)}\n"
            f"  请先在项目根目录执行表结构迁移：{_MIGRATE_HINT}"
        )
        raise SafetyError(message)
    return conn


def load_json_if_exists(path: Path, label: str) -> Any | None:
    """读取 JSON 文件；文件不存在时返回 None，内容损坏时报错拒绝。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        message = f"{label}不是合法的 JSON（{path}）：{exc}"
        raise SafetyError(message) from exc
    except OSError as exc:
        message = f"{label}读取失败（{path}）：{exc}"
        raise SafetyError(message) from exc


def issues_error(label: str, issues: list[str]) -> SafetyError:
    """把校验问题渲染为拒绝执行的错误对象（调用处以 raise 抛出）。"""
    lines = [
        f"{label}存在 {len(issues)} 处问题，已拒绝操作（可运行 verify 查看详情）："
    ]
    lines.extend(f"  - {issue}" for issue in issues)
    return SafetyError("\n".join(lines))


def service_state(service_name: str) -> tuple[str, str]:
    """查询机器人服务的运行状态，返回（分类, systemctl 原始输出）。

    分类取值：
    - running：运行中（active/activating/deactivating）
    - stopped：已停止（inactive/failed）
    - missing：未找到该服务单元
    - unavailable：本机无 systemctl（如本地开发环境）
    - unknown：无法确认状态
    """
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except FileNotFoundError:
        return "unavailable", ""
    except (OSError, subprocess.SubprocessError) as exc:
        return "unknown", str(exc)
    output = proc.stdout.strip()
    if proc.returncode == _SYSTEMCTL_NO_SUCH_UNIT:
        return "missing", output
    if output in {"active", "activating", "deactivating"}:
        return "running", output
    if output in {"inactive", "failed"}:
        return "stopped", output
    return "unknown", output


def enforce_service_gate(ctx: Context, state: str, raw: str) -> None:
    """按服务状态决定放行或拒绝：仅确认已停止（或无 systemctl）时放行。

    dry-run 模式不写盘，运行中只警告不拒绝。
    """
    if state == "stopped":
        return
    if state == "unavailable":
        print("[提示] 本机 systemctl 不可用（本地环境），跳过服务运行检查。")
        return
    if state == "running":
        message = (
            f"机器人服务 {ctx.service_name} 正在运行，已拒绝修改数据文件：\n"
            "  运行中修改会被机器人内存中的旧数据覆盖。\n"
            "  请先停止服务，修改完成后再启动：\n"
            f"    sudo systemctl stop {ctx.service_name}\n"
            f"    sudo systemctl start {ctx.service_name}"
        )
    elif state == "missing":
        message = (
            f"未找到 systemd 服务单元 {ctx.service_name}，无法确认机器人是否在运行，"
            "已拒绝执行。若实际使用的服务名不同，请用 --service-name 指定。"
        )
    else:
        message = (
            f"无法确认服务 {ctx.service_name} 的状态（systemctl 输出异常：{raw!r}），"
            "已拒绝执行。"
        )
    if ctx.dry_run:
        print(f"[警告] {message}")
        return
    raise SafetyError(message)


@contextmanager
def data_lock(data_dir: Path) -> Iterator[None]:
    """对数据目录加排他文件锁，防止多个会话同时修改（本地 Windows 无锁）。"""
    try:
        import fcntl
    except ImportError:
        yield
        return
    lock_path = backup_root(data_dir) / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def make_backup_set(data_dir: Path) -> Path:
    """创建本次操作的备份集目录并清理过旧备份；同秒冲突自动加序号。"""
    root = backup_root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    base = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    stamp = base
    counter = 2
    while (root / stamp).exists():
        stamp = f"{base}-{counter}"
        counter += 1
    backup_set = root / stamp
    backup_set.mkdir()
    prune_backup_sets(root)
    return backup_set


def is_backup_set_name(name: str) -> bool:
    """判断目录名是否为备份集时间戳名。"""
    return bool(_BACKUP_SET_PATTERN.fullmatch(name))


def list_backup_sets(root: Path) -> list[Path]:
    """列出全部备份集目录（按名升序，即从旧到新）。"""
    if not root.is_dir():
        return []
    backups = [
        path
        for path in root.iterdir()
        if path.is_dir() and is_backup_set_name(path.name)
    ]
    return sorted(backups, key=lambda path: path.name)


def prune_backup_sets(root: Path) -> None:
    """只保留最近 _BACKUP_KEEP 个备份集，删除更旧的。"""
    backups = list_backup_sets(root)
    for old in backups[:-_BACKUP_KEEP]:
        shutil.rmtree(old, ignore_errors=True)


def backup_database(ctx: Context, backup_set: Path) -> Path:
    """把当前数据库快照到备份集目录，返回备份文件路径。

    快照用 `VACUUM INTO` 生成：内容一致、不含未提交的改动，也不会
    继承 -wal/-shm 残留；备份集里的文件可直接拷回作为数据库文件使用。
    """
    source = db_path(ctx.data_dir)
    target = backup_set / DB_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(source)
    except sqlite3.Error as exc:
        message = f"打开数据库失败（{source}）：{exc}"
        raise SafetyError(message) from exc
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        message = f"备份数据库失败，已中止（未修改数据）：{exc}"
        raise SafetyError(message) from exc
    finally:
        conn.close()
    return target


def confirm(ctx: Context) -> None:
    """请求用户确认执行（-y 跳过；无法交互确认且未指定 -y 时拒绝）。"""
    if ctx.assume_yes:
        return
    if not sys.stdin.isatty():
        message = "当前环境无法交互确认变更，请加 -y 跳过后重试。"
        raise SafetyError(message)
    try:
        answer = input("确认执行？[y/N] ").strip().lower()
    except EOFError:
        message = "未收到确认输入（标准输入已关闭），已取消，未做任何修改。"
        raise OperationCancelledError(message) from None
    if answer not in {"y", "yes"}:
        raise OperationCancelledError("已取消，未做任何修改。")


def execute_change(ctx: Context, plan: DbChangePlan, *, tables: Sequence[str]) -> None:
    """执行一次安全修改：服务门禁 -> 预览 -> 确认 -> 快照备份 -> 单事务写入。"""
    if not plan.changes:
        print("没有需要修改的内容（数据与目标值一致）。")
        return
    state, raw = service_state(ctx.service_name)
    enforce_service_gate(ctx, state, raw)
    print(f"[目标] {plan.label}（{db_path(ctx.data_dir)}）")
    print("将执行以下修改：")
    for line in plan.changes:
        print(f"  - {line}")
    if ctx.dry_run:
        print("[dry-run] 已预览全部变更，未写入数据库。")
        return
    confirm(ctx)
    with data_lock(ctx.data_dir):
        backup_path = backup_database(ctx, make_backup_set(ctx.data_dir))
        conn = connect_db(ctx, tables)
        try:
            conn.execute("BEGIN IMMEDIATE")
            plan.apply(conn)
            conn.commit()
        except sqlite3.Error as exc:
            with suppress(sqlite3.Error):
                conn.rollback()
            message = f"写入数据库失败，已整体回滚：{exc}"
            raise SafetyError(message) from exc
        finally:
            conn.close()
    print(f"[完成] 已更新：{plan.label}")
    print(f"[备份] {backup_path}")
    print(
        f'[回滚] cp "{backup_path}" "{db_path(ctx.data_dir)}"'
        "（并删除同目录下的 -wal/-shm 文件）"
    )
    if state == "unavailable":
        print("[下一步] 请按你的方式重启机器人使修改生效。")
    else:
        print(f"[下一步] 请启动机器人：sudo systemctl start {ctx.service_name}")


def add_group_user_args(parser: argparse.ArgumentParser) -> None:
    """为子命令添加 -g/--group 与 -u/--user 参数。"""
    parser.add_argument("-g", "--group", required=True, metavar="群号", help="群号")
    parser.add_argument(
        "-u", "--user", required=True, metavar="QQ号", help="成员 QQ 号"
    )


def add_global_options(parser: argparse.ArgumentParser, *, suppressed: bool) -> None:
    """在 parser 上注册全局参数（--data-dir/--service-name/-y/--dry-run）。

    suppressed=True 用于子命令层：未显式给出时不写入默认值，避免覆盖
    子命令之前已解析到的全局参数（这样全局参数可写在命令的任意位置）。
    """
    data_dir_default: Any = argparse.SUPPRESS if suppressed else None
    service_default: Any = argparse.SUPPRESS if suppressed else "gbpbot"
    flag_default: Any = argparse.SUPPRESS if suppressed else False
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        default=data_dir_default,
        help="数据目录（默认：仓库根/data）",
    )
    parser.add_argument(
        "--service-name",
        metavar="NAME",
        default=service_default,
        help="机器人 systemd 服务名（默认 gbpbot）",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        default=flag_default,
        help="跳过交互确认（非交互环境必须）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=flag_default,
        help="只预览变更，不写入磁盘",
    )
