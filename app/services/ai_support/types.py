"""Provider-independent AI support types.

These types intentionally have no database or Anthropic dependency. They are
safe to introduce before the durable queue migration is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AiSupportMode(StrEnum):
    OFF = 'off'
    SHADOW = 'shadow'
    AUTO = 'auto'


class AiSupportDecision(StrEnum):
    DISABLED = 'disabled'
    CALL_PROVIDER = 'call_provider'
    ESCALATE = 'escalate'


class RedactionCategory(StrEnum):
    CREDENTIAL = 'credential'
    EMAIL = 'email'
    IPV4 = 'ipv4'
    IPV6 = 'ipv6'
    LONG_IDENTIFIER = 'long_identifier'
    PHONE = 'phone'
    TELEGRAM_USERNAME = 'telegram_username'
    URL = 'url'
    UUID = 'uuid'
    TRUNCATED = 'truncated'


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    categories: frozenset[RedactionCategory]

    @property
    def changed(self) -> bool:
        return bool(self.categories)


@dataclass(frozen=True, slots=True)
class MessageAssessment:
    decision: AiSupportDecision
    reason_codes: tuple[str, ...]
    redaction: RedactionResult
