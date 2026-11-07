"""把旧版 JSON 数据一次性迁移到 nonebot-plugin-orm 的 SQLite 数据库。

用法（项目根目录）：
    .venv/bin/python -m scripts.data.migrate_orm [--data-dir PATH] [--db PATH]
                                                [--dry-run] [-y]

流程：服务门禁（运行中拒绝）-> 备份 4 个 JSON -> 校验 JSON -> 校验数据库
表结构 -> 单事务写入 -> 行数核对 -> 把 4 个 JSON 改名归档为
`<原名>.migrated-<时间戳>`；任一环节失败都不改名归档，可修正后重跑。
写入本身是幂等的（分数/库存/状态按主键覆盖，签到忽略重复），因此预演、
部分失败后重跑都安全。

脚本不导入 nonebot 与插件代码：JSON 结构与插件旧版存储格式一致，表名
与列名与插件模型一致，表结构变更时必须同步维护本模块。
"""

import argparse
import json
import re
import shutil
import sqlite3
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .edit import _pipeline

# 迁移涉及的数据表（与插件模型一致）
SCORE_TABLE = "gbp_duel_score"
INVENTORY_TABLE = "gbp_item_inventory"
STATE_TABLE = "gbp_item_state"
SIGN_TABLE = "gbp_sign_record"

_TABLES = (SCORE_TABLE, INVENTORY_TABLE, STATE_TABLE, SIGN_TABLE)

# 各表用于核对的主键列（记录元组的前若干列按此顺序排列）
_KEY_COLUMNS = {
    SCORE_TABLE: "group_id, user_id",
    INVENTORY_TABLE: "group_id, user_id, item_id",
    STATE_TABLE: "group_id, user_id, state_key",
    SIGN_TABLE: "group_id, user_id, date",
}

# 每批核对的主键数量（避免 SQL 参数过多）
_VERIFY_CHUNK = 500

# 归档文件名的日期部分格式
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Source:
    """一个旧版 JSON 数据文件及其规范化结果。"""

    table: str
    """目标数据表。"""

    path: Path
    """JSON 文件路径。"""

    records: list[tuple[Any, ...]] = field(default_factory=list)
    """待写入的记录（主键列在前，可直接参数化写入）。"""

    issues: list[str] = field(default_factory=list)
    """校验问题（非空时拒绝迁移该文件）。"""


def _is_digits(value: Any) -> bool:
    """判断是否为十进制数字字符串（群号/QQ 号）。"""
    return _pipeline.is_digits(value)


# ===== 各表 JSON 的校验与规范化 =====


def _normalize_scores(raw: Any) -> tuple[list[tuple[Any, ...]], list[str]]:
    """校验分数 JSON：群号 -> QQ 号 -> {name, score}。"""
    if not isinstance(raw, dict):
        return [], ["顶层结构不是 JSON 对象"]
    issues: list[str] = []
    records: list[tuple[int, int, str, int]] = []
    for group_key, members in raw.items():
        if not _is_digits(group_key) or not isinstance(members, dict):
            issues.append(f"群记录无效（键：{group_key!r}）")
            continue
        for user_key, record in members.items():
            if not _is_digits(user_key) or not isinstance(record, dict):
                issues.append(f"群 {group_key} 存在无效成员记录（键：{user_key!r}）")
                continue
            score = record.get("score")
            if isinstance(score, bool) or not isinstance(score, int):
                issues.append(
                    f"群 {group_key} 成员 {user_key} 的分数无效（应为整数）：{score!r}"
                )
                continue
            name = str(record.get("name") or "")
            records.append((int(group_key), int(user_key), name, score))
    return records, issues


