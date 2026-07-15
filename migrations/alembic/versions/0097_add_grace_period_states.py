"""add grace-period rescue state table

Revision ID: 0097
Revises: 0096
Create Date: 2026-07-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = '0097'
down_revision: Union[str, None] = '0096'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'grace_period_states' in inspector.get_table_names():
        return

    op.create_table(
        'grace_period_states',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('subscription_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('remnawave_uuid', sa.String(length=255), nullable=False),
        sa.Column('kind', sa.String(length=20), server_default='expiry', nullable=False),
        sa.Column('state', sa.String(length=20), server_default='activating', nullable=False),
        sa.Column('real_end_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('grace_started_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('grace_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('original_squads', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
        sa.Column('original_traffic_limit_bytes', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('original_device_limit', sa.Integer(), nullable=True),
        sa.Column('rescue_traffic_limit_bytes', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('restored_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_grace_period_states_id', 'grace_period_states', ['id'], unique=False)
    op.create_index('ix_grace_period_states_subscription_id', 'grace_period_states', ['subscription_id'], unique=False)
    op.create_index('ix_grace_period_states_user_id', 'grace_period_states', ['user_id'], unique=False)
    op.create_index('ix_grace_period_states_remnawave_uuid', 'grace_period_states', ['remnawave_uuid'], unique=False)
    op.create_index('ix_grace_period_states_kind', 'grace_period_states', ['kind'], unique=False)
    op.create_index('ix_grace_period_state', 'grace_period_states', ['state'], unique=False)
    op.create_index(
        'uq_grace_period_open_kind',
        'grace_period_states',
        ['subscription_id', 'kind'],
        unique=True,
        postgresql_where=sa.text("state IN ('activating','active','activation_failed','closing')"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'grace_period_states' in inspector.get_table_names():
        op.drop_table('grace_period_states')
