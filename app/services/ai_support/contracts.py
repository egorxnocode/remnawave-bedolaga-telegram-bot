"""Provider-neutral request/result contracts for AI support."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import settings
from app.services.ai_support.knowledge import (
    AiSupportKnowledgePackage,
    AiSupportKnowledgeSection,
)
from app.services.ai_support.policy import redact_customer_text
from app.services.ai_support.types import AiSupportDecision, MessageAssessment, RedactionCategory


SAFE_CODE_RE = re.compile(r'^[a-z][a-z0-9_.-]{0,63}$')
SECTION_ID_RE = re.compile(r'^[A-Z][A-Z0-9_]{0,63}$')
URL_RE = re.compile(r'https?://[^\s)>]+')


class AiSupportContractError(RuntimeError):
    """Raised when safe context or a provider result violates the contract."""


class SafePaymentContext(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    provider: str = Field(min_length=1, max_length=32, pattern=r'^[a-z0-9][a-z0-9_-]*$')
    status: Literal['pending', 'succeeded', 'failed', 'expired', 'cancelled']
    amount_kopeks: int = Field(ge=0)
    observed_at: datetime

    @field_validator('observed_at')
    @classmethod
    def observed_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('observed_at must be timezone-aware')
        return value.astimezone(UTC)


class SafeCustomerContext(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    subscription_status: Literal['unknown', 'none', 'trial', 'active', 'expired', 'pending'] = 'unknown'
    tariff_name: str | None = Field(default=None, min_length=1, max_length=80)
    expires_on: date | None = None
    traffic_used_bytes: int | None = Field(default=None, ge=0)
    traffic_limit_bytes: int | None = Field(default=None, ge=0)
    device_count: int | None = Field(default=None, ge=0, le=100)
    device_limit: int | None = Field(default=None, ge=0, le=100)
    platform: Literal['unknown', 'ios', 'android', 'windows', 'macos', 'linux', 'android_tv', 'apple_tv'] = (
        'unknown'
    )
    latest_payment: SafePaymentContext | None = None

    @field_validator('tariff_name')
    @classmethod
    def tariff_name_must_not_contain_identifiers(cls, value: str | None) -> str | None:
        if value is not None and redact_customer_text(value).changed:
            raise ValueError('tariff_name contains unsafe identifiers')
        return value


class ProviderKnowledgeSection(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    section_id: str = Field(pattern=r'^[A-Z][A-Z0-9_]{0,63}$')
    source_file: str = Field(min_length=4, max_length=128, pattern=r'^[a-z0-9][a-z0-9-]*\.md$')
    last_verified: date
    content: str = Field(min_length=1, max_length=32_000)
    content_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class AiSupportProviderRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    contract_version: Literal[1] = 1
    prompt_version: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9][a-z0-9._-]*$')
    kb_version: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9][a-z0-9._-]*$')
    kb_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    language: Literal['ru'] = 'ru'
    customer_message: str = Field(min_length=1, max_length=4_000)
    customer_context: SafeCustomerContext
    knowledge_sections: tuple[ProviderKnowledgeSection, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode='after')
    def knowledge_section_ids_must_be_unique(self) -> AiSupportProviderRequest:
        ids = [section.section_id for section in self.knowledge_sections]
        if len(ids) != len(set(ids)):
            raise ValueError('knowledge section ids must be unique')
        return self


class AiSupportProviderResult(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    contract_version: Literal[1] = 1
    decision: Literal['answer', 'escalate']
    answer_text: str | None = Field(default=None, min_length=1, max_length=2_000)
    citations: tuple[str, ...] = Field(default=(), max_length=32)
    reason_codes: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator('citations')
    @classmethod
    def citations_must_be_safe_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not SECTION_ID_RE.fullmatch(item) for item in value):
            raise ValueError('citations must contain unique section ids')
        return value

    @field_validator('reason_codes')
    @classmethod
    def reason_codes_must_be_safe_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not SAFE_CODE_RE.fullmatch(item) for item in value):
            raise ValueError('reason codes must be unique safe codes')
        return value

    @model_validator(mode='after')
    def decision_fields_must_match(self) -> AiSupportProviderResult:
        if self.decision == 'answer' and (not self.answer_text or not self.citations):
            raise ValueError('answer decision requires text and citations')
        if self.decision == 'escalate' and self.answer_text is not None:
            raise ValueError('escalation must not contain customer answer text')
        return self


class AiSupportProviderUsage(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)


class AiSupportProviderResponse(BaseModel):
    """Validated adapter output without any raw provider payload."""

    model_config = ConfigDict(extra='forbid', frozen=True)

    provider: str = Field(min_length=1, max_length=32, pattern=r'^[a-z0-9][a-z0-9_-]*$')
    model_id: str = Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$')
    latency_ms: int = Field(ge=0)
    usage: AiSupportProviderUsage = Field(default_factory=AiSupportProviderUsage)
    result: AiSupportProviderResult


class AiSupportContextBuilder:
    """Build requests only from deterministic redaction and the reviewed KB."""

    def __init__(self, knowledge: AiSupportKnowledgePackage) -> None:
        if knowledge.kb_version != settings.AI_SUPPORT_KB_VERSION:
            raise AiSupportContractError('knowledge version does not match runtime settings')
        self._knowledge = knowledge

    def build_request(
        self,
        assessment: MessageAssessment,
        customer_context: SafeCustomerContext,
    ) -> AiSupportProviderRequest:
        if assessment.decision is not AiSupportDecision.CALL_PROVIDER:
            raise AiSupportContractError('message is not eligible for a provider call')
        if redact_customer_text(assessment.redaction.text).changed:
            raise AiSupportContractError('customer message is not fully redacted')
        sections = tuple(self._provider_section(section) for section in self._knowledge.sections)
        return AiSupportProviderRequest(
            prompt_version=settings.AI_SUPPORT_PROMPT_VERSION,
            kb_version=self._knowledge.kb_version,
            kb_sha256=self._knowledge.package_sha256,
            customer_message=assessment.redaction.text,
            customer_context=customer_context,
            knowledge_sections=sections,
        )

    def validate_result(self, result: AiSupportProviderResult) -> None:
        self._knowledge.validate_citations(result.citations)
        if result.answer_text is None:
            return
        redaction = redact_customer_text(result.answer_text)
        unsafe_categories = redaction.categories - {RedactionCategory.URL}
        if unsafe_categories:
            raise AiSupportContractError('provider answer contains unsafe identifiers')
        if RedactionCategory.URL in redaction.categories:
            allowed_urls = {
                url.rstrip('.,')
                for section in self._knowledge.sections
                for url in URL_RE.findall(section.content)
            }
            answer_urls = {url.rstrip('.,') for url in URL_RE.findall(result.answer_text)}
            if not answer_urls <= allowed_urls:
                raise AiSupportContractError('provider answer contains an unapproved URL')

    @staticmethod
    def _provider_section(section: AiSupportKnowledgeSection) -> ProviderKnowledgeSection:
        return ProviderKnowledgeSection(
            section_id=section.section_id,
            source_file=section.source_file,
            last_verified=section.last_verified,
            content=section.content,
            content_sha256=section.content_sha256,
        )