def _normalize_inventory(raw: Any) -> tuple[list[tuple[Any, ...]], list[str]]:
    """校验库存 JSON：群号 -> QQ 号 -> 道具编号 -> 数量。"""
    if not isinstance(raw, dict):
        return [], ["顶层结构不是 JSON 对象"]
    issues: list[str] = []
    records: list[tuple[int, int, str, int]] = []
    for group_key, users in raw.items():
        if not _is_digits(group_key) or not isinstance(users, dict):
            issues.append(f"群记录无效（键：{group_key!r}）")
            continue
        for user_key, items in users.items():
            if not _is_digits(user_key) or not isinstance(items, dict):
                issues.append(f"群 {group_key} 存在无效成员记录（键：{user_key!r}）")
                continue
            for item_key, count in items.items():
                if not isinstance(item_key, str) or not item_key.isdigit():
                    issues.append(
                        f"群 {group_key} 成员 {user_key} 的道具编号无效：{item_key!r}"
                    )
                    continue
                if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    issues.append(
                        f"群 {group_key} 成员 {user_key} 的道具 {item_key}"
                        f" 数量无效（应为正整数）：{count!r}"
                    )
                    continue
                records.append(
                    (int(group_key), int(user_key), f"{int(item_key):03d}", count)
                )
    return records, issues


def _normalize_states(raw: Any) -> tuple[list[tuple[Any, ...]], list[str]]:
    """校验状态 JSON：群号 -> QQ 号 -> [状态条目, ...]。"""
    if not isinstance(raw, dict):
        return [], ["顶层结构不是 JSON 对象"]
    issues: list[str] = []
    records: list[tuple[int, int, str, str, float | None, str]] = []
    for group_key, users in raw.items():
        if not _is_digits(group_key) or not isinstance(users, dict):
            issues.append(f"群记录无效（键：{group_key!r}）")
            continue
        for user_key, entries in users.items():
            if not _is_digits(user_key) or not isinstance(entries, list):
                issues.append(f"群 {group_key} 存在无效成员记录（键：{user_key!r}）")
                continue
            for index, entry in enumerate(entries):
                record = _normalize_state_entry(
                    group_key, user_key, index, entry, issues
                )
                if record is not None:
                    records.append((int(group_key), int(user_key), *record))
    return records, issues


def _normalize_state_entry(
    group_key: Any, user_key: Any, index: int, entry: Any, issues: list[str]
) -> tuple[str, str, float | None, str] | None:
    """校验单条状态记录，返回（状态键, 道具编号, 过期时间, 数据 JSON）。"""
    where = f"群 {group_key} 成员 {user_key} 的第 {index + 1} 条状态"
    if not isinstance(entry, dict):
        issues.append(f"{where}无效（应为对象）")
        return None
    key = entry.get("key")
    if not isinstance(key, str) or not key:
        issues.append(f"{where}缺少有效的状态键：{key!r}")
        return None
    item_id = entry.get("item_id")
    if not isinstance(item_id, str):
        issues.append(f"{where}的道具编号无效：{item_id!r}")
        return None
    expires_at = entry.get("expires_at")
    if isinstance(expires_at, bool) or (
        expires_at is not None and not isinstance(expires_at, (int, float))
    ):
        issues.append(f"{where}的过期时间无效：{expires_at!r}")
        return None
    data = entry.get("data", {})
    if not isinstance(data, dict):
        issues.append(f"{where}的附加数据无效（应为对象）：{data!r}")
        return None
    expires = None if expires_at is None else float(expires_at)
    return key, item_id, expires, json.dumps(data, ensure_ascii=False)


def _normalize_signs(raw: Any) -> tuple[list[tuple[Any, ...]], list[str]]:
    """校验签到 JSON：群号 -> QQ 号 -> 逐条记录（兼容旧版汇总记录）。

    旧版格式为每人一条 {name, date, count, item} 汇总对象，迁移为当天
    唯一的一条记录；同一 (群, 成员, 日期) 重复时保留最后一条。
    """
    if not isinstance(raw, dict):
        return [], ["顶层结构不是 JSON 对象"]
    issues: list[str] = []
    records: dict[tuple[int, int, str], str] = {}
    for group_key, members in raw.items():
        if not _is_digits(group_key) or not isinstance(members, dict):
            issues.append(f"群记录无效（键：{group_key!r}）")
            continue
        for user_key, member_data in members.items():
            if not _is_digits(user_key):
                issues.append(f"群 {group_key} 存在无效成员键：{user_key!r}")
                continue
            for date, item_id in _sign_entries(
                group_key, user_key, member_data, issues
            ):
                records[(int(group_key), int(user_key), date)] = item_id
    clean = [
        (group, user, date, item)
        for (group, user, date), item in sorted(records.items())
    ]
    return clean, issues


