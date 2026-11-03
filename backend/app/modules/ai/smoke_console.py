from app.core.database import open_sqlite_connection
from app.modules.ai.budget import (
    evaluate_live_scaleway_smoke_gate,
    scaleway_usage_snapshot,
)
from app.modules.ai.execution import AiTaskOutcome, run_ai_task
from app.modules.ai.models import AISettingsRead, SmokeConsoleRequest, SmokeConsoleResponse
from app.modules.ai.privacy import PrivacyPolicyEngine
from app.modules.ai.provider_registry import registry_bindings
from app.modules.ai.providers.scaleway_adapter import SCALEWAY_ADAPTER_INTERFACE
from app.modules.ai.settings import get_ai_settings
from app.modules.ai.token_guard import estimate_tokens
from app.modules.events.service import log_event, utc_now

SMOKE_CONSOLE_MODE = "live_smoke_console"
SMOKE_CONSOLE_ROUTE = "external:scaleway"
SMOKE_CONSOLE_TASK_KIND = "synthesis"
SMOKE_CONSOLE_TOKEN_THRESHOLD = 500000
SMOKE_CONSOLE_DEFAULT_OUTPUT_TOKENS = 80
SMOKE_CONSOLE_MAX_OUTPUT_TOKENS = 80
SMOKE_CONSOLE_MAX_PROMPT_LENGTH = 500


def run_smoke_console(request: SmokeConsoleRequest) -> SmokeConsoleResponse:
    settings = get_ai_settings()
    provider, model = _binding_identity()
    prompt = request.prompt.strip()
    requested_output_tokens = (
        request.max_output_tokens or SMOKE_CONSOLE_DEFAULT_OUTPUT_TOKENS
    )
    estimated_output_tokens = min(
        requested_output_tokens,
        SMOKE_CONSOLE_MAX_OUTPUT_TOKENS,
    )
    estimated_input_tokens = estimate_tokens(prompt) if prompt else 0

    _log_console_event(
        "AISmokeConsoleStarted",
        settings=settings,
        workspace_id=request.workspace_id,
        provider=provider,
        model=model,
        privacy_class="not_evaluated",
        blocked_reason=None,
        external_call_attempted=False,
        external_call_succeeded=False,
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
        actual_input_tokens=None,
        actual_output_tokens=None,
        usage_source="estimated",
        prompt_length=len(prompt),
    )

    gate_reason = evaluate_live_scaleway_smoke_gate(
        settings,
        settings.provider_mode,
    )
    if gate_reason:
        return _blocked_response(
            settings=settings,
            workspace_id=request.workspace_id,
            provider=provider,
            model=model,
            privacy_class="not_evaluated",
            blocked_reason=gate_reason,
            external_call_attempted=False,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            actual_input_tokens=None,
            actual_output_tokens=None,
            usage_source="estimated",
            prompt_length=len(prompt),
        )

    if not prompt:
        return _blocked_response(
            settings=settings,
            workspace_id=request.workspace_id,
            provider=provider,
            model=model,
            privacy_class="unknown",
            blocked_reason="smoke_console_prompt_empty",
            external_call_attempted=False,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            actual_input_tokens=None,
            actual_output_tokens=None,
            usage_source="estimated",
            prompt_length=0,
        )

    if len(prompt) > SMOKE_CONSOLE_MAX_PROMPT_LENGTH:
        return _blocked_response(
            settings=settings,
            workspace_id=request.workspace_id,
            provider=provider,
            model=model,
            privacy_class="unknown",
            blocked_reason="smoke_console_prompt_too_long",
            external_call_attempted=False,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            actual_input_tokens=None,
            actual_output_tokens=None,
            usage_source="estimated",
            prompt_length=len(prompt),
        )

    if requested_output_tokens > SMOKE_CONSOLE_MAX_OUTPUT_TOKENS:
        return _blocked_response(
            settings=settings,
            workspace_id=request.workspace_id,
            provider=provider,
            model=model,
            privacy_class="unknown",
            blocked_reason="smoke_console_max_output_tokens_exceeded",
            external_call_attempted=False,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            actual_input_tokens=None,
            actual_output_tokens=None,
            usage_source="estimated",
            prompt_length=len(prompt),
        )

    policy_decision = PrivacyPolicyEngine().decide_for_smoke_console(
        prompt,
        policy_mode=settings.policy_mode,
    )
    if not policy_decision.external_allowed:
        return _blocked_response(
            settings=settings,
            workspace_id=request.workspace_id,
            provider=provider,
            model=model,
            privacy_class=policy_decision.privacy_class,
            blocked_reason=(
                policy_decision.blocking_reason or "privacy_policy_blocked"
            ),
            external_call_attempted=False,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            actual_input_tokens=None,
            actual_output_tokens=None,
            usage_source="estimated",
            prompt_length=len(prompt),
        )

    try:
        outcome = run_ai_task(
            user_prompt=prompt,
            task_kind=SMOKE_CONSOLE_TASK_KIND,
            route_class=SMOKE_CONSOLE_ROUTE,
            max_output_tokens=estimated_output_tokens,
            workspace_id=request.workspace_id,
        )
    except Exception as exc:
        return _blocked_response(
            settings=get_ai_settings(),
            workspace_id=request.workspace_id,
            provider=provider,
            model=model,
            privacy_class=policy_decision.privacy_class,
            blocked_reason="smoke_console_execution_failed",
            external_call_attempted=False,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            actual_input_tokens=None,
            actual_output_tokens=None,
            usage_source="estimated",
            prompt_length=len(prompt),
            error_type=type(exc).__name__,
        )

    settings = get_ai_settings()
    if outcome.status != "success" or outcome.response is None:
        attempted = _external_call_attempted(outcome)
        return _blocked_response(
            settings=settings,
            workspace_id=request.workspace_id,
            provider=(
                outcome.response.provider_id
                if outcome.response is not None
                else provider
            ),
            model=(
                outcome.response.model_id
                if outcome.response is not None
                else model
            ),
            privacy_class=policy_decision.privacy_class,
            blocked_reason=_outcome_blocking_reason(outcome),
            external_call_attempted=attempted,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            actual_input_tokens=None,
            actual_output_tokens=None,
            usage_source="estimated",
            prompt_length=len(prompt),
            error_type=outcome.error_type,
            egress_decision_id=outcome.egress_decision_id,
            egress_ticket_id=outcome.egress_ticket_id,
            egress_reservation_id=outcome.egress_reservation_id,
        )

    response = outcome.response
    actual_input_tokens = _reported_token(
        response.raw_provider_metadata.get("reported_input_tokens")
    )
    actual_output_tokens = _reported_token(
        response.raw_provider_metadata.get("reported_output_tokens")
    )
    usage_source = _usage_source_value(response.usage.usage_source)
    result = _response(
        settings=settings,
        response_text=response.text,
        provider=response.provider_id,
        model=response.model_id,
        privacy_class=policy_decision.privacy_class,
        blocked_reason=None,
        external_call_attempted=_external_call_attempted(outcome),
        external_call_succeeded=True,
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
        actual_input_tokens=actual_input_tokens,
        actual_output_tokens=actual_output_tokens,
        usage_source=usage_source,
    )
    _log_console_event(
        "AISmokeConsoleCompleted",
        settings=settings,
        workspace_id=request.workspace_id,
        provider=response.provider_id,
        model=response.model_id,
        privacy_class=policy_decision.privacy_class,
        blocked_reason=None,
        external_call_attempted=result.external_call_attempted,
        external_call_succeeded=True,
        estimated_input_tokens=result.estimated_input_tokens,
        estimated_output_tokens=result.estimated_output_tokens,
        actual_input_tokens=result.actual_input_tokens,
        actual_output_tokens=result.actual_output_tokens,
        usage_source=result.usage_source,
        prompt_length=len(prompt),
        egress_decision_id=outcome.egress_decision_id,
        egress_ticket_id=outcome.egress_ticket_id,
        egress_reservation_id=outcome.egress_reservation_id,
    )
    return result


