"""Cabinet endpoints for Lava recurrent subscription management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription, Tariff, User
from app.services.lava_recurrent_service import (
    cancel_recurrent_subscription,
    get_current_recurrent_subscription,
    start_recurrent_subscription,
)
from app.services.lava_service import LavaAPIError
from app.services.pricing_engine import pricing_engine

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
    subscription = (
        await db.execute(
            select(Subscription).where(
                Subscription.id == request.subscription_id,
                Subscription.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')
    if subscription.is_trial is not True or user.has_had_paid_subscription:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Only the first purchase after trial is eligible')
    tariff = await db.get(Tariff, request.tariff_id)
    if not tariff or not tariff.is_active or tariff.is_daily:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Tariff is not eligible')
    raw_price = tariff.get_price_for_period(request.period_days)
    product_id = settings.get_lava_recurrent_product_map().get((tariff.name.strip(), request.period_days))
    if not product_id or raw_price is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Recurrent product is not configured')
    pricing = await pricing_engine.calculate_tariff_purchase_price(
        tariff, request.period_days, device_limit=tariff.device_limit, custom_traffic_gb=None, user=user
    )
    if pricing.final_total != int(raw_price):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Recurrent checkout is unavailable with discounts or add-ons')
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


@router.post('/subscribe', status_code=status.HTTP_410_GONE)
async def rejected_standalone_recurrent_checkout():
    raise HTTPException(status_code=status.HTTP_410_GONE, detail='Use recurrent checkout during the first purchase after trial')


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
