from __future__ import annotations

import sqlite3

import pytest

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.modules.project_knowledge.models import (
    ApprovalRequest,
    DraftCreate,
    DraftUpdate,
    ProjectKnowledgeOperation,
    ReconcileRequest,
)
from app.modules.project_knowledge.service import (
    ProjectKnowledgeError,
    approve_draft,
    create_draft,
    get_snapshot,
    preview_impact,
    reconcile,
    revalidation_status,
    update_draft,
)


def _initialize() -> None:
    initialize_storage(seed_default=True)


def _requirement_create(statement: str = "Maximum pressure must be bounded") -> ProjectKnowledgeOperation:
    return ProjectKnowledgeOperation(
        owner_kind="requirement",
        operation_kind="create",
        fields={
            "statement": statement,
            "status": "active",
            "basis_kind": "requirement",
            "reconciliation_gate": "advisory",
        },
    )


def _approved_requirement_revision(
    *,
    approval_key: str = "approve-1",
    statement: str = "Maximum pressure must be bounded",
) -> tuple[str, str]:
    draft = create_draft(DraftCreate(workspace_id="main", operations=[_requirement_create(statement)]))
    preview = preview_impact("main", draft.id)
    approval = approve_draft(
        ApprovalRequest(
            workspace_id="main",
            approval_request_key=approval_key,
            draft_id=draft.id,
            expected_draft_revision_token=draft.revision_token,
            expected_preview_digest=preview.digest,
        )
    )
    assert approval.state == "success"
    assert approval.working_revision_id is not None
    return approval.working_revision_id, preview.digest


def test_112_schema_is_additive_and_bootstrap_idempotent() -> None:
    _initialize()
    initialize_storage(seed_default=True)

    with open_sqlite_connection() as connection:
        migration = connection.execute(
            "SELECT status FROM schema_migrations WHERE migration_id = '0017_project_knowledge_core'"
        ).fetchone()
        assert migration is not None
        assert migration["status"] == "applied"

        requirement_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(requirements)").fetchall()
        }
        assert {
            "basis_kind",
            "reconciliation_gate",
            "criterion_output_name",
            "criterion_operator",
            "criterion_expected_value",
            "criterion_expected_unit",
            "criterion_rule_version",
        }.issubset(requirement_columns)

        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        assert {
            "project_knowledge_drafts",
            "project_knowledge_revisions",
            "project_knowledge_approval_requests",
            "requirement_applicability",
            "simulation_run_scalar_results",
            "project_knowledge_validation",
            "project_knowledge_reconciled_snapshots",
            "project_knowledge_reconciliation_requests",
        }.issubset(tables)


def test_draft_cas_and_approval_are_noncanonical_until_reconciliation() -> None:
    _initialize()
    draft = create_draft(DraftCreate(workspace_id="main", operations=[_requirement_create()]))

    with pytest.raises(ProjectKnowledgeError, match="Draft changed"):
        update_draft(
            draft.id,
            DraftUpdate(
                workspace_id="main",
                expected_revision_token="stale-token",
                operations=[_requirement_create("Changed")],
            ),
        )

    preview = preview_impact("main", draft.id)
    approval = approve_draft(
        ApprovalRequest(
            workspace_id="main",
            approval_request_key="approve-noncanonical",
            draft_id=draft.id,
            expected_draft_revision_token=draft.revision_token,
            expected_preview_digest=preview.digest,
        )
    )
    assert approval.state == "success"

    with open_sqlite_connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM requirements WHERE workspace_id = 'main'"
        ).fetchone()["count"] == 0


def test_approval_retry_is_idempotent_and_rebinding_conflicts() -> None:
    _initialize()
    draft = create_draft(DraftCreate(workspace_id="main", operations=[_requirement_create()]))
    preview = preview_impact("main", draft.id)
    payload = ApprovalRequest(
        workspace_id="main",
        approval_request_key="approve-retry",
        draft_id=draft.id,
        expected_draft_revision_token=draft.revision_token,
        expected_preview_digest=preview.digest,
    )
    first = approve_draft(payload)
    retry = approve_draft(payload)
    assert retry == first

    other = create_draft(DraftCreate(workspace_id="main", operations=[_requirement_create("Other")]))
    other_preview = preview_impact("main", other.id)
    with pytest.raises(ProjectKnowledgeError) as exc_info:
        approve_draft(
            ApprovalRequest(
                workspace_id="main",
                approval_request_key="approve-retry",
                draft_id=other.id,
                expected_draft_revision_token=other.revision_token,
                expected_preview_digest=other_preview.digest,
            )
        )
    assert exc_info.value.code == "approval_idempotency_conflict"


def test_failed_approval_retry_reuses_bound_terminal_failure() -> None:
    _initialize()
    draft = create_draft(DraftCreate(workspace_id="main", operations=[_requirement_create()]))
    payload = ApprovalRequest(
        workspace_id="main",
        approval_request_key="approve-failed-retry",
        draft_id=draft.id,
        expected_draft_revision_token=draft.revision_token,
        expected_preview_digest="0" * 64,
    )

    with pytest.raises(ProjectKnowledgeError) as exc_info:
        approve_draft(payload)
    assert exc_info.value.code == "preview_stale"

    retry = approve_draft(payload)
    assert retry.state == "failed"
    assert retry.failure_code == "preview_stale"
    assert retry.working_revision_id is None


