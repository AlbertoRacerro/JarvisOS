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


def _enable_deepseek_provider(client: TestClient, **overrides: object) -> None:
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


def _fail_if_deepseek_provider_called(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.ai.providers.deepseek import DeepSeekProvider

    def fail(self: DeepSeekProvider, *, prompt: str, estimated_output_tokens: int) -> object:
        raise AssertionError("Legacy DeepSeek provider should not have been called.")

    monkeypatch.setattr(DeepSeekProvider, "create_live_console_completion", fail)


def _mock_canonical_deepseek_success(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    from app.modules.ai import deepseek_provider_smoke
    from app.modules.ai.contracts import AIResponse, AIUsage, AIUsageSource, RoutingDecision
    from app.modules.ai.execution import AiTaskOutcome

    captured: dict[str, object] = {}

    def fake_run_ai_task(**kwargs: object) -> AiTaskOutcome:
        captured.update(kwargs)
        response = AIResponse(
            provider_id="deepseek",
            model_id="mock-deepseek-model",
            usage=AIUsage(
                provider_id="deepseek",
                model_id="mock-deepseek-model",
                input_tokens=10,
                output_tokens=8,
                usage_source=AIUsageSource.actual,
            ),
            request_id="provider-smoke-request",
            text="A mass balance compares inputs, outputs, and accumulation.",
            content="A mass balance compares inputs, outputs, and accumulation.",
            finish_reason="stop",
            raw_provider_metadata={
                "external_call_attempted": True,
                "implementation": "mock-canonical",
                "usage_returned": True,
                "reported_input_tokens": 10,
                "reported_output_tokens": 8,
            },
        )
        return AiTaskOutcome(
            status="success",
            ledger_id="provider-smoke-ledger",
            selected_route_class="external:deepseek",
            decision=RoutingDecision(
                provider_id="deepseek",
                model_id="deepseek-v4-pro",
                decision_reason="bound:external:deepseek",
            ),
            response=response,
        )

    monkeypatch.setattr(deepseek_provider_smoke, "run_ai_task", fake_run_ai_task)
    return captured


def test_provider_smoke_default_state_blocks_without_provider_call(client: TestClient, monkeypatch) -> None:
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post("/ai/provider-smoke/run", json={"prompt": "Explain what a mass balance is."})

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "deepseek"
    assert body["blocked_reason"] == "deepseek_provider_mode_required"
    assert body["external_call_attempted"] is False


def test_provider_smoke_rejects_provider_or_model_override_fields(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_provider(client)
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post(
        "/ai/provider-smoke/run",
        json={
            "prompt": "Explain what a mass balance is.",
            "provider_id": "scaleway",
            "model_id": "fake-user-model",
        },
    )

    assert response.status_code == 422


def test_provider_smoke_missing_key_fails_closed_through_canonical_gate(client: TestClient, monkeypatch) -> None:
    _enable_deepseek_provider(client)
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post("/ai/provider-smoke/run", json={"prompt": "Explain what a mass balance is."})

    assert response.status_code == 200
    body = response.json()
    assert body["blocked_reason"] == "provider_gate_blocked"
    assert body["external_call_attempted"] is False


def test_provider_smoke_paid_ai_disabled_fails_closed_through_canonical_gate(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_provider(client, paid_ai_enabled=False)
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post("/ai/provider-smoke/run", json={"prompt": "Explain mass balance."})

    assert response.status_code == 200
    assert response.json()["blocked_reason"] == "provider_gate_blocked"
    assert response.json()["external_call_attempted"] is False


def test_provider_smoke_fast_dev_maps_success_from_canonical_execution(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_key = "ds-test-secret-1234abcd"
    monkeypatch.setenv("DEEPSEEK_API_KEY", raw_key)
    _enable_deepseek_provider(client)
    captured = _mock_canonical_deepseek_success(monkeypatch)

    prompt = "Explain what a mass balance is in one paragraph."
    response = client.post("/ai/provider-smoke/run", json={"prompt": prompt})

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "deepseek"
    assert body["model"] == "mock-deepseek-model"
    assert body["privacy_class"] == "internal"
    assert body["blocked_reason"] is None
    assert body["external_call_attempted"] is True
    assert body["external_call_succeeded"] is True
    assert body["actual_input_tokens"] == 10
    assert body["actual_output_tokens"] == 8
    assert body["usage_source"] == "actual"
    assert captured["route_class"] == "external:deepseek"
    assert captured["task_kind"] == "test"
    assert captured["max_output_tokens"] == 120
    assert captured["user_prompt"] == prompt
    body_text = json.dumps(body)
    assert raw_key not in body_text
    assert "Authorization" not in body_text

    event_payload = _latest_event_payload("AIProviderSmokeCompleted")
    event_text = json.dumps(event_payload)
    assert event_payload["provider"] == "deepseek"
    assert event_payload["policy_mode"] == "FAST_DEV"
    assert event_payload["external_call_attempted"] is True
    assert raw_key not in event_text
    assert "mass balance" not in event_text


def test_provider_smoke_generic_public_physics_uses_same_canonical_deepseek_route(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_provider(client)
    captured = _mock_canonical_deepseek_success(monkeypatch)

    response = client.post(
        "/ai/provider-smoke/run",
        json={"prompt": "Review this toy equation for early Zephyr public physics: X = X0 exp(mu t)."},
    )

    assert response.status_code == 200
    assert response.json()["blocked_reason"] is None
    assert response.json()["external_call_attempted"] is True
    assert captured["route_class"] == "external:deepseek"


def test_provider_smoke_unstubbed_external_request_requires_canonical_confirmation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_provider(client)
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post("/ai/provider-smoke/run", json={"prompt": "Explain mass balance."})

    assert response.status_code == 200
    body = response.json()
    assert body["blocked_reason"] == "confirmation_required"
    assert body["external_call_attempted"] is False


def test_provider_specific_deepseek_route_has_no_cross_provider_fallback() -> None:
    from app.modules.ai.execution import resolve_binding
    from app.modules.ai.provider_registry import load_default_provider_registry

    binding, decision = resolve_binding("external:deepseek")
    assert binding is not None
    assert decision.blocked is False
    assert binding.provider_id == "deepseek"
    assert binding.model_id == "deepseek-v4-pro"

    registry = load_default_provider_registry()
    assert registry.fallback_chains.get("external:deepseek") is None
    cheap_chain = registry.fallback_chains["external:cheap"]
    assert [(entry.provider_id, entry.model_id) for entry in cheap_chain] == [
        ("deepseek", "deepseek-v4-pro"),
        ("glm", "glm-5.2"),
    ]


def test_provider_smoke_structural_secret_blocks_before_provider(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_provider(client)
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post(
        "/ai/provider-smoke/run",
        json={"prompt": "Authorization: Bearer ds-test-secret-1234abcd"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["privacy_class"] == "secret"
    assert body["blocked_reason"] == "privacy_policy_secret_blocked"
    assert body["external_call_attempted"] is False

    event_text = json.dumps(_latest_event_payload("AIProviderSmokeBlocked"))
    assert "ds-test-secret-1234abcd" not in event_text
    assert "Authorization" not in event_text


def test_provider_smoke_output_cap_blocks_before_provider(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_provider(client)
    _fail_if_deepseek_provider_called(monkeypatch)

    response = client.post(
        "/ai/provider-smoke/run",
        json={"prompt": "Explain mass balance.", "max_output_tokens": 500},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["blocked_reason"] == "provider_smoke_max_output_tokens_exceeded"
    assert body["external_call_attempted"] is False


def test_ai_status_reports_deepseek_provider_neutral_fields(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test-secret-1234abcd")
    _enable_deepseek_provider(client)

    response = client.get("/ai/status")

    assert response.status_code == 200
    status = response.json()
    assert status["provider_mode"] == "deepseek"
    assert status["provider_id"] == "deepseek"
    assert status["adapter_enabled"] is True
    assert status["credential_status"] == "present"
    assert status["budget_status"] == "available"
    assert status["external_calls_allowed"] is True
    assert "ds-test-secret-1234abcd" not in json.dumps(status)
