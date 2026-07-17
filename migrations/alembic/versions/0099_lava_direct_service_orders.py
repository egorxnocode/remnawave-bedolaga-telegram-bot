"""add direct Lava service orders

Revision ID: 0099
Revises: 0098
Create Date: 2026-07-17
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0099'
down_revision: Union[str, None] = '0098'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('lava_recurrent_subscriptions', sa.Column('terms_snapshot', sa.JSON(), nullable=True))
    op.create_table(
        'lava_service_orders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('subscription_id', sa.Integer(), nullable=True),
        sa.Column('tariff_id', sa.Integer(), nullable=True),
        sa.Column('recurrent_subscription_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('payment_mode', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=24), server_default='created', nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=10), server_default='RUB', nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('provider_order_id', sa.String(length=64), nullable=True),
        sa.Column('provider_invoice_id', sa.String(length=128), nullable=True),
        sa.Column('transaction_id', sa.Integer(), nullable=True),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('fulfilled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['recurrent_subscription_id'], ['lava_recurrent_subscriptions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tariff_id'], ['tariffs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('provider_order_id'),
        sa.UniqueConstraint('transaction_id'),
    )
    for column in ('id', 'user_id', 'subscription_id', 'tariff_id', 'recurrent_subscription_id', 'kind', 'status', 'provider_order_id', 'provider_invoice_id'):
        op.create_index(f'ix_lava_service_orders_{column}', 'lava_service_orders', [column])
    op.create_index('ix_lava_service_orders_user_status', 'lava_service_orders', ['user_id', 'status'])
    op.create_index('ix_lava_service_orders_target', 'lava_service_orders', ['subscription_id', 'kind'])


def downgrade() -> None:
    op.drop_table('lava_service_orders')
    op.drop_column('lava_recurrent_subscriptions', 'terms_snapshot')
