from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.database.models import AiSupportRun, AiSupportRunDecision
from app.services.ai_support import AiSupportWorker, AiSupportWorkerStatus


def _result(message) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = message
    return result


def _db(message) -> SimpleNamespace:
    db = SimpleNamespace(
        add=MagicMock(),
        execute=AsyncMock(side_effect=[_result(message), MagicMock()]),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )

    async def assign_run_id() -> None:
        if db.add.call_args:
            db.add.call_args.args[0].id = 31

    db.flush.side_effect = assign_run_id
    return db


@pytest.mark.asyncio
async def test_off_mode_does_not_claim_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'off')
    db = _db(None)

    with patch(
        'app.services.ai_support.worker.AiSupportQueueCRUD.claim_next',
        new_callable=AsyncMock,
    ) as claim:
        result = await AiSupportWorker().process_next(db)

    assert result.status is AiSupportWorkerStatus.DISABLED
    claim.assert_not_awaited()
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_queue_is_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'shadow')
    db = _db(None)

    with patch(
        'app.services.ai_support.worker.AiSupportQueueCRUD.claim_next',
        new=AsyncMock(return_value=None),
    ):
        result = await AiSupportWorker().process_next(db)

    assert result.status is AiSupportWorkerStatus.IDLE
    assert result.reason_codes == ('queue_empty',)
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_sensitive_message_is_audited_and_escalated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'shadow')
    job = SimpleNamespace(id=19, ticket_id=7, trigger_message_id=11)
    message = SimpleNamespace(message_text='token=top-secret-value', has_media=False)
    db = _db(message)

    with (
        patch(
            'app.services.ai_support.worker.AiSupportQueueCRUD.claim_next',
            new=AsyncMock(return_value=job),
        ),
        patch(
            'app.services.ai_support.worker.AiSupportQueueCRUD.mark_escalated',
            new_callable=AsyncMock,
        ) as escalate,
    ):
        result = await AiSupportWorker().process_next(db)

    assert result.status is AiSupportWorkerStatus.ESCALATED
    assert result.reason_codes == ('sensitive_data', 'credential')
    run = db.add.call_args.args[0]
    assert isinstance(run, AiSupportRun)
    assert run.decision == AiSupportRunDecision.ESCALATE.value
    assert run.provider is None
    assert run.model_id is None
    assert run.reason_codes == ['sensitive_data', 'credential']
    assert 'top-secret-value' not in repr(run.__dict__)
    escalate.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_eligible_message_abstains_without_provider_or_customer_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'shadow')
    job = SimpleNamespace(id=19, ticket_id=7, trigger_message_id=11)
    message = SimpleNamespace(message_text='Как подключить телефон?', has_media=False)
    db = _db(message)

    with (
        patch(
            'app.services.ai_support.worker.AiSupportQueueCRUD.claim_next',
            new=AsyncMock(return_value=job),
        ),
        patch(
            'app.services.ai_support.worker.AiSupportQueueCRUD.mark_escalated',
            new_callable=AsyncMock,
        ) as escalate,
    ):
        result = await AiSupportWorker().process_next(db)

    assert result.status is AiSupportWorkerStatus.PROVIDER_UNAVAILABLE
    assert result.reason_codes == ('provider_not_configured',)
    run = db.add.call_args.args[0]
    assert type(run) is AiSupportRun
    assert run.decision == AiSupportRunDecision.ABSTAIN.value
    assert run.input_tokens == 0
    assert run.output_tokens == 0
    escalate.assert_awaited_once()
    db.commit.assert_not_awaited()
