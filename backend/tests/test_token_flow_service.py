from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.modules.ai.token_flow_service import (
    TokenFlowConflictError,
    TokenFlowError,
    create_flow,
    get_flow,
    link_attempt_to_flow,
    recompute_flow_aggregates,
    transition_flow_state,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


@pytest.fixture
def initialized_database():
    from app.core.database import initialize_database
    from app.modules.ai.settings import ensure_ai_settings

    initialize_database()
    ensure_ai_settings()


def _insert_job(
    job_id: str,
    *,
    task_kind: str = "synthesis",
    status: str = "success",
    selected_route_class: str | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    fallback_index: int | None = None,
    route_reason_json: str = "{}",
    execution_class: str | None = "none",
    adapter_invoked: int | None = 0,
    dispatch: str | None = "not_applicable",
    input_tokens: int | None = 0,
    output_tokens: int | None = 0,
    cache_read_tokens: int | None = 0,
    reasoning_tokens: int | None = 0,
    usage_source: str | None = "none",
    latency_ms: int | None = 0,
    accounting_basis: str | None = "no_execution",
    spend: str | None = "0",
    continuation_index: int | None = None,
    output_digest: str | None = None,
    context_sources_json: str | None = None,
) -> None:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import utc_now

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, selected_route_class,
                provider_id, model_id, route_reason_json, fallback_index,
                execution_class, adapter_invoked, external_dispatch_state,
                input_tokens, output_tokens, cache_read_tokens, reasoning_tokens,
                normalized_usage_source, latency_ms, accounting_basis,
                accounted_provider_spend_usd_decimal, continuation_index,
                output_digest, context_sources_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                utc_now(),
                status,
                task_kind,
                selected_route_class,
                provider_id,
                model_id,
                route_reason_json,
                fallback_index,
                execution_class,
                adapter_invoked,
                dispatch,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                reasoning_tokens,
                usage_source,
                latency_ms,
                accounting_basis,
                spend,
                continuation_index,
                output_digest,
                context_sources_json,
            ),
        )
        connection.commit()


def _insert_workspace(workspace_id: str) -> None:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import utc_now

    with open_sqlite_connection() as connection:
        now = utc_now()
        connection.execute(
            """
            INSERT INTO workspaces (id, name, slug, status, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?)
            """,
            (workspace_id, workspace_id, workspace_id, now, now),
        )
        connection.commit()


