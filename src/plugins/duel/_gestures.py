"""猜拳手势解析与胜负判定的手势常量。

QQ"包剪锤"表情的结果值（NapCat 等协议端直接透传 QQ 的 resultId）：
1 剪刀、2 石头、3 布，即按"剪刀石头布"顺序编号，与常见的
"1 石头、2 剪刀、3 布"写法相反，胜负判定以实测值为准。
"""

from typing import Any

from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent, Message

# 猜拳手势的合法结果值与文本映射
_GESTURE_CHOICES = frozenset({1, 2, 3})
_GESTURE_TEXT_TO_VALUE = {"1": 1, "2": 2, "3": 3}

# 胜负判定：(挑战者手势, 接受者手势) 在该集合中表示挑战者胜，
# 其余非平局组合为接受者胜（剪刀胜布、石头胜剪刀、布胜石头）
WINNING_GESTURES = frozenset({(1, 3), (2, 1), (3, 2)})

# "包剪锤"表情的 QQ 表情 ID：部分协议端可能以 face 段上报，作兼容处理
_RPS_FACE_ID = 359


def parse_gesture(value: Any) -> int | None:
    """解析猜拳手势值，返回 1-3，无效时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value in _GESTURE_CHOICES else None
    if isinstance(value, str):
        return _GESTURE_TEXT_TO_VALUE.get(value.strip())
    return None


def segment_gesture(segment_type: str, segment_data: dict[str, Any]) -> int | None:
    """从单个消息段中解析猜拳手势，非猜拳段或无效时返回 None。"""
    if segment_type == "rps":
        return parse_gesture(segment_data.get("result"))
    if segment_type == "face" and str(segment_data.get("id")) == str(_RPS_FACE_ID):
        # 兼容部分协议端以 face 段上报"包剪锤"表情的情况（resultId 为结果）
        return parse_gesture(segment_data.get("resultId"))
    return None


def extract_gesture(message: Message) -> int | None:
    """从消息中提取第一个有效的手势。"""
    for segment in message:
        gesture = segment_gesture(segment.type, segment.data)
        if gesture is not None:
            return gesture
    return None


def message_to_gesture(message: Any) -> int | None:
    """从 get_msg 等接口返回的原始消息（字符串或消息段数组）中提取手势。"""
    if isinstance(message, Message):
        return extract_gesture(message)
    if isinstance(message, str):
        return extract_gesture(Message(message))
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict):
                continue
            data = segment.get("data")
            if not isinstance(data, dict):
                continue
            gesture = segment_gesture(str(segment.get("type") or ""), data)
            if gesture is not None:
                return gesture
    return None


def is_rps_message(event: Event) -> bool:
    """判断事件是否为群成员发送的猜拳表情消息（包括机器人自己）。"""
    return (
        isinstance(event, GroupMessageEvent)
        and extract_gesture(event.get_message()) is not None
    )
