from __future__ import annotations

import pytest

from app.modules.ai.token_flow_evidence import AttemptEvidence
from app.modules.ai.token_flow_service import create_flow, get_flow, transition_flow_state
from app.modules.ai.token_flow_transaction import record_attempt_evidence_in_transaction

DIGEST = "sha256:" + "f" * 64


@pytest.fixture
def initialized_database():
    from app.core.database import initialize_database
    from app.modules.ai.settings import ensure_ai_settings

    initialize_database()
    ensure_ai_settings()


def _insert_job(connection, job_id: str) -> None:
    from app.modules.events.service import utc_now

    connection.execute(
        """
        INSERT INTO ai_jobs (
            id, created_at, status, task_kind, selected_route_class,
            provider_id, model_id, route_reason_json, fallback_index,
            input_tokens, output_tokens, output_digest
        ) VALUES (?, ?, 'success', 'synthesis', 'local:fake',
                  'fake', 'fake-modeling-draft-v1', '{}', 0, 4, 2, ?)
        """,
        (job_id, utc_now(), DIGEST),
    )


def _evidence() -> AttemptEvidence:
    return AttemptEvidence(
        execution_class="synthetic",
        adapter_invoked=True,
        external_dispatch_state="not_applicable",
        normalized_usage_source="estimated",
        accounting_basis="synthetic_not_economic",
        accounted_provider_spend_usd_decimal="0",
        outcome_reason="completed",
        accounting_version="token-flow-v0",
        provider_id="fake",
        model_id="fake-modeling-draft-v1",
        selected_route_class="local:fake",
        fallback_index=0,
        normalized_finish_reason="stop",
        capability_version="provider-registry-v1",
    )


def test_writer_uses_caller_transaction_and_rolls_back_atomically(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    flow = create_flow(task_kind="synthesis")
    with pytest.raises(RuntimeError, match="force rollback"):
        with open_sqlite_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _insert_job(connection, "attempt-a")
            record_attempt_evidence_in_transaction(
                connection,
                flow_id=str(flow["id"]),
                attempt_id="attempt-a",
                evidence=_evidence(),
            )
            connection.rollback()
            raise RuntimeError("force rollback")

    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT id FROM ai_jobs WHERE id = 'attempt-a'"
        ).fetchone()
    assert row is None
    assert get_flow(str(flow["id"]))["attempt_count"] == 0


def test_exact_replay_is_valid_after_flow_terminalization(initialized_database) -> None:
    from app.core.database import open_sqlite_connection

    flow = create_flow(task_kind="synthesis")
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _insert_job(connection, "attempt-a")
        record_attempt_evidence_in_transaction(
            connection,
            flow_id=str(flow["id"]),
            attempt_id="attempt-a",
            evidence=_evidence(),
        )
        connection.commit()

    transition_flow_state(
        flow_id=str(flow["id"]),
        new_state="complete",
        terminal_reason="completed",
        terminal_attempt_id="attempt-a",
    )

    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        replay = record_attempt_evidence_in_transaction(
            connection,
            flow_id=str(flow["id"]),
            attempt_id="attempt-a",
            evidence=_evidence(),
        )
        connection.commit()

    assert replay["state"] == "complete"
    assert replay["ordered_attempt_ids"] == ["attempt-a"]
