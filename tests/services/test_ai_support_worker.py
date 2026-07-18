from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.database.models import AiSupportRun, AiSupportRunDecision
from app.services.ai_support import AiSupportWorker, AiSupportWorkerStatus
from app.services.ai_support.budget import AiSupportBudgetDecision
from app.services.ai_support.contracts import (
    AiSupportProviderResponse,
    AiSupportProviderResult,
    AiSupportProviderUsage,
    SafeCustomerContext,
)


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


def _eligible_worker(*, budget_allowed: bool, provider_response=None) -> tuple[AiSupportWorker, AsyncMock]:
    provider = AsyncMock()
    if provider_response is not None:
        provider.generate.return_value = provider_response
    collector = SimpleNamespace(collect=AsyncMock(return_value=SafeCustomerContext()))
    budget = SimpleNamespace(
        check=AsyncMock(
            return_value=AiSupportBudgetDecision(
                budget_allowed,
                'budget_available' if budget_allowed else 'budget_disabled',
                0,
                100_000 if budget_allowed else 0,
            )
        )
    )
    return AiSupportWorker(provider=provider, context_collector=collector, budget_guard=budget), provider.generate


def _provider_response(*, decision: str = 'answer') -> AiSupportProviderResponse:
    return AiSupportProviderResponse(
        provider='anthropic',
        model_id=settings.AI_SUPPORT_MODEL_ID,
        latency_ms=25,
        usage=AiSupportProviderUsage(input_tokens=120, output_tokens=30),
        result=AiSupportProviderResult(
            decision=decision,
            answer_text='Откройте приложение и добавьте подписку.' if decision == 'answer' else None,
            citations=('CONNECTION',) if decision == 'answer' else (),
            reason_codes=() if decision == 'answer' else ('provider_escalated',),
        ),
    )


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
async def test_zero_budget_abstains_without_provider_or_customer_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'shadow')
    job = SimpleNamespace(id=19, ticket_id=7, trigger_message_id=11)
    message = SimpleNamespace(message_text='Как подключить телефон?', has_media=False)
    db = _db(message)
    worker, generate = _eligible_worker(budget_allowed=False)

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
        result = await worker.process_next(db)

    assert result.status is AiSupportWorkerStatus.PROVIDER_UNAVAILABLE
    assert result.reason_codes == ('budget_disabled',)
    run = db.add.call_args.args[0]
    assert type(run) is AiSupportRun
    assert run.decision == AiSupportRunDecision.ABSTAIN.value
    assert run.input_tokens == 0
    assert run.output_tokens == 0
    generate.assert_not_awaited()
    escalate.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_provider_answer_creates_draft_without_ticket_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'shadow')
    job = SimpleNamespace(id=19, ticket_id=7, trigger_message_id=11)
    message = SimpleNamespace(message_text='Как подключить телефон?', has_media=False)
    db = _db(message)
    worker, generate = _eligible_worker(
        budget_allowed=True,
        provider_response=_provider_response(),
    )

    with (
        patch(
            'app.services.ai_support.worker.AiSupportQueueCRUD.claim_next',
            new=AsyncMock(return_value=job),
        ),
        patch(
            'app.services.ai_support.worker.AiSupportQueueCRUD.lock_ticket_for_ai_delivery',
            new=AsyncMock(return_value=True),
        ),
        patch(
            'app.services.ai_support.worker.AiSupportQueueCRUD.mark_completed',
            new_callable=AsyncMock,
        ) as complete,
        patch(
            'app.services.ai_support.worker.AiSupportDraftCRUD.create',
            new_callable=AsyncMock,
        ) as create_draft,
    ):
        result = await worker.process_next(db)

    assert result.status is AiSupportWorkerStatus.DRAFT_CREATED
    run = db.add.call_args.args[0]
    assert type(run) is AiSupportRun
    assert run.decision == AiSupportRunDecision.ANSWER.value
    assert run.input_tokens == 120
    assert run.output_tokens == 30
    generate.assert_awaited_once()
    create_draft.assert_awaited_once_with(
        db,
        run_id=31,
        ticket_id=7,
        trigger_message_id=11,
        answer_text='Откройте приложение и добавьте подписку.',
        citations=('CONNECTION',),
        now=run.completed_at,
    )
    complete.assert_awaited_once()
    assert all(type(item) is AiSupportRun for item in (call.args[0] for call in db.add.call_args_list))
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_escalation_creates_no_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'shadow')
    job = SimpleNamespace(id=19, ticket_id=7, trigger_message_id=11)
    message = SimpleNamespace(message_text='Как подключить телефон?', has_media=False)
    db = _db(message)
    worker, _ = _eligible_worker(
        budget_allowed=True,
        provider_response=_provider_response(decision='escalate'),
    )

    with (
        patch(
            'app.services.ai_support.worker.AiSupportQueueCRUD.claim_next',
            new=AsyncMock(return_value=job),
        ),
        patch(
            'app.services.ai_support.worker.AiSupportQueueCRUD.mark_escalated',
            new_callable=AsyncMock,
        ) as escalate,
        patch(
            'app.services.ai_support.worker.AiSupportDraftCRUD.create',
            new_callable=AsyncMock,
        ) as create_draft,
    ):
        result = await worker.process_next(db)

    assert result.status is AiSupportWorkerStatus.ESCALATED
    assert result.reason_codes == ('provider_escalated',)
    assert db.add.call_args.args[0].decision == AiSupportRunDecision.ESCALATE.value
    create_draft.assert_not_awaited()
    escalate.assert_awaited_once()
