from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.contracts import (
    AIExternalDispatchState,
    AIRequest,
    AIResponse,
    AITaskType,
    AIUsage,
    AIUsageSource,
)
from app.modules.ai.egress_lifecycle import (
    consume_confirmation_ticket,
    reconcile_reserved_attempt,
    start_reserved_attempt,
)
from app.modules.ai.egress_persistence import prepare_egress_attempt
from app.modules.ai.egress_policy import EXTERNAL_PROVIDER_OPERATION
from app.modules.ai.egress_runtime import run_external_task
from app.modules.ai.egress_service import EgressPacketMaterial
from app.modules.ai.egress_spine import create_queued_ai_job, finalize_queued_ai_job
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.models import AISettingsUpdate
from app.modules.ai.provider_registry import load_default_provider_registry, resolve_model_pricing
from app.modules.ai.settings import ensure_ai_settings, update_ai_settings
from app.modules.ai.usage_cost import actual_registry_cost_usd
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"
NOW = datetime(2026, 7, 14, 21, 0, tzinfo=UTC)
BINDING = ProviderBinding(
    route_class="external:cheap",
    provider_id="deepseek",
    model_id="deepseek-v4-pro",
    requires_network=True,
    max_output_tokens=2048,
)


class CountingAdapter:
    provider_id = "deepseek"

    def __init__(
        self,
        *,
        text: str = "Generic answer.",
        provider_id: str = "deepseek",
        model_id: str = "deepseek-v4-pro",
    ) -> None:
        self.text = text
        self.response_provider_id = provider_id
        self.response_model_id = model_id
        self.calls = 0
        self.requests: list[AIRequest] = []

    def complete(self, request: AIRequest) -> AIResponse:
        self.calls += 1
        self.requests.append(request)
        pricing = resolve_model_pricing(load_default_provider_registry(), self.response_provider_id, self.response_model_id)
        return AIResponse(
            provider_id=self.response_provider_id,
            model_id=self.response_model_id,
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            text=self.text,
            content=self.text,
            usage=AIUsage(
                provider_id=self.response_provider_id,
                model_id=self.response_model_id,
                input_tokens=11,
                output_tokens=5,
                usage_source=AIUsageSource.actual,
                provider_cost_estimate=(11 * pricing.input_usd_per_1m_tokens + 5 * pricing.output_usd_per_1m_tokens) / 1_000_000,
                currency="USD",
            ),
            finish_reason="stop",
            safety_status="allowed",
            external_dispatch_state=AIExternalDispatchState.started,
        )

    def health(self):  # pragma: no cover - protocol method unused here
        raise NotImplementedError

    def list_models(self):  # pragma: no cover - protocol method unused here
        raise NotImplementedError

    def stream(self, request: AIRequest):  # pragma: no cover - protocol method unused
        raise NotImplementedError


