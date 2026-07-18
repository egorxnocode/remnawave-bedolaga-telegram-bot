from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.ai_support.knowledge import AiSupportKnowledgeError, load_ai_support_knowledge
from app.services.ai_support.runner import AiSupportWorkerRunner
from app.services.ai_support.worker import AiSupportWorkerResult, AiSupportWorkerStatus


class _Session:
    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


def _runner(result: AiSupportWorkerResult | BaseException) -> tuple[AiSupportWorkerRunner, _Session, AsyncMock]:
    session = _Session()
    process_next = AsyncMock(side_effect=result if isinstance(result, BaseException) else None)
    if not isinstance(result, BaseException):
        process_next.return_value = result
    worker = SimpleNamespace(process_next=process_next)
    runner = AiSupportWorkerRunner(worker=worker, session_factory=lambda: session)
    return runner, session, process_next


@pytest.mark.asyncio
async def test_terminal_job_commits_one_transaction() -> None:
    outcome = AiSupportWorkerResult(
        AiSupportWorkerStatus.ESCALATED,
        ('human_requested',),
        job_id=19,
        run_id=31,
    )
    runner, session, process_next = _runner(outcome)

    result = await runner.run_once()

    assert result is outcome
    process_next.assert_awaited_once_with(session)
    session.commit.assert_awaited_once_with()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_shadow_draft_commits_one_transaction() -> None:
    outcome = AiSupportWorkerResult(
        AiSupportWorkerStatus.DRAFT_CREATED,
        ('answer_ready',),
        job_id=19,
        run_id=31,
    )
    runner, session, _ = _runner(outcome)

    await runner.run_once()

    session.commit.assert_awaited_once_with()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_cycle_rolls_back_read_transaction() -> None:
    outcome = AiSupportWorkerResult(AiSupportWorkerStatus.IDLE, ('queue_empty',))
    runner, session, _ = _runner(outcome)

    await runner.run_once()

    session.rollback.assert_awaited_once_with()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cycle_crash_rolls_back_claim_for_restart_recovery() -> None:
    runner, session, _ = _runner(RuntimeError('private provider detail'))

    with pytest.raises(RuntimeError, match='private provider detail'):
        await runner.run_once()

    session.rollback.assert_awaited_once_with()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_is_inert_when_worker_flag_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_WORKER_ENABLED', False)
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'shadow')
    runner, _, process_next = _runner(AiSupportWorkerResult(AiSupportWorkerStatus.IDLE, ('queue_empty',)))

    assert await runner.start() is False
    assert runner.is_running() is False
    assert runner.get_status()['state'] == 'disabled'
    process_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_requires_non_off_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_WORKER_ENABLED', True)
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'off')
    runner, _, process_next = _runner(AiSupportWorkerResult(AiSupportWorkerStatus.IDLE, ('queue_empty',)))

    assert await runner.start() is False
    assert runner.get_status()['state'] == 'blocked_mode_off'
    assert runner.get_status()['healthy'] is False
    process_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_fails_closed_when_knowledge_artifact_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_WORKER_ENABLED', True)
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'shadow')
    session = _Session()
    process_next = AsyncMock()

    def broken_loader():
        raise AiSupportKnowledgeError('private artifact detail')

    runner = AiSupportWorkerRunner(
        worker=SimpleNamespace(process_next=process_next),
        session_factory=lambda: session,
        knowledge_loader=broken_loader,
    )

    assert await runner.start() is False
    status = runner.get_status()
    assert status['state'] == 'blocked_knowledge'
    assert status['knowledge_error_type'] == 'AiSupportKnowledgeError'
    assert 'private artifact detail' not in repr(status)
    process_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_running_loop_stops_gracefully_and_reports_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_WORKER_ENABLED', True)
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'shadow')
    monkeypatch.setattr(settings, 'AI_SUPPORT_WORKER_POLL_SECONDS', 0.1)
    monkeypatch.setattr(settings, 'AI_SUPPORT_WORKER_SHUTDOWN_SECONDS', 1.0)
    runner, session, _ = _runner(AiSupportWorkerResult(AiSupportWorkerStatus.IDLE, ('queue_empty',)))

    assert await runner.start() is True
    for _ in range(20):
        if runner.get_status()['last_success_at'] is not None:
            break
        await asyncio.sleep(0)

    running_status = runner.get_status()
    assert running_status['state'] == 'running'
    assert running_status['healthy'] is True
    assert running_status['last_success_at'] is not None
    assert running_status['kb_version'] == load_ai_support_knowledge().kb_version
    assert running_status['kb_sha256'] == load_ai_support_knowledge().package_sha256

    await runner.stop()

    assert runner.is_running() is False
    assert runner.get_status()['state'] == 'stopped'
    assert runner.get_status()['stopped_at'] is not None
    session.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_health_exposes_only_error_type_not_exception_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_WORKER_ENABLED', True)
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'shadow')
    monkeypatch.setattr(settings, 'AI_SUPPORT_WORKER_POLL_SECONDS', 0.1)
    monkeypatch.setattr(settings, 'AI_SUPPORT_WORKER_SHUTDOWN_SECONDS', 1.0)
    runner, _, _ = _runner(RuntimeError('secret-token-must-not-leak'))

    await runner.start()
    for _ in range(20):
        if runner.get_status()['last_error_at'] is not None:
            break
        await asyncio.sleep(0)
    await runner.stop()

    status = runner.get_status()
    assert status['last_error_type'] == 'RuntimeError'
    assert status['consecutive_failures'] == 1
    assert 'secret-token-must-not-leak' not in repr(status)
