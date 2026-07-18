"""Strict runtime loader for the generated customer-safe knowledge artifact."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.config import settings


SECTION_ID_RE = re.compile(r'^[A-Z][A-Z0-9_]{0,63}$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
DEFAULT_ARTIFACT_PATH = (
    Path(__file__).resolve().parents[2] / 'resources' / 'ai_support' / 'customer-support.json'
)
MAX_DOCUMENTS = 32
MAX_SECTION_CHARS = 32_000
MAX_PACKAGE_CHARS = 256_000
EXPECTED_SOURCE_FILES = (
    '00-policy.md',
    'service-and-tariffs.md',
    'apps-and-connection.md',
    'mobile-internet.md',
    'devices-and-traffic.md',
    'payments.md',
    'troubleshooting.md',
    'human-handoff.md',
    'legal.md',
)
EXPECTED_PACKAGE_SHA256 = 'c2e8c31279f85d07c8a70980ee4f4eb4ff03562cedef8db06dfc19c2b6184bc5'


class AiSupportKnowledgeError(RuntimeError):
    """Raised when the runtime KB artifact violates the reviewed contract."""


@dataclass(frozen=True, slots=True)
class AiSupportKnowledgeSection:
    section_id: str
    source_file: str
    last_verified: date
    content: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class AiSupportKnowledgePackage:
    kb_version: str
    language: str
    audience: str
    sections: tuple[AiSupportKnowledgeSection, ...]
    package_sha256: str

    @property
    def section_ids(self) -> frozenset[str]:
        return frozenset(section.section_id for section in self.sections)

    def validate_citations(self, citations: tuple[str, ...]) -> None:
        unknown = set(citations) - self.section_ids
        if unknown:
            raise AiSupportKnowledgeError(f'unknown knowledge citations: {sorted(unknown)}')


def _canonical_sections(raw_sections: list[dict[str, object]]) -> bytes:
    return json.dumps(
        raw_sections,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def load_ai_support_knowledge(
    path: Path = DEFAULT_ARTIFACT_PATH,
    *,
    expected_version: str | None = None,
    expected_sha256: str | None = EXPECTED_PACKAGE_SHA256,
) -> AiSupportKnowledgePackage:
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise AiSupportKnowledgeError('AI support knowledge artifact is unavailable') from error
    if not isinstance(raw, dict):
        raise AiSupportKnowledgeError('knowledge artifact root must be an object')
    if raw.get('artifact_version') != 1:
        raise AiSupportKnowledgeError('unsupported knowledge artifact version')
    if raw.get('language') != 'ru' or raw.get('audience') != 'customer':
        raise AiSupportKnowledgeError('knowledge artifact audience boundary failed')

    kb_version = raw.get('kb_version')
    required_version = expected_version or settings.AI_SUPPORT_KB_VERSION
    if kb_version != required_version:
        raise AiSupportKnowledgeError('knowledge artifact version mismatch')
    raw_sections = raw.get('sections')
    if not isinstance(raw_sections, list) or not 1 <= len(raw_sections) <= MAX_DOCUMENTS:
        raise AiSupportKnowledgeError('knowledge artifact has invalid section count')
    if raw.get('document_count') != len(raw_sections):
        raise AiSupportKnowledgeError('knowledge artifact document count mismatch')
    source_files = tuple(
        section.get('source_file') if isinstance(section, dict) else None for section in raw_sections
    )
    if source_files != EXPECTED_SOURCE_FILES:
        raise AiSupportKnowledgeError('knowledge artifact source allowlist mismatch')

    package_sha256 = raw.get('package_sha256')
    if not isinstance(package_sha256, str) or not SHA256_RE.fullmatch(package_sha256):
        raise AiSupportKnowledgeError('knowledge artifact package digest is invalid')
    actual_package_sha256 = hashlib.sha256(_canonical_sections(raw_sections)).hexdigest()
    if actual_package_sha256 != package_sha256:
        raise AiSupportKnowledgeError('knowledge artifact package digest mismatch')
    if expected_sha256 is not None and package_sha256 != expected_sha256:
        raise AiSupportKnowledgeError('knowledge artifact digest does not match runtime pin')

    sections: list[AiSupportKnowledgeSection] = []
    seen_ids: set[str] = set()
    total_chars = 0
    for raw_section in raw_sections:
        if not isinstance(raw_section, dict):
            raise AiSupportKnowledgeError('knowledge section must be an object')
        section_id = raw_section.get('section_id')
        source_file = raw_section.get('source_file')
        content = raw_section.get('content')
        content_sha256 = raw_section.get('content_sha256')
        last_verified = raw_section.get('last_verified')
        if not isinstance(section_id, str) or not SECTION_ID_RE.fullmatch(section_id):
            raise AiSupportKnowledgeError('knowledge section id is invalid')
        if section_id in seen_ids:
            raise AiSupportKnowledgeError('knowledge section id is duplicated')
        seen_ids.add(section_id)
        if (
            not isinstance(source_file, str)
            or Path(source_file).name != source_file
            or Path(source_file).suffix != '.md'
        ):
            raise AiSupportKnowledgeError('knowledge source filename is unsafe')
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_SECTION_CHARS:
            raise AiSupportKnowledgeError('knowledge section content is invalid')
        total_chars += len(content)
        if total_chars > MAX_PACKAGE_CHARS:
            raise AiSupportKnowledgeError('knowledge package is too large')
        if not isinstance(content_sha256, str) or not SHA256_RE.fullmatch(content_sha256):
            raise AiSupportKnowledgeError('knowledge section digest is invalid')
        if hashlib.sha256(content.encode('utf-8')).hexdigest() != content_sha256:
            raise AiSupportKnowledgeError('knowledge section digest mismatch')
        if not isinstance(last_verified, str):
            raise AiSupportKnowledgeError('knowledge verification date is invalid')
        try:
            verified_date = date.fromisoformat(last_verified)
        except ValueError as error:
            raise AiSupportKnowledgeError('knowledge verification date is invalid') from error

        sections.append(
            AiSupportKnowledgeSection(
                section_id=section_id,
                source_file=source_file,
                last_verified=verified_date,
                content=content,
                content_sha256=content_sha256,
            )
        )

    return AiSupportKnowledgePackage(
        kb_version=kb_version,
        language='ru',
        audience='customer',
        sections=tuple(sections),
        package_sha256=package_sha256,
    )