def _bootstrap(monkeypatch) -> None:
    initialize_database()
    ensure_ai_settings()
    update_ai_settings(
        AISettingsUpdate(
            policy_mode="FAST_DEV",
            monthly_api_budget_usd=100.0,
            paid_ai_enabled=True,
            provider_mode="deepseek",
        )
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    with open_sqlite_connection() as connection:
        now = utc_now()
        connection.execute(
            """
            INSERT OR IGNORE INTO workspaces (
                id, name, slug, description, status, created_at, updated_at
            ) VALUES (?, 'Zephyr', 'main', NULL, 'active', ?, ?)
            """,
            (WORKSPACE_ID, now, now),
        )
        connection.commit()


def _task_type(_task_kind: str) -> AITaskType:
    return AITaskType.synthesis


def _run(
    adapter: CountingAdapter,
    *,
    prompt: str = "Explain a generic pump sizing method.",
    context_blocks=None,
    workspace_id: str | None = None,
):
    return run_external_task(
        user_prompt=prompt,
        task_kind="general",
        selected_route_class="external:cheap",
        requested_route_class="external:cheap",
        context_blocks=context_blocks,
        max_output_tokens=128,
        adapters={"deepseek": adapter},
        bindings={"external:cheap": BINDING},
        workspace_id=workspace_id,
        context_build_error=None,
        external_blocked_reason=None,
        task_type_for=_task_type,
    )


def _seed_prior_network_attempt() -> None:
    material = EgressPacketMaterial(
        operation=EXTERNAL_PROVIDER_OPERATION,
        task_kind="general",
        route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        fallback_index=0,
        prompt="Seed a prior generic provider attempt.",
        context_blocks=(),
        prompt_level="S1",
        context_level="S0",
        final_level="S1",
        max_output_tokens=32,
    )
    preparation = prepare_egress_attempt(material, now=NOW)
    assert preparation.ticket_id is not None
    consumed = consume_confirmation_ticket(preparation.ticket_id, now=NOW)
    assert consumed.authorized is True
    queued = create_queued_ai_job(
        task_kind="general",
        requested_route_class="external:cheap",
        selected_route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        decision_reason="seed prior attempt",
        prompt_digest=canonical_digest({"prompt": material.prompt}),
        now=NOW,
    )
    start_reserved_attempt(
        consumed.reservation_id or "",
        ai_job_id=queued.ai_job_id,
        now=NOW,
    )
    response = AIResponse(
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        request_id=str(uuid4()),
        correlation_id=str(uuid4()),
        text="Seed response.",
        content="Seed response.",
        usage=AIUsage(
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            input_tokens=1,
            output_tokens=1,
            usage_source=AIUsageSource.actual,
            provider_cost_estimate=actual_registry_cost_usd(
                provider_id="deepseek", model_id="deepseek-v4-pro",
                input_tokens=1, output_tokens=1,
            ),
            currency="USD",
        ),
        finish_reason="stop",
        safety_status="allowed",
        external_dispatch_state=AIExternalDispatchState.started,
    )
    finalize_queued_ai_job(
        queued.ai_job_id,
        status="success",
        response=response,
        latency_ms=1,
    )
    reconcile_reserved_attempt(
        consumed.reservation_id or "",
        ai_job_id=queued.ai_job_id,
        network_attempt=True,
        actual_input_tokens=1,
        actual_output_tokens=1,
        usage_source="actual",
        now=NOW,
    )


def test_first_external_use_returns_ticket_and_makes_zero_adapter_calls(monkeypatch):
    _bootstrap(monkeypatch)
    adapter = CountingAdapter()

    outcome = _run(adapter)

    assert outcome.status == "validation_error"
    assert outcome.egress_reason_code == "confirmation_required"
    assert outcome.egress_ticket_id is not None
    assert outcome.egress_packet_digest is not None
    assert outcome.flow_id is not None
    assert adapter.calls == 0
    with open_sqlite_connection() as connection:
        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) AS count FROM {table}"
            ).fetchone()["count"]
            for table in (
                "ai_jobs",
                "egress_packets",
                "egress_decisions",
                "egress_confirmation_tickets",
                "egress_budget_reservations",
            )
        }
        job = connection.execute(
            "SELECT status, flow_id, execution_class, adapter_invoked, "
            "external_dispatch_state, accounting_basis FROM ai_jobs"
        ).fetchone()
        flow = connection.execute(
            "SELECT state, terminal_attempt_id FROM ai_flows WHERE id = ?",
            (outcome.flow_id,),
        ).fetchone()
    assert counts == {
        "ai_jobs": 1,
        "egress_packets": 1,
        "egress_decisions": 1,
        "egress_confirmation_tickets": 1,
        "egress_budget_reservations": 0,
    }
    assert job["status"] == "validation_error"
    assert job["flow_id"] == outcome.flow_id
    assert job["execution_class"] == "external_provider"
    assert job["adapter_invoked"] == 0
    assert job["external_dispatch_state"] == "not_started"
    assert job["accounting_basis"] == "external_not_sent"
    assert flow["state"] == "confirmation_required"
    assert flow["terminal_attempt_id"] is None


def test_secret_prompt_is_denied_prepacket_with_zero_adapter_calls(monkeypatch):
    _bootstrap(monkeypatch)
    adapter = CountingAdapter()
    secret = "api_key=super-secret-value"

    outcome = _run(adapter, prompt=secret)

    assert outcome.status == "config_error"
    assert outcome.egress_reason_code == "prompt_secret_detected"
    assert outcome.egress_packet_digest is None
    assert adapter.calls == 0
    with open_sqlite_connection() as connection:
        decision = connection.execute("SELECT * FROM egress_decisions").fetchone()
        packet_count = connection.execute(
            "SELECT COUNT(*) AS count FROM egress_packets"
        ).fetchone()["count"]
    assert decision["packet_id"] is None
    assert decision["packet_digest"] is None
    assert secret not in str(dict(decision))
    assert packet_count == 0


