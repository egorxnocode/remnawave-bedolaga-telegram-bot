"""add Lava recurrent subscription state and event ledger

Revision ID: 0098
Revises: 0097
Create Date: 2026-07-17
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0098'
down_revision: Union[str, None] = '0097'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'lava_recurrent_consumers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('consumer_id', sa.String(length=128), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('consumer_id'),
        sa.UniqueConstraint('user_id'),
    )
    op.create_index('ix_lava_recurrent_consumers_id', 'lava_recurrent_consumers', ['id'])
    op.create_index('ix_lava_recurrent_consumers_user_id', 'lava_recurrent_consumers', ['user_id'])
    op.create_index('ix_lava_recurrent_consumers_consumer_id', 'lava_recurrent_consumers', ['consumer_id'])

    op.create_table(
        'lava_recurrent_subscriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('subscription_id', sa.Integer(), nullable=True),
        sa.Column('tariff_id', sa.Integer(), nullable=True),
        sa.Column('product_id', sa.String(length=64), nullable=False),
        sa.Column('consumer_id', sa.String(length=128), nullable=False),
        sa.Column('order_id', sa.String(length=64), nullable=False),
        sa.Column('lava_subscription_id', sa.String(length=128), nullable=True),
        sa.Column('payment_url', sa.Text(), nullable=True),
        sa.Column('period_days', sa.Integer(), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('consent_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consent_ip', sa.String(length=64), nullable=True),
        sa.Column('consent_user_agent', sa.String(length=512), nullable=True),
        sa.Column('status', sa.String(length=32), server_default='created', nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('last_invoice_id', sa.String(length=128), nullable=True),
        sa.Column('payer_details', sa.String(length=255), nullable=True),
        sa.Column('next_pay_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('suspended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deactivated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deactivated_reason', sa.Text(), nullable=True),
        sa.Column('callback_payload', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tariff_id'], ['tariffs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['consumer_id'],
            ['lava_recurrent_consumers.consumer_id'],
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('lava_subscription_id'),
        sa.UniqueConstraint('order_id'),
    )
    op.create_index('ix_lava_recurrent_subscriptions_id', 'lava_recurrent_subscriptions', ['id'])
    op.create_index('ix_lava_recurrent_subscriptions_user_id', 'lava_recurrent_subscriptions', ['user_id'])
    op.create_index(
        'ix_lava_recurrent_subscriptions_subscription_id', 'lava_recurrent_subscriptions', ['subscription_id']
    )
    op.create_index('ix_lava_recurrent_subscriptions_tariff_id', 'lava_recurrent_subscriptions', ['tariff_id'])
    op.create_index('ix_lava_recurrent_subscriptions_product_id', 'lava_recurrent_subscriptions', ['product_id'])
    op.create_index('ix_lava_recurrent_subscriptions_consumer_id', 'lava_recurrent_subscriptions', ['consumer_id'])
    op.create_index('ix_lava_recurrent_subscriptions_order_id', 'lava_recurrent_subscriptions', ['order_id'])
    op.create_index(
        'ix_lava_recurrent_subscriptions_lava_subscription_id',
        'lava_recurrent_subscriptions',
        ['lava_subscription_id'],
    )
    op.create_index(
        'ix_lava_recurrent_subscriptions_last_invoice_id', 'lava_recurrent_subscriptions', ['last_invoice_id']
    )
    op.create_index('ix_lava_recurrent_user_status', 'lava_recurrent_subscriptions', ['user_id', 'status'])
    op.create_index(
        'uq_lava_recurrent_open_subscription',
        'lava_recurrent_subscriptions',
        ['subscription_id'],
        unique=True,
        postgresql_where=sa.text("status IN ('created','activated','suspended','cancel_requested')"),
    )

    op.create_table(
        'lava_recurrent_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('recurrent_subscription_id', sa.Integer(), nullable=False),
        sa.Column('event_key', sa.String(length=128), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('invoice_id', sa.String(length=128), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('outcome', sa.String(length=64), nullable=True),
        sa.Column('transaction_id', sa.Integer(), nullable=True),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ['recurrent_subscription_id'],
            ['lava_recurrent_subscriptions.id'],
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_key'),
    )
    op.create_index('ix_lava_recurrent_events_id', 'lava_recurrent_events', ['id'])
    op.create_index(
        'ix_lava_recurrent_events_recurrent_subscription_id',
        'lava_recurrent_events',
        ['recurrent_subscription_id'],
    )
    op.create_index('ix_lava_recurrent_events_event_key', 'lava_recurrent_events', ['event_key'])
    op.create_index('ix_lava_recurrent_events_status', 'lava_recurrent_events', ['status'])
    op.create_index('ix_lava_recurrent_events_invoice_id', 'lava_recurrent_events', ['invoice_id'])


def downgrade() -> None:
    op.drop_table('lava_recurrent_events')
    op.drop_table('lava_recurrent_subscriptions')
    op.drop_table('lava_recurrent_consumers')
