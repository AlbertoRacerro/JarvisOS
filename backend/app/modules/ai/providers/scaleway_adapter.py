import httpx

from app.modules.ai.contracts import (
    AIExternalDispatchState,
    AIMessage,
    AIModelCapability,
    AIPrivacyClass,
    AIProviderError,
    AIProviderErrorCode,
    AIProviderHealth,
    AIProviderKind,
    AIProviderStatus,
    AIRequest,
    AIResponse,
    AITaskType,
    AIUsage,
    AIUsageSource,
    ModelRegistryEntry,
    ProviderRegistryEntry,
)
from app.modules.ai.providers.scaleway import (
    ScalewayChatResult,
    ScalewayNotConfiguredError,
    ScalewayProvider,
)
from app.modules.ai.token_guard import estimate_tokens
from app.modules.ai.usage_cost import actual_registry_cost_usd

SCALEWAY_PROVIDER_ID = "scaleway"
SCALEWAY_ADAPTER_INTERFACE = "provider_neutral"
SCALEWAY_WORK_TASK_TYPES = {
    AITaskType.synthesis,
    AITaskType.code_review,
    AITaskType.decision_support,
}
SAFE_SCALEWAY_METADATA_KEYS = {
    "implementation",
    "base_url_configured",
    "usage_returned",
    "finish_reason",
}


