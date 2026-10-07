"""跨插件共享配置。

若干插件共同使用的配置集中在本模块读取与解析，各插件只依赖本共享
模块、不互相导入；配置在 `.env.{environment}` 文件中设置。

当前共享配置：

- `BOT_LIST`：机器人名单（机器人 QQ 号列表，所有群通用）。名单中的
  机器人不参与决斗（不能向其发起决斗），道具交换时也不能向其发起。
"""

from functools import lru_cache
from typing import Any

from nonebot import get_plugin_config, logger
from pydantic import BaseModel


class Config(BaseModel):
    """共享配置的字段声明（取值来自全局配置的同名项）。"""

    bot_list: Any = None
    """机器人名单：机器人 QQ 号列表（JSON 数组），默认为空。"""


def _as_qq(value: Any) -> int | None:
    """把配置项转换为 QQ 号，无效时返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _normalize_bot_list(value: Any) -> frozenset[int]:
    """校验并返回机器人名单（所有群通用的机器人 QQ 号集合）。

    配置应为机器人 QQ 号列表（JSON 数组）；兼容旧版按群配置格式的
    JSON 对象（键为群号、值为机器人 QQ 号列表），此时各群名单会合并。
    """
    if value is None:
        return frozenset()
    if isinstance(value, dict):
        # 旧版按群配置格式：各群名单合并为全局名单
        members = [
            qq for bots in value.values() if isinstance(bots, list) for qq in bots
        ]
        if members:
            logger.warning(
                "配置 BOT_LIST 使用了旧版按群配置格式，"
                "已将各群的机器人合并为全局名单，建议改为机器人 QQ 号列表"
            )
        value = members
    if not isinstance(value, list):
        logger.warning("配置 BOT_LIST 格式无效（应为机器人 QQ 号列表），已视为空名单")
        return frozenset()
    return frozenset(qq for item in value if (qq := _as_qq(item)) is not None)


@lru_cache(maxsize=1)
def get_bot_list() -> frozenset[int]:
    """返回机器人名单（共享配置 BOT_LIST，所有群通用）。

    首次调用时读取配置并缓存（配置在运行期间不变）；
    配置无效时记录 warning 并视为空名单。
    """
    return _normalize_bot_list(get_plugin_config(Config).bot_list)