def test_arbitrary_manual_context_is_paused_before_packet(monkeypatch):
    _bootstrap(monkeypatch)
    adapter = CountingAdapter()
    raw_context = [
        {
            "source": "caller:inline",
            "content": "Zephyr proprietary geometry from caller body.",
        }
    ]

    outcome = _run(
        adapter,
        context_blocks=raw_context,
        workspace_id=WORKSPACE_ID,
    )

    assert outcome.status == "validation_error"
    assert outcome.egress_reason_code == "manual_context_not_authorized"
    assert outcome.egress_packet_digest is None
    assert adapter.calls == 0
    with open_sqlite_connection() as connection:
        packet_count = connection.execute(
            "SELECT COUNT(*) AS count FROM egress_packets"
        ).fetchone()["count"]
    assert packet_count == 0


def test_silent_allow_uses_persisted_packet_and_reconciles_one_call(monkeypatch):
    _bootstrap(monkeypatch)
    _seed_prior_network_attempt()
    adapter = CountingAdapter(text="Bound generic answer.")
    prompt = "Explain a generic compressor efficiency calculation."

    outcome = _run(adapter, prompt=prompt)

    assert outcome.status == "success"
    assert outcome.egress_reason_code == "silent_allow"
    assert outcome.egress_reservation_id is not None
    assert outcome.flow_id is not None
    assert adapter.calls == 1
    assert adapter.requests[0].prompt == prompt
    assert adapter.requests[0].metadata["egress_packet_digest"] == outcome.egress_packet_digest
    with open_sqlite_connection() as connection:
        job = connection.execute(
            "SELECT status, output_digest, flow_id, execution_class, adapter_invoked, "
            "external_dispatch_state, normalized_usage_source, accounting_basis, "
            "accounted_provider_spend_usd_decimal FROM ai_jobs WHERE id = ?",
            (outcome.ledger_id,),
        ).fetchone()
        reservation = connection.execute(
            """
            SELECT state, reconciliation_status, actual_input_tokens,
                   actual_output_tokens
            FROM egress_budget_reservations WHERE id = ?
            """,
            (outcome.egress_reservation_id,),
        ).fetchone()
        attempt = connection.execute(
            "SELECT network_attempt, ai_job_id FROM egress_attempts WHERE ai_job_id = ?",
            (outcome.ledger_id,),
        ).fetchone()
        packet = connection.execute(
            "SELECT packet_json FROM egress_packets WHERE packet_digest = ?",
            (outcome.egress_packet_digest,),
        ).fetchone()
        flow = connection.execute(
            "SELECT state, terminal_attempt_id FROM ai_flows WHERE id = ?",
            (outcome.flow_id,),
        ).fetchone()
    assert job["status"] == "success"
    assert job["output_digest"] == canonical_digest({"text": "Bound generic answer."})
    assert job["flow_id"] == outcome.flow_id
    assert job["execution_class"] == "external_provider"
    assert job["adapter_invoked"] == 1
    assert job["external_dispatch_state"] == "started"
    assert job["normalized_usage_source"] == "actual"
    assert job["accounting_basis"] == "provider_exact"
    assert float(job["accounted_provider_spend_usd_decimal"]) > 0
    assert flow["state"] == "complete"
    assert flow["terminal_attempt_id"] == outcome.ledger_id
    assert reservation["state"] == "reconciled"
    assert reservation["reconciliation_status"] == "actual"
    assert reservation["actual_input_tokens"] == 11
    assert reservation["actual_output_tokens"] == 5
    assert attempt["network_attempt"] == 1
    assert attempt["ai_job_id"] == outcome.ledger_id
    assert prompt in packet["packet_json"]