def _pending_confirmation_ticket(monkeypatch, *, workspace_id: str | None = None):
    from app.modules.ai.egress_persistence import prepare_egress_attempt
    from app.modules.ai.egress_policy import EXTERNAL_PROVIDER_OPERATION
    from app.modules.ai.egress_service import EgressPacketMaterial
    from app.modules.ai.models import AISettingsUpdate
    from app.modules.ai.settings import update_ai_settings

    update_ai_settings(
        AISettingsUpdate(
            policy_mode="FAST_DEV",
            monthly_api_budget_usd=100,
            paid_ai_enabled=True,
            provider_mode="deepseek",
        )
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    if workspace_id is not None:
        _insert_workspace(workspace_id)
    preparation = prepare_egress_attempt(
        EgressPacketMaterial(
            operation=EXTERNAL_PROVIDER_OPERATION,
            task_kind="synthesis",
            route_class="external:cheap",
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            fallback_index=0,
            prompt="Summarize the approved generic pump note.",
            context_blocks=(),
            prompt_level="S1",
            context_level="S0",
            final_level="S1",
            max_output_tokens=128,
            workspace_id=workspace_id,
            included_manifest=(),
            source_digests=(),
        ),
        now=datetime.now(UTC),
    )
    assert preparation.ticket_id is not None
    return preparation


def _link_confirmation_attempt(monkeypatch, *, workspace_id: str | None = None):
    preparation = _pending_confirmation_ticket(monkeypatch, workspace_id=workspace_id)
    flow = create_flow(task_kind="synthesis", workspace_id=workspace_id)
    _insert_job(
        "pause",
        status="validation_error",
        selected_route_class="external:cheap",
        provider_id=preparation.provider_id,
        model_id=preparation.model_id,
        fallback_index=preparation.fallback_index,
        route_reason_json=json.dumps(
            {
                "egress_decision_id": preparation.decision_id,
                "egress_packet_digest": preparation.packet_digest,
                "egress_reason_code": preparation.reason_code,
                "egress_ticket_id": preparation.ticket_id,
                "egress_trigger_ids": list(preparation.trigger_ids),
                "fallback_attempt_index": preparation.fallback_index,
                "fallback_chain_route": "external:cheap",
                "fallback_model_id": preparation.model_id,
                "fallback_provider_id": preparation.provider_id,
            },
            sort_keys=True,
        ),
        execution_class="external_provider",
        adapter_invoked=0,
        dispatch="not_started",
        usage_source="none",
        accounting_basis="external_not_sent",
        spend="0",
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="pause")
    return flow, preparation


def test_create_flow_snapshots_server_owned_continuation_setting(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    first = create_flow(task_kind="synthesis", requested_route_class="local:fake")

    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_settings SET max_direct_continuations = 3 WHERE id = 'default'"
        )
        connection.commit()

    second = create_flow(task_kind="synthesis", requested_route_class="auto")

    assert first["id"] != second["id"]
    assert first["max_direct_continuations_snapshot"] == 8
    assert get_flow(str(first["id"]))["max_direct_continuations_snapshot"] == 8
    assert second["max_direct_continuations_snapshot"] == 3
    with pytest.raises(TypeError):
        create_flow(task_kind="synthesis", max_direct_continuations_snapshot=16)  # type: ignore[call-arg]


def test_link_attempt_is_ordered_idempotent_and_conflict_safe(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    first_flow = create_flow(task_kind="synthesis")
    second_flow = create_flow(task_kind="synthesis")
    _insert_job("job-a")
    _insert_job("job-b")

    link_attempt_to_flow(flow_id=str(first_flow["id"]), attempt_id="job-a")
    linked = link_attempt_to_flow(flow_id=str(first_flow["id"]), attempt_id="job-b")
    replayed = link_attempt_to_flow(flow_id=str(first_flow["id"]), attempt_id="job-a")

    assert linked["ordered_attempt_ids"] == ["job-a", "job-b"]
    assert replayed["ordered_attempt_ids"] == ["job-a", "job-b"]
    assert replayed["attempt_count"] == 2
    with pytest.raises(TokenFlowConflictError):
        link_attempt_to_flow(flow_id=str(second_flow["id"]), attempt_id="job-a")

    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT id, flow_attempt_index FROM ai_jobs ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["flow_attempt_index"]) for row in rows] == [
        ("job-a", 0),
        ("job-b", 1),
    ]


def test_link_attempt_rejects_cross_task_before_link_cas(initialized_database) -> None:
    from app.core.database import open_sqlite_connection

    flow = create_flow(task_kind="synthesis")
    _insert_job("cross-task", task_kind="general")

    with pytest.raises(TokenFlowConflictError, match="task kind"):
        link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="cross-task")

    with open_sqlite_connection() as connection:
        job = connection.execute(
            "SELECT flow_id, flow_attempt_index FROM ai_jobs WHERE id = 'cross-task'"
        ).fetchone()
    assert job["flow_id"] is None
    assert job["flow_attempt_index"] is None
    assert get_flow(str(flow["id"]))["ordered_attempt_ids"] == []


