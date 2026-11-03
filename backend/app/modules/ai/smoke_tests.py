from dataclasses import dataclass

from app.core.database import open_sqlite_connection
from app.modules.ai.budget import (
    evaluate_live_scaleway_smoke_gate,
    scaleway_api_key_configured,
    scaleway_usage_snapshot,
)
from app.modules.ai.contracts import AIPolicyMode
from app.modules.ai.execution import AiTaskOutcome, run_ai_task
from app.modules.ai.models import SmokeTestResponse, SmokeTestResult, SmokeTestTokenMetadata
from app.modules.ai.privacy import PrivacyPolicyEngine
from app.modules.ai.provider_registry import registry_bindings
from app.modules.ai.providers.fake import FakeModelingProvider
from app.modules.ai.providers.scaleway import ScalewayProvider
from app.modules.ai.providers.scaleway_adapter import SCALEWAY_ADAPTER_INTERFACE
from app.modules.ai.settings import get_ai_settings
from app.modules.ai.token_guard import estimate_tokens, evaluate_token_guard
from app.modules.events.service import log_event

LIVE_SMOKE_ROUTE = "external:scaleway"
LIVE_SMOKE_TASK_KIND = "synthesis"
LIVE_SMOKE_OUTPUT_TOKENS = 80


@dataclass(frozen=True)
class SmokeCase:
    case_id: str
    text: str
    expected_classes: tuple[str, ...]
    live_allowed: bool = False


SMOKE_CASES = [
    SmokeCase(
        "public_research_request",
        "Public research request: summarize published literature on algae photobioreactor mixing.",
        ("public", "internal"),
        True,
    ),
    SmokeCase(
        "generic_engineering_note",
        "Generic engineering note for rough sizing of a support structure.",
        ("internal", "confidential"),
        True,
    ),
    SmokeCase(
        "quick_coupler_geometry",
        "Quick Coupler proprietary geometry with patent-like dimensions for a connector.",
        ("sensitive_ip",),
    ),
    SmokeCase(
        "api_key_example",
        "Example .env content: API key and password for a private service.",
        ("secret",),
    ),
    SmokeCase(
        "ambiguous_project_brainstorming",
        "Ambiguous brainstorming about a novel cultivation layout.",
        ("confidential", "sensitive_ip"),
    ),
]


