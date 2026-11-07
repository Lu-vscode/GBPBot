"""签到记录（表 gbp_sign_record）的只读查询命令。

记录由签到插件写入（对应 src/plugins/sign.py 的 SignRecord）：主键为
自增 id，(群号, 成员 QQ 号, 签到日期) 唯一。本模块不提供改写命令：签到
记录只由插件的签到流程追加（同日重复签到由唯一约束拒绝），补录或删除
应通过插件流程或直接连库完成，避免破坏"当天唯一"的约束语义。
"""

import argparse
import re
import sqlite3
from typing import Any

from . import _pipeline

TABLE = "gbp_sign_record"
LABEL = "签到记录数据"

# 签到日期格式（本地时区 YYYY-MM-DD）
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate(conn: sqlite3.Connection) -> list[str]:
    """校验表内记录，返回问题列表（均为需要人工修正的异常数据）。"""
    issues: list[str] = []
    rows = conn.execute(
        f"SELECT group_id, user_id, date, item_id FROM {TABLE}"
        " ORDER BY group_id, user_id, date"
    ).fetchall()
    for row in rows:
        group_id, user_id = row["group_id"], row["user_id"]
        if not isinstance(group_id, int) or not isinstance(user_id, int):
            issues.append(f"群号/QQ 号不是整数：{group_id!r} / {user_id!r}")
            continue
        if group_id <= 0 or user_id <= 0:
            issues.append(f"群号/QQ 号无效：{group_id} / {user_id}")
            continue
        date = row["date"]
        if not isinstance(date, str) or not _DATE_PATTERN.fullmatch(date):
            issues.append(f"群 {group_id} 成员 {user_id} 的签到日期无效：{date!r}")
        if not isinstance(row["item_id"], str):
            issues.append(
                f"群 {group_id} 成员 {user_id} 的道具编号无效：{row['item_id']!r}"
            )
    return issues


def summary(conn: sqlite3.Connection) -> str:
    """verify 命令的摘要文本。"""
    row = conn.execute(
        f"SELECT COUNT(DISTINCT group_id),"
        f" COUNT(DISTINCT group_id || '-' || user_id), COUNT(*) FROM {TABLE}"
    ).fetchone()
    return f"{int(row[0])} 个群、{int(row[1])} 名成员、{int(row[2])} 条签到记录"


def _as_int(text: str, label: str) -> int:
    """把命令行给出的群号转为正整数（无效时报错拒绝）。"""
    value = text.strip()
    if not value.isdigit() or int(value) <= 0:
        message = f"{label}必须为正整数：{text!r}"
        raise _pipeline.SafetyError(message)
    return int(value)


def run_list(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """列出某群成员的签到天数与最近签到日期。"""
    group_id = _as_int(args.group, "群号")
    conn = _pipeline.connect_db(ctx, [TABLE])
    try:
        rows = conn.execute(
            f"SELECT user_id, COUNT(*) AS days, MAX(date) AS last FROM {TABLE}"
            " WHERE group_id = ? GROUP BY user_id ORDER BY user_id",
            (group_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print(f"群 {args.group} 没有签到记录。")
        return 0
    print(f"群 {args.group} 共 {len(rows)} 名成员有签到记录：")
    for row in rows:
        print(f"  {row['user_id']}：签到 {int(row['days'])} 天，最近 {row['last']}")
    return 0


def register(subparsers: Any) -> None:
    """注册 signs 子命令。"""
    parser = subparsers.add_parser("signs", help=f"签到记录（{TABLE}，只读）")
    _pipeline.add_global_options(parser, suppressed=True)
    verbs = parser.add_subparsers(dest="verb", required=True, metavar="<子命令>")

    list_parser = verbs.add_parser("list", help="列出某群成员的签到天数")
    _pipeline.add_global_options(list_parser, suppressed=True)
    list_parser.add_argument(
        "-g", "--group", required=True, metavar="群号", help="群号"
    )
    list_parser.set_defaults(handler=run_list)
