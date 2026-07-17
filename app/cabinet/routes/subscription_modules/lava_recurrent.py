"""Cabinet endpoints for Lava recurrent subscription management."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.services.lava_recurrent_service import (
    SETUP_WINDOW_DAYS,
    cancel_recurrent_subscription,
    configured_product_id,
    get_current_recurrent_subscription,
    start_recurrent_subscription,
)
from app.services.lava_service import LavaAPIError

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import LavaRecurrentSubscribeRequest
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
    await db.refresh(subscription, ['tariff'])
    tariff = subscription.tariff
    plans: list[dict] = []
    if tariff and not tariff.is_daily:
        for period_days in tariff.get_available_periods():
            product_id = configured_product_id(tariff.name, period_days)
            price = tariff.get_price_for_period(period_days)
            if product_id and price:
                plans.append(
                    {
                        'period_days': period_days,
                        'amount_kopeks': int(price),
                        'product_id': product_id,
                    }
                )
    current = await get_current_recurrent_subscription(
        db,
        subscription_id=subscription.id,
        user_id=user.id,
    )
    available_from = None
    inside_setup_window = True
    if subscription.end_date and subscription.end_date > datetime.now(UTC):
        available_at = subscription.end_date - timedelta(days=SETUP_WINDOW_DAYS)
        if available_at > datetime.now(UTC):
            available_from = available_at.isoformat()
            inside_setup_window = False
    return {
        'enabled': settings.is_lava_recurrent_enabled(),
        'eligible': bool(plans) and subscription.is_trial is False and inside_setup_window,
        'available_from': available_from,
        'email_required': not bool(user.email),
        'plans': plans,
        'subscription': _serialize(current) or None,
    }


@router.post('/subscribe')
async def subscribe_lava_recurrent(
    request: LavaRecurrentSubscribeRequest,
    http_request: Request,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None),
):
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')
    if subscription.is_trial is not False:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Trial subscriptions are not eligible')
    await db.refresh(subscription, ['tariff'])
    tariff = subscription.tariff
    if not tariff or tariff.is_daily:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Tariff is not eligible')
    email = str(request.email or user.email or '').strip()
    if not email:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Email is required')
    try:
        record, payment_url = await start_recurrent_subscription(
            db,
            user=user,
            subscription=subscription,
            tariff=tariff,
            period_days=request.period_days,
            email=email,
            consent_ip=http_request.client.host if http_request.client else None,
            consent_user_agent=http_request.headers.get('user-agent'),
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except LavaAPIError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=error.message) from error
    return {'payment_url': payment_url, 'subscription': _serialize(record)}


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
