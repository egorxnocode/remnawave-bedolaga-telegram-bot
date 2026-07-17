"""Regression coverage for Lava's path-based Cabinet return flow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cabinet.routes import balance as balance_route
from app.database.models import User


@pytest.mark.asyncio
async def test_latest_payment_by_method_supports_lava() -> None:
    """External Lava redirects must resolve without relying on sessionStorage."""
    now = datetime.now(UTC)
    user = User(id=7, telegram_id=7007, username='lava-user')
    payment = SimpleNamespace(
        id=42,
        user_id=user.id,
        user=user,
        order_id='lava-order-42',
        amount_kopeks=1000,
        status='success',
        is_paid=True,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        payment_url='https://pay.lava.ru/invoice/example',
    )

    result = MagicMock()
    result.scalars.return_value.first.return_value = payment
    db = AsyncMock()
    db.execute.return_value = result

    response = await balance_route.get_latest_payment_by_method(
        method='lava',
        user=user,
        db=db,
    )

    assert response.id == payment.id
    assert response.method == 'lava'
    assert response.identifier == str(payment.id)
    assert response.amount_kopeks == 1000
    assert response.status == 'success'
    assert response.is_paid is True
    assert response.user_id == user.id
