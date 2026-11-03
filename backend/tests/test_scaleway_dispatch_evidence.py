from __future__ import annotations

import httpx

from app.modules.ai.contracts import (
    AIExternalDispatchState,
    AIProviderErrorCode,
    AIRequest,
    AITaskType,
)
from app.modules.ai.providers.scaleway import (
    ScalewayChatResult,
    ScalewayNotConfiguredError,
    ScalewayProviderStatus,
)
from app.modules.ai.providers.scaleway_adapter import ScalewayProviderAdapter

MODEL_ID = "gemma-4-26b-a4b-it"


class _Provider:
    def __init__(self, *, configured: bool, result=None, error: Exception | None = None):
        self.configured = configured
        self.result = result
        self.error = error
        self.calls = 0
        self.models: list[str] = []

    def status(self) -> ScalewayProviderStatus:
        return ScalewayProviderStatus(
            provider="scaleway",
            configured=self.configured,
            base_url="https://example.test/v1",
            model="legacy-smoke-model",
            implementation="test",
        )

    def model(self) -> str:
        return "legacy-smoke-model"

    def create_work_completion(
        self,
        *,
        prompt: str,
        estimated_output_tokens: int,
        model: str,
    ):
        self.calls += 1
        self.models.append(model)
        if not self.configured:
            raise ScalewayNotConfiguredError("missing before transport")
        if self.error is not None:
            raise self.error
        return self.result

    def create_live_console_completion(
        self,
        *,
        prompt: str,
        estimated_output_tokens: int,
    ):
        return self.create_work_completion(
            prompt=prompt,
            estimated_output_tokens=estimated_output_tokens,
            model=self.model(),
        )

    create_live_smoke_completion = create_live_console_completion


def _request(task_type: AITaskType = AITaskType.synthesis) -> AIRequest:
    return AIRequest(
        task_type=task_type,
        prompt="harmless test",
        model_preference=MODEL_ID,
        max_output_tokens=16,
    )


def test_scaleway_missing_credentials_is_not_started() -> None:
    provider = _Provider(configured=False)
    response = ScalewayProviderAdapter(provider=provider).complete(_request())

    assert response.error.code == AIProviderErrorCode.provider_auth_missing
    assert response.external_dispatch_state == AIExternalDispatchState.not_started
    assert response.raw_provider_metadata["external_dispatch_state"] == "not_started"
    assert provider.calls == 1
    assert provider.models == [MODEL_ID]


def test_scaleway_unsupported_task_is_not_started() -> None:
    provider = _Provider(configured=True)
    response = ScalewayProviderAdapter(provider=provider).complete(
        _request(AITaskType.assumption_review)
    )

    assert response.error.code == AIProviderErrorCode.provider_bad_request
    assert response.external_dispatch_state == AIExternalDispatchState.not_started
    assert provider.calls == 0


def test_scaleway_success_is_started() -> None:
    provider = _Provider(
        configured=True,
        result=ScalewayChatResult(
            provider_name="scaleway",
            model=MODEL_ID,
            mode="work",
            external_call_attempted=True,
            external_call_succeeded=True,
            response_text="done",
            reported_input_tokens=3,
            reported_output_tokens=2,
            reported_total_tokens=5,
            sanitized_metadata={"finish_reason": "stop"},
        ),
    )
    response = ScalewayProviderAdapter(provider=provider).complete(_request())

    assert response.error is None
    assert response.model_id == MODEL_ID
    assert response.external_dispatch_state == AIExternalDispatchState.started
    assert response.raw_provider_metadata["external_dispatch_state"] == "started"
    assert provider.calls == 1
    assert provider.models == [MODEL_ID]


def test_scaleway_transport_exception_is_unknown() -> None:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    provider = _Provider(
        configured=True,
        error=httpx.TimeoutException("timeout", request=request),
    )
    response = ScalewayProviderAdapter(provider=provider).complete(_request())

    assert response.error.code == AIProviderErrorCode.provider_timeout
    assert response.external_dispatch_state == AIExternalDispatchState.unknown
    assert response.raw_provider_metadata["external_dispatch_state"] == "unknown"
    assert provider.calls == 1
    assert provider.models == [MODEL_ID]
