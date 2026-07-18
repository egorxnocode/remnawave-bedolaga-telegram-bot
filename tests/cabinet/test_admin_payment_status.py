from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.cabinet.routes.admin_payments import _get_status_info
from app.database.models import PaymentMethod
from app.services.payment_verification_service import PendingPayment


def _lava_payment(status: str, *, is_paid: bool = False) -> PendingPayment:
    return PendingPayment(
        method=PaymentMethod.LAVA,
        local_id=1,
        identifier='lava-test-order',
        amount_kopeks=2700,
        status=status,
        is_paid=is_paid,
        created_at=datetime.now(UTC),
        user=SimpleNamespace(),
        payment=SimpleNamespace(),
    )


@pytest.mark.parametrize(
    ('status', 'expected'),
    [
        ('created', ('⏳', 'Ожидает оплаты')),
        ('pending', ('⏳', 'Ожидает оплаты')),
        ('processing', ('⌛', 'Обрабатывается')),
        ('success', ('✅', 'Оплачено')),
        ('expired', ('⌛', 'Истёк')),
        ('cancelled', ('❌', 'Отменено')),
        ('failed', ('❌', 'Ошибка')),
    ],
)
def test_lava_status_has_human_readable_label(status: str, expected: tuple[str, str]) -> None:
    assert _get_status_info(_lava_payment(status)) == expected


def test_paid_lava_status_wins_over_provider_text() -> None:
    assert _get_status_info(_lava_payment('unexpected', is_paid=True)) == ('✅', 'Оплачено')
