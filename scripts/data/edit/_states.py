"""道具状态数据（data/item/states.json）的读取、校验与修改命令。

数据格式与 src/plugins/item/_storage.py 的加载/写盘规则保持一致：
顶层为 群号字符串 -> QQ 号字符串 -> [状态条目, ...]，条目字段为
key（非空字符串）、item_id（字符串）、expires_at（Unix 时间戳，
null 表示永久）、data（对象）。插件加载时会丢弃的条目在本模块列为
问题并拒绝修改；空成员、空群等无数据价值的空容器被静默清理。
插件侧数据格式变更时必须同步维护本模块。
"""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import _pipeline

FILE_REL = Path("item/states.json")
LABEL = "道具状态数据"

# 状态附加数据在展示时的最大长度（超出部分截断）
_DATA_PREVIEW_LIMIT = 120

_Entry = dict[str, Any]
_States = dict[str, dict[str, list[_Entry]]]


def _as_state(raw: Any) -> _Entry | None:
    """校验单条状态记录（与插件规则一致），无效时返回 None。"""
    if not isinstance(raw, dict):
        return None
    key = raw.get("key")
    if not isinstance(key, str) or not key:
        return None
    item_id = raw.get("item_id")
    if not isinstance(item_id, str):
        return None
    expires_at = raw.get("expires_at")
    if isinstance(expires_at, bool) or (
        expires_at is not None and not isinstance(expires_at, (int, float))
    ):
        return None
    data = raw.get("data", {})
    if not isinstance(data, dict):
        return None
    return {
        "key": key,
        "item_id": item_id,
        "expires_at": None if expires_at is None else float(expires_at),
        "data": dict(data),
    }


def normalize(raw: Any) -> tuple[_States, list[str]]:
    """校验并规范化状态数据，返回（干净数据, 问题列表）。"""
    if not isinstance(raw, dict):
        return {}, ["顶层结构不是 JSON 对象"]
    issues: list[str] = []
    clean: _States = {}
    for group_key, users in raw.items():
        if not _pipeline.is_digits(group_key) or not isinstance(users, dict):
            issues.append(f"群记录无效（键：{group_key!r}）")
            continue
        group: dict[str, list[_Entry]] = {}
        for user_key, entries in users.items():
            if not _pipeline.is_digits(user_key) or not isinstance(entries, list):
                issues.append(f"群 {group_key} 存在无效成员记录（键：{user_key!r}）")
                continue
            user_states: list[_Entry] = []
            for index, entry in enumerate(entries):
                state = _as_state(entry)
                if state is None:
                    issues.append(
                        f"群 {group_key} 成员 {user_key} 的第 {index + 1} 条状态无效"
                    )
                    continue
                user_states.append(state)
            if user_states:
                group[user_key] = user_states
        if group:
            clean[group_key] = group
    return clean, issues


def summary(data: _States) -> str:
    """verify 命令的摘要文本。"""
    members = sum(len(group) for group in data.values())
    entries = sum(
        len(user_states) for group in data.values() for user_states in group.values()
    )
    return f"{len(data)} 个群、{members} 名成员、{entries} 条状态"


def _format_expiry(expires_at: float | None) -> str:
    """把过期时间格式化为本地时间文本（None 表示永久）。"""
    if expires_at is None:
        return "永久"
    moment = datetime.fromtimestamp(expires_at, tz=UTC).astimezone()
    label = moment.strftime("%Y-%m-%d %H:%M:%S")
    if expires_at < time.time():
        return f"{label}（已过期）"
    return label


def _entry_text(entry: _Entry) -> str:
    """状态条目的展示文本。"""
    extra = json.dumps(entry["data"], ensure_ascii=False)
    if len(extra) > _DATA_PREVIEW_LIMIT:
        extra = extra[: _DATA_PREVIEW_LIMIT - 3] + "..."
    return (
        f"{entry['key']}（道具 {entry['item_id']}，"
        f"到期 {_format_expiry(entry['expires_at'])}，数据 {extra}）"
    )


def _read(ctx: _pipeline.Context) -> tuple[_States, int | None]:
    """读取并严格校验状态数据，返回（数据, 原文件修改时间）。"""
    path = ctx.data_dir / FILE_REL
    mtime = _pipeline.current_mtime_ns(path)
    raw = _pipeline.load_json_if_exists(path, LABEL)
    data, issues = normalize(raw if raw is not None else {})
    if issues:
        raise _pipeline.issues_error(LABEL, issues)
    return data, mtime


def run_list(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """列出状态记录（可按群号、QQ 号过滤）。"""
    data, _ = _read(ctx)
    rows: list[tuple[str, str, _Entry]] = []
    for group_key in sorted(data, key=int):
        if args.group and group_key != args.group:
            continue
        for user_key in sorted(data[group_key], key=int):
            if args.user and user_key != args.user:
                continue
            rows.extend(
                (group_key, user_key, entry) for entry in data[group_key][user_key]
            )
    if not rows:
        print("没有状态记录。")
        return 0
    print(f"共 {len(rows)} 条状态记录：")
    for group_key, user_key, entry in rows:
        print(f"  群 {group_key} 成员 {user_key}：{_entry_text(entry)}")
    return 0


def run_clear(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """清除某成员的全部状态。"""
    data, mtime = _read(ctx)
    group = data.get(args.group)
    states = group.get(args.user) if group else None
    if not states:
        message = f"群 {args.group} 成员 {args.user} 没有状态记录。"
        raise _pipeline.SafetyError(message)
    count = len(states)
    del group[args.user]
    changes = [f"清除成员 {args.user} 的全部状态（{count} 条）"]
    _pipeline.execute_change(
        ctx,
        _pipeline.ChangePlan(
            path=ctx.data_dir / FILE_REL,
            label=LABEL,
            changes=changes,
            new_data=data,
            validator=normalize,
            expected_mtime_ns=mtime,
        ),
    )
    return 0


def run_purge(ctx: _pipeline.Context, _args: argparse.Namespace) -> int:
    """清理全部已过期的状态（expires_at 早于当前时间）。"""
    data, mtime = _read(ctx)
    now = time.time()
    removed: list[str] = []
    total_removed = 0
    for group_key in list(data):
        group = data[group_key]
        for user_key in list(group):
            kept: list[_Entry] = []
            removed_count = 0
            for entry in group[user_key]:
                if entry["expires_at"] is not None and entry["expires_at"] < now:
                    removed_count += 1
                else:
                    kept.append(entry)
            if not removed_count:
                continue
            total_removed += removed_count
            removed.append(f"群 {group_key} 成员 {user_key}：{removed_count} 条")
            if kept:
                group[user_key] = kept
            else:
                del group[user_key]
        if not group:
            del data[group_key]
    if not removed:
        print("没有已过期的状态记录。")
        return 0
    changes = [f"清理已过期状态：共 {total_removed} 条（涉及 {len(removed)} 名成员）"]
    changes.extend(f"  {line}" for line in removed)
    _pipeline.execute_change(
        ctx,
        _pipeline.ChangePlan(
            path=ctx.data_dir / FILE_REL,
            label=LABEL,
            changes=changes,
            new_data=data,
            validator=normalize,
            expected_mtime_ns=mtime,
        ),
    )
    return 0


def register(subparsers: Any) -> None:
    """注册 states 子命令。"""
    parser = subparsers.add_parser("states", help="道具状态（item/states.json）")
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