class ScalewayProviderAdapter:
    provider_id = SCALEWAY_PROVIDER_ID

    def __init__(self, provider: ScalewayProvider | None = None) -> None:
        self.provider = provider or ScalewayProvider()

    def health(self) -> AIProviderHealth:
        status = self.provider.status()
        return (
            AIProviderHealth.healthy
            if status.configured
            else AIProviderHealth.unavailable
        )

    def list_models(self) -> list[ModelRegistryEntry]:
        return [scaleway_model_registry_entry(self.provider)]

    def complete(self, request: AIRequest) -> AIResponse:
        prompt = _prompt_from_request(request)
        estimated_output_tokens = request.max_output_tokens or 80
        call_kwargs: dict[str, object] = {
            "prompt": prompt,
            "estimated_output_tokens": estimated_output_tokens,
        }

        if request.task_type == AITaskType.smoke_console_test:
            live_call = self.provider.create_live_console_completion
        elif request.task_type == AITaskType.smoke_test:
            live_call = self.provider.create_live_smoke_completion
        elif request.task_type in SCALEWAY_WORK_TASK_TYPES:
            if not request.model_preference or not request.model_preference.strip():
                return self._error_response(
                    request,
                    prompt=prompt,
                    estimated_output_tokens=estimated_output_tokens,
                    code=AIProviderErrorCode.provider_bad_request,
                    blocked_reason="scaleway_routed_model_missing",
                    message="Scaleway routed work requires a registry-bound model.",
                    retryable=False,
                    dispatch_state=AIExternalDispatchState.not_started,
                )
            live_call = self.provider.create_work_completion
            call_kwargs["model"] = request.model_preference.strip()
        else:
            return self._error_response(
                request,
                prompt=prompt,
                estimated_output_tokens=estimated_output_tokens,
                code=AIProviderErrorCode.provider_bad_request,
                blocked_reason="unsupported_scaleway_task_type",
                message="Scaleway adapter does not support this task type.",
                retryable=False,
                dispatch_state=AIExternalDispatchState.not_started,
            )

        try:
            result = live_call(**call_kwargs)
        except ScalewayNotConfiguredError as exc:
            return self._error_response(
                request,
                prompt=prompt,
                estimated_output_tokens=estimated_output_tokens,
                code=AIProviderErrorCode.provider_auth_missing,
                blocked_reason="scaleway_api_key_missing",
                message="Scaleway API key is missing.",
                retryable=False,
                error_type=type(exc).__name__,
                dispatch_state=AIExternalDispatchState.not_started,
            )
        except (RuntimeError, httpx.HTTPError, ValueError, TypeError) as exc:
            return self._error_response(
                request,
                prompt=prompt,
                estimated_output_tokens=estimated_output_tokens,
                code=_error_code_for_exception(exc),
                blocked_reason="scaleway_live_call_failed",
                message="Scaleway live call failed.",
                retryable=isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)),
                error_type=type(exc).__name__,
                dispatch_state=AIExternalDispatchState.unknown,
            )

        if not result.response_text.strip():
            return self._error_response(
                request,
                prompt=prompt,
                estimated_output_tokens=estimated_output_tokens,
                code=AIProviderErrorCode.provider_unknown_error,
                blocked_reason="scaleway_empty_completion",
                message="Scaleway returned an empty completion.",
                retryable=False,
                dispatch_state=AIExternalDispatchState.started,
            )

        return _response_from_scaleway_result(
            request,
            result,
            prompt=prompt,
            estimated_output_tokens=estimated_output_tokens,
        )

    def stream(self, request: AIRequest) -> object:
        raise NotImplementedError("Scaleway streaming is not implemented.")

    def _error_response(
        self,
        request: AIRequest,
        *,
        prompt: str,
        estimated_output_tokens: int,
        code: AIProviderErrorCode,
        blocked_reason: str,
        message: str,
        retryable: bool,
        dispatch_state: AIExternalDispatchState,
        error_type: str | None = None,
    ) -> AIResponse:
        model = request.model_preference or self.provider.model()
        estimated_input_tokens = estimate_tokens(prompt) if prompt else 0
        metadata: dict[str, object] = {
            "adapter_interface": SCALEWAY_ADAPTER_INTERFACE,
            "external_call_attempted": (
                dispatch_state is not AIExternalDispatchState.not_started
            ),
            "external_call_succeeded": False,
            "external_dispatch_state": dispatch_state.value,
        }
        if error_type:
            metadata["error_type"] = error_type
        return AIResponse(
            provider_id=self.provider_id,
            model_id=model,
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            text=None,
            content=None,
            usage=AIUsage(
                provider_id=self.provider_id,
                model_id=model,
                input_tokens=estimated_input_tokens,
                output_tokens=estimated_output_tokens,
                usage_source=AIUsageSource.estimated,
            ),
            safety_status="blocked",
            blocked_reason=blocked_reason,
            external_dispatch_state=dispatch_state,
            raw_provider_metadata=metadata,
            error=AIProviderError(
                code=code,
                message=message,
                retryable=retryable,
                safe_metadata={"error_type": error_type} if error_type else {},
            ),
        )


def scaleway_provider_registry_entry(
    provider: ScalewayProvider | None = None,
) -> ProviderRegistryEntry:
    scaleway = provider or ScalewayProvider()
    status = scaleway.status()
    return ProviderRegistryEntry(
        provider_id=SCALEWAY_PROVIDER_ID,
        kind=AIProviderKind.scaleway,
        display_name="Scaleway",
        status=(
            AIProviderStatus.enabled
            if status.configured
            else AIProviderStatus.missing_credentials
        ),
        health=(
            AIProviderHealth.healthy
            if status.configured
            else AIProviderHealth.unavailable
        ),
        enabled=True,
        credential_required=True,
        supports_streaming=False,
        supports_structured_output=False,
        supports_vision=False,
        locality="eu_hosted",
        notes="Provider-neutral adapter for the existing Scaleway smoke-test path.",
    )


