"""道具库存数据（data/item/inventory.json）的读取、校验与修改命令。

数据格式与 src/plugins/item/_storage.py 的加载/写盘规则保持一致：
顶层为 群号字符串 -> QQ 号字符串 -> 道具编号字符串 -> 数量（正整数）。
插件加载时会丢弃的记录（键或数量无效）在本模块列为问题并拒绝修改；
空成员、空群等无数据价值的空容器被静默清理。插件侧数据格式变更时
必须同步维护本模块。
"""

import argparse
from pathlib import Path
from typing import Any

from . import _pipeline

FILE_REL = Path("item/inventory.json")
LABEL = "道具库存数据"

_UserItems = dict[str, int]
_Group = dict[str, _UserItems]
_Inventory = dict[str, _Group]


def _as_count(value: Any) -> int | None:
    """校验道具数量（正整数），无效时返回 None（与插件规则一致）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def normalize(raw: Any) -> tuple[_Inventory, list[str]]:
    """校验并规范化库存数据，返回（干净数据, 问题列表）。"""
    if not isinstance(raw, dict):
        return {}, ["顶层结构不是 JSON 对象"]
    issues: list[str] = []
    clean: _Inventory = {}
    for group_key, users in raw.items():
        if not _pipeline.is_digits(group_key) or not isinstance(users, dict):
            issues.append(f"群记录无效（键：{group_key!r}）")
            continue
        group: _Group = {}
        for user_key, items in users.items():
            if not _pipeline.is_digits(user_key) or not isinstance(items, dict):
                issues.append(f"群 {group_key} 存在无效成员记录（键：{user_key!r}）")
                continue
            user_items = _normalize_user_items(group_key, user_key, items, issues)
            if user_items:
                group[user_key] = user_items
        if group:
            clean[group_key] = group
    return clean, issues


def _normalize_user_items(
    group_key: Any, user_key: Any, items: dict[Any, Any], issues: list[str]
) -> _UserItems:
    """校验单个成员的道具表；无效条目记入 issues 并跳过。"""
    user_items: _UserItems = {}
    for item_key, count in items.items():
        if not isinstance(item_key, str):
            issues.append(
                f"群 {group_key} 成员 {user_key} 存在无效道具编号：{item_key!r}"
            )
            continue
        count_value = _as_count(count)
        if count_value is None:
            issues.append(
                f"群 {group_key} 成员 {user_key} 的道具 {item_key}"
                f" 数量无效（应为正整数）：{count!r}"
            )
            continue
        user_items[item_key] = count_value
    return user_items


def summary(data: _Inventory) -> str:
    """verify 命令的摘要文本。"""
    members = sum(len(group) for group in data.values())
    items = sum(
        len(user_items) for group in data.values() for user_items in group.values()
    )
    return f"{len(data)} 个群、{members} 名成员、{items} 条道具记录"


def normalize_item_id(raw: str) -> str:
    """把道具编号规范化为三位字符串（如 "44" -> "044"）。"""
    text = raw.strip()
    if not text.isdigit() or int(text) <= 0:
        message = f"道具编号必须为正整数（如 44 或 044）：{raw!r}"
        raise _pipeline.SafetyError(message)
    return f"{int(text):03d}"


def _read(ctx: _pipeline.Context) -> tuple[_Inventory, int | None]:
    """读取并严格校验库存数据，返回（数据, 原文件修改时间）。"""
    path = ctx.data_dir / FILE_REL
    mtime = _pipeline.current_mtime_ns(path)
    raw = _pipeline.load_json_if_exists(path, LABEL)
    data, issues = normalize(raw if raw is not None else {})
    if issues:
        raise _pipeline.issues_error(LABEL, issues)
    return data, mtime


def _items_text(user_items: _UserItems) -> str:
    """成员道具的紧凑展示文本。"""
    return "  ".join(
        f"{item_key}x{user_items[item_key]}" for item_key in sorted(user_items)
    )


def run_get(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """查看某成员的道具。"""
    data, _ = _read(ctx)
    group = data.get(args.group)
    user_items = group.get(args.user) if group else None
    if not user_items:
        print(f"群 {args.group} 成员 {args.user} 没有道具记录。")
        return 0
    total = sum(user_items.values())
    print(f"群 {args.group} 成员 {args.user} 的道具（共 {total} 件）：")
    for item_key in sorted(user_items):
        print(f"  {item_key} x{user_items[item_key]}")
    return 0


def run_list(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """列出某群全部成员的道具。"""
    data, _ = _read(ctx)
    group = data.get(args.group)
    if not group:
        print(f"群 {args.group} 没有道具记录。")
        return 0
    print(f"群 {args.group} 共 {len(group)} 名成员有道具记录：")
    for user_key in sorted(group, key=int):
        print(f"  {user_key}：{_items_text(group[user_key])}")
    return 0


def _check_count(count: int) -> None:
    """校验数量参数为正整数。"""
    if count < 1:
        message = f"数量必须为正整数：{count}"
        raise _pipeline.SafetyError(message)


def run_give(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """给成员增加道具数量（没有该道具时新建条目）。"""
    item_id = normalize_item_id(args.item)
    _check_count(args.count)
    data, mtime = _read(ctx)
    group = data.setdefault(args.group, {})
    user_items = group.setdefault(args.user, {})
    old = user_items.get(item_id, 0)
    user_items[item_id] = old + args.count
    changes = [
        f"成员 {args.user} 道具 {item_id}：{old} -> {old + args.count}（+{args.count}）"
    ]
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


def run_take(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """从成员处扣除道具数量（扣到 0 时移除条目）。"""
    item_id = normalize_item_id(args.item)
    _check_count(args.count)
    data, mtime = _read(ctx)
    group = data.get(args.group)
    old = group.get(args.user, {}).get(item_id) if group else None
    if group is None or old is None:
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
        del group[args.user][item_id]
        changes = [f"成员 {args.user} 道具 {item_id}：{old} -> 0（已移除条目）"]
    else:
        group[args.user][item_id] = remaining
        changes = [
            f"成员 {args.user} 道具 {item_id}：{old} -> {remaining}（-{args.count}）"
        ]
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


def run_set(ctx: _pipeline.Context, args: argparse.Namespace) -> int:
    """直接设定道具数量（设为 0 请用 del 删除条目）。"""
    item_id = normalize_item_id(args.item)
    if args.count < 1:
        message = f"数量必须为正整数（设为 0 请用 del 删除条目）：{args.count}"
        raise _pipeline.SafetyError(message)
    data, mtime = _read(ctx)
    group = data.setdefault(args.group, {})
    user_items = group.setdefault(args.user, {})
    old = user_items.get(item_id, 0)
    if old == args.count:
        print("没有需要修改的内容（数据与目标值一致）。")
        return 0
    user_items[item_id] = args.count
    changes = [f"成员 {args.user} 道具 {item_id}：{old} -> {args.count}"]
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
    """删除成员的道具条目。"""
    item_id = normalize_item_id(args.item)
    data, mtime = _read(ctx)
    group = data.get(args.group)
    old = group.get(args.user, {}).get(item_id) if group else None
    if group is None or old is None:
        message = f"群 {args.group} 成员 {args.user} 没有道具 {item_id}。"
        raise _pipeline.SafetyError(message)
    del group[args.user][item_id]
    changes = [f"删除成员 {args.user} 的道具 {item_id}（原 x{old}）"]
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
    parser = subparsers.add_parser("inventory", help="道具库存（item/inventory.json）")
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
