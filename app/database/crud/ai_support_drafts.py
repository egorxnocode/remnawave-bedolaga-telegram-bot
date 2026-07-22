"""Transactional CRUD for operator-only AI support drafts."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AiSupportDraft, AiSupportDraftStatus


_SAFE_REASON = re.compile(r'^[a-z][a-z0-9_.-]{0,63}$')
_SECTION_ID = re.compile(r'^[A-Z][A-Z0-9_]{0,63}$')


class AiSupportDraftConflictError(RuntimeError):
    """Raised when a draft is missing, mismatched or already reviewed."""


class AiSupportDraftCRUD:
    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        run_id: int,
        ticket_id: int,
        trigger_message_id: int,
        answer_text: str,
        citations: tuple[str, ...],
        now: datetime | None = None,
    ) -> AiSupportDraft:
        text = answer_text.strip()
        if not text or len(text) > 2000:
            raise ValueError('draft answer_text must contain 1..2000 characters')
        if not citations or len(citations) > 32 or len(citations) != len(set(citations)):
            raise ValueError('draft citations must be non-empty and unique')
        if any(not _SECTION_ID.fullmatch(citation) for citation in citations):
            raise ValueError('draft citations must contain safe section ids')
        draft = AiSupportDraft(
            run_id=run_id,
            ticket_id=ticket_id,
            trigger_message_id=trigger_message_id,
            status=AiSupportDraftStatus.PENDING.value,
            answer_text=text,
            citations=list(citations),
            created_at=now or datetime.now(UTC),
            updated_at=now or datetime.now(UTC),
        )
        db.add(draft)
        await db.flush()
        return draft

    @staticmethod
    async def list_for_ticket(db: AsyncSession, *, ticket_id: int) -> list[AiSupportDraft]:
        result = await db.execute(
            select(AiSupportDraft)
            .where(AiSupportDraft.ticket_id == ticket_id)
            .order_by(AiSupportDraft.created_at.desc(), AiSupportDraft.id.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def review(
        db: AsyncSession,
        *,
        ticket_id: int,
        draft_id: int,
        reviewer_user_id: int,
        action: str,
        reviewed_text: str | None,
        reason: str | None,
        now: datetime | None = None,
    ) -> AiSupportDraft:
        if action not in {AiSupportDraftStatus.ACCEPTED.value, AiSupportDraftStatus.REJECTED.value}:
            raise ValueError('unsupported draft review action')
        if reason is not None and not _SAFE_REASON.fullmatch(reason):
            raise ValueError('review reason must be a safe code')
        result = await db.execute(
            select(AiSupportDraft)
            .where(AiSupportDraft.id == draft_id, AiSupportDraft.ticket_id == ticket_id)
            .with_for_update()
        )
        draft = result.scalar_one_or_none()
        if draft is None:
            raise AiSupportDraftConflictError('draft not found for ticket')
        if draft.status != AiSupportDraftStatus.PENDING.value:
            raise AiSupportDraftConflictError('draft is no longer pending')

        if action == AiSupportDraftStatus.ACCEPTED.value:
            final_text = (reviewed_text if reviewed_text is not None else draft.answer_text).strip()
            if not final_text or len(final_text) > 2000:
                raise ValueError('reviewed_text must contain 1..2000 characters')
            draft.reviewed_text = final_text
        elif reviewed_text is not None:
            raise ValueError('rejected draft cannot contain reviewed_text')

        current = now or datetime.now(UTC)
        draft.status = action
        draft.reviewed_by_user_id = reviewer_user_id
        draft.reviewed_at = current
        draft.review_reason = reason
        draft.updated_at = current
        await db.flush()
        return draft

    @staticmethod
    async def mark_auto_accepted(
        db: AsyncSession,
        *,
        ticket_id: int,
        draft_id: int,
        answer_text: str,
        now: datetime | None = None,
    ) -> AiSupportDraft:
        """Mark a draft ACCEPTED by the AI auto-delivery path (no human reviewer)."""
        result = await db.execute(
            select(AiSupportDraft)
            .where(AiSupportDraft.id == draft_id, AiSupportDraft.ticket_id == ticket_id)
            .with_for_update()
        )
        draft = result.scalar_one_or_none()
        if draft is None:
            raise AiSupportDraftConflictError('draft not found for ticket')
        if draft.status != AiSupportDraftStatus.PENDING.value:
            raise AiSupportDraftConflictError('draft is no longer pending')
        final_text = answer_text.strip()
        if not final_text or len(final_text) > 2000:
            raise ValueError('answer_text must contain 1..2000 characters')
        current = now or datetime.now(UTC)
        draft.reviewed_text = final_text
        draft.status = AiSupportDraftStatus.ACCEPTED.value
        draft.reviewed_by_user_id = None
        draft.reviewed_at = current
        draft.review_reason = 'auto_delivered'
        draft.updated_at = current
        await db.flush()
        return draft

    @staticmethod
    async def supersede_pending(db: AsyncSession, *, ticket_id: int, now: datetime | None = None) -> int:
        result = await db.execute(
            update(AiSupportDraft)
            .where(
                AiSupportDraft.ticket_id == ticket_id,
                AiSupportDraft.status == AiSupportDraftStatus.PENDING.value,
            )
            .values(status=AiSupportDraftStatus.SUPERSEDED.value, updated_at=now or datetime.now(UTC))
        )
        return int(result.rowcount or 0)
