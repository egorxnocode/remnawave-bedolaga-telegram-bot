"""add durable AI support queue and ticket state

Revision ID: 0101
Revises: 0100
Create Date: 2026-07-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = '0101'
down_revision: Union[str, None] = '0100'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'ticket_messages',
        sa.Column('author_kind', sa.String(length=16), server_default='user', nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE ticket_messages SET author_kind = CASE "
            "WHEN is_from_admin THEN 'admin' ELSE 'user' END"
        )
    )
    op.alter_column('ticket_messages', 'author_kind', nullable=False)
    op.alter_column('ticket_messages', 'author_kind', server_default=None)
    op.create_check_constraint(
        'ck_ticket_messages_author_kind',
        'ticket_messages',
        "author_kind IN ('user','admin','ai','system')",
    )

    op.create_table(
        'ai_support_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('trigger_message_id', sa.Integer(), nullable=False),
        sa.Column('channel', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=32), server_default='pending', nullable=False),
        sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error_code', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint('attempt_count >= 0', name='ck_ai_support_jobs_attempt_count'),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','escalated','failed')",
            name='ck_ai_support_jobs_status',
        ),
        sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['trigger_message_id'], ['ticket_messages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('trigger_message_id', name='uq_ai_support_jobs_trigger_message_id'),
    )
    op.create_index('ix_ai_support_jobs_id', 'ai_support_jobs', ['id'])
    op.create_index('ix_ai_support_jobs_ticket_id', 'ai_support_jobs', ['ticket_id'])
    op.create_index('ix_ai_support_jobs_locked_at', 'ai_support_jobs', ['locked_at'])
    op.create_index('ix_ai_support_jobs_claim', 'ai_support_jobs', ['status', 'available_at'])

    op.create_table(
        'ai_support_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('trigger_message_id', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=True),
        sa.Column('model_id', sa.String(length=128), nullable=True),
        sa.Column('prompt_version', sa.String(length=64), nullable=False),
        sa.Column('kb_version', sa.String(length=64), nullable=False),
        sa.Column('decision', sa.String(length=32), nullable=False),
        sa.Column('sanitized_intent', sa.String(length=64), nullable=True),
        sa.Column(
            'reason_codes',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('input_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.Column('output_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.Column('cache_read_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.Column('cache_write_tokens', sa.Integer(), server_default='0', nullable=False),
        sa.Column('estimated_cost_microusd', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column(
            'tool_events',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('answer','escalate','abstain','error')",
            name='ck_ai_support_runs_decision',
        ),
        sa.CheckConstraint(
            'latency_ms IS NULL OR latency_ms >= 0',
            name='ck_ai_support_runs_latency_ms',
        ),
        sa.CheckConstraint(
            'input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 '
            'AND cache_write_tokens >= 0 AND estimated_cost_microusd >= 0',
            name='ck_ai_support_runs_usage_nonnegative',
        ),
        sa.ForeignKeyConstraint(['job_id'], ['ai_support_jobs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['trigger_message_id'], ['ticket_messages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_support_runs_id', 'ai_support_runs', ['id'])
    op.create_index('ix_ai_support_runs_job_id', 'ai_support_runs', ['job_id'])
    op.create_index('ix_ai_support_runs_ticket_id', 'ai_support_runs', ['ticket_id'])
    op.create_index('ix_ai_support_runs_trigger_message_id', 'ai_support_runs', ['trigger_message_id'])
    op.create_index('ix_ai_support_runs_ticket_created', 'ai_support_runs', ['ticket_id', 'created_at'])

    op.add_column('ticket_messages', sa.Column('ai_run_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_ticket_messages_ai_run_id',
        'ticket_messages',
        'ai_support_runs',
        ['ai_run_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_unique_constraint('uq_ticket_messages_ai_run_id', 'ticket_messages', ['ai_run_id'])

    op.create_table(
        'ai_support_ticket_states',
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(length=32), server_default='active', nullable=False),
        sa.Column('last_trigger_message_id', sa.Integer(), nullable=True),
        sa.Column('last_run_id', sa.Integer(), nullable=True),
        sa.Column('taken_over_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('takeover_reason', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('active','processing','escalated','human_owned','disabled')",
            name='ck_ai_support_ticket_states_state',
        ),
        sa.ForeignKeyConstraint(['last_run_id'], ['ai_support_runs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(
            ['last_trigger_message_id'],
            ['ticket_messages.id'],
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('ticket_id'),
    )


def downgrade() -> None:
    op.drop_table('ai_support_ticket_states')
    op.drop_constraint('uq_ticket_messages_ai_run_id', 'ticket_messages', type_='unique')
    op.drop_constraint('fk_ticket_messages_ai_run_id', 'ticket_messages', type_='foreignkey')
    op.drop_column('ticket_messages', 'ai_run_id')
    op.drop_table('ai_support_runs')
    op.drop_table('ai_support_jobs')
    op.drop_constraint('ck_ticket_messages_author_kind', 'ticket_messages', type_='check')
    op.drop_column('ticket_messages', 'author_kind')
