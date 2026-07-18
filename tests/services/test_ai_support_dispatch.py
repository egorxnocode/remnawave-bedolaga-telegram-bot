from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.services.ai_support import AiSupportDispatchService, AiSupportDispatchStatus


class _NestedTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


def _db() -> SimpleNamespace:
    return SimpleNamespace(
        begin_nested=MagicMock(return_value=_NestedTransaction()),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_off_mode_does_not_touch_database_or_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'off')
    db = _db()
    message = SimpleNamespace(id=11)

    with patch(
        'app.services.ai_support.dispatch.AiSupportQueueCRUD.enqueue_if_eligible',
        new_callable=AsyncMock,
    ) as enqueue:
        result = await AiSupportDispatchService().on_user_message(
            db,
            ticket_id=7,
            message=message,
            channel='cabinet',
        )

    assert result.status is AiSupportDispatchStatus.DISABLED
    assert result.reason_code == 'mode_off'
    db.begin_nested.assert_not_called()
    enqueue.assert_not_awaited()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_shadow_mode_enqueues_inside_savepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'shadow')
    db = _db()
    message = SimpleNamespace(id=11)
    outcome = SimpleNamespace(job=SimpleNamespace(id=23), reason_code='created')

    with patch(
        'app.services.ai_support.dispatch.AiSupportQueueCRUD.enqueue_if_eligible',
        new=AsyncMock(return_value=outcome),
    ) as enqueue:
        result = await AiSupportDispatchService().on_user_message(
            db,
            ticket_id=7,
            message=message,
            channel='support_ws',
        )

    assert result.status is AiSupportDispatchStatus.ENQUEUED
    assert result.job_id == 23
    db.begin_nested.assert_called_once_with()
    db.flush.assert_awaited_once_with()
    enqueue.assert_awaited_once_with(db, ticket_id=7, trigger_message_id=11, channel='support_ws')
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_database_error_fails_open_for_human_support(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'shadow')
    db = _db()
    message = SimpleNamespace(id=11)

    with patch(
        'app.services.ai_support.dispatch.AiSupportQueueCRUD.enqueue_if_eligible',
        new=AsyncMock(side_effect=SQLAlchemyError('database unavailable')),
    ):
        result = await AiSupportDispatchService().on_user_message(
            db,
            ticket_id=7,
            message=message,
            channel='telegram',
        )

    assert result.status is AiSupportDispatchStatus.ERROR
    assert result.reason_code == 'queue_error'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_human_reply_takes_ticket_over_without_committing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODE', 'shadow')
    db = _db()
    outcome = SimpleNamespace(jobs_abstained=2)

    with (
        patch(
            'app.services.ai_support.dispatch.AiSupportQueueCRUD.take_over_ticket',
            new=AsyncMock(return_value=outcome),
        ) as take_over,
        patch(
            'app.services.ai_support.dispatch.AiSupportDraftCRUD.supersede_pending',
            new=AsyncMock(return_value=1),
        ) as supersede,
    ):
        result = await AiSupportDispatchService().on_human_reply(db, ticket_id=7)

    assert result.status is AiSupportDispatchStatus.HUMAN_OWNED
    assert result.jobs_abstained == 2
    take_over.assert_awaited_once_with(db, ticket_id=7, reason_code='human_reply')
    supersede.assert_awaited_once_with(db, ticket_id=7)
    db.commit.assert_not_awaited()