def test_state_transitions_verify_terminal_attempt_and_freeze_terminal_state(
    initialized_database,
) -> None:
    flow = create_flow(task_kind="synthesis")
    other_flow = create_flow(task_kind="synthesis")
    _insert_job(
        "terminal-job",
        execution_class="synthetic",
        adapter_invoked=1,
        usage_source="estimated",
        accounting_basis="synthetic_not_economic",
        output_digest=DIGEST_A,
    )
    _insert_job(
        "other-job",
        execution_class="synthetic",
        adapter_invoked=1,
        usage_source="estimated",
        accounting_basis="synthetic_not_economic",
        output_digest=DIGEST_B,
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="terminal-job")
    link_attempt_to_flow(flow_id=str(other_flow["id"]), attempt_id="other-job")

    with pytest.raises(TokenFlowConflictError, match="must belong"):
        transition_flow_state(
            flow_id=str(flow["id"]),
            new_state="complete",
            terminal_reason="completed",
            terminal_attempt_id="other-job",
        )

    complete = transition_flow_state(
        flow_id=str(flow["id"]),
        new_state="complete",
        terminal_reason="completed",
        terminal_attempt_id="terminal-job",
    )

    assert complete["state"] == "complete"
    assert complete["completed_at"] is not None
    assert complete["final_accounting_digest"] is not None
    assert complete["final_output_digest"] is not None
    with pytest.raises(TokenFlowConflictError, match="immutable"):
        transition_flow_state(flow_id=str(flow["id"]), new_state="running")
    with pytest.raises(TokenFlowConflictError, match="only running"):
        link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="other-job")


def test_confirmation_pause_requires_canonical_latest_attempt_and_blocks_linking(
    initialized_database,
    monkeypatch,
) -> None:
    empty = create_flow(task_kind="synthesis")
    with pytest.raises(TokenFlowConflictError, match="canonical pause attempt"):
        transition_flow_state(flow_id=str(empty["id"]), new_state="confirmation_required")

    flow, _preparation = _link_confirmation_attempt(monkeypatch)
    _insert_job("after-pause")

    paused = transition_flow_state(
        flow_id=str(flow["id"]), new_state="confirmation_required"
    )
    assert paused["state"] == "confirmation_required"
    with pytest.raises(TokenFlowConflictError, match="only running"):
        link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="after-pause")
    with pytest.raises(TokenFlowConflictError, match="not allowed"):
        transition_flow_state(flow_id=str(flow["id"]), new_state="running")
    assert get_flow(str(flow["id"]))["state"] == "confirmation_required"


@pytest.mark.parametrize("workspace_id", [None, "matching-workspace"])
def test_confirmation_packet_identity_accepts_matching_nullable_workspace(
    initialized_database,
    monkeypatch,
    workspace_id: str | None,
) -> None:
    flow, _preparation = _link_confirmation_attempt(
        monkeypatch, workspace_id=workspace_id
    )

    paused = transition_flow_state(
        flow_id=str(flow["id"]), new_state="confirmation_required"
    )

    assert paused["state"] == "confirmation_required"
    assert paused["workspace_id"] == workspace_id


