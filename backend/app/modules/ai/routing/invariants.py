from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CRITICAL_PERMISSION_FIELDS = (
    "route_action",
    "allowed_execution_mode",
    "provider_call_allowed_now",
    "external_network_allowed_now",
    "state_change_allowed_now",
)
SENSITIVE_BUCKETS = {"unknown", "sensitive", "secret"}
EXTERNAL_TARGET_PREFIX = "external:"


@dataclass(frozen=True)
class RouterDecisionInvariantResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_router_decision_for_runtime(decision: dict[str, Any]) -> RouterDecisionInvariantResult:
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(decision, dict):
        return RouterDecisionInvariantResult(ok=False, errors=["decision must be an object"])

    for field_name in CRITICAL_PERMISSION_FIELDS:
        if field_name not in decision:
            errors.append(f"missing critical permission field: {field_name}")

    route_action = decision.get("route_action")
    mode = decision.get("allowed_execution_mode")
    provider_call_allowed = decision.get("provider_call_allowed_now")
    external_network_allowed = decision.get("external_network_allowed_now")
    state_change_allowed = decision.get("state_change_allowed_now")
    sensitivity = _sensitivity_bucket(decision)

    if route_action == "blocked":
        _require_false(errors, decision, "provider_call_allowed_now", "blocked decision cannot allow provider call")
        _require_false(errors, decision, "external_network_allowed_now", "blocked decision cannot allow external network")
        _require_false(errors, decision, "state_change_allowed_now", "blocked decision cannot allow state change")

    if route_action in {"ask_clarification", "ask_user_confirm"} and provider_call_allowed is not False:
        errors.append(f"{route_action} decision cannot allow provider execution")

    if mode == "propose_only":
        if provider_call_allowed is not False:
            errors.append("propose_only decision cannot allow provider execution")
        if state_change_allowed is not False:
            errors.append("propose_only decision cannot allow state change")

    if mode == "blocked":
        _require_false(errors, decision, "provider_call_allowed_now", "blocked mode cannot allow provider call")
        _require_false(errors, decision, "external_network_allowed_now", "blocked mode cannot allow external network")
        _require_false(errors, decision, "state_change_allowed_now", "blocked mode cannot allow state change")

    if provider_call_allowed is False and decision.get("execution_authorized") is True:
        errors.append("provider execution cannot be authorized when provider_call_allowed_now is false")

    proposed_target = decision.get("proposed_external_target")
    if external_network_allowed is False and _is_external_target(proposed_target) and route_action != "ask_user_confirm":
        errors.append("external target cannot be treated as executable when external network is disallowed")

    if sensitivity in SENSITIVE_BUCKETS and external_network_allowed is True:
        errors.append("unsafe or unknown sensitivity cannot allow external network now")

    if route_action in {"route_external_candidate", "ask_user_confirm"} and _is_external_target(proposed_target):
        if provider_call_allowed is True or external_network_allowed is True:
            errors.append("external candidate/proposal cannot imply immediate provider execution")

    if decision.get("external_allowed") is False and external_network_allowed is True:
        errors.append("external_network_allowed_now cannot be true when external_allowed is false")

    if not isinstance(provider_call_allowed, bool):
        errors.append("provider_call_allowed_now must be boolean")
    if not isinstance(external_network_allowed, bool):
        errors.append("external_network_allowed_now must be boolean")
    if not isinstance(state_change_allowed, bool):
        errors.append("state_change_allowed_now must be boolean")

    if route_action not in {"answer_local", "route_local", "blocked", "ask_clarification", "ask_user_confirm"}:
        warnings.append(f"unknown route_action for slim runtime invariants: {route_action!r}")

    return RouterDecisionInvariantResult(ok=not errors, errors=errors, warnings=warnings)


def _require_false(errors: list[str], decision: dict[str, Any], field_name: str, message: str) -> None:
    if decision.get(field_name) is not False:
        errors.append(message)


def _is_external_target(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(EXTERNAL_TARGET_PREFIX)


def _sensitivity_bucket(decision: dict[str, Any]) -> str | None:
    value = decision.get("sensitivity_bucket")
    if isinstance(value, str):
        return value
    value = decision.get("sensitivity")
    if isinstance(value, str):
        return value
    metadata = decision.get("sensitivity_metadata")
    if isinstance(metadata, dict):
        value = metadata.get("sensitivity_bucket_proposal")
        if isinstance(value, str):
            return value
    return None
