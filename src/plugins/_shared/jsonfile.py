"""插件数据 JSON 文件的安全读写（跨插件共享）。

本模块属于以 "_" 开头的共享代码包，不会被 NoneBot 当作插件加载；
插件中通过 `from src.plugins._shared.jsonfile import ...` 导入。

写入采用"临时文件 + fsync + 原子替换"：写盘过程中进程退出或磁盘
写满时原文件保持不变，不会留下被截断的半个 JSON。

读取遇到损坏（无法解析或顶层不是 JSON 对象）时，先把原文件改名
备份为 `<文件名>.corrupt-<时间戳>` 保留现场，再返回空数据；该文件
在本次进程内被标记为加载失败，后续写入一律拒绝——避免用内存中的
空数据覆盖掉仍可人工恢复的现场文件。重启进程后会重新读取（此时
可用备份文件恢复现场）。
"""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from nonebot import logger

# 本次进程内读取失败的数据文件：写入被拒绝，避免空数据覆盖损坏现场
_load_failed: set[Path] = set()


def _backup_corrupt(path: Path, label: str) -> None:
    """把损坏的数据文件改名备份以保留现场，失败时只记录错误。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    counter = 2
    while backup.exists():
        backup = path.with_name(f"{path.name}.corrupt-{stamp}-{counter}")
        counter += 1
    try:
        path.rename(backup)
    except OSError as exc:
        logger.error(f"{label}已损坏且无法备份（{path}），请尽快人工处理：{exc}")
        return
    logger.error(f"{label}已损坏，原文件已备份为 {backup.name}，请人工检查恢复")


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    """读取 JSON 对象文件，文件不存在时返回空数据。

    文件损坏（无法解析或顶层不是 JSON 对象）时先备份原文件，再返回
    空数据并把该文件标记为加载失败（本次进程内拒绝写入）。
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error(f"读取{label}失败：{exc}")
        _load_failed.add(path)
        _backup_corrupt(path, label)
        return {}
    if not isinstance(raw, dict):
        logger.error(f"{label}格式异常（应为 JSON 对象），已按损坏处理")
        _load_failed.add(path)
        _backup_corrupt(path, label)
        return {}
    return raw


def save_json_atomic(path: Path, data: Any, label: str) -> bool:
    """以临时文件 + fsync + 原子替换写入 JSON，返回是否写入成功。

    写入格式与各插件原有的 save 函数一致（UTF-8、缩进 2、中文不转义）。
    该文件曾在本次进程内读取失败时拒绝写入：此时内存数据是空的，写回
    会覆盖损坏现场，宁可本次不保存。
    """
    if path in _load_failed:
        logger.error(
            f"{label}曾在本次运行中读取失败，已拒绝写入以免覆盖损坏现场"
            f"（{path}）；请修复或删除该文件后重启机器人"
        )
        return False
    text = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
    except OSError as exc:
        logger.error(f"写入{label}失败，本次修改未保存：{exc}")
        return False
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        logger.error(f"写入{label}失败，本次修改未保存：{exc}")
        return False
    return True
