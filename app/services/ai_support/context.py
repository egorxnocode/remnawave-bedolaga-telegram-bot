"""Minimized read-only database context for provider-eligible support jobs."""

from __future__ import annotations

from datetime import UTC

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Subscription,
    SubscriptionStatus,
    Tariff,
    Ticket,
    TicketMessage,
    TicketMessageAuthorKind,
    Transaction,
    TransactionType,
)
from app.services.ai_support.contracts import SafeCustomerContext, SafePaymentContext
from app.services.ai_support.policy import redact_customer_text


class AiSupportContextAuthorizationError(RuntimeError):
    """Raised when a trigger message cannot be proven to belong to its ticket user."""


_PAYMENT_TRANSACTION_TYPES = (
    TransactionType.DEPOSIT.value,
    TransactionType.SUBSCRIPTION_PAYMENT.value,
    TransactionType.GIFT_PAYMENT.value,
)


def _gb_to_bytes(value: object) -> int | None:
    if value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount < 0:
        return None
    return int(amount * 1024**3)


def _safe_device_limit(value: object) -> int | None:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return None
    return limit if 0 <= limit <= 100 else None


def _safe_tariff_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 80 or redact_customer_text(normalized).changed:
        return None
    return normalized


def _subscription_status(value: object) -> str:
    mapping = {
        SubscriptionStatus.TRIAL.value: 'trial',
        SubscriptionStatus.ACTIVE.value: 'active',
        SubscriptionStatus.EXPIRED.value: 'expired',
        SubscriptionStatus.PENDING.value: 'pending',
    }
    return mapping.get(value, 'unknown')


class AiSupportDatabaseContextCollector:
    """Select only reviewed scalar fields and never expose local identifiers."""

    async def collect(
        self,
        db: AsyncSession,
        *,
        ticket_id: int,
        trigger_message_id: int,
    ) -> SafeCustomerContext:
        user_id = await self._authorized_user_id(
            db,
            ticket_id=ticket_id,
            trigger_message_id=trigger_message_id,
        )
        subscription = await self._subscription_row(db, user_id=user_id)
        payment = await self._latest_successful_payment(db, user_id=user_id)

        if subscription is None:
            return SafeCustomerContext(subscription_status='none', latest_payment=payment)

        return SafeCustomerContext(
            subscription_status=_subscription_status(subscription.status),
            tariff_name=_safe_tariff_name(subscription.tariff_name),
            expires_on=subscription.end_date.date() if subscription.end_date is not None else None,
            traffic_used_bytes=_gb_to_bytes(subscription.traffic_used_gb),
            traffic_limit_bytes=_gb_to_bytes(subscription.traffic_limit_gb),
            # BEDOLAGA stores the purchased limit, not an authoritative current
            # HWID count. A future Remnawave tool must be designed separately.
            device_count=None,
            device_limit=_safe_device_limit(subscription.device_limit),
            platform='unknown',
            latest_payment=payment,
        )

    @staticmethod
    async def _authorized_user_id(
        db: AsyncSession,
        *,
        ticket_id: int,
        trigger_message_id: int,
    ) -> int:
        statement = (
            select(Ticket.user_id)
            .join(TicketMessage, TicketMessage.ticket_id == Ticket.id)
            .where(
                Ticket.id == ticket_id,
                TicketMessage.id == trigger_message_id,
                TicketMessage.user_id == Ticket.user_id,
                TicketMessage.author_kind == TicketMessageAuthorKind.USER.value,
            )
        )
        result = await db.execute(statement)
        user_id = result.scalar_one_or_none()
        if user_id is None:
            raise AiSupportContextAuthorizationError('trigger message is not authorized for ticket context')
        return user_id

    @staticmethod
    async def _subscription_row(db: AsyncSession, *, user_id: int):
        priority = case(
            (Subscription.status == SubscriptionStatus.ACTIVE.value, 0),
            (Subscription.status == SubscriptionStatus.TRIAL.value, 1),
            (Subscription.status == SubscriptionStatus.LIMITED.value, 2),
            (Subscription.status == SubscriptionStatus.PENDING.value, 3),
            (Subscription.status == SubscriptionStatus.EXPIRED.value, 4),
            else_=5,
        )
        statement = (
            select(
                Subscription.status,
                Subscription.end_date,
                Subscription.traffic_used_gb,
                Subscription.traffic_limit_gb,
                Subscription.device_limit,
                Tariff.name.label('tariff_name'),
            )
            .outerjoin(Tariff, Tariff.id == Subscription.tariff_id)
            .where(Subscription.user_id == user_id)
            .order_by(priority, Subscription.created_at.desc())
            .limit(1)
        )
        result = await db.execute(statement)
        return result.one_or_none()

    @staticmethod
    async def _latest_successful_payment(
        db: AsyncSession,
        *,
        user_id: int,
    ) -> SafePaymentContext | None:
        statement = (
            select(
                Transaction.payment_method,
                Transaction.amount_kopeks,
                Transaction.completed_at,
                Transaction.created_at,
            )
            .where(
                Transaction.user_id == user_id,
                Transaction.type.in_(_PAYMENT_TRANSACTION_TYPES),
                Transaction.is_completed.is_(True),
                Transaction.payment_method.is_not(None),
                Transaction.amount_kopeks >= 0,
            )
            .order_by(Transaction.created_at.desc())
            .limit(1)
        )
        result = await db.execute(statement)
        row = result.one_or_none()
        if row is None:
            return None

        observed_at = row.completed_at or row.created_at
        if observed_at is None:
            return None
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        provider = str(row.payment_method).strip().lower()
        try:
            return SafePaymentContext(
                provider=provider,
                status='succeeded',
                amount_kopeks=int(row.amount_kopeks),
                observed_at=observed_at,
            )
        except (TypeError, ValueError):
            return None


ai_support_database_context_collector = AiSupportDatabaseContextCollector()
