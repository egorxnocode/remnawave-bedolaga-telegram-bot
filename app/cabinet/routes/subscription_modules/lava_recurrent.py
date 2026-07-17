"""Cabinet endpoints for Lava recurrent subscription management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.services.lava_recurrent_service import (
    cancel_recurrent_subscription,
    get_current_recurrent_subscription,
)
from app.services.lava_service import LavaAPIError

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import LavaRecurrentCheckoutRequest
from .helpers import resolve_subscription


router = APIRouter(prefix='/lava-recurrent')


def _serialize(record) -> dict:
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
    }


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
    return {
        'enabled': settings.is_lava_recurrent_enabled(),
        'subscription': _serialize(current) or None,
    }


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
    try:
        record = await cancel_recurrent_subscription(db, record)
    except LavaAPIError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=error.message) from error
    return {'success': True, 'subscription': _serialize(record)}
