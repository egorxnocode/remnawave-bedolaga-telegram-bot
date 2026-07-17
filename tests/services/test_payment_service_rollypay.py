from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.database.crud.rollypay as rollypay_crud_module
import app.services.payment.rollypay as rollypay_mixin_module
import app.services.payment_service as payment_service_module
from app.config import settings
from app.services.payment.rollypay import ROLLYPAY_STATUS_MAP
from app.services.payment_service import PaymentService


class DummySession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, *_: Any) -> None:
        return None

    async def flush(self) -> None:
        return None


class DummyUser:
    telegram_id = 555


class DummyLocalPayment:
    id = 7


class DummyRollyPayPayment:
    def __init__(self) -> None:
        self.id = 7
        self.user_id = 42
        self.order_id = 'rp555_abc123'
        self.rollypay_payment_id = 'pay-123'
        self.amount_kopeks = 10_000
        self.status = 'success'
        self.is_paid = True
        self.transaction_id = 99
        self.callback_payload: dict[str, Any] | None = None
        self.updated_at = datetime.now(UTC)


class StubRollyPayService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create_payment(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            'payment_id': 'pay-123',
            'pay_url': 'https://pay.rollypay.io/pay/test',
            'expires_at': '2030-01-01T00:00:00+00:00',
        }


def _make_service() -> PaymentService:
    service = PaymentService.__new__(PaymentService)
    service.bot = None
    return service


@pytest.mark.asyncio
async def test_create_rollypay_payment_can_request_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = StubRollyPayService()
    db = DummySession()
    service = _make_service()

    monkeypatch.setattr(settings, 'ROLLYPAY_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'ROLLYPAY_API_KEY', 'api-key', raising=False)
    monkeypatch.setattr(settings, 'ROLLYPAY_SIGNING_SECRET', 'signing-secret', raising=False)
    monkeypatch.setattr(settings, 'ROLLYPAY_MIN_AMOUNT_KOPEKS', 100, raising=False)
    monkeypatch.setattr(settings, 'ROLLYPAY_MAX_AMOUNT_KOPEKS', 1_000_000, raising=False)
    monkeypatch.setattr(settings, 'ROLLYPAY_CURRENCY', 'RUB', raising=False)
    monkeypatch.setattr(rollypay_mixin_module, 'rollypay_service', stub, raising=False)

    async def fake_get_user_by_id(*_: Any) -> DummyUser:
        return DummyUser()

    async def fake_create_payment(**_: Any) -> DummyLocalPayment:
        return DummyLocalPayment()

    monkeypatch.setattr(payment_service_module, 'get_user_by_id', fake_get_user_by_id, raising=False)
    monkeypatch.setattr(rollypay_crud_module, 'create_rollypay_payment', fake_create_payment, raising=False)

    result = await service.create_rollypay_payment(
        db=db,
        user_id=42,
        amount_kopeks=10_000,
        test_mode=True,
    )

    assert result is not None
    assert stub.calls[0]['test'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('event_type', 'provider_status', 'expected_status'),
    [
        ('payment.refunded', 'refunded', 'refunded'),
        ('refund_request.completed', 'refunded', 'refunded'),
        ('payment.chargeback', 'chargeback', 'chargeback'),
    ],
)
async def test_reversal_after_credit_is_recorded_without_debit(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    provider_status: str,
    expected_status: str,
) -> None:
    db = DummySession()
    service = _make_service()
    payment = DummyRollyPayPayment()

    async def get_by_order_id(*_: Any) -> DummyRollyPayPayment:
        return payment

    async def get_for_update(*_: Any) -> DummyRollyPayPayment:
        return payment

    monkeypatch.setattr(rollypay_crud_module, 'get_rollypay_payment_by_order_id', get_by_order_id)
    monkeypatch.setattr(rollypay_crud_module, 'get_rollypay_payment_by_id_for_update', get_for_update)

    processed = await service.process_rollypay_webhook(
        db,
        {
            'event_type': event_type,
            'payment_id': payment.rollypay_payment_id,
            'order_id': payment.order_id,
            'status': provider_status,
            'amount': '100.00',
            'currency': 'RUB',
        },
    )

    assert processed is True
    assert payment.status == expected_status
    assert payment.is_paid is True
    assert payment.transaction_id == 99
    assert db.commits == 1


def test_refunded_status_is_explicitly_mapped() -> None:
    assert ROLLYPAY_STATUS_MAP['refunded'] == ('refunded', False)
