"""Cabinet endpoints for Lava recurrent subscription management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import LavaRecurrentSubscription, Tariff, User
from app.services.lava_recurrent_service import (
    cancel_recurrent_subscription,
    get_current_recurrent_subscription,
)
from app.services.lava_service import LavaAPIError

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import LavaRecurrentCheckoutRequest
from .helpers import resolve_subscription


router = APIRouter(prefix='/lava-recurrent')


def _serialize(record, tariff_name: str | None = None) -> dict:
    if record is None:
        return {}
    return {
        'id': record.id,
        'status': record.status,
        'is_active': record.is_active,
        'period_days': record.period_days,
        'amount_kopeks': record.amount_kopeks,
        'product_id': record.product_id,
        'payment_url': record.payment_url if record.status == 'created' else None,
        'payer_details': record.payer_details,
        'next_pay_at': record.next_pay_at.isoformat() if record.next_pay_at else None,
        'created_at': record.created_at.isoformat() if record.created_at else None,
        'subscription_id': record.subscription_id,
        'tariff_id': record.tariff_id,
        'tariff_name': tariff_name,
        'deactivated_at': record.deactivated_at.isoformat() if record.deactivated_at else None,
    }


async def _cancel_owned_record(
    db: AsyncSession,
    user: User,
    recurrent_id: int,
) -> tuple[LavaRecurrentSubscription, bool]:
    record = (
        await db.execute(
            select(LavaRecurrentSubscription)
            .where(
                LavaRecurrentSubscription.id == recurrent_id,
                LavaRecurrentSubscription.user_id == user.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Lava recurrent subscription not found')
    if record.status == 'deactivated':
        return record, True
    try:
        record = await cancel_recurrent_subscription(db, record)
    except LavaAPIError:
        # The user instruction was committed as cancel_requested before the
        # provider call. A background reconciler will retry it safely.
        await db.rollback()
        record = await db.get(LavaRecurrentSubscription, recurrent_id)
        return record, False
    return record, record.status == 'deactivated'


@router.get('')
async def get_lava_recurrent_state(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None),
):
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')
    current = await get_current_recurrent_subscription(
        db,
        subscription_id=subscription.id,
        user_id=user.id,
    )
    if current is None:
        current = (
            await db.execute(
                select(LavaRecurrentSubscription)
                .where(
                    LavaRecurrentSubscription.subscription_id == subscription.id,
                    LavaRecurrentSubscription.user_id == user.id,
                )
                .order_by(LavaRecurrentSubscription.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return {
        'enabled': settings.is_lava_recurrent_enabled_for_user(user.telegram_id),
        'subscription': _serialize(current) or None,
    }


@router.get('/agreements')
async def list_lava_recurrent_agreements(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    rows = (
        await db.execute(
            select(LavaRecurrentSubscription, Tariff.name)
            .outerjoin(Tariff, Tariff.id == LavaRecurrentSubscription.tariff_id)
            .where(
                LavaRecurrentSubscription.user_id == user.id,
                LavaRecurrentSubscription.status.in_({'created', 'activated', 'suspended', 'cancel_requested'}),
            )
            .order_by(LavaRecurrentSubscription.created_at.desc())
        )
    ).all()
    return {'agreements': [_serialize(record, tariff_name) for record, tariff_name in rows]}


@router.delete('/{recurrent_id}')
async def unsubscribe_lava_recurrent_by_id(
    recurrent_id: int,
    response: Response,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    record, confirmed = await _cancel_owned_record(db, user, recurrent_id)
    if not confirmed:
        response.status_code = status.HTTP_202_ACCEPTED
    return {'success': confirmed, 'pending': not confirmed, 'subscription': _serialize(record)}


@router.post('/checkout')
async def checkout_lava_recurrent(
    request: LavaRecurrentCheckoutRequest,
    http_request: Request,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail='Use /subscription/lava-orders/checkout for direct service payment',
    )


@router.post('/subscribe', status_code=status.HTTP_410_GONE)
async def rejected_standalone_recurrent_checkout():
    raise HTTPException(status_code=status.HTTP_410_GONE, detail='Use /subscription/lava-orders/checkout')


@router.delete('')
async def unsubscribe_lava_recurrent(
    response: Response,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None),
):
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')
    record = await get_current_recurrent_subscription(
        db,
        subscription_id=subscription.id,
        user_id=user.id,
    )
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No Lava recurrent subscription found')
    record, confirmed = await _cancel_owned_record(db, user, record.id)
    if not confirmed:
        response.status_code = status.HTTP_202_ACCEPTED
    return {'success': confirmed, 'pending': not confirmed, 'subscription': _serialize(record)}
