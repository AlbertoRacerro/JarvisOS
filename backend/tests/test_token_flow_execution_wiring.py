from __future__ import annotations


def _initialize(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "token-flow-execution"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.database import initialize_database

    initialize_database()


def _job(job_id: str) -> dict[str, object]:
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT * FROM ai_jobs WHERE id = ?", (job_id,)).fetchone()
    assert row is not None
    return dict(row)


class _RaisingLocalAdapter:
    provider_id = "local_test"

    def complete(self, request):
        raise RuntimeError("local test failure")

    def health(self):  # pragma: no cover - not used
        raise NotImplementedError

    def list_models(self):  # pragma: no cover - not used
        return []

    def stream(self, request):  # pragma: no cover - not used
        raise NotImplementedError


def test_fake_execution_creates_complete_flow_with_synthetic_evidence(monkeypatch, tmp_path) -> None:
    _initialize(monkeypatch, tmp_path)
    from app.modules.ai.execution import run_ai_task
    from app.modules.ai.token_flow_service import get_flow

    outcome = run_ai_task(user_prompt="deterministic", route_class="local:fake")

    assert outcome.status == "success"
    assert outcome.flow_id is not None
    row = _job(outcome.ledger_id)
    flow = get_flow(outcome.flow_id)
    assert row["flow_id"] == outcome.flow_id
    assert row["flow_attempt_index"] == 0
    assert row["fallback_index"] == 0
    assert row["execution_class"] == "synthetic"
    assert row["adapter_invoked"] == 1
    assert row["external_dispatch_state"] == "not_applicable"
    assert row["normalized_usage_source"] == "estimated"
    assert row["accounting_basis"] == "synthetic_not_economic"
    assert row["accounted_provider_spend_usd_decimal"] == "0"
    assert flow["state"] == "complete"
    assert flow["terminal_attempt_id"] == outcome.ledger_id
    assert flow["ordered_attempt_ids"] == [outcome.ledger_id]
    assert flow["synthetic_evidence_present"] is True
    assert flow["external_provider_spend_usd_decimal"] == "0"


def test_local_adapter_exception_is_linked_and_terminalized_as_unpriced(monkeypatch, tmp_path) -> None:
    _initialize(monkeypatch, tmp_path)
    from app.modules.ai.execution import run_ai_task
    from app.modules.ai.execution_types import ProviderBinding
    from app.modules.ai.token_flow_service import get_flow

    binding = ProviderBinding(
        route_class="local:test",
        provider_id="local_test",
        model_id="local-model",
        requires_network=False,
        max_output_tokens=64,
        execution_class="local_compute",
        context_window_tokens=4096,
    )
    outcome = run_ai_task(
        user_prompt="fail locally",
        route_class="local:test",
        adapters={"local_test": _RaisingLocalAdapter()},
        bindings={"local:test": binding},
    )

    assert outcome.status == "provider_error"
    assert outcome.flow_id is not None
    row = _job(outcome.ledger_id)
    flow = get_flow(outcome.flow_id)
    assert row["execution_class"] == "local_compute"
    assert row["adapter_invoked"] == 1
    assert row["external_dispatch_state"] == "not_applicable"
    assert row["normalized_usage_source"] == "estimated"
    assert row["accounting_basis"] == "local_compute_unpriced"
    assert row["input_tokens"] > 0
    assert row["output_tokens"] == 0
    assert row["normalized_finish_reason"] == "error"
    assert flow["state"] == "failed_terminal"
    assert flow["local_compute_cost_unpriced"] is True


def test_unbound_route_records_no_execution_inside_failed_flow(monkeypatch, tmp_path) -> None:
    _initialize(monkeypatch, tmp_path)
    from app.modules.ai.execution import run_ai_task
    from app.modules.ai.token_flow_service import get_flow

    outcome = run_ai_task(
        user_prompt="no binding",
        route_class="local:missing",
        adapters={},
        bindings={},
    )

    assert outcome.status == "route_unavailable"
    assert outcome.flow_id is not None
    row = _job(outcome.ledger_id)
    flow = get_flow(outcome.flow_id)
    assert row["execution_class"] == "none"
    assert row["adapter_invoked"] == 0
    assert row["external_dispatch_state"] == "not_applicable"
    assert row["normalized_usage_source"] == "none"
    assert row["accounting_basis"] == "no_execution"
    assert row["provider_id"] is None
    assert row["model_id"] is None
    assert flow["state"] == "failed_terminal"
    assert flow["attempt_count"] == 1


def test_malformed_route_keeps_safe_flow_evidence_without_invalid_binding(monkeypatch, tmp_path) -> None:
    _initialize(monkeypatch, tmp_path)
    from app.modules.ai.execution import run_ai_task

    outcome = run_ai_task(user_prompt="bad route", route_class="external reasoning")

    assert outcome.status == "validation_error"
    assert outcome.selected_route_class == "external reasoning"
    row = _job(outcome.ledger_id)
    assert row["selected_route_class"] is None
    assert row["execution_class"] == "none"
    assert row["accounting_basis"] == "no_execution"
