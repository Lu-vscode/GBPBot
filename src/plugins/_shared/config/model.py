"""共享配置的字段声明。

本模块只声明 nonebot 全局配置中的字段；各配置项的含义、解析规则与
读取函数分别在 bot_list.py、nickname.py 与 nickname_guard.py 中，
配置值的通用类型转换见 parse.py。
"""

from typing import Any

from pydantic import BaseModel


class Config(BaseModel):
    """共享配置的字段声明（取值来自全局配置的同名项）。"""

    bot_list: Any = None
    """机器人名单：机器人 QQ 号列表（JSON 数组），默认为空。"""

    nickname_guard_disabled_groups: Any = None
    """不启用群昵称保护的群号列表（JSON 数组），默认为空。"""

    nickname_max_length: Any = None
    """群昵称最大显示长度（正整数），默认 12。"""
