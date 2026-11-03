from __future__ import annotations

from typing import Any

import httpx

from app.modules.ai.contracts import (
    AIExternalDispatchState,
    AIProviderError,
    AIProviderErrorCode,
    AIProviderHealth,
    AIRequest,
    AIResponse,
    AIUsage,
    AIUsageSource,
)
from app.modules.ai.token_guard import estimate_tokens
from app.modules.ai.usage_cost import actual_registry_cost_usd
from app.modules.secrets.storage import resolve_secret_ref

OPENAI_COMPAT_ADAPTER_INTERFACE = "provider_neutral_openai_compatible"
_SAFE_METADATA_KEYS = {"id", "object", "created", "finish_reason", "usage_returned"}


class OpenAICompatAdapter:
    """Generic non-streaming OpenAI-compatible chat-completions adapter."""

    def __init__(
        self,
        *,
        provider_id: str,
        model_id: str,
        base_url: str,
        api_key_ref: str,
        timeout_seconds: float = 20,
        client: httpx.Client | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self.api_key_ref = api_key_ref
        self.timeout_seconds = timeout_seconds
        self._client = client

    def health(self) -> AIProviderHealth:
        return (
            AIProviderHealth.healthy
            if resolve_secret_ref(self.api_key_ref).key_present
            else AIProviderHealth.unavailable
        )

    def list_models(self) -> list[Any]:
        return []

    def complete(self, request: AIRequest) -> AIResponse:
        prompt = _prompt_from_request(request)
        max_tokens = request.max_output_tokens or 512
        secret = resolve_secret_ref(self.api_key_ref)
        model = request.model_preference or self.model_id
        if not secret.value:
            return self._error_response(
                request,
                prompt=prompt,
                model=model,
                estimated_output_tokens=max_tokens,
                code=AIProviderErrorCode.provider_auth_missing,
                blocked_reason=f"{self.provider_id}_api_key_missing",
                message="Provider API key is missing.",
                retryable=False,
                dispatch_state=AIExternalDispatchState.not_started,
            )
        payload = {
            "model": model,
            "messages": _messages_from_request(request, prompt),
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
        }
        dispatch_state = AIExternalDispatchState.unknown
        try:
            response = self._post(payload=payload, api_key=secret.value)
            dispatch_state = AIExternalDispatchState.started
            response.raise_for_status()
            data = response.json()
            return self._response_from_data(
                request,
                data,
                prompt=prompt,
                model=model,
                estimated_output_tokens=max_tokens,
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                dispatch_state = AIExternalDispatchState.started
            return self._error_response(
                request,
                prompt=prompt,
                model=model,
                estimated_output_tokens=max_tokens,
                code=_error_code_for_exception(exc),
                blocked_reason=f"{self.provider_id}_live_call_failed",
                message="OpenAI-compatible provider call failed.",
                retryable=isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))
                or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code in {429, 500, 502, 503, 504}
                ),
                error_type=type(exc).__name__,
                dispatch_state=dispatch_state,
            )

    def stream(self, request: AIRequest) -> object:
        raise NotImplementedError("OpenAI-compatible streaming is not implemented.")

    def _post(self, *, payload: dict[str, Any], api_key: str) -> httpx.Response:
        if self._client is not None:
            return self._client.post(
                _chat_completions_url(self.base_url),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
        with httpx.Client() as client:
            return client.post(
                _chat_completions_url(self.base_url),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )

    def _response_from_data(
        self,
        request: AIRequest,
        data: dict[str, Any],
        *,
        prompt: str,
        model: str,
        estimated_output_tokens: int,
    ) -> AIResponse:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("missing choices")
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        if not isinstance(message, dict):
            raise ValueError("missing message")
        text = str(message.get("content") or "").strip()
        if not text:
            raise ValueError("empty content")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        prompt_tokens = _optional_int(usage.get("prompt_tokens"))
        completion_tokens = _optional_int(usage.get("completion_tokens"))
        input_tokens = prompt_tokens if prompt_tokens is not None else estimate_tokens(prompt)
        output_tokens = (
            completion_tokens
            if completion_tokens is not None
            else estimated_output_tokens
        )
        usage_source = (
            AIUsageSource.actual
            if prompt_tokens is not None and completion_tokens is not None
            else AIUsageSource.estimated
        )
        provider_cost_estimate = (
            actual_registry_cost_usd(
                provider_id=self.provider_id,
                model_id=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            if usage_source == AIUsageSource.actual
            else None
        )
        return AIResponse(
            provider_id=self.provider_id,
            model_id=model,
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            text=text,
            content=text,
            usage=AIUsage(
                provider_id=self.provider_id,
                model_id=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usage_source=usage_source,
                provider_cost_estimate=provider_cost_estimate,
                currency="USD" if provider_cost_estimate is not None else None,
            ),
            finish_reason=(
                str(first.get("finish_reason"))
                if first.get("finish_reason") is not None
                else None
            ),
            safety_status="allowed",
            external_dispatch_state=AIExternalDispatchState.started,
            raw_provider_metadata={
                "adapter_interface": OPENAI_COMPAT_ADAPTER_INTERFACE,
                "external_call_attempted": True,
                "external_call_succeeded": True,
                "external_dispatch_state": AIExternalDispatchState.started.value,
                "usage_returned": bool(usage),
                **{key: data[key] for key in _SAFE_METADATA_KEYS if key in data},
            },
        )

    def _error_response(
        self,
        request: AIRequest,
        *,
        prompt: str,
        model: str,
        estimated_output_tokens: int,
        code: AIProviderErrorCode,
        blocked_reason: str,
        message: str,
        retryable: bool,
        dispatch_state: AIExternalDispatchState,
        error_type: str | None = None,
    ) -> AIResponse:
        metadata: dict[str, object] = {
            "adapter_interface": OPENAI_COMPAT_ADAPTER_INTERFACE,
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
            usage=AIUsage(
                provider_id=self.provider_id,
                model_id=model,
                input_tokens=estimate_tokens(prompt) if prompt else 0,
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


def _chat_completions_url(base_url: str) -> str:
    return (
        base_url
        if base_url.endswith("/chat/completions")
        else f"{base_url}/chat/completions"
    )


def _prompt_from_request(request: AIRequest) -> str:
    if request.prompt is not None:
        return request.prompt
    return "\n".join(
        f"{message.role}: {message.content}" for message in request.messages
    ).strip()


def _messages_from_request(request: AIRequest, prompt: str) -> list[dict[str, str]]:
    if request.messages:
        return [
            {"role": message.role, "content": message.content}
            for message in request.messages
        ]
    return [
        {
            "role": "system",
            "content": (
                "You are a precise engineering assistant. "
                "Answer concisely and usefully."
            ),
        },
        {"role": "user", "content": prompt},
    ]


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
    return AIProviderErrorCode.provider_response_invalid
