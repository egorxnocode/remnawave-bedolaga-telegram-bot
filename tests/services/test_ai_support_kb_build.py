from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_ai_support_kb_artifact import build_artifact


def _write_manifest(source: Path, documents: list[str]) -> None:
    (source / 'manifest.json').write_text(
        json.dumps(
            {
                'kb_version': 'test-v1',
                'language': 'ru',
                'audience': 'customer',
                'documents': documents,
            }
        ),
        encoding='utf-8',
    )


def test_build_artifact_is_deterministic_and_strips_front_matter(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    _write_manifest(source, ['policy.md'])
    (source / 'policy.md').write_text(
        '---\n'
        'section_id: POLICY\n'
        'audience: customer\n'
        'status: verified\n'
        'last_verified: 2026-07-18\n'
        '---\n\n'
        '# Безопасная справка\n',
        encoding='utf-8',
    )

    first = build_artifact(source)
    second = build_artifact(source)

    assert first == second
    assert first['document_count'] == 1
    assert first['sections'][0]['content'] == '# Безопасная справка\n'  # type: ignore[index]


def test_build_artifact_rejects_manifest_path_traversal(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    _write_manifest(source, ['../internal.md'])

    with pytest.raises(ValueError, match='unsafe manifest document path'):
        build_artifact(source)


def test_build_artifact_rejects_sensitive_content(tmp_path: Path) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    _write_manifest(source, ['policy.md'])
    (source / 'policy.md').write_text(
        '---\n'
        'section_id: POLICY\n'
        'audience: customer\n'
        'status: verified\n'
        'last_verified: 2026-07-18\n'
        '---\n\n'
        '# Внутреннее\nАдрес 192.0.2.1\n',
        encoding='utf-8',
    )

    with pytest.raises(ValueError, match='sensitive-data scan'):
        build_artifact(source)
