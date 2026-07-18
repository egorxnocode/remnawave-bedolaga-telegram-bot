from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.database.crud.ai_support import (
    AiSupportJobTransitionError,
    AiSupportQueueCRUD,
    AiSupportQueueInvariantError,
)
from app.database.models import AiSupportJobStatus, AiSupportTicketStateValue


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _result(*, one=None, optional=None, rowcount: int = 0) -> MagicMock:
    result = MagicMock()
    result.scalar_one.return_value = one
    result.scalar_one_or_none.return_value = optional
    result.rowcount = rowcount
    return result


def _db(*results: MagicMock):
    return SimpleNamespace(
        execute=AsyncMock(side_effect=list(results)),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={'literal_binds': True},
        )
    )


@pytest.mark.asyncio
async def test_enqueue_serializes_on_ticket_state_and_is_idempotent() -> None:
    state = SimpleNamespace(state='active', last_trigger_message_id=None, updated_at=None)
    job = SimpleNamespace(ticket_id=7, trigger_message_id=11, status='pending')
    db = _db(_result(), _result(one=state), _result(optional=job))

    outcome = await AiSupportQueueCRUD.enqueue_if_eligible(
        db,
        ticket_id=7,
        trigger_message_id=11,
        channel='cabinet',
        now=NOW,
    )

    assert outcome.job is job
    assert outcome.created is True
    assert outcome.reason_code == 'created'
    assert state.state == AiSupportTicketStateValue.PROCESSING.value
    assert state.last_trigger_message_id == 11
    assert 'ON CONFLICT' in _sql(db.execute.await_args_list[0].args[0])
    assert 'FOR UPDATE' in _sql(db.execute.await_args_list[1].args[0])
    job_sql = _sql(db.execute.await_args_list[2].args[0])
    assert 'ON CONFLICT (trigger_message_id) DO NOTHING' in job_sql
    assert 'RETURNING' in job_sql
    db.flush.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_reuses_active_duplicate() -> None:
    state = SimpleNamespace(state='processing', last_trigger_message_id=10, updated_at=None)
    job = SimpleNamespace(ticket_id=7, trigger_message_id=11, status='processing')
    db = _db(_result(), _result(one=state), _result(optional=None), _result(one=job))

    outcome = await AiSupportQueueCRUD.enqueue_if_eligible(
        db,
        ticket_id=7,
        trigger_message_id=11,
        channel='telegram',
        now=NOW,
    )

    assert outcome.job is job
    assert outcome.created is False
    assert outcome.reason_code == 'duplicate_active'
    assert db.execute.await_count == 4


@pytest.mark.asyncio
async def test_enqueue_does_not_reopen_terminal_duplicate() -> None:
    state = SimpleNamespace(state='active', last_trigger_message_id=10, updated_at=None)
    job = SimpleNamespace(ticket_id=7, trigger_message_id=11, status='completed')
    db = _db(_result(), _result(one=state), _result(optional=None), _result(one=job))

    outcome = await AiSupportQueueCRUD.enqueue_if_eligible(
        db,
        ticket_id=7,
        trigger_message_id=11,
        channel='cabinet',
        now=NOW,
    )

    assert outcome.reason_code == 'duplicate_terminal'
    assert state.state == 'active'
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_stops_after_human_takeover() -> None:
    state = SimpleNamespace(state='human_owned')
    db = _db(_result(), _result(one=state))

    outcome = await AiSupportQueueCRUD.enqueue_if_eligible(
        db,
        ticket_id=7,
        trigger_message_id=11,
        channel='cabinet',
        now=NOW,
    )

    assert outcome.job is None
    assert outcome.reason_code == 'ticket_human_owned'
    assert db.execute.await_count == 2
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_rejects_cross_ticket_duplicate() -> None:
    state = SimpleNamespace(state='active')
    other_job = SimpleNamespace(ticket_id=8, trigger_message_id=11, status='pending')
    db = _db(_result(), _result(one=state), _result(optional=None), _result(one=other_job))

    with pytest.raises(AiSupportQueueInvariantError):
        await AiSupportQueueCRUD.enqueue_if_eligible(
            db,
            ticket_id=7,
            trigger_message_id=11,
            channel='cabinet',
            now=NOW,
        )


