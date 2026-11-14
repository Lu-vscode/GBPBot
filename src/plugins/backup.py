"""数据库自动备份插件。

每天凌晨 5 点（本地时区，BACKUP_TIME 可配置）对 nonebot-plugin-orm 的
SQLite 数据库做一次快照备份：用 `VACUUM INTO` 在后台线程生成内容一致、
不含 -wal/-shm 残留的独立副本，完整性检查通过后按时间戳命名归档到备份
目录（默认 data/backup），并只保留最近 BACKUP_KEEP 份（默认 30 份）。

备份在机器人运行期间完成，不需要停服；备份目录与 `gbpedit` 的安全快照
（data/_backup/）互不影响，后者由数据编辑工具自行管理。启动时若当天还
没有备份（如凌晨 5 点时服务未运行），会在启动 60 秒后补做一次；备份失败
只记录错误日志，不影响机器人其它功能。

【后续开发注意】
1. 备份依赖 SQLite 的 `VACUUM INTO`；改为其它数据库（如 PostgreSQL）时
   插件自动禁用，需要另行为该数据库提供备份方式。
2. 备份文件名（gbp-YYYYmmdd-HHMMSS.sqlite3）同时用于识别与排序（按文件名
   即按时间），不要改动已有命名，否则清理逻辑会漏删或误删无关文件。
"""

import asyncio
import contextlib
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nonebot import get_driver, get_plugin_config, logger, require
from nonebot.plugin import PluginMetadata
from pydantic import BaseModel, ValidationInfo, field_validator
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_localstore")

import nonebot_plugin_localstore as localstore
from nonebot_plugin_apscheduler import scheduler

__plugin_meta__ = PluginMetadata(
    name="数据库备份",
    description="每天定时对数据库做一次快照备份，并自动清理过旧的备份",
    usage="自动生效，无触发指令；可通过 BACKUP_* 环境变量配置",
    type="application",
    supported_adapters={"~onebot.v11"},
)


class Config(BaseModel):
    """数据库自动备份插件配置。

    可在 `.env.{environment}` 文件中通过 `BACKUP_*` 系列变量配置，
    缺失或为空时使用默认行为（每天 05:00 备份、保留最近 30 份）。
    """

    backup_time: str | None = None
    """每日备份时刻（本地时区，24 小时制 HH:MM），默认 "05:00"。"""

    backup_keep: int | None = None
    """备份文件保留份数（正整数），超出后删除最旧的，默认 30。"""

    backup_dir: str | None = None
    """备份目录（绝对路径，或相对项目根目录的路径），默认 data/backup。"""

    backup_on_startup: bool | None = None
    """启动时若当天还没有备份是否补做一次，默认是。"""

    @field_validator("backup_time", "backup_dir", "backup_on_startup", mode="before")
    @classmethod
    def blank_as_none(cls, value: Any) -> Any:
        """将空字符串视为未配置，避免变量留空导致插件加载失败。"""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("backup_keep", mode="before")
    @classmethod
    def invalid_int_as_none(cls, value: Any, info: ValidationInfo) -> Any:
        """将空字符串或无法解析为整数的值视为未配置，避免插件加载失败。"""
        if isinstance(value, str):
            if not value.strip():
                return None
            try:
                return int(value)
            except ValueError:
                logger.warning(
                    f"数据库备份配置 {info.field_name}={value!r} 无法解析为整数，"
                    f"已视为未配置并使用默认值"
                )
                return None
        return value


plugin_config = get_plugin_config(Config)

# 每日备份时刻的默认值（本地时区，24 小时制）
_DEFAULT_HOUR = 5
_DEFAULT_MINUTE = 0

# 备份文件保留份数默认值
_DEFAULT_KEEP = 30

# 启动补做前等待的秒数（给其它插件留出初始化时间）
_STARTUP_DELAY_SECONDS = 60

# 生成快照时等待数据库写锁释放的秒数（备份与写入并发时避免直接失败）
_BUSY_TIMEOUT_SECONDS = 30

# 每日定时任务的补偿窗口：进程卡顿或短时停机后仍会执行错过的备份
_MISFIRE_GRACE_SECONDS = 3600

# 备份文件名：gbp-日期-时间.sqlite3（按文件名排序即按时间排序）
_BACKUP_FILE_PATTERN = re.compile(r"^gbp-\d{8}-\d{6}\.sqlite3$")

