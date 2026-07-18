"""add operator-only AI support shadow drafts

Revision ID: 0102
Revises: 0101
Create Date: 2026-07-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = '0102'
down_revision: Union[str, None] = '0101'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ai_support_drafts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('trigger_message_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=32), server_default='pending', nullable=False),
        sa.Column('answer_text', sa.Text(), nullable=False),
        sa.Column(
            'citations',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column('reviewed_text', sa.Text(), nullable=True),
        sa.Column('reviewed_by_user_id', sa.Integer(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('review_reason', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','accepted','rejected','superseded')",
            name='ck_ai_support_drafts_status',
        ),
        sa.ForeignKeyConstraint(['reviewed_by_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['run_id'], ['ai_support_runs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['trigger_message_id'], ['ticket_messages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('run_id', name='uq_ai_support_drafts_run_id'),
    )
    op.create_index('ix_ai_support_drafts_id', 'ai_support_drafts', ['id'])
    op.create_index('ix_ai_support_drafts_ticket_id', 'ai_support_drafts', ['ticket_id'])
    op.create_index('ix_ai_support_drafts_trigger_message_id', 'ai_support_drafts', ['trigger_message_id'])
    op.create_index('ix_ai_support_drafts_ticket_created', 'ai_support_drafts', ['ticket_id', 'created_at'])


def downgrade() -> None:
    op.drop_table('ai_support_drafts')
