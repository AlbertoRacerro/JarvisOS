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
    monkeypatch.delenv("SCALEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("SCALEWAY_MODEL", raising=False)

    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)

    with TestClient(create_app()) as test_client:
        yield test_client

    get_settings.cache_clear()


def _event_count(event_type: str) -> int:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import count_events_by_type

    with open_sqlite_connection() as connection:
        return count_events_by_type(connection, event_type)


def _latest_event_payload(event_type: str) -> dict[str, object]:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import list_events_by_type

    with open_sqlite_connection() as connection:
        rows = list_events_by_type(connection, event_type)
    assert rows
    return json.loads(rows[-1]["payload"])


def _enable_scaleway_smoke(client: TestClient, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider_mode": "scaleway",
        "default_ai_provider": "scaleway",
        "paid_ai_enabled": True,
        "monthly_api_budget_usd": 1,
        "scaleway_enabled": True,
        "scaleway_smoke_test_enabled": True,
        "scaleway_live_smoke_test_enabled": False,
        "scaleway_monthly_token_cap": 500000,
        "scaleway_hard_stop_token_cap": 800000,
        "use_fake_provider_when_budget_zero": False,
    }
    payload.update(overrides)
    response = client.put("/ai/settings", json=payload)
    assert response.status_code == 200
    return response.json()


def test_scaleway_smoke_defaults_are_disabled(client: TestClient) -> None:
    response = client.get("/ai/status")

    assert response.status_code == 200
    status = response.json()
    assert status["active_provider_mode"] == "fake"
    assert status["policy_mode"] == "FAST_DEV"
    assert status["ai_enabled"] is True
    assert status["provider_mode"] == "fake"
    assert status["provider_id"] == "fake"
    assert status["adapter_enabled"] is True
    assert status["budget_status"] == "monthly_budget_exhausted"
    assert status["credential_status"] == "not_required"
    assert status["scaleway_enabled"] is False
    assert status["scaleway_smoke_test_enabled"] is False
    assert status["scaleway_live_smoke_test_enabled"] is False
    assert status["scaleway_api_key_configured"] is False
    assert status["scaleway_provider_implementation"] == "stub_no_external_calls"
    assert status["scaleway_monthly_token_cap"] == 500000
    assert status["scaleway_hard_stop_token_cap"] == 800000
    assert status["scaleway_free_tier_reference_tokens"] == 1000000
    assert status["scaleway_input_tokens_month_to_date"] == 0
    assert status["scaleway_output_tokens_month_to_date"] == 0
    assert status["usage_total_tokens"] == 0
    assert status["external_calls_allowed"] is False
    assert "scaleway_token_cap" not in status
    assert "scaleway_tokens_month_to_date" not in status

    settings = client.get("/ai/settings").json()
    assert settings["policy_mode"] == "FAST_DEV"
    assert settings["usage_total_tokens"] == 0
    assert "scaleway_token_cap" not in settings
    assert "scaleway_tokens_month_to_date" not in settings


def test_fake_smoke_endpoint_returns_deterministic_structured_results(client: TestClient) -> None:
    response = client.post("/ai/smoke-tests/run", json={"provider_mode": "fake"})

    assert response.status_code == 200
    body = response.json()
    assert body["provider_mode"] == "fake"
    assert body["smoke_mode"] == "synthetic"
    assert body["external_call_attempted"] is False
    assert body["external_call_succeeded"] is False
    assert len(body["results"]) == 5
    classifications = {result["case_id"]: result["fake_classification"] for result in body["results"]}
    assert classifications["public_research_request"] == "public"
    assert classifications["generic_engineering_note"] == "internal"
    assert classifications["quick_coupler_geometry"] == "sensitive_ip"
    assert classifications["api_key_example"] == "secret"
    assert classifications["ambiguous_project_brainstorming"] == "confidential"
    assert all(result["passed"] for result in body["results"])
    assert all(result["blocking_reason"] is None for result in body["results"])
    assert _event_count("AISmokeTestStarted") == 1
    assert _event_count("AISmokeTestCompleted") == 1
    payload = _latest_event_payload("AISmokeTestCompleted")
    assert payload["provider"] == "fake"
    assert payload["model"] == "fake-modeling-draft-v1"
    assert payload["external_call_attempted"] is False
    assert payload["external_call_succeeded"] is False
    assert payload["synthetic_only"] is True
    assert len(payload["results"]) == 5
    assert payload["results"][0]["provider"] == "fake"
    assert payload["results"][0]["privacy_class"] in {"public", "internal", "confidential", "sensitive_ip", "secret", "unknown"}
    assert "estimated_input_tokens" in payload["results"][0]
    assert "API key" not in json.dumps(payload)
    assert "password" not in json.dumps(payload)


