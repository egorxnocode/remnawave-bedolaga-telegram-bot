from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.cabinet.routes import admin_ai_support as routes
from app.cabinet.routes.admin_ai_support import AiSupportDraftReviewRequest


NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)


def _draft():
    return SimpleNamespace(
        id=5,
        run_id=3,
        ticket_id=7,
        trigger_message_id=11,
        status='accepted',
        answer_text='Черновик',
        citations=['POLICY'],
        reviewed_text='Проверенный ответ',
        reviewed_by_user_id=42,
        reviewed_at=NOW,
        review_reason='edited',
        created_at=NOW,
        updated_at=NOW,
    )


def test_rejection_requires_safe_reason_and_forbids_text() -> None:
    with pytest.raises(ValidationError):
        AiSupportDraftReviewRequest(action='rejected')
    with pytest.raises(ValidationError):
        AiSupportDraftReviewRequest(action='rejected', reason='incorrect', reviewed_text='text')
    with pytest.raises(ValidationError):
        AiSupportDraftReviewRequest(action='rejected', reason='contains spaces')


@pytest.mark.asyncio
async def test_acceptance_only_reviews_draft_and_does_not_create_message(monkeypatch: pytest.MonkeyPatch) -> None:
    review = AsyncMock(return_value=_draft())
    monkeypatch.setattr(routes.AiSupportDraftCRUD, 'review', review)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock(), add=AsyncMock())
    admin = SimpleNamespace(id=42)

    response = await routes.review_ai_support_draft(
        ticket_id=7,
        draft_id=5,
        request=AiSupportDraftReviewRequest(action='accepted', reviewed_text='Проверенный ответ', reason='edited'),
        admin=admin,
        db=db,
    )

    assert response.status == 'accepted'
    review.assert_awaited_once()
    db.commit.assert_awaited_once()
    db.add.assert_not_awaited()
