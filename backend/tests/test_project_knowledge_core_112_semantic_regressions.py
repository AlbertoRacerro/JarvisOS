from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.modules.modeling.models import SimulationRunCreate
from app.modules.modeling.service import create_simulation_run
from app.modules.project_knowledge.models import (
    ApprovalRequest,
    DraftCreate,
    ProjectKnowledgeOperation,
    ReconcileRequest,
    RevisionStateCommand,
    ScalarAdmissionRequest,
)
from app.modules.project_knowledge.revision_lifecycle import change_revision_state
from app.modules.project_knowledge.service import (
    ProjectKnowledgeError,
    admit_scalar_result,
    approve_draft,
    create_draft,
    get_revision,
    preview_impact,
    reconcile,
    revalidation_status,
)


def _approve(*, key: str, parent_revision_id: str | None = None) -> str:
    draft = create_draft(
        DraftCreate(
            workspace_id="main",
            parent_kind="working" if parent_revision_id else "reconciled",
            parent_revision_id=parent_revision_id,
            operations=[
                ProjectKnowledgeOperation(
                    owner_kind="requirement",
                    operation_kind="create",
                    fields={
                        "statement": f"criterion {key}",
                        "status": "active",
                        "basis_kind": "requirement",
                        "reconciliation_gate": "advisory",
                    },
                )
            ],
        )
    )
    preview = preview_impact("main", draft.id)
    approval = approve_draft(
        ApprovalRequest(
            workspace_id="main",
            approval_request_key=key,
            draft_id=draft.id,
            expected_draft_revision_token=draft.revision_token,
            expected_preview_digest=preview.digest,
        )
    )
    assert approval.working_revision_id is not None
    return approval.working_revision_id


def _reconcile_working(*, revision_id: str, key: str, target_snapshot_id: str | None):
    revision = get_revision("main", revision_id)
    validation = revalidation_status("main", revision_id)
    return reconcile(
        ReconcileRequest(
            workspace_id="main",
            idempotency_key=key,
            working_revision_id=revision_id,
            expected_target_snapshot_id=target_snapshot_id,
            expected_target_digest=revision.projected_state_digest,
            expected_selected_validation_set_digest=validation.selected_validation_set_digest,
        )
    )


def test_scalar_admission_rejects_client_authored_value_without_owner_proof() -> None:
    """A valid payload digest must not authorize a caller-invented scalar value."""
    initialize_storage(seed_default=True)
    output_payload = '{"pressure_bar":10.0}'
    run = create_simulation_run(
        "main",
        SimulationRunCreate(status="completed", output_payload=output_payload),
    )
    source_digest = hashlib.sha256(output_payload.encode("utf-8")).hexdigest()

    with pytest.raises(ProjectKnowledgeError):
        admit_scalar_result(
            ScalarAdmissionRequest(
                workspace_id="main",
                run_id=run.id,
                output_name="pressure_bar",
                value="999",
                unit="bar",
                source_payload_digest=source_digest,
                extractor_id="caller-claimed-extractor",
                extractor_version="1",
            )
        )


def test_superseded_parent_does_not_strand_exact_direct_successor() -> None:
    """The accepted direct successor must remain traversable after superseding its parent."""
    initialize_storage(seed_default=True)
    source_id = _approve(key="semantic-supersede-source")
    successor_id = _approve(key="semantic-supersede-successor", parent_revision_id=source_id)

    change_revision_state(
        source_id,
        RevisionStateCommand(
            workspace_id="main",
            action="supersede",
            superseded_by_revision_id=successor_id,
        ),
    )

    continuation = create_draft(
        DraftCreate(
            workspace_id="main",
            parent_kind="working",
            parent_revision_id=successor_id,
            operations=[],
        )
    )
    preview = preview_impact("main", continuation.id)
    assert successor_id in preview.ancestor_revision_ids
    assert source_id in preview.ancestor_revision_ids


