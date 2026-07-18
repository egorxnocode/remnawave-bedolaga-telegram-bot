"""Deterministic privacy and escalation gate for customer messages."""

from __future__ import annotations

import re

from app.services.ai_support.types import (
    AiSupportDecision,
    AiSupportMode,
    MessageAssessment,
    RedactionCategory,
    RedactionResult,
)


MAX_PROVIDER_TEXT_CHARS = 4_000

_PATTERNS: tuple[tuple[RedactionCategory, re.Pattern[str]], ...] = (
    (
        RedactionCategory.CREDENTIAL,
        re.compile(
            r'(?i)\b(?:bearer\s+[a-z0-9._~+/=-]{12,}|'
            r'(?:api[_ -]?key|token|password|secret)\s*[:=]\s*\S+)'
        ),
    ),
    (RedactionCategory.URL, re.compile(r'(?i)(?:https?://|tg://|t\.me/)\S+')),
    (
        RedactionCategory.EMAIL,
        re.compile(r'(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+(?![\w.-])'),
    ),
    (
        RedactionCategory.UUID,
        re.compile(
            r'(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-'
            r'[89ab][0-9a-f]{3}-[0-9a-f]{12}\b'
        ),
    ),
    (
        RedactionCategory.IPV4,
        re.compile(r'(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])'),
    ),
    (
        RedactionCategory.IPV6,
        re.compile(r'(?i)(?<![\w:])(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{0,4}(?![\w:])'),
    ),
    (
        RedactionCategory.LONG_IDENTIFIER,
        re.compile(r'(?<!\d)\d{8,}(?!\d)'),
    ),
    (
        RedactionCategory.PHONE,
        re.compile(r'(?<!\w)(?:\+?\d[\s().-]*){10,15}(?!\w)'),
    ),
    (RedactionCategory.TELEGRAM_USERNAME, re.compile(r'(?<!\w)@[a-zA-Z][a-zA-Z0-9_]{4,31}\b')),
)

_HUMAN_REQUEST_RE = re.compile(
    r'(?i)\b(?:позов(?:и|ите)|соедин(?:и|ите)|переключ(?:и|ите))\b.{0,30}\b(?:человек|оператор|специалист)|'
    r'\b(?:живой\s+(?:человек|оператор)|хочу\s+(?:человека|оператора|специалиста))\b'
)
_MANDATORY_HANDOFF_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        'financial_dispute',
        re.compile(
            r'(?i)\b(?:возврат|верн(?:ите|уть)\s+деньги|чарджбэк|списал[ио]?\s+(?:дважды|лишн)|'
            r'двойн(?:ое|ого)\s+списан|оспорить\s+плат[её]ж)\b'
        ),
    ),
    (
        'legal_or_privacy',
        re.compile(
            r'(?i)\b(?:персональн\w*\s+данн\w*|удал(?:ите|ить)\s+(?:мои\s+)?данн\w*|'
            r'претензи\w*|суд|полици\w*\s+конфиденциальност\w*|публичн\w*\s+оферт\w*)\b'
        ),
    ),
    (
        'security_or_abuse',
        re.compile(r'(?i)\b(?:взлом\w*|украл\w*\s+аккаунт|мошенн\w*|угроз\w*|ddos|компрометаци\w*)\b'),
    ),
)

_HIGH_RISK_REDACTIONS = {
    RedactionCategory.CREDENTIAL,
    RedactionCategory.IPV4,
    RedactionCategory.IPV6,
    RedactionCategory.URL,
    RedactionCategory.UUID,
    RedactionCategory.TRUNCATED,
}


def redact_customer_text(text: str, *, max_chars: int = MAX_PROVIDER_TEXT_CHARS) -> RedactionResult:
    """Replace personal/secret-like values with stable non-reversible markers."""
    if max_chars < 1:
        raise ValueError('max_chars must be positive')

    categories: set[RedactionCategory] = set()
    sanitized = text
    if len(sanitized) > max_chars:
        sanitized = sanitized[:max_chars]
        categories.add(RedactionCategory.TRUNCATED)

    for category, pattern in _PATTERNS:

        def replace(_match: re.Match[str], *, current: RedactionCategory = category) -> str:
            categories.add(current)
            return f'[REDACTED_{current.value.upper()}]'

        sanitized = pattern.sub(replace, sanitized)

    return RedactionResult(text=sanitized.strip(), categories=frozenset(categories))


def assess_customer_message(
    mode: AiSupportMode | str,
    text: str,
    *,
    has_media: bool = False,
) -> MessageAssessment:
    """Decide whether a sanitized message may reach a future provider.

    This function never calls a model and never mutates ticket state. In OFF
    mode it returns immediately, which keeps the not-yet-integrated feature
    completely inert.
    """
    active_mode = AiSupportMode(mode)
    untouched = RedactionResult(text=text, categories=frozenset())
    if active_mode is AiSupportMode.OFF:
        return MessageAssessment(AiSupportDecision.DISABLED, ('mode_off',), untouched)
    if has_media:
        return MessageAssessment(AiSupportDecision.ESCALATE, ('attachment',), untouched)
    if _HUMAN_REQUEST_RE.search(text):
        return MessageAssessment(AiSupportDecision.ESCALATE, ('human_requested',), untouched)

    for reason_code, pattern in _MANDATORY_HANDOFF_PATTERNS:
        if pattern.search(text):
            return MessageAssessment(AiSupportDecision.ESCALATE, (reason_code,), untouched)

    redaction = redact_customer_text(text)
    if not redaction.text:
        return MessageAssessment(AiSupportDecision.ESCALATE, ('empty_message',), redaction)

    high_risk = sorted(category.value for category in redaction.categories & _HIGH_RISK_REDACTIONS)
    if high_risk:
        return MessageAssessment(
            AiSupportDecision.ESCALATE,
            ('sensitive_data', *high_risk),
            redaction,
        )

    return MessageAssessment(AiSupportDecision.CALL_PROVIDER, ('eligible',), redaction)
