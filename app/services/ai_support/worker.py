"""Deterministic worker shell with no model provider or customer delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.ai_support import AiSupportQueueCRUD, AiSupportQueueInvariantError
from app.database.models import (
    AiSupportRun,
    AiSupportRunDecision,
    AiSupportTicketState,
    TicketMessage,
)
from app.services.ai_support.policy import assess_customer_message
from app.services.ai_support.types import AiSupportDecision, AiSupportMode


class AiSupportWorkerStatus(StrEnum):
    DISABLED = 'disabled'
    IDLE = 'idle'
    ESCALATED = 'escalated'
    PROVIDER_UNAVAILABLE = 'provider_unavailable'


@dataclass(frozen=True, slots=True)
class AiSupportWorkerResult:
    status: AiSupportWorkerStatus
    reason_codes: tuple[str, ...]
    job_id: int | None = None
    run_id: int | None = None


class AiSupportWorker:
    """Claim and audit one job; never call a provider or write a reply."""

    @staticmethod
    def mode() -> AiSupportMode:
        return AiSupportMode(settings.AI_SUPPORT_MODE)

    async def process_next(self, db: AsyncSession) -> AiSupportWorkerResult:
        mode = self.mode()
        if mode is AiSupportMode.OFF:
            return AiSupportWorkerResult(AiSupportWorkerStatus.DISABLED, ('mode_off',))

        job = await AiSupportQueueCRUD.claim_next(db)
        if job is None:
            return AiSupportWorkerResult(AiSupportWorkerStatus.IDLE, ('queue_empty',))

        message_result = await db.execute(
            select(TicketMessage).where(TicketMessage.id == job.trigger_message_id)
        )
        message = message_result.scalar_one_or_none()
        if message is None:
            raise AiSupportQueueInvariantError('claimed job trigger message is missing')

        assessment = assess_customer_message(
            mode,
            message.message_text,
            has_media=message.has_media,
        )
        if assessment.decision is AiSupportDecision.ESCALATE:
            run_decision = AiSupportRunDecision.ESCALATE.value
            reason_codes = assessment.reason_codes
            terminal_reason = reason_codes[0]
            worker_status = AiSupportWorkerStatus.ESCALATED
        else:
            # Provider integration is intentionally absent in this foundation.
            run_decision = AiSupportRunDecision.ABSTAIN.value
            reason_codes = ('provider_not_configured',)
            terminal_reason = reason_codes[0]
            worker_status = AiSupportWorkerStatus.PROVIDER_UNAVAILABLE

        now = datetime.now(UTC)
        run = AiSupportRun(
            job_id=job.id,
            ticket_id=job.ticket_id,
            trigger_message_id=job.trigger_message_id,
            provider=None,
            model_id=None,
            prompt_version=settings.AI_SUPPORT_PROMPT_VERSION,
            kb_version=settings.AI_SUPPORT_KB_VERSION,
            decision=run_decision,
            sanitized_intent='deterministic_policy',
            reason_codes=list(reason_codes),
            latency_ms=0,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            estimated_cost_microusd=0,
            tool_events=[],
            started_at=now,
            completed_at=now,
            created_at=now,
        )
        db.add(run)
        await db.flush()

        await AiSupportQueueCRUD.mark_escalated(db, job, reason_code=terminal_reason, now=now)
        await db.execute(
            update(AiSupportTicketState)
            .where(AiSupportTicketState.ticket_id == job.ticket_id)
            .values(last_run_id=run.id, updated_at=now)
        )
        await db.flush()

        return AiSupportWorkerResult(
            worker_status,
            reason_codes,
            job_id=job.id,
            run_id=run.id,
        )


ai_support_worker = AiSupportWorker()
