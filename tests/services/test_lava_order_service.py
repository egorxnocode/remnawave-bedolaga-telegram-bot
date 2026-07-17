from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database.models import PaymentMethod, TransactionType
from app.services.lava_order_service import fulfill_lava_service_order
from app.services.payment.lava import LavaPaymentMixin


def _result(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value)


@pytest.mark.asyncio
async def test_new_authenticated_lava_balance_topup_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(type(settings), 'is_lava_enabled', lambda _self: True)
    db = MagicMock()
    with patch('app.services.payment.lava.lava_service.create_invoice', AsyncMock()) as create_invoice:
        result = await LavaPaymentMixin().create_lava_payment(
            db,
            user_id=42,
            amount_kopeks=10000,
        )

    assert result is None
    create_invoice.assert_not_awaited()


@pytest.mark.asyncio
async def test_fulfilled_order_is_a_noop_on_replayed_callback() -> None:
    order = SimpleNamespace(id=12, status='fulfilled')
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(order))

    with patch('app.services.lava_order_service.create_transaction', AsyncMock()) as create_transaction:
        ok, returned = await fulfill_lava_service_order(
            db,
            order_id=12,
            provider_invoice_id='invoice-1',
        )

    assert ok is True
    assert returned is order
    create_transaction.assert_not_awaited()
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_tariff_order_never_credits_internal_balance() -> None:
    order = SimpleNamespace(
        id=12,
        user_id=42,
        subscription_id=9,
        tariff_id=3,
        recurrent_subscription_id=None,
        kind='tariff',
        status='pending',
        amount_kopeks=27900,
        description="Покупка тарифа 'Стандартный'",
        snapshot={
            'period_days': 30,
            'traffic_limit_gb': 100,
            'device_limit': 3,
            'connected_squads': ['ultra'],
        },
        provider_invoice_id=None,
        transaction_id=None,
        paid_at=None,
        fulfilled_at=None,
        updated_at=None,
    )
    user = SimpleNamespace(id=42, balance_kopeks=12345, has_had_paid_subscription=False, updated_at=None)
    subscription = SimpleNamespace(
        id=9,
        user_id=42,
        remnawave_uuid='rw-user',
        autopay_enabled=True,
        autopay_period_days=30,
    )
    transaction = SimpleNamespace(id=77)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(order), _result(user), _result(subscription)])
    db.commit = AsyncMock()

    with (
        patch(
            'app.services.lava_order_service.extend_subscription',
            AsyncMock(return_value=subscription),
        ) as extend,
        patch(
            'app.services.lava_order_service.create_transaction',
            AsyncMock(return_value=transaction),
        ) as create_transaction,
        patch('app.services.lava_order_service.emit_lava_service_order_side_effects', AsyncMock()) as side_effects,
    ):
        ok, returned = await fulfill_lava_service_order(
            db,
            order_id=12,
            provider_invoice_id='invoice-1',
        )

    assert ok is True
    assert returned is order
    assert user.balance_kopeks == 12345
    assert user.has_had_paid_subscription is True
    assert subscription.autopay_enabled is False
    assert subscription.autopay_period_days is None
    assert order.status == 'fulfilled'
    assert order.transaction_id == 77
    extend.assert_awaited_once()
    create_transaction.assert_awaited_once_with(
        db,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=27900,
        description="Покупка тарифа 'Стандартный'",
        payment_method=PaymentMethod.LAVA,
        external_id='lava-service:invoice-1',
        commit=False,
    )
    side_effects.assert_awaited_once_with(db, order)
