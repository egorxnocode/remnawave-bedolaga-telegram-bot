"""Cabinet checkout for direct Lava service orders."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription, Tariff, User
from app.services.lava_order_service import attach_provider_payment, create_service_order
from app.services.lava_recurrent_service import (
    cancel_recurrent_subscription,
    configured_product_id,
    get_current_recurrent_subscription,
    start_recurrent_subscription,
)
from app.services.lava_service import LavaAPIError
from app.services.payment_service import PaymentService
from app.services.pricing_engine import pricing_engine

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import LavaServiceCheckoutRequest
from .helpers import _apply_addon_discount


router = APIRouter(prefix='/lava-orders')


async def _owned_subscription(db: AsyncSession, user_id: int, subscription_id: int | None) -> Subscription | None:
    if subscription_id is None:
        return None
    return (
        await db.execute(
            select(Subscription).where(Subscription.id == subscription_id, Subscription.user_id == user_id)
        )
    ).scalar_one_or_none()


async def _tariff_snapshot(db: AsyncSession, tariff: Tariff, period_days: int) -> dict:
    squads = list(tariff.allowed_squads or [])
    if not squads:
        from app.database.crud.server_squad import get_all_server_squads

        all_servers, _ = await get_all_server_squads(db, available_only=True)
        squads = [server.squad_uuid for server in all_servers if server.squad_uuid]
    return {
        'tariff_name': tariff.name,
        'period_days': period_days,
        'traffic_limit_gb': tariff.traffic_limit_gb,
        'device_limit': tariff.device_limit,
        'connected_squads': squads,
        'is_daily': bool(tariff.is_daily),
    }


@router.post('/checkout')
async def checkout_lava_service(
    request: LavaServiceCheckoutRequest,
    http_request: Request,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    if not settings.is_lava_enabled():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Lava payment method is unavailable')
    if getattr(user, 'restriction_subscription', False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Purchases are restricted for this account')

    subscription = await _owned_subscription(db, user.id, request.subscription_id)
    tariff = await db.get(Tariff, request.tariff_id) if request.tariff_id else None
    kind = request.kind
    amount_kopeks: int
    description: str
    snapshot: dict

    if kind in {'tariff', 'daily'}:
        if tariff is None or not tariff.is_active:
            raise HTTPException(status_code=404, detail='Tariff not found or inactive')
        if kind == 'daily' and not tariff.is_daily:
            raise HTTPException(status_code=400, detail='Selected tariff is not daily')
        if kind == 'tariff' and tariff.is_daily:
            raise HTTPException(status_code=400, detail='Daily tariff requires one-time checkout')
        period_days = 1 if tariff.is_daily else int(request.period_days or 0)
        raw_price = tariff.get_price_for_period(period_days)
        if tariff.is_daily:
            raw_price = tariff.daily_price_kopeks
        elif raw_price is None and tariff.can_purchase_custom_days():
            raw_price = tariff.get_price_for_custom_days(period_days)
        if raw_price is None or int(raw_price) <= 0:
            raise HTTPException(status_code=400, detail='Selected tariff period is unavailable')
        custom_traffic_gb = None
        if request.traffic_gb is not None:
            if not tariff.can_purchase_custom_traffic():
                raise HTTPException(status_code=400, detail='Custom traffic is unavailable for this tariff')
            if request.traffic_gb < tariff.min_traffic_gb or request.traffic_gb > tariff.max_traffic_gb:
                raise HTTPException(status_code=400, detail='Custom traffic value is outside tariff limits')
            custom_traffic_gb = request.traffic_gb
        device_limit = subscription.device_limit if subscription and subscription.tariff_id == tariff.id else None
        pricing = await pricing_engine.calculate_tariff_purchase_price(
            tariff,
            period_days,
            device_limit=device_limit,
            custom_traffic_gb=custom_traffic_gb,
            user=user,
        )
        amount_kopeks = int(pricing.final_total)
        if amount_kopeks <= 0:
            raise HTTPException(status_code=400, detail='Zero-price checkout is not supported by Lava')
        snapshot = await _tariff_snapshot(db, tariff, period_days)
        if custom_traffic_gb is not None:
            snapshot['traffic_limit_gb'] = custom_traffic_gb
            snapshot['custom_traffic_gb'] = custom_traffic_gb
        if subscription and subscription.tariff_id == tariff.id:
            snapshot['device_limit'] = max(tariff.device_limit or 0, subscription.device_limit or 0)
        description = (
            f"Активация суточного тарифа '{tariff.name}'"
            if tariff.is_daily
            else f"Покупка или продление тарифа '{tariff.name}' на {period_days} дней"
        )
    elif kind == 'traffic':
        if subscription is None or subscription.status not in {'active', 'trial', 'limited'}:
            raise HTTPException(status_code=400, detail='Active subscription is required')
        tariff = await db.get(Tariff, subscription.tariff_id) if subscription.tariff_id else None
        if tariff is None or not tariff.traffic_topup_enabled or tariff.is_daily:
            raise HTTPException(status_code=400, detail='Traffic top-up is unavailable')
        packages = tariff.get_traffic_topup_packages()
        gb = int(request.traffic_gb or 0)
        if gb not in packages or int(packages[gb]) <= 0:
            raise HTTPException(status_code=400, detail='Traffic package is unavailable')
        discount = _apply_addon_discount(user, 'traffic', int(packages[gb]), 30)
        amount_kopeks = int(discount['discounted'])
        if discount['percent'] < 100:
            amount_kopeks = max(100, amount_kopeks)
        description = f'Докупка {gb} ГБ трафика'
        snapshot = {'traffic_gb': gb, 'discount_percent': discount['percent']}
    else:
        if subscription is None or subscription.status not in {'active', 'trial'}:
            raise HTTPException(status_code=400, detail='Active subscription is required')
        tariff = await db.get(Tariff, subscription.tariff_id) if subscription.tariff_id else None
        if tariff is None or not tariff.device_price_kopeks or tariff.is_daily:
            raise HTTPException(status_code=400, detail='Additional devices are unavailable')
        devices = int(request.devices or 0)
        days_left = max(1, math.ceil((subscription.end_date - datetime.now(UTC)).total_seconds() / 86400))
        base_price = max(100, int(tariff.device_price_kopeks * devices * days_left / 30))
        discount = _apply_addon_discount(user, 'devices', base_price, days_left)
        amount_kopeks = int(discount['discounted'])
        if discount['percent'] < 100:
            amount_kopeks = max(100, amount_kopeks)
        description = f'Покупка {devices} доп. устройств'
        snapshot = {'devices': devices, 'discount_percent': discount['percent'], 'days_left_snapshot': days_left}

    recurrent = bool(request.recurrent and kind == 'tariff')
    if recurrent:
        if not settings.is_lava_recurrent_enabled() or tariff is None:
            raise HTTPException(status_code=400, detail='Lava recurrent payments are unavailable')
        if not request.accepted_terms:
            raise HTTPException(status_code=422, detail='Recurrent payment consent is required')
        product_id = configured_product_id(tariff.name, int(snapshot['period_days']))
        raw_price = tariff.get_price_for_period(int(snapshot['period_days']))
        if snapshot.get('custom_traffic_gb') is not None or not product_id or raw_price is None or int(raw_price) != amount_kopeks:
            raise HTTPException(status_code=400, detail='Recurrent checkout is unavailable for this price')

    if recurrent and not str(request.email or user.email or '').strip():
        raise HTTPException(status_code=422, detail='Email is required')

    order = await create_service_order(
        db,
        user_id=user.id,
        kind=kind,
        payment_mode='recurrent' if recurrent else 'one_time',
        amount_kopeks=amount_kopeks,
        description=description,
        snapshot=snapshot,
        subscription_id=subscription.id if subscription else None,
        tariff_id=tariff.id if tariff else None,
    )

    if recurrent:
        current = (
            await get_current_recurrent_subscription(db, subscription_id=subscription.id, user_id=user.id)
            if subscription
            else None
        )
        if current:
            try:
                await cancel_recurrent_subscription(db, current)
            except LavaAPIError as error:
                order.status = 'failed'
                order.failure_reason = 'Previous recurrent subscription could not be cancelled'
                await db.commit()
                raise HTTPException(status_code=502, detail=error.message) from error
        email = str(request.email or user.email or '').strip()
        try:
            record, payment_url = await start_recurrent_subscription(
                db,
                user=user,
                subscription=subscription,
                tariff=tariff,
                service_order=order,
                period_days=int(snapshot['period_days']),
                email=email,
                consent_ip=http_request.client.host if http_request.client else None,
                consent_user_agent=http_request.headers.get('user-agent'),
            )
        except (ValueError, LavaAPIError) as error:
            order.status = 'failed'
            order.failure_reason = str(error)
            await db.commit()
            code = 502 if isinstance(error, LavaAPIError) else 400
            raise HTTPException(status_code=code, detail=str(error)) from error
        return {'order_id': order.id, 'payment_mode': 'recurrent', 'payment_url': payment_url, 'recurrent_id': record.id}

    provider_order_id = f'lavasvc{order.id}_{uuid4().hex[:24]}'
    result = await PaymentService().create_lava_payment(
        db=db,
        user_id=user.id,
        amount_kopeks=amount_kopeks,
        description=description,
        email=getattr(user, 'email', None),
        language=getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE,
        return_url=f'{settings.CABINET_URL.rstrip("/")}/subscriptions',
        order_id=provider_order_id,
        metadata_extra={'type': 'service_order', 'service_order_id': order.id, 'service_kind': kind},
    )
    if not result or not result.get('payment_url'):
        order.status = 'failed'
        order.failure_reason = 'Lava invoice creation failed'
        await db.commit()
        raise HTTPException(status_code=502, detail='Failed to create Lava invoice')
    await attach_provider_payment(
        db,
        order,
        provider_order_id=provider_order_id,
        provider_invoice_id=result.get('payment_id'),
    )
    return {'order_id': order.id, 'payment_mode': 'one_time', 'payment_url': result['payment_url']}
