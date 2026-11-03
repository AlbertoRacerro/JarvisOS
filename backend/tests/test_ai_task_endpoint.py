from __future__ import annotations

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
    monkeypatch.delenv("GLM_API_KEY", raising=False)
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


def _all_ai_jobs() -> list[dict[str, object]]:
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        rows = connection.execute("SELECT * FROM ai_jobs ORDER BY created_at ASC").fetchall()
    return [dict(row) for row in rows]


def test_task_endpoint_local_fake_uses_run_ai_task_and_writes_one_ai_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.modules.ai.execution as execution

    real_run_ai_task = execution.run_ai_task
    calls: list[dict[str, object]] = []

    def spy_run_ai_task(**kwargs):
        calls.append(kwargs)
        return real_run_ai_task(**kwargs)

    monkeypatch.setattr(execution, "run_ai_task", spy_run_ai_task)

    response = client.post(
        "/ai/tasks/run",
        json={
            "prompt": "POS-1B fake endpoint smoke",
            "route_class": "local:fake",
            "task_kind": "general",
            "max_tokens": 64,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["include_project_context"] is False
    assert body["workspace_id"] is None
    assert body["context_digest"] is None
    assert body["context_sources_count"] == 0
    assert body["ledger_id"]
    assert body["selected_route_class"] == "local:fake"
    assert body["response_text"].startswith("[fake:")
    assert body["provider_id"] == "fake"
    assert body["model_id"] == "fake-deterministic-v1"
    assert body["usage"]["input_tokens"] > 0
    assert body["usage"]["output_tokens"] > 0
    assert len(calls) == 1
    assert calls[0]["route_class"] == "local:fake"

    rows = _all_ai_jobs()
    assert len(rows) == 1
    assert rows[0]["id"] == body["ledger_id"]
    assert rows[0]["status"] == "success"
    assert rows[0]["selected_route_class"] == "local:fake"


def test_task_endpoint_defaults_to_safe_local_fake_route(client: TestClient) -> None:
    response = client.post(
        "/ai/tasks/run",
        json={"prompt": "default route must stay local even for synthesis", "task_kind": "synthesis"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["include_project_context"] is False
    assert body["workspace_id"] is None
    assert body["context_digest"] is None
    assert body["context_sources_count"] == 0
    assert body["selected_route_class"] == "local:fake"

    rows = _all_ai_jobs()
    assert len(rows) == 1
    assert rows[0]["selected_route_class"] == "local:fake"


def test_task_endpoint_unbound_route_fails_closed_and_writes_one_ai_job(client: TestClient) -> None:
    response = client.post(
        "/ai/tasks/run",
        json={"prompt": "unbound route", "route_class": "external:not_bound", "max_tokens": 64},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "route_unavailable"
    assert body["selected_route_class"] == "external:not_bound"
    assert body["blocked_reason"] == "route_unavailable"
    assert body["error_type"] == "route_unavailable"
    assert body["response_text"] is None
    assert body["provider_id"] is None
    assert body["model_id"] is None

    rows = _all_ai_jobs()
    assert len(rows) == 1
    assert rows[0]["id"] == body["ledger_id"]
    assert rows[0]["status"] == "route_unavailable"


def test_task_endpoint_malformed_route_fails_closed_and_writes_one_ai_job(client: TestClient) -> None:
    response = client.post(
        "/ai/tasks/run",
        json={"prompt": "malformed route", "route_class": "external reasoning", "max_tokens": 64},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "validation_error"
    assert body["selected_route_class"] == "external reasoning"
    assert body["blocked_reason"] == "route_class_malformed"
    assert body["error_type"] == "validation_error"
    assert body["response_text"] is None

    rows = _all_ai_jobs()
    assert len(rows) == 1
    assert rows[0]["id"] == body["ledger_id"]
    assert rows[0]["status"] == "validation_error"


def test_task_endpoint_external_route_requires_max_tokens_before_provider_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    settings_response = client.put(
        "/ai/settings",
        json={
            "provider_mode": "scaleway",
            "default_ai_provider": "scaleway",
            "paid_ai_enabled": True,
            "monthly_api_budget_usd": 1,
            "scaleway_enabled": True,
            "scaleway_smoke_test_enabled": True,
            "scaleway_live_smoke_test_enabled": True,
            "use_fake_provider_when_budget_zero": False,
        },
    )
    assert settings_response.status_code == 200

    from app.modules.ai.contracts import AIRequest, AIResponse
    from app.modules.ai.providers.openai_compat_adapter import OpenAICompatAdapter

    def fail(self: OpenAICompatAdapter, request: AIRequest) -> AIResponse:
        raise AssertionError("external task endpoint must require max_tokens before provider call")

    monkeypatch.setattr(OpenAICompatAdapter, "complete", fail)

    response = client.post(
        "/ai/tasks/run",
        json={"prompt": "external route without explicit max", "route_class": "external:cheap"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "config_error"
    assert body["selected_route_class"] == "external:cheap"
    assert body["provider_id"] == "deepseek"
    assert body["model_id"] == "deepseek-v4-pro"
    assert body["blocked_reason"] == "max_output_tokens_required"
    assert "max_output_tokens_required" in body["decision_reason"]
    assert body["error_type"] == "config_error"
    assert body["response_text"] is None

    rows = _all_ai_jobs()
    assert len(rows) == 1
    assert rows[0]["id"] == body["ledger_id"]
    assert rows[0]["status"] == "config_error"
    assert rows[0]["provider_id"] == "deepseek"
    assert rows[0]["error_type"] == "config_error"


def test_task_endpoint_external_route_respects_ai_settings_gate_before_provider_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")

    from app.modules.ai.contracts import AIRequest, AIResponse
    from app.modules.ai.providers.openai_compat_adapter import OpenAICompatAdapter

    def fail(self: OpenAICompatAdapter, request: AIRequest) -> AIResponse:
        raise AssertionError("external task endpoint must respect settings gate before provider call")

    monkeypatch.setattr(OpenAICompatAdapter, "complete", fail)

    response = client.post(
        "/ai/tasks/run",
        json={"prompt": "settings disabled external route", "route_class": "external:cheap", "max_tokens": 64},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "config_error"
    assert body["selected_route_class"] == "external:cheap"
    assert body["provider_id"] == "deepseek"
    assert body["model_id"] == "deepseek-v4-pro"
    assert body["blocked_reason"] == "paid_ai_disabled"
    assert body["error_type"] == "config_error"
    assert "paid_ai_disabled" in body["decision_reason"]
    assert body["response_text"] is None

    rows = _all_ai_jobs()
    assert len(rows) == 1
    assert rows[0]["id"] == body["ledger_id"]
    assert rows[0]["status"] == "config_error"
    assert rows[0]["provider_id"] == "deepseek"
    assert rows[0]["error_type"] == "config_error"


def test_task_endpoint_rejects_too_many_context_blocks(client: TestClient) -> None:
    response = client.post(
        "/ai/tasks/run",
        json={
            "prompt": "too many context blocks",
            "route_class": "local:fake",
            "context_blocks": [{"content": str(index)} for index in range(21)],
        },
    )

    assert response.status_code == 422
    assert _all_ai_jobs() == []


def test_task_endpoint_rejects_oversized_context_blocks(client: TestClient) -> None:
    response = client.post(
        "/ai/tasks/run",
        json={
            "prompt": "too large context",
            "route_class": "local:fake",
            "context_blocks": [{"content": "x" * 32_001}],
        },
    )

    assert response.status_code == 422
    assert _all_ai_jobs() == []


def test_task_endpoint_accepts_small_context_blocks_for_local_fake(client: TestClient) -> None:
    response = client.post(
        "/ai/tasks/run",
        json={
            "prompt": "small context",
            "route_class": "local:fake",
            "context_blocks": [{"source": "manual", "content": "small context"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["include_project_context"] is False
    assert body["workspace_id"] is None
    assert body["context_digest"] is not None
    assert body["context_sources_count"] >= 1
    rows = _all_ai_jobs()
    assert len(rows) == 1
    assert rows[0]["context_digest"].startswith("sha256:")


def test_task_endpoint_without_project_context_records_no_sources(client: TestClient) -> None:
    response = client.post(
        "/ai/tasks/run",
        json={"prompt": "no project context", "route_class": "local:fake"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["include_project_context"] is False
    assert body["workspace_id"] is None
    assert body["context_digest"] is None
    assert body["context_sources_count"] == 0
    rows = _all_ai_jobs()
    assert len(rows) == 1
    assert rows[0]["context_sources_json"] is None


def test_task_endpoint_include_project_context_injects_workspace_context(client: TestClient) -> None:
    from app.modules.modeling.models import DecisionCreate, ParameterCreate
    from app.modules.modeling.service import create_decision, create_parameter

    create_decision("main", DecisionCreate(title="Provider", decision_text="Scaleway EU first", status="accepted"))
    create_parameter(
        "main",
        ParameterCreate(name="tube_diameter", value="0.05", unit="m", source_ref="spec-1", status="approved"),
    )

    response = client.post(
        "/ai/tasks/run",
        json={
            "prompt": "what do you know about the project?",
            "route_class": "local:fake",
            "include_project_context": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["include_project_context"] is True
    assert body["workspace_id"] == "main"
    assert body["context_digest"] is not None
    assert body["context_sources_count"] >= 2

    rows = _all_ai_jobs()
    assert len(rows) == 1
    assert rows[0]["context_digest"] is not None
    sources = json.loads(rows[0]["context_sources_json"])
    assert any(source["type"] == "decision" for source in sources)
    assert any(source["type"] == "parameter" for source in sources)


def test_task_endpoint_include_project_context_uses_legacy_no_selection_bundle(client: TestClient) -> None:
    from app.modules.ai.context_builder import build_workspace_context_bundle
    from app.modules.modeling.models import AssumptionCreate, DecisionCreate, ParameterCreate
    from app.modules.modeling.service import create_assumption, create_decision, create_parameter

    create_decision(
        "main", DecisionCreate(title="Legacy decision", decision_text="Keep full dump", status="accepted")
    )
    create_assumption(
        "main", AssumptionCreate(statement="Legacy assumption", confidence="medium", status="accepted")
    )
    create_parameter(
        "main",
        ParameterCreate(name="legacy_parameter", value="42", unit="mm", source_ref="legacy-spec", status="approved"),
    )
    expected_bundle = build_workspace_context_bundle("main")

    response = client.post(
        "/ai/tasks/run",
        json={
            "prompt": "use the unselected legacy workspace context",
            "route_class": "local:fake",
            "include_project_context": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["include_project_context"] is True
    assert body["workspace_id"] == "main"
    assert body["context_digest"] == expected_bundle.context_digest
    assert body["context_sources_count"] == len(expected_bundle.sources)

    rows = _all_ai_jobs()
    assert len(rows) == 1
    assert rows[0]["context_digest"] == expected_bundle.context_digest
    assert json.loads(rows[0]["context_sources_json"]) == expected_bundle.sources


def test_task_endpoint_invalid_workspace_fails_closed_before_provider(client: TestClient) -> None:
    response = client.post(
        "/ai/tasks/run",
        json={
            "prompt": "context from a missing workspace",
            "route_class": "local:fake",
            "include_project_context": True,
            "workspace_id": "does-not-exist",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "config_error"
    assert body["error_type"] == "context_build_error"
    assert body["include_project_context"] is True
    assert body["workspace_id"] == "does-not-exist"
    assert body["context_digest"] is None
    assert body["context_sources_count"] == 0
    assert body["response_text"] is None

    rows = _all_ai_jobs()
    assert len(rows) == 1
    assert rows[0]["status"] == "config_error"
    assert rows[0]["error_type"] == "context_build_error"


def _enable_external(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    response = client.put(
        "/ai/settings",
        json={
            "provider_mode": "deepseek",
            "default_ai_provider": "deepseek",
            "paid_ai_enabled": True,
            "monthly_api_budget_usd": 1,
            "use_fake_provider_when_budget_zero": False,
        },
    )
    assert response.status_code == 200


def _request_ticket(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/ai/tasks/run",
        json={
            "prompt": "Explain a generic pump sizing method.",
            "route_class": "external:cheap",
            "task_kind": "general",
            "max_tokens": 64,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "validation_error"
    assert body["blocked_reason"] == "confirmation_required"
    assert body["egress_reason_code"] == "confirmation_required"
    assert body["egress_ticket_id"]
    assert body["confirmation_payload"] == {"ticket_id": body["egress_ticket_id"]}
    assert body["egress_trigger_ids"] == ["t1"]
    return body


def test_confirm_escalation_consumes_ticket_and_executes_exact_packet(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_external(client, monkeypatch)
    from app.modules.ai.contracts import AIExternalDispatchState, AIRequest, AIResponse, AIUsage
    from app.modules.ai.providers.openai_compat_adapter import OpenAICompatAdapter

    seen: list[AIRequest] = []

    def complete(self: OpenAICompatAdapter, request: AIRequest) -> AIResponse:
        seen.append(request)
        return AIResponse(
            provider_id="deepseek",
            model_id=request.model_preference or "deepseek-v4-pro",
            request_id=request.request_id,
            text="confirmed answer",
            content="confirmed answer",
            usage=AIUsage(
                provider_id="deepseek",
                model_id=request.model_preference or "deepseek-v4-pro",
                input_tokens=5,
                output_tokens=7,
            ),
            safety_status="allowed",
            external_dispatch_state=AIExternalDispatchState.started,
        )

    monkeypatch.setattr(OpenAICompatAdapter, "complete", complete)
    ticket = _request_ticket(client)
    assert seen == []

    response = client.post(
        "/ai/tasks/escalations/confirm",
        json={"ticket_id": ticket["egress_ticket_id"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["ticket_id"] == ticket["egress_ticket_id"]
    assert body["reason_code"] == "ticket_consumed"
    task = body["task_response"]
    assert task["status"] == "success"
    assert task["response_text"] == "confirmed answer"
    assert task["egress_ticket_id"] == ticket["egress_ticket_id"]
    assert task["egress_packet_digest"] == ticket["egress_packet_digest"]
    assert task["egress_trigger_ids"] == ["t1"]
    assert len(seen) == 1
    assert seen[0].prompt == "Explain a generic pump sizing method."
    assert seen[0].model_preference == "deepseek-v4-pro"
    assert seen[0].max_output_tokens == 64
    rows = _all_ai_jobs()
    assert [row["status"] for row in rows] == ["validation_error", "success"]


def test_confirm_escalation_replay_returns_conflict_without_second_provider_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_external(client, monkeypatch)
    from app.modules.ai.contracts import AIExternalDispatchState, AIRequest, AIResponse, AIUsage
    from app.modules.ai.providers.openai_compat_adapter import OpenAICompatAdapter

    calls = 0

    def complete(self: OpenAICompatAdapter, request: AIRequest) -> AIResponse:
        nonlocal calls
        calls += 1
        return AIResponse(
            provider_id="deepseek",
            model_id=request.model_preference or "deepseek-v4-pro",
            request_id=request.request_id,
            text="ok",
            content="ok",
            usage=AIUsage(provider_id="deepseek", model_id="deepseek-v4-pro", input_tokens=1, output_tokens=1),
            safety_status="allowed",
            external_dispatch_state=AIExternalDispatchState.started,
        )

    monkeypatch.setattr(OpenAICompatAdapter, "complete", complete)
    ticket = _request_ticket(client)
    payload = {"ticket_id": ticket["egress_ticket_id"]}
    first = client.post("/ai/tasks/escalations/confirm", json=payload)
    second = client.post("/ai/tasks/escalations/confirm", json=payload)

    assert first.status_code == 200
    assert second.status_code == 409
    assert "not pending: consumed" in second.json()["detail"]
    assert calls == 1


def test_confirm_escalation_rejects_legacy_client_owned_payload(client: TestClient) -> None:
    response = client.post(
        "/ai/tasks/escalations/confirm",
        json={
            "proposal": {
                "proposal_ledger_id": "proposal-1",
                "proposed_route_class": "external:reasoning",
                "outbound_text": "client-owned replacement prompt",
            },
            "task_kind": "general",
        },
    )

    assert response.status_code == 422
    assert _all_ai_jobs() == []


def test_confirm_escalation_gate_change_revokes_ticket_without_provider_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_external(client, monkeypatch)
    from app.modules.ai.providers.openai_compat_adapter import OpenAICompatAdapter

    def fail(self, request):
        raise AssertionError("provider must not be called after gate change")

    monkeypatch.setattr(OpenAICompatAdapter, "complete", fail)
    ticket = _request_ticket(client)
    client.put("/ai/settings", json={"paid_ai_enabled": False})

    response = client.post(
        "/ai/tasks/escalations/confirm",
        json={"ticket_id": ticket["egress_ticket_id"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "config_error"
    assert body["reason_code"] == "paid_ai_disabled"
    assert body["task_response"]["blocked_reason"] == "paid_ai_disabled"
    rows = _all_ai_jobs()
    assert [row["status"] for row in rows] == ["validation_error", "config_error"]


def test_confirm_escalation_credential_removal_revokes_ticket_without_provider_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_external(client, monkeypatch)
    from app.modules.ai.providers.openai_compat_adapter import OpenAICompatAdapter

    def fail(self, request):
        raise AssertionError("provider must not be called without credentials")

    monkeypatch.setattr(OpenAICompatAdapter, "complete", fail)
    ticket = _request_ticket(client)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    response = client.post(
        "/ai/tasks/escalations/confirm",
        json={"ticket_id": ticket["egress_ticket_id"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "config_error"
    assert body["reason_code"] == "provider_credentials_missing"
    assert body["task_response"]["blocked_reason"] == "provider_credentials_missing"


@pytest.mark.parametrize("task_kind", ["code-review", "CODE_REVIEW"])
def test_task_endpoint_accepts_safe_task_kind_variants(client: TestClient, task_kind: str) -> None:
    response = client.post(
        "/ai/tasks/run",
        json={
            "prompt": "Validate task kind compatibility.",
            "route_class": "local:fake",
            "task_kind": task_kind,
            "max_tokens": 32,
        },
    )

    assert response.status_code == 200
    jobs = _all_ai_jobs()
    assert len(jobs) == 1
    assert jobs[0]["task_kind"] == task_kind
    assert jobs[0]["flow_id"] is not None


def test_task_endpoint_rejects_malformed_task_kind_before_execution(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.modules.ai.execution as execution

    def fail_execution(**_kwargs):
        pytest.fail("malformed task_kind must be rejected before execution")

    monkeypatch.setattr(execution, "run_ai_task", fail_execution)
    response = client.post(
        "/ai/tasks/run",
        json={
            "prompt": "Reject malformed task kind.",
            "route_class": "local:fake",
            "task_kind": "bad kind!",
            "max_tokens": 32,
        },
    )

    assert response.status_code == 422
    assert _all_ai_jobs() == []
