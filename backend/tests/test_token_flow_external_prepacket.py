from __future__ import annotations

from app.modules.ai.contracts import AIExternalDispatchState
from app.modules.ai.egress_spine import create_queued_ai_job
from app.modules.ai.provider_registry import load_default_provider_registry
from app.modules.ai.token_flow_external_transaction import finalize_external_attempt
from app.modules.ai.token_flow_service import create_flow, get_flow


def _initialize(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "external-prepacket"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.database import initialize_database
    from app.modules.ai.settings import ensure_ai_settings

    initialize_database()
    ensure_ai_settings()


def test_prepacket_external_stop_finalizes_with_zero_dispatch(monkeypatch, tmp_path) -> None:
    _initialize(monkeypatch, tmp_path)
    registry = load_default_provider_registry()
    binding = registry.bindings["external:cheap"]
    flow = create_flow(task_kind="synthesis", requested_route_class="external:cheap")
    job_id = create_queued_ai_job(
        task_kind="synthesis",
        requested_route_class="external:cheap",
        selected_route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        decision_reason="bound:external:cheap",
        prompt_digest="sha256:" + "a" * 64,
        context_digest=None,
        context_sources=None,
        route_metadata={"fallback_attempt_index": 0},
    ).ai_job_id

    result = finalize_external_attempt(
        flow_id=str(flow["id"]),
        ai_job_id=job_id,
        binding=binding,
        fallback_index=0,
        status="config_error",
        response=None,
        latency_ms=1,
        error_type="provider_gate_blocked",
        adapter_invoked=False,
        dispatch_state=AIExternalDispatchState.not_started,
        requested_output_ceiling=32,
        effective_output_ceiling=None,
        outcome_reason="provider gate blocked",
        registry=registry,
    )

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        job = connection.execute("SELECT * FROM ai_jobs WHERE id = ?", (job_id,)).fetchone()
        egress_count = connection.execute("SELECT COUNT(*) AS n FROM egress_attempts").fetchone()["n"]
    assert result.reconciliation_status == "not_sent"
    assert job["flow_id"] == flow["id"]
    assert job["execution_class"] == "external_provider"
    assert job["adapter_invoked"] == 0
    assert job["external_dispatch_state"] == "not_started"
    assert job["normalized_usage_source"] == "none"
    assert job["accounting_basis"] == "external_not_sent"
    assert job["accounted_provider_spend_usd_decimal"] == "0"
    assert egress_count == 0
    assert get_flow(str(flow["id"]))["attempt_count"] == 1
