from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.modules.local_ai.decision_contracts import DecisionResult, EnumDecisionOutput, ScoreDecisionOutput
from app.modules.local_ai.decision_gateway import DecisionGateway
from app.modules.local_ai.decision_service import LocalCandidate
from app.modules.local_ai.resource_contracts import LoadedModelState, RuntimeResourceSnapshot


@pytest.mark.parametrize(("kind", "payload", "recommendation"), [
    ("route_class", {"summary": "Answer the question", "read_tool_ids": [],
                      "required_capability_available": True}, "answer_directly"),
    ("route_class", {"summary": "Find current records", "read_tool_ids": ["jarvis_retrieval_query"],
                      "required_capability_available": True}, "use_read_tool"),
    ("route_class", {"summary": "Action requires unavailable access", "read_tool_ids": [],
                      "required_capability_available": False}, "needs_ungranted_capability"),
    ("retry_or_stop", {"previous_outcome": "failed", "attempt_count": 0, "retryable": True}, "retry"),
    ("retry_or_stop", {"previous_outcome": "empty", "attempt_count": 1, "retryable": True}, "stop"),
    ("escalate", {"summary": "Current route failed", "stronger_route_ids": [],
                   "local_route_available": False}, "ask_human"),
    ("escalate", {"summary": "Current route failed", "stronger_route_ids": ["local:coder"],
                   "local_route_available": True}, "suggest_stronger_route"),
])
def test_rules_gateway_returns_typed_advice(kind: str, payload: dict[str, Any], recommendation: str) -> None:
    result = DecisionGateway.from_config().decide(kind, payload)
    assert result["outcome"] == "decided"
    assert result["recommendation"] == recommendation
    assert result["confidence"] == 1.0
    assert str(result["request_digest"]).startswith("sha256:")
    assert result["model_ref"] == "jarvis.laya-rules.v1"


@pytest.mark.parametrize(("kind", "payload"), [
    ("route_class", {"summary": "x" * 2001, "required_capability_available": True}),
    ("route_class", {"summary": "ok", "required_capability_available": True, "extra": True}),
    ("retry_or_stop", {"previous_outcome": "unknown", "attempt_count": 0, "retryable": True}),
    ("retry_or_stop", {"previous_outcome": "failed", "attempt_count": 9, "retryable": True}),
    ("escalate", {"summary": "x", "local_route_available": True, "stronger_route_ids": ["r"] * 9}),
    ("model_select", {"task_kind": "general", "candidate_ids": ["local:a"], "context_tokens": True}),
])
def test_gateway_abstains_on_invalid_or_unregistered_request(kind: str, payload: dict[str, Any]) -> None:
    result = DecisionGateway.from_config().decide(kind, payload)
    assert result["outcome"] == "abstained"
    assert result["recommendation"] is None


def test_unknown_kind_abstains_without_disclosing_request() -> None:
    secret = "private task text"
    result = DecisionGateway.from_config().decide("other", {"summary": secret})
    assert result["outcome"] == "abstained"
    assert secret not in str(result)


class _Backend:
    model_ref = "test.backend@rev-1"

    def __init__(self, *, score: float = 0.9, recommendation: str = "retry", invalid: bool = False) -> None:
        self.score = score
        self.recommendation = recommendation
        self.invalid = invalid
        self.calls = 0

    def decide(self, kind: str, request: Any, _payload: Any, _candidates: Any) -> DecisionResult:
        self.calls += 1
        if self.invalid == "error":
            raise RuntimeError("backend unavailable")
        return DecisionResult(
            decision_id=request.decision_id, outcome="decided",
            outputs=(EnumDecisionOutput(name="recommendation", value="bad" if self.invalid else self.recommendation),
                     ScoreDecisionOutput(name="confidence", value=self.score)),
            model_ref=self.model_ref, reason_code="test", decided_at=datetime.now(UTC),
        )


def test_backend_is_replaceable_without_changing_gateway_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_LAYA_BACKEND", "test")
    backend = _Backend()
    gateway = DecisionGateway.from_config(backend=backend)
    result = gateway.decide("retry_or_stop", {"previous_outcome": "failed", "attempt_count": 0,
                                               "retryable": True})
    assert result["outcome"] == "decided" and result["recommendation"] == "retry"
    assert result["model_ref"] == "test.backend@rev-1" and backend.calls == 1


def test_unavailable_configured_backend_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_LAYA_BACKEND", "unqualified_local")
    result = DecisionGateway.from_config().decide(
        "retry_or_stop", {"previous_outcome": "failed", "attempt_count": 0, "retryable": True})
    assert result["outcome"] == "abstained"
    assert result["model_ref"] == "jarvis.laya-unavailable.unqualified_local"


@pytest.mark.parametrize(("backend", "expected_reason"), [
    (_Backend(score=0.49), "low_confidence"),
    (_Backend(invalid=True), "invalid_or_backend_error"),
    (_Backend(invalid="error"), "invalid_or_backend_error"),
])
def test_gateway_abstains_on_low_confidence_and_invalid_backend_result(
    backend: _Backend, expected_reason: str,
) -> None:
    result = DecisionGateway(backend).decide(
        "retry_or_stop", {"previous_outcome": "failed", "attempt_count": 0, "retryable": True})
    assert result["outcome"] == "abstained"
    assert result["reason_code"] == expected_reason


def test_gateway_abstains_when_backend_returns_after_request_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.local_ai import decision_gateway as gateway_module

    monkeypatch.setattr(gateway_module, "_REQUEST_DEADLINE", timedelta(microseconds=1))
    result = DecisionGateway(_Backend()).decide(
        "retry_or_stop", {"previous_outcome": "failed", "attempt_count": 0, "retryable": True})
    assert result["outcome"] == "abstained" and result["reason_code"] == "expired_request"


def test_model_select_intersects_safe_bindings_with_fresh_available_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.local_ai import decision_gateway as gateway_module

    candidate = LocalCandidate("local:coder", "local-model", 8192, frozenset({"coder"}))
    unavailable = LocalCandidate("local:fast", "other-model", 8192, frozenset({"fast"}))
    monkeypatch.setattr(gateway_module, "local_candidates", lambda _registry: (candidate, unavailable))
    monkeypatch.setattr(gateway_module, "_safe_binding", lambda _candidate, _registry: object())
    monkeypatch.setattr(gateway_module, "load_default_provider_registry", lambda: object())
    monkeypatch.setattr(gateway_module, "get_resource_arbiter", lambda: type("Arbiter", (), {
        "snapshot": lambda _self: RuntimeResourceSnapshot(
            generation=1, observed_at=datetime.now(UTC),
            loaded_models=(LoadedModelState(name="local-model"),),
        )
    })())
    available_reads: list[set[str]] = [set()]
    gateway = DecisionGateway(available_routes=lambda: available_reads[-1])
    payload = {"task_kind": "coding", "candidate_ids": ["local:coder", "local:fast"],
               "context_tokens": 10}
    assert gateway.decide("model_select", payload)["outcome"] == "abstained"
    available_reads.append({"local:coder"})
    result = gateway.decide("model_select", payload)
    assert result["outcome"] == "decided" and result["recommendation"] == "local:coder"


def test_rules_route_class_abstains_on_contentless_summary() -> None:
    result = DecisionGateway(available_routes=lambda: set()).decide("route_class", {
        "summary": "?", "read_tool_ids": [], "required_capability_available": True})
    assert result["outcome"] == "abstained"
    assert result["reason_code"] == "low_confidence"
