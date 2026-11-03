from __future__ import annotations

import json

import pytest

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.modules.project_knowledge.models import (
    ApprovalRequest,
    DraftCreate,
    ProjectKnowledgeOperation,
    ReconcileRequest,
)
from app.modules.project_knowledge.service import (
    ProjectKnowledgeError,
    approve_draft,
    create_draft,
    get_revision,
    get_snapshot,
    preview_impact,
    reconcile,
    revalidation_status,
)


def _approve(*, key: str, operations: list[ProjectKnowledgeOperation], parent_revision_id: str | None = None) -> str:
    draft = create_draft(
        DraftCreate(
            workspace_id="main",
            parent_kind="reconciled" if parent_revision_id is None else "reconciled",
            parent_revision_id=parent_revision_id,
            operations=operations,
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


def _reconcile(*, revision_id: str, key: str, target_snapshot_id: str | None):
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


def _historical_branch_fixture() -> tuple[str, str, str]:
    initialize_storage(seed_default=True)
    r1_id = _approve(
        key="snapshot-r1-approve",
        operations=[
            ProjectKnowledgeOperation(
                owner_kind="requirement",
                operation_kind="create",
                fields={
                    "statement": "R1 requirement",
                    "status": "active",
                    "basis_kind": "requirement",
                    "reconciliation_gate": "advisory",
                },
            )
        ],
    )
    r1 = _reconcile(revision_id=r1_id, key="snapshot-r1-reconcile", target_snapshot_id=None)
    assert r1.resulting_snapshot_id is not None
    requirement_id = next(iter(r1.canonical_id_map.values()))
    with open_sqlite_connection() as connection:
        r1_token = str(
            connection.execute(
                "SELECT updated_at FROM requirements WHERE id = ? AND workspace_id = 'main'",
                (requirement_id,),
            ).fetchone()["updated_at"]
        )

    r2_id = _approve(
        key="snapshot-r2-approve",
        parent_revision_id=r1_id,
        operations=[
            ProjectKnowledgeOperation(
                owner_kind="requirement",
                operation_kind="update",
                owner_id=requirement_id,
                expected_updated_at=r1_token,
                fields={"statement": "R2 requirement"},
            )
        ],
    )
    r2 = _reconcile(
        revision_id=r2_id,
        key="snapshot-r2-reconcile",
        target_snapshot_id=r1.resulting_snapshot_id,
    )
    assert r2.resulting_snapshot_id is not None

    historical = create_draft(
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
    return r1_id, r1.resulting_snapshot_id, historical.id


def _assert_preview_error(draft_id: str, code: str) -> None:
    with pytest.raises(ProjectKnowledgeError) as exc_info:
        preview_impact("main", draft_id)
    assert exc_info.value.code == code


def test_historical_owner_manifest_corruption_fails_closed_without_r2_fallback() -> None:
    """Malformed R1 owner evidence must never fall back to current R2 owner truth."""
    _r1_id, snapshot_id, draft_id = _historical_branch_fixture()
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT owner_manifest_json FROM project_knowledge_reconciled_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        assert row is not None
        original = str(row["owner_manifest_json"])
        owner_manifest = json.loads(original)
        assert owner_manifest

    malformed_cases: list[str] = ["{", "{}"]
    missing_key = json.loads(original)
    missing_key[0].pop("state_digest")
    malformed_cases.append(json.dumps(missing_key))
    bad_state_digest = json.loads(original)
    bad_state_digest[0]["state_digest"] = "0" * 64
    malformed_cases.append(json.dumps(bad_state_digest))
    duplicate = json.loads(original)
    duplicate.append(dict(duplicate[0]))
    malformed_cases.append(json.dumps(duplicate))

    for corrupted in malformed_cases:
        with open_sqlite_connection() as connection:
            connection.execute(
                "UPDATE project_knowledge_reconciled_snapshots SET owner_manifest_json = ? WHERE id = ?",
                (corrupted, snapshot_id),
            )
            connection.commit()
        _assert_preview_error(draft_id, "snapshot_manifest_invalid")
        with open_sqlite_connection() as connection:
            connection.execute(
                "UPDATE project_knowledge_reconciled_snapshots SET owner_manifest_json = ? WHERE id = ?",
                (original, snapshot_id),
            )
            connection.commit()


def test_historical_graph_manifest_corruption_fails_closed() -> None:
    """Malformed or duplicate graph identities are bounded snapshot errors."""
    _r1_id, snapshot_id, draft_id = _historical_branch_fixture()
    with open_sqlite_connection() as connection:
        original = str(
            connection.execute(
                "SELECT edge_manifest_json FROM project_knowledge_reconciled_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()["edge_manifest_json"]
        )

    valid_edge = {
        "upstream_ref": "requirement:a",
        "downstream_ref": "requirement:b",
        "relation": "depends_on",
        "edge_class": "dependency",
    }
    malformed_cases = [
        "[",
        "{}",
        json.dumps([{"upstream_ref": "requirement:a"}]),
        json.dumps([valid_edge, dict(valid_edge)]),
    ]
    for corrupted in malformed_cases:
        with open_sqlite_connection() as connection:
            connection.execute(
                "UPDATE project_knowledge_reconciled_snapshots SET edge_manifest_json = ? WHERE id = ?",
                (corrupted, snapshot_id),
            )
            connection.commit()
        _assert_preview_error(draft_id, "snapshot_manifest_invalid")
        with open_sqlite_connection() as connection:
            connection.execute(
                "UPDATE project_knowledge_reconciled_snapshots SET edge_manifest_json = ? WHERE id = ?",
                (original, snapshot_id),
            )
            connection.commit()


def test_snapshot_version_digest_and_canonical_map_corruption_have_bounded_errors() -> None:
    """R32 preserves distinct version/digest errors and validates readback maps."""
    _r1_id, snapshot_id, draft_id = _historical_branch_fixture()
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT manifest_version, owner_manifest_digest, canonical_id_map_json FROM project_knowledge_reconciled_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        assert row is not None
        original_version = str(row["manifest_version"])
        original_owner_digest = str(row["owner_manifest_digest"])
        original_id_map = str(row["canonical_id_map_json"])

        connection.execute(
            "UPDATE project_knowledge_reconciled_snapshots SET manifest_version = '999' WHERE id = ?",
            (snapshot_id,),
        )
        connection.commit()
    _assert_preview_error(draft_id, "snapshot_version_unsupported")

    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE project_knowledge_reconciled_snapshots SET manifest_version = ?, owner_manifest_digest = ? WHERE id = ?",
            (original_version, "0" * 64, snapshot_id),
        )
        connection.commit()
    _assert_preview_error(draft_id, "snapshot_digest_mismatch")

    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE project_knowledge_reconciled_snapshots SET owner_manifest_digest = ?, canonical_id_map_json = ? WHERE id = ?",
            (original_owner_digest, "[]", snapshot_id),
        )
        connection.commit()
    with pytest.raises(ProjectKnowledgeError) as exc_info:
        get_snapshot("main", snapshot_id)
    assert exc_info.value.code == "snapshot_manifest_invalid"

    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE project_knowledge_reconciled_snapshots SET canonical_id_map_json = ? WHERE id = ?",
            (original_id_map, snapshot_id),
        )
        connection.commit()


def test_missing_historical_snapshot_fails_closed() -> None:
    """A historical parent whose exact snapshot disappeared is never reconstructed from live state."""
    _r1_id, snapshot_id, draft_id = _historical_branch_fixture()
    with open_sqlite_connection() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "DELETE FROM project_knowledge_reconciled_snapshots WHERE id = ?",
            (snapshot_id,),
        )
        connection.commit()
    _assert_preview_error(draft_id, "snapshot_missing")
