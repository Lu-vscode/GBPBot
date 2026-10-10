"""决斗发起限频（全局滑动窗口）。

全部群聊合计每小时最多发起 HOURLY_LIMIT 次决斗（默认 20，可由
DUEL_HOURLY_LIMIT 配置，见 _config.py）；只有通过全部校验、challenge
事件未阻止、准备创建决斗的发起才被登记，用法错误、双方已有进行中
决斗、被 challenge 事件阻止的发起不占用名额。

窗口按滚动 1 小时计算：每次登记时丢弃超过 1 小时的旧记录；因此限额
恢复与单片窗口不同——最早的发起滑出窗口后即可再次发起，而不是等待
下一个自然小时。主模块在发起流程中先经 limit_reached 预检（不消费
名额），再在同步段经 record_initiation 登记（检查与登记之间没有
await，避免并发指令超出上限）；提示的预计等待时间由
retry_after_seconds 计算。
"""

import time
from collections import deque

from src.plugins.duel._config import HOURLY_LIMIT

# 限频窗口长度（秒）
_WINDOW_SECONDS = 3600.0

# 已登记的决斗发起时间（time.monotonic），全局统计、不分群
_initiations: deque[float] = deque()


def _purge(now: float) -> None:
    """丢弃已滑出窗口的发起记录。"""
    while _initiations and now - _initiations[0] >= _WINDOW_SECONDS:
        _initiations.popleft()


def limit_reached() -> bool:
    """判断本小时的决斗发起次数是否已达上限（预检，不消费名额）。"""
    _purge(time.monotonic())
    return len(_initiations) >= HOURLY_LIMIT


def retry_after_seconds() -> float:
    """返回距最早一次发起滑出窗口的剩余秒数（用于预计等待时间提示）。

    没有登记记录时返回 0.0。
    """
    now = time.monotonic()
    _purge(now)
    if not _initiations:
        return 0.0
    return _initiations[0] + _WINDOW_SECONDS - now


def record_initiation() -> bool:
    """登记一次决斗发起（同步段），名额已满时不登记并返回 False。"""
    now = time.monotonic()
    _purge(now)
    if len(_initiations) >= HOURLY_LIMIT:
        return False
    _initiations.append(now)
    return True
