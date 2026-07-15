from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.database.models import SubscriptionStatus
from app.external.remnawave_api import TrafficLimitStrategy, UserStatus as RemnaWaveUserStatus
from app.services.grace_period_service import (
    GracePeriodService,
    _parse_test_user_scope,
    _traffic_cycle_limit_increased,
)
from app.services.remnawave_webhook_service import RemnaWaveWebhookService


class _AsyncContext:
    def __init__(self, value) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


def _state(now: datetime, *, state: str = 'activating', kind: str = 'expiry') -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        subscription_id=10,
        user_id=20,
        remnawave_uuid='remna-user',
        kind=kind,
        state=state,
        real_end_date=now - timedelta(minutes=1),
        grace_started_at=now,
        grace_expires_at=now + timedelta(days=5),
        rescue_traffic_limit_bytes=4 * 1024**3,
        restored_at=None,
        closed_at=None,
        last_error=None,
        updated_at=now,
    )


def test_parse_test_scope_is_fail_closed_and_requires_explicit_all() -> None:
    assert _parse_test_user_scope('') == set()
    assert _parse_test_user_scope('invalid,-3') == set()
    assert _parse_test_user_scope('12, 34,invalid') == {12, 34}
    assert _parse_test_user_scope('all') is None


def test_same_commercial_quota_cannot_receive_traffic_rescue_twice() -> None:
    latest = SimpleNamespace(original_traffic_limit_bytes=100 * 1024**3)

    assert _traffic_cycle_limit_increased(100 * 1024**3, latest) is False
    assert _traffic_cycle_limit_increased(105 * 1024**3, latest) is True


def test_service_does_not_start_without_test_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'GRACE_PERIOD_ENABLED', True)
    monkeypatch.setattr(settings, 'GRACE_PERIOD_SQUAD_UUID', 'grace-squad')
    monkeypatch.setattr(settings, 'GRACE_PERIOD_TEST_USER_IDS', '')

    assert GracePeriodService.is_configured() is False


async def test_activation_uses_only_grace_squad_and_additional_traffic(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    state = _state(now)
    db = AsyncMock()
    api = SimpleNamespace(update_user=AsyncMock())
    service = GracePeriodService()
    service.subscription_service.get_api_client = lambda: _AsyncContext(api)
    monkeypatch.setattr(settings, 'GRACE_PERIOD_SQUAD_UUID', 'grace-squad')

    activated = await service._activate_state(db, state)

    assert activated is True
    assert state.state == 'active'
    api.update_user.assert_awaited_once_with(
        uuid='remna-user',
        status=RemnaWaveUserStatus.ACTIVE,
        expire_at=state.grace_expires_at,
        traffic_limit_bytes=4 * 1024**3,
        traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
        active_internal_squads=['grace-squad'],
    )
    db.commit.assert_awaited_once()


async def test_close_failure_stays_closing_and_never_becomes_activation_failure() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    state = _state(now, state='active')
    db = AsyncMock()
    service = GracePeriodService()
    service.subscription_service.disable_remnawave_user = AsyncMock(return_value=False)

    closed = await service._close_rescue_access(db, state, now, reason='traffic allowance exhausted')

    assert closed is False
    assert state.state == 'closing'
    assert state.closed_at is None
    assert 'traffic allowance exhausted' in state.last_error


async def test_traffic_grace_uses_dedicated_non_whitelist_squad(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    state = _state(now, kind='traffic')
    db = AsyncMock()
    api = SimpleNamespace(update_user=AsyncMock())
    service = GracePeriodService()
    service.subscription_service.get_api_client = lambda: _AsyncContext(api)
    monkeypatch.setattr(settings, 'TRAFFIC_GRACE_SQUAD_UUID', 'grace-wl-squad')

    activated = await service._activate_state(db, state)

    assert activated is True
    assert api.update_user.await_args.kwargs['active_internal_squads'] == ['grace-wl-squad']


async def test_traffic_topup_restores_commercial_limit_without_resetting_usage() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    state = _state(now, state='active', kind='traffic')
    state.original_traffic_limit_bytes = 100 * 1024**3
    subscription = SimpleNamespace(
        id=10,
        user_id=20,
        status=SubscriptionStatus.LIMITED.value,
        traffic_limit_gb=110,
    )
    db = AsyncMock()
    service = GracePeriodService()
    service.subscription_service.update_remnawave_user = AsyncMock(return_value=SimpleNamespace(uuid='remna-user'))

    restored = await service._restore_traffic_access(db, subscription, state, now)

    assert restored is True
    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert state.state == 'restored'
    service.subscription_service.update_remnawave_user.assert_awaited_once_with(
        db,
        subscription,
        reset_traffic=False,
        sync_squads=True,
    )


async def test_paid_renewal_restores_tariff_and_resets_temporary_counter() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    state = _state(now, state='active')
    subscription = SimpleNamespace(
        id=10,
        user_id=20,
        is_trial=False,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=now + timedelta(days=30),
    )
    db = AsyncMock()
    service = GracePeriodService()
    service.subscription_service.update_remnawave_user = AsyncMock(return_value=SimpleNamespace(uuid='remna-user'))

    restored = await service._restore_paid_access(db, subscription, state, now)

    assert restored is True
    assert state.state == 'restored'
    assert state.restored_at == now
    service.subscription_service.update_remnawave_user.assert_awaited_once_with(
        db,
        subscription,
        reset_traffic=True,
        reset_reason='grace period paid renewal',
        sync_squads=True,
    )


def test_expired_trial_can_be_restored_after_conversion_to_paid() -> None:
    now = datetime(2026, 7, 14, tzinfo=UTC)
    state = _state(now, state='active')
    converted_trial = SimpleNamespace(
        is_trial=False,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=now + timedelta(days=30),
    )

    assert GracePeriodService._is_paid_again(converted_trial, state, now) is True


async def test_webhook_guard_preserves_real_subscription_values(monkeypatch: pytest.MonkeyPatch) -> None:
    subscription = SimpleNamespace(
        id=10,
        user_id=20,
        status=SubscriptionStatus.EXPIRED.value,
        end_date=datetime(2026, 7, 14, tzinfo=UTC),
        traffic_limit_gb=100,
        last_webhook_update_at=None,
    )
    db = AsyncMock()
    preserve = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.grace_period_service.should_preserve_grace_subscription', preserve)
    service = RemnaWaveWebhookService.__new__(RemnaWaveWebhookService)

    guarded = await service._guard_grace_webhook(db, subscription, 'user.modified')

    assert guarded is True
    assert subscription.status == SubscriptionStatus.EXPIRED.value
    assert subscription.end_date == datetime(2026, 7, 14, tzinfo=UTC)
    assert subscription.traffic_limit_gb == 100
    assert subscription.last_webhook_update_at is not None
    db.commit.assert_awaited_once()
