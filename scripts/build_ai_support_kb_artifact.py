#!/usr/bin/env python3
"""Build the runtime AI-support KB artifact from the reviewed manifest only."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path


SECTION_ID_RE = re.compile(r'^[A-Z][A-Z0-9_]{0,63}$')
EXPECTED_AUDIENCE = 'customer'
EXPECTED_LANGUAGE = 'ru'
EXPECTED_STATUS = 'verified'
REQUIRED_FRONT_MATTER = {'section_id', 'audience', 'status', 'last_verified'}
ALLOWED_PUBLIC_URLS = {
    'https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-07-14-52',
    'https://telegra.ph/PUBLICHNAYA-OFERTA-07-14-6',
}
URL_RE = re.compile(r'https?://[^\s)>]+')
SENSITIVE_PATTERNS = (
    re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])'),
    re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b'),
    re.compile(r'BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY'),
    re.compile(r'\bsk-ant-[A-Za-z0-9_-]+'),
    re.compile(r'(?i)\b(?:Remnawave|MultiRoller|Caddy|Xray|VPNBOT|REMNAPANEL)\b'),
    re.compile(r'(?i)\b(?:[a-z0-9-]+\.)*(?:nasvyazi\.site|panelnasvyazi\.xyz)\b'),
)


def _parse_front_matter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding='utf-8')
    lines = text.splitlines()
    if not lines or lines[0] != '---':
        raise ValueError(f'{path.name}: missing front matter')
    try:
        end = lines.index('---', 1)
    except ValueError as error:
        raise ValueError(f'{path.name}: unclosed front matter') from error

    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        if ':' not in line:
            raise ValueError(f'{path.name}: invalid front matter line')
        key, value = line.split(':', 1)
        fields[key.strip()] = value.strip()
    missing = REQUIRED_FRONT_MATTER - fields.keys()
    if missing:
        raise ValueError(f'{path.name}: missing front matter fields {sorted(missing)}')

    content = '\n'.join(lines[end + 1 :]).strip() + '\n'
    if not content.strip():
        raise ValueError(f'{path.name}: empty content')
    return fields, content


def _canonical_sections(sections: list[dict[str, str]]) -> bytes:
    return json.dumps(
        sections,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def build_artifact(source: Path) -> dict[str, object]:
    source = source.resolve()
    manifest_path = source / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('language') != EXPECTED_LANGUAGE:
        raise ValueError('manifest language must be ru')
    if manifest.get('audience') != EXPECTED_AUDIENCE:
        raise ValueError('manifest audience must be customer')

    kb_version = manifest.get('kb_version')
    if not isinstance(kb_version, str) or not kb_version:
        raise ValueError('manifest kb_version must be a non-empty string')
    documents = manifest.get('documents')
    if not isinstance(documents, list) or not documents:
        raise ValueError('manifest documents must be a non-empty list')
    if len(documents) != len(set(documents)):
        raise ValueError('manifest documents contain duplicates')

    sections: list[dict[str, str]] = []
    section_ids: set[str] = set()
    for relative in documents:
        if not isinstance(relative, str):
            raise ValueError('manifest document name must be a string')
        candidate = Path(relative)
        if candidate.is_absolute() or '..' in candidate.parts or len(candidate.parts) != 1:
            raise ValueError(f'unsafe manifest document path: {relative!r}')
        if candidate.suffix != '.md':
            raise ValueError(f'manifest document must be Markdown: {relative!r}')

        path = source / candidate
        if not path.is_file():
            raise ValueError(f'manifest document is missing: {relative!r}')
        fields, content = _parse_front_matter(path)
        section_id = fields['section_id']
        if not SECTION_ID_RE.fullmatch(section_id):
            raise ValueError(f'{relative}: invalid section_id')
        if section_id in section_ids:
            raise ValueError(f'{relative}: duplicate section_id')
        section_ids.add(section_id)
        if fields['audience'] != EXPECTED_AUDIENCE:
            raise ValueError(f'{relative}: audience must be customer')
        if fields['status'] != EXPECTED_STATUS:
            raise ValueError(f'{relative}: status must be verified')
        date.fromisoformat(fields['last_verified'])
        if any(pattern.search(content) for pattern in SENSITIVE_PATTERNS):
            raise ValueError(f'{relative}: content failed sensitive-data scan')
        for url in URL_RE.findall(content):
            if url.rstrip('.,') not in ALLOWED_PUBLIC_URLS:
                raise ValueError(f'{relative}: URL is not allowlisted')

        sections.append(
            {
                'section_id': section_id,
                'source_file': relative,
                'last_verified': fields['last_verified'],
                'content': content,
                'content_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
            }
        )

    return {
        'artifact_version': 1,
        'kb_version': kb_version,
        'language': EXPECTED_LANGUAGE,
        'audience': EXPECTED_AUDIENCE,
        'document_count': len(sections),
        'sections': sections,
        'package_sha256': hashlib.sha256(_canonical_sections(sections)).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()

    artifact = build_artifact(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    print(
        f"OK: AI support KB {artifact['kb_version']}; "
        f"{artifact['document_count']} documents; sha256={artifact['package_sha256']}"
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
