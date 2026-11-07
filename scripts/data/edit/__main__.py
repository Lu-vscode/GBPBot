"""GBPBot 数据安全编辑工具的 CLI 入口。

用法（在项目根目录运行）：
    .venv/bin/python -m scripts.data.edit <命令> [选项]

入口会先确认机器人服务已停止（运行中拒绝修改），修改类操作先生成
数据库快照，再在单个事务内写入（失败整体回滚）。退出码：0 成功；
1 环境或参数错误；2 安全拒绝（服务运行中、数据库或表缺失、数据校验
失败等）；3 用户取消。
"""

import argparse
import shutil
import sys
from pathlib import Path

from . import _inventory, _pipeline, _scores, _signs, _states

# 受支持的数据表注册表：(表名, 展示名, 数据模块)
_SUPPORTED_TABLES = (
    (_scores.TABLE, _scores.LABEL, _scores),
    (_inventory.TABLE, _inventory.LABEL, _inventory),
    (_states.TABLE, _states.LABEL, _states),
    (_signs.TABLE, _signs.LABEL, _signs),
)

_TABLE_NAMES = [name for name, _, _ in _SUPPORTED_TABLES]


def build_parser() -> argparse.ArgumentParser:
    """构建完整的命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.data.edit",
        description=(
            "GBPBot 数据安全编辑工具：机器人运行中拒绝修改，修改前自动生成数据库"
            "快照，修改在单个事务内完成（任何一步失败都会整体回滚）。"
        ),
    )
    _pipeline.add_global_options(parser, suppressed=False)

    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<命令>")

    verify_parser = subparsers.add_parser("verify", help="校验受支持的数据表")
    _pipeline.add_global_options(verify_parser, suppressed=True)
    verify_parser.set_defaults(handler=cmd_verify)

    backup_parser = subparsers.add_parser("backup", help="查看 / 恢复自动备份")
    _pipeline.add_global_options(backup_parser, suppressed=True)
    backup_verbs = backup_parser.add_subparsers(
        dest="verb", required=True, metavar="<子命令>"
    )
    backup_list = backup_verbs.add_parser("list", help="列出备份集")
    _pipeline.add_global_options(backup_list, suppressed=True)
    backup_list.set_defaults(handler=cmd_backup_list)
    backup_restore = backup_verbs.add_parser("restore", help="用备份集恢复整个数据库")
    _pipeline.add_global_options(backup_restore, suppressed=True)
    backup_restore.add_argument(
        "stamp", metavar="时间戳", help="备份集时间戳（见 backup list）"
    )
    backup_restore.set_defaults(handler=cmd_backup_restore)

    for _, _, module in _SUPPORTED_TABLES:
        module.register(subparsers)
    return parser


def cmd_verify(ctx: _pipeline.Context, _args: argparse.Namespace) -> int:
    """校验受支持的数据表（只读）。"""
    conn = _pipeline.connect_db(ctx, _TABLE_NAMES)
    has_problem = False
    try:
        for table, label, module in _SUPPORTED_TABLES:
            issues = module.validate(conn)
            if issues:
                has_problem = True
                print(f"[{table}] {label}发现 {len(issues)} 处问题：")
                for issue in issues:
                    print(f"  - {issue}")
            else:
                print(f"[{table}] {label}正常：{module.summary(conn)}")
    finally:
        conn.close()
    if has_problem:
        print("校验完成：存在问题（可用对应的 set/del 等命令修正）。")
        return 2
    print("校验完成：全部正常。")
    return 0


def cmd_backup_list(ctx: _pipeline.Context, _args: argparse.Namespace) -> int:
    """列出全部备份集（只读）。"""
    backups = _pipeline.list_backup_sets(_pipeline.backup_root(ctx.data_dir))
    if not backups:
        print("暂无备份。")
        return 0
    print(f"共 {len(backups)} 个备份集（新到旧）：")
    for backup_set in reversed(backups):
        files = sorted(p for p in backup_set.rglob("*") if p.is_file())
        names = "、".join(str(p.relative_to(backup_set)) for p in files) or "（空）"
        print(f"  {backup_set.name}：{names}")
    return 0


def _remove_journal_files(db: Path) -> None:
    """删除数据库旁的 -wal/-shm 残留（恢复快照后必须清理）。"""
    for suffix in ("-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)


def _restore_source(ctx: _pipeline.Context, stamp: str) -> Path:
    """校验备份集与其中的数据库快照，返回快照文件路径。"""
    if not _pipeline.is_backup_set_name(stamp):
        message = f"时间戳格式不正确：{stamp!r}"
        raise _pipeline.SafetyError(message)
    backup_set = _pipeline.backup_root(ctx.data_dir) / stamp
    if not backup_set.is_dir():
        message = f"备份集不存在：{stamp}（可运行 backup list 查看）"
        raise _pipeline.SafetyError(message)
    source = backup_set / _pipeline.DB_REL
    if not source.is_file():
        message = f"备份集中没有数据库快照：{_pipeline.DB_REL}"
        raise _pipeline.SafetyError(message)
    return source


def cmd_backup_restore(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """用备份集恢复整个数据库（走完整安全流程，恢复前再快照当前库）。"""
    source = _restore_source(ctx, args.stamp)
    state, raw_state = _pipeline.service_state(ctx.service_name)
    _pipeline.enforce_service_gate(ctx, state, raw_state)
    target = _pipeline.db_path(ctx.data_dir)
    print(f"[目标] 用备份集 {args.stamp} 的数据库快照覆盖：{target}")
    if ctx.dry_run:
        print("[dry-run] 已预览恢复计划，未写入磁盘。")
        return 0
    _pipeline.confirm(ctx)
    backup_path: Path | None = None
    with _pipeline.data_lock(ctx.data_dir):
        if target.is_file():
            backup_path = _pipeline.backup_database(
                ctx, _pipeline.make_backup_set(ctx.data_dir)
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            _remove_journal_files(target)
        except OSError as exc:
            hint = f"（可用 {backup_path} 回滚）" if backup_path else ""
            message = f"恢复数据库失败{hint}：{exc}"
            raise _pipeline.SafetyError(message) from exc
    print("[完成] 数据库已恢复。")
    if backup_path is None:
        print("[备份] 恢复前数据库不存在，无需备份。")
    else:
        print(f"[备份] 恢复前的数据库快照：{backup_path}")
        print(
            f'[回滚] cp "{backup_path}" "{target}"（并删除同目录下的 -wal/-shm 文件）'
        )
    if state == "unavailable":
        print("[下一步] 请按你的方式重启机器人使修改生效。")
    else:
        print(f"[下一步] 请启动机器人：sudo systemctl start {ctx.service_name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口，返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else _pipeline.default_data_dir()
    if not data_dir.is_dir():
        print(
            f"错误：数据目录不存在：{data_dir}（可用 --data-dir 指定）",
            file=sys.stderr,
        )
        return 1
    ctx = _pipeline.Context(
        data_dir=data_dir.resolve(),
        service_name=args.service_name,
        assume_yes=args.yes,
        dry_run=args.dry_run,
    )
    try:
        return args.handler(ctx, args)
    except _pipeline.SafetyError as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2
    except _pipeline.OperationCancelledError as exc:
        print(f"[取消] {exc}")
        return 3
    except KeyboardInterrupt:
        print("\n[中断] 已退出，未完成的修改会被回滚。")
        return 3


if __name__ == "__main__":
    sys.exit(main())
