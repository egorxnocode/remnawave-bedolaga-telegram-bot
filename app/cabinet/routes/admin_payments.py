"""Admin routes for payment verification in cabinet."""

import math
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.database.crud.transaction import create_transaction
from app.database.models import (
    LavaRefundRequest,
    LavaServiceOrder,
    PaymentMethod,
    Subscription,
    TransactionType,
    User,
)
from app.services.payment_search_service import (
    MAX_ALL_TIME_DAYS,
    PeriodPreset,
    SearchParams,
    StatusFilter,
    search_payments,
    search_payments_stats,
)
from app.services.payment_service import PaymentService
from app.services.payment_verification_service import (
    SUPPORTED_MANUAL_CHECK_METHODS,
    PendingPayment,
    get_payment_record,
    list_recent_pending_payments,
    method_display_name,
    run_manual_check,
)

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/payments', tags=['Cabinet Admin Payments'])


# ============ Schemas ============


class PendingPaymentResponse(BaseModel):
    """Pending payment details."""

    id: int
    method: str
    method_display: str
    identifier: str
    amount_kopeks: int
    amount_rubles: float
    status: str
    status_emoji: str
    status_text: str
    is_paid: bool
    is_checkable: bool
    created_at: datetime
    expires_at: datetime | None = None
    payment_url: str | None = None
    user_id: int | None = None
    user_telegram_id: int | None = None
    user_username: str | None = None
    user_email: str | None = None
    decline_reason: str | None = None
    service_order_id: int | None = None

    class Config:
        from_attributes = True


class PendingPaymentListResponse(BaseModel):
    """Paginated list of pending payments."""

    items: list[PendingPaymentResponse]
    total: int
    page: int
    per_page: int
    pages: int


class ManualCheckResponse(BaseModel):
    """Response after manual payment status check."""

    success: bool
    message: str
    payment: PendingPaymentResponse | None = None
    status_changed: bool = False
    old_status: str | None = None
    new_status: str | None = None


class PaymentsStatsResponse(BaseModel):
    """Statistics about pending payments."""

    total_pending: int
    by_method: dict


class SearchStatsResponse(BaseModel):
    """Statistics for payment search results."""

    total: int
    pending: int
    paid: int
    cancelled: int
    by_method: dict


class LavaRefundCreateRequest(BaseModel):
    reason: str
    revoke_service: bool = False


class LavaRefundConfirmRequest(BaseModel):
    money_returned: bool
    provider_reference: str
    admin_comment: str | None = None


class LavaRefundResponse(BaseModel):
    id: int
    service_order_id: int
    user_id: int
    amount_kopeks: int
    currency: str
    status: str
    reason: str
    provider_reference: str | None
    admin_comment: str | None
    revoke_service: bool
    service_revoked_at: datetime | None
    refund_transaction_id: int | None
    requested_at: datetime
    completed_at: datetime | None

    class Config:
        from_attributes = True


# ============ Helper functions ============