def test_historical_reconciled_branch_uses_snapshot_owner_token_not_current_owner() -> None:
    """Branching R1 after R2 exists must remain bound to R1's immutable owner token."""
    initialize_storage(seed_default=True)
    r1_id = _approve(key="historical-r1-create")
    r1_result = _reconcile_working(revision_id=r1_id, key="historical-r1-reconcile", target_snapshot_id=None)
    assert r1_result.resulting_snapshot_id is not None
    assert len(r1_result.canonical_id_map) == 1
    requirement_id = next(iter(r1_result.canonical_id_map.values()))

    with open_sqlite_connection() as connection:
        r1_token = str(
            connection.execute(
                "SELECT updated_at FROM requirements WHERE id = ? AND workspace_id = 'main'",
                (requirement_id,),
            ).fetchone()["updated_at"]
        )

    r2_draft = create_draft(
        DraftCreate(
            workspace_id="main",
            parent_kind="reconciled",
            parent_revision_id=r1_id,
            operations=[
                ProjectKnowledgeOperation(
                    owner_kind="requirement",
                    operation_kind="update",
                    owner_id=requirement_id,
                    expected_updated_at=r1_token,
                    fields={"statement": "criterion after R2"},
                )
            ],
        )
    )
    r2_preview = preview_impact("main", r2_draft.id)
    r2_approval = approve_draft(
        ApprovalRequest(
            workspace_id="main",
            approval_request_key="historical-r2-approve",
            draft_id=r2_draft.id,
            expected_draft_revision_token=r2_draft.revision_token,
            expected_preview_digest=r2_preview.digest,
        )
    )
    assert r2_approval.working_revision_id is not None
    r2_result = _reconcile_working(
        revision_id=r2_approval.working_revision_id,
        key="historical-r2-reconcile",
        target_snapshot_id=r1_result.resulting_snapshot_id,
    )
    assert r2_result.resulting_snapshot_id is not None

    historical_draft = create_draft(
        DraftCreate(
            workspace_id="main",
            parent_kind="reconciled",
            parent_revision_id=r1_id,
            operations=[
                ProjectKnowledgeOperation(
                    owner_kind="requirement",
                    operation_kind="update",
                    owner_id=requirement_id,
                    expected_updated_at=r1_token,
                    fields={"statement": "branch from exact R1"},
                )
            ],
        )
    )
    historical_preview = preview_impact("main", historical_draft.id)
    assert historical_preview.owner_tokens[f"requirement:{requirement_id}"] == r1_token


def test_dependency_add_rejects_unresolvable_projected_endpoints() -> None:
    """A proposed dependency cannot make a complete preview out of dangling refs."""
    initialize_storage(seed_default=True)
    draft = create_draft(
        DraftCreate(
            workspace_id="main",
            operations=[
                ProjectKnowledgeOperation(
                    owner_kind="requirement",
                    operation_kind="create",
                    fields={
                        "statement": "dependency guard",
                        "status": "active",
                        "basis_kind": "requirement",
                        "reconciliation_gate": "advisory",
                    },
                    dependency_add=[
                        ("requirement:missing-upstream", "model_spec:missing-downstream", "depends_on")
                    ],
                )
            ],
        )
    )

    with pytest.raises(ProjectKnowledgeError):
        preview_impact("main", draft.id)


def test_concurrent_reconciliation_first_use_returns_one_persisted_request_identity() -> None:
    """R27: two simultaneous first uses of one workspace/key converge on one committed identity."""
    initialize_storage(seed_default=True)
    revision_id = _approve(key="semantic-concurrent-reconcile-source")
    revision = get_revision("main", revision_id)
    validation = revalidation_status("main", revision_id)
    payload = ReconcileRequest(
        workspace_id="main",
        idempotency_key="semantic-concurrent-reconcile",
        working_revision_id=revision_id,
        expected_target_snapshot_id=None,
        expected_target_digest=revision.projected_state_digest,
        expected_selected_validation_set_digest=validation.selected_validation_set_digest,
    )
    start = Barrier(2)

    def attempt():
        start.wait(timeout=5)
        return reconcile(payload)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]

    first, second = results
    assert first.state == "success"
    assert second.state == "success"
    assert second == first
    assert first.resulting_snapshot_id is not None

    with open_sqlite_connection() as connection:
        request_rows = connection.execute(
            "SELECT id, state, resulting_snapshot_id FROM project_knowledge_reconciliation_requests "
            "WHERE workspace_id = ? AND idempotency_key = ?",
            ("main", payload.idempotency_key),
        ).fetchall()
        assert len(request_rows) == 1
        assert request_rows[0]["id"] == first.request_id
        assert request_rows[0]["state"] == "success"
        assert request_rows[0]["resulting_snapshot_id"] == first.resulting_snapshot_id
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM project_knowledge_reconciled_snapshots "
            "WHERE workspace_id = ? AND id = ?",
            ("main", first.resulting_snapshot_id),
        ).fetchone()["count"] == 1
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM requirements WHERE workspace_id = 'main'"
        ).fetchone()["count"] == 1
