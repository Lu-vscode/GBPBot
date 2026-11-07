"""初始表结构

迁移 ID: 68117ac7adf9
父迁移: （无）
生成方式: python -m scripts.data.orm revision（依据插件模型自动比对空库生成）

四张表分别对应：决斗分数 gbp_duel_score、道具库存 gbp_item_inventory、
道具状态 gbp_item_state、签到记录 gbp_sign_record。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "68117ac7adf9"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    op.create_table(
        "gbp_duel_score",
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), server_default="", nullable=False),
        sa.Column("score", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("group_id", "user_id", name=op.f("pk_gbp_duel_score")),
        info={"bind_key": "duel"},
    )
    op.create_table(
        "gbp_item_inventory",
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.String(length=3), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "count > 0", name=op.f("ck_gbp_item_inventory_ck_gbp_item_inventory_count")
        ),
        sa.PrimaryKeyConstraint(
            "group_id", "user_id", "item_id", name=op.f("pk_gbp_item_inventory")
        ),
        info={"bind_key": "item"},
    )
    op.create_table(
        "gbp_item_state",
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("state_key", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=3), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint(
            "group_id", "user_id", "state_key", name=op.f("pk_gbp_item_state")
        ),
        info={"bind_key": "item"},
    )
    with op.batch_alter_table("gbp_item_state", schema=None) as batch_op:
        batch_op.create_index("ix_gbp_item_state_expires", ["expires_at"], unique=False)

    op.create_table(
        "gbp_sign_record",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.String(length=10), nullable=False),
        sa.Column("item_id", sa.String(length=3), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gbp_sign_record")),
        sa.UniqueConstraint(
            "group_id", "user_id", "date", name="uq_gbp_sign_record_day"
        ),
        info={"bind_key": "sign"},
    )


def downgrade(name: str = "") -> None:
    if name:
        return
    op.drop_table("gbp_sign_record")
    with op.batch_alter_table("gbp_item_state", schema=None) as batch_op:
        batch_op.drop_index("ix_gbp_item_state_expires")

    op.drop_table("gbp_item_state")
    op.drop_table("gbp_item_inventory")
    op.drop_table("gbp_duel_score")
