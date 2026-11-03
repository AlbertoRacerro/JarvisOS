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


def test_ai_gateway_uses_fake_provider_by_default(client: TestClient) -> None:
    response = client.get("/ai/status")

    assert response.status_code == 200
    status = response.json()
    assert status["active_provider_mode"] == "fake"
    assert status["policy_mode"] == "FAST_DEV"
    assert status["ai_enabled"] is True
    assert status["provider_id"] == "fake"
    assert status["usage_total_tokens"] == 0
    assert status["monthly_api_budget_usd"] == 0
    assert status["external_calls_allowed"] is False
    assert status["fake_provider_enabled"] is True


def test_modeling_draft_returns_structured_fake_output(client: TestClient) -> None:
    response = client.post(
        "/ai/modeling/draft",
        json={
            "workspace_id": "main",
            "informal_model_idea": "Estimate buoyancy margin for a algae photobioreactor.",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["engineering_question"]
    assert body["draft"]["proposed_assumptions"]
    assert body["draft"]["proposed_parameters"]
    assert body["ai_metadata"]["provider"] == "fake"
    assert body["ai_metadata"]["paid_api_call_attempted"] is False
    assert body["ai_metadata"]["estimated_cost_usd"] == 0
    assert _event_count("AIModelingDraftRequested") == 1
    assert _event_count("AIModelingDraftCompleted") == 1


def test_paid_provider_path_is_blocked_when_budget_is_zero(client: TestClient) -> None:
    settings_response = client.put(
        "/ai/settings",
        json={
            "provider_mode": "scaleway",
            "default_ai_provider": "scaleway",
            "scaleway_enabled": True,
            "use_fake_provider_when_budget_zero": False,
        },
    )
    assert settings_response.status_code == 200

    response = client.post(
        "/ai/modeling/draft",
        json={
            "workspace_id": "main",
            "informal_model_idea": "Estimate wave loads on a submerged tube.",
            "provider_mode": "scaleway",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["draft"] is None
    assert body["ai_metadata"]["provider_mode"] == "scaleway"
    assert body["ai_metadata"]["paid_api_call_attempted"] is False
    assert body["ai_metadata"]["success"] is False
    assert body["ai_metadata"]["blocked_reason"] in {
        "scaleway_smoke_test_disabled",
        "scaleway_api_key_missing",
        "paid_ai_disabled",
        "monthly_budget_zero",
    }
    assert _event_count("AIModelingDraftBlockedByBudget") == 1


def test_budget_settings_can_be_updated_without_calling_real_provider(client: TestClient) -> None:
    response = client.put(
        "/ai/settings",
        json={
            "monthly_api_budget_usd": 25,
            "paid_ai_enabled": True,
            "provider_mode": "fake",
        },
    )

    assert response.status_code == 200
    settings = response.json()
    assert settings["monthly_api_budget_usd"] == 25
    assert settings["paid_ai_enabled"] is True

    draft_response = client.post(
        "/ai/modeling/draft",
        json={
            "workspace_id": "main",
            "informal_model_idea": "Estimate required pumping power.",
        },
    )

    assert draft_response.status_code == 200
    metadata = draft_response.json()["ai_metadata"]
    assert metadata["provider"] == "fake"
    assert metadata["paid_api_call_attempted"] is False


def test_scaleway_status_reports_missing_key_without_exposing_secret(client: TestClient) -> None:
    client.put(
        "/ai/settings",
        json={"provider_mode": "scaleway", "scaleway_enabled": True, "monthly_api_budget_usd": 10},
    )

    response = client.get("/ai/status")

    assert response.status_code == 200
    status = response.json()
    assert status["scaleway_enabled"] is True
    assert status["scaleway_api_key_configured"] is False
    assert status["external_calls_allowed"] is False
    assert "key" not in status


def test_endpoint_does_not_create_model_spec_without_explicit_save(client: TestClient) -> None:
    before = client.get("/workspaces/main/model-specs").json()

    client.post(
        "/ai/modeling/draft",
        json={
            "workspace_id": "main",
            "informal_model_idea": "Draft a model for pond circulation energy.",
        },
    )

    after = client.get("/workspaces/main/model-specs").json()
    assert after == before
    assert _latest_event_payload("AIModelingDraftCompleted")["provider"] == "fake"