def run_smoke_tests(
    provider_mode: str | None = None,
    smoke_mode: str = "synthetic",
) -> SmokeTestResponse:
    settings = get_ai_settings()
    mode = provider_mode or settings.provider_mode
    selected_smoke_mode = "live" if smoke_mode == "live" else "synthetic"
    fake_provider = FakeModelingProvider()
    scaleway_provider = ScalewayProvider()
    scaleway_route_model = _scaleway_route_model()
    scaleway_event_model = (
        scaleway_route_model
        if selected_smoke_mode == "live"
        else scaleway_provider.model()
    )
    policy = PrivacyPolicyEngine()
    external_attempted = False
    external_succeeded = False
    shared_live_block: tuple[str, dict[str, object] | None] | None = None

    _log_smoke_event(
        "AISmokeTestStarted",
        provider_mode=mode,
        smoke_mode=selected_smoke_mode,
        provider=_event_provider_for_mode(mode),
        model=_event_model_for_mode(
            mode,
            settings.default_ai_model,
            scaleway_event_model,
        ),
        reason=None,
        external_call_attempted=False,
        external_call_succeeded=False,
    )

    results: list[SmokeTestResult] = []
    for case in SMOKE_CASES:
        if selected_smoke_mode == "synthetic" and mode == "fake":
            local_decision = policy.decide_for_external_smoke_test(
                case.text,
                confidential_allowed=settings.scaleway_smoke_test_enabled,
            )
            token_decision = evaluate_token_guard(settings, input_text=case.text)
            fake_class = fake_provider.classify_smoke_case(case.text)
            results.append(
                _result(
                    case,
                    provider_mode=mode,
                    smoke_mode=selected_smoke_mode,
                    provider="fake",
                    local_privacy_class=local_decision.privacy_class,
                    token_metadata=token_decision.metadata,
                    fake_classification=fake_class,
                    provider_reported_class=fake_class,
                    passed=fake_class in case.expected_classes,
                    blocking_reason=None,
                    external_call_attempted=False,
                    external_call_succeeded=False,
                    response_text=None,
                    provider_metadata={"implementation": "deterministic_fake"},
                )
            )
            continue

        local_decision = policy.decide_for_external_smoke_test(
            case.text,
            confidential_allowed=(
                settings.scaleway_smoke_test_enabled
                if selected_smoke_mode == "synthetic"
                else False
            ),
        )

        if mode != "scaleway":
            token_metadata = _live_token_metadata(settings, case.text)
            reason = (
                "scaleway_provider_mode_required"
                if selected_smoke_mode == "live"
                else "unsupported_provider_mode"
            )
            results.append(
                _blocked_result(
                    case,
                    mode,
                    selected_smoke_mode,
                    reason,
                    local_decision.privacy_class,
                    token_metadata,
                )
            )
            continue

        if selected_smoke_mode == "live":
            token_metadata = _live_token_metadata(settings, case.text)
            gate_reason = evaluate_live_scaleway_smoke_gate(settings, mode)
            if gate_reason:
                results.append(
                    _blocked_result(
                        case,
                        mode,
                        selected_smoke_mode,
                        gate_reason,
                        local_decision.privacy_class,
                        token_metadata,
                    )
                )
                continue

            if not local_decision.external_allowed:
                results.append(
                    _blocked_result(
                        case,
                        mode,
                        selected_smoke_mode,
                        local_decision.blocking_reason
                        or "privacy_policy_blocked",
                        local_decision.privacy_class,
                        token_metadata,
                    )
                )
                continue

            if not case.live_allowed:
                results.append(
                    _blocked_result(
                        case,
                        mode,
                        selected_smoke_mode,
                        "live_smoke_case_not_allowed",
                        local_decision.privacy_class,
                        token_metadata,
                    )
                )
                continue

            if shared_live_block is not None:
                reason, provider_metadata = shared_live_block
                results.append(
                    _blocked_result(
                        case,
                        mode,
                        selected_smoke_mode,
                        reason,
                        local_decision.privacy_class,
                        token_metadata,
                        provider_metadata=provider_metadata,
                    )
                )
                continue

            try:
                outcome = run_ai_task(
                    user_prompt=case.text,
                    task_kind=LIVE_SMOKE_TASK_KIND,
                    route_class=LIVE_SMOKE_ROUTE,
                    max_output_tokens=LIVE_SMOKE_OUTPUT_TOKENS,
                )
            except Exception as exc:
                reason = "live_smoke_execution_failed"
                metadata = {
                    "route_class": LIVE_SMOKE_ROUTE,
                    "error_type": type(exc).__name__,
                }
                shared_live_block = (reason, metadata)
                results.append(
                    _blocked_result(
                        case,
                        mode,
                        selected_smoke_mode,
                        reason,
                        local_decision.privacy_class,
                        token_metadata,
                        provider_metadata=metadata,
                    )
                )
                continue

            settings = get_ai_settings()
            attempted = _external_call_attempted(outcome)
            external_attempted = external_attempted or attempted
            provider_metadata = _outcome_metadata(outcome)
            if outcome.status != "success" or outcome.response is None:
                reason = _outcome_blocking_reason(outcome)
                if outcome.egress_reason_code == "confirmation_required":
                    shared_live_block = (reason, provider_metadata)
                results.append(
                    _blocked_result(
                        case,
                        mode,
                        selected_smoke_mode,
                        reason,
                        local_decision.privacy_class,
                        _live_token_metadata(settings, case.text),
                        external_call_attempted=attempted,
                        provider_metadata=provider_metadata,
                    )
                )
                continue

            response = outcome.response
            external_succeeded = True
            reported_input = _reported_token(
                response.raw_provider_metadata.get("reported_input_tokens")
            )
            reported_output = _reported_token(
                response.raw_provider_metadata.get("reported_output_tokens")
            )
            usage_source = _usage_source_value(response.usage.usage_source)
            results.append(
                _result(
                    case,
                    provider_mode=mode,
                    smoke_mode=selected_smoke_mode,
                    provider=response.provider_id,
                    local_privacy_class=local_decision.privacy_class,
                    token_metadata=_live_token_metadata(
                        settings,
                        case.text,
                        reported_input_tokens=reported_input,
                        reported_output_tokens=reported_output,
                        usage_source=usage_source,
                    ),
                    fake_classification=None,
                    provider_reported_class=None,
                    passed=True,
                    blocking_reason=None,
                    external_call_attempted=attempted,
                    external_call_succeeded=True,
                    response_text=response.text,
                    provider_metadata=provider_metadata,
                )
            )
            continue

        gate_reason = _synthetic_scaleway_gate(settings)
        token_decision = evaluate_token_guard(settings, input_text=case.text)
        if gate_reason:
            results.append(
                _blocked_result(
                    case,
                    mode,
                    selected_smoke_mode,
                    gate_reason,
                    local_decision.privacy_class,
                    token_decision.metadata,
                )
            )
            continue

        if not local_decision.external_allowed:
            results.append(
                _blocked_result(
                    case,
                    mode,
                    selected_smoke_mode,
                    local_decision.blocking_reason or "privacy_policy_blocked",
                    local_decision.privacy_class,
                    token_decision.metadata,
                )
            )
            continue

        if not token_decision.allowed:
            results.append(
                _blocked_result(
                    case,
                    mode,
                    selected_smoke_mode,
                    token_decision.reason
                    or "scaleway_monthly_token_cap_exceeded",
                    local_decision.privacy_class,
                    token_decision.metadata,
                )
            )
            continue

        provider_status = scaleway_provider.status()
        results.append(
            _blocked_result(
                case,
                mode,
                selected_smoke_mode,
                provider_status.implementation,
                local_decision.privacy_class,
                token_decision.metadata,
                provider_metadata={
                    "implementation": provider_status.implementation,
                    "route_class": None,
                },
            )
        )

    event_type = (
        "AISmokeTestBlocked"
        if any(result.blocking_reason for result in results) and mode == "scaleway"
        else "AISmokeTestCompleted"
    )
    _log_smoke_event(
        event_type,
        provider_mode=mode,
        smoke_mode=selected_smoke_mode,
        provider=_event_provider_for_mode(mode),
        model=_event_model_for_mode(
            mode,
            settings.default_ai_model,
            scaleway_event_model,
        ),
        reason=(
            "blocked_results_present"
            if event_type == "AISmokeTestBlocked"
            else None
        ),
        result_count=len(results),
        external_call_attempted=external_attempted,
        external_call_succeeded=external_succeeded,
        results=results,
    )
    return SmokeTestResponse(
        provider_mode=mode,
        smoke_mode=selected_smoke_mode,
        external_call_attempted=external_attempted,
        external_call_succeeded=external_succeeded,
        results=results,
    )


