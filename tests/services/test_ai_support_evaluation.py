import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.ai_support.contracts import AiSupportProviderResult, AiSupportProviderUsage
from app.services.ai_support.evaluation import AiSupportEvaluationRunner, load_evaluation_suite


def test_suite_loader_rejects_declared_count_mismatch(tmp_path: Path) -> None:
    path = tmp_path / 'cases.json'
    path.write_text(
        json.dumps(
            {
                'suite_id': 'test',
                'kb_version': '2026-07-18.1',
                'case_count': 1,
                'utterance_count': 0,
                'cases': [],
            }
        ),
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='case_count'):
        load_evaluation_suite(path)


@pytest.mark.asyncio
async def test_runner_scores_provider_output_without_returning_answer_text(tmp_path: Path) -> None:
    path = tmp_path / 'cases.json'
    path.write_text(
        json.dumps(
            {
                'suite_id': 'test',
                'kb_version': '2026-07-18.1',
                'case_count': 1,
                'utterance_count': 2,
                'cases': [
                    {
                        'id': 'APP-001',
                        'category': 'apps',
                        'utterances': ['Как подключиться?', 'Помогите подключить VPN'],
                        'context': {'subscription_status': 'active'},
                        'expected_decision': 'answer',
                        'allowed_sections': ['APPS'],
                        'required_points': ['кабинет'],
                        'forbidden_points': ['секрет'],
                    }
                ],
            }
        ),
        encoding='utf-8',
    )
    provider = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(
                result=AiSupportProviderResult(decision='answer', answer_text='Откройте кабинет.', citations=('APPS',)),
                usage=AiSupportProviderUsage(input_tokens=10, output_tokens=5),
            )
        )
    )
    builder = SimpleNamespace(
        build_request=lambda assessment, context: SimpleNamespace(assessment=assessment, context=context)
    )

    report = await AiSupportEvaluationRunner(provider=provider, builder=builder).run(load_evaluation_suite(path))

    assert report['processed'] == 2
    assert report['passed'] == 2
    assert report['provider_calls'] == 2
    assert report['input_tokens'] == 20
    assert 'answer_text' not in json.dumps(report)


def test_scoring_detects_bad_citation_missing_and_forbidden_points() -> None:
    case = SimpleNamespace(
        expected_decision='answer',
        allowed_sections=('APPS',),
        required_points=('кабинет',),
        forbidden_points=('секрет',),
    )
    checks = AiSupportEvaluationRunner._checks(
        case,
        decision='answer',
        citations=('WRONG',),
        answer='Передайте секрет.',
    )
    assert checks == ('citations', 'required_points', 'forbidden_points')
