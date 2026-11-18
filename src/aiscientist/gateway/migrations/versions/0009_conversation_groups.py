"""Chat groups in the sidebar, and where a chat's title came from.

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "conversation_groups" not in inspector.get_table_names():
        op.create_table(
            "conversation_groups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_conversation_groups_user_id", "conversation_groups", ["user_id"])
    columns = {c["name"] for c in inspector.get_columns("conversations")}
    # Plain columns: SQLite cannot add a foreign key to an existing table, and the API checks that a
    # group belongs to the chat's owner and clears the column when a group is deleted.
    if "group_id" not in columns:
        op.add_column("conversations", sa.Column("group_id", sa.Integer(), nullable=True))
        op.create_index("ix_conversations_group_id", "conversations", ["group_id"])
    if "title_source" not in columns:
        op.add_column("conversations", sa.Column("title_source", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_index("ix_conversations_group_id", "conversations")
    op.drop_column("conversations", "title_source")
    op.drop_column("conversations", "group_id")
    op.drop_table("conversation_groups")
