"""metadata_records.description: the dataset description the PI writes the first time a run reads
a dataset, with the owner's edits (DATASET.md, RCHENLAB issue 10 part 3).

The column first came in through the old additive start-up step, so a database may have it already.
The upgrade checks first.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("metadata_records")}
    if "description" not in columns:
        with op.batch_alter_table("metadata_records") as batch:
            batch.add_column(sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("metadata_records") as batch:
        batch.drop_column("description")
