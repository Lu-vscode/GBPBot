"""跨插件共享配置（包入口）。

若干插件共同使用的配置集中在本包读取与解析，各插件只依赖本共享包、
不互相导入；配置在 `.env.{environment}` 文件中设置。当前共享配置：

- `BOT_LIST`（bot_list.py）：机器人名单（机器人 QQ 号列表，所有群通用）。
  名单中的机器人不参与决斗（不能向其发起决斗），道具交换时也不能向其发起。
- `NICKNAME_GUARD_DISABLED_GROUPS`（nickname_guard.py）：不启用群昵称保护
  的群号列表，群昵称保护插件读取该配置，篡天道具（002）在禁用本群前读取
  并合并现有名单。
- `NICKNAME_MAX_LENGTH`（nickname.py）：bot 消息中群昵称的最大显示长度
  （字符数），决斗与道具插件共用，避免两侧截断规则不一致。

字段声明见 model.py，配置值的通用转换见 parse.py。
"""

from src.plugins._shared.config.bot_list import get_bot_list, normalize_bot_list
from src.plugins._shared.config.model import Config
from src.plugins._shared.config.nickname import NICKNAME_MAX_LENGTH
from src.plugins._shared.config.nickname_guard import get_nickname_guard_disabled_groups

__all__ = [
    "NICKNAME_MAX_LENGTH",
    "Config",
    "get_bot_list",
    "get_nickname_guard_disabled_groups",
    "normalize_bot_list",
]