def test_response_binding_mismatch_is_recorded_and_reconciled_conservatively(monkeypatch):
    _bootstrap(monkeypatch)
    _seed_prior_network_attempt()
    adapter = CountingAdapter(provider_id="glm", model_id="glm-5.2")

    outcome = _run(adapter)

    assert outcome.status == "provider_error"
    assert outcome.egress_reason_code == "response_binding_mismatch"
    assert adapter.calls == 1
    with open_sqlite_connection() as connection:
        job = connection.execute(
            "SELECT status, output_digest, error_type, flow_id, external_dispatch_state, "
            "normalized_usage_source, accounting_basis, "
            "accounted_provider_spend_usd_decimal FROM ai_jobs WHERE id = ?",
            (outcome.ledger_id,),
        ).fetchone()
        reservation = connection.execute(
            """
            SELECT state, reconciliation_status, actual_input_tokens,
                   actual_output_tokens, projected_input_tokens,
                   projected_output_tokens
            FROM egress_budget_reservations WHERE id = ?
            """,
            (outcome.egress_reservation_id,),
        ).fetchone()
    assert job["status"] == "provider_error"
    assert job["output_digest"] is None
    assert job["error_type"] == "EgressSpineStateError"
    assert job["flow_id"] == outcome.flow_id
    assert job["external_dispatch_state"] == "unknown"
    assert job["normalized_usage_source"] == "estimated"
    assert job["accounting_basis"] == "conservative_estimated_usage"
    assert float(job["accounted_provider_spend_usd_decimal"]) > 0
    assert reservation["state"] == "reconciled"
    assert reservation["reconciliation_status"] == "conservative_missing_usage"
    assert reservation["actual_input_tokens"] == reservation["projected_input_tokens"]
    assert reservation["actual_output_tokens"] == reservation["projected_output_tokens"]



def test_length_response_is_recorded_as_partial_terminal(monkeypatch):
    _bootstrap(monkeypatch)
    _seed_prior_network_attempt()
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_settings SET max_direct_continuations = 0 WHERE id = 'default'"
        )
        connection.commit()

    class LengthAdapter(CountingAdapter):
        def complete(self, request: AIRequest) -> AIResponse:
            response = super().complete(request)
            return response.model_copy(update={"finish_reason": "length"})

    adapter = LengthAdapter(text="Truncated provider answer.")
    outcome = _run(adapter)

    assert outcome.status == "success"
    assert adapter.calls == 1
    with open_sqlite_connection() as connection:
        flow = connection.execute(
            "SELECT state, terminal_reason, terminal_attempt_id FROM ai_flows WHERE id = ?",
            (outcome.flow_id,),
        ).fetchone()
    assert flow["state"] == "partial_terminal"
    assert flow["terminal_reason"] == "continuation_guard_exhausted"
    assert flow["terminal_attempt_id"] == outcome.ledger_id


def test_expired_reservation_start_failure_finalizes_job_and_flow(monkeypatch):
    _bootstrap(monkeypatch)
    _seed_prior_network_attempt()
    from app.modules.ai import egress_runtime

    real_start = egress_runtime.start_reserved_attempt

    def expire_before_start(reservation_id: str, *, ai_job_id: str):
        return real_start(
            reservation_id,
            ai_job_id=ai_job_id,
            now=datetime(2100, 1, 1, tzinfo=UTC),
        )

    monkeypatch.setattr(egress_runtime, "start_reserved_attempt", expire_before_start)
    adapter = CountingAdapter()
    outcome = _run(adapter)

    assert outcome.status == "config_error"
    assert outcome.egress_reason_code == "egress_start_failed"
    assert adapter.calls == 0
    with open_sqlite_connection() as connection:
        reservation = connection.execute(
            "SELECT state, reconciliation_status FROM egress_budget_reservations WHERE id = ?",
            (outcome.egress_reservation_id,),
        ).fetchone()
        job = connection.execute(
            """
            SELECT status, flow_id, execution_class, adapter_invoked,
                   external_dispatch_state, normalized_usage_source, accounting_basis
            FROM ai_jobs WHERE id = ?
            """,
            (outcome.ledger_id,),
        ).fetchone()
        flow = connection.execute(
            "SELECT state, terminal_reason, terminal_attempt_id FROM ai_flows WHERE id = ?",
            (outcome.flow_id,),
        ).fetchone()
    assert reservation["state"] == "expired"
    assert reservation["reconciliation_status"] == "expired_before_start"
    assert job["status"] == "config_error"
    assert job["flow_id"] == outcome.flow_id
    assert job["execution_class"] == "external_provider"
    assert job["adapter_invoked"] == 0
    assert job["external_dispatch_state"] == "not_started"
    assert job["normalized_usage_source"] == "none"
    assert job["accounting_basis"] == "external_not_sent"
    assert flow["state"] == "failed_terminal"
    assert flow["terminal_reason"] == "egress_start_failed"
    assert flow["terminal_attempt_id"] == outcome.ledger_id
