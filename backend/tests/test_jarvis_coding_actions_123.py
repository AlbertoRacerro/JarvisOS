from __future__ import annotations

import json

import pytest

from app.modules.ai.contracts import AIResponse, AIUsage, RoutingDecision
from app.modules.ai.execution import AiTaskOutcome
from app.modules.ai.jarvis_context import JarvisCapabilityRegistry, JarvisContextError
from app.modules.ai.jarvis_context_models import JarvisCapabilityDescriptor
from app.modules.coding.actions import (
    CodingActionsService,
    CodingInspectRequest,
    CodingSuggestModificationRequest,
)
from app.modules.coding.repository_truth import RepositoryTruthResult

REPOSITORY = "AlbertoRacerro/JarvisOS"
HEAD = "a" * 40
MOVED = "b" * 40


def result(operation: str, *, sha: str = HEAD, payload: dict[str, object] | None = None) -> RepositoryTruthResult:
    return RepositoryTruthResult(
        provider="github",
        repository=REPOSITORY,
        operation=operation,
        requested_ref="master",
        resolved_sha=sha,
        partial=False,
        payload=payload or {},
        observed_at="2026-09-04T00:00:00+00:00",
    )


class FakeTruth:
    def __init__(self, *, move_after_generation: bool = False) -> None:
        self.ref_reads = 0
        self.file_reads: list[str] = []
        self.move_after_generation = move_after_generation

    def repository_ref_truth(self, repository: str, ref: str) -> RepositoryTruthResult:
        assert repository == REPOSITORY and ref == "master"
        self.ref_reads += 1
        sha = MOVED if self.move_after_generation and self.ref_reads > 1 else HEAD
        return result("repository_ref_truth", sha=sha)

    def file_preview(self, repository: str, ref: str, path: str) -> RepositoryTruthResult:
        assert repository == REPOSITORY and ref == HEAD
        self.file_reads.append(path)
        return result("file_preview", payload={"text": f"content:{path}"})


def inspect_request(**updates: object) -> CodingInspectRequest:
    data: dict[str, object] = {
        "workspace_id": "ws-1",
        "repository": REPOSITORY,
        "base_ref": "master",
        "base_sha": HEAD,
        "target_paths": ["backend/app/example.py"],
    }
    data.update(updates)
    return CodingInspectRequest.model_validate(data)


def suggest_request(**updates: object) -> CodingSuggestModificationRequest:
    data = inspect_request().model_dump()
    data.update({"intent": "Make the example explicit", "expected_checks": ["pytest -q"]})
    data.update(updates)
    return CodingSuggestModificationRequest.model_validate(data)


def ai_outcome(payload: dict[str, object]) -> AiTaskOutcome:
    response = AIResponse(
        provider_id="fake",
        model_id="fake-model",
        usage=AIUsage(provider_id="fake", model_id="fake-model"),
        request_id="req-1",
        text=json.dumps(payload),
    )
    return AiTaskOutcome(
        "success",
        "job-1",
        "local:fake",
        RoutingDecision(provider_id="fake", model_id="fake-model", decision_reason="test"),
        response=response,
    )


def good_payload() -> dict[str, object]:
    return {
        "summary": "Make behavior explicit",
        "changes": [{"path": "backend/app/example.py", "plan": "Add an explicit bounded branch."}],
        "assumptions": [],
        "warnings": [],
        "expected_checks": ["pytest -q"],
    }


def test_inspect_is_deterministic_and_never_dispatches_ai() -> None:
    truth = FakeTruth()

    def forbidden_ai(**_: object) -> AiTaskOutcome:
        raise AssertionError("deterministic inspect must not dispatch AI")

    response = CodingActionsService(truth, ai_runner=forbidden_ai).inspect(inspect_request())  # type: ignore[arg-type]
    assert response["state"] == "current"
    assert response["base_sha"] == HEAD
    assert truth.file_reads == ["backend/app/example.py"]


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../secret.txt",
        "backend\\escape.py",
        "C:/secret.txt",
        ".github/workflows/ci.yml",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/PROCESS.md",
        ".env",
        "assets/image.png",
    ],
)
def test_unsupported_targets_fail_before_provider_dispatch(path: str) -> None:
    calls = 0

    def forbidden_ai(**_: object) -> AiTaskOutcome:
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not run")

    service = CodingActionsService(FakeTruth(), ai_runner=forbidden_ai)  # type: ignore[arg-type]
    response = service.suggest_modification(suggest_request(target_paths=[path]))
    assert response == {"state": "refused", "reason": "unsupported_target"}
    assert calls == 0


