"""Shared datasets attached to a chat (RCHENLAB/AiScientist issue 100).

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "conversation_shared_datasets" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "conversation_shared_datasets",
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("shared_datasets.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("version_id", sa.Integer(), sa.ForeignKey("shared_dataset_versions.id", ondelete="CASCADE"),
                  nullable=True),
        sa.Column("files", sa.Text(), nullable=True),
        sa.Column("role", sa.String(32), nullable=False, server_default="reference"),
        sa.Column("position", sa.Integer(), nullable=False),
    )
    op.create_index("ix_conversation_shared_datasets_dataset_id", "conversation_shared_datasets", ["dataset_id"])


def downgrade() -> None:
    op.drop_table("conversation_shared_datasets")
