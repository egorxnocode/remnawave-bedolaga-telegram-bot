from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.ai_support.budget import AiSupportBudgetGuard


@pytest.mark.asyncio
async def test_zero_budget_blocks_without_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_DAILY_TOKEN_BUDGET', 0)
    db = SimpleNamespace(execute=AsyncMock())
    decision = await AiSupportBudgetGuard().check(db, projected_tokens=100)
    assert decision.reason_code == 'budget_disabled'
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_persisted_usage_blocks_projected_overspend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_DAILY_TOKEN_BUDGET', 1000)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: 950)))
    decision = await AiSupportBudgetGuard().check(
        db,
        projected_tokens=100,
        now=datetime(2026, 7, 18, 12, tzinfo=UTC),
    )
    assert not decision.allowed
    assert decision.reason_code == 'daily_token_budget_exhausted'
    assert decision.consumed_tokens == 950
    assert decision.remaining_tokens == 50


@pytest.mark.asyncio
async def test_persisted_usage_allows_request_within_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_DAILY_TOKEN_BUDGET', 1000)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: 100)))
    decision = await AiSupportBudgetGuard().check(db, projected_tokens=800)
    assert decision.allowed
    assert decision.reason_code == 'budget_available'
    assert decision.remaining_tokens == 900


@pytest.mark.asyncio
async def test_invalid_projection_is_rejected() -> None:
    with pytest.raises(ValueError, match='positive'):
        await AiSupportBudgetGuard().check(SimpleNamespace(), projected_tokens=0)