def _synthetic_scaleway_gate(settings) -> str | None:
    if settings.policy_mode == AIPolicyMode.DISABLED:
        return "ai_policy_disabled"
    if not settings.paid_ai_enabled:
        return "paid_ai_disabled"
    if settings.monthly_api_budget_usd <= 0:
        return "monthly_budget_zero"
    if settings.api_spend_month_to_date_usd >= settings.monthly_api_budget_usd:
        return "monthly_budget_exhausted"
    if not settings.scaleway_enabled:
        return "scaleway_disabled"
    if not settings.scaleway_smoke_test_enabled:
        return "scaleway_smoke_test_disabled"
    if not scaleway_api_key_configured():
        return "scaleway_api_key_missing"
    return None


def _scaleway_route_model() -> str:
    binding = registry_bindings().get(LIVE_SMOKE_ROUTE)
    return binding.model_id if binding is not None else "unbound"


def _live_token_metadata(
    settings,
    text: str,
    *,
    reported_input_tokens: int | None = None,
    reported_output_tokens: int | None = None,
    usage_source: str = "estimated",
) -> SmokeTestTokenMetadata:
    usage = scaleway_usage_snapshot(settings)
    current_usage = usage.total_tokens
    return SmokeTestTokenMetadata(
        blocked_by_token_cap=(
            current_usage >= settings.scaleway_monthly_token_cap
            or current_usage >= settings.scaleway_hard_stop_token_cap
        ),
        estimated_input_tokens=estimate_tokens(text),
        estimated_output_tokens=LIVE_SMOKE_OUTPUT_TOKENS,
        reported_input_tokens=reported_input_tokens,
        reported_output_tokens=reported_output_tokens,
        monthly_token_cap=settings.scaleway_monthly_token_cap,
        hard_stop_token_cap=settings.scaleway_hard_stop_token_cap,
        token_usage_month_to_date=current_usage,
        usage_source=usage_source,
    )


