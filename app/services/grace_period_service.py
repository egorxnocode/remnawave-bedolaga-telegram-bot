"""Expiry and exhausted-traffic rescue access ("Спасательный круг").

Bedolaga remains the source of truth for the subscription or trial end date. This
service only gives the matching Remnawave user a short, traffic-limited GRACE
window.  Webhook guards in ``remnawave_webhook_service`` prevent the temporary
panel values from being copied back into the commercial subscription row.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import GracePeriodState, Subscription, SubscriptionStatus, Tariff, User
from app.external.remnawave_api import TrafficLimitStrategy, UserStatus as RemnaWaveUserStatus
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)
_GB = 1024**3
_OPEN_STATES = ('activating', 'active', 'activation_failed', 'closing')
_EXPIRY_KIND = 'expiry'
_TRAFFIC_KIND = 'traffic'


@dataclass(slots=True)
class GracePeriodRunResult:
    entered: int = 0
    restored: int = 0
    closed: int = 0
    failed: int = 0


def _parse_test_user_scope(raw: str) -> set[int] | None:
    """Return allowed Telegram IDs; ``None`` means explicit global mode.

    Empty/invalid input is fail-closed and returns an empty set.  Global mode
    requires the literal value ``all`` so clearing the setting can never
    accidentally enable rescue access for everyone.
    """
    value = (raw or '').strip()
    if value.casefold() == 'all':
        return None
    if not value:
        return set()

    result: set[int] = set()
    for item in value.split(','):
        try:
            parsed = int(item.strip())
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            result.add(parsed)
    return result


def _eligible_tariff_names(raw: str) -> set[str]:
    return {item.strip().casefold() for item in (raw or '').split(',') if item.strip()}


def _squad_uuids(raw_squads) -> list[str]:
    result: list[str] = []
    for squad in raw_squads or []:
        value = squad.get('uuid') if isinstance(squad, dict) else squad
        if value:
            result.append(str(value))
    return result


def _traffic_cycle_limit_increased(commercial_limit_bytes: int, latest_state: GracePeriodState | None) -> bool:
    return latest_state is None or commercial_limit_bytes > latest_state.original_traffic_limit_bytes


async def get_open_grace_state(
    db: AsyncSession, subscription_id: int, *, kind: str | None = None
) -> GracePeriodState | None:
    conditions = [
        GracePeriodState.subscription_id == subscription_id,
        GracePeriodState.state.in_(_OPEN_STATES),
    ]
    if kind is not None:
        conditions.append(GracePeriodState.kind == kind)
    result = await db.execute(select(GracePeriodState).where(*conditions).order_by(GracePeriodState.id.desc()).limit(1))
    return result.scalar_one_or_none()


async def get_latest_grace_state(
    db: AsyncSession, subscription_id: int, *, kind: str | None = None
) -> GracePeriodState | None:
    conditions = [GracePeriodState.subscription_id == subscription_id]
    if kind is not None:
        conditions.append(GracePeriodState.kind == kind)
    result = await db.execute(select(GracePeriodState).where(*conditions).order_by(GracePeriodState.id.desc()).limit(1))
    return result.scalar_one_or_none()


async def should_preserve_grace_subscription(
    db: AsyncSession,
    subscription: Subscription,
) -> bool:
    """Whether a panel webhook must not mutate the commercial subscription.

    Open rescue cycles always use temporary panel values.  A terminal expired
    cycle also guards the final disable echo so the Bedolaga status remains
    ``expired`` instead of becoming ``disabled``.
    """
    if not (settings.GRACE_PERIOD_ENABLED or settings.TRAFFIC_GRACE_ENABLED):
        return False

    if await get_open_grace_state(db, subscription.id) is not None:
        return True
    state = await get_latest_grace_state(db, subscription.id, kind=_EXPIRY_KIND)
    if state is None:
        return False
    return bool(
        state.state == 'expired'
        and subscription.status == SubscriptionStatus.EXPIRED.value
        and subscription.end_date == state.real_end_date
    )


class GracePeriodService:
    def __init__(self) -> None:
        self.subscription_service = SubscriptionService()

    @staticmethod
    def is_configured() -> bool:
        scope = _parse_test_user_scope(settings.GRACE_PERIOD_TEST_USER_IDS)
        return bool(
            settings.GRACE_PERIOD_ENABLED
            and (settings.GRACE_PERIOD_SQUAD_UUID or '').strip()
            and settings.GRACE_PERIOD_DAYS > 0
            and settings.GRACE_PERIOD_TRAFFIC_GB > 0
            and settings.GRACE_PERIOD_CHECK_INTERVAL_SECONDS > 0
            and _eligible_tariff_names(settings.GRACE_PERIOD_ELIGIBLE_TARIFFS)
            and (scope is None or bool(scope))
        )

    @staticmethod
    def is_traffic_configured() -> bool:
        scope = _parse_test_user_scope(settings.TRAFFIC_GRACE_TEST_USER_IDS)
        return bool(
            settings.TRAFFIC_GRACE_ENABLED
            and (settings.TRAFFIC_GRACE_SQUAD_UUID or '').strip()
            and settings.TRAFFIC_GRACE_DAYS > 0
            and settings.TRAFFIC_GRACE_TRAFFIC_GB > 0
            and _eligible_tariff_names(settings.TRAFFIC_GRACE_ELIGIBLE_TARIFFS)
            and (scope is None or bool(scope))
        )

    async def process_once(self, db: AsyncSession, *, now: datetime | None = None) -> GracePeriodRunResult:
        result = GracePeriodRunResult()
        if not (self.is_configured() or self.is_traffic_configured()):
            return result

        current_time = now or datetime.now(UTC)
        if self.is_configured():
            await self._process_existing_states(db, current_time, result)
            await self._enter_new_states(db, current_time, result)
        if self.is_traffic_configured():
            await self._process_existing_traffic_states(db, current_time, result)
            await self._enter_new_traffic_states(db, current_time, result)
        return result

    async def _process_existing_states(
        self,
        db: AsyncSession,
        now: datetime,
        run_result: GracePeriodRunResult,
    ) -> None:
        states_result = await db.execute(
            select(GracePeriodState)
            .where(GracePeriodState.kind == _EXPIRY_KIND, GracePeriodState.state.in_(_OPEN_STATES))
            .order_by(GracePeriodState.id)
        )
        for state in states_result.scalars().all():
            subscription = await self._get_subscription(db, state.subscription_id)
            if subscription is None:
                state.state = 'closed'
                state.closed_at = now
                state.last_error = 'subscription missing'
                await db.commit()
                run_result.closed += 1
                continue

            paid_subscription = (
                subscription
                if self._is_paid_again(subscription, state, now)
                else await self._get_paid_replacement(db, state, now)
            )
            if paid_subscription is not None:
                if await self._restore_paid_access(db, paid_subscription, state, now):
                    run_result.restored += 1
                else:
                    run_result.failed += 1
                continue

            if state.state == 'closing':
                if await self._close_rescue_access(db, state, now, reason=state.last_error or 'closing retry'):
                    run_result.closed += 1
                else:
                    run_result.failed += 1
                continue

            if now >= state.grace_expires_at:
                if await self._close_rescue_access(db, state, now, reason='time window exhausted'):
                    run_result.closed += 1
                else:
                    run_result.failed += 1
                continue

            if state.state in ('activating', 'activation_failed'):
                if await self._activate_state(db, state):
                    run_result.entered += 1
                else:
                    run_result.failed += 1
                continue

            if await self._panel_rescue_is_exhausted(state):
                if await self._close_rescue_access(db, state, now, reason='traffic allowance exhausted'):
                    run_result.closed += 1
                else:
                    run_result.failed += 1

    async def _enter_new_states(
        self,
        db: AsyncSession,
        now: datetime,
        run_result: GracePeriodRunResult,
    ) -> None:
        scope = _parse_test_user_scope(settings.GRACE_PERIOD_TEST_USER_IDS)
        if scope == set():
            return

        tariff_names = _eligible_tariff_names(settings.GRACE_PERIOD_ELIGIBLE_TARIFFS)
        cutoff = now - timedelta(hours=max(1, settings.GRACE_PERIOD_ENTRY_MAX_AGE_HOURS))

        query = (
            select(Subscription)
            .join(Tariff, Subscription.tariff_id == Tariff.id)
            .join(User, Subscription.user_id == User.id)
            .options(selectinload(Subscription.tariff), selectinload(Subscription.user))
            .where(
                Subscription.status == SubscriptionStatus.EXPIRED.value,
                Subscription.end_date <= now,
                Subscription.end_date >= cutoff,
                Tariff.is_daily.is_(False),
            )
            .order_by(Subscription.end_date)
        )
        if scope is not None:
            query = query.where(User.telegram_id.in_(scope))

        subscriptions = (await db.execute(query)).scalars().unique().all()
        for subscription in subscriptions:
            if subscription.is_trial and not settings.GRACE_PERIOD_INCLUDE_TRIALS:
                continue
            tariff_name = (subscription.tariff.name if subscription.tariff else '').strip().casefold()
            if tariff_name not in tariff_names:
                continue
            if await self._cycle_exists(db, subscription):
                continue

            remnawave_uuid = subscription.remnawave_uuid or getattr(subscription.user, 'remnawave_uuid', None)
            if not remnawave_uuid:
                logger.warning(
                    'Grace period skipped: Remnawave UUID missing',
                    subscription_id=subscription.id,
                    user_id=subscription.user_id,
                )
                continue

            try:
                async with self.subscription_service.get_api_client() as api:
                    panel_user = await api.get_user_by_uuid(remnawave_uuid)
                if panel_user is None:
                    raise RuntimeError('Remnawave user not found')
            except Exception as exc:
                logger.warning(
                    'Grace period preflight failed',
                    subscription_id=subscription.id,
                    error=str(exc),
                )
                run_result.failed += 1
                continue

            rescue_limit = panel_user.used_traffic_bytes + settings.GRACE_PERIOD_TRAFFIC_GB * _GB
            state = GracePeriodState(
                subscription_id=subscription.id,
                user_id=subscription.user_id,
                remnawave_uuid=remnawave_uuid,
                kind=_EXPIRY_KIND,
                state='activating',
                real_end_date=subscription.end_date,
                grace_started_at=now,
                grace_expires_at=now + timedelta(days=settings.GRACE_PERIOD_DAYS),
                original_squads=_squad_uuids(panel_user.active_internal_squads),
                original_traffic_limit_bytes=panel_user.traffic_limit_bytes or 0,
                original_device_limit=panel_user.hwid_device_limit,
                rescue_traffic_limit_bytes=rescue_limit,
            )
            db.add(state)
            await db.commit()
            await db.refresh(state)

            if await self._activate_state(db, state):
                run_result.entered += 1
            else:
                run_result.failed += 1

    async def _process_existing_traffic_states(
        self,
        db: AsyncSession,
        now: datetime,
        run_result: GracePeriodRunResult,
    ) -> None:
        states_result = await db.execute(
            select(GracePeriodState)
            .where(GracePeriodState.kind == _TRAFFIC_KIND, GracePeriodState.state.in_(_OPEN_STATES))
            .order_by(GracePeriodState.id)
        )
        for state in states_result.scalars().all():
            subscription = await self._get_subscription(db, state.subscription_id)
            if subscription is None or subscription.end_date <= now:
                if await self._close_rescue_access(db, state, now, reason='subscription no longer active'):
                    run_result.closed += 1
                else:
                    run_result.failed += 1
                continue

            commercial_limit_bytes = max(0, int((subscription.traffic_limit_gb or 0) * _GB))
            if commercial_limit_bytes > state.original_traffic_limit_bytes:
                if await self._restore_traffic_access(db, subscription, state, now):
                    run_result.restored += 1
                else:
                    run_result.failed += 1
                continue

            if state.state == 'closing':
                if await self._close_rescue_access(db, state, now, reason=state.last_error or 'closing retry'):
                    run_result.closed += 1
                else:
                    run_result.failed += 1
                continue

            if now >= state.grace_expires_at:
                if await self._close_rescue_access(db, state, now, reason='traffic rescue time window exhausted'):
                    run_result.closed += 1
                else:
                    run_result.failed += 1
                continue

            if subscription.status != SubscriptionStatus.LIMITED.value:
                subscription.status = SubscriptionStatus.LIMITED.value
                await db.commit()

            if state.state in ('activating', 'activation_failed'):
                if await self._activate_state(db, state):
                    run_result.entered += 1
                else:
                    run_result.failed += 1
                continue

            if await self._panel_rescue_is_exhausted(state):
                if await self._close_rescue_access(db, state, now, reason='traffic rescue allowance exhausted'):
                    run_result.closed += 1
                else:
                    run_result.failed += 1

    async def _enter_new_traffic_states(
        self,
        db: AsyncSession,
        now: datetime,
        run_result: GracePeriodRunResult,
    ) -> None:
        scope = _parse_test_user_scope(settings.TRAFFIC_GRACE_TEST_USER_IDS)
        if scope == set():
            return

        tariff_names = _eligible_tariff_names(settings.TRAFFIC_GRACE_ELIGIBLE_TARIFFS)
        query = (
            select(Subscription)
            .join(Tariff, Subscription.tariff_id == Tariff.id)
            .join(User, Subscription.user_id == User.id)
            .options(selectinload(Subscription.tariff), selectinload(Subscription.user))
            .where(
                Subscription.status.in_((SubscriptionStatus.ACTIVE.value, SubscriptionStatus.LIMITED.value)),
                Subscription.is_trial.is_(False),
                Subscription.end_date > now,
                Subscription.traffic_limit_gb > 0,
                Tariff.is_daily.is_(False),
                Tariff.traffic_topup_enabled.is_(True),
            )
        )
        if scope is not None:
            query = query.where(User.telegram_id.in_(scope))

        subscriptions = (await db.execute(query)).scalars().unique().all()
        for subscription in subscriptions:
            tariff_name = (subscription.tariff.name if subscription.tariff else '').strip().casefold()
            if tariff_name not in tariff_names:
                continue
            if await get_open_grace_state(db, subscription.id, kind=_TRAFFIC_KIND) is not None:
                continue

            commercial_limit = int((subscription.traffic_limit_gb or 0) * _GB)
            latest_cycle = await get_latest_grace_state(db, subscription.id, kind=_TRAFFIC_KIND)
            if not _traffic_cycle_limit_increased(commercial_limit, latest_cycle):
                # The same exhausted commercial quota has already received its
                # one rescue allowance. A top-up must increase it before a new
                # traffic-rescue cycle can ever be granted.
                continue

            remnawave_uuid = subscription.remnawave_uuid or getattr(subscription.user, 'remnawave_uuid', None)
            if not remnawave_uuid:
                continue
            try:
                async with self.subscription_service.get_api_client() as api:
                    panel_user = await api.get_user_by_uuid(remnawave_uuid)
                if panel_user is None:
                    continue
            except Exception as exc:
                logger.warning('Traffic grace preflight failed', subscription_id=subscription.id, error=str(exc))
                run_result.failed += 1
                continue

            panel_limit = panel_user.traffic_limit_bytes or 0
            exhausted = panel_user.status == RemnaWaveUserStatus.LIMITED or (
                panel_limit > 0 and panel_user.used_traffic_bytes >= panel_limit
            )
            if not exhausted:
                continue

            state = GracePeriodState(
                subscription_id=subscription.id,
                user_id=subscription.user_id,
                remnawave_uuid=remnawave_uuid,
                kind=_TRAFFIC_KIND,
                state='activating',
                real_end_date=subscription.end_date,
                grace_started_at=now,
                grace_expires_at=now + timedelta(days=settings.TRAFFIC_GRACE_DAYS),
                original_squads=_squad_uuids(panel_user.active_internal_squads),
                original_traffic_limit_bytes=commercial_limit,
                original_device_limit=panel_user.hwid_device_limit,
                rescue_traffic_limit_bytes=panel_user.used_traffic_bytes + settings.TRAFFIC_GRACE_TRAFFIC_GB * _GB,
            )
            subscription.status = SubscriptionStatus.LIMITED.value
            db.add(state)
            await db.commit()
            await db.refresh(state)
            if await self._activate_state(db, state):
                run_result.entered += 1
            else:
                run_result.failed += 1

    async def _activate_state(self, db: AsyncSession, state: GracePeriodState) -> bool:
        configured_squad = (
            settings.TRAFFIC_GRACE_SQUAD_UUID if state.kind == _TRAFFIC_KIND else settings.GRACE_PERIOD_SQUAD_UUID
        )
        grace_squad_uuid = (configured_squad or '').strip()
        if not grace_squad_uuid:
            state.state = 'activation_failed'
            state.last_error = f'{state.kind} rescue squad UUID is empty'
            state.updated_at = datetime.now(UTC)
            await db.commit()
            return False

        try:
            async with self.subscription_service.get_api_client() as api:
                await api.update_user(
                    uuid=state.remnawave_uuid,
                    status=RemnaWaveUserStatus.ACTIVE,
                    expire_at=state.grace_expires_at,
                    traffic_limit_bytes=state.rescue_traffic_limit_bytes,
                    traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
                    active_internal_squads=[grace_squad_uuid],
                )
            state.state = 'active'
            state.last_error = None
            state.updated_at = datetime.now(UTC)
            await db.commit()
            logger.info(
                'Grace period activated',
                subscription_id=state.subscription_id,
                user_id=state.user_id,
                kind=state.kind,
                grace_expires_at=state.grace_expires_at,
            )
            return True
        except Exception as exc:
            state.state = 'activation_failed'
            state.last_error = str(exc)[:2000]
            state.updated_at = datetime.now(UTC)
            await db.commit()
            logger.error(
                'Grace period activation failed',
                subscription_id=state.subscription_id,
                error=str(exc),
            )
            return False

    async def _restore_paid_access(
        self,
        db: AsyncSession,
        subscription: Subscription,
        state: GracePeriodState,
        now: datetime,
    ) -> bool:
        try:
            restored_user = await self.subscription_service.update_remnawave_user(
                db,
                subscription,
                reset_traffic=True,
                reset_reason='grace period paid renewal',
                sync_squads=True,
            )
            if restored_user is None:
                raise RuntimeError('paid access sync returned no user')
            state.state = 'restored'
            state.restored_at = now
            state.closed_at = now
            state.last_error = None
            await db.commit()
            logger.info(
                'Grace period restored to paid access',
                subscription_id=subscription.id,
                user_id=subscription.user_id,
            )
            return True
        except Exception as exc:
            state.last_error = str(exc)[:2000]
            state.updated_at = now
            await db.commit()
            logger.error(
                'Grace period paid restore failed',
                subscription_id=subscription.id,
                error=str(exc),
            )
            return False

    async def _restore_traffic_access(
        self,
        db: AsyncSession,
        subscription: Subscription,
        state: GracePeriodState,
        now: datetime,
    ) -> bool:
        subscription.status = SubscriptionStatus.ACTIVE.value
        await db.commit()
        try:
            restored_user = await self.subscription_service.update_remnawave_user(
                db,
                subscription,
                reset_traffic=False,
                sync_squads=True,
            )
            if restored_user is None:
                raise RuntimeError('traffic top-up sync returned no user')
            state.state = 'restored'
            state.restored_at = now
            state.closed_at = now
            state.last_error = None
            await db.commit()
            return True
        except Exception as exc:
            subscription.status = SubscriptionStatus.LIMITED.value
            state.last_error = str(exc)[:2000]
            state.updated_at = now
            await db.commit()
            logger.error('Traffic grace restore failed', subscription_id=subscription.id, error=str(exc))
            return False

    async def _panel_rescue_is_exhausted(self, state: GracePeriodState) -> bool:
        try:
            async with self.subscription_service.get_api_client() as api:
                panel_user = await api.get_user_by_uuid(state.remnawave_uuid)
            if panel_user is None:
                return True
            return (
                panel_user.status in {RemnaWaveUserStatus.LIMITED, RemnaWaveUserStatus.EXPIRED}
                or panel_user.used_traffic_bytes >= state.rescue_traffic_limit_bytes
            )
        except Exception as exc:
            logger.warning(
                'Grace period panel check failed; keeping current state',
                subscription_id=state.subscription_id,
                error=str(exc),
            )
            return False

    async def _close_rescue_access(
        self,
        db: AsyncSession,
        state: GracePeriodState,
        now: datetime,
        *,
        reason: str,
    ) -> bool:
        state.state = 'closing'
        state.last_error = reason
        state.updated_at = now
        await db.commit()

        disabled = await self.subscription_service.disable_remnawave_user(state.remnawave_uuid)
        terminal_state = 'closed' if state.kind == _TRAFFIC_KIND else 'expired'
        state.state = terminal_state if disabled else 'closing'
        state.closed_at = now if disabled else None
        state.last_error = None if disabled else f'could not disable panel user: {reason}'
        state.updated_at = now
        await db.commit()
        logger.info(
            'Grace period closed',
            subscription_id=state.subscription_id,
            user_id=state.user_id,
            reason=reason,
            disabled=disabled,
        )
        return disabled

    @staticmethod
    async def _get_subscription(db: AsyncSession, subscription_id: int) -> Subscription | None:
        result = await db.execute(
            select(Subscription)
            .options(selectinload(Subscription.tariff), selectinload(Subscription.user))
            .where(Subscription.id == subscription_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _get_paid_replacement(
        db: AsyncSession,
        state: GracePeriodState,
        now: datetime,
    ) -> Subscription | None:
        """Find a new paid row created while an expired trial is in GRACE."""
        result = await db.execute(
            select(Subscription)
            .options(selectinload(Subscription.tariff), selectinload(Subscription.user))
            .where(
                Subscription.user_id == state.user_id,
                Subscription.id != state.subscription_id,
                Subscription.is_trial.is_(False),
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.end_date > now,
            )
            .order_by(Subscription.end_date.desc(), Subscription.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _is_paid_again(
        subscription: Subscription,
        state: GracePeriodState,
        now: datetime,
    ) -> bool:
        return bool(
            subscription.status == SubscriptionStatus.ACTIVE.value
            and subscription.end_date > now
            and subscription.end_date > state.real_end_date
        )

    @staticmethod
    async def _cycle_exists(db: AsyncSession, subscription: Subscription) -> bool:
        result = await db.execute(
            select(GracePeriodState.id)
            .where(
                GracePeriodState.subscription_id == subscription.id,
                GracePeriodState.kind == _EXPIRY_KIND,
                GracePeriodState.real_end_date == subscription.end_date,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


class GracePeriodScheduler:
    def __init__(self) -> None:
        self.is_running = False
        self.service = GracePeriodService()

    def is_enabled(self) -> bool:
        return self.service.is_configured() or self.service.is_traffic_configured()

    async def start_monitoring(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        logger.info(
            'Grace period scheduler started',
            interval_seconds=settings.GRACE_PERIOD_CHECK_INTERVAL_SECONDS,
        )
        while self.is_running:
            try:
                async with AsyncSessionLocal() as db:
                    result = await self.service.process_once(db)
                    if result.entered or result.restored or result.closed or result.failed:
                        logger.info(
                            'Grace period cycle completed',
                            entered=result.entered,
                            restored=result.restored,
                            closed=result.closed,
                            failed=result.failed,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception('Grace period scheduler cycle failed', error=str(exc))
            await asyncio.sleep(max(15, settings.GRACE_PERIOD_CHECK_INTERVAL_SECONDS))

    def stop_monitoring(self) -> None:
        self.is_running = False


grace_period_scheduler = GracePeriodScheduler()
