"""Offline synthetic evaluation runner with no ticket or customer database access."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.services.ai_support.contracts import AiSupportContextBuilder, SafeCustomerContext
from app.services.ai_support.knowledge import load_ai_support_knowledge
from app.services.ai_support.policy import assess_customer_message
from app.services.ai_support.provider import AnthropicSupportProvider
from app.services.ai_support.types import AiSupportDecision, AiSupportMode


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str = Field(pattern=r'^[A-Z]{3}-\d{3}$')
    category: str
    utterances: tuple[str, str]
    context: dict[str, Any]
    expected_decision: str
    allowed_sections: tuple[str, ...]
    required_points: tuple[str, ...]
    forbidden_points: tuple[str, ...]


class EvaluationSuite(BaseModel):
    model_config = ConfigDict(extra='forbid')

    suite_id: str
    kb_version: str
    case_count: int
    utterance_count: int
    cases: tuple[EvaluationCase, ...]


@dataclass(frozen=True, slots=True)
class EvaluationFailure:
    case_id: str
    utterance_index: int
    checks: tuple[str, ...]


def load_evaluation_suite(path: Path) -> EvaluationSuite:
    suite = EvaluationSuite.model_validate_json(path.read_text(encoding='utf-8'))
    if suite.case_count != len(suite.cases):
        raise ValueError('evaluation case_count mismatch')
    if suite.utterance_count != sum(len(case.utterances) for case in suite.cases):
        raise ValueError('evaluation utterance_count mismatch')
    return suite


def _safe_context(raw: dict[str, Any]) -> SafeCustomerContext:
    status = raw.get('subscription_status', 'unknown')
    if status not in {'unknown', 'none', 'trial', 'active', 'expired', 'pending'}:
        status = 'unknown'
    platform = raw.get('platform', 'unknown')
    if platform == 'mobile' or platform not in {
        'unknown',
        'ios',
        'android',
        'windows',
        'macos',
        'linux',
        'android_tv',
        'apple_tv',
    }:
        platform = 'unknown'
    tariff = raw.get('tariff')
    return SafeCustomerContext(
        subscription_status=status,
        tariff_name=tariff if isinstance(tariff, str) else None,
        traffic_used_bytes=_gb(raw.get('traffic_used_gb')),
        traffic_limit_bytes=_gb(raw.get('traffic_limit_gb')),
        device_count=_bounded_int(raw.get('device_count')),
        device_limit=_bounded_int(raw.get('device_limit')),
        platform=platform,
    )


def _gb(value: Any) -> int | None:
    return int(float(value) * 1024**3) if isinstance(value, (int, float)) and value >= 0 else None


def _bounded_int(value: Any) -> int | None:
    return value if isinstance(value, int) and 0 <= value <= 100 else None


class AiSupportEvaluationRunner:
    def __init__(self, *, provider: AnthropicSupportProvider, builder: AiSupportContextBuilder) -> None:
        self._provider = provider
        self._builder = builder

    async def run(self, suite: EvaluationSuite, *, limit: int | None = None) -> dict[str, Any]:
        failures: list[EvaluationFailure] = []
        processed = 0
        provider_calls = 0
        total_input_tokens = 0
        total_output_tokens = 0
        for case in suite.cases:
            for utterance_index, utterance in enumerate(case.utterances):
                if limit is not None and processed >= limit:
                    break
                processed += 1
                assessment = assess_customer_message(AiSupportMode.SHADOW, utterance)
                if assessment.decision is AiSupportDecision.ESCALATE:
                    decision, citations, answer = 'escalate', (), ''
                else:
                    request = self._builder.build_request(assessment, _safe_context(case.context))
                    response = await self._provider.generate(request, self._builder)
                    provider_calls += 1
                    total_input_tokens += response.usage.input_tokens
                    total_output_tokens += response.usage.output_tokens
                    decision = response.result.decision
                    citations = response.result.citations
                    answer = response.result.answer_text or ''
                checks = self._checks(case, decision=decision, citations=citations, answer=answer)
                if checks:
                    failures.append(EvaluationFailure(case.id, utterance_index, checks))
            if limit is not None and processed >= limit:
                break
        return {
            'suite_id': suite.suite_id,
            'kb_version': suite.kb_version,
            'processed': processed,
            'provider_calls': provider_calls,
            'passed': processed - len(failures),
            'failed': len(failures),
            'input_tokens': total_input_tokens,
            'output_tokens': total_output_tokens,
            'failures': [
                {'case_id': item.case_id, 'utterance_index': item.utterance_index, 'checks': item.checks}
                for item in failures
            ],
        }

    @staticmethod
    def _checks(
        case: EvaluationCase,
        *,
        decision: str,
        citations: tuple[str, ...],
        answer: str,
    ) -> tuple[str, ...]:
        checks: list[str] = []
        folded = answer.casefold()
        if decision != case.expected_decision:
            checks.append('decision')
        if not set(citations) <= set(case.allowed_sections):
            checks.append('citations')
        if decision == 'answer':
            if any(point.casefold() not in folded for point in case.required_points):
                checks.append('required_points')
            if any(point.casefold() in folded for point in case.forbidden_points):
                checks.append('forbidden_points')
        return tuple(checks)


async def _main(args: argparse.Namespace) -> int:
    if not args.live:
        raise SystemExit('Refusing provider calls without explicit --live')
    suite = load_evaluation_suite(args.cases)
    knowledge = load_ai_support_knowledge()
    if suite.kb_version != knowledge.kb_version:
        raise SystemExit('Evaluation and runtime KB versions differ')
    report = await AiSupportEvaluationRunner(
        provider=AnthropicSupportProvider(),
        builder=AiSupportContextBuilder(knowledge),
    ).run(suite, limit=args.limit)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['failed'] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=1, choices=range(1, 117))
    parser.add_argument('--live', action='store_true')
    return asyncio.run(_main(parser.parse_args()))


if __name__ == '__main__':
    raise SystemExit(main())
