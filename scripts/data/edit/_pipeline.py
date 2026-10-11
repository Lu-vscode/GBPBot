"""数据文件安全编辑的公共管线。

修改类操作固定经过：机器人服务运行检查（运行中拒绝）-> 交互确认 ->
自动备份 -> 原子替换写盘；任何一步失败都不会触碰原文件。本模块不
导入任何插件代码，仅与各插件数据文件的格式约定保持一致（各数据
模块内注明对应的插件源码）。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
class ChangePlan:
    """一次待执行的单文件修改（由数据模块构造后交给 execute_change）。"""

    path: Path
    """目标数据文件路径。"""

    label: str
    """数据文件的展示名（用于输出与报错）。"""

    changes: list[str]
    """变更摘要行（为空表示数据与目标值一致，无需修改）。"""

    new_data: Any
    """修改后的完整数据（写盘前会再经 validator 复验并采用其清理结果）。"""

    validator: Callable[[Any], tuple[Any, list[str]]]
    """数据模块的校验函数（返回值同 normalize：干净数据 + 问题列表）。"""

    expected_mtime_ns: int | None
    """读取原文件时记录的修改时间（纳秒）；None 表示当时文件不存在。"""


def default_data_dir() -> Path:
    """默认数据目录：仓库根/data（由脚本位置推导，与 cwd 无关）。"""
    return Path(__file__).resolve().parents[3] / "data"


def backup_root(data_dir: Path) -> Path:
    """数据目录下的备份根目录路径。"""
    return data_dir / _BACKUP_DIR_NAME


def is_digits(value: Any) -> bool:
    """判断是否为十进制数字字符串（群号/QQ 号）。"""
    return isinstance(value, str) and value.isdigit()


def current_mtime_ns(path: Path) -> int | None:
    """返回文件的修改时间（纳秒）；文件不存在时返回 None。"""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


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


def write_json_atomic(path: Path, data: Any) -> None:
    """以临时文件 + 原子替换写入 JSON（UTF-8、缩进 2、中文不转义）。

    写入格式与各插件 save 函数一致；替换失败时原文件保持不变。
    """
    text = json.dumps(data, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
    except OSError as exc:
        message = f"创建临时文件失败，未修改原文件：{exc}"
        raise SafetyError(message) from exc
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            with suppress(OSError):
                tmp.chmod(path.stat().st_mode & 0o777)
        tmp.replace(path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        message = f"写入数据文件失败，原文件未修改：{exc}"
        raise SafetyError(message) from exc


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


def execute_change(ctx: Context, plan: ChangePlan) -> None:
    """执行一次安全修改：服务门禁 -> 预览 -> 确认 -> 备份 -> 原子写盘。"""
    if not plan.changes:
        print("没有需要修改的内容（数据与目标值一致）。")
        return
    state, raw = service_state(ctx.service_name)
    enforce_service_gate(ctx, state, raw)
    print(f"[目标] {plan.label}（{plan.path}）")
    print("将执行以下修改：")
    for line in plan.changes:
        print(f"  - {line}")
    if ctx.dry_run:
        print("[dry-run] 已预览全部变更，未写入磁盘。")
        return
    confirm(ctx)
    backup_path: Path | None = None
    with data_lock(ctx.data_dir):
        if current_mtime_ns(plan.path) != plan.expected_mtime_ns:
            message = (
                "数据文件在本次操作期间被其它进程修改（可能机器人仍在运行），"
                "已拒绝写入，请重试。"
            )
            raise SafetyError(message)
        clean, issues = plan.validator(plan.new_data)
        if issues:
            message = "内部错误：待写入数据未通过校验：" + "；".join(issues)
            raise SafetyError(message)
        if plan.path.exists():
            backup_set = make_backup_set(ctx.data_dir)
            backup_path = backup_set / plan.path.relative_to(ctx.data_dir)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(plan.path, backup_path)
            except OSError as exc:
                message = f"备份原文件失败，已中止（原文件未修改）：{exc}"
                raise SafetyError(message) from exc
        write_json_atomic(plan.path, clean)
    print(f"[完成] 已更新：{plan.path}")
    if backup_path is None:
        print("[备份] 原文件不存在，本次为新建，无需备份。")
    else:
        print(f"[备份] {backup_path}")
        print(f'[回滚] cp "{backup_path}" "{plan.path}"')
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
