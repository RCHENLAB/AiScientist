"""Shared datasets: a dataset the lab maintains for every account, in immutable versions
(RCHENLAB/AiScientist issue 92).

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "shared_datasets" not in tables:
        op.create_table(
            "shared_datasets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("uuid", sa.String(36), nullable=False),
            sa.Column("slug", sa.String(40), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("citation", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("uuid"),
            sa.UniqueConstraint("slug"),
        )
    if "shared_dataset_versions" not in tables:
        op.create_table(
            "shared_dataset_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("uuid", sa.String(36), nullable=False),
            sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("shared_datasets.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("label", sa.String(40), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("manifest_sha256", sa.String(64), nullable=True),
            sa.Column("changelog", sa.Text(), nullable=False),
            sa.Column("size_bytes", sa.BigInteger(), nullable=False),
            sa.Column("n_files", sa.Integer(), nullable=False),
            sa.Column("published_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("hpc_verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("uuid"),
            sa.UniqueConstraint("dataset_id", "label"),
        )
        op.create_index("ix_shared_dataset_versions_dataset_id", "shared_dataset_versions", ["dataset_id"])
    if "shared_dataset_files" not in tables:
        op.create_table(
            "shared_dataset_files",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("version_id", sa.Integer(),
                      sa.ForeignKey("shared_dataset_versions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(512), nullable=False),
            sa.Column("size_bytes", sa.BigInteger(), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("facets", sa.Text(), nullable=False),
            sa.UniqueConstraint("version_id", "name"),
        )
        op.create_index("ix_shared_dataset_files_version_id", "shared_dataset_files", ["version_id"])
    if "shared_dataset_maintainers" not in tables:
        op.create_table(
            "shared_dataset_maintainers",
            sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("shared_datasets.id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_shared_dataset_maintainers_user_id", "shared_dataset_maintainers", ["user_id"])


def downgrade() -> None:
    op.drop_table("shared_dataset_maintainers")
    op.drop_table("shared_dataset_files")
    op.drop_table("shared_dataset_versions")
    op.drop_table("shared_datasets")
