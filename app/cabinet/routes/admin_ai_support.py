"""Operator-only review API for AI support shadow drafts."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.ai_support_drafts import AiSupportDraftConflictError, AiSupportDraftCRUD
from app.database.models import AiSupportDraft, User

from ..dependencies import get_cabinet_db, require_permission


router = APIRouter(prefix='/admin/tickets', tags=['Cabinet Admin AI Support'])


class AiSupportDraftResponse(BaseModel):
    id: int
    run_id: int
    ticket_id: int
    trigger_message_id: int
    status: str
    answer_text: str
    citations: list[str]
    reviewed_text: str | None
    reviewed_by_user_id: int | None
    reviewed_at: datetime | None
    review_reason: str | None
    created_at: datetime
    updated_at: datetime


class AiSupportDraftReviewRequest(BaseModel):
    action: Literal['accepted', 'rejected']
    reviewed_text: str | None = Field(default=None, min_length=1, max_length=2000)
    reason: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_.-]{0,63}$')

    @model_validator(mode='after')
    def fields_match_action(self) -> 'AiSupportDraftReviewRequest':
        if self.action == 'rejected' and self.reviewed_text is not None:
            raise ValueError('rejected draft cannot contain reviewed_text')
        if self.action == 'rejected' and self.reason is None:
            raise ValueError('rejected draft requires a reason code')
        return self


def _response(draft: AiSupportDraft) -> AiSupportDraftResponse:
    return AiSupportDraftResponse(
        id=draft.id,
        run_id=draft.run_id,
        ticket_id=draft.ticket_id,
        trigger_message_id=draft.trigger_message_id,
        status=draft.status,
        answer_text=draft.answer_text,
        citations=list(draft.citations or []),
        reviewed_text=draft.reviewed_text,
        reviewed_by_user_id=draft.reviewed_by_user_id,
        reviewed_at=draft.reviewed_at,
        review_reason=draft.review_reason,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


@router.get('/{ticket_id}/ai-drafts', response_model=list[AiSupportDraftResponse])
async def list_ai_support_drafts(
    ticket_id: int,
    admin: User = Depends(require_permission('tickets:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> list[AiSupportDraftResponse]:
    del admin
    return [_response(draft) for draft in await AiSupportDraftCRUD.list_for_ticket(db, ticket_id=ticket_id)]


@router.post('/{ticket_id}/ai-drafts/{draft_id}/review', response_model=AiSupportDraftResponse)
async def review_ai_support_draft(
    ticket_id: int,
    draft_id: int,
    request: AiSupportDraftReviewRequest,
    admin: User = Depends(require_permission('tickets:reply')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AiSupportDraftResponse:
    try:
        draft = await AiSupportDraftCRUD.review(
            db,
            ticket_id=ticket_id,
            draft_id=draft_id,
            reviewer_user_id=admin.id,
            action=request.action,
            reviewed_text=request.reviewed_text,
            reason=request.reason,
        )
    except AiSupportDraftConflictError as error:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Draft is unavailable') from error
    except ValueError as error:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Invalid draft review') from error
    await db.commit()
    return _response(draft)