def _external_call_attempted(outcome: AiTaskOutcome) -> bool:
    if outcome.response is None:
        return False
    return bool(
        outcome.response.raw_provider_metadata.get(
            "external_call_attempted",
            False,
        )
    )


def _outcome_blocking_reason(outcome: AiTaskOutcome) -> str:
    if outcome.response is not None and outcome.response.blocked_reason:
        return outcome.response.blocked_reason
    if outcome.egress_reason_code:
        return outcome.egress_reason_code
    if outcome.error_type:
        return outcome.error_type
    return outcome.status


def _outcome_metadata(outcome: AiTaskOutcome) -> dict[str, object]:
    metadata: dict[str, object] = {
        "adapter_interface": SCALEWAY_ADAPTER_INTERFACE,
        "route_class": LIVE_SMOKE_ROUTE,
        "egress_decision_id": outcome.egress_decision_id,
        "egress_ticket_id": outcome.egress_ticket_id,
        "egress_reservation_id": outcome.egress_reservation_id,
        "egress_reason_code": outcome.egress_reason_code,
        "flow_id": outcome.flow_id,
        "ledger_id": outcome.ledger_id,
    }
    if outcome.response is not None:
        metadata.update(outcome.response.raw_provider_metadata)
        metadata["model_id"] = outcome.response.model_id
        metadata["provider_id"] = outcome.response.provider_id
    return metadata


def _result(
    case: SmokeCase,
    *,
    provider_mode: str,
    smoke_mode: str,
    provider: str,
    local_privacy_class: str,
    token_metadata: SmokeTestTokenMetadata,
    fake_classification: str | None,
    provider_reported_class: str | None,
    passed: bool,
    blocking_reason: str | None,
    external_call_attempted: bool,
    external_call_succeeded: bool,
    response_text: str | None,
    provider_metadata: dict[str, object] | None,
) -> SmokeTestResult:
    return SmokeTestResult(
        case_id=case.case_id,
        input_excerpt=case.text[:120],
        expected_class="/".join(case.expected_classes),
        local_privacy_class=local_privacy_class,
        provider_reported_class=provider_reported_class,
        fake_classification=fake_classification,
        passed=passed,
        provider_mode=provider_mode,
        provider=provider,
        smoke_mode=smoke_mode,
        external_call_attempted=external_call_attempted,
        external_call_succeeded=external_call_succeeded,
        blocking_reason=blocking_reason,
        response_text=response_text,
        usage_source=token_metadata.usage_source,
        provider_metadata=provider_metadata,
        token_metadata=token_metadata,
    )