def scaleway_model_registry_entry(
    provider: ScalewayProvider | None = None,
) -> ModelRegistryEntry:
    scaleway = provider or ScalewayProvider()
    model = scaleway.model()
    return ModelRegistryEntry(
        model_id=model,
        provider_id=SCALEWAY_PROVIDER_ID,
        provider_model_name=model,
        display_name=f"Scaleway {model}",
        enabled=True,
        capabilities={AIModelCapability.chat_text, AIModelCapability.low_latency},
        default_task_types={AITaskType.smoke_console_test, AITaskType.smoke_test},
        max_output_tokens=80,
        latency_class="low",
        reasoning_class="general",
        allowed_privacy_classes={AIPrivacyClass.public, AIPrivacyClass.internal},
        notes=(
            "Static seed for the configured Scaleway smoke model; "
            "no dynamic model discovery."
        ),
    )


def _prompt_from_request(request: AIRequest) -> str:
    if request.prompt is not None:
        return request.prompt
    return "\n".join(
        _message_to_line(message) for message in request.messages
    ).strip()


def _message_to_line(message: AIMessage) -> str:
    return f"{message.role}: {message.content}"


def _response_from_scaleway_result(
    request: AIRequest,
    result: ScalewayChatResult,
    *,
    prompt: str,
    estimated_output_tokens: int,
) -> AIResponse:
    reported_input = result.reported_input_tokens
    reported_output = result.reported_output_tokens
    input_tokens = reported_input if reported_input is not None else estimate_tokens(prompt)
    output_tokens = (
        reported_output
        if reported_output is not None
        else estimated_output_tokens
    )
    usage_source = _usage_source_for_reported_tokens(reported_input, reported_output)
    provider_cost_estimate = (
        actual_registry_cost_usd(
            provider_id=SCALEWAY_PROVIDER_ID,
            model_id=result.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        if usage_source == AIUsageSource.actual
        else None
    )
    metadata = {
        **_safe_scaleway_metadata(result.sanitized_metadata),
        "adapter_interface": SCALEWAY_ADAPTER_INTERFACE,
        "mode": result.mode,
        "external_call_attempted": result.external_call_attempted,
        "external_call_succeeded": result.external_call_succeeded,
        "external_dispatch_state": AIExternalDispatchState.started.value,
        "reported_input_tokens": reported_input,
        "reported_output_tokens": reported_output,
        "reported_total_tokens": result.reported_total_tokens,
    }
    return AIResponse(
        provider_id=SCALEWAY_PROVIDER_ID,
        model_id=result.model,
        request_id=request.request_id,
        correlation_id=request.correlation_id,
        text=result.response_text,
        content=result.response_text,
        usage=AIUsage(
            provider_id=SCALEWAY_PROVIDER_ID,
            model_id=result.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_source=usage_source,
            provider_cost_estimate=provider_cost_estimate,
            currency="USD" if provider_cost_estimate is not None else None,
        ),
        finish_reason=_optional_string(result.sanitized_metadata.get("finish_reason")),
        safety_status="allowed",
        blocked_reason=None,
        external_dispatch_state=AIExternalDispatchState.started,
        raw_provider_metadata=metadata,
        error=None,
    )


def _usage_source_for_reported_tokens(
    reported_input: int | None,
    reported_output: int | None,
) -> AIUsageSource:
    if reported_input is None and reported_output is None:
        return AIUsageSource.estimated
    if reported_input is not None and reported_output is not None:
        return AIUsageSource.actual
    return AIUsageSource.mixed


def _safe_scaleway_metadata(metadata: dict[str, object]) -> dict[str, object]:
    return {
        key: metadata[key]
        for key in SAFE_SCALEWAY_METADATA_KEYS
        if key in metadata
    }


def _error_code_for_exception(exc: Exception) -> AIProviderErrorCode:
    if isinstance(exc, httpx.TimeoutException):
        return AIProviderErrorCode.provider_timeout
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403}:
        return AIProviderErrorCode.provider_auth_failed
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return AIProviderErrorCode.provider_rate_limited
    if isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500:
        return AIProviderErrorCode.provider_bad_request
    if isinstance(exc, httpx.HTTPError):
        return AIProviderErrorCode.provider_unavailable
    return AIProviderErrorCode.provider_unknown_error


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