# 每日备份时刻的书写格式（HH:MM，允许一位数小时）与取值范围
_TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})$")
_MAX_HOUR = 23
_MAX_MINUTE = 59

# 备份互斥锁：定时备份与启动补做不会同时进行
_backup_lock = asyncio.Lock()


def _time_or_default(value: str | None) -> tuple[int, int]:
    """返回每日备份时刻（时, 分）：未配置用默认值，格式或取值无效时回退默认值。"""
    default = (_DEFAULT_HOUR, _DEFAULT_MINUTE)
    text = (value or "").strip()
    if not text:
        return default
    match = _TIME_PATTERN.fullmatch(text)
    if match is None:
        logger.warning(
            f"数据库备份配置 BACKUP_TIME={text!r} 格式无效（需为 HH:MM），"
            f"已使用默认时刻 {_DEFAULT_HOUR:02d}:{_DEFAULT_MINUTE:02d}"
        )
        return default
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > _MAX_HOUR or minute > _MAX_MINUTE:
        logger.warning(
            f"数据库备份配置 BACKUP_TIME={text!r} 超出取值范围（00:00~23:59），"
            f"已使用默认时刻 {_DEFAULT_HOUR:02d}:{_DEFAULT_MINUTE:02d}"
        )
        return default
    return hour, minute


def _keep_or_default(value: int | None) -> int:
    """返回备份保留份数：未配置用默认值，非正数回退默认值。"""
    if value is None:
        return _DEFAULT_KEEP
    if value < 1:
        logger.warning(
            f"数据库备份配置 BACKUP_KEEP={value} 无效（需为正整数），"
            f"已使用默认值 {_DEFAULT_KEEP}"
        )
        return _DEFAULT_KEEP
    return value


def _resolve_backup_dir(value: str | None) -> Path:
    """解析备份目录：未配置时使用本插件的数据目录（默认 data/backup）。"""
    text = (value or "").strip()
    if not text:
        return localstore.get_data_dir("backup").resolve()
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _resolve_database_path() -> Path | None:
    """解析 SQLALCHEMY_DATABASE_URL 指向的 SQLite 数据库文件。

    仅支持文件形式的 SQLite 数据库；配置缺失、无法解析、非 SQLite 或
    内存数据库时返回 None（此时不注册备份任务）。
    """
    raw = getattr(get_driver().config, "sqlalchemy_database_url", None)
    if not isinstance(raw, str) or not raw.strip():
        logger.warning("未配置 SQLALCHEMY_DATABASE_URL，数据库自动备份已禁用")
        return None
    try:
        url = make_url(raw)
    except (ArgumentError, ValueError) as exc:
        logger.warning(
            f"无法解析 SQLALCHEMY_DATABASE_URL（{raw!r}），数据库自动备份已禁用：{exc}"
        )
        return None
    if not url.drivername.startswith("sqlite"):
        logger.warning(
            f"数据库自动备份只支持 SQLite 文件数据库（当前为 {url.drivername}），已禁用"
        )
        return None
    database = url.database
    if not database or database == ":memory:":
        logger.warning("数据库自动备份不支持内存数据库（:memory:），已禁用")
        return None
    path = Path(database).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


# 数据库文件与备份目录在插件加载时确定（配置变更需重启）
_database_path = _resolve_database_path()
_backup_dir = _resolve_backup_dir(plugin_config.backup_dir)
_keep = _keep_or_default(plugin_config.backup_keep)
_startup_backup_enabled = plugin_config.backup_on_startup is not False


def _list_backups() -> list[Path]:
    """列出备份目录中的备份文件（按文件名升序，即从旧到新）。"""
    if not _backup_dir.is_dir():
        return []
    backups = [
        path
        for path in _backup_dir.iterdir()
        if path.is_file() and _BACKUP_FILE_PATTERN.fullmatch(path.name)
    ]
    return sorted(backups, key=lambda path: path.name)


def _has_backup_today() -> bool:
    """判断备份目录中是否已有今天（本地时区）的备份。"""
    prefix = f"gbp-{datetime.now(UTC).astimezone():%Y%m%d}-"
    return any(path.name.startswith(prefix) for path in _list_backups())


