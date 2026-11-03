from __future__ import annotations

import json

from app.modules.ai.contracts import AIResponse, AIUsage, RoutingDecision
from app.modules.ai.execution import AiTaskOutcome
from app.modules.coding.actions import CodingActionsService, CodingSuggestModificationRequest
from app.modules.coding.repository_truth import RepositoryTruthError, RepositoryTruthResult

REPOSITORY = "AlbertoRacerro/JarvisOS"
HEAD = "a" * 40


def _result(operation: str) -> RepositoryTruthResult:
    return RepositoryTruthResult(
        provider="github",
        repository=REPOSITORY,
        operation=operation,
        requested_ref="master",
        resolved_sha=HEAD,
        partial=False,
        payload={"text": "content"},
        observed_at="2026-09-04T00:00:00+00:00",
    )


class TruthThatFailsFinalRefresh:
    def __init__(self) -> None:
        self.ref_reads = 0

    def repository_ref_truth(self, repository: str, ref: str) -> RepositoryTruthResult:
        self.ref_reads += 1
        if self.ref_reads > 1:
            raise RepositoryTruthError("provider_unavailable", "sensitive provider detail")
        return _result("repository_ref_truth")

    def file_preview(self, repository: str, ref: str, path: str) -> RepositoryTruthResult:
        return _result("file_preview")


def _request() -> CodingSuggestModificationRequest:
    return CodingSuggestModificationRequest(
        workspace_id="ws-1",
        repository=REPOSITORY,
        base_ref="master",
        base_sha=HEAD,
        target_paths=["backend/app/example.py"],
        intent="Make behavior explicit",
    )


def _success_outcome() -> AiTaskOutcome:
    payload = {
        "summary": "Make behavior explicit",
        "changes": [{"path": "backend/app/example.py", "plan": "Add a bounded branch."}],
        "assumptions": [],
        "warnings": [],
        "expected_checks": [],
    }
    response = AIResponse(
        provider_id="fake",
        model_id="fake",
        usage=AIUsage(provider_id="fake", model_id="fake"),
        request_id="req-1",
        text=json.dumps(payload),
    )
    return AiTaskOutcome(
        "success",
        "job-1",
        "local:fake",
        RoutingDecision(provider_id="fake", model_id="fake", decision_reason="test"),
        response=response,
    )


def test_provider_exception_is_closed_refusal_without_error_leak() -> None:
    service = CodingActionsService(
        TruthThatFailsFinalRefresh(),  # type: ignore[arg-type]
        ai_runner=lambda **_: (_ for _ in ()).throw(RuntimeError("sensitive provider detail")),
    )
    assert service.suggest_modification(_request()) == {
        "state": "refused",
        "reason": "provider_unavailable",
    }


def test_final_exact_ref_refresh_failure_is_closed_missing_evidence() -> None:
    service = CodingActionsService(
        TruthThatFailsFinalRefresh(),  # type: ignore[arg-type]
        ai_runner=lambda **_: _success_outcome(),
    )
    assert service.suggest_modification(_request()) == {
        "state": "refused",
        "reason": "missing_evidence",
    }


def test_current_secret_owner_path_is_rejected_before_provider_dispatch() -> None:
    calls = 0

    def forbidden_ai(**_: object) -> AiTaskOutcome:
        nonlocal calls
        calls += 1
        raise AssertionError("provider must not run for credential-bearing targets")

    service = CodingActionsService(
        TruthThatFailsFinalRefresh(),  # type: ignore[arg-type]
        ai_runner=forbidden_ai,
    )
    request = _request().model_copy(
        update={"target_paths": ["backend/app/modules/secrets/storage.py"]}
    )
    assert service.suggest_modification(request) == {
        "state": "refused",
        "reason": "unsupported_target",
    }
    assert calls == 0
