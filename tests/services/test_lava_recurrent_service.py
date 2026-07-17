from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database.models import PaymentMethod, TransactionType
from app.services.lava_recurrent_service import (
    _event_key,
    process_lava_recurrent_callback,
    start_recurrent_subscription,
)


def _result(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value, scalar_one=lambda: value)


def _payload(**overrides):
    payload = {
        'type': '4',
        'status': 'activated',
        'order_id': 'lavarec42_order',
        'subscription_id': 'lava-sub-1',
        'product_id': 'lava-product-1',
        'consumer_id': 'bedolaga-user-42',
        'invoice_id': 'lava-invoice-1',
        'amount': '279.00',
        'activation_time': '2026-07-17T12:00:00Z',
        'next_pay_time': '2026-08-17T12:00:00Z',
        'payer_details': '**** 4242',
    }
    payload.update(overrides)
    return payload


def test_activated_event_key_is_stable_when_non_identity_fields_change() -> None:
    original = _payload()
    replay = _payload(activation_time='2026-07-17 12:00:00+00:00', payer_details='**** 4242 updated')

    assert _event_key(original) == _event_key(replay)


@pytest.mark.asyncio
async def test_activated_callback_converts_trial_without_crediting_balance() -> None:
    record = SimpleNamespace(
        id=7,
        user_id=42,
        subscription_id=9,
        tariff_id=3,
        order_id='lavarec42_order',
        lava_subscription_id='lava-sub-1',
        product_id='lava-product-1',
        consumer_id='bedolaga-user-42',
        amount_kopeks=27900,
        period_days=30,
        status='created',
        is_active=False,
        last_invoice_id=None,
        callback_payload=None,
        updated_at=None,
        payer_details=None,
        next_pay_at=None,
        activated_at=None,
    )
    user = SimpleNamespace(id=42, balance_kopeks=1000, updated_at=None, has_had_paid_subscription=False)
    subscription = SimpleNamespace(id=9, is_trial=True, autopay_enabled=False, autopay_period_days=None)
    tariff = SimpleNamespace(id=3, name='Стандартный', allowed_squads=['squad'], traffic_limit_gb=100, device_limit=3)
    transaction = SimpleNamespace(id=77)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(record), _result(None), _result(None), _result(user), _result(subscription)])
    db.get = AsyncMock(return_value=tariff)
    db.commit = AsyncMock()
    db.add = MagicMock()

    with (
        patch(
            'app.services.lava_recurrent_service.create_transaction',
            AsyncMock(return_value=transaction),
        ) as create_transaction,
        patch(
            'app.services.lava_recurrent_service.emit_transaction_side_effects',
            AsyncMock(),
        ) as emit_side_effects,
        patch('app.services.lava_recurrent_service.extend_subscription', AsyncMock()) as extend,
    ):
        assert await process_lava_recurrent_callback(db, _payload()) is True

    assert user.balance_kopeks == 1000
    assert user.has_had_paid_subscription is True
    assert subscription.autopay_enabled is False
    assert subscription.autopay_period_days is None
    extend.assert_awaited_once()
    assert record.status == 'activated'
    assert record.is_active is True
    assert record.last_invoice_id == 'lava-invoice-1'
    create_transaction.assert_awaited_once_with(
        db,
        user_id=42,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=27900,
        description="Первая покупка тарифа 'Стандартный' через Lava с рекуррентными платежами",
        payment_method=PaymentMethod.LAVA,
        external_id='lava-recurrent:lava-invoice-1',
        commit=False,
    )
    db.commit.assert_awaited_once()
    emit_side_effects.assert_awaited_once()


@pytest.mark.asyncio
async def test_replayed_callback_returns_success_without_second_credit() -> None:
    record = SimpleNamespace(
        order_id='lavarec42_order',
        lava_subscription_id='lava-sub-1',
        product_id='lava-product-1',
        consumer_id='bedolaga-user-42',
    )
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(record), _result(SimpleNamespace(id=1))])
    db.commit = AsyncMock()

    with patch('app.services.lava_recurrent_service.create_transaction', AsyncMock()) as create_transaction:
        assert await process_lava_recurrent_callback(db, _payload()) is True

    create_transaction.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_callback_amount_is_rejected_without_credit() -> None:
    record = SimpleNamespace(
        id=7,
        order_id='lavarec42_order',
        lava_subscription_id='lava-sub-1',
        product_id='lava-product-1',
        consumer_id='bedolaga-user-42',
        amount_kopeks=27900,
        callback_payload=None,
        updated_at=None,
    )
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(record), _result(None)])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()

    with patch('app.services.lava_recurrent_service.create_transaction', AsyncMock()) as create_transaction:
        assert await process_lava_recurrent_callback(db, _payload(amount='invalid')) is False

    create_transaction.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_is_blocked_after_first_paid_purchase(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(type(settings), 'is_lava_recurrent_enabled', lambda _self: True)
    monkeypatch.setattr(
        type(settings),
        'get_lava_recurrent_product_map',
        lambda _self: {('Стандартный', 30): 'product'},
    )
    user = SimpleNamespace(id=42, has_had_paid_subscription=True)
    subscription = SimpleNamespace(id=9, is_trial=True)
    tariff = SimpleNamespace(name='Стандартный', is_daily=False, get_price_for_period=lambda _days: 27900)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(subscription), _result(None)])

    with pytest.raises(ValueError, match='first purchase after trial'):
        await start_recurrent_subscription(
            db,
            user=user,
            subscription=subscription,
            tariff=tariff,
            period_days=30,
            email='user@example.com',
        )
