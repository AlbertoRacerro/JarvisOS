from __future__ import annotations

from app.modules.ai.contracts import RoutingDecision
from app.modules.ai.egress_runtime import ExternalTaskOutcome, _terminalize_external_flow


def _failed_outcome(*, reason_code: str | None, error_type: str | None) -> ExternalTaskOutcome:
    return ExternalTaskOutcome(
        status="provider_error",
        ledger_id="job-1",
        selected_route_class="external:cheap",
        decision=RoutingDecision(
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            decision_reason="test",
        ),
        response=None,
        error_type=error_type,
        context_digest=None,
        context_sources_count=0,
        retryable_error_code=None,
        egress_decision_id="decision-1",
        egress_packet_digest="sha256:" + "a" * 64,
        egress_ticket_id=None,
        egress_reservation_id="reservation-1",
        egress_reason_code=reason_code,
        egress_trigger_ids=(),
        flow_id="flow-1",
    )


def test_policy_allow_reason_does_not_mask_provider_failure(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "app.modules.ai.egress_runtime.transition_flow_state",
        lambda **kwargs: captured.update(kwargs),
    )

    _terminalize_external_flow(
        "flow-1",
        _failed_outcome(reason_code="silent_allow", error_type="TimeoutError"),
    )

    assert captured["new_state"] == "failed_terminal"
    assert captured["terminal_reason"] == "timeouterror"
    assert captured["terminal_attempt_id"] == "job-1"


def test_specific_failure_reason_still_wins_over_generic_error_type(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "app.modules.ai.egress_runtime.transition_flow_state",
        lambda **kwargs: captured.update(kwargs),
    )

    _terminalize_external_flow(
        "flow-1",
        _failed_outcome(
            reason_code="response_binding_mismatch",
            error_type="EgressSpineStateError",
        ),
    )

    assert captured["new_state"] == "failed_terminal"
    assert captured["terminal_reason"] == "response_binding_mismatch"
