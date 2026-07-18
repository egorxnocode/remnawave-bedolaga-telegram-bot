from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services.ai_support.knowledge import (
    DEFAULT_ARTIFACT_PATH,
    EXPECTED_PACKAGE_SHA256,
    EXPECTED_SOURCE_FILES,
    AiSupportKnowledgeError,
    load_ai_support_knowledge,
)


def _write_artifact(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / 'customer-support.json'
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
    return path


def _package_digest(sections: list[dict]) -> str:
    canonical = json.dumps(
        sections,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


def test_default_artifact_loads_only_exact_reviewed_allowlist() -> None:
    package = load_ai_support_knowledge()

    assert package.kb_version == '2026-07-18.1'
    assert package.language == 'ru'
    assert package.audience == 'customer'
    assert package.package_sha256 == EXPECTED_PACKAGE_SHA256
    assert tuple(section.source_file for section in package.sections) == EXPECTED_SOURCE_FILES
    assert len(package.sections) == 9
    assert package.sections[0].section_id == 'POLICY'
    assert all('evals/' not in section.source_file for section in package.sections)
    assert all(section.source_file != 'README.md' for section in package.sections)
    assert all(not section.content.startswith('---') for section in package.sections)


def test_tampered_content_fails_closed(tmp_path: Path) -> None:
    raw = json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding='utf-8'))
    raw['sections'][0]['content'] += '\nsecret mutation'
    path = _write_artifact(tmp_path, raw)

    with pytest.raises(AiSupportKnowledgeError, match='package digest mismatch'):
        load_ai_support_knowledge(path)


def test_rehashed_unapproved_source_file_still_fails_allowlist(tmp_path: Path) -> None:
    raw = json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding='utf-8'))
    raw['sections'][0]['source_file'] = 'internal-ops.md'
    raw['package_sha256'] = _package_digest(raw['sections'])
    path = _write_artifact(tmp_path, raw)

    with pytest.raises(AiSupportKnowledgeError, match='source allowlist mismatch'):
        load_ai_support_knowledge(path)


def test_runtime_version_mismatch_fails_closed() -> None:
    with pytest.raises(AiSupportKnowledgeError, match='version mismatch'):
        load_ai_support_knowledge(expected_version='future-version')


def test_unknown_citation_is_rejected() -> None:
    package = load_ai_support_knowledge()

    with pytest.raises(AiSupportKnowledgeError, match='unknown knowledge citations'):
        package.validate_citations(('POLICY', 'INTERNAL_OPS'))
