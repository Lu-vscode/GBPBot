"""道具插件的配置模型。

可在 `.env.{environment}` 文件中通过 `ITEM_*` 系列变量配置，缺失或为空时
使用默认行为（默认抽取/合成概率）。plugin_config 在导入时读取一次，
解析与校验见 resolve.py。
"""

from typing import Any

from nonebot import get_plugin_config
from pydantic import BaseModel


class Config(BaseModel):
    """道具插件配置。"""

    item_draw_black: Any = None
    """签到抽取黑色诅咒道具的概率，默认 0.055。"""

    item_draw_gray: Any = None
    """签到抽取灰色垃圾道具的概率，默认 0.55。"""

    item_draw_white: Any = None
    """签到抽取白色普通道具的概率，默认 0.25。"""

    item_draw_green: Any = None
    """签到抽取绿色精良道具的概率，默认 0.1。"""

    item_draw_blue: Any = None
    """签到抽取蓝色稀有道具的概率，默认 0.03。"""

    item_draw_purple: Any = None
    """签到抽取紫色史诗道具的概率，默认 0.01。"""

    item_draw_gold: Any = None
    """签到抽取金色传说道具的概率，默认 0.005。"""

    item_craft_down: Any = None
    """合成获得低一级品质道具的概率，默认 0.24。"""

    item_craft_up: Any = None
    """合成获得高一级品质道具的概率，默认 0.75。"""

    item_craft_up2: Any = None
    """合成获得高两级品质道具的概率，默认 0.01。"""

    item_test_groups: Any = None
    """测试群群号列表（JSON 数组）：仅这些群中的超级用户可使用 /item.get
    获取测试道具，缺失或为空时功能禁用。"""

    item_list_forward_threshold: Any = None
    """道具清单改用合并转发的种类数阈值（正整数），默认 10。"""


plugin_config = get_plugin_config(Config)
