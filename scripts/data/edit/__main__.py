"""GBPBot 数据文件安全编辑工具的 CLI 入口。

用法（在项目根目录运行）：
    .venv/bin/python -m scripts.data.edit <命令> [选项]

入口会先确认机器人服务已停止（运行中拒绝修改），修改类操作自动
备份原文件并以临时文件 + 原子替换写盘。退出码：0 成功；1 环境或
参数错误；2 安全拒绝（服务运行中、数据校验失败等）；3 用户取消。
"""

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

from . import _inventory, _pipeline, _scores, _states

# 受支持的数据文件注册表：(相对路径, 展示名, 数据模块)
_SUPPORTED_FILES = (
    (_scores.FILE_REL, _scores.LABEL, _scores),
    (_inventory.FILE_REL, _inventory.LABEL, _inventory),
    (_states.FILE_REL, _states.LABEL, _states),
)


def build_parser() -> argparse.ArgumentParser:
    """构建完整的命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.data.edit",
        description=(
            "GBPBot 数据文件安全编辑工具：机器人运行中拒绝修改，修改前自动备份，"
            "以临时文件 + 原子替换写盘（任何一步失败都不会触碰原文件）。"
        ),
    )
    _pipeline.add_global_options(parser, suppressed=False)

    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<命令>")

    verify_parser = subparsers.add_parser("verify", help="校验受支持的数据文件")
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
    backup_restore = backup_verbs.add_parser("restore", help="恢复指定备份集")
    _pipeline.add_global_options(backup_restore, suppressed=True)
    backup_restore.add_argument(
        "stamp", metavar="时间戳", help="备份集时间戳（见 backup list）"
    )
    backup_restore.add_argument(
        "--file",
        dest="target",
        default=None,
        metavar="相对路径",
        help="只恢复指定文件（如 duel/scores.json）",
    )
    backup_restore.set_defaults(handler=cmd_backup_restore)

    for _, _, module in _SUPPORTED_FILES:
        module.register(subparsers)
    return parser


def cmd_verify(ctx: _pipeline.Context, _args: argparse.Namespace) -> int:
    """校验全部受支持数据文件（只读）。"""
    has_problem = False
    for rel, label, module in _SUPPORTED_FILES:
        path = ctx.data_dir / rel
        if not path.exists():
            print(f"[{rel}] 文件不存在（视为空数据）")
            continue
        try:
            raw = _pipeline.load_json_if_exists(path, label)
        except _pipeline.SafetyError as exc:
            has_problem = True
            print(f"[{rel}] 异常：{exc}")
            continue
        data, issues = module.normalize(raw)
        if issues:
            has_problem = True
            print(f"[{rel}] 发现 {len(issues)} 处问题（相关记录会被插件加载丢弃）：")
            for issue in issues:
                print(f"  - {issue}")
        else:
            print(f"[{rel}] 正常：{module.summary(data)}")
    if has_problem:
        print("校验完成：存在问题。")
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


def _collect_restore_sources(
    ctx: _pipeline.Context, stamp: str, target: str | None
) -> list[tuple[Path, str, Any]]:
    """收集并校验待恢复文件，返回（相对路径, 展示名, 干净数据）列表。"""
    root = _pipeline.backup_root(ctx.data_dir)
    if not _pipeline.is_backup_set_name(stamp):
        message = f"时间戳格式不正确：{stamp!r}"
        raise _pipeline.SafetyError(message)
    backup_set = root / stamp
    if not backup_set.is_dir():
        message = f"备份集不存在：{stamp}（可运行 backup list 查看）"
        raise _pipeline.SafetyError(message)

    supported = {rel: (label, module) for rel, label, module in _SUPPORTED_FILES}
    if target:
        target_rel = Path(target)
        if target_rel not in supported:
            names = "、".join(str(rel) for rel in supported)
            message = f"不支持恢复该文件：{target}（仅支持：{names}）"
            raise _pipeline.SafetyError(message)
        source_rels = [target_rel]
    else:
        source_rels = [rel for rel in supported if (backup_set / rel).is_file()]

    plans: list[tuple[Path, str, Any]] = []
    for rel in source_rels:
        source = backup_set / rel
        if not source.is_file():
            message = f"备份集中不存在该文件：{rel}"
            raise _pipeline.SafetyError(message)
        label, module = supported[rel]
        raw = _pipeline.load_json_if_exists(source, label)
        clean, issues = module.normalize(raw)
        if issues:
            message = (
                f"备份文件 {rel} 自身存在 {len(issues)} 处问题，已拒绝恢复："
                + "；".join(issues)
            )
            raise _pipeline.SafetyError(message)
        plans.append((rel, label, clean))
    if not plans:
        message = "该备份集中没有可恢复的受支持文件。"
        raise _pipeline.SafetyError(message)
    return plans


def cmd_backup_restore(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """从备份集恢复数据文件（走完整安全流程，恢复前再备份当前文件）。"""
    plans = _collect_restore_sources(ctx, args.stamp, args.target)
    state, raw_state = _pipeline.service_state(ctx.service_name)
    _pipeline.enforce_service_gate(ctx, state, raw_state)
    print(f"[目标] 从备份集 {args.stamp} 恢复以下文件：")
    for rel, label, _ in plans:
        print(f"  - {rel}（{label}）")
    if ctx.dry_run:
        print("[dry-run] 已预览恢复计划，未写入磁盘。")
        return 0
    _pipeline.confirm(ctx)
    with _pipeline.data_lock(ctx.data_dir):
        new_backup = _pipeline.make_backup_set(ctx.data_dir)
        for rel, _, clean in plans:
            path = ctx.data_dir / rel
            if path.exists():
                current_backup = new_backup / rel
                current_backup.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(path, current_backup)
                except OSError as exc:
                    message = f"备份当前文件失败，已中止：{exc}"
                    raise _pipeline.SafetyError(message) from exc
            _pipeline.write_json_atomic(path, clean)
    print("[完成] 恢复完成，已写回以下文件：")
    for rel, _, _ in plans:
        print(f"  - {ctx.data_dir / rel}")
    print(f"[备份] 本次操作前的文件已备份到：{new_backup}")
    for rel, _, _ in plans:
        print(f'[回滚] cp "{new_backup / rel}" "{ctx.data_dir / rel}"')
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
        print("\n[中断] 已退出，未完成的修改不会写入原文件。")
        return 3


if __name__ == "__main__":
    sys.exit(main())