def _binding_identity() -> tuple[str, str]:
    binding = registry_bindings().get(SMOKE_CONSOLE_ROUTE)
    if binding is None:
        return "scaleway", "unbound"
    return binding.provider_id, binding.model_id


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


def _blocked_response(
    *,
    settings: AISettingsRead,
    workspace_id: str | None,
    provider: str,
    model: str,
    privacy_class: str,
    blocked_reason: str,
    external_call_attempted: bool,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    actual_input_tokens: int | None,
    actual_output_tokens: int | None,
    usage_source: str,
    prompt_length: int,
    error_type: str | None = None,
    egress_decision_id: str | None = None,
    egress_ticket_id: str | None = None,
    egress_reservation_id: str | None = None,
) -> SmokeConsoleResponse:
    response = _response(
        settings=settings,
        response_text=None,
        provider=provider,
        model=model,
        privacy_class=privacy_class,
        blocked_reason=blocked_reason,
        external_call_attempted=external_call_attempted,
        external_call_succeeded=False,
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
        actual_input_tokens=actual_input_tokens,
        actual_output_tokens=actual_output_tokens,
        usage_source=usage_source,
    )
    _log_console_event(
        "AISmokeConsoleBlocked",
        settings=settings,
        workspace_id=workspace_id,
        provider=provider,
        model=model,
        privacy_class=privacy_class,
        blocked_reason=blocked_reason,
        external_call_attempted=external_call_attempted,
        external_call_succeeded=False,
        estimated_input_tokens=response.estimated_input_tokens,
        estimated_output_tokens=response.estimated_output_tokens,
        actual_input_tokens=response.actual_input_tokens,
        actual_output_tokens=response.actual_output_tokens,
        usage_source=response.usage_source,
        prompt_length=prompt_length,
        error_type=error_type,
        egress_decision_id=egress_decision_id,
        egress_ticket_id=egress_ticket_id,
        egress_reservation_id=egress_reservation_id,
    )
    return response


