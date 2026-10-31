"""Save chat dataset attachments and their roles.

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "conversation_datasets" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "conversation_datasets",
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("datasets.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(32), nullable=True),
    )
    op.create_index("ix_conversation_datasets_dataset_id", "conversation_datasets", ["dataset_id"])


def downgrade() -> None:
    op.drop_table("conversation_datasets")
