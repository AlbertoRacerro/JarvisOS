import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.modules.ai.contracts import AIPolicyMode


@pytest.fixture(autouse=True)
def _fast_dev_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """These flows exercise the opt-in FAST_DEV prompt default; the product default is STRICT_IP."""
    monkeypatch.setattr("app.modules.ai.settings.DEFAULT_POLICY_MODE", AIPolicyMode.FAST_DEV)


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    monkeypatch.delenv("SCALEWAY_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)

    with TestClient(create_app()) as test_client:
        yield test_client

    get_settings.cache_clear()


def _latest_event_payload(event_type: str) -> dict[str, object]:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import list_events_by_type

    with open_sqlite_connection() as connection:
        rows = list_events_by_type(connection, event_type)
    assert rows
    return json.loads(rows[-1]["payload"])


def _event_payloads(event_type: str) -> list[dict[str, object]]:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import list_events_by_type

    with open_sqlite_connection() as connection:
        rows = list_events_by_type(connection, event_type)
    return [json.loads(row["payload"]) for row in rows]


def _enable_deepseek_supervisor(client: TestClient, **overrides: object) -> None:
    payload: dict[str, object] = {
        "provider_mode": "deepseek",
        "default_ai_provider": "deepseek",
        "paid_ai_enabled": True,
        "monthly_api_budget_usd": 1,
        "use_fake_provider_when_budget_zero": False,
    }
    payload.update(overrides)
    response = client.put("/ai/settings", json=payload)
    assert response.status_code == 200


def _enable_scaleway_supervisor(client: TestClient, **overrides: object) -> None:
    payload: dict[str, object] = {
        "provider_mode": "scaleway",
        "default_ai_provider": "scaleway",
        "paid_ai_enabled": True,
        "monthly_api_budget_usd": 1,
        "scaleway_enabled": True,
        "scaleway_smoke_test_enabled": True,
        "scaleway_live_smoke_test_enabled": True,
        "scaleway_monthly_token_cap": 500000,
        "scaleway_hard_stop_token_cap": 800000,
        "scaleway_input_tokens_month_to_date": 0,
        "scaleway_output_tokens_month_to_date": 0,
        "use_fake_provider_when_budget_zero": False,
    }
    payload.update(overrides)
    response = client.put("/ai/settings", json=payload)
    assert response.status_code == 200


def _fail_if_deepseek_provider_called(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.ai.providers.deepseek import DeepSeekProvider

    def fail(self: DeepSeekProvider, *, prompt: str, estimated_output_tokens: int) -> object:
        raise AssertionError("Legacy DeepSeek provider should not have been called.")

    monkeypatch.setattr(DeepSeekProvider, "create_live_console_completion", fail)


def _mock_canonical_success(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider_id: str = "deepseek",
    model_id: str = "mock-deepseek-model",
    input_tokens: int = 12,
    output_tokens: int = 9,
    response_text: str = "The exponential equation assumes constant growth rate and unlimited resources.",
) -> dict[str, object]:
    from app.modules.ai import supervisor_public_test
    from app.modules.ai.contracts import AIResponse, AIUsage, AIUsageSource, RoutingDecision
    from app.modules.ai.execution import AiTaskOutcome

    captured: dict[str, object] = {}

    def fake_run_ai_task(**kwargs: object) -> AiTaskOutcome:
        captured.update(kwargs)
        response = AIResponse(
            provider_id=provider_id,
            model_id=model_id,
            usage=AIUsage(
                provider_id=provider_id,
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usage_source=AIUsageSource.actual,
            ),
            request_id="canonical-supervisor-request",
            correlation_id="canonical-supervisor-correlation",
            text=response_text,
            content=response_text,
            finish_reason="stop",
            raw_provider_metadata={
                "external_call_attempted": True,
                "implementation": "mock-canonical",
                "usage_returned": True,
            },
        )
        return AiTaskOutcome(
            status="success",
            ledger_id="canonical-supervisor-ledger",
            selected_route_class=str(kwargs["route_class"]),
            decision=RoutingDecision(
                provider_id=provider_id,
                model_id=model_id,
                decision_reason=f"bound:{kwargs['route_class']}",
            ),
            response=response,
        )

    monkeypatch.setattr(supervisor_public_test, "run_ai_task", fake_run_ai_task)
    return captured


def _mock_canonical_provider_error(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    from app.modules.ai import supervisor_public_test
    from app.modules.ai.contracts import (
        AIProviderError,
        AIProviderErrorCode,
        AIResponse,
        AIUsage,
        AIUsageSource,
        RoutingDecision,
    )
    from app.modules.ai.execution import AiTaskOutcome

    captured: dict[str, object] = {}

    def fake_run_ai_task(**kwargs: object) -> AiTaskOutcome:
        captured.update(kwargs)
        response = AIResponse(
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            usage=AIUsage(
                provider_id="deepseek",
                model_id="deepseek-v4-pro",
                input_tokens=0,
                output_tokens=0,
                usage_source=AIUsageSource.estimated,
            ),
            request_id="canonical-supervisor-error-request",
            text=None,
            error=AIProviderError(
                code=AIProviderErrorCode.provider_timeout,
                message="provider timeout",
                retryable=True,
            ),
            raw_provider_metadata={"external_call_attempted": True},
        )
        return AiTaskOutcome(
            status="provider_error",
            ledger_id="canonical-supervisor-error-ledger",
            selected_route_class="external:deepseek",
            decision=RoutingDecision(
                provider_id="deepseek",
                model_id="deepseek-v4-pro",
                decision_reason="bound:external:deepseek",
            ),
            response=response,
            error_type="provider_timeout",
        )

    monkeypatch.setattr(supervisor_public_test, "run_ai_task", fake_run_ai_task)
    return captured


def test_supervisor_rejects_empty_prompt_before_provider(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_supervisor(client)
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post("/ai/supervisor/public-test", json={"prompt": "   "})

    assert response.status_code == 200
    body = response.json()
    assert body["blocked_reason"] == "supervisor_prompt_empty"
    assert body["external_call_attempted"] is False


def test_supervisor_bounds_prompt_length_before_provider(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_supervisor(client)
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post("/ai/supervisor/public-test", json={"prompt": "x" * 2001})

    assert response.status_code == 200
    assert response.json()["blocked_reason"] == "supervisor_prompt_too_long"
    assert response.json()["external_call_attempted"] is False


def test_supervisor_fast_dev_maps_deepseek_success_from_canonical_execution(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_key = "ds-test-secret-1234abcd"
    monkeypatch.setenv("DEEPSEEK_API_KEY", raw_key)
    _enable_deepseek_supervisor(client)
    captured = _mock_canonical_success(monkeypatch)

    response = client.post(
        "/ai/supervisor/public-test",
        json={
            "prompt": "Review this toy equation and identify obvious modeling limitations: X = X0 exp(mu t)",
            "task_type": "equation_review",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"]
    assert body["task_type"] == "equation_review"
    assert body["policy_mode"] == "FAST_DEV"
    assert body["provider_id"] == "deepseek"
    assert body["model_id"] == "mock-deepseek-model"
    assert body["usage"]["input_tokens"] == 12
    assert body["usage"]["output_tokens"] == 9
    assert body["usage"]["usage_source"] == "actual"
    assert body["safety_status"] == "allowed"
    assert body["blocked_reason"] is None
    assert body["external_call_attempted"] is True
    assert body["external_call_succeeded"] is True
    assert body["event_id"]
    assert body["request_id"]
    assert body["correlation_id"]
    assert body["limitations"]
    assert captured["route_class"] == "external:deepseek"
    assert captured["task_kind"] == "test"
    assert captured["max_output_tokens"] == 180
    assert "Task type: equation_review" in str(captured["user_prompt"])
    assert raw_key not in json.dumps(body)
    assert "Authorization" not in json.dumps(body)

    completed_payload = _latest_event_payload("AISupervisorPublicTestCompleted")
    payload_text = json.dumps(completed_payload)
    assert completed_payload["provider_id"] == "deepseek"
    assert completed_payload["usage"]["total_tokens"] == 21
    assert raw_key not in payload_text
    assert "X0 exp" not in payload_text


def test_supervisor_request_cannot_force_provider_or_model(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_supervisor(client)
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post(
        "/ai/supervisor/public-test",
        json={
            "prompt": "Summarize what an AI provider adapter does.",
            "provider_id": "scaleway",
            "model_id": "fake-user-model",
        },
    )

    assert response.status_code == 422


def test_supervisor_metadata_cannot_force_provider_or_model(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_supervisor(client)
    captured = _mock_canonical_success(monkeypatch)

    response = client.post(
        "/ai/supervisor/public-test",
        json={
            "prompt": "Summarize what an AI provider adapter does.",
            "metadata": {
                "provider_id": "scaleway",
                "model_id": "fake-user-model",
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider_id"] == "deepseek"
    assert body["model_id"] == "mock-deepseek-model"
    assert captured["route_class"] == "external:deepseek"


def test_supervisor_returns_provider_unavailable_when_no_provider_configured(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post(
        "/ai/supervisor/public-test",
        json={"prompt": "Explain what a mass balance is."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["blocked_reason"] == "provider_unavailable"
    assert body["provider_id"] is None
    assert body["external_call_attempted"] is False


def test_supervisor_blocks_structural_secret_without_provider_call(client: TestClient, monkeypatch) -> None:
    raw_key = "ds-test-secret-1234abcd"
    monkeypatch.setenv("DEEPSEEK_API_KEY", raw_key)
    _enable_deepseek_supervisor(client)
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post(
        "/ai/supervisor/public-test",
        json={"prompt": f"Authorization: Bearer {raw_key}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["blocked_reason"] == "privacy_policy_secret_blocked"
    assert body["external_call_attempted"] is False
    payload_text = json.dumps(_latest_event_payload("AISupervisorPublicTestBlocked"))
    assert raw_key not in payload_text
    assert "Authorization" not in payload_text


def test_supervisor_maps_canonical_provider_error_without_legacy_adapter_bypass(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_supervisor(client)
    captured = _mock_canonical_provider_error(monkeypatch)

    response = client.post(
        "/ai/supervisor/public-test",
        json={"prompt": "Explain what a mass balance is."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider_id"] == "deepseek"
    assert body["blocked_reason"] == "provider_timeout"
    assert body["safety_status"] == "blocked"
    assert body["external_call_attempted"] is True
    assert body["external_call_succeeded"] is False
    assert captured["route_class"] == "external:deepseek"
    failed_payload = _latest_event_payload("AISupervisorPublicTestProviderFailed")
    assert failed_payload["error_code"] == "provider_timeout"


def test_supervisor_scaleway_mode_maps_only_to_canonical_scaleway_route(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-scaleway-key")
    _enable_scaleway_supervisor(client)
    captured = _mock_canonical_success(
        monkeypatch,
        provider_id="scaleway",
        model_id="mock-scaleway-model",
        input_tokens=7,
        output_tokens=5,
        response_text="The runner error suggests checking input parameters.",
    )

    response = client.post(
        "/ai/supervisor/public-test",
        json={"prompt": "Explain this generic runner error: invalid dt", "task_type": "runner_error_explanation"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider_id"] == "scaleway"
    assert body["model_id"] == "mock-scaleway-model"
    assert body["usage"]["input_tokens"] == 7
    assert body["usage"]["output_tokens"] == 5
    assert captured["route_class"] == "external:scaleway"

    # 129 removes the Supervisor-owned accounting path. A stubbed canonical
    # executor must not cause the wrapper to mutate the legacy counters itself.
    settings = client.get("/ai/settings").json()
    assert settings["scaleway_input_tokens_month_to_date"] == 0
    assert settings["scaleway_output_tokens_month_to_date"] == 0


def test_supervisor_unstubbed_external_request_requires_canonical_confirmation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_supervisor(client)
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post(
        "/ai/supervisor/public-test",
        json={"prompt": "Explain what a mass balance is."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["blocked_reason"] == "confirmation_required"
    assert body["external_call_attempted"] is False


def test_supervisor_non_fast_dev_policy_blocks_without_provider(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_supervisor(client, policy_mode="STRICT_IP")
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post(
        "/ai/supervisor/public-test",
        json={"prompt": "Explain what a mass balance is."},
    )

    assert response.status_code == 200
    assert response.json()["blocked_reason"] == "supervisor_public_test_requires_fast_dev_policy"


def test_supervisor_rejects_file_path_prompt_before_provider(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_supervisor(client)
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post(
        "/ai/supervisor/public-test",
        json={"prompt": "Read C:\\secret\\model.py and summarize it."},
    )

    assert response.status_code == 200
    assert response.json()["blocked_reason"] == "supervisor_file_paths_not_supported"


def test_supervisor_events_do_not_store_prompt_text(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_supervisor(client)
    _mock_canonical_success(monkeypatch)

    prompt = "Summarize what an AI provider adapter does."
    response = client.post("/ai/supervisor/public-test", json={"prompt": prompt})

    assert response.status_code == 200
    event_text = json.dumps(
        _event_payloads("AISupervisorPublicTestStarted")
        + _event_payloads("AISupervisorPublicTestProviderSelected")
        + _event_payloads("AISupervisorPublicTestCompleted")
    )
    assert prompt not in event_text
    assert "prompt_length" in event_text
