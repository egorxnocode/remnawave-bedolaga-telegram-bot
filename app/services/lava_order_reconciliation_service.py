"""Authoritative reconciliation for direct one-time Lava service orders."""

from __future__ import annotations

import asyncio
import html
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram import Bot
from sqlalchemy import or_, select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import LavaPayment, LavaServiceOrder
from app.services.admin_notification_service import AdminNotificationService, NotificationCategory
from app.services.lava_service import lava_service
from app.services.payment_service import PaymentService


logger = structlog.get_logger(__name__)
PENDING_PAYMENT_STATUSES = {'created', 'pending', 'processing'}
OPEN_ORDER_STATUSES = {'created', 'pending', 'fulfilling'}


class LavaOrderReconciliationService:
    def __init__(self) -> None:
        self.interval_seconds = max(30, int(settings.LAVA_ORDER_RECONCILIATION_INTERVAL_SECONDS))
        self.batch_size = max(1, min(1000, int(settings.LAVA_ORDER_RECONCILIATION_BATCH_SIZE)))
        self.alert_after_attempts = max(1, int(settings.LAVA_ORDER_RECONCILIATION_ALERT_AFTER_ATTEMPTS))
        self._payment_service: PaymentService | None = None
        self._bot: Bot | None = None
        self._stop_event = asyncio.Event()

    def set_payment_service(self, service: PaymentService) -> None:
        self._payment_service = service
        self._bot = getattr(service, 'bot', None)

    def is_enabled(self) -> bool:
        return bool(settings.LAVA_ORDER_RECONCILIATION_ENABLED and settings.is_lava_enabled())

    def stop(self) -> None:
        self._stop_event.set()

    async def start(self) -> None:
        self._stop_event.clear()
        logger.info('Lava order reconciliation started', interval_seconds=self.interval_seconds)
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception('Lava order reconciliation cycle failed')
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass

    async def _candidate_ids(self) -> list[int]:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(LavaServiceOrder.id)
                .join(LavaPayment, LavaPayment.order_id == LavaServiceOrder.provider_order_id)
                .where(
                    LavaServiceOrder.payment_mode == 'one_time',
                    or_(
                        LavaServiceOrder.status.in_(OPEN_ORDER_STATUSES),
                        LavaPayment.is_paid.is_(True),
                    ),
                    or_(
                        LavaPayment.status.in_(PENDING_PAYMENT_STATUSES),
                        LavaPayment.is_paid.is_(True),
                    ),
                    or_(
                        LavaServiceOrder.status != 'fulfilled',
                        LavaPayment.transaction_id.is_(None),
                    ),
                )
                .order_by(LavaServiceOrder.created_at.asc())
                .limit(self.batch_size)
            )
            return list(result.scalars().all())

    async def run_once(self) -> dict[str, int]:
        stats = {'checked': 0, 'pending': 0, 'terminal': 0, 'fulfilled': 0, 'failed': 0}
        for order_id in await self._candidate_ids():
            outcome = await self._reconcile_order(order_id)
            stats['checked'] += 1
            stats[outcome] = stats.get(outcome, 0) + 1
        if stats['checked']:
            logger.info('Lava order reconciliation completed', **stats)
        return stats

    async def _load_pair(self, db: Any, order_id: int) -> tuple[LavaServiceOrder | None, LavaPayment | None]:
        row = (
            await db.execute(
                select(LavaServiceOrder, LavaPayment)
                .join(LavaPayment, LavaPayment.order_id == LavaServiceOrder.provider_order_id)
                .where(LavaServiceOrder.id == order_id)
            )
        ).one_or_none()
        return row if row is not None else (None, None)

    @staticmethod
    def _status_payload(payment: LavaPayment, response: dict[str, Any]) -> dict[str, Any]:
        data = response.get('data') or response
        return {
            'order_id': str(data.get('orderId') or data.get('order_id') or payment.order_id),
            'invoice_id': str(data.get('id') or data.get('invoiceId') or payment.lava_invoice_id or ''),
            'status': str(data.get('status') or '').strip().lower(),
            'amount': data.get('amount'),
            'credited': data.get('credited'),
            'pay_service': data.get('pay_service') or data.get('payService'),
            'pay_time': data.get('pay_time') or data.get('payTime'),
            'payer_details': data.get('payer_details') or data.get('payerDetails'),
            'custom_fields': data.get('custom_fields') or data.get('customFields'),
        }

    async def _reconcile_order(self, order_id: int) -> str:
        async with AsyncSessionLocal() as db:
            order, payment = await self._load_pair(db, order_id)
            if order is None or payment is None:
                return 'failed'
            order.last_reconciliation_at = datetime.now(UTC)
            try:
                if payment.is_paid:
                    service = self._payment_service or PaymentService(self._bot)
                    ok = await service.reconcile_paid_lava_payment(db, payment.id)
                    if not ok:
                        raise RuntimeError('Provider-paid Lava order was not fulfilled locally')
                    return 'fulfilled'

                response = await lava_service.get_invoice_status(
                    order_id=None if payment.lava_invoice_id else payment.order_id,
                    invoice_id=payment.lava_invoice_id,
                )
                payload = self._status_payload(payment, response)
                if not payload['status']:
                    raise RuntimeError('Lava status response has no invoice status')
                service = self._payment_service or PaymentService(self._bot)
                ok = await service.process_lava_callback(db, payload)
                if not ok:
                    raise RuntimeError(f"Lava status '{payload['status']}' was not applied")
                await db.refresh(order)
                order.reconciliation_attempts = 0
                order.last_reconciliation_error = None
                await db.commit()
                if order.status == 'fulfilled':
                    return 'fulfilled'
                if order.status in {'cancelled', 'failed'}:
                    return 'terminal'
                return 'pending'
            except Exception as error:
                await db.rollback()
                order, payment = await self._load_pair(db, order_id)
                if order is None:
                    return 'failed'
                order.reconciliation_attempts = int(order.reconciliation_attempts or 0) + 1
                order.last_reconciliation_at = datetime.now(UTC)
                order.last_reconciliation_error = str(error)[:2000]
                should_alert = (
                    order.reconciliation_attempts >= self.alert_after_attempts
                    and order.reconciliation_alerted_at is None
                )
                if should_alert:
                    order.reconciliation_alerted_at = datetime.now(UTC)
                await db.commit()
                logger.exception('Lava order reconciliation failed', order_id=order_id)
                if should_alert:
                    await self._send_alert(order, payment, error)
                return 'failed'

    async def _send_alert(
        self,
        order: LavaServiceOrder,
        payment: LavaPayment | None,
        error: Exception,
    ) -> None:
        if self._bot is None:
            return
        paid_marker = 'ДА' if payment and payment.is_paid else 'нет'
        text = (
            '🚨 <b>LAVA: ЗАКАЗ ТРЕБУЕТ ПРОВЕРКИ</b>\n\n'
            f'Заказ: <code>{order.id}</code>\n'
            f'Пользователь: <code>{order.user_id}</code>\n'
            f'Тип: <code>{html.escape(order.kind)}</code>\n'
            f'Оплачен у провайдера: <b>{paid_marker}</b>\n'
            f'Попыток: <b>{order.reconciliation_attempts}</b>\n'
            f'Ошибка: <code>{html.escape(str(error)[:1000])}</code>\n\n'
            'Не выполняйте услугу вручную до проверки заказа и транзакции.'
        )
        try:
            await AdminNotificationService(self._bot).send_admin_notification(
                text,
                category=NotificationCategory.ERRORS,
            )
        except Exception:
            logger.exception('Could not send Lava reconciliation alert', order_id=order.id)


lava_order_reconciliation_service = LavaOrderReconciliationService()
