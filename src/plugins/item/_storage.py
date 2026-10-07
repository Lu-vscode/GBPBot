"""道具插件的数据存储：库存与状态的本地 JSON 读写。

- `inventory.json`：道具库存，结构为 `{"<群号>": {"<QQ>": {"<道具编号>": 数量}}}`
- `states.json`：道具产生的状态（Buff/永久型等），结构为
  `{"<群号>": {"<QQ>": [状态条目]}}`，条目字段：`key`（状态键）、
  `item_id`（来源道具编号）、`expires_at`（Unix 时间戳，null 表示永久）、
  `data`（附加数据）

两个文件都位于 localstore 解析出的插件数据目录，按群号隔离；读取时逐条
校验结构，无效条目跳过（有跳过时记录 warning，尽力保留有效数据）。
本模块仅由道具插件包内导入，不直接被其它插件使用。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nonebot import logger
from nonebot_plugin_localstore import get_plugin_data_file


@dataclass
class ItemState:
    """一个成员身上的道具状态（道具模块经状态 API 读写）。"""

    key: str
    """状态键（同一成员同键状态重复添加时覆盖）。"""

    item_id: str
    """产生该状态的道具编号。"""

    expires_at: float | None = None
    """过期时间（Unix 时间戳）；None 表示永久。作为 add_state 的状态
    模板构造时可省略（由框架按持续时长填充）。"""

    data: dict[str, Any] = field(default_factory=dict)
    """附加数据（道具模块自定义，须可 JSON 序列化）。"""


# 数据文件（localstore 插件数据目录）
_INVENTORY_FILE = get_plugin_data_file("inventory.json")
_STATES_FILE = get_plugin_data_file("states.json")


def _read_json(file: Path, label: str) -> dict[str, Any]:
    """读取 JSON 对象文件，文件不存在或损坏时返回空数据。"""
    if not file.exists():
        return {}
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"读取{label}失败，本次将视为无数据：{exc}")
        return {}
    if not isinstance(raw, dict):
        logger.warning(f"{label}格式异常（应为 JSON 对象），本次将视为无数据")
        return {}
    return raw


def _write_json(file: Path, data: Any, label: str) -> None:
    """把数据写入 JSON 文件，写入失败时仅记录错误。"""
    try:
        file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.error(f"写入{label}失败，本次修改未保存：{exc}")


def _is_digits(value: Any) -> bool:
    """判断是否为十进制数字字符串（群号/QQ 号）。"""
    return isinstance(value, str) and value.isdigit()


def _as_count(value: Any) -> int | None:
    """校验道具数量（正整数），无效时返回 None。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def load_inventory() -> dict[int, dict[int, dict[str, int]]]:
    """读取道具库存，逐条校验；无效条目跳过（有跳过时记录 warning）。"""
    raw = _read_json(_INVENTORY_FILE, "道具库存数据")
    skipped = 0
    inventory: dict[int, dict[int, dict[str, int]]] = {}
    for group_key, users in raw.items():
        if not _is_digits(group_key) or not isinstance(users, dict):
            skipped += 1
            continue
        group: dict[int, dict[str, int]] = {}
        for user_key, items in users.items():
            if not _is_digits(user_key) or not isinstance(items, dict):
                skipped += 1
                continue
            user_items: dict[str, int] = {}
            for item_key, count in items.items():
                count_value = _as_count(count)
                if not isinstance(item_key, str) or count_value is None:
                    skipped += 1
                    continue
                user_items[item_key] = count_value
            if user_items:
                group[int(user_key)] = user_items
        if group:
            inventory[int(group_key)] = group
    if skipped:
        logger.warning(f"道具库存数据中有 {skipped} 条无效记录已被跳过")
    return inventory


def save_inventory(inventory: dict[int, dict[int, dict[str, int]]]) -> None:
    """把道具库存写入本地文件。"""
    data = {
        str(group_id): {str(user_id): dict(items) for user_id, items in users.items()}
        for group_id, users in inventory.items()
    }
    _write_json(_INVENTORY_FILE, data, "道具库存数据")


def _as_state(raw: Any) -> ItemState | None:
    """校验单条状态记录，无效时返回 None。"""
    if not isinstance(raw, dict):
        return None
    key = raw.get("key")
    item_id = raw.get("item_id")
    expires_at = raw.get("expires_at")
    data = raw.get("data", {})
    if not isinstance(key, str) or not key:
        return None
    if not isinstance(item_id, str):
        return None
    if isinstance(expires_at, bool) or (
        expires_at is not None and not isinstance(expires_at, (int, float))
    ):
        return None
    if not isinstance(data, dict):
        return None
    return ItemState(
        key=key,
        item_id=item_id,
        expires_at=None if expires_at is None else float(expires_at),
        data=dict(data),
    )


def load_states() -> dict[int, dict[int, list[ItemState]]]:
    """读取道具状态，逐条校验；无效条目跳过（有跳过时记录 warning）。"""
    raw = _read_json(_STATES_FILE, "道具状态数据")
    skipped = 0
    states: dict[int, dict[int, list[ItemState]]] = {}
    for group_key, users in raw.items():
        if not _is_digits(group_key) or not isinstance(users, dict):
            skipped += 1
            continue
        group: dict[int, list[ItemState]] = {}
        for user_key, entries in users.items():
            if not _is_digits(user_key) or not isinstance(entries, list):
                skipped += 1
                continue
            user_states: list[ItemState] = []
            for entry in entries:
                state = _as_state(entry)
                if state is None:
                    skipped += 1
                    continue
                user_states.append(state)
            if user_states:
                group[int(user_key)] = user_states
        if group:
            states[int(group_key)] = group
    if skipped:
        logger.warning(f"道具状态数据中有 {skipped} 条无效记录已被跳过")
    return states


def save_states(states: dict[int, dict[int, list[ItemState]]]) -> None:
    """把道具状态写入本地文件。"""
    data = {
        str(group_id): {
            str(user_id): [
                {
                    "key": state.key,
                    "item_id": state.item_id,
                    "expires_at": state.expires_at,
                    "data": state.data,
                }
                for state in user_states
            ]
            for user_id, user_states in users.items()
        }
        for group_id, users in states.items()
    }
    _write_json(_STATES_FILE, data, "道具状态数据")