@pytest.mark.parametrize(
    ("flow_workspace", "field", "value"),
    [
        (None, "task_kind", "general"),
        (None, "workspace_id", "packet-only-workspace"),
        ("flow-workspace", "workspace_id", None),
        ("flow-workspace", "workspace_id", "other-workspace"),
    ],
)
def test_confirmation_pause_rejects_packet_task_or_workspace_mismatch(
    initialized_database,
    monkeypatch,
    flow_workspace: str | None,
    field: str,
    value: str | None,
) -> None:
    from app.core.database import open_sqlite_connection

    flow, preparation = _link_confirmation_attempt(
        monkeypatch, workspace_id=flow_workspace
    )
    if field == "workspace_id" and value is not None and value != flow_workspace:
        _insert_workspace(value)
    with open_sqlite_connection() as connection:
        updated = connection.execute(
            f"UPDATE egress_packets SET {field} = ? WHERE id = ?",
            (value, preparation.packet_id),
        )
        connection.commit()
    assert updated.rowcount == 1

    with pytest.raises(TokenFlowConflictError, match="packet task or workspace"):
        transition_flow_state(
            flow_id=str(flow["id"]), new_state="confirmation_required"
        )
    assert get_flow(str(flow["id"]))["state"] == "running"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "wrong_decision",
        "wrong_packet",
        "wrong_triggers",
        "expired",
        "consumed",
        "provider_binding",
        "route_binding",
        "fallback_binding",
    ],
)
def test_confirmation_pause_rejects_invalid_ticket_authority(
    initialized_database,
    monkeypatch,
    mutation: str,
) -> None:
    from app.core.database import open_sqlite_connection

    flow, preparation = _link_confirmation_attempt(monkeypatch)
    with open_sqlite_connection() as connection:
        if mutation == "missing":
            connection.execute(
                "DELETE FROM egress_confirmation_tickets WHERE id = ?",
                (preparation.ticket_id,),
            )
        elif mutation == "expired":
            connection.execute(
                "UPDATE egress_confirmation_tickets SET expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", preparation.ticket_id),
            )
        elif mutation == "consumed":
            connection.execute(
                "UPDATE egress_confirmation_tickets SET state = 'consumed' WHERE id = ?",
                (preparation.ticket_id,),
            )
        elif mutation.endswith("_binding"):
            statement, value = {
                "provider_binding": (
                    "UPDATE ai_jobs SET provider_id = ? WHERE id = 'pause'",
                    "other_provider",
                ),
                "route_binding": (
                    "UPDATE ai_jobs SET selected_route_class = ? WHERE id = 'pause'",
                    "external:other",
                ),
                "fallback_binding": (
                    "UPDATE ai_jobs SET fallback_index = ? WHERE id = 'pause'",
                    1,
                ),
            }[mutation]
            connection.execute(statement, (value,))
        else:
            row = connection.execute(
                "SELECT route_reason_json FROM ai_jobs WHERE id = 'pause'"
            ).fetchone()
            metadata = json.loads(row["route_reason_json"])
            key, value = {
                "wrong_decision": ("egress_decision_id", "wrong-decision"),
                "wrong_packet": ("egress_packet_digest", "wrong-packet"),
                "wrong_triggers": ("egress_trigger_ids", ["wrong-trigger"]),
            }[mutation]
            metadata[key] = value
            connection.execute(
                "UPDATE ai_jobs SET route_reason_json = ? WHERE id = 'pause'",
                (json.dumps(metadata, sort_keys=True),),
            )
        connection.commit()

    with pytest.raises(TokenFlowConflictError):
        transition_flow_state(
            flow_id=str(flow["id"]), new_state="confirmation_required"
        )
    assert get_flow(str(flow["id"]))["state"] == "running"


def test_confirmation_pause_rejects_malformed_attempt_route_metadata(
    initialized_database,
    monkeypatch,
) -> None:
    from app.core.database import open_sqlite_connection

    flow, _preparation = _link_confirmation_attempt(monkeypatch)
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_jobs SET route_reason_json = '[' WHERE id = 'pause'"
        )
        connection.commit()

    with pytest.raises(TokenFlowConflictError, match="route metadata is malformed"):
        transition_flow_state(
            flow_id=str(flow["id"]), new_state="confirmation_required"
        )
    assert get_flow(str(flow["id"]))["state"] == "running"


@pytest.mark.parametrize("terminal_state", ["complete", "partial_terminal", "failed_terminal"])
def test_non_cancelled_terminal_states_require_a_flow_attempt(
    initialized_database,
    terminal_state: str,
) -> None:
    flow = create_flow(task_kind="synthesis")

    with pytest.raises(TokenFlowError, match="requires terminal_attempt_id"):
        transition_flow_state(
            flow_id=str(flow["id"]),
            new_state=terminal_state,  # type: ignore[arg-type]
            terminal_reason="no_attempt",
        )

    assert get_flow(str(flow["id"]))["state"] == "running"


def test_cancelled_flow_may_be_terminal_before_first_attempt(initialized_database) -> None:
    flow = create_flow(task_kind="synthesis")

    cancelled = transition_flow_state(
        flow_id=str(flow["id"]),
        new_state="cancelled_terminal",
        terminal_reason="operator_cancelled",
    )

    assert cancelled["state"] == "cancelled_terminal"
    assert cancelled["terminal_attempt_id"] is None
    assert cancelled["cancelled_at"] is not None
    assert cancelled["final_output_digest"] is None


