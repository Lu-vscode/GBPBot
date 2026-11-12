"""道具状态数据（表 gbp_item_state）的读取、校验与修改命令。

数据由道具插件写入（对应 src/plugins/item/storage/models.py 的 ItemStateRow）：
主键为 (群号, 成员 QQ 号, 状态键)，同键状态直接覆盖；`expires_at` 为
NULL 表示永久，`data` 为道具模块自定义的 JSON 附加数据。本模块直接
读写数据库，不导入插件代码；插件侧表结构变更时必须同步维护本模块的
表名与列名。
"""

import argparse
import json
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from . import _pipeline

TABLE = "gbp_item_state"
LABEL = "道具状态数据"

# 状态附加数据在展示时的最大长度（超出部分截断）
_DATA_PREVIEW_LIMIT = 120


def validate(conn: sqlite3.Connection) -> list[str]:
    """校验表内记录，返回问题列表（均为需要人工修正的异常数据）。"""
    issues: list[str] = []
    rows = conn.execute(
        f"SELECT group_id, user_id, state_key, item_id, expires_at, data FROM {TABLE}"
        " ORDER BY group_id, user_id, state_key"
    ).fetchall()
    for row in rows:
        group_id, user_id = row["group_id"], row["user_id"]
        if not isinstance(group_id, int) or not isinstance(user_id, int):
            issues.append(f"群号/QQ 号不是整数：{group_id!r} / {user_id!r}")
            continue
        if group_id <= 0 or user_id <= 0:
            issues.append(f"群号/QQ 号无效：{group_id} / {user_id}")
            continue
        key = row["state_key"]
        if not isinstance(key, str) or not key:
            issues.append(f"群 {group_id} 成员 {user_id} 的状态键无效：{key!r}")
            continue
        if not isinstance(row["item_id"], str):
            issues.append(f"状态 {key} 的道具编号不是字符串：{row['item_id']!r}")
        expires_at = row["expires_at"]
        if expires_at is not None and (
            isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
        ):
            issues.append(f"状态 {key} 的过期时间无效：{expires_at!r}")
        if _load_data(row["data"]) is None:
            issues.append(f"状态 {key} 的附加数据不是合法的 JSON 对象：{row['data']!r}")
    return issues


def summary(conn: sqlite3.Connection) -> str:
    """verify 命令的摘要文本。"""
    row = conn.execute(
        f"SELECT COUNT(DISTINCT group_id),"
        f" COUNT(DISTINCT group_id || '-' || user_id), COUNT(*) FROM {TABLE}"
    ).fetchone()
    return f"{int(row[0])} 个群、{int(row[1])} 名成员、{int(row[2])} 条状态"


def _load_data(raw: Any) -> dict[str, Any] | None:
    """解析状态的附加数据（JSON 对象），不是合法对象时返回 None。"""
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _as_int(text: str, label: str) -> int:
    """把命令行给出的群号/QQ 号转为正整数（无效时报错拒绝）。"""
    value = text.strip()
    if not value.isdigit() or int(value) <= 0:
        message = f"{label}必须为正整数：{text!r}"
        raise _pipeline.SafetyError(message)
    return int(value)


def _connect(ctx: _pipeline.Context) -> sqlite3.Connection:
    """打开数据库（只确认状态表存在）。"""
    return _pipeline.connect_db(ctx, [TABLE])


def _format_expiry(expires_at: float | None) -> str:
    """把过期时间格式化为本地时间文本（None 表示永久）。"""
    if expires_at is None:
        return "永久"
    moment = datetime.fromtimestamp(expires_at, tz=UTC).astimezone()
    label = moment.strftime("%Y-%m-%d %H:%M:%S")
    if expires_at < time.time():
        return f"{label}（已过期）"
    return label


def _entry_text(row: sqlite3.Row) -> str:
    """状态条目的展示文本。"""
    extra = json.dumps(
        _load_data(row["data"]) or {}, ensure_ascii=False, sort_keys=True
    )
    if len(extra) > _DATA_PREVIEW_LIMIT:
        extra = extra[: _DATA_PREVIEW_LIMIT - 3] + "..."
    expires_at = row["expires_at"]
    expiry = None if expires_at is None else float(expires_at)
    return (
        f"{row['state_key']}（道具 {row['item_id']}，"
        f"到期 {_format_expiry(expiry)}，数据 {extra}）"
    )