@pytest.mark.asyncio
async def test_claim_uses_skip_locked_and_recovers_stale_lease() -> None:
    job = SimpleNamespace(
        status=AiSupportJobStatus.PROCESSING.value,
        attempt_count=1,
        locked_at=NOW - timedelta(minutes=3),
        last_error_code='timeout',
        updated_at=None,
    )
    db = _db(_result(optional=job))

    claimed = await AiSupportQueueCRUD.claim_next(db, now=NOW, lease_timeout=timedelta(minutes=2))

    assert claimed is job
    assert job.status == AiSupportJobStatus.PROCESSING.value
    assert job.attempt_count == 2
    assert job.locked_at == NOW
    assert job.last_error_code is None
    sql = _sql(db.execute.await_args.args[0])
    assert 'FOR UPDATE OF ai_support_jobs SKIP LOCKED' in sql
    assert "ai_support_jobs.status = 'pending'" in sql
    assert "ai_support_jobs.status = 'processing'" in sql


@pytest.mark.asyncio
async def test_claim_returns_none_without_mutation() -> None:
    db = _db(_result(optional=None))
    assert await AiSupportQueueCRUD.claim_next(db, now=NOW) is None
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_is_bounded_and_does_not_commit() -> None:
    job = SimpleNamespace(
        id=3,
        status='processing',
        attempt_count=1,
        locked_at=NOW,
        last_error_code=None,
        updated_at=None,
        available_at=NOW,
        ticket_id=7,
    )
    db = _db(_result(optional=SimpleNamespace(state='processing')), _result(rowcount=1))
    retry_at = NOW + timedelta(seconds=30)

    will_retry = await AiSupportQueueCRUD.schedule_retry(
        db,
        job,
        error_code='provider_timeout',
        available_at=retry_at,
        max_attempts=3,
        now=NOW,
    )

    assert will_retry is True
    assert job.status == 'pending'
    assert job.available_at == retry_at
    assert job.locked_at is None
    assert job.last_error_code == 'provider_timeout'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_exhaustion_fails_closed_to_escalation() -> None:
    job = SimpleNamespace(
        id=3,
        status='processing',
        attempt_count=3,
        locked_at=NOW,
        last_error_code=None,
        updated_at=None,
        available_at=NOW,
        ticket_id=7,
    )
    db = _db(
        _result(optional=SimpleNamespace(state='processing')),
        _result(rowcount=1),
        _result(rowcount=1),
    )

    will_retry = await AiSupportQueueCRUD.schedule_retry(
        db,
        job,
        error_code='provider_timeout',
        available_at=NOW + timedelta(minutes=1),
        max_attempts=3,
        now=NOW,
    )

    assert will_retry is False
    assert job.status == 'failed'
    assert 'FOR UPDATE' in _sql(db.execute.await_args_list[0].args[0])
    transition_sql = _sql(db.execute.await_args_list[1].args[0])
    assert "status='failed'" in transition_sql.replace(' ', '')
    assert "status = 'processing'" in transition_sql
    state_sql = _sql(db.execute.await_args_list[2].args[0])
    assert "state='escalated'" in state_sql.replace(' ', '')
    assert 'human_owned' in state_sql