def _response(
    *,
    settings: AISettingsRead,
    response_text: str | None,
    provider: str,
    model: str,
    privacy_class: str,
    blocked_reason: str | None,
    external_call_attempted: bool,
    external_call_succeeded: bool,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    actual_input_tokens: int | None,
    actual_output_tokens: int | None,
    usage_source: str,
) -> SmokeConsoleResponse:
    usage = scaleway_usage_snapshot(settings)
    current_input = usage.total_input_tokens
    current_output = usage.total_output_tokens
    current_total = usage.total_tokens
    return SmokeConsoleResponse(
        response_text=response_text,
        provider=provider,
        model=model,
        mode=SMOKE_CONSOLE_MODE,
        privacy_class=privacy_class,
        blocked_reason=blocked_reason,
        external_call_attempted=external_call_attempted,
        external_call_succeeded=external_call_succeeded,
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
        actual_input_tokens=actual_input_tokens,
        actual_output_tokens=actual_output_tokens,
        usage_source=usage_source,
        current_month_input_tokens=current_input,
        current_month_output_tokens=current_output,
        current_month_total_tokens=current_total,
        configured_monthly_token_cap=settings.scaleway_monthly_token_cap,
        token_threshold=SMOKE_CONSOLE_TOKEN_THRESHOLD,
        token_threshold_percent=_threshold_percent(current_total),
        remaining_tokens_to_threshold=max(
            SMOKE_CONSOLE_TOKEN_THRESHOLD - current_total,
            0,
        ),
    )


def _threshold_percent(total_tokens: int) -> float:
    return round((total_tokens / SMOKE_CONSOLE_TOKEN_THRESHOLD) * 100, 2)


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


def _log_console_event(
    event_type: str,
    *,
    settings: AISettingsRead,
    workspace_id: str | None,
    provider: str,
    model: str,
    privacy_class: str,
    blocked_reason: str | None,
    external_call_attempted: bool,
    external_call_succeeded: bool,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    actual_input_tokens: int | None,
    actual_output_tokens: int | None,
    usage_source: str,
    prompt_length: int,
    error_type: str | None = None,
    egress_decision_id: str | None = None,
    egress_ticket_id: str | None = None,
    egress_reservation_id: str | None = None,
) -> None:
    usage = scaleway_usage_snapshot(settings)
    current_total = usage.total_tokens
    payload = {
        "workspace_id": workspace_id,
        "provider": provider,
        "provider_id": provider,
        "model": model,
        "model_id": model,
        "adapter_interface": SCALEWAY_ADAPTER_INTERFACE,
        "mode": SMOKE_CONSOLE_MODE,
        "route_class": SMOKE_CONSOLE_ROUTE,
        "policy_mode": settings.policy_mode.value,
        "privacy_class": privacy_class,
        "blocked_reason": blocked_reason,
        "external_call_attempted": external_call_attempted,
        "external_call_succeeded": external_call_succeeded,
        "estimated_input_tokens": estimated_input_tokens,
        "estimated_output_tokens": estimated_output_tokens,
        "actual_input_tokens": actual_input_tokens,
        "actual_output_tokens": actual_output_tokens,
        "usage_source": usage_source,
        "current_month_input_tokens": usage.total_input_tokens,
        "current_month_output_tokens": usage.total_output_tokens,
        "current_month_total_tokens": current_total,
        "configured_monthly_token_cap": settings.scaleway_monthly_token_cap,
        "token_threshold": SMOKE_CONSOLE_TOKEN_THRESHOLD,
        "token_threshold_percent": _threshold_percent(current_total),
        "remaining_tokens_to_threshold": max(
            SMOKE_CONSOLE_TOKEN_THRESHOLD - current_total,
            0,
        ),
        "prompt_length": prompt_length,
        "timestamp": utc_now(),
        "error_type": error_type,
        "egress_decision_id": egress_decision_id,
        "egress_ticket_id": egress_ticket_id,
        "egress_reservation_id": egress_reservation_id,
    }
    with open_sqlite_connection() as connection:
        log_event(
            connection,
            event_type=event_type,
            actor="local-user",
            target_type="AISmokeConsole",
            target_id=None,
            workspace_id=workspace_id,
            payload=payload,
        )
        connection.commit()
