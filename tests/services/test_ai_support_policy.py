from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.ai_support import (
    AiSupportDecision,
    AiSupportMode,
    RedactionCategory,
    assess_customer_message,
    redact_customer_text,
)


def test_settings_default_to_off() -> None:
    candidate = Settings(BOT_TOKEN='test')
    assert candidate.AI_SUPPORT_MODE == 'off'


def test_settings_reject_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        Settings(BOT_TOKEN='test', AI_SUPPORT_MODE='enabled')


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('AI_SUPPORT_PROMPT_VERSION', '../prompt'),
        ('AI_SUPPORT_KB_VERSION', 'x' * 65),
    ],
)
def test_settings_reject_unsafe_audit_versions(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(BOT_TOKEN='test', **{field: value})


def test_off_mode_is_inert_and_does_not_transform_text() -> None:
    raw = 'token=top-secret'
    result = assess_customer_message(AiSupportMode.OFF, raw)
    assert result.decision is AiSupportDecision.DISABLED
    assert result.reason_codes == ('mode_off',)
    assert result.redaction.text == raw
    assert not result.redaction.changed


@pytest.mark.parametrize('mode', [AiSupportMode.SHADOW, AiSupportMode.AUTO])
def test_safe_routine_message_is_eligible(mode: AiSupportMode) -> None:
    result = assess_customer_message(mode, 'Как подключить телефон к подписке?')
    assert result.decision is AiSupportDecision.CALL_PROVIDER
    assert result.reason_codes == ('eligible',)


@pytest.mark.parametrize(
    ('message', 'category'),
    [
        ('Моя почта user@example.com', RedactionCategory.EMAIL),
        ('Телефон +7 (999) 123-45-67', RedactionCategory.PHONE),
        ('Telegram @customer_name', RedactionCategory.TELEGRAM_USERNAME),
        ('Заказ 123456789012', RedactionCategory.LONG_IDENTIFIER),
    ],
)
def test_personal_identifiers_are_redacted_before_provider(message: str, category: RedactionCategory) -> None:
    result = assess_customer_message(AiSupportMode.SHADOW, message)
    assert result.decision is AiSupportDecision.CALL_PROVIDER
    assert category in result.redaction.categories
    assert '[REDACTED_' in result.redaction.text


@pytest.mark.parametrize(
    ('message', 'category'),
    [
        ('token=super-secret-value', RedactionCategory.CREDENTIAL),
        ('Ссылка https://example.com/sub/secret', RedactionCategory.URL),
        ('IP 192.0.2.10', RedactionCategory.IPV4),
        ('ID 123e4567-e89b-12d3-a456-426614174000', RedactionCategory.UUID),
    ],
)
def test_high_risk_values_force_handoff(message: str, category: RedactionCategory) -> None:
    result = assess_customer_message(AiSupportMode.SHADOW, message)
    assert result.decision is AiSupportDecision.ESCALATE
    assert result.reason_codes[0] == 'sensitive_data'
    assert category in result.redaction.categories
    assert message not in result.redaction.text


@pytest.mark.parametrize(
    ('message', 'reason'),
    [
        ('Позовите живого оператора', 'human_requested'),
        ('Хочу оформить возврат', 'financial_dispute'),
        ('Удалите мои персональные данные', 'legal_or_privacy'),
        ('Кажется, мой аккаунт взломали', 'security_or_abuse'),
    ],
)
def test_mandatory_handoff_is_deterministic(message: str, reason: str) -> None:
    result = assess_customer_message(AiSupportMode.SHADOW, message)
    assert result.decision is AiSupportDecision.ESCALATE
    assert result.reason_codes == (reason,)


def test_attachment_never_reaches_provider() -> None:
    result = assess_customer_message(AiSupportMode.SHADOW, 'Посмотрите скриншот', has_media=True)
    assert result.decision is AiSupportDecision.ESCALATE
    assert result.reason_codes == ('attachment',)


def test_oversized_message_is_truncated_and_escalated() -> None:
    result = assess_customer_message(AiSupportMode.SHADOW, 'я' * 4_001)
    assert result.decision is AiSupportDecision.ESCALATE
    assert RedactionCategory.TRUNCATED in result.redaction.categories
    assert len(result.redaction.text) == 4_000


def test_redactor_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError, match='max_chars must be positive'):
        redact_customer_text('test', max_chars=0)
