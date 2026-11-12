"""道具插件的数据库模型。

- `gbp_item_inventory`：道具库存，主键 (群号, 成员 QQ 号, 道具编号)；
  数量约束 `count > 0` 下推数据库，数量归零即删除条目。
- `gbp_item_state`：道具产生的状态（Buff/永久型等），主键 (群号, 成员
  QQ 号, 状态键)，同键状态直接覆盖；`expires_at` 为 NULL 表示永久，
  `data` 为道具模块自定义的附加数据（JSON）。

模型定义在插件包内，导入本模块即完成注册（nonebot-plugin-orm 会扫描
全部 Model 子类建表）。
"""

from typing import Any

from nonebot import require

require("nonebot_plugin_orm")

from nonebot_plugin_orm import Model
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Float,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column


class ItemInventory(Model):
    """成员在单个群内拥有的一种道具及其数量。"""

    __tablename__ = "gbp_item_inventory"
    __table_args__ = (CheckConstraint("count > 0", name="ck_gbp_item_inventory_count"),)

    group_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    """群号。"""

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    """成员 QQ 号。"""

    item_id: Mapped[str] = mapped_column(String(3), primary_key=True)
    """道具编号（三位数字字符串）。"""

    count: Mapped[int] = mapped_column(Integer)
    """拥有数量（正整数）。"""


class ItemStateRow(Model):
    """成员身上的一个道具状态。"""

    __tablename__ = "gbp_item_state"
    __table_args__ = (Index("ix_gbp_item_state_expires", "expires_at"),)

    group_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    """群号。"""

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    """成员 QQ 号。"""

    state_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    """状态键（同一成员同键状态重复添加时覆盖）。"""

    item_id: Mapped[str] = mapped_column(String(3))
    """产生该状态的道具编号。"""

    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    """过期时间（Unix 时间戳）；NULL 表示永久。"""

    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    """附加数据（道具模块自定义，须可 JSON 序列化）。"""
