"""OneBot v11 群聊消息与成员信息共享工具（包入口）。

供各插件复用的通用发送/显示辅助函数（与具体插件业务无关）：

- naming.py：群成员显示名获取与截断（群昵称优先，按共享配置
  NICKNAME_MAX_LENGTH 截断）；
- messaging.py：群聊文本消息发送（纯文本段构造，避免文案被解析为 CQ 码）、
  群聊合并转发消息发送（节点内容为文本消息段数组）。

本包属于以 "_" 开头的共享代码包，不会被 NoneBot 当作插件加载；
插件中通过 `from src.plugins._shared.onebot import ...` 导入。
"""

from src.plugins._shared.onebot.messaging import send_group_forward, send_group_text
from src.plugins._shared.onebot.naming import (
    member_display_name,
    sender_display_name,
    truncate_name,
)

__all__ = [
    "member_display_name",
    "send_group_forward",
    "send_group_text",
    "sender_display_name",
    "truncate_name",
]
