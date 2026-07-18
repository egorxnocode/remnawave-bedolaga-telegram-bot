"""Fail-closed foundations for ticket-based AI support."""

from app.services.ai_support.dispatch import (
    AiSupportDispatchResult,
    AiSupportDispatchService,
    AiSupportDispatchStatus,
    ai_support_dispatch_service,
)
from app.services.ai_support.policy import assess_customer_message, redact_customer_text
from app.services.ai_support.types import (
    AiSupportDecision,
    AiSupportMode,
    MessageAssessment,
    RedactionCategory,
    RedactionResult,
)
from app.services.ai_support.worker import (
    AiSupportWorker,
    AiSupportWorkerResult,
    AiSupportWorkerStatus,
    ai_support_worker,
)


__all__ = [
    'AiSupportDecision',
    'AiSupportDispatchResult',
    'AiSupportDispatchService',
    'AiSupportDispatchStatus',
    'AiSupportMode',
    'AiSupportWorker',
    'AiSupportWorkerResult',
    'AiSupportWorkerStatus',
    'MessageAssessment',
    'RedactionCategory',
    'RedactionResult',
    'ai_support_dispatch_service',
    'ai_support_worker',
    'assess_customer_message',
    'redact_customer_text',
]