def _get_status_info(record: PendingPayment) -> tuple[str, str]:
    """Get status emoji and text for a pending payment."""
    status_str = (record.status or '').lower()

    if record.is_paid:
        return '✅', 'Оплачено'

    if record.method == PaymentMethod.PAL24:
        mapping = {
            'new': ('⏳', 'Ожидает оплаты'),
            'process': ('⌛', 'Обрабатывается'),
            'success': ('✅', 'Оплачено'),
            'fail': ('❌', 'Ошибка'),
            'canceled': ('❌', 'Отменено'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.MULENPAY:
        mapping = {
            'created': ('⏳', 'Ожидает оплаты'),
            'processing': ('⌛', 'Обрабатывается'),
            'hold': ('🔒', 'На удержании'),
            'success': ('✅', 'Оплачено'),
            'canceled': ('❌', 'Отменено'),
            'error': ('❌', 'Ошибка'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.WATA:
        mapping = {
            'opened': ('⏳', 'Ожидает оплаты'),
            'pending': ('⏳', 'Ожидает оплаты'),
            'processing': ('⌛', 'Обрабатывается'),
            'paid': ('✅', 'Оплачено'),
            'closed': ('✅', 'Оплачено'),
            'declined': ('❌', 'Отклонено'),
            'canceled': ('❌', 'Отменено'),
            'expired': ('⌛', 'Истёк'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.PLATEGA:
        mapping = {
            'pending': ('⏳', 'Ожидает оплаты'),
            'inprogress': ('⌛', 'Обрабатывается'),
            'confirmed': ('✅', 'Оплачено'),
            'failed': ('❌', 'Ошибка'),
            'canceled': ('❌', 'Отменено'),
            'expired': ('⌛', 'Истёк'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.HELEKET:
        if status_str in {'pending', 'created', 'waiting', 'check', 'processing'}:
            return '⏳', 'Ожидает оплаты'
        if status_str in {'paid', 'paid_over'}:
            return '✅', 'Оплачено'
        if status_str in {'cancel', 'canceled', 'fail', 'failed', 'expired'}:
            return '❌', 'Отменено'
        return '❓', 'Неизвестно'

    if record.method == PaymentMethod.YOOKASSA:
        mapping = {
            'pending': ('⏳', 'Ожидает оплаты'),
            'waiting_for_capture': ('⌛', 'Обрабатывается'),
            'succeeded': ('✅', 'Оплачено'),
            'canceled': ('❌', 'Отменено'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.CRYPTOBOT:
        mapping = {
            'active': ('⏳', 'Ожидает оплаты'),
            'paid': ('✅', 'Оплачено'),
            'expired': ('⌛', 'Истёк'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.CLOUDPAYMENTS:
        mapping = {
            'pending': ('⏳', 'Ожидает оплаты'),
            'authorized': ('⌛', 'Авторизовано'),
            'completed': ('✅', 'Оплачено'),
            'failed': ('❌', 'Ошибка'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.FREEKASSA:
        mapping = {
            'pending': ('⏳', 'Ожидает оплаты'),
            'success': ('✅', 'Оплачено'),
            'paid': ('✅', 'Оплачено'),
            'canceled': ('❌', 'Отменено'),
            'error': ('❌', 'Ошибка'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    if record.method == PaymentMethod.LAVA:
        mapping = {
            'created': ('⏳', 'Ожидает оплаты'),
            'pending': ('⏳', 'Ожидает оплаты'),
            'processing': ('⌛', 'Обрабатывается'),
            'success': ('✅', 'Оплачено'),
            'paid': ('✅', 'Оплачено'),
            'completed': ('✅', 'Оплачено'),
            'cancel': ('❌', 'Отменено'),
            'canceled': ('❌', 'Отменено'),
            'cancelled': ('❌', 'Отменено'),
            'expired': ('⌛', 'Истёк'),
            'fail': ('❌', 'Ошибка'),
            'failed': ('❌', 'Ошибка'),
            'error': ('❌', 'Ошибка'),
        }
        return mapping.get(status_str, ('❓', 'Неизвестно'))

    return '❓', 'Неизвестно'


def _is_checkable(record: PendingPayment) -> bool:
    """Check if payment can be manually checked."""
    if record.method not in SUPPORTED_MANUAL_CHECK_METHODS:
        return False
    if not record.is_recent():
        return False
    status_str = (record.status or '').lower()
    if record.method == PaymentMethod.PAL24:
        return status_str in {'new', 'process'}
    if record.method == PaymentMethod.MULENPAY:
        return status_str in {'created', 'processing', 'hold'}
    if record.method == PaymentMethod.WATA:
        return status_str in {'opened', 'pending', 'processing', 'inprogress', 'in_progress'}
    if record.method == PaymentMethod.PLATEGA:
        return status_str in {'pending', 'inprogress', 'in_progress'}
    if record.method == PaymentMethod.HELEKET:
        return status_str not in {'paid', 'paid_over', 'cancel', 'canceled', 'fail', 'failed', 'expired'}
    if record.method == PaymentMethod.YOOKASSA:
        return status_str in {'pending', 'waiting_for_capture'}
    if record.method == PaymentMethod.CRYPTOBOT:
        return status_str == 'active'
    if record.method == PaymentMethod.CLOUDPAYMENTS:
        return status_str in {'pending', 'authorized'}
    if record.method == PaymentMethod.FREEKASSA:
        return status_str in {'pending', 'created', 'processing'}
    return False


def _get_payment_url(record: PendingPayment) -> str | None:
    """Extract payment URL from record."""
    payment = record.payment
    payment_url = getattr(payment, 'payment_url', None)

    if record.method == PaymentMethod.PAL24:
        payment_url = getattr(payment, 'link_url', None) or getattr(payment, 'link_page_url', None) or payment_url
    elif record.method == PaymentMethod.WATA:
        payment_url = getattr(payment, 'url', None) or payment_url
    elif record.method == PaymentMethod.YOOKASSA:
        payment_url = getattr(payment, 'confirmation_url', None) or payment_url
    elif record.method == PaymentMethod.CRYPTOBOT:
        payment_url = (
            getattr(payment, 'bot_invoice_url', None)
            or getattr(payment, 'mini_app_invoice_url', None)
            or getattr(payment, 'web_app_invoice_url', None)
            or payment_url
        )
    elif record.method == PaymentMethod.PLATEGA:
        payment_url = getattr(payment, 'redirect_url', None) or payment_url
    elif record.method == PaymentMethod.CLOUDPAYMENTS or record.method == PaymentMethod.FREEKASSA:
        payment_url = getattr(payment, 'payment_url', None) or payment_url

    if payment_url and not payment_url.startswith(('https://', 'http://')):
        return None
    return payment_url


def _extract_decline_reason(record: PendingPayment) -> str | None:
    """Достаёт причину отклонения платежа из callback_payload (EtoPlatezhi и др.)."""
    status = (record.status or '').lower()
    if status not in ('declined', 'decline', 'error', 'failed'):
        return None
    payment = getattr(record, 'payment', None)
    if payment is None:
        return None
    import json as _json

    raw = getattr(payment, 'callback_payload', None)
    data = None
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            data = _json.loads(raw)
        except (ValueError, TypeError):
            data = None
    if not isinstance(data, dict):
        return None
    op = data.get('operation') if isinstance(data.get('operation'), dict) else {}
    code = op.get('code') or data.get('decline_code')
    msg = op.get('message') or data.get('decline_message') or op.get('description')
    if msg and code:
        return f'{msg} (код {code})'
    if msg:
        return str(msg)
    if code:
        return f'Код отказа: {code}'
    return None


def _record_to_response(record: PendingPayment) -> PendingPaymentResponse:
    """Convert PendingPayment to API response."""
    status_emoji, status_text = _get_status_info(record)
    metadata = dict(getattr(record.payment, 'metadata_json', None) or {})
    service_order_id = metadata.get('service_order_id')
    try:
        service_order_id = int(service_order_id) if service_order_id is not None else None
    except (TypeError, ValueError):
        service_order_id = None
    return PendingPaymentResponse(
        id=record.local_id,
        method=record.method.value,
        method_display=method_display_name(record.method),
        identifier=record.identifier,
        amount_kopeks=record.amount_kopeks,
        amount_rubles=record.amount_kopeks / 100,
        status=record.status or '',
        status_emoji=status_emoji,
        status_text=status_text,
        is_paid=record.is_paid,
        is_checkable=_is_checkable(record),
        created_at=record.created_at,
        expires_at=record.expires_at,
        payment_url=_get_payment_url(record),
        user_id=record.user.id if record.user else None,
        user_telegram_id=record.user.telegram_id if record.user else None,
        user_username=record.user.username if record.user else None,
        user_email=record.user.email if record.user else None,
        decline_reason=_extract_decline_reason(record),
        service_order_id=service_order_id,
    )


# ============ Routes ============


@router.get('', response_model=PendingPaymentListResponse)
async def get_all_pending_payments(
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    method_filter: str | None = Query(None, description='Filter by payment method'),
    admin: User = Depends(require_permission('payments:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get all pending payments for admin verification."""
    all_pending = await list_recent_pending_payments(db)

    # Apply method filter if specified
    if method_filter:
        try:
            filter_method = PaymentMethod(method_filter)
            all_pending = [p for p in all_pending if p.method == filter_method]
        except ValueError:
            pass

    total = len(all_pending)
    pages = math.ceil(total / per_page) if total > 0 else 1

    # Paginate
    start_idx = (page - 1) * per_page
    page_payments = all_pending[start_idx : start_idx + per_page]

    items = [_record_to_response(p) for p in page_payments]

    return PendingPaymentListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.get('/stats', response_model=PaymentsStatsResponse)
async def get_payments_stats(
    admin: User = Depends(require_permission('payments:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get statistics about pending payments."""
    all_pending = await list_recent_pending_payments(db)

    by_method = {}
    for p in all_pending:
        method_name = method_display_name(p.method)
        if method_name not in by_method:
            by_method[method_name] = 0
        by_method[method_name] += 1

    return PaymentsStatsResponse(
        total_pending=len(all_pending),
        by_method=by_method,
    )


@router.get('/search', response_model=PendingPaymentListResponse)
async def search_payments_endpoint(
    search: str | None = Query(
        None, max_length=256, description='Search query (invoice, @username, telegram_id, email)'
    ),
    status_filter: str = Query('all', description='Status filter: all, pending, paid, cancelled'),
    method_filter: str | None = Query(None, description='Filter by payment method'),
    period: str = Query('24h', description='Period preset: 24h, 7d, 30d, all'),
    date_from: datetime | None = Query(None, description='Custom range start (ISO 8601)'),
    date_to: datetime | None = Query(None, description='Custom range end (ISO 8601)'),
    page: int = Query(1, ge=1, description='Page number'),
    per_page: int = Query(20, ge=1, le=100, description='Items per page'),
    admin: User = Depends(require_permission('payments:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Search payments across all providers with filters."""
    try:
        parsed_status = StatusFilter(status_filter)
    except ValueError:
        parsed_status = StatusFilter.ALL

    try:
        parsed_period = PeriodPreset(period)
    except ValueError:
        parsed_period = PeriodPreset.H24

    parsed_method: PaymentMethod | None = None
    if method_filter:
        try:
            parsed_method = PaymentMethod(method_filter)
        except ValueError:
            pass

    # Ensure custom dates are timezone-aware
    if date_from is not None and date_from.tzinfo is None:
        date_from = date_from.replace(tzinfo=UTC)
    if date_to is not None and date_to.tzinfo is None:
        date_to = date_to.replace(tzinfo=UTC)

    # Clamp custom dates to safety limit
    min_allowed = datetime.now(UTC) - timedelta(days=MAX_ALL_TIME_DAYS)
    if date_from is not None and date_from < min_allowed:
        date_from = min_allowed
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='date_from must be before date_to')

    params = SearchParams(
        search=search.strip() if search else None,
        status_filter=parsed_status,
        method_filter=parsed_method,
        period=parsed_period,
        date_from=date_from,
        date_to=date_to,
        page=page,
        per_page=per_page,
    )

    page_items, total = await search_payments(db, params)
    pages = math.ceil(total / per_page) if total > 0 else 1
    items = [_record_to_response(p) for p in page_items]

    return PendingPaymentListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )


@router.get('/search/stats', response_model=SearchStatsResponse)
async def search_payments_stats_endpoint(
    search: str | None = Query(
        None, max_length=256, description='Search query (invoice, @username, telegram_id, email)'
    ),
    status_filter: str = Query('all', description='Status filter: all, pending, paid, cancelled'),
    method_filter: str | None = Query(None, description='Filter by payment method'),
    period: str = Query('24h', description='Period preset: 24h, 7d, 30d, all'),
    date_from: datetime | None = Query(None, description='Custom range start (ISO 8601)'),
    date_to: datetime | None = Query(None, description='Custom range end (ISO 8601)'),
    admin: User = Depends(require_permission('payments:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get aggregated statistics for payment search results."""
    try:
        parsed_status = StatusFilter(status_filter)
    except ValueError:
        parsed_status = StatusFilter.ALL

    try:
        parsed_period = PeriodPreset(period)
    except ValueError:
        parsed_period = PeriodPreset.H24

    parsed_method: PaymentMethod | None = None
    if method_filter:
        try:
            parsed_method = PaymentMethod(method_filter)
        except ValueError:
            pass

    # Ensure custom dates are timezone-aware
    if date_from is not None and date_from.tzinfo is None:
        date_from = date_from.replace(tzinfo=UTC)
    if date_to is not None and date_to.tzinfo is None:
        date_to = date_to.replace(tzinfo=UTC)

    # Clamp custom dates to safety limit
    min_allowed = datetime.now(UTC) - timedelta(days=MAX_ALL_TIME_DAYS)
    if date_from is not None and date_from < min_allowed:
        date_from = min_allowed
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='date_from must be before date_to')

    params = SearchParams(
        search=search.strip() if search else None,
        status_filter=parsed_status,
        method_filter=parsed_method,
        period=parsed_period,
        date_from=date_from,
        date_to=date_to,
    )

    stats = await search_payments_stats(db, params)

    return SearchStatsResponse(
        total=stats.total,
        pending=stats.pending,
        paid=stats.paid,
        cancelled=stats.cancelled,
        by_method=stats.by_method or {},
    )


@router.get('/lava-refunds', response_model=list[LavaRefundResponse])
async def list_lava_refunds(
    refund_status: str | None = Query(default=None),
    admin: User = Depends(require_permission('payments:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    query = select(LavaRefundRequest).order_by(LavaRefundRequest.requested_at.desc()).limit(200)
    if refund_status:
        query = query.where(LavaRefundRequest.status == refund_status.strip().lower())
    return list((await db.execute(query)).scalars().all())


@router.post('/lava-orders/{order_id}/refunds', response_model=LavaRefundResponse)
async def request_lava_refund(
    order_id: int,
    payload: LavaRefundCreateRequest,
    admin: User = Depends(require_permission('payments:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a manual provider-refund task without touching user balance."""
    reason = payload.reason.strip()
    if len(reason) < 5:
        raise HTTPException(status_code=422, detail='Refund reason is required')
    order = (
        await db.execute(select(LavaServiceOrder).where(LavaServiceOrder.id == order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail='Lava service order not found')
    if order.status != 'fulfilled' or not order.transaction_id or not order.provider_invoice_id:
        raise HTTPException(status_code=409, detail='Only a fulfilled provider-paid order can be refunded')
    if payload.revoke_service and order.kind not in {'tariff', 'daily'}:
        raise HTTPException(status_code=409, detail='Automatic revocation is unavailable for add-on refunds')
    existing = (
        await db.execute(
            select(LavaRefundRequest).where(LavaRefundRequest.service_order_id == order.id).with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    refund = LavaRefundRequest(
        service_order_id=order.id,
        user_id=order.user_id,
        amount_kopeks=order.amount_kopeks,
        currency=order.currency,
        status='manual_required',
        reason=reason,
        revoke_service=payload.revoke_service,
        requested_by=admin.id,
    )
    db.add(refund)
    await db.commit()
    await db.refresh(refund)
    logger.warning('Lava manual refund requested', refund_id=refund.id, order_id=order.id, admin_id=admin.id)
    return refund


@router.post('/lava-refunds/{refund_id}/confirm', response_model=LavaRefundResponse)
async def confirm_lava_refund(
    refund_id: int,
    payload: LavaRefundConfirmRequest,
    admin: User = Depends(require_permission('payments:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Record completion only after the operator returned money in Lava."""
    if not payload.money_returned:
        raise HTTPException(status_code=422, detail='Confirm that money was returned in Lava first')
    provider_reference = payload.provider_reference.strip()
    if len(provider_reference) < 3:
        raise HTTPException(status_code=422, detail='Provider refund reference is required')
    refund = (
        await db.execute(
            select(LavaRefundRequest).where(LavaRefundRequest.id == refund_id).with_for_update()
        )
    ).scalar_one_or_none()
    if refund is None:
        raise HTTPException(status_code=404, detail='Lava refund request not found')
    if refund.status == 'completed':
        return refund
    if refund.status != 'manual_required':
        raise HTTPException(status_code=409, detail='Refund request is not awaiting manual completion')
    order = (
        await db.execute(
            select(LavaServiceOrder).where(LavaServiceOrder.id == refund.service_order_id).with_for_update()
        )
    ).scalar_one()
    refund_transaction = await create_transaction(
        db,
        user_id=refund.user_id,
        type=TransactionType.REFUND,
        amount_kopeks=refund.amount_kopeks,
        description=f'Возврат через Lava: заказ {order.id}',
        payment_method=PaymentMethod.LAVA,
        external_id=f'lava-refund:{refund.id}',
        commit=False,
    )
    subscription = None
    if refund.revoke_service and order.subscription_id:
        subscription = (
            await db.execute(
                select(Subscription).where(Subscription.id == order.subscription_id).with_for_update()
            )
        ).scalar_one_or_none()
        if subscription is not None:
            subscription.status = 'disabled'
            subscription.autopay_enabled = False
            subscription.updated_at = datetime.now(UTC)
            refund.service_revoked_at = datetime.now(UTC)
    refund.status = 'completed'
    refund.provider_reference = provider_reference
    refund.admin_comment = (payload.admin_comment or '').strip() or None
    refund.completed_by = admin.id
    refund.refund_transaction_id = refund_transaction.id
    refund.completed_at = datetime.now(UTC)
    refund.updated_at = datetime.now(UTC)
    await db.commit()
    if subscription is not None:
        try:
            from app.services.remnawave_retry_queue import remnawave_retry_queue

            remnawave_retry_queue.enqueue(
                subscription_id=subscription.id,
                user_id=refund.user_id,
                action='update',
            )
        except Exception:
            logger.exception('Failed to enqueue subscription revocation after Lava refund', refund_id=refund.id)
    user = await db.get(User, refund.user_id)
    if user is not None and user.telegram_id:
        bot = create_bot()
        try:
            await bot.send_message(
                user.telegram_id,
                '✅ <b>Возврат выполнен</b>\n\n'
                f'Сумма: <b>{refund.amount_kopeks / 100:.2f} ₽</b>\n'
                'Деньги отправлены на исходный способ оплаты. Срок зачисления зависит от банка.',
            )
        except Exception:
            logger.exception('Failed to notify user about Lava refund', refund_id=refund.id)
        finally:
            await bot.session.close()
    await db.refresh(refund)
    logger.warning('Lava manual refund completed', refund_id=refund.id, admin_id=admin.id)
    return refund


@router.get('/{method}/{payment_id}', response_model=PendingPaymentResponse)
async def get_pending_payment_details(
    method: str,
    payment_id: int,
    admin: User = Depends(require_permission('payments:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get details of a specific pending payment."""
    try:
        payment_method = PaymentMethod(method)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid payment method',
        )

    record = await get_payment_record(db, payment_method, payment_id)

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Payment not found',
        )

    return _record_to_response(record)


@router.post('/{method}/{payment_id}/check', response_model=ManualCheckResponse)
async def check_payment_status(
    method: str,
    payment_id: int,
    admin: User = Depends(require_permission('payments:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Manually check and update payment status."""
    try:
        payment_method = PaymentMethod(method)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid payment method',
        )

    # Get current record
    record = await get_payment_record(db, payment_method, payment_id)

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Payment not found',
        )

    # Check if manual check is available
    if not _is_checkable(record):
        return ManualCheckResponse(
            success=False,
            message='Ручная проверка недоступна для этого платежа',
            payment=_record_to_response(record),
            status_changed=False,
        )

    old_status = record.status
    old_is_paid = record.is_paid

    # Run manual check
    bot = create_bot()
    try:
        payment_service = PaymentService(bot=bot)
        updated = await run_manual_check(db, payment_method, payment_id, payment_service)
    finally:
        await bot.session.close()

    if not updated:
        return ManualCheckResponse(
            success=False,
            message='Не удалось проверить статус платежа',
            payment=_record_to_response(record),
            status_changed=False,
        )

    status_changed = updated.status != old_status or updated.is_paid != old_is_paid

    if status_changed:
        _, new_status_text = _get_status_info(updated)
        message = f'Статус обновлён: {new_status_text}'
        logger.info(
            'Admin checked payment /',
            admin_id=admin.id,
            method=method,
            payment_id=payment_id,
            old_status=old_status,
            status=updated.status,
        )
    else:
        message = 'Статус не изменился'

    return ManualCheckResponse(
        success=True,
        message=message,
        payment=_record_to_response(updated),
        status_changed=status_changed,
        old_status=old_status,
        new_status=updated.status,
    )
