from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
async def test_activated_callback_fulfils_direct_service_order() -> None:
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
    service_order = SimpleNamespace(id=11, provider_invoice_id=None, transaction_id=77)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(record), _result(None), _result(service_order)])
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()

    with (
        patch(
            'app.services.lava_recurrent_service.fulfill_lava_service_order',
            AsyncMock(return_value=(True, service_order)),
        ) as fulfill,
        patch(
            'app.services.lava_recurrent_service.emit_lava_service_order_side_effects',
            AsyncMock(),
        ) as side_effects,
    ):
        assert await process_lava_recurrent_callback(db, _payload()) is True

    assert record.status == 'activated'
    assert record.is_active is True
    assert record.last_invoice_id == 'lava-invoice-1'
    fulfill.assert_awaited_once_with(
        db,
        order_id=11,
        provider_invoice_id='lava-invoice-1',
        commit=False,
    )
    db.commit.assert_awaited_once()
    side_effects.assert_awaited_once_with(db, service_order)


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

    with patch('app.services.lava_recurrent_service.fulfill_lava_service_order', AsyncMock()) as fulfill:
        assert await process_lava_recurrent_callback(db, _payload()) is True

    fulfill.assert_not_awaited()
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

    with patch('app.services.lava_recurrent_service.fulfill_lava_service_order', AsyncMock()) as fulfill:
        assert await process_lava_recurrent_callback(db, _payload(amount='invalid')) is False

    fulfill.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_is_allowed_for_an_existing_paid_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(type(settings), 'is_lava_recurrent_enabled', lambda _self: True)
    monkeypatch.setattr(
        type(settings),
        'get_lava_recurrent_product_map',
        lambda _self: {('Стандартный', 30): 'product'},
    )
    user = SimpleNamespace(id=42, has_had_paid_subscription=True)
    subscription = SimpleNamespace(id=9, is_trial=True)
    tariff = SimpleNamespace(id=3, name='Стандартный', is_daily=False, get_price_for_period=lambda _days: 27900)
    consumer = SimpleNamespace(consumer_id='bedolaga-user-42', email='user@example.com')
    service_order = SimpleNamespace(
        recurrent_subscription_id=None,
        provider_order_id=None,
        status='created',
        snapshot={'traffic_limit_gb': 100, 'device_limit': 3},
    )
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(subscription), _result(None), _result(consumer)])
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    with patch(
        'app.services.lava_recurrent_service.lava_service.create_recurrent_subscription',
        AsyncMock(return_value={'data': {'subscriptionId': 'provider-sub', 'url': 'https://pay', 'amount': '279'}}),
    ):
        record, payment_url = await start_recurrent_subscription(
            db,
            user=user,
            subscription=subscription,
            tariff=tariff,
            service_order=service_order,
            period_days=30,
            email='user@example.com',
        )

    assert record.subscription_id == 9
    assert payment_url == 'https://pay'
    assert service_order.provider_order_id == record.order_id
    assert service_order.status == 'pending'
