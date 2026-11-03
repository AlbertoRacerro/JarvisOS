from __future__ import annotations

import hashlib
import threading

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.main import create_app
from app.modules.project_knowledge import service as project_knowledge_service
from app.modules.project_knowledge.models import (
    ApprovalRequest,
    DraftCreate,
    ProjectKnowledgeOperation,
    ReconcileRequest,
    RevisionStateCommand,
    ScalarAdmissionRequest,
    ValidationRequest,
)
from app.modules.project_knowledge.service import ProjectKnowledgeError, approve_draft, create_draft, preview_impact


def _initialize() -> None:
    initialize_storage(seed_default=True)


def _requirement_create() -> ProjectKnowledgeOperation:
    return ProjectKnowledgeOperation(
        owner_kind="requirement",
        operation_kind="create",
        fields={
            "statement": "Maximum pressure must be bounded",
            "status": "active",
            "basis_kind": "requirement",
            "reconciliation_gate": "advisory",
        },
    )


def _approval_payload(*, key: str) -> ApprovalRequest:
    draft = create_draft(DraftCreate(workspace_id="main", operations=[_requirement_create()]))
    preview = preview_impact("main", draft.id)
    return ApprovalRequest(
        workspace_id="main",
        approval_request_key=key,
        draft_id=draft.id,
        expected_draft_revision_token=draft.revision_token,
        expected_preview_digest=preview.digest,
    )


def test_same_key_approval_replay_does_not_recompute_mutable_impact(monkeypatch: pytest.MonkeyPatch) -> None:
    _initialize()
    payload = _approval_payload(key="approve-replay-no-recompute")
    first = approve_draft(payload)

    def fail_if_recomputed(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("same-key terminal replay must not recompute impact")

    monkeypatch.setattr(project_knowledge_service, "_impact_from_connection", fail_if_recomputed)
    assert approve_draft(payload) == first


def test_different_key_cannot_create_second_working_revision_for_approved_draft() -> None:
    _initialize()
    payload = _approval_payload(key="approve-first")
    first = approve_draft(payload)
    assert first.working_revision_id is not None

    with pytest.raises(ProjectKnowledgeError) as exc_info:
        approve_draft(payload.model_copy(update={"approval_request_key": "approve-second"}))
    assert exc_info.value.code == "draft_already_approved"

    with open_sqlite_connection() as connection:
        revision_count = connection.execute(
            "SELECT COUNT(*) AS count FROM project_knowledge_revisions WHERE workspace_id = ?",
            (payload.workspace_id,),
        ).fetchone()["count"]
    assert revision_count == 1


def test_concurrent_different_keys_serialize_to_one_working_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    _initialize()
    payload = _approval_payload(key="approve-concurrent-a")
    payload_b = payload.model_copy(update={"approval_request_key": "approve-concurrent-b"})
    original_impact = project_knowledge_service._impact_from_connection
    impact_entered = threading.Event()
    release_first = threading.Event()
    start = threading.Barrier(3)
    impact_lock = threading.Lock()
    first_impact = True
    successes: list[str] = []
    errors: list[str] = []

    def hold_first_impact(*args: object, **kwargs: object) -> object:
        nonlocal first_impact
        with impact_lock:
            should_hold = first_impact
            first_impact = False
        if should_hold:
            impact_entered.set()
            assert release_first.wait(timeout=5), "timed out while holding the first approval transaction"
        return original_impact(*args, **kwargs)

    def approve(candidate: ApprovalRequest) -> None:
        start.wait(timeout=5)
        try:
            result = approve_draft(candidate)
            assert result.working_revision_id is not None
            successes.append(result.working_revision_id)
        except ProjectKnowledgeError as exc:
            errors.append(exc.code)

    monkeypatch.setattr(project_knowledge_service, "_impact_from_connection", hold_first_impact)
    first = threading.Thread(target=approve, args=(payload,))
    second = threading.Thread(target=approve, args=(payload_b,))
    first.start()
    second.start()
    start.wait(timeout=5)
    assert impact_entered.wait(timeout=5), "neither approval reached impact computation"
    assert first.is_alive() and second.is_alive(), "both contenders must overlap while the write transaction is held"
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()

    assert len(successes) == 1
    assert errors == ["draft_already_approved"]
    with open_sqlite_connection() as connection:
        revision_count = connection.execute(
            "SELECT COUNT(*) AS count FROM project_knowledge_revisions WHERE workspace_id = ?",
            (payload.workspace_id,),
        ).fetchone()["count"]
    assert revision_count == 1


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), {"nested": [1, float("nan")]}, {"nested": {"v": float("inf")}}],
)
def test_canonical_json_rejects_non_finite_values_with_typed_error(value: object) -> None:
    with pytest.raises(ProjectKnowledgeError) as exc_info:
        project_knowledge_service._canonical_json(value)
    assert exc_info.value.code == "canonical_json_non_finite"


def test_finite_canonical_json_and_digest_bytes_remain_stable() -> None:
    value = {"b": [1, 2.5], "a": "Ω"}
    expected = '{"a":"\\u03a9","b":[1,2.5]}'
    assert project_knowledge_service._canonical_json(value) == expected
    assert project_knowledge_service._digest(value) == hashlib.sha256(expected.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_draft_route_returns_typed_client_error_for_non_finite_operation_value(token: str) -> None:
    _initialize()
    client = TestClient(create_app())
    response = client.post(
        "/project-knowledge/drafts",
        content=(
            '{"workspace_id":"main","operations":[{"owner_kind":"requirement",'
            '"operation_kind":"create","fields":{"statement":"bounded","notes":'
            + token
            + "}}]}"
        ),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "canonical_json_non_finite"


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            ApprovalRequest,
            {
                "workspace_id": "main",
                "approval_request_key": "k",
                "draft_id": "d",
                "expected_draft_revision_token": "t",
                "expected_preview_digest": "p",
            },
        ),
        (
            ScalarAdmissionRequest,
            {
                "workspace_id": "main",
                "run_id": "r",
                "output_name": "o",
                "value": "1",
                "unit": "bar",
                "source_payload_digest": "0" * 64,
                "extractor_id": "e",
                "extractor_version": "1",
            },
        ),
        (
            ValidationRequest,
            {
                "workspace_id": "main",
                "working_revision_id": "r",
                "requirement_id": "q",
                "expected_requirement_updated_at": "t",
                "validated_basis_digest": "b",
                "applicability_set_digest": "a",
            },
        ),
        (
            ReconcileRequest,
            {
                "workspace_id": "main",
                "idempotency_key": "k",
                "working_revision_id": "r",
                "expected_target_digest": "d",
                "expected_selected_validation_set_digest": "v",
            },
        ),
        (
            RevisionStateCommand,
            {"workspace_id": "main", "action": "discard"},
        ),
    ],
)
def test_api_bound_request_models_reject_unknown_fields(model: type, payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "unexpected": "rejected"})


def test_approval_route_rejects_unknown_field_with_422() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/project-knowledge/approvals",
        json={
            "workspace_id": "main",
            "approval_request_key": "http-extra-field",
            "draft_id": "draft-does-not-need-to-exist",
            "expected_draft_revision_token": "token",
            "expected_preview_digest": "digest",
            "unexpected": "rejected",
        },
    )
    assert response.status_code == 422
