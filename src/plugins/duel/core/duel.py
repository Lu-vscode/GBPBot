"""一场进行中的决斗（内存值对象）。

状态只保存在内存中，不写数据库；相等判断请使用对象身份（Duel 为
eq=False 的 dataclass）。状态的增删与查询原语见 store.py。
"""

from dataclasses import dataclass, field


@dataclass(eq=False)
class Duel:
    """一场进行中的决斗，保存在内存中（相等判断请使用对象身份）。"""

    group_id: int
    """决斗所在群号。"""

    bot_self_id: int
    """处理该决斗的机器人 QQ 号。"""

    challenger_id: int
    """发起决斗的成员 QQ 号。"""

    challenger_name: str
    """发起决斗一方的群昵称（发起时记录，过长时已按上限截断）。"""

    opponent_id: int
    """接受决斗一方的成员 QQ 号（被 @ 的成员或机器人自己）。"""

    opponent_name: str
    """接受决斗一方的群昵称（发起时记录，过长时已按上限截断）。"""

    multiplier: int
    """决斗点数：胜者分数 +点数、败者分数 -点数。"""

    created_at: float
    """决斗创建时间（time.monotonic，用于超时判断）。"""

    accepted: bool = False
    """对方是否已接受（等待接受时为 False，机器人已掷骰接受时为 True）。"""

    provoked_by_bot: bool = False
    """是否被挑衅：该决斗中对方的手势由机器人代替（对方发送的猜拳表情无效）。"""

    bot_rps_active: bool = False
    """机器人代发猜拳（flow.botplay.send_bot_rps）是否执行中：代发期间对方
    （机器人自己）的消息事件不记录手势，由代发流程回查结果后统一
    记录，避免同一消息被重复记录。"""

    gestures: dict[int, list[int]] = field(default_factory=dict)
    """已发送的猜拳手势：成员 QQ 号 -> 手势结果列表（1 布、2 剪刀、
    3 石头），按发送顺序逐条记录。"""
