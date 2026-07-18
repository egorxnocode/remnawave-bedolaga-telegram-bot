"""add Lava order deduplication and reconciliation state

Revision ID: 0100
Revises: 0099
Create Date: 2026-07-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0100'
down_revision: Union[str, None] = '0099'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('lava_service_orders', sa.Column('dedup_key', sa.String(length=64), nullable=True))
    op.add_column(
        'lava_service_orders',
        sa.Column('reconciliation_attempts', sa.Integer(), server_default='0', nullable=False),
    )
    op.add_column('lava_service_orders', sa.Column('last_reconciliation_at', sa.DateTime(timezone=True)))
    op.add_column('lava_service_orders', sa.Column('last_reconciliation_error', sa.Text()))
    op.add_column('lava_service_orders', sa.Column('reconciliation_alerted_at', sa.DateTime(timezone=True)))
    op.create_index('ix_lava_service_orders_dedup_key', 'lava_service_orders', ['dedup_key'])
    op.create_index(
        'uq_lava_service_orders_open_dedup_key',
        'lava_service_orders',
        ['dedup_key'],
        unique=True,
        postgresql_where=sa.text("status IN ('created','pending','fulfilling') AND dedup_key IS NOT NULL"),
    )
    op.create_table(
        'lava_refund_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('service_order_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=10), server_default='RUB', nullable=False),
        sa.Column('status', sa.String(length=32), server_default='manual_required', nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('provider_reference', sa.String(length=255), nullable=True),
        sa.Column('admin_comment', sa.Text(), nullable=True),
        sa.Column('revoke_service', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('service_revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('requested_by', sa.Integer(), nullable=True),
        sa.Column('completed_by', sa.Integer(), nullable=True),
        sa.Column('refund_transaction_id', sa.Integer(), nullable=True),
        sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['completed_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['refund_transaction_id'], ['transactions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['requested_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['service_order_id'], ['lava_service_orders.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('refund_transaction_id'),
        sa.UniqueConstraint('service_order_id'),
    )
    op.create_index('ix_lava_refund_requests_id', 'lava_refund_requests', ['id'])
    op.create_index('ix_lava_refund_requests_service_order_id', 'lava_refund_requests', ['service_order_id'])
    op.create_index('ix_lava_refund_requests_user_id', 'lava_refund_requests', ['user_id'])
    op.create_index('ix_lava_refund_requests_status', 'lava_refund_requests', ['status'])


def downgrade() -> None:
    op.drop_table('lava_refund_requests')
    op.drop_index('uq_lava_service_orders_open_dedup_key', table_name='lava_service_orders')
    op.drop_index('ix_lava_service_orders_dedup_key', table_name='lava_service_orders')
    op.drop_column('lava_service_orders', 'reconciliation_alerted_at')
    op.drop_column('lava_service_orders', 'last_reconciliation_error')
    op.drop_column('lava_service_orders', 'last_reconciliation_at')
    op.drop_column('lava_service_orders', 'reconciliation_attempts')
    op.drop_column('lava_service_orders', 'dedup_key')