def test_terminal_attempt_must_be_the_final_ordered_attempt(initialized_database) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "first",
        execution_class="synthetic",
        adapter_invoked=1,
        usage_source="estimated",
        accounting_basis="synthetic_not_economic",
        output_digest=DIGEST_A,
    )
    _insert_job(
        "last",
        execution_class="synthetic",
        adapter_invoked=1,
        usage_source="estimated",
        accounting_basis="synthetic_not_economic",
        output_digest=DIGEST_B,
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="first")
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="last")

    with pytest.raises(TokenFlowConflictError, match="final ordered attempt"):
        transition_flow_state(
            flow_id=str(flow["id"]),
            new_state="complete",
            terminal_reason="completed",
            terminal_attempt_id="first",
        )

    complete = transition_flow_state(
        flow_id=str(flow["id"]),
        new_state="complete",
        terminal_reason="completed",
        terminal_attempt_id="last",
    )
    assert complete["final_output_digest"] == DIGEST_B


def test_complete_requires_canonical_terminal_output_digest(initialized_database) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "bad-digest",
        execution_class="synthetic",
        adapter_invoked=1,
        usage_source="estimated",
        accounting_basis="synthetic_not_economic",
        output_digest="sha256:short",
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="bad-digest")

    with pytest.raises(TokenFlowError, match="canonical sha256"):
        transition_flow_state(
            flow_id=str(flow["id"]),
            new_state="complete",
            terminal_reason="completed",
            terminal_attempt_id="bad-digest",
        )

    assert get_flow(str(flow["id"]))["state"] == "running"


@pytest.mark.parametrize(
    ("execution_class", "adapter_invoked", "dispatch", "accounting_basis"),
    [
        ("none", 0, "not_applicable", "no_execution"),
        ("external_provider", 0, "not_started", "external_not_sent"),
    ],
)
def test_non_executed_attempt_cannot_carry_output_digest(
    initialized_database,
    execution_class: str,
    adapter_invoked: int,
    dispatch: str,
    accounting_basis: str,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "false-output",
        execution_class=execution_class,
        adapter_invoked=adapter_invoked,
        dispatch=dispatch,
        accounting_basis=accounting_basis,
        output_digest=DIGEST_A,
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="false-output")

    with pytest.raises(TokenFlowError, match="cannot carry output_digest"):
        recompute_flow_aggregates(str(flow["id"]))


@pytest.mark.parametrize("status", ["success", "provider_error"])
def test_partial_terminal_accepts_success_or_error_with_output(
    initialized_database,
    status: str,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "partial",
        status=status,
        execution_class="synthetic",
        adapter_invoked=1,
        usage_source="estimated",
        accounting_basis="synthetic_not_economic",
        output_digest=DIGEST_A,
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="partial")

    partial = transition_flow_state(
        flow_id=str(flow["id"]),
        new_state="partial_terminal",
        terminal_reason="partial_output",
        terminal_attempt_id="partial",
    )

    assert partial["state"] == "partial_terminal"
    assert partial["final_output_digest"] == DIGEST_A


def test_partial_terminal_requires_output_digest(initialized_database) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "partial-no-output",
        execution_class="synthetic",
        adapter_invoked=1,
        usage_source="estimated",
        accounting_basis="synthetic_not_economic",
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="partial-no-output")

    with pytest.raises(TokenFlowError, match="canonical sha256"):
        transition_flow_state(
            flow_id=str(flow["id"]),
            new_state="partial_terminal",
            terminal_reason="partial_output",
            terminal_attempt_id="partial-no-output",
        )


