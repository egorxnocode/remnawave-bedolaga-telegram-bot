import importlib.util
from pathlib import Path

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.database.models import AiSupportDraft, AiSupportDraftStatus


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / 'migrations' / 'alembic' / 'versions' / '0102_ai_support_shadow_drafts.py'


def _load_migration():
    spec = importlib.util.spec_from_file_location('migration_0102', MIGRATION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shadow_draft_migration_is_linear() -> None:
    migration = _load_migration()
    assert migration.revision == '0102'
    assert migration.down_revision == '0101'


def test_one_draft_per_run_and_explicit_statuses() -> None:
    table = AiSupportDraft.__table__
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ('run_id',) in unique_columns
    checks = {constraint.name for constraint in table.constraints if isinstance(constraint, CheckConstraint)}
    assert 'ck_ai_support_drafts_status' in checks
    assert {item.value for item in AiSupportDraftStatus} == {'pending', 'accepted', 'rejected', 'superseded'}


def test_draft_is_not_a_customer_message() -> None:
    columns = set(AiSupportDraft.__table__.columns.keys())
    assert {'answer_text', 'reviewed_text', 'status', 'citations'} <= columns
    assert 'is_from_admin' not in columns
    assert 'author_kind' not in columns
