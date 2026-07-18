"""Shared transaction hook for every support-ticket write channel."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.ai_support import AiSupportQueueCRUD
from app.database.models import TicketMessage
from app.services.ai_support.types import AiSupportMode


logger = structlog.get_logger(__name__)


class AiSupportDispatchStatus(StrEnum):
    DISABLED = 'disabled'
    ENQUEUED = 'enqueued'
    SKIPPED = 'skipped'
    HUMAN_OWNED = 'human_owned'
    ERROR = 'error'


@dataclass(frozen=True, slots=True)
class AiSupportDispatchResult:
    status: AiSupportDispatchStatus
    reason_code: str
    job_id: int | None = None
    jobs_abstained: int = 0


class AiSupportDispatchService:
    """Persist queue/ownership changes inside the caller's ticket transaction."""

    @staticmethod
    def mode() -> AiSupportMode:
        return AiSupportMode(settings.AI_SUPPORT_MODE)

    async def on_user_message(
        self,
        db: AsyncSession,
        *,
        ticket_id: int,
        message: TicketMessage,
        channel: str,
    ) -> AiSupportDispatchResult:
        if self.mode() is AiSupportMode.OFF:
            return AiSupportDispatchResult(AiSupportDispatchStatus.DISABLED, 'mode_off')

        await db.flush()
        message_id = message.id
        if message_id is None:
            raise RuntimeError('ticket message did not receive an id after flush')

        try:
            async with db.begin_nested():
                outcome = await AiSupportQueueCRUD.enqueue_if_eligible(
                    db,
                    ticket_id=ticket_id,
                    trigger_message_id=message_id,
                    channel=channel,
                )
        except SQLAlchemyError as error:
            logger.warning(
                'AI support enqueue failed; ticket remains human-visible',
                ticket_id=ticket_id,
                message_id=message_id,
                error_type=type(error).__name__,
            )
            return AiSupportDispatchResult(AiSupportDispatchStatus.ERROR, 'queue_error')

        if outcome.job is None:
            return AiSupportDispatchResult(AiSupportDispatchStatus.SKIPPED, outcome.reason_code)
        return AiSupportDispatchResult(
            AiSupportDispatchStatus.ENQUEUED,
            outcome.reason_code,
            job_id=outcome.job.id,
        )

    async def on_human_reply(
        self,
        db: AsyncSession,
        *,
        ticket_id: int,
        reason_code: str = 'human_reply',
    ) -> AiSupportDispatchResult:
        if self.mode() is AiSupportMode.OFF:
            return AiSupportDispatchResult(AiSupportDispatchStatus.DISABLED, 'mode_off')

        try:
            async with db.begin_nested():
                outcome = await AiSupportQueueCRUD.take_over_ticket(
                    db,
                    ticket_id=ticket_id,
                    reason_code=reason_code,
                )
        except SQLAlchemyError as error:
            logger.error(
                'AI support human takeover persistence failed',
                ticket_id=ticket_id,
                error_type=type(error).__name__,
            )
            return AiSupportDispatchResult(AiSupportDispatchStatus.ERROR, 'takeover_error')

        return AiSupportDispatchResult(
            AiSupportDispatchStatus.HUMAN_OWNED,
            reason_code,
            jobs_abstained=outcome.jobs_abstained,
        )


ai_support_dispatch_service = AiSupportDispatchService()
