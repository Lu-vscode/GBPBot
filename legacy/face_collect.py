"""表情收集插件。

自动收集所有会话消息中的 QQ 表情（face 段），把表情 ID 去重记录到
本地文件，为 random_face 等需要发送表情的插件提供数据。

记录不区分普通黄脸与超级表情等类型，只保留出现过的表情 ID，
需要按类型筛选时由使用方自行处理。

记录先写入内存（同一 ID 只保留一条），新记录由定时任务定期批量
写入本地存储，避免高频磁盘 IO；机器人关闭前再落盘一次。数据保存
在 localstore 插件数据目录下的 faces.json，内容为升序排列的 ID 列表。
"""

import json

from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import Event, MessageEvent
from nonebot.message import event_preprocessor
from nonebot.plugin import PluginMetadata
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_localstore import get_plugin_data_file

__plugin_meta__ = PluginMetadata(
    name="表情收集",
    description="收集消息中的 QQ 表情 ID 并去重保存，供其它插件使用",
    usage=(
        "自动生效，无触发指令；表情 ID 去重记录保存在插件数据目录的"
        " faces.json，供 random_face 等需要发送表情的插件使用"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
)

# 表情记录文件（localstore 插件数据目录）
_FACES_FILE = get_plugin_data_file("faces.json")

# 记录条目数上限：防止异常消息中的大量不同 ID 撑大内存与记录文件
_MAX_FACES = 5000

# 落盘检查间隔（秒）：新记录先留在内存，由定时任务定期批量写入
_FLUSH_INTERVAL = 30


def _load_faces() -> set[int]:
    """从本地存储读取表情记录，文件不存在或损坏时返回空记录。"""
    if not _FACES_FILE.exists():
        return set()
    try:
        raw = json.loads(_FACES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"读取表情记录失败，本次将视为空记录：{exc}")
        return set()
    if not isinstance(raw, list):
        logger.warning(
            "读取表情记录失败，本次将视为空记录："
            f"内容应为列表，实际为 {type(raw).__name__}"
        )
        return set()
    return {
        face_id
        for face_id in raw
        if isinstance(face_id, int) and not isinstance(face_id, bool)
    }


# 表情记录：已收集的表情 ID，同一 ID 只保留一条
_faces = _load_faces()

# 已变更但尚未落盘的表情 ID：非空时说明需要写入文件
_pending: set[int] = set()

if _faces:
    logger.info(f"表情收集已加载 {len(_faces)} 条表情记录")
else:
    logger.info("表情收集已启用，开始收集消息中的 QQ 表情")


def _record_face(face_id: int) -> None:
    """记录一个表情 ID；已记录过的 ID 直接跳过，不做任何状态变更。"""
    if face_id in _faces:
        return
    if len(_faces) >= _MAX_FACES:
        return
    _faces.add(face_id)
    _pending.add(face_id)
    logger.debug(f"记录表情 {face_id}")
    if len(_faces) == _MAX_FACES:
        logger.warning(f"表情记录已达上限 {_MAX_FACES} 条，后续新表情将不再记录")


def _flush() -> None:
    """把未落盘的表情记录批量写入本地存储，无变更时跳过。"""
    if not _pending:
        return
    try:
        _FACES_FILE.write_text(
            json.dumps(sorted(_faces), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.error(f"写入表情记录失败，本次变更未保存：{exc}")
        return
    _pending.clear()
    logger.info(f"表情记录已保存，共 {len(_faces)} 条")


@event_preprocessor
async def _capture_faces(event: Event) -> None:
    """捕获消息中的所有表情段并记录。

    作为事件预处理器先于全部响应器执行，即使消息随后被其它插件
    阻断，也不会漏掉其中的表情。
    """
    if not isinstance(event, MessageEvent):
        return
    for segment in event.get_message():
        if segment.type != "face":
            continue
        face_id = str(segment.data.get("id", ""))
        if face_id.isascii() and face_id.isdigit():
            _record_face(int(face_id))


scheduler.add_job(
    _flush,
    "interval",
    seconds=_FLUSH_INTERVAL,
    id="face_collect_flush",
    replace_existing=True,
    misfire_grace_time=30,
)

driver = get_driver()


@driver.on_shutdown
async def _flush_on_shutdown() -> None:
    """机器人关闭前把未落盘的表情记录写入本地存储。"""
    _flush()
