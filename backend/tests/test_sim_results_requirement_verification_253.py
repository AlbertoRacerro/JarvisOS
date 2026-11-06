from __future__ import annotations

from app.core.database import open_sqlite_connection
from app.modules.project_knowledge.service import (
    _applicable_required_requirements,
    revalidation_status,
)
from tests.test_project_knowledge_core_112 import _approved_requirement_revision, _initialize

_NOW = "2026-08-29T00:00:00+00:00"


def _add_required_requirement(requirement_id: str) -> None:
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO requirements (
                id, workspace_id, statement, status, schema_version, created_at, updated_at,
                basis_kind, reconciliation_gate
            ) VALUES (?, 'main', 'criterion', 'active', 1, ?, ?, 'acceptance_criterion', 'required')
            """,
            (requirement_id, _NOW, _NOW),
        )
        connection.execute(
            """
            INSERT INTO requirement_applicability (
                id, workspace_id, requirement_id, target_kind, target_id, effect,
                lifecycle_state, created_at, updated_at
            ) VALUES (?, 'main', ?, 'workspace', 'main', 'include', 'active', ?, ?)
            """,
            (f"app-{requirement_id}", requirement_id, _NOW, _NOW),
        )
        connection.commit()


def test_253_revalidation_returns_computed_applicability_digest() -> None:
    _initialize()
    revision_id, _ = _approved_requirement_revision()
    with open_sqlite_connection() as connection:
        _required, digest, _diagnostics = _applicable_required_requirements(connection, "main", revision_id)
    read = revalidation_status("main", revision_id)
    assert read.applicability_set_digest == digest
    assert read.applicability_set_digest != read.selected_validation_set_digest


def test_253_applicability_digest_changes_with_applicable_set() -> None:
    _initialize()
    revision_id, _ = _approved_requirement_revision()
    before = revalidation_status("main", revision_id)
    _add_required_requirement("req-253")
    after = revalidation_status("main", revision_id)
    assert "req-253" in after.mandatory_requirement_ids
    assert after.applicability_set_digest != before.applicability_set_digest
    assert after.applicability_set_digest != after.selected_validation_set_digest
