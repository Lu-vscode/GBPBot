"""决斗分数数据（表 gbp_duel_score）的读取、校验与修改命令。

数据由决斗插件写入（对应 src/plugins/duel/_models.py 的 DuelScore）：
主键为 (群号, 成员 QQ 号)，昵称仅用于排行榜展示。本模块直接读写
数据库，不导入插件代码；插件侧表结构变更时必须同步维护本模块的
表名与列名。
"""

import argparse
import sqlite3
from collections.abc import Callable
from typing import Any

from . import _pipeline

TABLE = "gbp_duel_score"
LABEL = "决斗分数数据"


def validate(conn: sqlite3.Connection) -> list[str]:
    """校验表内记录，返回问题列表（均为需要人工修正的异常数据）。"""
    issues: list[str] = []
    rows = conn.execute(
        f"SELECT group_id, user_id, name, score FROM {TABLE}"
    ).fetchall()
    for row in rows:
        group_id, user_id = row["group_id"], row["user_id"]
        if not isinstance(group_id, int) or not isinstance(user_id, int):
            issues.append(f"群号/QQ 号不是整数：{group_id!r} / {user_id!r}")
            continue
        if group_id <= 0 or user_id <= 0:
            issues.append(f"群号/QQ 号无效：{group_id} / {user_id}")
            continue
        score = row["score"]
        if isinstance(score, bool) or not isinstance(score, int):
            issues.append(f"群 {group_id} 成员 {user_id} 的分数不是整数：{score!r}")
        if not isinstance(row["name"], str):
            issues.append(
                f"群 {group_id} 成员 {user_id} 的昵称不是字符串：{row['name']!r}"
            )
    return issues


def summary(conn: sqlite3.Connection) -> str:
    """verify 命令的摘要文本。"""
    row = conn.execute(
        f"SELECT COUNT(DISTINCT group_id), COUNT(*) FROM {TABLE}"
    ).fetchone()
    return f"{int(row[0])} 个群、{int(row[1])} 名成员"


def _as_int(text: str, label: str) -> int:
    """把命令行给出的群号/QQ 号转为正整数（无效时报错拒绝）。"""
    value = text.strip()
    if not value.isdigit() or int(value) <= 0:
        message = f"{label}必须为正整数：{text!r}"
        raise _pipeline.SafetyError(message)
    return int(value)


def _connect(ctx: _pipeline.Context) -> sqlite3.Connection:
    """打开数据库（只确认分数表存在）。"""
    return _pipeline.connect_db(ctx, [TABLE])


