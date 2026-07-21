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
from app.services.ai_support.provider import AiSupportProviderError, OpenRouterSupportProvider


_VALID_JSON = (
    '{"contract_version":1,"decision":"answer",'
    '"answer_text":"Откройте личный кабинет.","citations":["APPS"],"reason_codes":[]}'
)


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


def _response(
    status: int = 200,
    *,
    model: str = 'anthropic/claude-haiku-4-5',
    finish_reason: str | None = 'stop',
    content: str = _VALID_JSON,
) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            'id': 'chatcmpl-x',
            'object': 'chat.completion',
            'model': model,
            'choices': [
                {
                    'index': 0,
                    'message': {'role': 'assistant', 'content': content},
                    'finish_reason': finish_reason,
                }
            ],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20},
        },
    )


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_PROVIDER_API_KEY', 'test-or-secret')
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODEL_ID', 'anthropic/claude-haiku-4-5')
    monkeypatch.setattr(settings, 'AI_SUPPORT_PROVIDER_MAX_RETRIES', 2)
    monkeypatch.setattr(settings, 'AI_SUPPORT_PROVIDER_CIRCUIT_FAILURES', 2)
    monkeypatch.setattr(settings, 'AI_SUPPORT_PROVIDER_CIRCUIT_RESET_SECONDS', 60.0)


@pytest.mark.asyncio
async def test_not_configured_never_calls_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_PROVIDER_API_KEY', '')
    client = AsyncMock()

    with pytest.raises(AiSupportProviderError, match='provider_not_configured'):
        await OpenRouterSupportProvider(client=client).generate(_request(), Mock())

    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_uses_bearer_auth_and_prompt_json(configured: None) -> None:
    client = AsyncMock()
    client.post.return_value = _response()
    validator = Mock()
    provider = OpenRouterSupportProvider(client=client, clock=Mock(side_effect=[0.0, 10.0, 10.25]))

    result = await provider.generate(_request(), validator)

    assert result.result.decision == 'answer'
    assert result.provider == 'openrouter'
    assert result.model_id == 'anthropic/claude-haiku-4-5'
    assert result.latency_ms == 250
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 20
    assert result.usage.cache_read_tokens == 0
    validator.validate_result.assert_called_once_with(result.result)
    call = client.post.await_args
    assert call.kwargs['headers']['Authorization'] == 'Bearer test-or-secret'
    assert 'test-or-secret' not in str(call.kwargs['json'])
    assert 'output_config' not in call.kwargs['json']
    assert 'response_format' not in call.kwargs['json']
    assert call.kwargs['json']['messages'][0]['role'] == 'system'
    assert call.kwargs['json']['messages'][1]['role'] == 'user'
    assert call.kwargs['json']['model'] == 'anthropic/claude-haiku-4-5'


@pytest.mark.asyncio
async def test_retries_transient_status_and_honors_bounded_retry_after(configured: None) -> None:
    client = AsyncMock()
    client.post.side_effect = [httpx.Response(529, headers={'retry-after': '99'}), _response()]
    sleep = AsyncMock()

    result = await OpenRouterSupportProvider(client=client, sleep=sleep).generate(_request(), Mock())

    assert result.result.decision == 'answer'
    assert client.post.await_count == 2
    sleep.assert_awaited_once_with(2.0)


@pytest.mark.asyncio
async def test_authentication_error_is_not_retried_or_added_to_circuit(configured: None) -> None:
    client = AsyncMock()
    client.post.return_value = httpx.Response(401)

    for _ in range(2):
        with pytest.raises(AiSupportProviderError, match='provider_http_401'):
            await OpenRouterSupportProvider(client=client).generate(_request(), Mock())

    assert client.post.await_count == 2


@pytest.mark.asyncio
async def test_circuit_opens_after_repeated_retryable_request_failures(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, 'AI_SUPPORT_PROVIDER_MAX_RETRIES', 0)
    client = AsyncMock()
    client.post.return_value = httpx.Response(529)
    provider = OpenRouterSupportProvider(client=client, clock=lambda: 100.0)

    for _ in range(2):
        with pytest.raises(AiSupportProviderError, match='provider_http_529'):
            await provider.generate(_request(), Mock())
    with pytest.raises(AiSupportProviderError, match='provider_circuit_open'):
        await provider.generate(_request(), Mock())

    assert client.post.await_count == 2


@pytest.mark.asyncio
async def test_truncation_fail_closed(configured: None) -> None:
    client = AsyncMock()
    client.post.return_value = _response(finish_reason='length')

    with pytest.raises(AiSupportProviderError, match='provider_truncated'):
        await OpenRouterSupportProvider(client=client).generate(_request(), Mock())


@pytest.mark.asyncio
async def test_fenced_json_is_extracted(configured: None) -> None:
    client = AsyncMock()
    client.post.return_value = _response(content='```json\n' + _VALID_JSON + '\n```')

    result = await OpenRouterSupportProvider(client=client).generate(_request(), Mock())

    assert result.result.decision == 'answer'


@pytest.mark.asyncio
async def test_invalid_json_fail_closed(configured: None) -> None:
    client = AsyncMock()
    client.post.return_value = _response(content='Извините, я не могу помочь с этим вопросом.')

    with pytest.raises(AiSupportProviderError, match='provider_invalid_response'):
        await OpenRouterSupportProvider(client=client).generate(_request(), Mock())


@pytest.mark.asyncio
async def test_insufficient_credits_not_retried(configured: None) -> None:
    client = AsyncMock()
    client.post.return_value = httpx.Response(402)

    with pytest.raises(AiSupportProviderError, match='provider_http_402'):
        await OpenRouterSupportProvider(client=client).generate(_request(), Mock())

    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_model_slug_with_slash_is_recorded_without_mismatch(configured: None) -> None:
    client = AsyncMock()
    # Response echoes a different slug than the request model; OpenRouter may normalize.
    client.post.return_value = _response(model='anthropic/claude-haiku-4-5-20251001')

    result = await OpenRouterSupportProvider(client=client).generate(_request(), Mock())

    assert result.model_id == 'anthropic/claude-haiku-4-5-20251001'
    assert result.provider == 'openrouter'
