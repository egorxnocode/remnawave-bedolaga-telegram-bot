from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.database.models import (
    AiSupportJob,
    AiSupportRun,
    AiSupportTicketState,
    TicketMessage,
    TicketMessageAuthorKind,
)


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / 'migrations' / 'alembic' / 'versions' / '0101_ai_support_foundation.py'


def _load_migration():
    spec = importlib.util.spec_from_file_location('migration_0101', MIGRATION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_is_linear_after_lava_0100() -> None:
    migration = _load_migration()
    assert migration.revision == '0101'
    assert migration.down_revision == '0100'


def test_durable_job_is_idempotent_per_trigger_message() -> None:
    table = AiSupportJob.__table__
    unique_columns = {
        tuple(item.name for item in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ('trigger_message_id',) in unique_columns
    assert {'status', 'available_at'} == {
        column.name
        for column in next(index for index in table.indexes if index.name == 'ix_ai_support_jobs_claim').columns
    }
    checks = {constraint.name for constraint in table.constraints if isinstance(constraint, CheckConstraint)}
    assert {'ck_ai_support_jobs_status', 'ck_ai_support_jobs_attempt_count'} <= checks


def test_run_metadata_has_no_raw_conversation_storage() -> None:
    columns = set(AiSupportRun.__table__.columns.keys())
    assert not {'raw_message', 'message_text', 'prompt_text', 'response_text', 'provider_payload'} & columns
    assert {
        'prompt_version',
        'kb_version',
        'decision',
        'reason_codes',
        'input_tokens',
        'output_tokens',
        'estimated_cost_microusd',
    } <= columns


def test_ticket_state_is_one_row_per_ticket() -> None:
    table = AiSupportTicketState.__table__
    assert table.c.ticket_id.primary_key is True
    checks = {constraint.name for constraint in table.constraints if isinstance(constraint, CheckConstraint)}
    assert 'ck_ai_support_ticket_states_state' in checks


def test_ai_output_link_is_one_to_one_and_nullable() -> None:
    column = TicketMessage.__table__.c.ai_run_id
    assert column.nullable is True
    unique_columns = {
        tuple(item.name for item in constraint.columns)
        for constraint in TicketMessage.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ('ai_run_id',) in unique_columns


def test_author_kind_values_are_explicit() -> None:
    assert {item.value for item in TicketMessageAuthorKind} == {'user', 'admin', 'ai', 'system'}
    assert TicketMessage.__table__.c.author_kind.nullable is False
    assert TicketMessage.__table__.c.author_kind.server_default is None


def test_every_ticket_message_constructor_sets_author_kind() -> None:
    missing: list[str] = []
    for path in (ROOT / 'app').rglob('*.py'):
        if path == ROOT / 'app' / 'database' / 'models.py':
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'TicketMessage'):
                continue
            if not any(keyword.arg == 'author_kind' for keyword in node.keywords):
                missing.append(f'{path.relative_to(ROOT)}:{node.lineno}')
    assert missing == []


def test_migration_backfills_existing_message_authors() -> None:
    source = MIGRATION_PATH.read_text(encoding='utf-8')
    assert "WHEN is_from_admin THEN 'admin' ELSE 'user'" in source
    assert "author_kind IN ('user','admin','ai','system')" in source
