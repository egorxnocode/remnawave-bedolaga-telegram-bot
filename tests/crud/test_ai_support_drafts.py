from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.database.crud.ai_support_drafts import AiSupportDraftConflictError, AiSupportDraftCRUD


NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)


def _result(*, optional=None, rows=None, rowcount=0):
    result = MagicMock()
    result.scalar_one_or_none.return_value = optional
    result.scalars.return_value.all.return_value = rows or []
    result.rowcount = rowcount
    return result


def _db(*results):
    return SimpleNamespace(
        add=MagicMock(),
        execute=AsyncMock(side_effect=results),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))


@pytest.mark.asyncio
async def test_create_validates_and_flushes_without_commit() -> None:
    db = _db()
    draft = await AiSupportDraftCRUD.create(
        db,
        run_id=3,
        ticket_id=7,
        trigger_message_id=11,
        answer_text='  Безопасный ответ.  ',
        citations=('POLICY',),
        now=NOW,
    )
    assert draft.answer_text == 'Безопасный ответ.'
    assert draft.status == 'pending'
    assert draft.citations == ['POLICY']
    db.add.assert_called_once_with(draft)
    db.flush.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_rejects_missing_or_unsafe_citations() -> None:
    db = _db()
    with pytest.raises(ValueError, match='non-empty'):
        await AiSupportDraftCRUD.create(
            db, run_id=3, ticket_id=7, trigger_message_id=11, answer_text='Ответ', citations=()
        )
    with pytest.raises(ValueError, match='safe section'):
        await AiSupportDraftCRUD.create(
            db, run_id=3, ticket_id=7, trigger_message_id=11, answer_text='Ответ', citations=('bad',)
        )


@pytest.mark.asyncio
async def test_review_locks_pending_draft_and_never_commits() -> None:
    draft = SimpleNamespace(
        status='pending',
        answer_text='Черновик',
        reviewed_text=None,
        reviewed_by_user_id=None,
        reviewed_at=None,
        review_reason=None,
        updated_at=None,
    )
    db = _db(_result(optional=draft))
    result = await AiSupportDraftCRUD.review(
        db,
        ticket_id=7,
        draft_id=5,
        reviewer_user_id=42,
        action='accepted',
        reviewed_text='Исправленный ответ',
        reason='edited',
        now=NOW,
    )
    assert result is draft
    assert draft.status == 'accepted'
    assert draft.reviewed_text == 'Исправленный ответ'
    assert draft.reviewed_by_user_id == 42
    assert 'FOR UPDATE' in _sql(db.execute.await_args.args[0])
    db.flush.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_rejects_stale_or_cross_ticket_draft() -> None:
    db = _db(_result(optional=None))
    with pytest.raises(AiSupportDraftConflictError):
        await AiSupportDraftCRUD.review(
            db,
            ticket_id=7,
            draft_id=5,
            reviewer_user_id=42,
            action='rejected',
            reviewed_text=None,
            reason='incorrect',
        )


@pytest.mark.asyncio
async def test_human_reply_supersedes_only_pending_drafts() -> None:
    db = _db(_result(rowcount=2))
    count = await AiSupportDraftCRUD.supersede_pending(db, ticket_id=7, now=NOW)
    assert count == 2
    sql = _sql(db.execute.await_args.args[0])
    assert "status = 'pending'" in sql
    assert "status='superseded'" in sql.replace(' ', '')
    db.commit.assert_not_awaited()
