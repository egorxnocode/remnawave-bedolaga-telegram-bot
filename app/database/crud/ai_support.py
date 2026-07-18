"""Transactional persistence operations for the durable AI support queue.

Every method deliberately leaves commit/rollback to its caller so ticket writes,
queueing and human takeover can share one database transaction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AiSupportJob,
    AiSupportJobStatus,
    AiSupportTicketState,
    AiSupportTicketStateValue,
)


_SAFE_CODE_RE = re.compile(r'^[a-z][a-z0-9_.-]{0,63}$')
_ELIGIBLE_TICKET_STATES = (
    AiSupportTicketStateValue.ACTIVE.value,
    AiSupportTicketStateValue.PROCESSING.value,
)
_ACTIVE_JOB_STATUSES = (
    AiSupportJobStatus.PENDING.value,
    AiSupportJobStatus.PROCESSING.value,
)


class AiSupportQueueInvariantError(RuntimeError):
    """Raised when persisted queue data violates an expected relationship."""


class AiSupportJobTransitionError(RuntimeError):
    """Raised when a job transition is attempted from an invalid state."""


@dataclass(frozen=True, slots=True)
class AiSupportEnqueueResult:
    job: AiSupportJob | None
    created: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class AiSupportHumanTakeoverResult:
    state: AiSupportTicketState
    jobs_abstained: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_code(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if not _SAFE_CODE_RE.fullmatch(normalized):
        raise ValueError(f'{field} must match {_SAFE_CODE_RE.pattern}')
    return normalized


class AiSupportQueueCRUD:
    """Queue operations with explicit row-locking and no implicit commit."""

    @staticmethod
    async def _lock_ticket_state(db: AsyncSession, *, ticket_id: int) -> AiSupportTicketState:
        result = await db.execute(
            select(AiSupportTicketState).where(AiSupportTicketState.ticket_id == ticket_id).with_for_update()
        )
        state = result.scalar_one_or_none()
        if state is None:
            raise AiSupportQueueInvariantError('ticket AI state is missing')
        return state

    @classmethod
    async def _lock_worker_owned_ticket(cls, db: AsyncSession, *, ticket_id: int) -> AiSupportTicketState:
        state = await cls._lock_ticket_state(db, ticket_id=ticket_id)
        if state.state not in _ELIGIBLE_TICKET_STATES:
            raise AiSupportJobTransitionError(f'ticket is no longer AI-owned: {state.state}')
        return state

    @staticmethod
    async def enqueue_if_eligible(
        db: AsyncSession,
        *,
        ticket_id: int,
        trigger_message_id: int,
        channel: str,
        now: datetime | None = None,
    ) -> AiSupportEnqueueResult:
        """Serialize enqueue against human takeover using the ticket-state row."""
        safe_channel = _safe_code(channel, field='channel')
        current_time = now or _utc_now()

        ensure_state = (
            pg_insert(AiSupportTicketState)
            .values(
                ticket_id=ticket_id,
                state=AiSupportTicketStateValue.ACTIVE.value,
                created_at=current_time,
                updated_at=current_time,
            )
            .on_conflict_do_nothing(index_elements=[AiSupportTicketState.ticket_id])
        )
        await db.execute(ensure_state)

        state_result = await db.execute(
            select(AiSupportTicketState).where(AiSupportTicketState.ticket_id == ticket_id).with_for_update()
        )
        ticket_state = state_result.scalar_one()
        if ticket_state.state not in _ELIGIBLE_TICKET_STATES:
            return AiSupportEnqueueResult(None, False, f'ticket_{ticket_state.state}')

        insert_job = (
            pg_insert(AiSupportJob)
            .values(
                ticket_id=ticket_id,
                trigger_message_id=trigger_message_id,
                channel=safe_channel,
                status=AiSupportJobStatus.PENDING.value,
                attempt_count=0,
                available_at=current_time,
                created_at=current_time,
                updated_at=current_time,
            )
            .on_conflict_do_nothing(index_elements=[AiSupportJob.trigger_message_id])
            .returning(AiSupportJob)
        )
        insert_result = await db.execute(insert_job)
        job = insert_result.scalar_one_or_none()
        created = job is not None
        if job is None:
            existing_result = await db.execute(
                select(AiSupportJob).where(AiSupportJob.trigger_message_id == trigger_message_id)
            )
            job = existing_result.scalar_one()
            if job.ticket_id != ticket_id:
                raise AiSupportQueueInvariantError('trigger message belongs to a different ticket job')

        if job.status not in _ACTIVE_JOB_STATUSES:
            return AiSupportEnqueueResult(job, False, 'duplicate_terminal')

        ticket_state.state = AiSupportTicketStateValue.PROCESSING.value
        ticket_state.last_trigger_message_id = trigger_message_id
        ticket_state.updated_at = current_time
        await db.flush()
        return AiSupportEnqueueResult(job, created, 'created' if created else 'duplicate_active')

    @staticmethod
    def build_claim_statement(*, now: datetime, stale_before: datetime) -> Select[tuple[AiSupportJob]]:
        """Build the PostgreSQL claim query for inspection and deterministic tests."""
        return (
            select(AiSupportJob)
            .join(AiSupportTicketState, AiSupportTicketState.ticket_id == AiSupportJob.ticket_id)
            .where(
                AiSupportTicketState.state.in_(_ELIGIBLE_TICKET_STATES),
                or_(
                    and_(
                        AiSupportJob.status == AiSupportJobStatus.PENDING.value,
                        AiSupportJob.available_at <= now,
                    ),
                    and_(
                        AiSupportJob.status == AiSupportJobStatus.PROCESSING.value,
                        AiSupportJob.locked_at.is_not(None),
                        AiSupportJob.locked_at <= stale_before,
                    ),
                ),
            )
            .order_by(AiSupportJob.available_at, AiSupportJob.created_at, AiSupportJob.id)
            .with_for_update(of=AiSupportJob, skip_locked=True)
            .limit(1)
        )

    @classmethod
    async def claim_next(
        cls,
        db: AsyncSession,
        *,
        now: datetime | None = None,
        lease_timeout: timedelta = timedelta(minutes=2),
    ) -> AiSupportJob | None:
        """Claim one ready or stale job without blocking another worker."""
        if lease_timeout <= timedelta(0):
            raise ValueError('lease_timeout must be positive')
        current_time = now or _utc_now()
        statement = cls.build_claim_statement(
            now=current_time,
            stale_before=current_time - lease_timeout,
        )
        result = await db.execute(statement)
        job = result.scalar_one_or_none()
        if job is None:
            return None

        job.status = AiSupportJobStatus.PROCESSING.value
        job.attempt_count += 1
        job.locked_at = current_time
        job.last_error_code = None
        job.updated_at = current_time
        await db.flush()
        return job

    @classmethod
    async def lock_ticket_for_ai_delivery(cls, db: AsyncSession, *, ticket_id: int) -> bool:
        """Lock ownership immediately before an AI draft/message is persisted."""
        try:
            state = await cls._lock_ticket_state(db, ticket_id=ticket_id)
        except AiSupportQueueInvariantError:
            return False
        return state.state in _ELIGIBLE_TICKET_STATES

    @staticmethod
    async def schedule_retry(
        db: AsyncSession,
        job: AiSupportJob,
        *,
        error_code: str,
        available_at: datetime,
        max_attempts: int,
        now: datetime | None = None,
    ) -> bool:
        """Schedule a bounded retry; exhausted jobs fail closed to escalation."""
        if max_attempts < 1:
            raise ValueError('max_attempts must be positive')
        if job.status != AiSupportJobStatus.PROCESSING.value:
            raise AiSupportJobTransitionError(f'cannot retry job from {job.status}')

        safe_error_code = _safe_code(error_code, field='error_code')
        current_time = now or _utc_now()
        await AiSupportQueueCRUD._lock_worker_owned_ticket(db, ticket_id=job.ticket_id)
        will_retry = job.attempt_count < max_attempts
        next_status = AiSupportJobStatus.PENDING.value if will_retry else AiSupportJobStatus.FAILED.value
        transition_result = await db.execute(
            update(AiSupportJob)
            .where(
                AiSupportJob.id == job.id,
                AiSupportJob.status == AiSupportJobStatus.PROCESSING.value,
            )
            .values(
                status=next_status,
                available_at=available_at if will_retry else job.available_at,
                locked_at=None,
                last_error_code=safe_error_code,
                updated_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        if transition_result.rowcount != 1:
            raise AiSupportJobTransitionError('job was taken over before retry transition')

        job.status = next_status
        job.last_error_code = safe_error_code
        job.locked_at = None
        job.updated_at = current_time
        if will_retry:
            job.available_at = available_at
            await db.flush()
            return True

        await db.execute(
            update(AiSupportTicketState)
            .where(
                AiSupportTicketState.ticket_id == job.ticket_id,
                AiSupportTicketState.state != AiSupportTicketStateValue.HUMAN_OWNED.value,
            )
            .values(
                state=AiSupportTicketStateValue.ESCALATED.value,
                takeover_reason='retry_exhausted',
                updated_at=current_time,
            )
        )
        await db.flush()
        return False

    @staticmethod
    async def mark_completed(
        db: AsyncSession,
        job: AiSupportJob,
        *,
        now: datetime | None = None,
    ) -> None:
        """Complete a job and release the ticket only if it is still the latest job."""
        if job.status != AiSupportJobStatus.PROCESSING.value:
            raise AiSupportJobTransitionError(f'cannot complete job from {job.status}')
        current_time = now or _utc_now()
        await AiSupportQueueCRUD._lock_worker_owned_ticket(db, ticket_id=job.ticket_id)
        transition_result = await db.execute(
            update(AiSupportJob)
            .where(
                AiSupportJob.id == job.id,
                AiSupportJob.status == AiSupportJobStatus.PROCESSING.value,
            )
            .values(
                status=AiSupportJobStatus.COMPLETED.value,
                locked_at=None,
                updated_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        if transition_result.rowcount != 1:
            raise AiSupportJobTransitionError('job was taken over before completion')

        job.status = AiSupportJobStatus.COMPLETED.value
        job.locked_at = None
        job.updated_at = current_time
        await db.execute(
            update(AiSupportTicketState)
            .where(
                AiSupportTicketState.ticket_id == job.ticket_id,
                AiSupportTicketState.state == AiSupportTicketStateValue.PROCESSING.value,
                AiSupportTicketState.last_trigger_message_id == job.trigger_message_id,
            )
            .values(state=AiSupportTicketStateValue.ACTIVE.value, updated_at=current_time)
        )
        await db.flush()

    @staticmethod
    async def mark_escalated(
        db: AsyncSession,
        job: AiSupportJob,
        *,
        reason_code: str,
        now: datetime | None = None,
    ) -> None:
        """Persist a deterministic handoff without overwriting human ownership."""
        if job.status not in _ACTIVE_JOB_STATUSES:
            raise AiSupportJobTransitionError(f'cannot escalate job from {job.status}')
        safe_reason = _safe_code(reason_code, field='reason_code')
        current_time = now or _utc_now()
        await AiSupportQueueCRUD._lock_worker_owned_ticket(db, ticket_id=job.ticket_id)
        transition_result = await db.execute(
            update(AiSupportJob)
            .where(
                AiSupportJob.id == job.id,
                AiSupportJob.status.in_(_ACTIVE_JOB_STATUSES),
            )
            .values(
                status=AiSupportJobStatus.ESCALATED.value,
                locked_at=None,
                last_error_code=safe_reason,
                updated_at=current_time,
            )
            .execution_options(synchronize_session=False)
        )
        if transition_result.rowcount != 1:
            raise AiSupportJobTransitionError('job was taken over before escalation')

        job.status = AiSupportJobStatus.ESCALATED.value
        job.last_error_code = safe_reason
        job.locked_at = None
        job.updated_at = current_time
        await db.execute(
            update(AiSupportTicketState)
            .where(
                AiSupportTicketState.ticket_id == job.ticket_id,
                AiSupportTicketState.state != AiSupportTicketStateValue.HUMAN_OWNED.value,
            )
            .values(
                state=AiSupportTicketStateValue.ESCALATED.value,
                takeover_reason=safe_reason,
                updated_at=current_time,
            )
        )
        await db.flush()

    @staticmethod
    async def take_over_ticket(
        db: AsyncSession,
        *,
        ticket_id: int,
        reason_code: str = 'human_reply',
        now: datetime | None = None,
    ) -> AiSupportHumanTakeoverResult:
        """Atomically transfer ownership and make queued/claimed work abstain."""
        safe_reason = _safe_code(reason_code, field='reason_code')
        current_time = now or _utc_now()
        upsert_state = (
            pg_insert(AiSupportTicketState)
            .values(
                ticket_id=ticket_id,
                state=AiSupportTicketStateValue.HUMAN_OWNED.value,
                taken_over_at=current_time,
                takeover_reason=safe_reason,
                created_at=current_time,
                updated_at=current_time,
            )
            .on_conflict_do_update(
                index_elements=[AiSupportTicketState.ticket_id],
                set_={
                    'state': AiSupportTicketStateValue.HUMAN_OWNED.value,
                    'taken_over_at': current_time,
                    'takeover_reason': safe_reason,
                    'updated_at': current_time,
                },
            )
            .returning(AiSupportTicketState)
        )
        state_result = await db.execute(upsert_state)
        state = state_result.scalar_one()

        abstain_result = await db.execute(
            update(AiSupportJob)
            .where(
                AiSupportJob.ticket_id == ticket_id,
                AiSupportJob.status.in_(_ACTIVE_JOB_STATUSES),
            )
            .values(
                status=AiSupportJobStatus.ESCALATED.value,
                locked_at=None,
                last_error_code='human_takeover',
                updated_at=current_time,
            )
        )
        await db.flush()
        return AiSupportHumanTakeoverResult(state, int(abstain_result.rowcount or 0))
