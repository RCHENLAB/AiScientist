"""Baseline: the schema every deployment had when migrations were introduced.

A database created before then (``create_all`` plus the additive columns of ``db._legacy_upgrade``)
is brought to this shape by those same steps and stamped with this revision, so this file runs only
on a new, empty database. Later changes are revisions of their own; never edit this one.

Revision ID: 0001
Revises:
Create Date: 2026-10-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('compute_estimates',
    sa.Column('run_id', sa.String(length=32), nullable=False),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('run_id')
    )
    op.create_table('compute_usage',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.String(length=32), nullable=False),
    sa.Column('skill', sa.String(length=128), nullable=False),
    sa.Column('units', sa.Float(), nullable=False),
    sa.Column('seconds', sa.Float(), nullable=False),
    sa.Column('cpu_hours', sa.Float(), nullable=False),
    sa.Column('gpu_hours', sa.Float(), nullable=False),
    sa.Column('cost', sa.Float(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('success', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_compute_usage_run_id', 'compute_usage', ['run_id'], unique=False)
    op.create_index('ix_compute_usage_skill', 'compute_usage', ['skill'], unique=False)
    op.create_table('pending_registrations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('code_hash', sa.String(length=255), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_pending_registrations_email', 'pending_registrations', ['email'], unique=False)
    op.create_index('ix_pending_registrations_username', 'pending_registrations', ['username'], unique=False)
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_users_username', 'users', ['username'], unique=True)
    op.create_table('access_tokens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('prefix', sa.String(length=16), nullable=False),
    sa.Column('scope', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_access_tokens_token_hash', 'access_tokens', ['token_hash'], unique=True)
    op.create_index('ix_access_tokens_user_id', 'access_tokens', ['user_id'], unique=False)
    op.create_table('conversations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('preset_key', sa.String(length=64), nullable=True),
    sa.Column('context_prompt', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_conversations_user_id', 'conversations', ['user_id'], unique=False)
    op.create_table('datasets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('path', sa.String(length=1024), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=True),
    sa.Column('uploaded_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('conversation_id', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uuid')
    )
    op.create_index('ix_datasets_user_id', 'datasets', ['user_id'], unique=False)
    op.create_table('llm_calls',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.String(length=32), nullable=True),
    sa.Column('conversation_id', sa.String(length=64), nullable=True),
    sa.Column('step', sa.String(length=160), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('role', sa.String(length=24), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('endpoint', sa.String(length=128), nullable=False),
    sa.Column('remote', sa.Boolean(), nullable=False),
    sa.Column('provider', sa.String(length=16), nullable=True),
    sa.Column('prompt_tokens', sa.Integer(), nullable=False),
    sa.Column('completion_tokens', sa.Integer(), nullable=False),
    sa.Column('total_tokens', sa.Integer(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('cache_write_tokens', sa.Integer(), nullable=True),
    sa.Column('cache_read_tokens', sa.Integer(), nullable=True),
    sa.Column('cost_usd', sa.Float(), nullable=True),
    sa.Column('est_cost_usd', sa.Float(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=True),
    sa.Column('error_type', sa.String(length=32), nullable=True),
    sa.Column('max_tokens', sa.Integer(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_llm_calls_conversation_id', 'llm_calls', ['conversation_id'], unique=False)
    op.create_index('ix_llm_calls_run_id', 'llm_calls', ['run_id'], unique=False)
    op.create_index('ix_llm_calls_user_id', 'llm_calls', ['user_id'], unique=False)
    op.create_table('metadata_links',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('parent_uuid', sa.String(length=36), nullable=False),
    sa.Column('child_uuid', sa.String(length=36), nullable=False),
    sa.Column('relation', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_metadata_links_child_uuid', 'metadata_links', ['child_uuid'], unique=False)
    op.create_index('ix_metadata_links_parent_uuid', 'metadata_links', ['parent_uuid'], unique=False)
    op.create_index('ix_metadata_links_user_id', 'metadata_links', ['user_id'], unique=False)
    op.create_table('usage_budgets',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('monthly_credit_usd', sa.Float(), nullable=True),
    sa.Column('reset_day', sa.Integer(), nullable=False),
    sa.Column('thresholds', sa.String(length=64), nullable=False),
    sa.Column('hard_limit_usd', sa.Float(), nullable=True),
    sa.Column('hard_limit_mode', sa.String(length=16), nullable=False),
    sa.Column('notified_cycle', sa.String(length=10), nullable=True),
    sa.Column('notified', sa.String(length=64), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('meta', sa.Text(), nullable=True),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_messages_conversation_id', 'messages', ['conversation_id'], unique=False)
    op.create_table('metadata_records',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uuid', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('dataset_id', sa.Integer(), nullable=True),
    sa.Column('run_id', sa.String(length=32), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('document', sa.Text(), nullable=False),
    sa.Column('facts', sa.Text(), nullable=False),
    sa.Column('user_fields', sa.Text(), nullable=False),
    sa.Column('provenance', sa.Text(), nullable=False),
    sa.Column('confidence', sa.Text(), nullable=False),
    sa.Column('search_text', sa.Text(), nullable=False),
    sa.Column('facets', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('validation', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_metadata_records_dataset_id', 'metadata_records', ['dataset_id'], unique=False)
    op.create_index('ix_metadata_records_run_id', 'metadata_records', ['run_id'], unique=False)
    op.create_index('ix_metadata_records_user_id', 'metadata_records', ['user_id'], unique=False)
    op.create_index('ix_metadata_records_uuid', 'metadata_records', ['uuid'], unique=True)
    op.create_table('runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.String(length=32), nullable=False),
    sa.Column('conversation_id', sa.String(length=64), nullable=True),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('dataset_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('plan_mode', sa.Boolean(), nullable=False),
    sa.Column('artifacts_path', sa.String(length=1024), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_runs_conversation_id', 'runs', ['conversation_id'], unique=False)
    op.create_index('ix_runs_run_id', 'runs', ['run_id'], unique=True)
    op.create_index('ix_runs_user_id', 'runs', ['user_id'], unique=False)


def downgrade() -> None:
    for table in ('runs', 'metadata_records', 'messages', 'usage_budgets', 'metadata_links',
                  'llm_calls', 'datasets', 'conversations', 'access_tokens', 'users',
                  'pending_registrations', 'compute_usage', 'compute_estimates'):
        op.drop_table(table)