@pytest.mark.parametrize(
    ("terminal_state", "status", "message"),
    [
        ("complete", "provider_error", "successful ai_job"),
        ("failed_terminal", "success", "non-success ai_job"),
        ("complete", "queued", "queued ai_job"),
        ("partial_terminal", "queued", "queued ai_job"),
        ("failed_terminal", "queued", "queued ai_job"),
        ("cancelled_terminal", "queued", "queued ai_job"),
    ],
)
def test_terminal_state_rejects_incompatible_job_status(
    initialized_database,
    terminal_state: str,
    status: str,
    message: str,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "bad-status",
        status=status,
        execution_class="synthetic",
        adapter_invoked=1,
        usage_source="estimated",
        accounting_basis="synthetic_not_economic",
        output_digest=DIGEST_A,
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="bad-status")

    with pytest.raises(TokenFlowConflictError, match=message):
        transition_flow_state(
            flow_id=str(flow["id"]),
            new_state=terminal_state,  # type: ignore[arg-type]
            terminal_reason="invalid_status",
            terminal_attempt_id="bad-status",
        )

    assert get_flow(str(flow["id"]))["state"] == "running"


def test_failed_terminal_may_have_no_output_digest(initialized_database) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job("failed-no-output", status="provider_error")
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="failed-no-output")

    failed = transition_flow_state(
        flow_id=str(flow["id"]),
        new_state="failed_terminal",
        terminal_reason="provider_failed",
        terminal_attempt_id="failed-no-output",
    )

    assert failed["final_output_digest"] is None


def test_recompute_mixed_aggregates_uses_exact_decimal_without_double_count(
    initialized_database,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "synthetic",
        execution_class="synthetic",
        adapter_invoked=1,
        dispatch="not_applicable",
        input_tokens=10,
        output_tokens=2,
        usage_source="estimated",
        latency_ms=1,
        accounting_basis="synthetic_not_economic",
        spend="0",
        continuation_index=0,
    )
    _insert_job(
        "local",
        execution_class="local_compute",
        adapter_invoked=1,
        dispatch="not_applicable",
        input_tokens=20,
        output_tokens=5,
        cache_read_tokens=2,
        reasoning_tokens=1,
        usage_source="actual",
        latency_ms=3,
        accounting_basis="local_compute_unpriced",
        spend="0",
        continuation_index=1,
    )
    _insert_job(
        "external-a",
        execution_class="external_provider",
        adapter_invoked=1,
        dispatch="started",
        input_tokens=30,
        output_tokens=10,
        usage_source="actual",
        latency_ms=5,
        accounting_basis="provider_exact",
        spend="0.10",
        continuation_index=1,
    )
    _insert_job(
        "external-b",
        execution_class="external_provider",
        adapter_invoked=1,
        dispatch="unknown",
        input_tokens=40,
        output_tokens=8,
        usage_source="estimated",
        latency_ms=7,
        accounting_basis="conservative_estimated_usage",
        spend="0.20",
        continuation_index=2,
    )
    for job_id in ("synthetic", "local", "external-a", "external-b"):
        link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id=job_id)

    aggregate = recompute_flow_aggregates(str(flow["id"]))
    replayed = recompute_flow_aggregates(str(flow["id"]))

    assert aggregate["ordered_attempt_ids"] == [
        "synthetic",
        "local",
        "external-a",
        "external-b",
    ]
    assert aggregate["attempt_count"] == 4
    assert aggregate["continuation_count"] == 2
    assert aggregate["execution_class_counts"] == {
        "external_provider": 2,
        "local_compute": 1,
        "synthetic": 1,
    }
    assert aggregate["external_dispatch_counts"] == {
        "not_applicable": 2,
        "started": 1,
        "unknown": 1,
    }
    assert aggregate["accounting_basis_counts"]["provider_exact"] == 1
    assert aggregate["usage_totals"]["input_tokens"] == 100
    assert aggregate["usage_totals"]["output_tokens"] == 25
    assert aggregate["usage_totals"]["total_tokens"] == 125
    assert aggregate["external_provider_spend_usd_decimal"] == "0.3"
    assert replayed["external_provider_spend_usd_decimal"] == "0.3"
    assert aggregate["local_compute_cost_unpriced"] is True
    assert aggregate["synthetic_evidence_present"] is True


