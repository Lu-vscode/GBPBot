"""决斗分数数据（data/duel/scores.json）的读取、校验与修改命令。

数据格式与 src/plugins/duel/_scores.py 的加载/写盘规则保持一致：
顶层为 群号字符串 -> QQ 号字符串 -> {"name": 群昵称, "score": 分数}；
score 必须为 int（bool 除外），name 缺失时视为空串。插件加载时会
丢弃的记录在本模块列为问题并拒绝修改；空群等无数据价值的空容器被
静默清理。插件侧数据格式变更时必须同步维护本模块。
"""

import argparse
from pathlib import Path
from typing import Any

from . import _pipeline

FILE_REL = Path("duel/scores.json")
LABEL = "决斗分数数据"

_Record = dict[str, Any]
_Group = dict[str, _Record]
_Scores = dict[str, _Group]


def normalize(raw: Any) -> tuple[_Scores, list[str]]:
    """校验并规范化分数数据，返回（干净数据, 问题列表）。"""
    if not isinstance(raw, dict):
        return {}, ["顶层结构不是 JSON 对象"]
    issues: list[str] = []
    clean: _Scores = {}
    for group_key, members in raw.items():
        if not _pipeline.is_digits(group_key) or not isinstance(members, dict):
            issues.append(f"群记录无效（键：{group_key!r}）")
            continue
        group: _Group = {}
        for user_key, record in members.items():
            if not _pipeline.is_digits(user_key) or not isinstance(record, dict):
                issues.append(f"群 {group_key} 存在无效成员记录（键：{user_key!r}）")
                continue
            score = record.get("score")
            if isinstance(score, bool) or not isinstance(score, int):
                issues.append(
                    f"群 {group_key} 成员 {user_key} 的分数无效（应为整数）：{score!r}"
                )
                continue
            group[user_key] = {"name": str(record.get("name") or ""), "score": score}
        if group:
            clean[group_key] = group
    return clean, issues


def summary(data: _Scores) -> str:
    """verify 命令的摘要文本。"""
    members = sum(len(group) for group in data.values())
    return f"{len(data)} 个群、{members} 名成员"


def _read(ctx: _pipeline.Context) -> tuple[_Scores, int | None]:
    """读取并严格校验分数数据，返回（数据, 原文件修改时间）。"""
    path = ctx.data_dir / FILE_REL
    mtime = _pipeline.current_mtime_ns(path)
    raw = _pipeline.load_json_if_exists(path, LABEL)
    data, issues = normalize(raw if raw is not None else {})
    if issues:
        raise _pipeline.issues_error(LABEL, issues)
    return data, mtime


def _record_text(record: _Record) -> str:
    """成员记录的展示文本。"""
    return f"分数 {record['score']}，昵称 {record['name'] or '（空）'}"


def run_get(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """查看某成员分数。"""
    data, _ = _read(ctx)
    group = data.get(args.group)
    record = group.get(args.user) if group else None
    if record is None:
        print(f"群 {args.group} 成员 {args.user} 没有分数记录（视为 0 分）。")
        return 0
    print(f"群 {args.group} 成员 {args.user}：{_record_text(record)}")
    return 0


def run_list(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """列出分数：-g 指定群时按分数降序，否则列出全部群及人数。"""
    data, _ = _read(ctx)
    if args.group:
        group = data.get(args.group)
        if not group:
            print(f"群 {args.group} 没有分数记录。")
            return 0
        entries = sorted(group.items(), key=lambda kv: (-kv[1]["score"], int(kv[0])))
        print(f"群 {args.group} 共 {len(entries)} 名成员记录（按分数降序）：")
        for rank, (user, record) in enumerate(entries, start=1):
            name = record["name"] or "（空）"
            print(f"  {rank:>3}. {user}  {record['score']:>7}  {name}")
        return 0
    if not data:
        print("没有分数记录。")
        return 0
    print(f"共 {len(data)} 个群：")
    for group_key, members in sorted(data.items(), key=lambda kv: int(kv[0])):
        print(f"  {group_key}：{len(members)} 名成员")
    return 0


def run_set(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """直接设定分数（成员不存在时新建）。"""
    data, mtime = _read(ctx)
    group = data.setdefault(args.group, {})
    record = group.get(args.user)
    if record is None:
        name = args.name or ""
        changes = [f"新增成员 {args.user}：分数 {args.score}，昵称 {name or '（空）'}"]
        group[args.user] = {"name": name, "score": args.score}
    else:
        changes: list[str] = []
        if record["score"] != args.score:
            changes.append(f"成员 {args.user} 分数：{record['score']} -> {args.score}")
        if args.name is not None and record["name"] != args.name:
            changes.append(
                f"成员 {args.user} 昵称：{record['name'] or '（空）'}"
                f" -> {args.name or '（空）'}"
            )
        if not changes:
            print("没有需要修改的内容（数据与目标值一致）。")
            return 0
        new_record = dict(record)
        new_record["score"] = args.score
        if args.name is not None:
            new_record["name"] = args.name
        group[args.user] = new_record
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


def run_add(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """增减分数（成员不存在时以增减量为初始分数新建）。"""
    data, mtime = _read(ctx)
    group = data.setdefault(args.group, {})
    record = group.get(args.user)
    changes: list[str] = []
    if record is None:
        name = args.name or ""
        changes.append(
            f"新增成员 {args.user}：分数 {args.delta}，昵称 {name or '（空）'}"
        )
        group[args.user] = {"name": name, "score": args.delta}
    else:
        new_record = dict(record)
        new_score = record["score"] + args.delta
        if new_score != record["score"]:
            changes.append(
                f"成员 {args.user} 分数：{record['score']} -> {new_score}"
                f"（{args.delta:+d}）"
            )
            new_record["score"] = new_score
        if args.name is not None and record["name"] != args.name:
            changes.append(
                f"成员 {args.user} 昵称：{record['name'] or '（空）'}"
                f" -> {args.name or '（空）'}"
            )
            new_record["name"] = args.name
        if not changes:
            print("没有需要修改的内容（数据与目标值一致）。")
            return 0
        group[args.user] = new_record
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


def run_del(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """删除某成员的分数记录。"""
    data, mtime = _read(ctx)
    group = data.get(args.group)
    record = group.get(args.user) if group else None
    if group is None or record is None:
        message = f"群 {args.group} 中找不到成员 {args.user} 的分数记录。"
        raise _pipeline.SafetyError(message)
    changes = [f"删除成员 {args.user} 的分数记录（原{_record_text(record)}）"]
    del group[args.user]
    if not group:
        del data[args.group]
        changes.append(f"群 {args.group} 已无成员记录，一并移除该群")
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
    """注册 scores 子命令。"""
    parser = subparsers.add_parser("scores", help="决斗分数（duel/scores.json）")
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
