"""Direct-to-service Lava orders and idempotent fulfilment.

New real-money payments must go through this ledger.  Legacy balance top-ups
remain readable for backwards compatibility, but this module never reads or
writes ``User.balance_kopeks``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.subscription import (
    add_subscription_traffic,
    create_paid_subscription,
    extend_subscription,
)
from app.database.crud.transaction import create_transaction, emit_transaction_side_effects
from app.database.models import (
    LavaRecurrentSubscription,
    LavaServiceOrder,
    PaymentMethod,
    Subscription,
    Tariff,
    Transaction,
    TransactionType,
    User,
)


logger = structlog.get_logger(__name__)
ORDER_KINDS = {'tariff', 'daily', 'traffic', 'devices'}
OPEN_ORDER_STATUSES = {'created', 'pending', 'fulfilling'}


def build_service_order_dedup_key(
    *,
    user_id: int,
    kind: str,
    payment_mode: str,
    amount_kopeks: int,
    snapshot: dict[str, Any],
    subscription_id: int | None,
    tariff_id: int | None,
) -> str:
    payload = {
        'user_id': int(user_id),
        'kind': kind,
        'payment_mode': payment_mode,
        'amount_kopeks': int(amount_kopeks),
        'snapshot': snapshot,
        'subscription_id': subscription_id,
        'tariff_id': tariff_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


async def create_service_order(
    db: AsyncSession,
    *,
    user_id: int,
    kind: str,
    payment_mode: str,
    amount_kopeks: int,
    description: str,
    snapshot: dict[str, Any],
    subscription_id: int | None = None,
    tariff_id: int | None = None,
) -> tuple[LavaServiceOrder, bool]:
    if kind not in ORDER_KINDS:
        raise ValueError('Unsupported Lava service order kind')
    if payment_mode not in {'one_time', 'recurrent'}:
        raise ValueError('Unsupported Lava payment mode')
    if amount_kopeks <= 0:
        raise ValueError('Lava service order amount must be positive')
    dedup_key = build_service_order_dedup_key(
        user_id=user_id,
        kind=kind,
        payment_mode=payment_mode,
        amount_kopeks=amount_kopeks,
        snapshot=snapshot,
        subscription_id=subscription_id,
        tariff_id=tariff_id,
    )
    existing = (
        await db.execute(
            select(LavaServiceOrder).where(
                LavaServiceOrder.dedup_key == dedup_key,
                LavaServiceOrder.status.in_(OPEN_ORDER_STATUSES),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    order = LavaServiceOrder(
        user_id=user_id,
        subscription_id=subscription_id,
        tariff_id=tariff_id,
        kind=kind,
        payment_mode=payment_mode,
        status='created',
        amount_kopeks=int(amount_kopeks),
        currency='RUB',
        description=description,
        snapshot=dict(snapshot),
        dedup_key=dedup_key,
    )
    db.add(order)
    try:
        await db.commit()
    except IntegrityError:
        # The partial unique index closes the double-click race between two
        # simultaneous Cabinet requests. Return the winner's open order.
        await db.rollback()
        existing = (
            await db.execute(
                select(LavaServiceOrder).where(
                    LavaServiceOrder.dedup_key == dedup_key,
                    LavaServiceOrder.status.in_(OPEN_ORDER_STATUSES),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing, False
    await db.refresh(order)
    return order, True


async def get_service_order_payment_url(db: AsyncSession, order: LavaServiceOrder) -> str | None:
    """Return a still-payable provider URL for a reusable open order."""
    if order.payment_mode == 'recurrent' and order.recurrent_subscription_id:
        record = await db.get(LavaRecurrentSubscription, order.recurrent_subscription_id)
        if record is not None and record.status == 'created' and record.payment_url:
            return str(record.payment_url)
        return None

    if order.payment_mode != 'one_time' or not order.provider_order_id:
        return None
    from app.database.models import LavaPayment

    payment = (
        await db.execute(select(LavaPayment).where(LavaPayment.order_id == order.provider_order_id))
    ).scalar_one_or_none()
    if payment is None or payment.is_paid or payment.status not in {'created', 'pending', 'processing'}:
        return None
    if payment.expires_at and payment.expires_at <= datetime.now(UTC):
        payment.status = 'expired'
        payment.updated_at = datetime.now(UTC)
        order.status = 'failed'
        order.failure_reason = 'Lava invoice expired before checkout retry'
        order.updated_at = datetime.now(UTC)
        await db.commit()
        return None
    return str(payment.payment_url) if payment.payment_url else None


async def attach_provider_payment(
    db: AsyncSession,
    order: LavaServiceOrder,
    *,
    provider_order_id: str,
    provider_invoice_id: str | None = None,
    recurrent_subscription_id: int | None = None,
) -> None:
    order.provider_order_id = provider_order_id
    order.provider_invoice_id = provider_invoice_id
    order.recurrent_subscription_id = recurrent_subscription_id
    order.status = 'pending'
    order.updated_at = datetime.now(UTC)
    await db.commit()


async def _resolve_tariff_subscription(
    db: AsyncSession,
    order: LavaServiceOrder,
    snapshot: dict[str, Any],
) -> Subscription:
    subscription = None
    if order.subscription_id:
        subscription = (
            await db.execute(
                select(Subscription)
                .where(Subscription.id == order.subscription_id, Subscription.user_id == order.user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    # An order created for a fresh catalog purchase may be paid after another
    # request created the same tariff. Reuse that row instead of issuing a
    # second subscription.
    if subscription is None and order.tariff_id:
        subscription = (
            await db.execute(
                select(Subscription)
                .where(Subscription.user_id == order.user_id, Subscription.tariff_id == order.tariff_id)
                .order_by(Subscription.created_at.desc())
                .with_for_update()
                .limit(1)
            )
        ).scalar_one_or_none()

    if subscription is not None:
        subscription = await extend_subscription(
            db,
            subscription,
            int(snapshot['period_days']),
            tariff_id=order.tariff_id,
            traffic_limit_gb=int(snapshot['traffic_limit_gb']),
            device_limit=int(snapshot['device_limit']),
            connected_squads=list(snapshot.get('connected_squads') or []),
            commit=False,
        )
    else:
        subscription = await create_paid_subscription(
            db,
            user_id=order.user_id,
            duration_days=int(snapshot['period_days']),
            traffic_limit_gb=int(snapshot['traffic_limit_gb']),
            device_limit=int(snapshot['device_limit']),
            connected_squads=list(snapshot.get('connected_squads') or []),
            tariff_id=order.tariff_id,
            commit=False,
        )

    subscription.autopay_enabled = False
    subscription.autopay_period_days = None
    if order.kind == 'daily':
        subscription.last_daily_charge_at = datetime.now(UTC)
        subscription.is_daily_paused = False
    order.subscription_id = subscription.id
    return subscription


async def fulfill_lava_service_order(
    db: AsyncSession,
    *,
    order_id: int,
    provider_invoice_id: str,
    commit: bool = True,
) -> tuple[bool, LavaServiceOrder | None]:
    """Fulfil an order exactly once.

    A duplicate callback sees ``fulfilled`` under the row lock and returns
    success without touching the subscription for a second time.
    """
    order = (
        await db.execute(
            select(LavaServiceOrder)
            .where(LavaServiceOrder.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if order is None:
        logger.error('Lava service order not found', order_id=order_id)
        return False, None
    if order.status == 'fulfilled':
        return True, order
    if order.status in {'cancelled', 'failed'}:
        logger.error('Lava service order is terminal', order_id=order.id, status=order.status)
        return False, order
    if order.provider_invoice_id and order.provider_invoice_id != provider_invoice_id:
        logger.error('Lava service order invoice mismatch', order_id=order.id)
        return False, order

    order.provider_invoice_id = provider_invoice_id
    order.status = 'fulfilling'
    order.paid_at = order.paid_at or datetime.now(UTC)
    snapshot = dict(order.snapshot or {})

    user = (
        await db.execute(select(User).where(User.id == order.user_id).with_for_update())
    ).scalar_one_or_none()
    if user is None:
        await db.rollback()
        return False, order

    subscription: Subscription | None = None
    if order.kind in {'tariff', 'daily'}:
        subscription = await _resolve_tariff_subscription(db, order, snapshot)
        user.has_had_paid_subscription = True
        user.updated_at = datetime.now(UTC)
    elif order.kind == 'traffic':
        subscription = (
            await db.execute(
                select(Subscription)
                .where(Subscription.id == order.subscription_id, Subscription.user_id == order.user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if subscription is None:
            await db.rollback()
            return False, order
        await add_subscription_traffic(db, subscription, int(snapshot['traffic_gb']), commit=False)
        if subscription.status in {'expired', 'disabled', 'limited'}:
            subscription.status = 'active'
    else:
        subscription = (
            await db.execute(
                select(Subscription)
                .where(Subscription.id == order.subscription_id, Subscription.user_id == order.user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if subscription is None:
            await db.rollback()
            return False, order
        subscription.device_limit = (subscription.device_limit or 1) + int(snapshot['devices'])
        subscription.updated_at = datetime.now(UTC)

    external_id = f'lava-service:{provider_invoice_id}'
    transaction = await create_transaction(
        db,
        user_id=order.user_id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=order.amount_kopeks,
        description=order.description,
        payment_method=PaymentMethod.LAVA,
        external_id=external_id,
        commit=False,
    )
    order.transaction_id = transaction.id
    order.status = 'fulfilled'
    order.fulfilled_at = datetime.now(UTC)
    order.updated_at = datetime.now(UTC)

    if order.recurrent_subscription_id:
        recurrent = await db.get(LavaRecurrentSubscription, order.recurrent_subscription_id)
        if recurrent is not None and subscription is not None:
            recurrent.subscription_id = subscription.id

    if not commit:
        await db.flush()
        return True, order

    await db.commit()
    await emit_lava_service_order_side_effects(db, order)
    return True, order


async def emit_lava_service_order_side_effects(db: AsyncSession, order: LavaServiceOrder) -> None:
    """Emit notifications and enqueue panel sync after the database commit."""
    transaction = await db.get(Transaction, order.transaction_id) if order.transaction_id else None
    if transaction is not None:
        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=order.amount_kopeks,
            user_id=order.user_id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            payment_method=PaymentMethod.LAVA,
            external_id=transaction.external_id,
            description=order.description,
        )

    subscription = await db.get(Subscription, order.subscription_id) if order.subscription_id else None
    if subscription is not None:
        try:
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            remnawave_retry_queue.enqueue(
                subscription_id=subscription.id,
                user_id=order.user_id,
                action='update' if subscription.remnawave_uuid else 'create',
            )
        except Exception as error:
            logger.error('Failed to enqueue RemnaWave sync for Lava order', order_id=order.id, error=error)


async def create_recurrent_renewal_order(
    db: AsyncSession,
    record: LavaRecurrentSubscription,
    *,
    provider_invoice_id: str,
) -> LavaServiceOrder:
    """Build an immutable renewal order from the binding's original terms."""
    existing = (
        await db.execute(
            select(LavaServiceOrder).where(
                LavaServiceOrder.recurrent_subscription_id == record.id,
                LavaServiceOrder.provider_invoice_id == provider_invoice_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    tariff = await db.get(Tariff, record.tariff_id)
    if tariff is None:
        raise ValueError('Tariff for recurrent renewal is missing')
    terms = dict(getattr(record, 'terms_snapshot', None) or {})
    squads = list(terms.get('connected_squads') or tariff.allowed_squads or [])
    order = LavaServiceOrder(
        user_id=record.user_id,
        subscription_id=record.subscription_id,
        tariff_id=record.tariff_id,
        recurrent_subscription_id=record.id,
        kind='tariff',
        payment_mode='recurrent',
        status='pending',
        amount_kopeks=record.amount_kopeks,
        currency='RUB',
        description=f"Продление тарифа '{tariff.name}' через Lava",
        snapshot={
            'tariff_name': tariff.name,
            'period_days': record.period_days,
            'traffic_limit_gb': int(terms.get('traffic_limit_gb', tariff.traffic_limit_gb)),
            'device_limit': int(terms.get('device_limit', tariff.device_limit)),
            'connected_squads': squads,
        },
        provider_order_id=f'lavarenew_{provider_invoice_id}'[:64],
        provider_invoice_id=provider_invoice_id,
    )
    db.add(order)
    await db.flush()
    return order
