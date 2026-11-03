from __future__ import annotations

import sqlite3

import pytest

from app.modules.ai.token_flow_evidence import AttemptEvidence, record_attempt_evidence
from app.modules.ai.token_flow_service import (
    TokenFlowConflictError,
    TokenFlowError,
    create_flow,
    get_flow,
)

DIGEST = "sha256:" + "d" * 64


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
    selected_route_class: str | None = "local:fake",
    provider_id: str | None = "fake",
    model_id: str | None = "fake-modeling-draft-v1",
    fallback_index: int | None = 0,
    input_tokens: int | None = 10,
    output_tokens: int | None = 4,
    output_digest: str | None = DIGEST,
) -> None:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import utc_now

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, selected_route_class,
                provider_id, model_id, route_reason_json, fallback_index,
                input_tokens, output_tokens, output_digest
            ) VALUES (?, ?, 'success', ?, ?, ?, ?, '{}', ?, ?, ?, ?)
            """,
            (
                job_id,
                utc_now(),
                task_kind,
                selected_route_class,
                provider_id,
                model_id,
                fallback_index,
                input_tokens,
                output_tokens,
                output_digest,
            ),
        )
        connection.commit()


def _synthetic_evidence(**overrides) -> AttemptEvidence:
    values = {
        "execution_class": "synthetic",
        "adapter_invoked": True,
        "external_dispatch_state": "not_applicable",
        "normalized_usage_source": "estimated",
        "accounting_basis": "synthetic_not_economic",
        "accounted_provider_spend_usd_decimal": "0.000",
        "outcome_reason": "completed",
        "accounting_version": "token-flow-v0",
        "provider_id": "fake",
        "model_id": "fake-modeling-draft-v1",
        "selected_route_class": "local:fake",
        "fallback_index": 0,
        "requested_output_ceiling": 128,
        "effective_output_ceiling": 96,
        "normalized_finish_reason": "stop",
        "cache_read_tokens": 0,
        "reasoning_tokens": 1,
        "capability_version": "registry-v1",
    }
    values.update(overrides)
    return AttemptEvidence(**values)


def _row(job_id: str) -> sqlite3.Row:
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        return connection.execute("SELECT * FROM ai_jobs WHERE id = ?", (job_id,)).fetchone()


def test_record_attempt_evidence_links_and_writes_in_one_transaction(
    initialized_database,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job("attempt-a")

    updated_flow = record_attempt_evidence(
        flow_id=str(flow["id"]),
        attempt_id="attempt-a",
        evidence=_synthetic_evidence(),
    )

    row = _row("attempt-a")
    assert row["flow_id"] == flow["id"]
    assert row["flow_attempt_index"] == 0
    assert row["execution_class"] == "synthetic"
    assert row["adapter_invoked"] == 1
    assert row["external_dispatch_state"] == "not_applicable"
    assert row["normalized_usage_source"] == "estimated"
    assert row["accounting_basis"] == "synthetic_not_economic"
    assert row["accounted_provider_spend_usd_decimal"] == "0"
    assert row["outcome_reason"] == "completed"
    assert updated_flow["ordered_attempt_ids"] == ["attempt-a"]
    assert updated_flow["attempt_count"] == 1


def test_exact_replay_is_idempotent_and_conflicting_replay_is_rejected(
    initialized_database,
) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job("attempt-a")
    evidence = _synthetic_evidence()

    first = record_attempt_evidence(
        flow_id=str(flow["id"]), attempt_id="attempt-a", evidence=evidence
    )
    replay = record_attempt_evidence(
        flow_id=str(flow["id"]), attempt_id="attempt-a", evidence=evidence
    )

    assert replay["ordered_attempt_ids"] == first["ordered_attempt_ids"]
    with pytest.raises(TokenFlowConflictError, match="replay"):
        record_attempt_evidence(
            flow_id=str(flow["id"]),
            attempt_id="attempt-a",
            evidence=_synthetic_evidence(outcome_reason="different_outcome"),
        )
    assert _row("attempt-a")["outcome_reason"] == "completed"


def test_invalid_usage_rolls_back_linkage_and_all_evidence(initialized_database) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job("attempt-a")

    with pytest.raises(TokenFlowError):
        record_attempt_evidence(
            flow_id=str(flow["id"]),
            attempt_id="attempt-a",
            evidence=_synthetic_evidence(normalized_usage_source="none"),
        )

    row = _row("attempt-a")
    assert row["flow_id"] is None
    assert row["flow_attempt_index"] is None
    assert row["execution_class"] is None
    assert row["accounting_basis"] is None
    assert get_flow(str(flow["id"]))["ordered_attempt_ids"] == []


def test_task_or_binding_mismatch_fails_before_write(initialized_database) -> None:
    flow = create_flow(task_kind="synthesis")
    _insert_job("wrong-task", task_kind="general")
    _insert_job("wrong-binding")

    with pytest.raises(TokenFlowConflictError, match="task kind"):
        record_attempt_evidence(
            flow_id=str(flow["id"]),
            attempt_id="wrong-task",
            evidence=_synthetic_evidence(),
        )
    with pytest.raises(TokenFlowConflictError, match="binding identity"):
        record_attempt_evidence(
            flow_id=str(flow["id"]),
            attempt_id="wrong-binding",
            evidence=_synthetic_evidence(model_id="different-model"),
        )

    assert _row("wrong-task")["flow_id"] is None
    assert _row("wrong-binding")["flow_id"] is None


def test_partial_preexisting_evidence_is_not_completed_opportunistically(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    flow = create_flow(task_kind="synthesis")
    _insert_job("partial")
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_jobs SET execution_class = 'synthetic' WHERE id = 'partial'"
        )
        connection.commit()

    with pytest.raises(TokenFlowConflictError, match="partial or pre-existing"):
        record_attempt_evidence(
            flow_id=str(flow["id"]),
            attempt_id="partial",
            evidence=_synthetic_evidence(),
        )
    row = _row("partial")
    assert row["flow_id"] is None
    assert row["execution_class"] == "synthetic"
    assert row["accounting_basis"] is None


def test_exact_replay_is_idempotent_after_flow_terminalization(
    initialized_database,
) -> None:
    from app.modules.ai.token_flow_service import transition_flow_state

    flow = create_flow(task_kind="synthesis")
    _insert_job("terminal-replay")
    evidence = _synthetic_evidence()
    record_attempt_evidence(
        flow_id=str(flow["id"]),
        attempt_id="terminal-replay",
        evidence=evidence,
    )
    terminal = transition_flow_state(
        flow_id=str(flow["id"]),
        new_state="complete",
        terminal_reason="completed",
        terminal_attempt_id="terminal-replay",
    )

    replay = record_attempt_evidence(
        flow_id=str(flow["id"]),
        attempt_id="terminal-replay",
        evidence=evidence,
    )

    assert replay["state"] == "complete"
    assert replay["terminal_attempt_id"] == "terminal-replay"
    assert replay["final_accounting_digest"] == terminal["final_accounting_digest"]
    assert replay["final_output_digest"] == terminal["final_output_digest"]
    assert replay["ordered_attempt_ids"] == ["terminal-replay"]
