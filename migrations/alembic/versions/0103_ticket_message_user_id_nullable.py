"""allow null user_id for AI-authored ticket messages

Revision ID: 0103
Revises: 0102
Create Date: 2026-07-22

AI auto-delivery posts ticket messages authored by the assistant itself
(author_kind='ai', is_from_admin=True) with no human author, so user_id must be
nullable. Existing user/admin rows keep their user_id; only new AI rows are null.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0103'
down_revision: Union[str, None] = '0102'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'ticket_messages',
        'user_id',
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    # Caveat: re-adding NOT NULL fails if any AI-authored row with null user_id
    # still exists. Delete or reassign those rows before downgrading.
    op.alter_column(
        'ticket_messages',
        'user_id',
        existing_type=sa.Integer(),
        nullable=False,
    )