def test_missing_scaleway_api_key_blocks_smoke_tests_clearly(client: TestClient) -> None:
    _enable_scaleway_smoke(client)

    status_response = client.get("/ai/status")
    assert status_response.status_code == 200
    status = status_response.json()
    assert status["scaleway_api_key_configured"] is False
    assert status["scaleway_provider_implementation"] == "stub_no_external_calls"
    assert status["external_calls_allowed"] is False
    assert status["blocking_reason"] == "scaleway_api_key_missing"

    response = client.post("/ai/smoke-tests/run", json={"provider_mode": "scaleway"})

    assert response.status_code == 200
    body = response.json()
    assert body["external_call_attempted"] is False
    assert {result["blocking_reason"] for result in body["results"]} == {"scaleway_api_key_missing"}
    assert _event_count("AISmokeTestStarted") == 1
    assert _event_count("AISmokeTestBlocked") == 1
    payload = _latest_event_payload("AISmokeTestBlocked")
    assert payload["provider"] == "scaleway"
    assert payload["model"] == "llama-3.1-8b-instruct"
    assert payload["external_call_attempted"] is False
    assert payload["external_call_succeeded"] is False
    assert payload["results"][0]["blocked_reason"] == "scaleway_api_key_missing"


def test_token_cap_blocks_over_cap_smoke_case(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-key")
    _enable_scaleway_smoke(client, scaleway_monthly_token_cap=1)
    _fail_if_live_adapter_called(monkeypatch)

    response = client.post("/ai/smoke-tests/run", json={"provider_mode": "scaleway"})

    assert response.status_code == 200
    public_case = next(result for result in response.json()["results"] if result["case_id"] == "public_research_request")
    assert public_case["blocking_reason"] == "scaleway_monthly_token_cap_exceeded"
    assert public_case["token_metadata"]["blocked_by_token_cap"] is True
    assert public_case["token_metadata"]["monthly_token_cap"] == 1
    assert public_case["token_metadata"]["estimated_input_tokens"] > 0
    assert public_case["token_metadata"]["estimated_output_tokens"] > 0
    assert response.json()["external_call_attempted"] is False


def test_secret_and_sensitive_ip_are_blocked_by_local_policy(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-key")
    _enable_scaleway_smoke(client)

    response = client.post("/ai/smoke-tests/run", json={"provider_mode": "scaleway"})

    assert response.status_code == 200
    results = {result["case_id"]: result for result in response.json()["results"]}
    assert results["api_key_example"]["local_privacy_class"] == "secret"
    assert results["api_key_example"]["blocking_reason"] == "privacy_policy_secret_blocked"
    assert results["quick_coupler_geometry"]["local_privacy_class"] == "sensitive_ip"
    assert results["quick_coupler_geometry"]["blocking_reason"] == "privacy_policy_sensitive_ip_blocked"
    assert response.json()["external_call_attempted"] is False


def test_public_and_internal_cases_can_pass_policy_when_manually_enabled() -> None:
    from app.modules.ai.privacy import PrivacyPolicyEngine

    policy = PrivacyPolicyEngine()

    public_decision = policy.decide_for_external_smoke_test(
        "Public research request: summarize published literature.",
        confidential_allowed=True,
    )
    internal_decision = policy.decide_for_external_smoke_test(
        "Generic engineering note for rough sizing.",
        confidential_allowed=True,
    )

    assert public_decision.privacy_class == "public"
    assert public_decision.external_allowed is True
    assert internal_decision.privacy_class == "internal"
    assert internal_decision.external_allowed is True


def test_unknown_privacy_class_is_not_treated_as_public() -> None:
    from app.modules.ai.privacy import PrivacyPolicyEngine

    decision = PrivacyPolicyEngine().decide_for_external_smoke_test(
        "Unlabeled material with no known public marker.",
        confidential_allowed=True,
    )

    assert decision.privacy_class == "unknown"
    assert decision.external_allowed is False
    assert decision.blocking_reason == "privacy_policy_unknown_blocked"


def test_scaleway_smoke_uses_stub_after_guards_without_external_attempt(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-key")
    _enable_scaleway_smoke(client)
    _fail_if_live_adapter_called(monkeypatch)

    response = client.post("/ai/smoke-tests/run", json={"provider_mode": "scaleway"})

    assert response.status_code == 200
    body = response.json()
    assert body["external_call_attempted"] is False
    public_case = next(result for result in body["results"] if result["case_id"] == "public_research_request")
    assert public_case["blocking_reason"] == "stub_no_external_calls"
    assert public_case["provider_reported_class"] is None
    assert _event_count("AISmokeTestBlocked") == 1


def test_legacy_token_cap_update_does_not_change_active_scaleway_cap(client: TestClient) -> None:
    before = client.get("/ai/settings").json()

    response = client.put("/ai/settings", json={"scaleway_token_cap": 1})

    assert response.status_code == 200
    after = response.json()
    assert after["scaleway_monthly_token_cap"] == before["scaleway_monthly_token_cap"]
    assert "scaleway_token_cap" not in after
    assert "scaleway_tokens_month_to_date" not in after


def test_live_smoke_requires_scaleway_provider_mode(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-key")
    _enable_scaleway_smoke(client, scaleway_live_smoke_test_enabled=True)
    _fail_if_live_adapter_called(monkeypatch)

    response = client.post("/ai/smoke-tests/run", json={"provider_mode": "fake", "smoke_mode": "live"})

    assert response.status_code == 200
    body = response.json()
    assert body["external_call_attempted"] is False
    assert {result["blocking_reason"] for result in body["results"]} == {"scaleway_provider_mode_required"}


def test_live_smoke_requires_paid_ai_before_provider_call(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-key")
    _enable_scaleway_smoke(client, paid_ai_enabled=False, scaleway_live_smoke_test_enabled=True)
    _fail_if_live_adapter_called(monkeypatch)
    _fail_if_live_provider_called(monkeypatch)

    response = client.post("/ai/smoke-tests/run", json={"provider_mode": "scaleway", "smoke_mode": "live"})

    assert response.status_code == 200
    body = response.json()
    assert body["external_call_attempted"] is False
    assert {result["blocking_reason"] for result in body["results"]} == {"paid_ai_disabled"}


def test_live_smoke_requires_live_flag_before_key_or_provider_call(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-key")
    _enable_scaleway_smoke(client)
    _fail_if_live_adapter_called(monkeypatch)
    _fail_if_live_provider_called(monkeypatch)

    response = client.post("/ai/smoke-tests/run", json={"provider_mode": "scaleway", "smoke_mode": "live"})

    assert response.status_code == 200
    body = response.json()
    assert body["external_call_attempted"] is False
    assert {result["blocking_reason"] for result in body["results"]} == {"scaleway_live_smoke_test_disabled"}


def test_live_smoke_requires_api_key_before_provider_call(client: TestClient, monkeypatch) -> None:
    _enable_scaleway_smoke(client, scaleway_live_smoke_test_enabled=True)
    _fail_if_live_adapter_called(monkeypatch)
    _fail_if_live_provider_called(monkeypatch)

    response = client.post("/ai/smoke-tests/run", json={"provider_mode": "scaleway", "smoke_mode": "live"})

    assert response.status_code == 200
    body = response.json()
    assert body["external_call_attempted"] is False
    assert {result["blocking_reason"] for result in body["results"]} == {"scaleway_api_key_missing"}


def test_live_smoke_blocks_secret_and_sensitive_ip_before_provider_call(client: TestClient, monkeypatch) -> None:
    from app.modules.ai.providers.scaleway import ScalewayChatResult, ScalewayProvider

    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-key")
    _enable_scaleway_smoke(client, scaleway_live_smoke_test_enabled=True)
    prompts: list[str] = []

    def fake_completion(self: ScalewayProvider, *, prompt: str, estimated_output_tokens: int) -> ScalewayChatResult:
        prompts.append(prompt)
        return ScalewayChatResult(
            provider_name="scaleway",
            model="mock-model",
            mode="live",
            external_call_attempted=True,
            external_call_succeeded=True,
            response_text="public synthetic classification",
            reported_input_tokens=10,
            reported_output_tokens=4,
            reported_total_tokens=14,
            sanitized_metadata={"implementation": "mock"},
        )

    monkeypatch.setattr(ScalewayProvider, "create_live_smoke_completion", fake_completion)

    response = client.post("/ai/smoke-tests/run", json={"provider_mode": "scaleway", "smoke_mode": "live"})

    assert response.status_code == 200
    results = {result["case_id"]: result for result in response.json()["results"]}
    assert results["api_key_example"]["blocking_reason"] == "privacy_policy_secret_blocked"
    assert results["quick_coupler_geometry"]["blocking_reason"] == "privacy_policy_sensitive_ip_blocked"
    assert all("API key" not in prompt for prompt in prompts)
    assert all("Quick Coupler" not in prompt for prompt in prompts)


def test_live_smoke_token_cap_blocks_before_provider_call(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-key")
    _enable_scaleway_smoke(client, scaleway_live_smoke_test_enabled=True, scaleway_monthly_token_cap=1)
    _fail_if_live_adapter_called(monkeypatch)
    _fail_if_live_provider_called(monkeypatch)

    response = client.post("/ai/smoke-tests/run", json={"provider_mode": "scaleway", "smoke_mode": "live"})

    assert response.status_code == 200
    body = response.json()
    assert body["external_call_attempted"] is False
    public_case = next(result for result in body["results"] if result["case_id"] == "public_research_request")
    assert public_case["blocking_reason"] == "scaleway_monthly_token_cap_exceeded"


def test_successful_mocked_live_scaleway_call_updates_usage_counters(client: TestClient, monkeypatch) -> None:
    from app.modules.ai.providers.scaleway import ScalewayChatResult, ScalewayProvider

    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-key")
    _enable_scaleway_smoke(client, scaleway_live_smoke_test_enabled=True)
    status = client.get("/ai/status").json()
    assert status["external_calls_allowed"] is True
    assert status["blocking_reason"] is None

    def fake_completion(self: ScalewayProvider, *, prompt: str, estimated_output_tokens: int) -> ScalewayChatResult:
        return ScalewayChatResult(
            provider_name="scaleway",
            model="mock-model",
            mode="live",
            external_call_attempted=True,
            external_call_succeeded=True,
            response_text="public synthetic classification",
            reported_input_tokens=11,
            reported_output_tokens=5,
            reported_total_tokens=16,
            sanitized_metadata={"implementation": "mock", "usage_returned": True},
        )

    monkeypatch.setattr(ScalewayProvider, "create_live_smoke_completion", fake_completion)

    response = client.post("/ai/smoke-tests/run", json={"provider_mode": "scaleway", "smoke_mode": "live"})

    assert response.status_code == 200
    body = response.json()
    assert body["smoke_mode"] == "live"
    assert body["external_call_attempted"] is True
    assert body["external_call_succeeded"] is True
    live_successes = [result for result in body["results"] if result["external_call_succeeded"]]
    assert len(live_successes) == 2
    assert live_successes[0]["token_metadata"]["reported_input_tokens"] == 11
    assert live_successes[0]["token_metadata"]["reported_output_tokens"] == 5
    assert live_successes[0]["token_metadata"]["usage_source"] == "actual"
    settings = client.get("/ai/settings").json()
    assert settings["scaleway_input_tokens_month_to_date"] == 22
    assert settings["scaleway_output_tokens_month_to_date"] == 10
    assert "test-only-key" not in json.dumps(body)
    event_payload = _latest_event_payload("AISmokeTestBlocked")
    assert event_payload["external_call_attempted"] is True
    assert event_payload["external_call_succeeded"] is True
    assert event_payload["synthetic_only"] is False
    assert event_payload["adapter_interface"] == "provider_neutral"
    assert any(result["adapter_interface"] == "provider_neutral" for result in event_payload["results"])
    assert "test-only-key" not in json.dumps(event_payload)


def test_live_scaleway_zero_reported_usage_updates_counters_exactly_once(client: TestClient, monkeypatch) -> None:
    from app.modules.ai.providers.scaleway import ScalewayChatResult, ScalewayProvider

    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-key")
    _enable_scaleway_smoke(client, scaleway_live_smoke_test_enabled=True)

    def fake_completion(self: ScalewayProvider, *, prompt: str, estimated_output_tokens: int) -> ScalewayChatResult:
        return ScalewayChatResult(
            provider_name="scaleway",
            model="mock-model",
            mode="live",
            external_call_attempted=True,
            external_call_succeeded=True,
            response_text="public synthetic classification",
            reported_input_tokens=0,
            reported_output_tokens=0,
            reported_total_tokens=0,
            sanitized_metadata={"implementation": "mock", "usage_returned": True},
        )

    monkeypatch.setattr(ScalewayProvider, "create_live_smoke_completion", fake_completion)

    response = client.post("/ai/smoke-tests/run", json={"provider_mode": "scaleway", "smoke_mode": "live"})

    assert response.status_code == 200
    live_successes = [result for result in response.json()["results"] if result["external_call_succeeded"]]
    assert len(live_successes) == 2
    assert all(result["token_metadata"]["reported_input_tokens"] == 0 for result in live_successes)
    assert all(result["token_metadata"]["reported_output_tokens"] == 0 for result in live_successes)
    assert all(result["token_metadata"]["usage_source"] == "actual" for result in live_successes)
    settings = client.get("/ai/settings").json()
    assert settings["scaleway_input_tokens_month_to_date"] == 0
    assert settings["scaleway_output_tokens_month_to_date"] == 0


def test_live_scaleway_missing_usage_uses_conservative_estimates(client: TestClient, monkeypatch) -> None:
    from app.modules.ai.providers.scaleway import ScalewayChatResult, ScalewayProvider

    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-key")
    _enable_scaleway_smoke(client, scaleway_live_smoke_test_enabled=True)

    def fake_completion(self: ScalewayProvider, *, prompt: str, estimated_output_tokens: int) -> ScalewayChatResult:
        return ScalewayChatResult(
            provider_name="scaleway",
            model="mock-model",
            mode="live",
            external_call_attempted=True,
            external_call_succeeded=True,
            response_text="public synthetic classification",
            reported_input_tokens=None,
            reported_output_tokens=None,
            reported_total_tokens=None,
            sanitized_metadata={"implementation": "mock", "usage_returned": False},
        )

    monkeypatch.setattr(ScalewayProvider, "create_live_smoke_completion", fake_completion)

    response = client.post("/ai/smoke-tests/run", json={"provider_mode": "scaleway", "smoke_mode": "live"})

    assert response.status_code == 200
    live_successes = [result for result in response.json()["results"] if result["external_call_succeeded"]]
    expected_input = sum(result["token_metadata"]["estimated_input_tokens"] for result in live_successes)
    expected_output = sum(result["token_metadata"]["estimated_output_tokens"] for result in live_successes)
    assert all(result["token_metadata"]["usage_source"] == "estimated" for result in live_successes)
    settings = client.get("/ai/settings").json()
    assert settings["scaleway_input_tokens_month_to_date"] == expected_input
    assert settings["scaleway_output_tokens_month_to_date"] == expected_output


def _fail_if_live_provider_called(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.ai.providers.scaleway import ScalewayProvider

    def fail(self: ScalewayProvider, *, prompt: str, estimated_output_tokens: int) -> object:
        raise AssertionError("Live Scaleway provider should not have been called.")

    monkeypatch.setattr(ScalewayProvider, "create_live_smoke_completion", fail)


def _fail_if_live_adapter_called(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.ai.contracts import AIRequest, AIResponse
    from app.modules.ai.providers.scaleway_adapter import ScalewayProviderAdapter

    def fail(self: ScalewayProviderAdapter, request: AIRequest) -> AIResponse:
        raise AssertionError("Live Scaleway adapter should not have been called.")

    monkeypatch.setattr(ScalewayProviderAdapter, "complete", fail)