def _sign_entries(
    group_key: Any, user_key: Any, member_data: Any, issues: list[str]
) -> list[tuple[str, str]]:
    """取出单个成员的签到条目（旧版汇总对象按一条记录处理）。"""
    if isinstance(member_data, dict):
        entries: list[Any] = [member_data]
    elif isinstance(member_data, list):
        entries = member_data
    else:
        issues.append(f"群 {group_key} 成员 {user_key} 的记录格式无效")
        return []
    result: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("date"), str):
            issues.append(f"群 {group_key} 成员 {user_key} 存在无效签到记录：{entry!r}")
            continue
        date = entry["date"]
        if not _DATE_PATTERN.fullmatch(date):
            issues.append(f"群 {group_key} 成员 {user_key} 的签到日期无效：{date!r}")
            continue
        result.append((date, str(entry.get("item") or "")))
    return result


# ===== 写入与核对 =====


def _write_scores(conn: sqlite3.Connection, records: Sequence[tuple[Any, ...]]) -> None:
    """按主键覆盖写入分数。"""
    conn.executemany(
        f"INSERT INTO {SCORE_TABLE} (group_id, user_id, name, score)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT (group_id, user_id) DO UPDATE SET"
        " name = excluded.name, score = excluded.score",
        records,
    )


def _write_inventory(
    conn: sqlite3.Connection, records: Sequence[tuple[Any, ...]]
) -> None:
    """按主键覆盖写入库存。"""
    conn.executemany(
        f"INSERT INTO {INVENTORY_TABLE} (group_id, user_id, item_id, count)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT (group_id, user_id, item_id) DO UPDATE SET"
        " count = excluded.count",
        records,
    )


def _write_states(conn: sqlite3.Connection, records: Sequence[tuple[Any, ...]]) -> None:
    """按主键覆盖写入状态。"""
    conn.executemany(
        f"INSERT INTO {STATE_TABLE}"
        " (group_id, user_id, state_key, item_id, expires_at, data)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (group_id, user_id, state_key) DO UPDATE SET"
        " item_id = excluded.item_id, expires_at = excluded.expires_at,"
        " data = excluded.data",
        records,
    )


def _write_signs(conn: sqlite3.Connection, records: Sequence[tuple[Any, ...]]) -> None:
    """追加签到记录（同日重复时忽略）。"""
    conn.executemany(
        f"INSERT OR IGNORE INTO {SIGN_TABLE} (group_id, user_id, date, item_id)"
        " VALUES (?, ?, ?, ?)",
        records,
    )


_WRITERS: dict[str, Callable[[sqlite3.Connection, Sequence[tuple[Any, ...]]], None]] = {
    SCORE_TABLE: _write_scores,
    INVENTORY_TABLE: _write_inventory,
    STATE_TABLE: _write_states,
    SIGN_TABLE: _write_signs,
}


def _matched_count(
    conn: sqlite3.Connection, table: str, records: Sequence[tuple[Any, ...]]
) -> int:
    """统计写入后能在表中找到的记录数（按主键分批核对）。"""
    key_count = len(_KEY_COLUMNS[table].split(","))
    total = 0
    for start in range(0, len(records), _VERIFY_CHUNK):
        chunk = records[start : start + _VERIFY_CHUNK]
        placeholders = ",".join(
            f"({', '.join('?' for _ in range(key_count))})" for _ in chunk
        )
        params = [value for record in chunk for value in record[:key_count]]
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table}"
            f" WHERE ({_KEY_COLUMNS[table]}) IN (VALUES {placeholders})",
            params,
        ).fetchone()
        total += int(row[0])
    return total


