"""Backend-only Supervisor public-test slice.

This module backs the existing /ai/supervisor/public-test route and is not the
full Supervisor AI product, chat, memory, retrieval, or routing layer. External
dispatch is owned by run_ai_task.
"""

from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.ai.contracts import AIPolicyMode, AITaskType, AIUsage, AIUsageSource
from app.modules.ai.execution import AiTaskOutcome, run_ai_task
from app.modules.ai.models import AISettingsRead, SupervisorPublicTestRequest, SupervisorPublicTestResponse
from app.modules.ai.privacy import PrivacyPolicyEngine
from app.modules.ai.settings import get_ai_settings
from app.modules.ai.token_guard import estimate_tokens
from app.modules.events.service import log_event, utc_now

SUPERVISOR_PUBLIC_TEST_MODE = "supervisor_public_test"
SUPERVISOR_DEFAULT_OUTPUT_TOKENS = 180
SUPERVISOR_MAX_OUTPUT_TOKENS = 240
SUPERVISOR_MAX_PROMPT_LENGTH = 2000
SUPERVISOR_ROUTE_BY_PROVIDER_MODE = {
    "deepseek": "external:deepseek",
    "scaleway": "external:scaleway",
}
SUPERVISOR_ALLOWED_TASK_TYPES = {
    AITaskType.smoke_console_test,
    AITaskType.assumption_review,
    AITaskType.equation_review,
    AITaskType.simulation_result_interpretation,
    AITaskType.runner_error_explanation,
    AITaskType.code_review,
}
SUPERVISOR_LIMITATIONS = [
    "Narrow public/internal technical test endpoint only.",
    "No chat history, memory, file upload, source grounding, runner execution, or proprietary project workflow.",
    "Operator provider choice maps only to canonical provider-specific execution routes.",
]


def run_supervisor_public_test(request: SupervisorPublicTestRequest) -> SupervisorPublicTestResponse:
    settings = get_ai_settings()
    prompt = request.prompt.strip()
    task_type = request.task_type or AITaskType.equation_review
    requested_output_tokens = request.max_output_tokens or SUPERVISOR_DEFAULT_OUTPUT_TOKENS
    estimated_output_tokens = min(requested_output_tokens, SUPERVISOR_MAX_OUTPUT_TOKENS)
    estimated_input_tokens = estimate_tokens(prompt) if prompt else 0
    request_id = str(uuid4())
    correlation_id = str(uuid4())

    _log_supervisor_event(
        "AISupervisorPublicTestStarted",
        settings=settings,
        workspace_id=request.workspace_id,
        provider_id=None,
        model_id=None,
        task_type=task_type,
        privacy_class="not_evaluated",
        blocked_reason=None,
        external_call_attempted=False,
        external_call_succeeded=False,
        usage=None,
        prompt_length=len(prompt),
        request_id=request_id,
        correlation_id=correlation_id,
    )

    block = _preflight_block(
        settings=settings,
        prompt=prompt,
        requested_output_tokens=requested_output_tokens,
        task_type=task_type,
    )
    if block is not None:
        return _blocked_response(
            settings=settings,
            request=request,
            task_type=task_type,
            request_id=request_id,
            correlation_id=correlation_id,
            blocked_reason=block,
            privacy_class="unknown",
            event_type="AISupervisorPublicTestBlocked",
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
        )

    policy_decision = PrivacyPolicyEngine().decide_for_smoke_console(prompt, policy_mode=settings.policy_mode)
    if not policy_decision.external_allowed:
        return _blocked_response(
            settings=settings,
            request=request,
            task_type=task_type,
            request_id=request_id,
            correlation_id=correlation_id,
            blocked_reason=policy_decision.blocking_reason or "privacy_policy_blocked",
            privacy_class=policy_decision.privacy_class,
            event_type="AISupervisorPublicTestBlocked",
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
        )

    if policy_decision.privacy_class not in {"public", "internal"}:
        return _blocked_response(
            settings=settings,
            request=request,
            task_type=task_type,
            request_id=request_id,
            correlation_id=correlation_id,
            blocked_reason="supervisor_privacy_class_not_allowed",
            privacy_class=policy_decision.privacy_class,
            event_type="AISupervisorPublicTestBlocked",
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
        )

    route_class, route_block = _provider_route(settings)
    if route_class is None:
        return _blocked_response(
            settings=settings,
            request=request,
            task_type=task_type,
            request_id=request_id,
            correlation_id=correlation_id,
            blocked_reason=route_block or "provider_unavailable",
            privacy_class=policy_decision.privacy_class,
            event_type="AISupervisorPublicTestBlocked",
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
        )

    _log_supervisor_event(
        "AISupervisorPublicTestProviderSelected",
        settings=settings,
        workspace_id=request.workspace_id,
        provider_id=settings.provider_mode,
        model_id=None,
        task_type=task_type,
        privacy_class=policy_decision.privacy_class,
        blocked_reason=None,
        external_call_attempted=False,
        external_call_succeeded=False,
        usage=None,
        prompt_length=len(prompt),
        request_id=request_id,
        correlation_id=correlation_id,
    )

    outcome = run_ai_task(
        user_prompt=_supervisor_prompt(task_type, prompt),
        task_kind="test",
        route_class=route_class,
        max_output_tokens=estimated_output_tokens,
        workspace_id=request.workspace_id,
    )
    return _response_from_outcome(
        outcome,
        settings=settings,
        request=request,
        task_type=task_type,
        request_id=request_id,
        correlation_id=correlation_id,
        privacy_class=policy_decision.privacy_class,
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
    )


