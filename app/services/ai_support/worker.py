"""Fail-closed shadow worker that can persist operator-only AI drafts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.ai_support import AiSupportQueueCRUD, AiSupportQueueInvariantError
from app.database.crud.ai_support_drafts import AiSupportDraftCRUD
from app.database.models import (
    AiSupportRun,
    AiSupportRunDecision,
    AiSupportTicketState,
    TicketMessage,
)
from app.services.ai_support.budget import AiSupportBudgetGuard, ai_support_budget_guard
from app.services.ai_support.context import (
    AiSupportContextAuthorizationError,
    AiSupportDatabaseContextCollector,
    ai_support_database_context_collector,
)
from app.services.ai_support.contracts import AiSupportContextBuilder, AiSupportContractError
from app.services.ai_support.knowledge import (
    AiSupportKnowledgeError,
    AiSupportKnowledgePackage,
    load_ai_support_knowledge,
)
from app.services.ai_support.policy import assess_customer_message
from app.services.ai_support.provider import (
    AiSupportProviderError,
    AnthropicSupportProvider,
    anthropic_support_provider,
)
from app.services.ai_support.types import AiSupportDecision, AiSupportMode


class AiSupportWorkerStatus(StrEnum):
    DISABLED = 'disabled'
    IDLE = 'idle'
    ESCALATED = 'escalated'
    PROVIDER_UNAVAILABLE = 'provider_unavailable'
    DRAFT_CREATED = 'draft_created'


@dataclass(frozen=True, slots=True)
class AiSupportWorkerResult:
    status: AiSupportWorkerStatus
    reason_codes: tuple[str, ...]
    job_id: int | None = None
    run_id: int | None = None


class AiSupportWorker:
    """Claim one job and persist, but never deliver, a validated answer."""

    def __init__(
        self,
        *,
        provider: AnthropicSupportProvider = anthropic_support_provider,
        context_collector: AiSupportDatabaseContextCollector = ai_support_database_context_collector,
        budget_guard: AiSupportBudgetGuard = ai_support_budget_guard,
        knowledge_loader: Callable[[], AiSupportKnowledgePackage] = load_ai_support_knowledge,
    ) -> None:
        self._provider = provider
        self._context_collector = context_collector
        self._budget_guard = budget_guard
        self._knowledge_loader = knowledge_loader

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

        message_result = await db.execute(select(TicketMessage).where(TicketMessage.id == job.trigger_message_id))
        message = message_result.scalar_one_or_none()
        if message is None:
            raise AiSupportQueueInvariantError('claimed job trigger message is missing')

        assessment = assess_customer_message(
            mode,
            message.message_text,
            has_media=message.has_media,
        )
        started_at = datetime.now(UTC)
        if assessment.decision is AiSupportDecision.ESCALATE:
            return await self._escalate(
                db,
                job=job,
                decision=AiSupportRunDecision.ESCALATE.value,
                reason_codes=assessment.reason_codes,
                status=AiSupportWorkerStatus.ESCALATED,
                started_at=started_at,
            )

        try:
            knowledge = self._knowledge_loader()
            context = await self._context_collector.collect(
                db,
                ticket_id=job.ticket_id,
                trigger_message_id=job.trigger_message_id,
            )
            builder = AiSupportContextBuilder(knowledge)
            request = builder.build_request(assessment, context)
            budget = await self._budget_guard.check(
                db,
                projected_tokens=self._projected_tokens(request.model_dump_json()),
            )
            if not budget.allowed:
                return await self._escalate(
                    db,
                    job=job,
                    decision=AiSupportRunDecision.ABSTAIN.value,
                    reason_codes=(budget.reason_code,),
                    status=AiSupportWorkerStatus.PROVIDER_UNAVAILABLE,
                    started_at=started_at,
                )
            response = await self._provider.generate(request, builder)
            builder.validate_result(response.result)
        except AiSupportProviderError as error:
            return await self._escalate(
                db,
                job=job,
                decision=AiSupportRunDecision.ERROR.value,
                reason_codes=(error.code,),
                status=AiSupportWorkerStatus.PROVIDER_UNAVAILABLE,
                started_at=started_at,
            )
        except AiSupportContextAuthorizationError:
            return await self._escalate(
                db,
                job=job,
                decision=AiSupportRunDecision.ERROR.value,
                reason_codes=('context_authorization_failed',),
                status=AiSupportWorkerStatus.ESCALATED,
                started_at=started_at,
            )
        except AiSupportKnowledgeError:
            return await self._escalate(
                db,
                job=job,
                decision=AiSupportRunDecision.ERROR.value,
                reason_codes=('knowledge_unavailable',),
                status=AiSupportWorkerStatus.PROVIDER_UNAVAILABLE,
                started_at=started_at,
            )
        except AiSupportContractError:
            return await self._escalate(
                db,
                job=job,
                decision=AiSupportRunDecision.ERROR.value,
                reason_codes=('contract_rejected',),
                status=AiSupportWorkerStatus.ESCALATED,
                started_at=started_at,
            )

        result = response.result
        reason_codes = result.reason_codes or (
            ('answer_ready',) if result.decision == 'answer' else ('provider_escalated',)
        )
        if result.decision == 'escalate':
            return await self._escalate(
                db,
                job=job,
                decision=AiSupportRunDecision.ESCALATE.value,
                reason_codes=reason_codes,
                status=AiSupportWorkerStatus.ESCALATED,
                started_at=started_at,
                provider_response=response,
            )

        if not await AiSupportQueueCRUD.lock_ticket_for_ai_delivery(db, ticket_id=job.ticket_id):
            return await self._escalate(
                db,
                job=job,
                decision=AiSupportRunDecision.ABSTAIN.value,
                reason_codes=('human_takeover',),
                status=AiSupportWorkerStatus.ESCALATED,
                started_at=started_at,
                provider_response=response,
            )

        completed_at = datetime.now(UTC)
        run = self._new_run(
            job=job,
            decision=AiSupportRunDecision.ANSWER.value,
            reason_codes=reason_codes,
            started_at=started_at,
            completed_at=completed_at,
            provider_response=response,
        )
        db.add(run)
        await db.flush()
        await AiSupportDraftCRUD.create(
            db,
            run_id=run.id,
            ticket_id=job.ticket_id,
            trigger_message_id=job.trigger_message_id,
            answer_text=result.answer_text,
            citations=result.citations,
            now=completed_at,
        )
        await AiSupportQueueCRUD.mark_completed(db, job, now=completed_at)
        await self._record_last_run(db, job.ticket_id, run.id, completed_at)
        return AiSupportWorkerResult(
            AiSupportWorkerStatus.DRAFT_CREATED,
            reason_codes,
            job_id=job.id,
            run_id=run.id,
        )

    async def _escalate(
        self,
        db: AsyncSession,
        *,
        job,
        decision: str,
        reason_codes: tuple[str, ...],
        status: AiSupportWorkerStatus,
        started_at: datetime,
        provider_response=None,
    ) -> AiSupportWorkerResult:
        completed_at = datetime.now(UTC)
        run = self._new_run(
            job=job,
            decision=decision,
            reason_codes=reason_codes,
            started_at=started_at,
            completed_at=completed_at,
            provider_response=provider_response,
        )
        db.add(run)
        await db.flush()
        await AiSupportQueueCRUD.mark_escalated(
            db,
            job,
            reason_code=reason_codes[0],
            now=completed_at,
        )
        await self._record_last_run(db, job.ticket_id, run.id, completed_at)
        return AiSupportWorkerResult(status, reason_codes, job_id=job.id, run_id=run.id)

    @staticmethod
    def _new_run(
        *,
        job,
        decision: str,
        reason_codes: tuple[str, ...],
        started_at: datetime,
        completed_at: datetime,
        provider_response=None,
    ) -> AiSupportRun:
        usage = provider_response.usage if provider_response is not None else None
        return AiSupportRun(
            job_id=job.id,
            ticket_id=job.ticket_id,
            trigger_message_id=job.trigger_message_id,
            provider=provider_response.provider if provider_response is not None else None,
            model_id=provider_response.model_id if provider_response is not None else None,
            prompt_version=settings.AI_SUPPORT_PROMPT_VERSION,
            kb_version=settings.AI_SUPPORT_KB_VERSION,
            decision=decision,
            sanitized_intent='shadow_draft' if decision == AiSupportRunDecision.ANSWER.value else 'handoff',
            reason_codes=list(reason_codes),
            latency_ms=provider_response.latency_ms if provider_response is not None else 0,
            input_tokens=usage.input_tokens if usage is not None else 0,
            output_tokens=usage.output_tokens if usage is not None else 0,
            cache_read_tokens=usage.cache_read_tokens if usage is not None else 0,
            cache_write_tokens=usage.cache_write_tokens if usage is not None else 0,
            estimated_cost_microusd=0,
            tool_events=[],
            started_at=started_at,
            completed_at=completed_at,
            created_at=completed_at,
        )

    @staticmethod
    async def _record_last_run(db: AsyncSession, ticket_id: int, run_id: int, now: datetime) -> None:
        await db.execute(
            update(AiSupportTicketState)
            .where(AiSupportTicketState.ticket_id == ticket_id)
            .values(last_run_id=run_id, updated_at=now)
        )
        await db.flush()

    @staticmethod
    def _projected_tokens(request_json: str) -> int:
        estimated_input = max(1, (len(request_json.encode('utf-8')) + 1) // 2)
        return estimated_input + settings.AI_SUPPORT_PROVIDER_MAX_TOKENS


ai_support_worker = AiSupportWorker()