@pytest.mark.asyncio
async def test_complete_releases_only_latest_processing_ticket() -> None:
    job = SimpleNamespace(
        id=3,
        status='processing',
        locked_at=NOW,
        updated_at=None,
        ticket_id=7,
        trigger_message_id=11,
    )
    db = _db(
        _result(optional=SimpleNamespace(state='processing')),
        _result(rowcount=1),
        _result(rowcount=1),
    )

    await AiSupportQueueCRUD.mark_completed(db, job, now=NOW)

    assert job.status == 'completed'
    assert job.locked_at is None
    assert 'FOR UPDATE' in _sql(db.execute.await_args_list[0].args[0])
    transition_sql = _sql(db.execute.await_args_list[1].args[0])
    assert "status='completed'" in transition_sql.replace(' ', '')
    assert "status = 'processing'" in transition_sql
    state_sql = _sql(db.execute.await_args_list[2].args[0])
    assert 'last_trigger_message_id = 11' in state_sql
    assert "state = 'processing'" in state_sql


@pytest.mark.asyncio
async def test_human_takeover_abstains_active_jobs_atomically() -> None:
    state = SimpleNamespace(state='human_owned')
    db = _db(_result(one=state), _result(rowcount=2))

    outcome = await AiSupportQueueCRUD.take_over_ticket(
        db,
        ticket_id=7,
        reason_code='human_reply',
        now=NOW,
    )

    assert outcome.state is state
    assert outcome.jobs_abstained == 2
    upsert_sql = _sql(db.execute.await_args_list[0].args[0])
    assert 'ON CONFLICT (ticket_id) DO UPDATE' in upsert_sql
    assert "'human_owned'" in upsert_sql
    abstain_sql = _sql(db.execute.await_args_list[1].args[0])
    assert "status='escalated'" in abstain_sql.replace(' ', '')
    assert 'human_takeover' in abstain_sql
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_worker_cannot_complete_after_human_takeover() -> None:
    stale_job = SimpleNamespace(
        id=3,
        status='processing',
        locked_at=NOW,
        updated_at=None,
        ticket_id=7,
        trigger_message_id=11,
    )
    db = _db(_result(optional=SimpleNamespace(state='human_owned')))

    with pytest.raises(AiSupportJobTransitionError, match='no longer AI-owned'):
        await AiSupportQueueCRUD.mark_completed(db, stale_job, now=NOW)

    assert stale_job.status == 'processing'
    assert db.execute.await_count == 1
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_escalation_uses_conditional_transition() -> None:
    job = SimpleNamespace(
        id=3,
        status='processing',
        locked_at=NOW,
        last_error_code=None,
        updated_at=None,
        ticket_id=7,
    )
    db = _db(
        _result(optional=SimpleNamespace(state='processing')),
        _result(rowcount=1),
        _result(rowcount=1),
    )

    await AiSupportQueueCRUD.mark_escalated(db, job, reason_code='human_requested', now=NOW)

    assert job.status == 'escalated'
    assert 'FOR UPDATE' in _sql(db.execute.await_args_list[0].args[0])
    transition_sql = _sql(db.execute.await_args_list[1].args[0])
    assert "status IN ('pending', 'processing')" in transition_sql
    assert 'human_requested' in transition_sql
    state_sql = _sql(db.execute.await_args_list[2].args[0])
    assert "state='escalated'" in state_sql.replace(' ', '')


@pytest.mark.asyncio
async def test_delivery_lock_rejects_human_owned_ticket() -> None:
    db = _db(_result(optional=SimpleNamespace(state='human_owned')))
    assert await AiSupportQueueCRUD.lock_ticket_for_ai_delivery(db, ticket_id=7) is False
    assert 'FOR UPDATE' in _sql(db.execute.await_args.args[0])


@pytest.mark.asyncio
async def test_invalid_transition_and_raw_error_are_rejected() -> None:
    completed = SimpleNamespace(status='completed')
    db = _db()
    with pytest.raises(AiSupportJobTransitionError):
        await AiSupportQueueCRUD.schedule_retry(
            db,
            completed,
            error_code='timeout',
            available_at=NOW,
            max_attempts=3,
        )

    processing = SimpleNamespace(status='processing')
    with pytest.raises(ValueError, match='error_code'):
        await AiSupportQueueCRUD.schedule_retry(
            db,
            processing,
            error_code='Timeout: secret URL leaked',
            available_at=NOW,
            max_attempts=3,
        )
