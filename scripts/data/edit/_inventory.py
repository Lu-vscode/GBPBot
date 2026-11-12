"""道具库存数据（表 gbp_item_inventory）的读取、校验与修改命令。

数据由道具插件写入（对应 src/plugins/item/storage/models.py 的 ItemInventory）：
主键为 (群号, 成员 QQ 号, 道具编号)，数量约束 `count > 0` 由数据库强制，
数量归零即删除条目。本模块直接读写数据库，不导入插件代码；插件侧表
结构变更时必须同步维护本模块的表名与列名。
"""

import argparse
import sqlite3
from collections.abc import Callable
from typing import Any

from . import _pipeline

TABLE = "gbp_item_inventory"
LABEL = "道具库存数据"

# 道具编号长度（三位数字字符串，如 "044"）
_ITEM_ID_LENGTH = 3


def validate(conn: sqlite3.Connection) -> list[str]:
    """校验表内记录，返回问题列表（均为需要人工修正的异常数据）。"""
    issues: list[str] = []
    rows = conn.execute(
        f"SELECT group_id, user_id, item_id, count FROM {TABLE}"
        " ORDER BY group_id, user_id, item_id"
    ).fetchall()
    for row in rows:
        group_id, user_id = row["group_id"], row["user_id"]
        if not isinstance(group_id, int) or not isinstance(user_id, int):
            issues.append(f"群号/QQ 号不是整数：{group_id!r} / {user_id!r}")
            continue
        if group_id <= 0 or user_id <= 0:
            issues.append(f"群号/QQ 号无效：{group_id} / {user_id}")
            continue
        if not _is_item_id(row["item_id"]):
            issues.append(
                f"群 {group_id} 成员 {user_id} 的道具编号无效：{row['item_id']!r}"
            )
        count = row["count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            issues.append(f"群 {group_id} 成员 {user_id} 的道具数量无效：{count!r}")
    return issues


def summary(conn: sqlite3.Connection) -> str:
    """verify 命令的摘要文本。"""
    row = conn.execute(
        f"SELECT COUNT(DISTINCT group_id),"
        f" COUNT(DISTINCT group_id || '-' || user_id), COUNT(*) FROM {TABLE}"
    ).fetchone()
    return f"{int(row[0])} 个群、{int(row[1])} 名成员、{int(row[2])} 条道具记录"


def _is_item_id(value: Any) -> bool:
    """判断是否为三位数字的道具编号。"""
    return isinstance(value, str) and len(value) == _ITEM_ID_LENGTH and value.isdigit()


def _as_int(text: str, label: str) -> int:
    """把命令行给出的群号/QQ 号转为正整数（无效时报错拒绝）。"""
    value = text.strip()
    if not value.isdigit() or int(value) <= 0:
        message = f"{label}必须为正整数：{text!r}"
        raise _pipeline.SafetyError(message)
    return int(value)


def normalize_item_id(raw: str) -> str:
    """把道具编号规范化为三位字符串（如 "44" -> "044"）。"""
    text = raw.strip()
    if not text.isdigit() or int(text) <= 0:
        message = f"道具编号必须为正整数（如 44 或 044）：{raw!r}"
        raise _pipeline.SafetyError(message)
    return f"{int(text):03d}"


def _check_count(count: int) -> None:
    """校验数量参数为正整数。"""
    if count < 1:
        message = f"数量必须为正整数：{count}"
        raise _pipeline.SafetyError(message)


def _connect(ctx: _pipeline.Context) -> sqlite3.Connection:
    """打开数据库（只确认库存表存在）。"""
    return _pipeline.connect_db(ctx, [TABLE])


def _count_of(
    ctx: _pipeline.Context, group_id: int, user_id: int, item_id: str
) -> int | None:
    """读取成员某道具的数量（没有该条目时返回 None）。"""
    conn = _connect(ctx)
    try:
        row = conn.execute(
            f"SELECT count FROM {TABLE}"
            " WHERE group_id = ? AND user_id = ? AND item_id = ?",
            (group_id, user_id, item_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else int(row["count"])


def _items_text(items: list[sqlite3.Row]) -> str:
    """成员道具的紧凑展示文本。"""
    return "  ".join(f"{row['item_id']}x{int(row['count'])}" for row in items)


def _execute(
    ctx: _pipeline.Context,
    changes: list[str],
    apply: Callable[[sqlite3.Connection], None],
) -> None:
    """把库存表的修改交给公共管线（门禁、备份、单事务）。"""
    _pipeline.execute_change(
        ctx,
        _pipeline.DbChangePlan(label=LABEL, changes=changes, apply=apply),
        tables=[TABLE],
    )


def run_get(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """查看某成员的道具。"""
    group_id = _as_int(args.group, "群号")
    user_id = _as_int(args.user, "QQ 号")
    conn = _connect(ctx)
    try:
        rows = conn.execute(
            f"SELECT item_id, count FROM {TABLE}"
            " WHERE group_id = ? AND user_id = ? ORDER BY item_id",
            (group_id, user_id),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print(f"群 {args.group} 成员 {args.user} 没有道具记录。")
        return 0
    total = sum(int(row["count"]) for row in rows)
    print(f"群 {args.group} 成员 {args.user} 的道具（共 {total} 件）：")
    for row in rows:
        print(f"  {row['item_id']} x{int(row['count'])}")
    return 0


def run_list(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """列出某群全部成员的道具。"""
    group_id = _as_int(args.group, "群号")
    conn = _connect(ctx)
    try:
        rows = conn.execute(
            f"SELECT user_id, item_id, count FROM {TABLE}"
            " WHERE group_id = ? ORDER BY user_id, item_id",
            (group_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print(f"群 {args.group} 没有道具记录。")
        return 0
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(int(row["user_id"]), []).append(row)
    print(f"群 {args.group} 共 {len(grouped)} 名成员有道具记录：")
    for user_id in sorted(grouped):
        print(f"  {user_id}：{_items_text(grouped[user_id])}")
    return 0


def run_give(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """给成员增加道具数量（没有该道具时新建条目）。"""
    group_id = _as_int(args.group, "群号")
    user_id = _as_int(args.user, "QQ 号")
    item_id = normalize_item_id(args.item)
    _check_count(args.count)
    old = _count_of(ctx, group_id, user_id, item_id) or 0
    changes = [
        f"成员 {args.user} 道具 {item_id}：{old} -> {old + args.count}（+{args.count}）"
    ]

    def apply(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"INSERT INTO {TABLE} (group_id, user_id, item_id, count)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (group_id, user_id, item_id) DO UPDATE SET"
            " count = count + excluded.count",
            (group_id, user_id, item_id, args.count),
        )

    _execute(ctx, changes, apply)
    return 0


def run_take(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """从成员处扣除道具数量（扣到 0 时移除条目）。"""
    group_id = _as_int(args.group, "群号")
    user_id = _as_int(args.user, "QQ 号")
    item_id = normalize_item_id(args.item)
    _check_count(args.count)
    old = _count_of(ctx, group_id, user_id, item_id)
    if old is None:
        message = f"群 {args.group} 成员 {args.user} 没有道具 {item_id}。"
        raise _pipeline.SafetyError(message)
    if args.count > old:
        message = (
            f"成员 {args.user} 的道具 {item_id} 只有 {old} 件，"
            f"不足以扣除 {args.count} 件。"
        )
        raise _pipeline.SafetyError(message)
    remaining = old - args.count
    if remaining == 0:
        changes = [f"成员 {args.user} 道具 {item_id}：{old} -> 0（已移除条目）"]
    else:
        changes = [
            f"成员 {args.user} 道具 {item_id}：{old} -> {remaining}（-{args.count}）"
        ]

    def apply(conn: sqlite3.Connection) -> None:
        where = " WHERE group_id = ? AND user_id = ? AND item_id = ? AND count = ?"
        params = (group_id, user_id, item_id, old)
        if remaining == 0:
            cursor = conn.execute(f"DELETE FROM {TABLE}{where}", params)
        else:
            cursor = conn.execute(
                f"UPDATE {TABLE} SET count = ?{where}", (remaining, *params)
            )
        if cursor.rowcount != 1:
            message = "成员道具数量在本次操作期间发生变化，已回滚，请重试。"
            raise _pipeline.SafetyError(message)

    _execute(ctx, changes, apply)
    return 0


def run_set(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """直接设定道具数量（设为 0 请用 del 删除条目）。"""
    group_id = _as_int(args.group, "群号")
    user_id = _as_int(args.user, "QQ 号")
    item_id = normalize_item_id(args.item)
    _check_count(args.count)
    old = _count_of(ctx, group_id, user_id, item_id) or 0
    if old == args.count:
        print("没有需要修改的内容（数据与目标值一致）。")
        return 0
    changes = [f"成员 {args.user} 道具 {item_id}：{old} -> {args.count}"]

    def apply(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"INSERT INTO {TABLE} (group_id, user_id, item_id, count)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (group_id, user_id, item_id) DO UPDATE SET"
            " count = excluded.count",
            (group_id, user_id, item_id, args.count),
        )

    _execute(ctx, changes, apply)
    return 0


def run_del(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """删除成员的道具条目。"""
    group_id = _as_int(args.group, "群号")
    user_id = _as_int(args.user, "QQ 号")
    item_id = normalize_item_id(args.item)
    old = _count_of(ctx, group_id, user_id, item_id)
    if old is None:
        message = f"群 {args.group} 成员 {args.user} 没有道具 {item_id}。"
        raise _pipeline.SafetyError(message)
    changes = [f"删除成员 {args.user} 的道具 {item_id}（原 x{old}）"]

    def apply(conn: sqlite3.Connection) -> None:
        cursor = conn.execute(
            f"DELETE FROM {TABLE}"
            " WHERE group_id = ? AND user_id = ? AND item_id = ? AND count = ?",
            (group_id, user_id, item_id, old),
        )
        if cursor.rowcount != 1:
            message = "成员道具数量在本次操作期间发生变化，已回滚，请重试。"
            raise _pipeline.SafetyError(message)

    _execute(ctx, changes, apply)
    return 0


def _add_item_args(parser: argparse.ArgumentParser, *, with_count: bool) -> None:
    """为子命令添加 -g/-u/-i（及可选 -n）参数。"""
    _pipeline.add_group_user_args(parser)
    parser.add_argument(
        "-i", "--item", required=True, metavar="编号", help='道具编号（如 44 或 "044"）'
    )
    if with_count:
        parser.add_argument(
            "-n", "--count", type=int, default=1, metavar="数量", help="数量（默认 1）"
        )


def register(subparsers: Any) -> None:
    """注册 inventory 子命令。"""
    parser = subparsers.add_parser("inventory", help=f"道具库存（{TABLE}）")
    _pipeline.add_global_options(parser, suppressed=True)
    verbs = parser.add_subparsers(dest="verb", required=True, metavar="<子命令>")

    get_parser = verbs.add_parser("get", help="查看某成员的道具")
    _pipeline.add_global_options(get_parser, suppressed=True)
    _pipeline.add_group_user_args(get_parser)
    get_parser.set_defaults(handler=run_get)

    list_parser = verbs.add_parser("list", help="列出某群全部成员的道具")
    _pipeline.add_global_options(list_parser, suppressed=True)
    list_parser.add_argument(
        "-g", "--group", required=True, metavar="群号", help="群号"
    )
    list_parser.set_defaults(handler=run_list)

    give_parser = verbs.add_parser("give", help="给成员增加道具")
    _pipeline.add_global_options(give_parser, suppressed=True)
    _add_item_args(give_parser, with_count=True)
    give_parser.set_defaults(handler=run_give)

    take_parser = verbs.add_parser("take", help="从成员处扣除道具（扣到 0 移除条目）")
    _pipeline.add_global_options(take_parser, suppressed=True)
    _add_item_args(take_parser, with_count=True)
    take_parser.set_defaults(handler=run_take)

    set_parser = verbs.add_parser("set", help="直接设定道具数量")
    _pipeline.add_global_options(set_parser, suppressed=True)
    _add_item_args(set_parser, with_count=True)
    set_parser.set_defaults(handler=run_set)

    del_parser = verbs.add_parser("del", help="删除成员的道具条目")
    _pipeline.add_global_options(del_parser, suppressed=True)
    _add_item_args(del_parser, with_count=False)
    del_parser.set_defaults(handler=run_del)
