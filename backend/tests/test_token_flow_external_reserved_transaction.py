from __future__ import annotations

from dataclasses import replace

import pytest

from app.modules.ai.contracts import (
    AIExternalDispatchState,
    AIRequest,
    AIResponse,
    AITaskType,
    AIUsage,
    AIUsageSource,
)
from app.modules.ai.egress_lifecycle import consume_confirmation_ticket, start_reserved_attempt
from app.modules.ai.egress_persistence import prepare_egress_attempt
from app.modules.ai.egress_policy import EXTERNAL_PROVIDER_OPERATION
from app.modules.ai.egress_service import EgressContractError, EgressPacketMaterial
from app.modules.ai.egress_spine import create_queued_ai_job
from app.modules.ai.provider_registry import load_default_provider_registry
from app.modules.ai.token_flow_external_transaction import finalize_external_attempt
from app.modules.ai.token_flow_service import TokenFlowConflictError, create_flow
from app.modules.ai.usage_cost import actual_registry_cost_usd


def _initialize(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "external-reserved"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.database import initialize_database
    from app.modules.ai.models import AISettingsUpdate
    from app.modules.ai.settings import update_ai_settings

    initialize_database()
    update_ai_settings(AISettingsUpdate(paid_ai_enabled=True, monthly_api_budget_usd=10))


def _material() -> EgressPacketMaterial:
    return EgressPacketMaterial(
        operation=EXTERNAL_PROVIDER_OPERATION,
        task_kind="synthesis",
        route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        fallback_index=0,
        prompt="Public engineering summary.",
        context_blocks=(),
        prompt_level="S0",
        context_level="S0",
        final_level="S0",
        max_output_tokens=32,
    )


def _started_reservation() -> tuple[object, object, str, str]:
    registry = load_default_provider_registry()
    binding = registry.bindings["external:cheap"]
    preparation = prepare_egress_attempt(_material(), registry=registry)
    assert preparation.result == "pause"
    assert preparation.ticket_id is not None
    consumed = consume_confirmation_ticket(preparation.ticket_id, registry=registry)
    assert consumed.authorized is True
    assert consumed.reservation_id is not None
    job_id = create_queued_ai_job(
        task_kind="synthesis",
        requested_route_class="external:cheap",
        selected_route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        decision_reason="bound:external:cheap",
        prompt_digest="sha256:" + "b" * 64,
        context_digest=None,
        context_sources=None,
        route_metadata={"fallback_attempt_index": 0},
    ).ai_job_id
    start_reserved_attempt(consumed.reservation_id, ai_job_id=job_id)
    return binding, registry, consumed.reservation_id, job_id


def _response() -> AIResponse:
    request = AIRequest(task_type=AITaskType.synthesis, prompt="hello")
    cost = actual_registry_cost_usd(
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        input_tokens=3,
        output_tokens=2,
    )
    return AIResponse(
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        request_id=request.request_id,
        text="done",
        finish_reason="stop",
        usage=AIUsage(
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            input_tokens=3,
            output_tokens=2,
            usage_source=AIUsageSource.actual,
            provider_cost_estimate=cost,
            currency="USD",
        ),
        external_dispatch_state=AIExternalDispatchState.started,
    )


def test_reserved_external_attempt_reconciles_and_links_atomically(monkeypatch, tmp_path) -> None:
    _initialize(monkeypatch, tmp_path)
    binding, registry, reservation_id, job_id = _started_reservation()
    flow = create_flow(task_kind="synthesis", requested_route_class="external:cheap")

    result = finalize_external_attempt(
        flow_id=str(flow["id"]),
        ai_job_id=job_id,
        binding=binding,
        fallback_index=0,
        status="success",
        response=_response(),
        latency_ms=2,
        error_type=None,
        adapter_invoked=True,
        dispatch_state=AIExternalDispatchState.started,
        requested_output_ceiling=32,
        effective_output_ceiling=32,
        outcome_reason="success",
        reservation_id=reservation_id,
        registry=registry,
    )

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        job = connection.execute("SELECT * FROM ai_jobs WHERE id = ?", (job_id,)).fetchone()
        reservation = connection.execute(
            "SELECT * FROM egress_budget_reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
        egress_attempt = connection.execute(
            "SELECT * FROM egress_attempts WHERE ai_job_id = ?", (job_id,)
        ).fetchone()
    assert result.reconciliation_status == "actual"
    assert (result.input_tokens, result.output_tokens) == (3, 2)
    assert job["flow_id"] == flow["id"]
    assert job["execution_class"] == "external_provider"
    assert job["external_dispatch_state"] == "started"
    assert job["normalized_usage_source"] == "actual"
    assert job["accounting_basis"] == "provider_exact"
    assert float(job["accounted_provider_spend_usd_decimal"]) > 0
    assert reservation["state"] == "reconciled"
    assert egress_attempt["reconciliation_status"] == "actual"


def test_flow_link_failure_rolls_back_job_and_reconciliation(monkeypatch, tmp_path) -> None:
    _initialize(monkeypatch, tmp_path)
    binding, registry, reservation_id, job_id = _started_reservation()
    wrong_flow = create_flow(task_kind="code_review", requested_route_class="external:cheap")

    with pytest.raises(TokenFlowConflictError, match="task kind"):
        finalize_external_attempt(
            flow_id=str(wrong_flow["id"]),
            ai_job_id=job_id,
            binding=binding,
            fallback_index=0,
            status="success",
            response=_response(),
            latency_ms=2,
            error_type=None,
            adapter_invoked=True,
            dispatch_state=AIExternalDispatchState.started,
            requested_output_ceiling=32,
            effective_output_ceiling=32,
            outcome_reason="success",
            reservation_id=reservation_id,
            registry=registry,
        )

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        job = connection.execute("SELECT * FROM ai_jobs WHERE id = ?", (job_id,)).fetchone()
        reservation = connection.execute(
            "SELECT * FROM egress_budget_reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
        attempt_count = connection.execute(
            "SELECT COUNT(*) AS n FROM egress_attempts WHERE ai_job_id = ?", (job_id,)
        ).fetchone()["n"]
    assert job["status"] == "queued"
    assert job["flow_id"] is None
    assert job["fallback_index"] is None
    assert reservation["state"] == "in_flight"
    assert attempt_count == 0


def test_confirmation_pricing_snapshot_is_rejected_for_dispatched_attempt(
    monkeypatch, tmp_path
) -> None:
    _initialize(monkeypatch, tmp_path)
    binding, registry, reservation_id, job_id = _started_reservation()
    flow = create_flow(task_kind="synthesis", requested_route_class="external:cheap")

    with pytest.raises(EgressContractError, match="confirmation pricing snapshot"):
        finalize_external_attempt(
            flow_id=str(flow["id"]),
            ai_job_id=job_id,
            binding=binding,
            fallback_index=0,
            status="success",
            response=_response(),
            latency_ms=2,
            error_type=None,
            adapter_invoked=True,
            dispatch_state=AIExternalDispatchState.started,
            requested_output_ceiling=32,
            effective_output_ceiling=32,
            outcome_reason="success",
            reservation_id=reservation_id,
            registry=registry,
            use_confirmation_pricing_snapshot=True,
        )


def test_generic_queued_job_cannot_use_confirmation_pricing_snapshot(
    monkeypatch, tmp_path
) -> None:
    _initialize(monkeypatch, tmp_path)
    registry = load_default_provider_registry()
    binding = registry.bindings["external:cheap"]
    job_id = create_queued_ai_job(
        task_kind="synthesis",
        requested_route_class="external:cheap",
        selected_route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        decision_reason="ordinary-external-job",
        prompt_digest="sha256:" + "c" * 64,
        context_digest=None,
        context_sources=None,
        route_metadata={"fallback_attempt_index": 0},
    ).ai_job_id
    flow = create_flow(task_kind="synthesis", requested_route_class="external:cheap")

    with pytest.raises(EgressContractError, match="snapshot ticket is missing"):
        finalize_external_attempt(
            flow_id=str(flow["id"]),
            ai_job_id=job_id,
            binding=binding,
            fallback_index=0,
            status="config_error",
            response=None,
            latency_ms=1,
            error_type="config_error",
            adapter_invoked=False,
            dispatch_state=AIExternalDispatchState.not_started,
            requested_output_ceiling=32,
            effective_output_ceiling=32,
            outcome_reason="external_not_sent",
            registry=registry,
            use_confirmation_pricing_snapshot=True,
        )

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        job = connection.execute(
            "SELECT status, flow_id, fallback_index FROM ai_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        linked = connection.execute(
            "SELECT COUNT(*) AS n FROM ai_jobs WHERE flow_id = ?",
            (flow["id"],),
        ).fetchone()["n"]
    assert tuple(job) == ("queued", None, None)
    assert linked == 0


def test_mismatched_confirmation_snapshot_metadata_rolls_back(
    monkeypatch, tmp_path
) -> None:
    _initialize(monkeypatch, tmp_path)
    registry = load_default_provider_registry()
    binding = registry.bindings["external:cheap"]
    preparation = prepare_egress_attempt(_material(), registry=registry)
    assert preparation.ticket_id is not None

    providers = dict(registry.providers)
    providers["deepseek"] = replace(providers["deepseek"], enabled=False)
    revoked_registry = replace(
        registry,
        providers=providers,
        bindings={
            route: candidate
            for route, candidate in registry.bindings.items()
            if candidate.provider_id != "deepseek"
        },
    )
    consumed = consume_confirmation_ticket(
        preparation.ticket_id, registry=revoked_registry
    )
    assert consumed.authorized is False
    assert consumed.reason_code == "ticket_binding_or_policy_drift"

    job_id = create_queued_ai_job(
        task_kind="synthesis",
        requested_route_class="external:cheap",
        selected_route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        decision_reason=f"confirmed_ticket:{consumed.ticket_id}",
        prompt_digest="sha256:" + "d" * 64,
        context_digest=None,
        context_sources=None,
        route_metadata={
            "egress_confirmation_ticket_id": consumed.ticket_id,
            "egress_decision_id": "forged-decision",
            "egress_packet_digest": consumed.packet_digest,
            "fallback_attempt_index": consumed.fallback_index,
            "fallback_chain_route": consumed.route_class,
            "fallback_model_id": consumed.model_id,
            "fallback_provider_id": consumed.provider_id,
        },
    ).ai_job_id
    flow = create_flow(task_kind="synthesis", requested_route_class="external:cheap")

    with pytest.raises(EgressContractError, match="metadata mismatch"):
        finalize_external_attempt(
            flow_id=str(flow["id"]),
            ai_job_id=job_id,
            binding=binding,
            fallback_index=0,
            status="config_error",
            response=None,
            latency_ms=1,
            error_type="config_error",
            adapter_invoked=False,
            dispatch_state=AIExternalDispatchState.not_started,
            requested_output_ceiling=32,
            effective_output_ceiling=32,
            outcome_reason="ticket_binding_or_policy_drift",
            registry=revoked_registry,
            use_confirmation_pricing_snapshot=True,
        )

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        job = connection.execute(
            "SELECT status, flow_id, fallback_index FROM ai_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        linked = connection.execute(
            "SELECT COUNT(*) AS n FROM ai_jobs WHERE flow_id = ?",
            (flow["id"],),
        ).fetchone()["n"]
    assert tuple(job) == ("queued", None, None)
    assert linked == 0