@pytest.mark.parametrize(
    ("execution_class", "adapter_invoked", "dispatch", "usage_source", "accounting"),
    [
        ("synthetic", 1, "not_applicable", "actual", "synthetic_not_economic"),
        ("local_compute", 1, "not_applicable", "mixed", "local_compute_unpriced"),
        ("external_provider", 1, "started", "none", "conservative_estimated_usage"),
        ("external_provider", 1, "unknown", "actual", "conservative_estimated_usage"),
    ],
)
def test_execution_class_usage_matrix_fails_closed(
    initialized_database,
    execution_class: str,
    adapter_invoked: int,
    dispatch: str,
    usage_source: str,
    accounting: str,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "invalid-usage",
        execution_class=execution_class,
        adapter_invoked=adapter_invoked,
        dispatch=dispatch,
        usage_source=usage_source,
        accounting_basis=accounting,
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="invalid-usage")

    with pytest.raises(TokenFlowError):
        recompute_flow_aggregates(str(flow["id"]))


@pytest.mark.parametrize(
    ("accounting_basis", "usage_source"),
    [
        ("conservative_estimated_usage", "estimated"),
        ("conservative_standard_input", "actual"),
    ],
)
@pytest.mark.parametrize(
    ("spend", "accepted"), [("0", False), ("-0.01", False), ("0.01", True)]
)
def test_started_conservative_accounting_requires_positive_spend(
    initialized_database,
    accounting_basis: str,
    usage_source: str,
    spend: str,
    accepted: bool,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "conservative-started",
        execution_class="external_provider",
        adapter_invoked=1,
        dispatch="started",
        usage_source=usage_source,
        accounting_basis=accounting_basis,
        spend=spend,
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="conservative-started")

    if not accepted:
        with pytest.raises(TokenFlowError):
            recompute_flow_aggregates(str(flow["id"]))
        return

    aggregate = recompute_flow_aggregates(str(flow["id"]))
    assert aggregate["external_provider_spend_usd_decimal"] == "0.01"


def test_started_provider_exact_zero_spend_remains_valid(initialized_database) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "provider-exact",
        execution_class="external_provider",
        adapter_invoked=1,
        dispatch="started",
        usage_source="actual",
        accounting_basis="provider_exact",
        spend="0",
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="provider-exact")

    aggregate = recompute_flow_aggregates(str(flow["id"]))
    assert aggregate["external_provider_spend_usd_decimal"] == "0"


@pytest.mark.parametrize("spend", ["0", "-0.01"])
def test_unknown_external_dispatch_rejects_nonpositive_spend(
    initialized_database,
    spend: str,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "unknown-spend",
        status="provider_error",
        execution_class="external_provider",
        adapter_invoked=1,
        dispatch="unknown",
        usage_source="estimated",
        accounting_basis="conservative_estimated_usage",
        spend=spend,
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="unknown-spend")

    with pytest.raises(TokenFlowError):
        recompute_flow_aggregates(str(flow["id"]))


def test_unknown_external_dispatch_accepts_positive_spend(initialized_database) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "unknown-spend",
        status="provider_error",
        execution_class="external_provider",
        adapter_invoked=1,
        dispatch="unknown",
        usage_source="estimated",
        accounting_basis="conservative_estimated_usage",
        spend="0.01",
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="unknown-spend")

    aggregate = recompute_flow_aggregates(str(flow["id"]))

    assert aggregate["external_provider_spend_usd_decimal"] == "0.01"
    assert aggregate["external_dispatch_counts"]["unknown"] == 1


@pytest.mark.parametrize(
    "spend", ["invalid", "NaN", "Infinity", "-0.01", "1e2", "0" * 65]
)
def test_invalid_external_decimal_evidence_fails_recompute_closed(
    initialized_database,
    spend: str,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "bad-external",
        execution_class="external_provider",
        adapter_invoked=1,
        dispatch="started",
        usage_source="estimated",
        accounting_basis="conservative_estimated_usage",
        spend=spend,
    )

    linked = link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="bad-external")
    assert linked["attempt_count"] == 1

    with pytest.raises(TokenFlowError):
        recompute_flow_aggregates(str(flow["id"]))

    assert get_flow(str(flow["id"]))["external_provider_spend_usd_decimal"] == "0"


