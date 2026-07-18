from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.lava_order_reconciliation_service import LavaOrderReconciliationService
from app.services.payment.lava import LavaPaymentMixin


def test_status_payload_uses_authoritative_invoice_data() -> None:
    payment = SimpleNamespace(order_id='local-order', lava_invoice_id='local-invoice')
    payload = LavaOrderReconciliationService._status_payload(
        payment,
        {
            'status': 'success',
            'data': {
                'status': 'created',
                'id': 'provider-invoice',
                'orderId': 'local-order',
                'amount': 279,
            },
        },
    )

    assert payload['status'] == 'created'
    assert payload['invoice_id'] == 'provider-invoice'
    assert payload['order_id'] == 'local-order'
    assert payload['amount'] == 279


@pytest.mark.asyncio
async def test_paid_reconciliation_retries_local_fulfilment() -> None:
    payment = SimpleNamespace(id=9, is_paid=True, transaction_id=None)
    db = SimpleNamespace()
    mixin = LavaPaymentMixin()
    mixin._finalize_lava_payment = AsyncMock(return_value=True)

    async def locked(_db, payment_id):
        assert payment_id == 9
        return payment

    from app.database.crud import lava as lava_crud

    original = lava_crud.get_lava_payment_by_id_for_update
    lava_crud.get_lava_payment_by_id_for_update = locked
    try:
        result = await mixin.reconcile_paid_lava_payment(db, 9)
    finally:
        lava_crud.get_lava_payment_by_id_for_update = original

    assert result is True
    mixin._finalize_lava_payment.assert_awaited_once_with(db, payment, trigger='reconciliation')


@pytest.mark.asyncio
async def test_paid_reconciliation_is_noop_after_transaction_link() -> None:
    payment = SimpleNamespace(id=9, is_paid=True, transaction_id=77)
    db = SimpleNamespace()
    mixin = LavaPaymentMixin()
    mixin._finalize_lava_payment = AsyncMock()

    async def locked(_db, _payment_id):
        return payment

    from app.database.crud import lava as lava_crud

    original = lava_crud.get_lava_payment_by_id_for_update
    lava_crud.get_lava_payment_by_id_for_update = locked
    try:
        result = await mixin.reconcile_paid_lava_payment(db, 9)
    finally:
        lava_crud.get_lava_payment_by_id_for_update = original

    assert result is True
    mixin._finalize_lava_payment.assert_not_awaited()
