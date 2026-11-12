"""决斗插件的数据库模型。

分数按群隔离保存在 `gbp_duel_score` 表中，主键为 (群号, 成员 QQ 号)；
昵称随分数一起更新，仅用于排行榜展示（写入时按显示规范截断）。
模型定义在插件包内，导入本模块即完成注册（nonebot-plugin-orm 会扫描
全部 Model 子类建表）。
"""

from nonebot import require

require("nonebot_plugin_orm")

from nonebot_plugin_orm import Model
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column


class DuelScore(Model):
    """成员在单个群内的决斗分数。"""

    __tablename__ = "gbp_duel_score"

    group_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    """群号。"""

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    """成员 QQ 号。"""

    name: Mapped[str] = mapped_column(String(64), default="", server_default="")
    """最近一次写入时的群昵称（已按显示规范截断）。"""

    score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    """决斗分数。"""
