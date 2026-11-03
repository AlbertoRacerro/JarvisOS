from __future__ import annotations

import pytest

from app.core.bootstrap import initialize_storage
from app.modules.project_knowledge.models import (
    ApprovalRequest,
    DraftCreate,
    ProjectKnowledgeOperation,
    RevisionStateCommand,
)
from app.modules.project_knowledge.revision_lifecycle import change_revision_state
from app.modules.project_knowledge.service import ProjectKnowledgeError, approve_draft, create_draft, preview_impact


def _approve(*, parent_kind: str = "reconciled", parent_revision_id: str | None = None, key: str) -> str:
    draft = create_draft(
        DraftCreate(
            workspace_id="main",
            parent_kind=parent_kind,
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
    approved = approve_draft(
        ApprovalRequest(
            workspace_id="main",
            approval_request_key=key,
            draft_id=draft.id,
            expected_draft_revision_token=draft.revision_token,
            expected_preview_digest=preview.digest,
        )
    )
    assert approved.working_revision_id is not None
    return approved.working_revision_id


def test_discard_is_explicit_terminal_and_stale_safe() -> None:
    initialize_storage(seed_default=True)
    revision_id = _approve(key="discard-source")

    discarded = change_revision_state(
        revision_id,
        RevisionStateCommand(workspace_id="main", action="discard"),
    )
    assert discarded.state == "discarded"
    assert discarded.superseded_by_revision_id is None

    with pytest.raises(ProjectKnowledgeError) as exc_info:
        change_revision_state(
            revision_id,
            RevisionStateCommand(workspace_id="main", action="discard"),
        )
    assert exc_info.value.code == "revision_not_working"


def test_supersede_requires_direct_accepted_successor() -> None:
    initialize_storage(seed_default=True)
    source_id = _approve(key="supersede-source")
    successor_id = _approve(parent_kind="working", parent_revision_id=source_id, key="supersede-successor")

    superseded = change_revision_state(
        source_id,
        RevisionStateCommand(
            workspace_id="main",
            action="supersede",
            superseded_by_revision_id=successor_id,
        ),
    )
    assert superseded.state == "superseded"
    assert superseded.superseded_by_revision_id == successor_id


def test_supersede_rejects_unrelated_sibling() -> None:
    initialize_storage(seed_default=True)
    source_id = _approve(key="sibling-source")
    sibling_id = _approve(key="sibling-other")

    with pytest.raises(ProjectKnowledgeError) as exc_info:
        change_revision_state(
            source_id,
            RevisionStateCommand(
                workspace_id="main",
                action="supersede",
                superseded_by_revision_id=sibling_id,
            ),
        )
    assert exc_info.value.code == "supersede_successor_not_direct"
