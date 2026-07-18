from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes.admin_payments import (
    LavaRefundConfirmRequest,
    LavaRefundCreateRequest,
    confirm_lava_refund,
    request_lava_refund,
)


def _result(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value)


@pytest.mark.asyncio
async def test_refund_request_rejects_unfulfilled_order() -> None:
    order = SimpleNamespace(id=3, status='pending', transaction_id=None, provider_invoice_id='inv')
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(order))

    with pytest.raises(HTTPException) as caught:
        await request_lava_refund(
            3,
            LavaRefundCreateRequest(reason='Услуга не предоставлена'),
            admin=SimpleNamespace(id=1),
            db=db,
        )

    assert caught.value.status_code == 409
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_manual_refund_confirmation_requires_real_money_marker() -> None:
    db = MagicMock()

    with pytest.raises(HTTPException) as caught:
        await confirm_lava_refund(
            1,
            LavaRefundConfirmRequest(money_returned=False, provider_reference='lava-operation-1'),
            admin=SimpleNamespace(id=1),
            db=db,
        )

    assert caught.value.status_code == 422
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_completed_refund_confirmation_is_idempotent() -> None:
    refund = SimpleNamespace(id=1, status='completed')
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(refund))

    with patch('app.cabinet.routes.admin_payments.create_transaction', AsyncMock()) as create:
        result = await confirm_lava_refund(
            1,
            LavaRefundConfirmRequest(money_returned=True, provider_reference='lava-operation-1'),
            admin=SimpleNamespace(id=1),
            db=db,
        )

    assert result is refund
    create.assert_not_awaited()