def _fetch(ctx: _pipeline.Context, group_id: int, user_id: int) -> sqlite3.Row | None:
    """读取某成员的分数记录（不存在时返回 None）。"""
    conn = _connect(ctx)
    try:
        return conn.execute(
            f"SELECT name, score FROM {TABLE} WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        ).fetchone()
    finally:
        conn.close()


def _record_text(row: sqlite3.Row) -> str:
    """成员记录的展示文本。"""
    name = str(row["name"] or "")
    return f"分数 {int(row['score'])}，昵称 {name or '（空）'}"


def _stored_name(current: str, requested: str | None) -> str:
    """本次写入的昵称：未指定 --name 时保留原值（新成员为空串）。"""
    return current if requested is None else requested


def _execute(
    ctx: _pipeline.Context,
    changes: list[str],
    apply: Callable[[sqlite3.Connection], None],
) -> None:
    """把分数表的修改交给公共管线（门禁、备份、单事务）。"""
    _pipeline.execute_change(
        ctx,
        _pipeline.DbChangePlan(label=LABEL, changes=changes, apply=apply),
        tables=[TABLE],
    )


def run_get(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """查看某成员分数。"""
    group_id = _as_int(args.group, "群号")
    user_id = _as_int(args.user, "QQ 号")
    row = _fetch(ctx, group_id, user_id)
    if row is None:
        print(f"群 {args.group} 成员 {args.user} 没有分数记录（视为 0 分）。")
        return 0
    print(f"群 {args.group} 成员 {args.user}：{_record_text(row)}")
    return 0


def _list_group(ctx: _pipeline.Context, group_text: str) -> int:
    """按分数降序列出某群成员。"""
    group_id = _as_int(group_text, "群号")
    conn = _connect(ctx)
    try:
        rows = conn.execute(
            f"SELECT user_id, name, score FROM {TABLE}"
            " WHERE group_id = ? ORDER BY score DESC, user_id ASC",
            (group_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print(f"群 {group_text} 没有分数记录。")
        return 0
    print(f"群 {group_text} 共 {len(rows)} 名成员记录（按分数降序）：")
    for rank, row in enumerate(rows, start=1):
        name = str(row["name"] or "") or "（空）"
        print(f"  {rank:>3}. {row['user_id']}  {int(row['score']):>7}  {name}")
    return 0


def run_list(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """列出分数：-g 指定群时按分数降序，否则列出全部群及人数。"""
    if args.group:
        return _list_group(ctx, args.group)
    conn = _connect(ctx)
    try:
        rows = conn.execute(
            f"SELECT group_id, COUNT(*) AS members FROM {TABLE}"
            " GROUP BY group_id ORDER BY group_id"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("没有分数记录。")
        return 0
    print(f"共 {len(rows)} 个群：")
    for row in rows:
        print(f"  {row['group_id']}：{int(row['members'])} 名成员")
    return 0


def run_set(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """直接设定分数（成员不存在时新建）。"""
    group_id = _as_int(args.group, "群号")
    user_id = _as_int(args.user, "QQ 号")
    row = _fetch(ctx, group_id, user_id)
    if row is None:
        name = args.name or ""
        changes = [f"新增成员 {args.user}：分数 {args.score}，昵称 {name or '（空）'}"]
        stored_name = name
    else:
        changes: list[str] = []
        old_score = int(row["score"])
        old_name = str(row["name"] or "")
        if old_score != args.score:
            changes.append(f"成员 {args.user} 分数：{old_score} -> {args.score}")
        if args.name is not None and old_name != args.name:
            changes.append(
                f"成员 {args.user} 昵称：{old_name or '（空）'}"
                f" -> {args.name or '（空）'}"
            )
        if not changes:
            print("没有需要修改的内容（数据与目标值一致）。")
            return 0
        stored_name = _stored_name(old_name, args.name)

    def apply(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"INSERT INTO {TABLE} (group_id, user_id, name, score)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (group_id, user_id) DO UPDATE SET"
            " score = excluded.score, name = excluded.name",
            (group_id, user_id, stored_name, args.score),
        )

    _execute(ctx, changes, apply)
    return 0


def run_add(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """增减分数（成员不存在时以增减量为初始分数新建）。"""
    group_id = _as_int(args.group, "群号")
    user_id = _as_int(args.user, "QQ 号")
    row = _fetch(ctx, group_id, user_id)
    if row is None:
        name = args.name or ""
        changes = [f"新增成员 {args.user}：分数 {args.delta}，昵称 {name or '（空）'}"]
        stored_name = name
    else:
        changes = []
        old_score = int(row["score"])
        old_name = str(row["name"] or "")
        new_score = old_score + args.delta
        if new_score != old_score:
            changes.append(
                f"成员 {args.user} 分数：{old_score} -> {new_score}（{args.delta:+d}）"
            )
        if args.name is not None and old_name != args.name:
            changes.append(
                f"成员 {args.user} 昵称：{old_name or '（空）'}"
                f" -> {args.name or '（空）'}"
            )
        if not changes:
            print("没有需要修改的内容（数据与目标值一致）。")
            return 0
        stored_name = _stored_name(old_name, args.name)

    def apply(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"INSERT INTO {TABLE} (group_id, user_id, name, score)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (group_id, user_id) DO UPDATE SET"
            " score = score + excluded.score, name = excluded.name",
            (group_id, user_id, stored_name, args.delta),
        )

    _execute(ctx, changes, apply)
    return 0


def run_del(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """删除某成员的分数记录。"""
    group_id = _as_int(args.group, "群号")
    user_id = _as_int(args.user, "QQ 号")
    row = _fetch(ctx, group_id, user_id)
    if row is None:
        message = f"群 {args.group} 中找不到成员 {args.user} 的分数记录。"
        raise _pipeline.SafetyError(message)
    changes = [f"删除成员 {args.user} 的分数记录（原{_record_text(row)}）"]

    def apply(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"DELETE FROM {TABLE} WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )

    _execute(ctx, changes, apply)
    return 0


def register(subparsers: Any) -> None:
    """注册 scores 子命令。"""
    parser = subparsers.add_parser("scores", help=f"决斗分数（{TABLE}）")
    _pipeline.add_global_options(parser, suppressed=True)
    verbs = parser.add_subparsers(dest="verb", required=True, metavar="<子命令>")

    get_parser = verbs.add_parser("get", help="查看某成员分数")
    _pipeline.add_global_options(get_parser, suppressed=True)
    _pipeline.add_group_user_args(get_parser)
    get_parser.set_defaults(handler=run_get)

    list_parser = verbs.add_parser(
        "list", help="列出分数（-g 指定群时按分数降序，否则列出全部群）"
    )
    _pipeline.add_global_options(list_parser, suppressed=True)
    list_parser.add_argument(
        "-g", "--group", metavar="群号", help="群号（缺省列出全部群及人数）"
    )
    list_parser.set_defaults(handler=run_list)

    set_parser = verbs.add_parser("set", help="直接设定分数")
    _pipeline.add_global_options(set_parser, suppressed=True)
    _pipeline.add_group_user_args(set_parser)
    set_parser.add_argument(
        "--score", type=int, required=True, metavar="分值", help="设定后的分数"
    )
    set_parser.add_argument(
        "--name", metavar="群昵称", help="同时更新昵称（新成员缺省为空）"
    )
    set_parser.set_defaults(handler=run_set)

    add_parser = verbs.add_parser("add", help="增减分数（可为负）")
    _pipeline.add_global_options(add_parser, suppressed=True)
    _pipeline.add_group_user_args(add_parser)
    add_parser.add_argument(
        "--delta",
        type=int,
        required=True,
        metavar="增减量",
        help="分数增减量（可为负）",
    )
    add_parser.add_argument(
        "--name", metavar="群昵称", help="同时更新昵称（新成员缺省为空）"
    )
    add_parser.set_defaults(handler=run_add)

    del_parser = verbs.add_parser("del", help="删除某成员的分数记录")
    _pipeline.add_global_options(del_parser, suppressed=True)
    _pipeline.add_group_user_args(del_parser)
    del_parser.set_defaults(handler=run_del)