def test_reconciliation_commits_requirement_snapshot_and_exact_retry_once() -> None:
    _initialize()
    revision_id, target_digest = _approved_requirement_revision()
    validation = revalidation_status("main", revision_id)
    assert validation.complete is True

    payload = ReconcileRequest(
        workspace_id="main",
        idempotency_key="reconcile-1",
        working_revision_id=revision_id,
        expected_target_snapshot_id=None,
        expected_target_digest=target_digest,
        expected_selected_validation_set_digest=validation.selected_validation_set_digest,
    )
    first = reconcile(payload)
    retry = reconcile(payload)
    assert retry == first
    assert first.state == "success"
    assert first.resulting_snapshot_id is not None
    assert len(first.canonical_id_map) == 1

    with open_sqlite_connection() as connection:
        requirements = connection.execute(
            "SELECT id, statement, status FROM requirements WHERE workspace_id = 'main'"
        ).fetchall()
        assert len(requirements) == 1
        assert requirements[0]["status"] == "active"
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM project_knowledge_reconciliation_requests WHERE workspace_id = 'main'"
        ).fetchone()["count"] == 1

    snapshot = get_snapshot("main", first.resulting_snapshot_id)
    assert snapshot.canonical_id_map == first.canonical_id_map
    assert snapshot.owner_manifest_digest
    assert snapshot.graph_digest


def test_reconciliation_same_key_changed_binding_conflicts_without_replay() -> None:
    _initialize()
    revision_id, target_digest = _approved_requirement_revision()
    validation = revalidation_status("main", revision_id)
    first = reconcile(
        ReconcileRequest(
            workspace_id="main",
            idempotency_key="reconcile-conflict",
            working_revision_id=revision_id,
            expected_target_snapshot_id=None,
            expected_target_digest=target_digest,
            expected_selected_validation_set_digest=validation.selected_validation_set_digest,
        )
    )
    assert first.state == "success"

    with pytest.raises(ProjectKnowledgeError) as exc_info:
        reconcile(
            ReconcileRequest(
                workspace_id="main",
                idempotency_key="reconcile-conflict",
                working_revision_id=revision_id,
                expected_target_snapshot_id=None,
                expected_target_digest="0" * 64,
                expected_selected_validation_set_digest=validation.selected_validation_set_digest,
            )
        )
    assert exc_info.value.code == "reconciliation_idempotency_conflict"

    with open_sqlite_connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM requirements WHERE workspace_id = 'main'"
        ).fetchone()["count"] == 1


def test_second_root_reconciliation_cannot_overwrite_newer_reconciled_target() -> None:
    _initialize()
    revision_a, digest_a = _approved_requirement_revision(
        approval_key="approve-root-a",
        statement="Root A",
    )
    revision_b, digest_b = _approved_requirement_revision(
        approval_key="approve-root-b",
        statement="Root B",
    )
    validation_a = revalidation_status("main", revision_a)
    validation_b = revalidation_status("main", revision_b)

    first = reconcile(
        ReconcileRequest(
            workspace_id="main",
            idempotency_key="reconcile-root-a",
            working_revision_id=revision_a,
            expected_target_snapshot_id=None,
            expected_target_digest=digest_a,
            expected_selected_validation_set_digest=validation_a.selected_validation_set_digest,
        )
    )
    assert first.state == "success"
    assert first.resulting_snapshot_id is not None

    stale_payload = ReconcileRequest(
        workspace_id="main",
        idempotency_key="reconcile-root-b",
        working_revision_id=revision_b,
        expected_target_snapshot_id=None,
        expected_target_digest=digest_b,
        expected_selected_validation_set_digest=validation_b.selected_validation_set_digest,
    )
    with pytest.raises(ProjectKnowledgeError) as exc_info:
        reconcile(stale_payload)
    assert exc_info.value.code == "target_snapshot_stale"

    retry = reconcile(stale_payload)
    assert retry.state == "failed"
    assert retry.failure_code == "target_snapshot_stale"

    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT statement FROM requirements WHERE workspace_id = 'main' ORDER BY statement"
        ).fetchall()
        assert [row["statement"] for row in rows] == ["Root A"]


def test_applicability_uniqueness_is_enforced_by_schema() -> None:
    _initialize()
    with open_sqlite_connection() as connection:
        now = "2026-08-29T00:00:00+00:00"
        connection.execute(
            """
            INSERT INTO requirements (
                id, workspace_id, statement, status, schema_version, created_at, updated_at,
                basis_kind, reconciliation_gate
            ) VALUES ('req', 'main', 'criterion', 'active', 1, ?, ?, 'acceptance_criterion', 'required')
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO requirement_applicability (
                id, workspace_id, requirement_id, target_kind, target_id, effect,
                lifecycle_state, created_at, updated_at
            ) VALUES ('a1', 'main', 'req', 'workspace', 'main', 'include', 'active', ?, ?)
            """,
            (now, now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO requirement_applicability (
                    id, workspace_id, requirement_id, target_kind, target_id, effect,
                    lifecycle_state, created_at, updated_at
                ) VALUES ('a2', 'main', 'req', 'workspace', 'main', 'exclude', 'active', ?, ?)
                """,
                (now, now),
            )
