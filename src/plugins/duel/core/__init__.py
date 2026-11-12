"""决斗领域核心（包入口）：内存状态、手势识别与胜负判定。

- duel.py：进行中决斗的值对象 Duel；
- store.py：决斗列表、群内查询、手势记录与移除原语、只读快照；
- gestures.py：猜拳表情的解析与基础胜负关系；
- judgement.py：三层判定引擎（基础规则 → 单组规则 → 整局效果）与注册。

包内其它模块与跨插件服务统一从本包导入领域模型，不直接依赖子模块。
"""

from src.plugins.duel.core.duel import Duel
from src.plugins.duel.core.gestures import (
    WINNING_GESTURES,
    extract_gesture,
    is_rps_message,
    message_to_gesture,
    parse_gesture,
)
from src.plugins.duel.core.judgement import (
    base_outcome,
    judge,
    pair_verdict,
    register_effect,
    register_pair_rule,
    required_gestures,
)
from src.plugins.duel.core.store import (
    add_duel,
    completed_gestures,
    duel_ready,
    duels,
    find_pair_duel,
    find_pending_duel,
    find_provoked_duel,
    group_duels,
    record_gesture,
    remove_duel,
    snapshot,
    take_gesture,
)

__all__ = [
    "WINNING_GESTURES",
    "Duel",
    "add_duel",
    "base_outcome",
    "completed_gestures",
    "duel_ready",
    "duels",
    "extract_gesture",
    "find_pair_duel",
    "find_pending_duel",
    "find_provoked_duel",
    "group_duels",
    "is_rps_message",
    "judge",
    "message_to_gesture",
    "pair_verdict",
    "parse_gesture",
    "record_gesture",
    "register_effect",
    "register_pair_rule",
    "remove_duel",
    "required_gestures",
    "snapshot",
    "take_gesture",
]
