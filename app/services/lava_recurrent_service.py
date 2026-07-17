"""Lava recurrent subscriptions: lifecycle API and idempotent callbacks."""

from __future__ import annotations

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
from app.database.crud.transaction import create_transaction, emit_transaction_side_effects
from app.database.models import (
    LavaRecurrentConsumer,
    LavaRecurrentEvent,
    LavaRecurrentSubscription,
    PaymentMethod,
    Subscription,
    Tariff,
    Transaction,
    TransactionType,
    User,
)
from app.services.lava_service import LavaAPIError, lava_service


logger = structlog.get_logger(__name__)
OPEN_STATUSES = {'created', 'activated', 'suspended', 'cancel_requested'}
SETUP_WINDOW_DAYS = 3


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
    subscription: Subscription,
    tariff: Tariff,
    period_days: int,
    email: str,
    consent_ip: str | None = None,
    consent_user_agent: str | None = None,
) -> tuple[LavaRecurrentSubscription, str]:
    if not settings.is_lava_recurrent_enabled():
        raise ValueError('Lava recurrent payments are disabled')
    locked_subscription = (
        await db.execute(
            select(Subscription)
            .where(Subscription.id == subscription.id, Subscription.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked_subscription is None:
        raise ValueError('Subscription not found')
    product_id = configured_product_id(tariff.name, period_days)
    if not product_id:
        raise ValueError('No Lava recurrent product configured for this tariff period')
    amount_kopeks = tariff.get_price_for_period(period_days)
    if amount_kopeks is None or int(amount_kopeks) <= 0:
        raise ValueError('Tariff period is not available')
    if await get_current_recurrent_subscription(db, subscription_id=subscription.id, user_id=user.id):
        raise ValueError('Lava recurrent subscription already exists')
    if subscription.end_date and subscription.end_date > datetime.now(UTC):
        seconds_left = (subscription.end_date - datetime.now(UTC)).total_seconds()
        if seconds_left > SETUP_WINDOW_DAYS * 86400:
            raise ValueError('Lava recurrent setup is available during the last 3 days of the subscription')

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
        subscription_id=subscription.id,
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
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record, str(payment_url)


async def cancel_recurrent_subscription(
    db: AsyncSession,
    record: LavaRecurrentSubscription,
) -> LavaRecurrentSubscription:
    response = await lava_service.unsubscribe_recurrent_subscription(
        subscription_id=record.lava_subscription_id,
        order_id=None if record.lava_subscription_id else record.order_id,
    )
    data = response.get('data') or response
    record.status = 'deactivated' if data.get('unsubscribed') is True else 'cancel_requested'
    record.is_active = False
    record.deactivated_at = datetime.now(UTC) if record.status == 'deactivated' else None
    subscription = await db.get(Subscription, record.subscription_id) if record.subscription_id else None
    if subscription:
        subscription.autopay_enabled = False
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

        external_id = f'lava-recurrent:{invoice_id}'
        transaction = (
            await db.execute(
                select(Transaction).where(
                    Transaction.external_id == external_id,
                    Transaction.payment_method == PaymentMethod.LAVA.value,
                )
            )
        ).scalar_one_or_none()
        if transaction is None:
            locked_user = (
                await db.execute(select(User).where(User.id == record.user_id).with_for_update())
            ).scalar_one()
            locked_user.balance_kopeks += record.amount_kopeks
            locked_user.updated_at = datetime.now(UTC)
            transaction = await create_transaction(
                db,
                user_id=record.user_id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=record.amount_kopeks,
                description='Рекуррентное пополнение через Lava',
                payment_method=PaymentMethod.LAVA,
                external_id=external_id,
                commit=False,
            )
        subscription = await db.get(Subscription, record.subscription_id) if record.subscription_id else None
        if subscription:
            subscription.autopay_enabled = True
            subscription.autopay_period_days = record.period_days
        record.status = 'activated'
        record.is_active = True
        record.last_invoice_id = invoice_id
        record.payer_details = str(payload.get('payer_details') or '') or None
        record.next_pay_at = _parse_datetime(payload.get('next_pay_time'))
        record.activated_at = _parse_datetime(payload.get('activation_time')) or datetime.now(UTC)
        event.transaction_id = transaction.id
        event.outcome = 'balance_credited'
        event.processed_at = datetime.now(UTC)
        await db.commit()
        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=record.amount_kopeks,
            user_id=record.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.LAVA,
            external_id=external_id,
            description='Рекуррентное пополнение через Lava',
        )
        return True

    if incoming_status == 'suspended':
        record.status = 'suspended'
        record.is_active = True
        record.last_invoice_id = str(payload.get('invoice_id') or '') or record.last_invoice_id
        record.next_pay_at = _parse_datetime(payload.get('next_pay_time'))
        record.suspended_at = _parse_datetime(payload.get('suspension_time')) or datetime.now(UTC)
        event.outcome = 'suspended'
    else:
        record.status = 'deactivated'
        record.is_active = False
        record.deactivated_at = _parse_datetime(payload.get('deactivation_time')) or datetime.now(UTC)
        record.deactivated_reason = str(payload.get('deactivated_reason') or '') or None
        subscription = await db.get(Subscription, record.subscription_id) if record.subscription_id else None
        if subscription:
            subscription.autopay_enabled = False
        event.outcome = 'deactivated'
    event.processed_at = datetime.now(UTC)
    await db.commit()
    return True
