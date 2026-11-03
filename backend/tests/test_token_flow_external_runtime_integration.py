from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest

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
from app.modules.ai.settings import ensure_ai_settings, update_ai_settings
from app.modules.ai.token_flow_service import get_flow
from app.modules.ai.usage_cost import actual_registry_cost_usd
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"
NOW = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)
BINDING = ProviderBinding(
    route_class="external:cheap",
    provider_id="deepseek",
    model_id="deepseek-v4-pro",
    requires_network=True,
    max_output_tokens=128,
    execution_class="external_provider",
    context_window_tokens=4096,
)
UNKNOWN_RECORD_OUTPUT = (
    "decision body before record\n"
    "```jarvis-records\n"
    '{"record_version":"jarvis_records_v0","records":['
    '{"record_kind":"decision","title":"Must not capture",'
    '"decision_text":"Unknown finish is not complete"}]}\n'
    "```"
)


class SequenceExternalAdapter:
    provider_id = "deepseek"

    def __init__(
        self,
        *,
        final_finish_reason: str | None = "stop",
        final_text: str = "external omega",
    ) -> None:
        self.requests: list[AIRequest] = []
        self.final_finish_reason = final_finish_reason
        self.final_text = final_text

    def complete(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)
        index = len(self.requests)
        text, finish_reason = (
            ("external alpha ", "length")
            if index == 1
            else (self.final_text, self.final_finish_reason)
        )
        return AIResponse(
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            text=text,
            content=text,
            usage=AIUsage(
                provider_id="deepseek",
                model_id="deepseek-v4-pro",
                input_tokens=20 + index,
                output_tokens=5 + index,
                usage_source=AIUsageSource.actual,
                provider_cost_estimate=actual_registry_cost_usd(
                    provider_id="deepseek", model_id="deepseek-v4-pro",
                    input_tokens=20 + index, output_tokens=5 + index,
                ),
                currency="USD",
            ),
            finish_reason=finish_reason,
            safety_status="allowed",
            external_dispatch_state=AIExternalDispatchState.started,
        )

    def health(self):  # pragma: no cover
        raise NotImplementedError

    def list_models(self):  # pragma: no cover
        raise NotImplementedError

    def stream(self, request: AIRequest):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def initialized_database(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    from app.core.config import get_settings

    get_settings.cache_clear()
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
    _seed_prior_network_attempt()
    yield
    get_settings.cache_clear()


def _task_type(_task_kind: str) -> AITaskType:
    return AITaskType.synthesis


def _seed_prior_network_attempt() -> None:
    material = EgressPacketMaterial(
        operation=EXTERNAL_PROVIDER_OPERATION,
        task_kind="general",
        route_class=BINDING.route_class,
        provider_id=BINDING.provider_id,
        model_id=BINDING.model_id,
        fallback_index=0,
        prompt="Seed prior confirmed provider use.",
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
        requested_route_class=BINDING.route_class,
        selected_route_class=BINDING.route_class,
        provider_id=BINDING.provider_id,
        model_id=BINDING.model_id,
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
        provider_id=BINDING.provider_id,
        model_id=BINDING.model_id,
        request_id=str(uuid4()),
        correlation_id=str(uuid4()),
        text="Seed response.",
        content="Seed response.",
        usage=AIUsage(
            provider_id=BINDING.provider_id,
            model_id=BINDING.model_id,
            input_tokens=1,
            output_tokens=1,
            usage_source=AIUsageSource.actual,
            provider_cost_estimate=25.0 / 1_000_000,
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


def test_silent_allow_length_runs_fresh_external_continuation(
    initialized_database,
) -> None:
    adapter = SequenceExternalAdapter()
    original_prompt = "Explain a generic compressor sizing method."

    outcome = run_external_task(
        user_prompt=original_prompt,
        task_kind="general",
        selected_route_class=BINDING.route_class,
        requested_route_class=BINDING.route_class,
        context_blocks=None,
        max_output_tokens=64,
        adapters={BINDING.provider_id: adapter},
        bindings={BINDING.route_class: BINDING},
        workspace_id=WORKSPACE_ID,
        context_build_error=None,
        external_blocked_reason=None,
        task_type_for=_task_type,
    )

    assert outcome.status == "success"
    assert outcome.response is not None
    assert outcome.response.text == "external alpha external omega"
    assert len(adapter.requests) == 2
    assert "VALIDATED_PARTIAL_OUTPUT" in (adapter.requests[1].prompt or "")
    assert "external alpha " in (adapter.requests[1].prompt or "")

    flow_id = str(outcome.flow_id)
    flow = get_flow(flow_id)
    assert flow["state"] == "complete"
    assert flow["attempt_count"] == 2
    assert flow["continuation_count"] == 1
    assert flow["final_output_digest"] == canonical_digest(
        {"text": "external alpha external omega"}
    )
    assert float(flow["external_provider_spend_usd_decimal"]) > 0


    with open_sqlite_connection() as connection:
        attempts = connection.execute(
            """
            SELECT id, flow_attempt_index, parent_attempt_id, continuation_index,
                   external_dispatch_state
            FROM ai_jobs
            WHERE flow_id = ?
            ORDER BY flow_attempt_index
            """,
            (flow_id,),
        ).fetchall()
        packets = connection.execute(
            """
            SELECT packet_json, source_digests_json
            FROM egress_packets
            WHERE task_kind = 'general' AND route_class = ?
            ORDER BY created_at
            """,
            (BINDING.route_class,),
        ).fetchall()
        reservations = connection.execute(
            """
            SELECT state, reconciliation_status
            FROM egress_budget_reservations
            WHERE provider_id = ? AND model_id = ?
            ORDER BY created_at
            """,
            (BINDING.provider_id, BINDING.model_id),
        ).fetchall()

    flow_attempts = [row for row in attempts if row["flow_attempt_index"] is not None]
    assert [row["flow_attempt_index"] for row in flow_attempts] == [0, 1]
    assert flow_attempts[1]["parent_attempt_id"] == flow_attempts[0]["id"]
    assert flow_attempts[1]["continuation_index"] == 1
    assert all(row["external_dispatch_state"] == "started" for row in flow_attempts)

    runtime_packets = [
        row for row in packets if original_prompt in json.loads(row["packet_json"])["prompt"]
    ]
    assert len(runtime_packets) == 2
    continuation_sources = json.loads(runtime_packets[1]["source_digests_json"])
    assert continuation_sources["segment:0"] == canonical_digest(
        {"text": "external alpha "}
    )
    assert [row["state"] for row in reservations[-2:]] == ["reconciled", "reconciled"]
    assert [row["reconciliation_status"] for row in reservations[-2:]] == [
        "actual",
        "actual",
    ]


def test_external_unknown_finish_is_partial_without_hidden_retry_or_capture(
    initialized_database,
) -> None:
    adapter = SequenceExternalAdapter(
        final_finish_reason=None,
        final_text=UNKNOWN_RECORD_OUTPUT,
    )

    outcome = run_external_task(
        user_prompt="Return one candidate decision.",
        task_kind="decision_support",
        selected_route_class=BINDING.route_class,
        requested_route_class=BINDING.route_class,
        context_blocks=None,
        max_output_tokens=64,
        adapters={BINDING.provider_id: adapter},
        bindings={BINDING.route_class: BINDING},
        workspace_id=WORKSPACE_ID,
        context_build_error=None,
        external_blocked_reason=None,
        task_type_for=_task_type,
    )

    assert outcome.status == "success"
    assert outcome.response is not None
    assert outcome.response.text == "external alpha " + UNKNOWN_RECORD_OUTPUT
    assert len(adapter.requests) == 2
    flow = get_flow(str(outcome.flow_id))
    assert flow["state"] == "partial_terminal"
    assert flow["terminal_reason"] == "continuation_finish_unknown"

    with open_sqlite_connection() as connection:
        proposals = connection.execute(
            "SELECT COUNT(*) AS count FROM decisions WHERE origin = 'ai_proposed'"
        ).fetchone()
    assert proposals["count"] == 0


class PauseAfterLengthAdapter(SequenceExternalAdapter):
    def complete(self, request: AIRequest) -> AIResponse:
        response = super().complete(request)
        if len(self.requests) == 1:
            with open_sqlite_connection() as connection:
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO workspace_egress_policy (
                        workspace_id, ask_me, created_at, updated_at, updated_by
                    ) VALUES (?, 1, ?, ?, 'test')
                    ON CONFLICT(workspace_id) DO UPDATE SET
                        ask_me = 1, updated_at = excluded.updated_at,
                        updated_by = excluded.updated_by
                    """,
                    (WORKSPACE_ID, now, now),
                )
                connection.commit()
        return response


class ConfirmedExternalAdapter:
    provider_id = "deepseek"

    def __init__(
        self,
        *,
        text: str = "external omega",
        finish_reason: str | None = "stop",
    ) -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.requests: list[AIRequest] = []

    def complete(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)
        return AIResponse(
            provider_id=BINDING.provider_id,
            model_id=BINDING.model_id,
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            text=self.text,
            content=self.text,
            usage=AIUsage(
                provider_id=BINDING.provider_id,
                model_id=BINDING.model_id,
                input_tokens=31,
                output_tokens=9,
                usage_source=AIUsageSource.actual,
                provider_cost_estimate=actual_registry_cost_usd(
                    provider_id="deepseek", model_id="deepseek-v4-pro",
                    input_tokens=31, output_tokens=9,
                ),
                currency="USD",
            ),
            finish_reason=self.finish_reason,
            safety_status="allowed",
            external_dispatch_state=AIExternalDispatchState.started,
        )

    def health(self):  # pragma: no cover
        raise NotImplementedError

    def list_models(self):  # pragma: no cover
        raise NotImplementedError

    def stream(self, request: AIRequest):  # pragma: no cover
        raise NotImplementedError


def _pause_external_continuation(*, task_kind: str = "general"):
    adapter = PauseAfterLengthAdapter()
    outcome = run_external_task(
        user_prompt="Return a bounded external answer.",
        task_kind=task_kind,
        selected_route_class=BINDING.route_class,
        requested_route_class=BINDING.route_class,
        context_blocks=None,
        max_output_tokens=64,
        adapters={BINDING.provider_id: adapter},
        bindings={BINDING.route_class: BINDING},
        workspace_id=WORKSPACE_ID,
        context_build_error=None,
        external_blocked_reason=None,
        task_type_for=_task_type,
    )
    assert outcome.status == "validation_error"
    assert outcome.egress_reason_code == "confirmation_required"
    assert outcome.egress_ticket_id is not None
    assert outcome.flow_id is not None
    assert len(adapter.requests) == 1
    flow = get_flow(str(outcome.flow_id))
    assert flow["state"] == "confirmation_required"
    assert flow["attempt_count"] == 2
    assert flow["continuation_count"] == 1

    from app.modules.ai.token_flow_confirmation_resume import (
        validate_pending_continuation_confirmation_in_transaction,
    )

    with open_sqlite_connection() as connection:
        ticket = connection.execute(
            "SELECT continuation_authority_json FROM egress_confirmation_tickets WHERE id = ?",
            (outcome.egress_ticket_id,),
        ).fetchone()
        validate_pending_continuation_confirmation_in_transaction(
            connection,
            flow_id=str(outcome.flow_id),
            ticket_id=str(outcome.egress_ticket_id),
            authority_json=str(ticket["continuation_authority_json"]),
        )
    return outcome


def test_paused_external_continuation_resumes_after_restart_exactly_once(
    initialized_database,
) -> None:
    from app.core.config import get_settings
    from app.modules.ai.egress_confirmation import run_confirmation_ticket
    from app.modules.ai.provider_registry import load_default_provider_registry

    paused = _pause_external_continuation()
    ticket_id = str(paused.egress_ticket_id)
    flow_id = str(paused.flow_id)
    pause_attempt_id = paused.ledger_id

    get_settings.cache_clear()
    load_default_provider_registry.cache_clear()
    confirmed_adapter = ConfirmedExternalAdapter()
    confirmed = run_confirmation_ticket(
        ticket_id,
        adapters={BINDING.provider_id: confirmed_adapter},
    ).outcome

    assert confirmed.status == "success"
    assert confirmed.response is not None
    assert confirmed.response.text == "external alpha external omega"
    assert len(confirmed_adapter.requests) == 1
    assert "VALIDATED_PARTIAL_OUTPUT" in (confirmed_adapter.requests[0].prompt or "")
    assert "external alpha " in (confirmed_adapter.requests[0].prompt or "")

    flow = get_flow(flow_id)
    assert flow["state"] == "complete"
    assert flow["attempt_count"] == 2
    assert flow["continuation_count"] == 1
    assert flow["final_output_digest"] == canonical_digest(
        {"text": "external alpha external omega"}
    )

    with open_sqlite_connection() as connection:
        linked = connection.execute(
            """
            SELECT id, flow_attempt_index, parent_attempt_id, continuation_index,
                   external_dispatch_state
            FROM ai_jobs WHERE flow_id = ? ORDER BY flow_attempt_index
            """,
            (flow_id,),
        ).fetchall()
        pause = connection.execute(
            "SELECT flow_id, flow_attempt_index, status FROM ai_jobs WHERE id = ?",
            (pause_attempt_id,),
        ).fetchone()
        ticket = connection.execute(
            "SELECT state, continuation_authority_json FROM egress_confirmation_tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
        reservation = connection.execute(
            """
            SELECT state, reconciliation_status
            FROM egress_budget_reservations WHERE id = ?
            """,
            (confirmed.egress_reservation_id,),
        ).fetchone()

    assert [row["flow_attempt_index"] for row in linked] == [0, 1]
    assert linked[1]["id"] == confirmed.ledger_id
    assert linked[1]["parent_attempt_id"] == linked[0]["id"]
    assert linked[1]["continuation_index"] == 1
    assert linked[1]["external_dispatch_state"] == "started"
    assert tuple(pause) == (None, None, "validation_error")
    assert ticket["state"] == "consumed"
    assert ticket["continuation_authority_json"] is not None
    assert tuple(reservation) == ("reconciled", "actual")


def test_consumed_continuation_ticket_replay_never_dispatches_again(
    initialized_database,
) -> None:
    from app.modules.ai.egress_confirmation import run_confirmation_ticket
    from app.modules.ai.egress_persistence import EgressStateError

    paused = _pause_external_continuation()
    ticket_id = str(paused.egress_ticket_id)
    first = ConfirmedExternalAdapter()
    run_confirmation_ticket(
        ticket_id,
        adapters={BINDING.provider_id: first},
    )
    replay = ConfirmedExternalAdapter()

    with pytest.raises(EgressStateError, match="not pending: consumed"):
        run_confirmation_ticket(
            ticket_id,
            adapters={BINDING.provider_id: replay},
        )

    assert len(first.requests) == 1
    assert replay.requests == []


def test_expired_continuation_ticket_terminalizes_pause_without_dispatch(
    initialized_database,
) -> None:
    from app.modules.ai.egress_confirmation import run_confirmation_ticket

    paused = _pause_external_continuation()
    ticket_id = str(paused.egress_ticket_id)
    flow_id = str(paused.flow_id)
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE egress_confirmation_tickets SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", ticket_id),
        )
        connection.commit()
    adapter = ConfirmedExternalAdapter()

    outcome = run_confirmation_ticket(
        ticket_id,
        adapters={BINDING.provider_id: adapter},
    ).outcome

    assert outcome.status == "config_error"
    assert outcome.egress_reason_code == "ticket_expired"
    assert outcome.ledger_id == paused.ledger_id
    assert adapter.requests == []
    flow = get_flow(flow_id)
    assert flow["state"] == "failed_terminal"
    assert flow["terminal_reason"] == "ticket_expired"
    assert flow["terminal_attempt_id"] == paused.ledger_id
    with open_sqlite_connection() as connection:
        ticket = connection.execute(
            "SELECT state FROM egress_confirmation_tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
        pause = connection.execute(
            "SELECT flow_id, flow_attempt_index FROM ai_jobs WHERE id = ?",
            (paused.ledger_id,),
        ).fetchone()
    assert ticket["state"] == "expired"
    assert pause["flow_id"] == flow_id
    assert pause["flow_attempt_index"] == 1


def test_tampered_continuation_authority_revokes_without_dispatch(
    initialized_database,
) -> None:
    from app.modules.ai.egress_confirmation import run_confirmation_ticket

    paused = _pause_external_continuation()
    ticket_id = str(paused.egress_ticket_id)
    flow_id = str(paused.flow_id)
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT continuation_authority_json FROM egress_confirmation_tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
        authority = json.loads(row["continuation_authority_json"])
        authority["parent_attempt_id"] = "tampered-parent"
        connection.execute(
            "UPDATE egress_confirmation_tickets SET continuation_authority_json = ? WHERE id = ?",
            (json.dumps(authority, sort_keys=True, separators=(",", ":")), ticket_id),
        )
        connection.commit()
    adapter = ConfirmedExternalAdapter()

    outcome = run_confirmation_ticket(
        ticket_id,
        adapters={BINDING.provider_id: adapter},
    ).outcome

    assert outcome.status == "config_error"
    assert outcome.egress_reason_code == "ticket_continuation_authority_invalid"
    assert adapter.requests == []
    flow = get_flow(flow_id)
    assert flow["state"] == "failed_terminal"
    assert flow["terminal_reason"] == "ticket_continuation_authority_invalid"
    with open_sqlite_connection() as connection:
        ticket = connection.execute(
            "SELECT state, revocation_reason FROM egress_confirmation_tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
    assert tuple(ticket) == (
        "revoked",
        "ticket_continuation_authority_invalid",
    )


def test_confirmed_unknown_continuation_is_partial_without_record_capture(
    initialized_database,
) -> None:
    from app.modules.ai.egress_confirmation import run_confirmation_ticket

    paused = _pause_external_continuation(task_kind="decision_support")
    adapter = ConfirmedExternalAdapter(
        text=UNKNOWN_RECORD_OUTPUT,
        finish_reason=None,
    )

    outcome = run_confirmation_ticket(
        str(paused.egress_ticket_id),
        adapters={BINDING.provider_id: adapter},
    ).outcome

    assert outcome.status == "success"
    assert outcome.response is not None
    assert outcome.response.text == "external alpha " + UNKNOWN_RECORD_OUTPUT
    assert outcome.proposed_record_ids in (None, [])
    flow = get_flow(str(paused.flow_id))
    assert flow["state"] == "partial_terminal"
    assert flow["terminal_reason"] == "continuation_finish_unknown"
    with open_sqlite_connection() as connection:
        proposals = connection.execute(
            "SELECT COUNT(*) AS count FROM decisions WHERE origin = 'ai_proposed'"
        ).fetchone()
    assert proposals["count"] == 0