def test_stale_base_fails_before_provider_dispatch() -> None:
    truth = FakeTruth()

    def forbidden_ai(**_: object) -> AiTaskOutcome:
        raise AssertionError("provider must not run")

    service = CodingActionsService(truth, ai_runner=forbidden_ai)  # type: ignore[arg-type]
    response = service.suggest_modification(suggest_request(base_sha=MOVED))
    assert response == {"state": "refused", "reason": "stale_target"}


def test_valid_semantic_proposal_is_exact_base_and_proposal_only() -> None:
    calls = 0

    def fake_ai(**kwargs: object) -> AiTaskOutcome:
        nonlocal calls
        calls += 1
        assert kwargs["task_kind"] == "synthesis"
        assert kwargs["route_class"] == "local:coder"
        return ai_outcome(good_payload())

    response = CodingActionsService(FakeTruth(), ai_runner=fake_ai).suggest_modification(  # type: ignore[arg-type]
        suggest_request()
    )
    assert response["state"] == "proposed"
    assert response["base_sha"] == HEAD
    assert response["target_paths"] == ["backend/app/example.py"]
    assert response["generated_by"]["ai_job_id"] == "job-1"  # type: ignore[index]
    assert calls == 1


def test_ref_move_after_generation_invalidates_proposal_without_retry() -> None:
    calls = 0

    def fake_ai(**_: object) -> AiTaskOutcome:
        nonlocal calls
        calls += 1
        return ai_outcome(good_payload())

    response = CodingActionsService(
        FakeTruth(move_after_generation=True), ai_runner=fake_ai  # type: ignore[arg-type]
    ).suggest_modification(suggest_request())
    assert response == {"state": "refused", "reason": "stale_target"}
    assert calls == 1


@pytest.mark.parametrize(
    "change",
    [
        {"path": "other.py", "plan": "escape"},
        {
            "path": "backend/app/example.py",
            "diff": "--- a/backend/app/example.py\n+++ b/other.py\n@@ -1 +1 @@\n-x\n+y",
        },
        {
            "path": "backend/app/example.py",
            "diff": "diff --git a/other.py b/other.py\n--- a/backend/app/example.py\n+++ b/backend/app/example.py\n@@ -1 +1 @@\n-x\n+y",
        },
        {
            "path": "backend/app/example.py",
            "diff": "old mode 100644\nnew mode 100755\n--- a/backend/app/example.py\n+++ b/backend/app/example.py",
        },
        {
            "path": "backend/app/example.py",
            "diff": "new file mode 100644\n--- /dev/null\n+++ b/backend/app/example.py",
        },
        {
            "path": "backend/app/example.py",
            "diff": "deleted file mode 100644\n--- a/backend/app/example.py\n+++ /dev/null",
        },
        {
            "path": "backend/app/example.py",
            "diff": "GIT binary patch\n--- a/backend/app/example.py\n+++ b/backend/app/example.py",
        },
    ],
)
def test_hostile_or_out_of_scope_model_changes_are_rejected(change: dict[str, object]) -> None:
    payload = good_payload()
    payload["changes"] = [change]
    service = CodingActionsService(FakeTruth(), ai_runner=lambda **_: ai_outcome(payload))  # type: ignore[arg-type]
    assert service.suggest_modification(suggest_request()) == {
        "state": "refused",
        "reason": "proposal_invalid",
    }


def test_malformed_model_payload_maps_to_closed_refusal() -> None:
    bad = ai_outcome(good_payload())
    assert bad.response is not None
    bad.response.text = "not-json"
    service = CodingActionsService(FakeTruth(), ai_runner=lambda **_: bad)  # type: ignore[arg-type]
    assert service.suggest_modification(suggest_request()) == {
        "state": "refused",
        "reason": "proposal_invalid",
    }


def test_route_scoped_registry_allows_stable_read_identity_but_never_commit_execute() -> None:
    registry = JarvisCapabilityRegistry()
    registry.register(
        JarvisCapabilityDescriptor(capability_id="coding.inspect", route_id="coding-repository", action_class="READ")
    )
    registry.register(
        JarvisCapabilityDescriptor(capability_id="coding.inspect", route_id="coding-runtime", action_class="READ")
    )
    assert [cap.capability_id for cap in registry.for_route("coding-repository")] == ["coding.inspect"]
    with pytest.raises(JarvisContextError):
        registry.register(
            JarvisCapabilityDescriptor(capability_id="coding.apply", route_id="coding-repository", action_class="COMMIT")
        )