def test_non_external_spend_is_rejected_and_link_rolls_back(
    initialized_database,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "bad-local",
        execution_class="local_compute",
        adapter_invoked=1,
        usage_source="estimated",
        accounting_basis="local_compute_unpriced",
        spend="0.01",
    )

    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="bad-local")

    with pytest.raises(TokenFlowError, match="non-external"):
        recompute_flow_aggregates(str(flow["id"]))

    assert get_flow(str(flow["id"]))["ordered_attempt_ids"] == ["bad-local"]


def test_null_current_attempt_evidence_is_not_reinterpreted_as_legacy(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    flow = create_flow(task_kind="synthesis")
    _insert_job("missing-evidence", output_digest=DIGEST_A)
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_jobs SET execution_class = NULL WHERE id = 'missing-evidence'"
        )
        connection.commit()
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="missing-evidence")

    with pytest.raises(TokenFlowError, match="execution_class is missing"):
        recompute_flow_aggregates(str(flow["id"]))
    with pytest.raises(TokenFlowError, match="execution_class is missing"):
        transition_flow_state(
            flow_id=str(flow["id"]),
            new_state="complete",
            terminal_reason="completed",
            terminal_attempt_id="missing-evidence",
        )

    persisted = get_flow(str(flow["id"]))
    assert persisted["state"] == "running"
    assert persisted["execution_class_counts"] == {}


def test_terminal_digests_are_deterministic_and_exclude_unsafe_ledger_fields(
    initialized_database,
) -> None:
    secret = "prompt-body-must-not-enter-flow"
    flow = create_flow(task_kind="synthesis", requested_route_class="local:fake")
    _insert_job(
        "terminal",
        execution_class="synthetic",
        adapter_invoked=1,
        dispatch="not_applicable",
        usage_source="estimated",
        accounting_basis="synthetic_not_economic",
        spend="0",
        output_digest=DIGEST_A,
        context_sources_json=json.dumps({"unsafe_body": secret}),
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="terminal")
    terminal = transition_flow_state(
        flow_id=str(flow["id"]),
        new_state="complete",
        terminal_reason="completed",
        terminal_attempt_id="terminal",
    )
    replayed = recompute_flow_aggregates(str(flow["id"]))

    assert replayed["final_accounting_digest"] == terminal["final_accounting_digest"]
    assert replayed["final_output_digest"] == terminal["final_output_digest"]
    assert str(terminal["final_accounting_digest"]).startswith("sha256:")
    assert terminal["final_output_digest"] == DIGEST_A
    assert secret not in json.dumps(terminal, sort_keys=True)


def test_terminal_digest_recompute_rejects_post_terminal_ledger_drift(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    flow = create_flow(task_kind="synthesis")
    _insert_job(
        "terminal-drift",
        execution_class="synthetic",
        adapter_invoked=1,
        usage_source="estimated",
        accounting_basis="synthetic_not_economic",
        output_digest=DIGEST_A,
    )
    link_attempt_to_flow(flow_id=str(flow["id"]), attempt_id="terminal-drift")
    terminal = transition_flow_state(
        flow_id=str(flow["id"]),
        new_state="complete",
        terminal_reason="completed",
        terminal_attempt_id="terminal-drift",
    )
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_jobs SET output_digest = ? WHERE id = 'terminal-drift'",
            (DIGEST_C,),
        )
        connection.commit()

    with pytest.raises(TokenFlowConflictError, match="digest evidence changed"):
        recompute_flow_aggregates(str(flow["id"]))

    persisted = get_flow(str(flow["id"]))
    assert persisted["final_accounting_digest"] == terminal["final_accounting_digest"]
    assert persisted["final_output_digest"] == DIGEST_A
