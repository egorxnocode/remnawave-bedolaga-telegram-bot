from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.services.ai_support.context import (
    AiSupportContextAuthorizationError,
    AiSupportDatabaseContextCollector,
)


def _result(*, scalar=None, row=None):
    return SimpleNamespace(
        scalar_one_or_none=lambda: scalar,
        one_or_none=lambda: row,
    )


@pytest.mark.asyncio
async def test_collects_only_safe_context_for_authorized_ticket_user() -> None:
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _result(scalar=42),
                _result(
                    row=SimpleNamespace(
                        status='active',
                        end_date=datetime(2026, 8, 20, tzinfo=UTC),
                        traffic_used_gb=1.5,
                        traffic_limit_gb=100,
                        device_limit=3,
                        tariff_name='Standard',
                    )
                ),
                _result(
                    row=SimpleNamespace(
                        payment_method='lava',
                        amount_kopeks=27900,
                        completed_at=datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
                        created_at=datetime(2026, 7, 18, 11, 59, tzinfo=UTC),
                    )
                ),
            ]
        )
    )

    context = await AiSupportDatabaseContextCollector().collect(
        db,
        ticket_id=7,
        trigger_message_id=9,
    )

    assert context.subscription_status == 'active'
    assert context.tariff_name == 'Standard'
    assert context.traffic_used_bytes == int(1.5 * 1024**3)
    assert context.traffic_limit_bytes == 100 * 1024**3
    assert context.device_count is None
    assert context.device_limit == 3
    assert context.latest_payment is not None
    assert context.latest_payment.model_dump() == {
        'provider': 'lava',
        'status': 'succeeded',
        'amount_kopeks': 27900,
        'observed_at': datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
    }
    assert not ({'user_id', 'ticket_id', 'trigger_message_id'} & context.model_dump().keys())


@pytest.mark.asyncio
async def test_rejects_message_not_proven_to_belong_to_ticket_user() -> None:
    db = SimpleNamespace(execute=AsyncMock(return_value=_result()))

    with pytest.raises(AiSupportContextAuthorizationError):
        await AiSupportDatabaseContextCollector().collect(db, ticket_id=7, trigger_message_id=9)

    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_subscription_returns_none_status_and_successful_payment() -> None:
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _result(scalar=42),
                _result(),
                _result(
                    row=SimpleNamespace(
                        payment_method='yookassa',
                        amount_kopeks=1000,
                        completed_at=None,
                        created_at=datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
                    )
                ),
            ]
        )
    )

    context = await AiSupportDatabaseContextCollector().collect(db, ticket_id=7, trigger_message_id=9)

    assert context.subscription_status == 'none'
    assert context.latest_payment is not None
    assert context.latest_payment.provider == 'yookassa'


@pytest.mark.asyncio
async def test_unsafe_optional_values_are_omitted_fail_closed() -> None:
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _result(scalar=42),
                _result(
                    row=SimpleNamespace(
                        status='limited',
                        end_date=None,
                        traffic_used_gb=-1,
                        traffic_limit_gb='invalid',
                        device_limit=500,
                        tariff_name='user@example.com',
                    )
                ),
                _result(
                    row=SimpleNamespace(
                        payment_method='unsafe provider',
                        amount_kopeks=1000,
                        completed_at=None,
                        created_at=datetime(2026, 7, 18),
                    )
                ),
            ]
        )
    )

    context = await AiSupportDatabaseContextCollector().collect(db, ticket_id=7, trigger_message_id=9)

    assert context.subscription_status == 'unknown'
    assert context.tariff_name is None
    assert context.traffic_used_bytes is None
    assert context.traffic_limit_bytes is None
    assert context.device_limit is None
    assert context.latest_payment is None


def test_queries_select_only_reviewed_scalar_columns() -> None:
    collector = AiSupportDatabaseContextCollector()
    db = SimpleNamespace(execute=AsyncMock(return_value=_result()))

    # Capture statements by invoking the query helpers without a real database.
    async def run() -> list[str]:
        await collector._subscription_row(db, user_id=42)
        await collector._latest_successful_payment(db, user_id=42)
        return [str(call.args[0].compile(dialect=postgresql.dialect())) for call in db.execute.await_args_list]

    import asyncio

    subscription_sql, payment_sql = asyncio.run(run())
    combined = f'{subscription_sql}\n{payment_sql}'.lower()
    for forbidden in ('telegram_id', 'email', 'username', 'external_id', 'remnawave_uuid', 'description'):
        assert forbidden not in combined
    assert 'subscriptions.user_id' in subscription_sql
    assert 'transactions.user_id' in payment_sql
