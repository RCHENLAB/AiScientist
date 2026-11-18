"""Dataset identities fixed at run start (issue 142).

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "run_datasets" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "run_datasets",
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("datasets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("dataset_uuid", sa.String(36), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("shared_version_id", sa.Integer(),
                  sa.ForeignKey("shared_dataset_versions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("role", sa.String(32), nullable=True),
    )
    for column in ("dataset_id", "dataset_uuid", "shared_version_id"):
        op.create_index(f"ix_run_datasets_{column}", "run_datasets", [column])


def downgrade() -> None:
    if "run_datasets" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("run_datasets")
