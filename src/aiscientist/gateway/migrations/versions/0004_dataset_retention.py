"""Persistent dataset retention notices and removal audit.

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "dataset_retention" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table("dataset_retention",
            sa.Column("dataset_uuid", sa.String(36), primary_key=True),
            sa.Column("dataset_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("owner", sa.String(64), nullable=False),
            sa.Column("dataset_name", sa.String(255), nullable=False),
            sa.Column("size_bytes", sa.BigInteger(), nullable=False),
            sa.Column("notice_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("removal_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index("ix_dataset_retention_user_id", "dataset_retention", ["user_id"])


def downgrade() -> None:
    op.drop_table("dataset_retention")