def _execute(
    ctx: _pipeline.Context,
    changes: list[str],
    apply: Callable[[sqlite3.Connection], None],
) -> None:
    """把状态表的修改交给公共管线（门禁、备份、单事务）。"""
    _pipeline.execute_change(
        ctx,
        _pipeline.DbChangePlan(label=LABEL, changes=changes, apply=apply),
        tables=[TABLE],
    )


def run_list(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """列出状态记录（可按群号、QQ 号过滤）。"""
    where: list[str] = []
    params: list[int] = []
    if args.group:
        where.append("group_id = ?")
        params.append(_as_int(args.group, "群号"))
    if args.user:
        where.append("user_id = ?")
        params.append(_as_int(args.user, "QQ 号"))
    statement = f"SELECT * FROM {TABLE}"
    if where:
        statement += " WHERE " + " AND ".join(where)
    statement += " ORDER BY group_id, user_id, state_key"
    conn = _connect(ctx)
    try:
        rows = conn.execute(statement, params).fetchall()
    finally:
        conn.close()
    if not rows:
        print("没有状态记录。")
        return 0
    print(f"共 {len(rows)} 条状态记录：")
    for row in rows:
        print(f"  群 {row['group_id']} 成员 {row['user_id']}：{_entry_text(row)}")
    return 0


def run_clear(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """清除某成员的全部状态。"""
    group_id = _as_int(args.group, "群号")
    user_id = _as_int(args.user, "QQ 号")
    conn = _connect(ctx)
    try:
        count = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {TABLE} WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    if not count:
        message = f"群 {args.group} 成员 {args.user} 没有状态记录。"
        raise _pipeline.SafetyError(message)
    changes = [f"清除成员 {args.user} 的全部状态（{count} 条）"]

    def apply(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"DELETE FROM {TABLE} WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )

    _execute(ctx, changes, apply)
    return 0


def run_purge(ctx: _pipeline.Context, _args: argparse.Namespace) -> int:
    """清理全部已过期的状态（expires_at 早于当前时间）。"""
    now = time.time()
    conn = _connect(ctx)
    try:
        rows = conn.execute(
            f"SELECT group_id, user_id, COUNT(*) AS expired FROM {TABLE}"
            " WHERE expires_at IS NOT NULL AND expires_at < ?"
            " GROUP BY group_id, user_id ORDER BY group_id, user_id",
            (now,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("没有已过期的状态记录。")
        return 0
    total = sum(int(row["expired"]) for row in rows)
    changes = [f"清理已过期状态：共 {total} 条（涉及 {len(rows)} 名成员）"]
    changes.extend(
        f"  群 {row['group_id']} 成员 {row['user_id']}：{int(row['expired'])} 条"
        for row in rows
    )

    def apply(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"DELETE FROM {TABLE} WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )

    _execute(ctx, changes, apply)
    return 0


def register(subparsers: Any) -> None:
    """注册 states 子命令。"""
    parser = subparsers.add_parser("states", help=f"道具状态（{TABLE}）")
    _pipeline.add_global_options(parser, suppressed=True)
    verbs = parser.add_subparsers(dest="verb", required=True, metavar="<子命令>")

    list_parser = verbs.add_parser("list", help="列出状态记录")
    _pipeline.add_global_options(list_parser, suppressed=True)
    list_parser.add_argument("-g", "--group", metavar="群号", help="只显示该群")
    list_parser.add_argument("-u", "--user", metavar="QQ号", help="只显示该成员")
    list_parser.set_defaults(handler=run_list)

    clear_parser = verbs.add_parser("clear", help="清除某成员的全部状态")
    _pipeline.add_global_options(clear_parser, suppressed=True)
    _pipeline.add_group_user_args(clear_parser)
    clear_parser.set_defaults(handler=run_clear)

    purge_parser = verbs.add_parser("purge", help="清理全部已过期的状态")
    _pipeline.add_global_options(purge_parser, suppressed=True)
    purge_parser.set_defaults(handler=run_purge)