def _prune_backups() -> int:
    """只保留最近 _keep 份备份，删除更旧的，返回删除份数。"""
    removed = 0
    for old in _list_backups()[:-_keep]:
        try:
            old.unlink()
        except OSError as exc:
            logger.warning(f"删除过期数据库备份失败（{old}）：{exc}")
        else:
            removed += 1
    return removed


def _vacuum_into(source: Path, target: Path) -> None:
    """用 `VACUUM INTO` 把源库快照到目标文件（目标文件不能已存在）。

    快照内容一致、不含未提交的改动，也不会继承 -wal/-shm 残留，可直接
    拷回作为数据库文件使用。该函数为阻塞调用，由调用方放在后台线程执行。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    with contextlib.closing(
        sqlite3.connect(source, timeout=_BUSY_TIMEOUT_SECONDS)
    ) as conn:
        conn.execute("VACUUM INTO ?", (str(target),))


def _check_snapshot(path: Path) -> None:
    """检查快照文件可读且结构完整（PRAGMA quick_check），不通过时抛异常。"""
    with contextlib.closing(sqlite3.connect(path)) as conn:
        row = conn.execute("PRAGMA quick_check").fetchone()
    if row is None or row[0] != "ok":
        message = f"备份文件完整性检查未通过：{row!r}"
        raise sqlite3.DatabaseError(message)


def _new_backup_file() -> Path:
    """生成新的备份文件路径（本地时区时间戳）。"""
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    return _backup_dir / f"gbp-{stamp}.sqlite3"


async def _run_backup() -> Path | None:
    """执行一次备份：生成临时快照 -> 校验 -> 归档 -> 清理过期备份。"""
    source = _database_path
    if source is None or not source.is_file():
        logger.warning(f"数据库文件不存在，本次自动备份跳过：{source}")
        return None
    started = time.monotonic()
    target = _new_backup_file()
    # 先写临时文件，校验通过后再改名，避免失败时留下半个文件
    temp = target.with_name(f".{target.name}.tmp")
    try:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        await asyncio.to_thread(_vacuum_into, source, temp)
        await asyncio.to_thread(_check_snapshot, temp)
        await asyncio.to_thread(temp.replace, target)
    except (sqlite3.Error, OSError) as exc:
        logger.error(f"数据库自动备份失败（源库 {source}）：{exc}")
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        return None
    size_kib = target.stat().st_size / 1024
    removed = _prune_backups()
    logger.info(
        f"数据库已备份到 {target}（{size_kib:.1f} KiB，"
        f"耗时 {time.monotonic() - started:.2f} 秒），"
        f"已清理 {removed} 份过期备份，当前共 {len(_list_backups())} 份"
    )
    return target


async def backup_database() -> Path | None:
    """执行一次数据库备份，返回备份文件路径（源库不可用或失败时返回 None）。"""
    async with _backup_lock:
        return await _run_backup()


driver = get_driver()


@driver.on_startup
async def _backup_after_startup() -> None:
    """启动时若当天还没有备份（如凌晨 5 点时服务未运行），延迟补做一次。"""
    if not _startup_backup_enabled or _database_path is None:
        return
    if _has_backup_today():
        logger.debug("今天已有数据库备份，无需启动补做")
        return
    run_date = datetime.now(UTC).astimezone() + timedelta(
        seconds=_STARTUP_DELAY_SECONDS
    )
    scheduler.add_job(
        backup_database,
        "date",
        run_date=run_date,
        id="backup_database_startup",
        replace_existing=True,
        misfire_grace_time=_MISFIRE_GRACE_SECONDS,
    )
    logger.info(f"今天还没有数据库备份，将在 {run_date:%Y-%m-%d %H:%M:%S} 补做一次")


if _database_path is None:
    logger.warning("数据库自动备份已禁用：仅支持 SQLite 文件数据库")
else:
    _hour, _minute = _time_or_default(plugin_config.backup_time)
    scheduler.add_job(
        backup_database,
        "cron",
        hour=_hour,
        minute=_minute,
        timezone=datetime.now(UTC).astimezone().tzinfo,
        id="backup_database_daily",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=_MISFIRE_GRACE_SECONDS,
    )
    logger.info(
        f"数据库自动备份已启用：每天 {_hour:02d}:{_minute:02d}（本地时区）"
        f"把 {_database_path} 备份到 {_backup_dir}，保留最近 {_keep} 份"
    )
