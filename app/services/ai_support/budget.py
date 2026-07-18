"""Durable fail-closed token budget checks backed by sanitized run usage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import AiSupportRun


@dataclass(frozen=True, slots=True)
class AiSupportBudgetDecision:
    allowed: bool
    reason_code: str
    consumed_tokens: int
    remaining_tokens: int


class AiSupportBudgetGuard:
    async def check(
        self,
        db: AsyncSession,
        *,
        projected_tokens: int,
        now: datetime | None = None,
    ) -> AiSupportBudgetDecision:
        if projected_tokens < 1:
            raise ValueError('projected_tokens must be positive')
        limit = settings.AI_SUPPORT_DAILY_TOKEN_BUDGET
        if limit == 0:
            return AiSupportBudgetDecision(False, 'budget_disabled', 0, 0)
        current = (now or datetime.now(UTC)).astimezone(UTC)
        day_start = datetime.combine(current.date(), time.min, tzinfo=UTC)
        used_expression = (
            AiSupportRun.input_tokens
            + AiSupportRun.output_tokens
            + AiSupportRun.cache_read_tokens
            + AiSupportRun.cache_write_tokens
        )
        result = await db.execute(
            select(func.coalesce(func.sum(used_expression), 0)).where(AiSupportRun.created_at >= day_start)
        )
        consumed = int(result.scalar_one())
        remaining = max(0, limit - consumed)
        if projected_tokens > remaining:
            return AiSupportBudgetDecision(False, 'daily_token_budget_exhausted', consumed, remaining)
        return AiSupportBudgetDecision(True, 'budget_available', consumed, remaining)


ai_support_budget_guard = AiSupportBudgetGuard()