def _preflight_block(
    *,
    settings: AISettingsRead,
    prompt: str,
    requested_output_tokens: int,
    task_type: AITaskType,
) -> str | None:
    if settings.policy_mode != AIPolicyMode.FAST_DEV:
        return "supervisor_public_test_requires_fast_dev_policy"
    if not prompt:
        return "supervisor_prompt_empty"
    if len(prompt) > SUPERVISOR_MAX_PROMPT_LENGTH:
        return "supervisor_prompt_too_long"
    if requested_output_tokens > SUPERVISOR_MAX_OUTPUT_TOKENS:
        return "supervisor_max_output_tokens_exceeded"
    if task_type not in SUPERVISOR_ALLOWED_TASK_TYPES:
        return "supervisor_task_type_not_allowed"
    if _looks_like_file_path_request(prompt):
        return "supervisor_file_paths_not_supported"
    return None


def _provider_route(settings: AISettingsRead) -> tuple[str | None, str | None]:
    route_class = SUPERVISOR_ROUTE_BY_PROVIDER_MODE.get(settings.provider_mode)
    if route_class is None:
        return None, "provider_unavailable"
    if settings.provider_mode == "scaleway":
        if not settings.scaleway_enabled:
            return None, "scaleway_disabled"
        if not settings.scaleway_smoke_test_enabled:
            return None, "scaleway_smoke_test_disabled"
        if not settings.scaleway_live_smoke_test_enabled:
            return None, "scaleway_live_smoke_test_disabled"
    return route_class, None


def _response_from_outcome(
    outcome: AiTaskOutcome,
    *,
    settings: AISettingsRead,
    request: SupervisorPublicTestRequest,
    task_type: AITaskType,
    request_id: str,
    correlation_id: str,
    privacy_class: str,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
) -> SupervisorPublicTestResponse:
    ai_response = outcome.response
    provider_id = ai_response.provider_id if ai_response is not None else outcome.decision.provider_id
    model_id = ai_response.model_id if ai_response is not None else outcome.decision.model_id
    usage = (
        ai_response.usage
        if ai_response is not None
        else AIUsage(
            provider_id=provider_id or "none",
            model_id=model_id or "none",
            input_tokens=estimated_input_tokens,
            output_tokens=estimated_output_tokens,
            usage_source=AIUsageSource.estimated,
        )
    )
    attempted = bool(
        ai_response is not None
        and ai_response.raw_provider_metadata.get("external_call_attempted", True)
    )

    if outcome.status != "success" or ai_response is None or ai_response.error is not None:
        blocked_reason = _outcome_blocked_reason(outcome)
        event_id = _log_supervisor_event(
            "AISupervisorPublicTestProviderFailed",
            settings=settings,
            workspace_id=request.workspace_id,
            provider_id=provider_id,
            model_id=model_id,
            task_type=task_type,
            privacy_class=privacy_class,
            blocked_reason=blocked_reason,
            external_call_attempted=attempted,
            external_call_succeeded=False,
            usage=usage,
            prompt_length=len(request.prompt.strip()),
            request_id=request_id,
            correlation_id=correlation_id,
            error_code=(ai_response.error.code.value if ai_response is not None and ai_response.error else outcome.error_type),
        )
        return SupervisorPublicTestResponse(
            answer=None,
            task_type=task_type,
            policy_mode=settings.policy_mode,
            provider_id=provider_id,
            model_id=model_id,
            usage=usage,
            safety_status="blocked",
            blocked_reason=blocked_reason,
            event_id=event_id,
            request_id=request_id,
            correlation_id=correlation_id,
            external_call_attempted=attempted,
            external_call_succeeded=False,
            limitations=SUPERVISOR_LIMITATIONS,
        )

    event_id = _log_supervisor_event(
        "AISupervisorPublicTestCompleted",
        settings=settings,
        workspace_id=request.workspace_id,
        provider_id=provider_id,
        model_id=model_id,
        task_type=task_type,
        privacy_class=privacy_class,
        blocked_reason=None,
        external_call_attempted=attempted,
        external_call_succeeded=True,
        usage=usage,
        prompt_length=len(request.prompt.strip()),
        request_id=request_id,
        correlation_id=correlation_id,
    )
    return SupervisorPublicTestResponse(
        answer=ai_response.text,
        task_type=task_type,
        policy_mode=settings.policy_mode,
        provider_id=provider_id,
        model_id=model_id,
        usage=usage,
        safety_status="allowed",
        blocked_reason=None,
        event_id=event_id,
        request_id=request_id,
        correlation_id=correlation_id,
        external_call_attempted=attempted,
        external_call_succeeded=True,
        limitations=SUPERVISOR_LIMITATIONS,
    )