def _blocked_result(
    case: SmokeCase,
    provider_mode: str,
    smoke_mode: str,
    reason: str,
    local_privacy_class: str,
    token_metadata: SmokeTestTokenMetadata,
    external_call_attempted: bool = False,
    provider_metadata: dict[str, object] | None = None,
) -> SmokeTestResult:
    passed = (
        reason.startswith("privacy_policy")
        and local_privacy_class in case.expected_classes
    )
    return _result(
        case,
        provider_mode=provider_mode,
        smoke_mode=smoke_mode,
        provider=provider_mode,
        local_privacy_class=local_privacy_class,
        token_metadata=token_metadata,
        fake_classification=None,
        provider_reported_class=None,
        passed=passed,
        blocking_reason=reason,
        external_call_attempted=external_call_attempted,
        external_call_succeeded=False,
        response_text=None,
        provider_metadata=provider_metadata,
    )


def _reported_token(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _usage_source_value(value: object) -> str:
    enum_value = getattr(value, "value", None)
    return str(enum_value if enum_value is not None else value)


def _log_smoke_event(
    event_type: str,
    *,
    provider_mode: str,
    smoke_mode: str,
    provider: str,
    model: str,
    reason: str | None,
    result_count: int | None = None,
    external_call_attempted: bool,
    external_call_succeeded: bool,
    results: list[SmokeTestResult] | None = None,
) -> None:
    result_summary = [_event_result_summary(result) for result in results or []]
    with open_sqlite_connection() as connection:
        log_event(
            connection,
            event_type=event_type,
            actor="local-user",
            target_type="AISmokeTest",
            workspace_id=None,
            payload={
                "provider_mode": provider_mode,
                "mode": smoke_mode,
                "provider": provider,
                "provider_id": provider,
                "model": model,
                "model_id": model,
                "adapter_interface": (
                    SCALEWAY_ADAPTER_INTERFACE
                    if provider == "scaleway" and smoke_mode == "live"
                    else None
                ),
                "route_class": (
                    LIVE_SMOKE_ROUTE
                    if provider == "scaleway" and smoke_mode == "live"
                    else None
                ),
                "reason": reason,
                "result_count": result_count,
                "external_call_attempted": external_call_attempted,
                "external_call_succeeded": external_call_succeeded,
                "synthetic_only": smoke_mode == "synthetic",
                "results": result_summary,
            },
        )
        connection.commit()


def _event_result_summary(result: SmokeTestResult) -> dict[str, object]:
    provider_metadata = result.provider_metadata or {}
    return {
        "case_id": result.case_id,
        "provider": result.provider,
        "provider_id": result.provider,
        "provider_mode": result.provider_mode,
        "mode": result.smoke_mode,
        "route_class": provider_metadata.get("route_class"),
        "adapter_interface": provider_metadata.get("adapter_interface"),
        "privacy_class": result.local_privacy_class,
        "blocked_reason": result.blocking_reason,
        "blocked_by_token_cap": result.token_metadata.blocked_by_token_cap,
        "estimated_input_tokens": result.token_metadata.estimated_input_tokens,
        "estimated_output_tokens": result.token_metadata.estimated_output_tokens,
        "reported_input_tokens": result.token_metadata.reported_input_tokens,
        "reported_output_tokens": result.token_metadata.reported_output_tokens,
        "usage_source": result.token_metadata.usage_source,
        "token_usage_month_to_date": result.token_metadata.token_usage_month_to_date,
        "external_call_attempted": result.external_call_attempted,
        "external_call_succeeded": result.external_call_succeeded,
        "egress_decision_id": provider_metadata.get("egress_decision_id"),
        "egress_ticket_id": provider_metadata.get("egress_ticket_id"),
        "egress_reservation_id": provider_metadata.get("egress_reservation_id"),
    }


def _event_provider_for_mode(provider_mode: str) -> str:
    if provider_mode == "fake":
        return "fake"
    if provider_mode == "scaleway":
        return "scaleway"
    return "none"


def _event_model_for_mode(
    provider_mode: str,
    default_model: str,
    scaleway_model: str,
) -> str:
    if provider_mode == "fake":
        return default_model
    if provider_mode == "scaleway":
        return scaleway_model
    return "none"
