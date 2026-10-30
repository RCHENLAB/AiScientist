"""llm_calls.key_fp and its index: the fingerprint of the API key a call used, which the spike check
reads (RCHENLAB issue 16).

The column first came in through the old additive start-up step, so a database may have it already:
one that main's code started on before migrations, or one made without Alembic. Both steps check
first.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("llm_calls")}
    if "key_fp" not in columns:
        with op.batch_alter_table("llm_calls") as batch:
            batch.add_column(sa.Column("key_fp", sa.String(length=32), nullable=True))
    op.create_index("ix_llm_calls_key_fp_created", "llm_calls", ["key_fp", "created_at"], unique=False,
                    if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_llm_calls_key_fp_created", table_name="llm_calls")
    with op.batch_alter_table("llm_calls") as batch:
        batch.drop_column("key_fp")
