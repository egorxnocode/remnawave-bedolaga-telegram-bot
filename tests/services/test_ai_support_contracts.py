from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.services.ai_support.contracts import (
    AiSupportContextBuilder,
    AiSupportContractError,
    AiSupportProviderResponse,
    AiSupportProviderResult,
    AiSupportProviderUsage,
    SafeCustomerContext,
    SafePaymentContext,
)
from app.services.ai_support.knowledge import AiSupportKnowledgeError, load_ai_support_knowledge
from app.services.ai_support.policy import assess_customer_message
from app.services.ai_support.types import AiSupportMode


def _builder() -> AiSupportContextBuilder:
    return AiSupportContextBuilder(load_ai_support_knowledge())


def test_request_contains_redacted_message_and_no_local_identifiers() -> None:
    assessment = assess_customer_message(
        AiSupportMode.SHADOW,
        'Моя почта user@example.com. Как подключить телефон?',
    )
    context = SafeCustomerContext(
        subscription_status='active',
        tariff_name='Стандартный',
        traffic_used_bytes=1_000,
        traffic_limit_bytes=10_000,
        device_count=1,
        device_limit=3,
        platform='android',
    )

    request = _builder().build_request(assessment, context)
    serialized = request.model_dump_json()

    assert request.contract_version == 1
    assert request.kb_version == '2026-07-18.1'
    assert len(request.knowledge_sections) == 9
    assert '[REDACTED_EMAIL]' in request.customer_message
    assert 'user@example.com' not in serialized
    assert 'ticket_id' not in serialized
    assert 'user_id' not in serialized
    assert 'telegram_id' not in serialized


def test_noneligible_message_cannot_build_provider_request() -> None:
    assessment = assess_customer_message(AiSupportMode.SHADOW, 'Позовите живого оператора')

    with pytest.raises(AiSupportContractError, match='not eligible'):
        _builder().build_request(assessment, SafeCustomerContext())


def test_safe_context_rejects_extra_identifying_fields() -> None:
    with pytest.raises(ValidationError):
        SafeCustomerContext(subscription_status='active', telegram_id=123)  # type: ignore[call-arg]


def test_safe_context_rejects_identifier_in_tariff_name() -> None:
    with pytest.raises(ValidationError, match='unsafe identifiers'):
        SafeCustomerContext(tariff_name='user@example.com')


def test_payment_observation_requires_timezone() -> None:
    with pytest.raises(ValidationError, match='timezone-aware'):
        SafePaymentContext(
            provider='lava',
            status='succeeded',
            amount_kopeks=2_700,
            observed_at=datetime(2026, 7, 18, 12, 0),
        )

    context = SafePaymentContext(
        provider='lava',
        status='succeeded',
        amount_kopeks=2_700,
        observed_at=datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
    )
    assert context.observed_at.tzinfo is UTC


def test_answer_requires_text_and_citations() -> None:
    with pytest.raises(ValidationError, match='requires text and citations'):
        AiSupportProviderResult(decision='answer', answer_text='Попробуйте ещё раз.')


def test_unknown_result_citation_is_rejected() -> None:
    result = AiSupportProviderResult(
        decision='answer',
        answer_text='Откройте кабинет и выберите подключение.',
        citations=('INTERNAL_OPS',),
    )

    with pytest.raises(AiSupportKnowledgeError, match='unknown knowledge citations'):
        _builder().validate_result(result)


def test_provider_answer_rejects_unapproved_url_and_sensitive_value() -> None:
    unknown_url = AiSupportProviderResult(
        decision='answer',
        answer_text='Откройте https://internal.example/path',
        citations=('CONNECTION',),
    )
    with pytest.raises(AiSupportContractError, match='unapproved URL'):
        _builder().validate_result(unknown_url)

    secret = AiSupportProviderResult(
        decision='answer',
        answer_text='Используйте token=top-secret-value',
        citations=('CONNECTION',),
    )
    with pytest.raises(AiSupportContractError, match='unsafe identifiers'):
        _builder().validate_result(secret)


def test_provider_answer_allows_url_present_in_reviewed_knowledge() -> None:
    result = AiSupportProviderResult(
        decision='answer',
        answer_text='Политика: https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-07-14-52',
        citations=('LEGAL',),
    )

    _builder().validate_result(result)


def test_escalation_result_cannot_contain_customer_answer() -> None:
    with pytest.raises(ValidationError, match='must not contain'):
        AiSupportProviderResult(
            decision='escalate',
            answer_text='Попробуйте сделать это самостоятельно.',
            reason_codes=('uncertain',),
        )


def test_provider_response_keeps_only_bounded_metadata_and_usage() -> None:
    result = AiSupportProviderResult(
        decision='escalate',
        reason_codes=('provider_uncertain',),
    )
    response = AiSupportProviderResponse(
        provider='anthropic',
        model_id='future-model-id',
        latency_ms=125,
        usage=AiSupportProviderUsage(input_tokens=100, output_tokens=20),
        result=result,
    )

    assert response.usage.input_tokens == 100
    assert 'raw_payload' not in response.model_dump()
    with pytest.raises(ValidationError):
        AiSupportProviderUsage(input_tokens=-1)