# ===== 迁移流程 =====

_LABELS = {
    SCORE_TABLE: "决斗分数数据",
    INVENTORY_TABLE: "道具库存数据",
    STATE_TABLE: "道具状态数据",
    SIGN_TABLE: "签到记录数据",
}

# (目标表, 旧版 JSON 相对路径, 规范化函数)
_SOURCE_FILES: tuple[tuple[str, Path, Callable[..., Any]], ...] = (
    (SCORE_TABLE, Path("duel") / "scores.json", _normalize_scores),
    (INVENTORY_TABLE, Path("item") / "inventory.json", _normalize_inventory),
    (STATE_TABLE, Path("item") / "states.json", _normalize_states),
    (SIGN_TABLE, Path("sign") / "sign_ins.json", _normalize_signs),
)


def _sources(ctx: _pipeline.Context) -> list[Source]:
    """读取并校验旧版 JSON，返回迁移源列表（文件不存在时记录为空）。"""
    sources: list[Source] = []
    for table, rel, normalize in _SOURCE_FILES:
        source = Source(table=table, path=ctx.data_dir / rel)
        raw = _pipeline.load_json_if_exists(source.path, _LABELS[table])
        if raw is not None:
            source.records, source.issues = normalize(raw)
        sources.append(source)
    return sources


def _reject_invalid(sources: list[Source]) -> None:
    """存在校验问题时报错拒绝（不写入、不改名归档）。"""
    invalid = [source for source in sources if source.issues]
    if not invalid:
        return
    lines = ["以下旧版数据未通过校验，已拒绝迁移（请先修正或剔除异常记录）："]
    for source in invalid:
        lines.append(
            f"  {_LABELS[source.table]}（{source.path}）存在"
            f" {len(source.issues)} 处问题："
        )
        lines.extend(f"    - {issue}" for issue in source.issues)
    raise _pipeline.SafetyError("\n".join(lines))


def _backup_json(
    ctx: _pipeline.Context, backup_set: Path, sources: list[Source]
) -> None:
    """把待迁移的 JSON 按数据目录内的相对路径复制进备份集。"""
    for source in sources:
        if not source.path.is_file():
            continue
        target = backup_set / source.path.relative_to(ctx.data_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source.path, target)
        except OSError as exc:
            message = f"备份 {source.path} 失败，已中止（未写入数据库）：{exc}"
            raise _pipeline.SafetyError(message) from exc


def _apply(conn: sqlite3.Connection, sources: list[Source]) -> None:
    """按表写入全部记录（同一事务内完成）。"""
    for source in sources:
        if source.records:
            _WRITERS[source.table](conn, source.records)


def _verify(conn: sqlite3.Connection, sources: list[Source]) -> list[str]:
    """核对写入结果，返回问题列表（为空表示每表写入数与核对数一致）。"""
    problems: list[str] = []
    for source in sources:
        if not source.records:
            continue
        matched = _matched_count(conn, source.table, source.records)
        if matched != len(source.records):
            problems.append(
                f"{source.table}：JSON 有 {len(source.records)} 条记录，"
                f"库中仅核对到 {matched} 条"
            )
    return problems


def _archive_target(path: Path, stamp: str) -> Path:
    """返回不存在的归档路径（同一秒内重复迁移时追加序号，避免覆盖旧归档）。"""
    target = path.with_name(f"{path.name}.migrated-{stamp}")
    index = 2
    while target.exists():
        target = path.with_name(f"{path.name}.migrated-{stamp}-{index}")
        index += 1
    return target


def _archive(sources: list[Source], stamp: str) -> list[tuple[Path, Path]]:
    """把已迁移的 JSON 改名归档，返回（原路径, 归档路径）列表。"""
    moved: list[tuple[Path, Path]] = []
    for source in sources:
        if not source.path.is_file():
            continue
        target = _archive_target(source.path, stamp)
        try:
            source.path.replace(target)
        except OSError as exc:
            message = f"数据已迁移，但归档 {source.path} 失败，请手动改名：{exc}"
            raise _pipeline.SafetyError(message) from exc
        moved.append((source.path, target))
    return moved