def _outcome_blocked_reason(outcome: AiTaskOutcome) -> str:
    if outcome.response is not None and outcome.response.blocked_reason:
        return outcome.response.blocked_reason
    return (
        outcome.egress_reason_code
        or outcome.decision.blocked_reason
        or outcome.error_type
        or f"canonical_execution_{outcome.status}"
    )


def _supervisor_prompt(task_type: AITaskType, prompt: str) -> str:
    return (
        "You are JarvisOS Supervisor AI in a narrow public/internal technical test mode. "
        "Answer concisely. Do not ask for or process API keys, Authorization headers, .env files, "
        "private keys, passwords, or secrets. Do not claim to run code or inspect files.\n\n"
        f"Task type: {task_type.value}\n"
        f"User technical prompt:\n{prompt}"
    )


def _looks_like_file_path_request(prompt: str) -> bool:
    lowered = prompt.lower()
    file_markers = ("c:\\", "/", "\\", ".csv", ".xlsx", ".pdf", ".docx", ".py")
    file_verbs = ("open ", "read ", "load ", "parse ", "upload ", "attach ")
    return any(marker in lowered for marker in file_markers) and any(verb in lowered for verb in file_verbs)


def _blocked_response(
    *,
    settings: AISettingsRead,
    request: SupervisorPublicTestRequest,
    task_type: AITaskType,
    request_id: str,
    correlation_id: str,
    blocked_reason: str,
    privacy_class: str,
    event_type: str,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
) -> SupervisorPublicTestResponse:
    usage = AIUsage(
        provider_id="none",
        model_id="none",
        input_tokens=estimated_input_tokens,
        output_tokens=estimated_output_tokens,
        usage_source=AIUsageSource.estimated,
    )
    event_id = _log_supervisor_event(
        event_type,
        settings=settings,
        workspace_id=request.workspace_id,
        provider_id=None,
        model_id=None,
        task_type=task_type,
        privacy_class=privacy_class,
        blocked_reason=blocked_reason,
        external_call_attempted=False,
        external_call_succeeded=False,
        usage=usage,
        prompt_length=len(request.prompt.strip()),
        request_id=request_id,
        correlation_id=correlation_id,
    )
    return SupervisorPublicTestResponse(
        answer=None,
        task_type=task_type,
        policy_mode=settings.policy_mode,
        provider_id=None,
        model_id=None,
        usage=usage,
        safety_status="blocked",
        blocked_reason=blocked_reason,
        event_id=event_id,
        request_id=request_id,
        correlation_id=correlation_id,
        external_call_attempted=False,
        external_call_succeeded=False,
        limitations=SUPERVISOR_LIMITATIONS,
    )


def _log_supervisor_event(
    event_type: str,
    *,
    settings: AISettingsRead,
    workspace_id: str | None,
    provider_id: str | None,
    model_id: str | None,
    task_type: AITaskType,
    privacy_class: str,
    blocked_reason: str | None,
    external_call_attempted: bool,
    external_call_succeeded: bool,
    usage: AIUsage | None,
    prompt_length: int,
    request_id: str,
    correlation_id: str,
    error_code: str | None = None,
) -> str:
    payload = {
        "workspace_id": workspace_id,
        "policy_mode": settings.policy_mode.value,
        "mode": SUPERVISOR_PUBLIC_TEST_MODE,
        "task_type": task_type.value,
        "provider_id": provider_id,
        "model_id": model_id,
        "privacy_class": privacy_class,
        "blocked_reason": blocked_reason,
        "external_call_attempted": external_call_attempted,
        "external_call_succeeded": external_call_succeeded,
        "usage": usage.model_dump(mode="json") if usage else None,
        "prompt_length": prompt_length,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "timestamp": utc_now(),
        "error_code": error_code,
    }
    with open_sqlite_connection() as connection:
        event_id = log_event(
            connection,
            event_type=event_type,
            actor="local-user",
            target_type="AISupervisorPublicTest",
            target_id=None,
            workspace_id=workspace_id,
            payload=payload,
        )
        connection.commit()
    return event_id
