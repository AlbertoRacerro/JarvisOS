import httpx

from app.modules.ai.contracts import (
    AIExternalDispatchState,
    AIProviderErrorCode,
    AIRequest,
    AITaskType,
)
from app.modules.ai.providers.openai_compat_adapter import OpenAICompatAdapter


class _Client:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def post(self, url, *, headers, json, timeout):
        self.requests.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return self.handler(url, headers, json, timeout)


def test_openai_compat_adapter_success_parses_usage_and_sanitizes_metadata(
    monkeypatch,
):
    monkeypatch.setenv("GLM_API_KEY", "secret-value")

    def handler(url, headers, payload, timeout):
        assert headers["Authorization"] == "Bearer secret-value"
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "id": "safe-id",
                "choices": [
                    {"message": {"content": "hello"}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 4,
                    "total_tokens": 7,
                },
            },
        )

    client = _Client(handler)
    adapter = OpenAICompatAdapter(
        provider_id="glm",
        model_id="glm-model",
        base_url="https://example.test/v1",
        api_key_ref="env:GLM_API_KEY",
        client=client,
    )
    response = adapter.complete(
        AIRequest(task_type=AITaskType.synthesis, prompt="hi", max_output_tokens=9)
    )

    assert response.error is None
    assert response.text == "hello"
    assert response.usage.input_tokens == 3
    assert response.usage.output_tokens == 4
    assert response.external_dispatch_state == AIExternalDispatchState.started
    assert response.raw_provider_metadata["external_dispatch_state"] == "started"
    assert "secret-value" not in str(response.raw_provider_metadata)
    assert "secret-value" not in str(response.model_dump())


def test_openai_compat_adapter_missing_secret_does_not_call_client(monkeypatch):
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    client = _Client(lambda *_: (_ for _ in ()).throw(AssertionError("should not call")))
    adapter = OpenAICompatAdapter(
        provider_id="kimi",
        model_id="kimi-model",
        base_url="https://example.test/v1",
        api_key_ref="env:KIMI_API_KEY",
        client=client,
    )

    response = adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="hi"))

    assert response.error.code == AIProviderErrorCode.provider_auth_missing
    assert response.external_dispatch_state == AIExternalDispatchState.not_started
    assert response.raw_provider_metadata["external_call_attempted"] is False
    assert response.raw_provider_metadata["external_dispatch_state"] == "not_started"
    assert client.requests == []


def test_openai_compat_adapter_http_error_has_started_dispatch(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-value")

    def handler(url, headers, payload, timeout):
        request = httpx.Request("POST", url)
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError(
            "rate limited", request=request, response=response
        )

    adapter = OpenAICompatAdapter(
        provider_id="deepseek",
        model_id="deepseek-chat",
        base_url="https://example.test/v1",
        api_key_ref="env:DEEPSEEK_API_KEY",
        client=_Client(handler),
    )

    response = adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="hi"))

    assert response.error.code == AIProviderErrorCode.provider_rate_limited
    assert response.error.retryable is True
    assert response.external_dispatch_state == AIExternalDispatchState.started
    assert "secret-value" not in str(response.model_dump())


def test_openai_compat_adapter_malformed_response_keeps_started_dispatch(monkeypatch):
    monkeypatch.setenv("SCALEWAY_API_KEY", "secret-value")
    adapter = OpenAICompatAdapter(
        provider_id="scaleway",
        model_id="m",
        base_url="https://example.test/v1",
        api_key_ref="env:SCALEWAY_API_KEY",
        client=_Client(
            lambda url, *_: httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"choices": []},
            )
        ),
    )

    response = adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="hi"))

    assert response.error.code == AIProviderErrorCode.provider_response_invalid
    assert response.external_dispatch_state == AIExternalDispatchState.started


def test_openai_compat_adapter_timeout_is_unknown_dispatch(monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "secret-value")

    def handler(url, headers, payload, timeout):
        request = httpx.Request("POST", url)
        raise httpx.TimeoutException("timed out", request=request)

    adapter = OpenAICompatAdapter(
        provider_id="glm",
        model_id="glm-5.2",
        base_url="https://example.test/v1",
        api_key_ref="env:GLM_API_KEY",
        client=_Client(handler),
    )

    response = adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="hi"))

    assert response.error.code == AIProviderErrorCode.provider_timeout
    assert response.error.retryable is True
    assert response.external_dispatch_state == AIExternalDispatchState.unknown
    assert response.raw_provider_metadata["external_dispatch_state"] == "unknown"
