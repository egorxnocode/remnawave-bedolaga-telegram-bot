"""Lava recurrent subscriptions: lifecycle API and idempotent callbacks."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import (
    LavaRecurrentConsumer,
    LavaRecurrentEvent,
    LavaRecurrentSubscription,
    LavaServiceOrder,
    Subscription,
    Tariff,
    User,
)
from app.services.lava_order_service import (
    create_recurrent_renewal_order,
    emit_lava_service_order_side_effects,
    fulfill_lava_service_order,
)
from app.services.lava_service import LavaAPIError, lava_service


logger = structlog.get_logger(__name__)
OPEN_STATUSES = {'created', 'activated', 'suspended', 'cancel_requested'}


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace('Z', '+00:00')
    for candidate in (text, text.replace(' ', 'T')):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _event_key(payload: dict[str, Any]) -> str:
    status = str(payload.get('status') or '').lower()
    provider_subscription_id = str(payload.get('subscription_id') or '')
    invoice_id = str(payload.get('invoice_id') or '')
    event_time = str(payload.get('suspension_time') or payload.get('deactivation_time') or '')
    if provider_subscription_id and invoice_id:
        raw = f'{status}:{provider_subscription_id}:{invoice_id}'
    elif provider_subscription_id and event_time:
        raw = f'{status}:{provider_subscription_id}:{event_time}'
    else:
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def configured_product_id(tariff_name: str, period_days: int) -> str | None:
    return settings.get_lava_recurrent_product_map().get((tariff_name.strip(), int(period_days)))


async def get_current_recurrent_subscription(
    db: AsyncSession,
    *,
    subscription_id: int,
    user_id: int | None = None,
) -> LavaRecurrentSubscription | None:
    query = select(LavaRecurrentSubscription).where(
        LavaRecurrentSubscription.subscription_id == subscription_id,
        LavaRecurrentSubscription.status.in_(OPEN_STATUSES),
    )
    if user_id is not None:
        query = query.where(LavaRecurrentSubscription.user_id == user_id)
    result = await db.execute(query.order_by(LavaRecurrentSubscription.created_at.desc()).limit(1))
    return result.scalar_one_or_none()


async def start_recurrent_subscription(
    db: AsyncSession,
    *,
    user: User,
    subscription: Subscription | None,
    tariff: Tariff,
    service_order: LavaServiceOrder,
    period_days: int,
    email: str,
    consent_ip: str | None = None,
    consent_user_agent: str | None = None,
) -> tuple[LavaRecurrentSubscription, str]:
    if not settings.is_lava_recurrent_enabled_for_user(user.telegram_id):
        raise ValueError('Lava recurrent payments are disabled')
    locked_subscription = None
    if subscription is not None:
        locked_subscription = (
            await db.execute(
                select(Subscription)
                .where(Subscription.id == subscription.id, Subscription.user_id == user.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked_subscription is None:
            raise ValueError('Subscription not found')
    else:
        # Serialize fresh multi-tariff checkouts. Without this lock, a double
        # click could create two provider subscriptions before either local
        # binding became visible.
        await db.execute(select(User).where(User.id == user.id).with_for_update())
    if tariff.is_daily:
        raise ValueError('Daily tariffs are not eligible for Lava recurrent')
    product_id = configured_product_id(tariff.name, period_days)
    if not product_id:
        raise ValueError('No Lava recurrent product configured for this tariff period')
    amount_kopeks = tariff.get_price_for_period(period_days)
    if amount_kopeks is None or int(amount_kopeks) <= 0:
        raise ValueError('Tariff period is not available')
    if locked_subscription and await get_current_recurrent_subscription(
        db, subscription_id=locked_subscription.id, user_id=user.id
    ):
        raise ValueError('Lava recurrent subscription already exists')
    if locked_subscription is None:
        fresh_binding = (
            await db.execute(
                select(LavaRecurrentSubscription)
                .where(
                    LavaRecurrentSubscription.user_id == user.id,
                    LavaRecurrentSubscription.tariff_id == tariff.id,
                    LavaRecurrentSubscription.subscription_id.is_(None),
                    LavaRecurrentSubscription.status.in_(OPEN_STATUSES),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if fresh_binding is not None:
            raise ValueError('Lava recurrent checkout for this tariff is already pending')
    consumer = (
        await db.execute(select(LavaRecurrentConsumer).where(LavaRecurrentConsumer.user_id == user.id).limit(1))
    ).scalar_one_or_none()
    if consumer is not None and consumer.email.casefold() != email.casefold():
        raise ValueError('Email for an existing Lava recurrent consumer cannot be changed')
    consumer_id = consumer.consumer_id if consumer else f'bedolaga-user-{user.id}'
    consumer_created = consumer is None
    if consumer_created:
        display_name = ' '.join(filter(None, [user.first_name, user.last_name])) or user.username or f'User {user.id}'
        await lava_service.create_recurrent_consumer(
            consumer_id=consumer_id,
            email=email,
            name=display_name[:255],
        )
        db.add(LavaRecurrentConsumer(user_id=user.id, consumer_id=consumer_id, email=email))
        await db.flush()

    order_id = f'lavarec{user.id}_{uuid4().hex[:24]}'
    try:
        response = await lava_service.create_recurrent_subscription(
            consumer_id=consumer_id,
            order_id=order_id,
            product_id=product_id,
        )
    except Exception:
        if consumer_created:
            # Provider already accepted the consumer. Persist it so a retry does
            # not attempt to create the same immutable consumer again.
            await db.commit()
        raise
    data = response.get('data') or response
    provider_subscription_id = data.get('subscriptionId') or data.get('subscription_id')
    payment_url = data.get('url') or data.get('payment_url')
    if not provider_subscription_id or not payment_url:
        raise LavaAPIError(200, 'Lava recurrent response is missing subscriptionId or url')
    provider_amount = data.get('amount')
    if provider_amount is not None:
        try:
            provider_amount_kopeks = int((Decimal(str(provider_amount)) * 100).quantize(Decimal(1)))
        except (InvalidOperation, ValueError):
            raise LavaAPIError(200, 'Lava recurrent product amount has invalid format') from None
        if provider_amount_kopeks != int(amount_kopeks):
            raise LavaAPIError(200, 'Lava recurrent product amount does not match tariff price')

    record = LavaRecurrentSubscription(
        user_id=user.id,
        subscription_id=locked_subscription.id if locked_subscription else None,
        tariff_id=tariff.id,
        product_id=product_id,
        consumer_id=consumer_id,
        order_id=order_id,
        lava_subscription_id=str(provider_subscription_id),
        payment_url=str(payment_url),
        period_days=int(period_days),
        amount_kopeks=int(amount_kopeks),
        email=email,
        consent_at=datetime.now(UTC),
        consent_ip=consent_ip,
        consent_user_agent=consent_user_agent[:512] if consent_user_agent else None,
        status='created',
        is_active=False,
        terms_snapshot=dict(service_order.snapshot or {}),
    )
    db.add(record)
    await db.flush()
    service_order.recurrent_subscription_id = record.id
    service_order.provider_order_id = order_id
    service_order.status = 'pending'
    await db.commit()
    await db.refresh(record)
    return record, str(payment_url)


async def cancel_recurrent_subscription(
    db: AsyncSession,
    record: LavaRecurrentSubscription,
) -> LavaRecurrentSubscription:
    if record.status == 'deactivated':
        return record
    record.status = 'cancel_requested'
    record.is_active = False
    record.updated_at = datetime.now(UTC)
    await db.commit()
    return await confirm_recurrent_cancellation(db, record)


async def _cancel_unpaid_recurrent_order(
    db: AsyncSession,
    recurrent_id: int,
) -> None:
    unpaid_order = (
        await db.execute(
            select(LavaServiceOrder)
            .where(
                LavaServiceOrder.recurrent_subscription_id == recurrent_id,
                LavaServiceOrder.status.in_({'created', 'pending'}),
                LavaServiceOrder.paid_at.is_(None),
            )
            .with_for_update()
            .limit(1)
        )
    ).scalar_one_or_none()
    if unpaid_order is not None:
        unpaid_order.status = 'cancelled'
        unpaid_order.failure_reason = 'Recurrent checkout deactivated before payment'
        unpaid_order.updated_at = datetime.now(UTC)


async def confirm_recurrent_cancellation(
    db: AsyncSession,
    record: LavaRecurrentSubscription,
) -> LavaRecurrentSubscription:
    """Confirm a persisted user cancellation with Lava.

    The caller must leave ``cancel_requested`` in place on provider errors so
    the background reconciler can retry without losing the user's instruction.
    """
    identifier = {
        'subscription_id': record.lava_subscription_id,
        'order_id': None if record.lava_subscription_id else record.order_id,
    }
    unsubscribe_error: LavaAPIError | None = None
    try:
        response = await lava_service.unsubscribe_recurrent_subscription(**identifier)
        data = response.get('data') or response
        confirmed = data.get('unsubscribed') is True
    except LavaAPIError as error:
        # The provider may already have applied the operation while its HTTP
        # response was lost. Status is the authoritative fallback.
        unsubscribe_error = error
        confirmed = False

    if not confirmed:
        status_response = await lava_service.get_recurrent_subscription_status(**identifier)
        status_data = status_response.get('data') or status_response
        provider_status = str(status_data.get('status') or status_data.get('subscriptionStatus') or '').strip().lower()
        confirmed = provider_status == 'deactivated'
        if not confirmed and unsubscribe_error is not None:
            raise unsubscribe_error

    record.status = 'deactivated' if confirmed else 'cancel_requested'
    record.is_active = False
    record.deactivated_at = datetime.now(UTC) if record.status == 'deactivated' else None
    if record.status == 'deactivated':
        await _cancel_unpaid_recurrent_order(db, record.id)
    await db.commit()
    await db.refresh(record)
    return record


async def process_lava_recurrent_callback(db: AsyncSession, payload: dict[str, Any]) -> bool:
    if str(payload.get('type') or '') != '4':
        return False
    order_id = str(payload.get('order_id') or '')
    incoming_status = str(payload.get('status') or '').strip().lower()
    if not order_id or incoming_status not in {'activated', 'suspended', 'deactivated'}:
        logger.warning('Lava recurrent webhook: invalid required fields', order_id=order_id, status=incoming_status)
        return False

    result = await db.execute(
        select(LavaRecurrentSubscription)
        .where(LavaRecurrentSubscription.order_id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    record = result.scalar_one_or_none()
    if not record:
        logger.warning('Lava recurrent webhook: subscription not found', order_id=order_id)
        return False
    cancellation_pending = getattr(record, 'status', 'created') in {'cancel_requested', 'deactivated'}
    if str(payload.get('product_id') or '') != record.product_id:
        logger.error('Lava recurrent webhook: product mismatch', order_id=order_id)
        return False
    if str(payload.get('consumer_id') or '') != record.consumer_id:
        logger.error('Lava recurrent webhook: consumer mismatch', order_id=order_id)
        return False

    provider_subscription_id = str(payload.get('subscription_id') or '')
    if not provider_subscription_id:
        return False
    if record.lava_subscription_id and record.lava_subscription_id != provider_subscription_id:
        logger.error('Lava recurrent webhook: subscription ID mismatch', order_id=order_id)
        return False
    record.lava_subscription_id = provider_subscription_id

    key = _event_key(payload)
    existing_event = (
        await db.execute(select(LavaRecurrentEvent).where(LavaRecurrentEvent.event_key == key))
    ).scalar_one_or_none()
    if existing_event:
        logger.info('Lava recurrent webhook: event already processed', order_id=order_id, status=incoming_status)
        return True

    event = LavaRecurrentEvent(
        recurrent_subscription_id=record.id,
        event_key=key,
        status=incoming_status,
        invoice_id=str(payload.get('invoice_id') or '') or None,
        payload=payload,
    )
    db.add(event)
    record.callback_payload = payload
    record.updated_at = datetime.now(UTC)

    if incoming_status == 'activated':
        invoice_id = str(payload.get('invoice_id') or '')
        amount = payload.get('amount')
        try:
            callback_amount_kopeks = int((Decimal(str(amount)) * 100).quantize(Decimal(1)))
        except (InvalidOperation, ValueError):
            callback_amount_kopeks = -1
        if not invoice_id or amount is None or callback_amount_kopeks != record.amount_kopeks:
            # Do not persist an idempotency marker for a rejected event. Lava
            # must retry, and a corrected redelivery of the same invoice must
            # still be processable.
            await db.rollback()
            logger.error('Lava recurrent webhook: amount or invoice mismatch', order_id=order_id)
            return False

        initial_order = (
            await db.execute(
                select(LavaServiceOrder)
                .where(
                    LavaServiceOrder.recurrent_subscription_id == record.id,
                    LavaServiceOrder.status.in_({'created', 'pending', 'fulfilling', 'cancelled'}),
                )
                .order_by(LavaServiceOrder.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        service_order = initial_order or await create_recurrent_renewal_order(
            db, record, provider_invoice_id=invoice_id
        )
        if getattr(service_order, 'status', 'pending') == 'cancelled':
            # A signed provider callback proves that the initial payment won
            # the race with cancellation. Fulfil the paid period exactly once,
            # but keep future charges scheduled for deactivation below.
            service_order.status = 'pending'
        service_order.provider_invoice_id = invoice_id
        await db.flush()
        fulfilled, service_order = await fulfill_lava_service_order(
            db,
            order_id=service_order.id,
            provider_invoice_id=invoice_id,
            commit=False,
        )
        if not fulfilled or service_order is None:
            await db.rollback()
            logger.error('Lava recurrent webhook: service order fulfillment failed', order_id=order_id)
            return False
        record.status = 'cancel_requested' if cancellation_pending else 'activated'
        record.is_active = not cancellation_pending
        record.last_invoice_id = invoice_id
        record.payer_details = str(payload.get('payer_details') or '') or None
        record.next_pay_at = _parse_datetime(payload.get('next_pay_time'))
        record.activated_at = _parse_datetime(payload.get('activation_time')) or datetime.now(UTC)
        event.transaction_id = service_order.transaction_id
        event.outcome = 'service_order_fulfilled'
        event.processed_at = datetime.now(UTC)
        await db.commit()
        await emit_lava_service_order_side_effects(db, service_order)
        if cancellation_pending:
            try:
                await confirm_recurrent_cancellation(db, record)
            except LavaAPIError as error:
                logger.warning(
                    'Lava recurrent cancellation remains pending after paid callback',
                    recurrent_id=record.id,
                    error=error.message,
                )
        return True

    retry_cancellation = False
    if incoming_status == 'suspended':
        record.status = 'cancel_requested' if cancellation_pending else 'suspended'
        record.is_active = not cancellation_pending
        record.last_invoice_id = str(payload.get('invoice_id') or '') or record.last_invoice_id
        record.next_pay_at = _parse_datetime(payload.get('next_pay_time'))
        record.suspended_at = _parse_datetime(payload.get('suspension_time')) or datetime.now(UTC)
        event.outcome = 'suspended_cancellation_pending' if cancellation_pending else 'suspended'
        retry_cancellation = cancellation_pending
    else:
        record.status = 'deactivated'
        record.is_active = False
        record.deactivated_at = _parse_datetime(payload.get('deactivation_time')) or datetime.now(UTC)
        record.deactivated_reason = str(payload.get('deactivated_reason') or '') or None
        event.outcome = 'deactivated'
        await _cancel_unpaid_recurrent_order(db, record.id)
    event.processed_at = datetime.now(UTC)
    await db.commit()
    if retry_cancellation:
        try:
            await confirm_recurrent_cancellation(db, record)
        except LavaAPIError as error:
            logger.warning(
                'Lava recurrent cancellation remains pending after suspended callback',
                recurrent_id=record.id,
                error=error.message,
            )
    return True


class LavaRecurrentCancellationReconciler:
    """Retry user-requested provider cancellations until Lava confirms them."""

    def __init__(self, interval_seconds: int = 300) -> None:
        self.interval_seconds = interval_seconds
        self._running = False

    async def reconcile_once(self) -> int:
        confirmed = 0
        async with AsyncSessionLocal() as db:
            record_ids = (
                (
                    await db.execute(
                        select(LavaRecurrentSubscription.id)
                        .where(LavaRecurrentSubscription.status == 'cancel_requested')
                        .order_by(LavaRecurrentSubscription.updated_at.asc())
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            for record_id in record_ids:
                record = await db.get(LavaRecurrentSubscription, record_id)
                if record is None or record.status != 'cancel_requested':
                    continue
                try:
                    updated = await confirm_recurrent_cancellation(db, record)
                    confirmed += int(updated.status == 'deactivated')
                except LavaAPIError as error:
                    await db.rollback()
                    logger.warning(
                        'Lava recurrent cancellation retry failed',
                        recurrent_id=record_id,
                        error=error.message,
                    )
        return confirmed

    async def start(self) -> None:
        self._running = True
        logger.info('Lava recurrent cancellation reconciler started', interval_seconds=self.interval_seconds)
        while self._running:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception('Lava recurrent cancellation reconciliation failed', error=str(error))
            await asyncio.sleep(self.interval_seconds)

    def stop(self) -> None:
        self._running = False


lava_recurrent_cancellation_reconciler = LavaRecurrentCancellationReconciler()
