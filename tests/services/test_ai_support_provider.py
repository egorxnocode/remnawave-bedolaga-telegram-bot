from datetime import date
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.config import settings
from app.services.ai_support.contracts import (
    AiSupportProviderRequest,
    ProviderKnowledgeSection,
    SafeCustomerContext,
)
from app.services.ai_support.provider import AiSupportProviderError, AnthropicSupportProvider


def _request() -> AiSupportProviderRequest:
    return AiSupportProviderRequest(
        prompt_version='support-v1',
        kb_version='2026-07-18.1',
        kb_sha256='a' * 64,
        customer_message='Как подключиться?',
        customer_context=SafeCustomerContext(subscription_status='active'),
        knowledge_sections=(
            ProviderKnowledgeSection(
                section_id='APPS',
                source_file='apps.md',
                last_verified=date(2026, 7, 18),
                content='Используйте приложение из личного кабинета.',
                content_sha256='b' * 64,
            ),
        ),
    )


def _response(status: int = 200, *, model: str = 'claude-haiku-4-5-20251001') -> httpx.Response:
    return httpx.Response(
        status,
        json={
            'model': model,
            'stop_reason': 'end_turn',
            'content': [
                {
                    'type': 'text',
                    'text': '{"contract_version":1,"decision":"answer",'
                    '"answer_text":"Откройте личный кабинет.","citations":["APPS"],"reason_codes":[]}',
                }
            ],
            'usage': {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 50},
        },
    )


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_ANTHROPIC_API_KEY', 'test-secret')
    monkeypatch.setattr(settings, 'AI_SUPPORT_PROVIDER_MAX_RETRIES', 2)
    monkeypatch.setattr(settings, 'AI_SUPPORT_PROVIDER_CIRCUIT_FAILURES', 2)
    monkeypatch.setattr(settings, 'AI_SUPPORT_PROVIDER_CIRCUIT_RESET_SECONDS', 60.0)


@pytest.mark.asyncio
async def test_not_configured_never_calls_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_ANTHROPIC_API_KEY', '')
    client = AsyncMock()

    with pytest.raises(AiSupportProviderError, match='provider_not_configured'):
        await AnthropicSupportProvider(client=client).generate(_request(), Mock())

    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_uses_structured_output_and_validates_result(configured: None) -> None:
    client = AsyncMock()
    client.post.return_value = _response()
    validator = Mock()
    provider = AnthropicSupportProvider(client=client, clock=Mock(side_effect=[0.0, 10.0, 10.25]))

    result = await provider.generate(_request(), validator)

    assert result.result.decision == 'answer'
    assert result.latency_ms == 250
    assert result.usage.cache_read_tokens == 50
    validator.validate_result.assert_called_once_with(result.result)
    call = client.post.await_args
    assert call.kwargs['headers']['x-api-key'] == 'test-secret'
    assert 'test-secret' not in str(call.kwargs['json'])
    assert call.kwargs['json']['output_config']['format']['type'] == 'json_schema'


@pytest.mark.asyncio
async def test_retries_transient_status_and_honors_bounded_retry_after(configured: None) -> None:
    client = AsyncMock(side_effect=None)
    client.post.side_effect = [httpx.Response(529, headers={'retry-after': '99'}), _response()]
    sleep = AsyncMock()

    result = await AnthropicSupportProvider(client=client, sleep=sleep).generate(_request(), Mock())

    assert result.result.decision == 'answer'
    assert client.post.await_count == 2
    sleep.assert_awaited_once_with(2.0)


@pytest.mark.asyncio
async def test_authentication_error_is_not_retried_or_added_to_circuit(configured: None) -> None:
    client = AsyncMock()
    client.post.return_value = httpx.Response(401)
    provider = AnthropicSupportProvider(client=client)

    for _ in range(2):
        with pytest.raises(AiSupportProviderError, match='provider_http_401'):
            await provider.generate(_request(), Mock())

    assert client.post.await_count == 2


@pytest.mark.asyncio
async def test_circuit_opens_after_repeated_retryable_request_failures(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_PROVIDER_MAX_RETRIES', 0)
    client = AsyncMock()
    client.post.return_value = httpx.Response(529)
    provider = AnthropicSupportProvider(client=client, clock=lambda: 100.0)

    for _ in range(2):
        with pytest.raises(AiSupportProviderError, match='provider_http_529'):
            await provider.generate(_request(), Mock())
    with pytest.raises(AiSupportProviderError, match='provider_circuit_open'):
        await provider.generate(_request(), Mock())

    assert client.post.await_count == 2


@pytest.mark.asyncio
async def test_model_mismatch_and_truncation_fail_closed(configured: None) -> None:
    mismatch = AsyncMock()
    mismatch.post.return_value = _response(model='claude-other')
    with pytest.raises(AiSupportProviderError, match='provider_model_mismatch'):
        await AnthropicSupportProvider(client=mismatch).generate(_request(), Mock())

    truncated_response = _response()
    body = truncated_response.json()
    body['stop_reason'] = 'max_tokens'
    truncated = AsyncMock()
    truncated.post.return_value = httpx.Response(200, json=body)
    with pytest.raises(AiSupportProviderError, match='provider_truncated'):
        await AnthropicSupportProvider(client=truncated).generate(_request(), Mock())


def test_provider_configuration_is_bounded_and_secret_is_hidden() -> None:
    representation = repr(settings)
    assert 'AI_SUPPORT_ANTHROPIC_API_KEY' not in representation or 'test-secret' not in representation
    assert 1 <= settings.AI_SUPPORT_PROVIDER_TIMEOUT_SECONDS <= 30
    assert 1 <= settings.AI_SUPPORT_PROVIDER_MAX_CONCURRENCY <= 10
    assert 128 <= settings.AI_SUPPORT_PROVIDER_MAX_TOKENS <= 2000