def _print_plan(sources: list[Source], db: Path) -> None:
    """输出迁移计划。"""
    print(f"[目标] 数据库：{db}")
    print("将迁移以下数据：")
    for source in sources:
        if not source.path.is_file():
            print(f"  - {source.path}：文件不存在，跳过")
            continue
        print(f"  - {source.path} -> {source.table}：{len(source.records)} 条记录")


def run(ctx: _pipeline.Context, db: Path) -> int:
    """执行迁移并返回退出码。"""
    sources = _sources(ctx)
    _reject_invalid(sources)
    if not any(source.path.is_file() for source in sources):
        print("没有找到任何旧版 JSON 数据文件，无需迁移。")
        return 0
    state, raw = _pipeline.service_state(ctx.service_name)
    _pipeline.enforce_service_gate(ctx, state, raw)
    _print_plan(sources, db)
    if ctx.dry_run:
        print("[dry-run] 已预览迁移计划，未写入数据库、未改名归档。")
        return 0
    _pipeline.confirm(ctx)
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    sources_with_data = [source for source in sources if source.records]
    with _pipeline.data_lock(ctx.data_dir):
        backup_set = _pipeline.make_backup_set(ctx.data_dir)
        _backup_json(ctx, backup_set, sources)
        conn = _pipeline.connect_db(ctx, list(_TABLES), db=db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _apply(conn, sources_with_data)
            problems = _verify(conn, sources_with_data)
            if problems:
                # 未提交即抛错，连接关闭时整体回滚
                message = "写入后的行数核对未通过，已整体回滚：\n  " + "\n  ".join(
                    problems
                )
                raise _pipeline.SafetyError(message)
            conn.commit()
        except sqlite3.Error as exc:
            with suppress(sqlite3.Error):
                conn.rollback()
            message = f"写入数据库失败，已整体回滚：{exc}"
            raise _pipeline.SafetyError(message) from exc
        finally:
            conn.close()
        moved = _archive(sources, stamp)
    if not sources_with_data:
        print("[完成] 旧版 JSON 中没有可迁移的记录（文件为空），已直接归档。")
    else:
        print("[完成] 迁移完成：")
        for source in sources_with_data:
            print(f"  - {source.table}：{len(source.records)} 条记录")
    print(f"[备份] 迁移前的 JSON：{backup_set}")
    for old, new in moved:
        print(f"[归档] {old.name} -> {new.name}")
    print("[回滚] 把归档文件改回原名、删除数据库文件（或执行 downgrade），回退代码版本")
    if state == "unavailable":
        print("[下一步] 请按你的方式启动机器人。")
    else:
        print(f"[下一步] 请启动机器人：sudo systemctl start {ctx.service_name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.data.migrate_orm",
        description=(
            "把旧版 JSON 数据一次性迁移到 nonebot-plugin-orm 的 SQLite 数据库："
            "迁移前备份并校验 JSON，单事务写入并按主键核对，成功后把 JSON 改名归档。"
        ),
    )
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        default=None,
        help="数据目录（默认：仓库根/data）",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="数据库文件（默认：<数据目录>/nonebot_plugin_orm/db.sqlite3）",
    )
    parser.add_argument(
        "--service-name",
        metavar="NAME",
        default="gbpbot",
        help="机器人 systemd 服务名（默认 gbpbot）",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="跳过交互确认（非交互环境必须）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只预览迁移计划，不写库、不改名归档",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口，返回进程退出码。"""
    args = build_parser().parse_args(argv)
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
    db = Path(args.db) if args.db else _pipeline.db_path(ctx.data_dir)
    try:
        return run(ctx, db)
    except _pipeline.SafetyError as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2
    except _pipeline.OperationCancelledError as exc:
        print(f"[取消] {exc}")
        return 3
    except KeyboardInterrupt:
        print("\n[中断] 已退出，未完成的迁移会被回滚。")
        return 3


if __name__ == "__main__":
    sys.exit(main())
