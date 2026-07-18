"""Fail-closed foundations for ticket-based AI support."""

from app.services.ai_support.policy import assess_customer_message, redact_customer_text
from app.services.ai_support.types import (
    AiSupportDecision,
    AiSupportMode,
    MessageAssessment,
    RedactionCategory,
    RedactionResult,
)


__all__ = [
    'AiSupportDecision',
    'AiSupportMode',
    'MessageAssessment',
    'RedactionCategory',
    'RedactionResult',
    'assess_customer_message',
    'redact_customer_text',
]
