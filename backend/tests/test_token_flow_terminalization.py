from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.token_flow_segments import store_protected_segment
from app.modules.ai.token_flow_service import (
    TokenFlowConflictError,
    create_flow,
    get_flow,
    recompute_flow_aggregates,
)
from app.modules.ai.token_flow_terminalization import (
    terminalize_assembled_output,
)

FIRST = "First bounded segment. "
SECOND = "Second final segment."
NOW = datetime(2026, 7, 20, 17, 0, tzinfo=UTC)


@pytest.fixture
def initialized_database():
    from app.core.database import initialize_database
    from app.modules.ai.settings import ensure_ai_settings

    initialize_database()
    ensure_ai_settings()


def _insert_attempt(
    *,
    flow_id: str,
    attempt_id: str,
    index: int,
    status: str,
    body: str | None,
    finish_reason: str,
    parent_attempt_id: str | None = None,
    continuation_index: int | None = None,
) -> None:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import utc_now

    successful = status == "success"
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, requested_route_class,
                selected_route_class, provider_id, model_id, fallback_index,
                route_reason_json, output_digest, input_tokens, output_tokens,
                flow_id, flow_attempt_index, parent_attempt_id,
                continuation_index, execution_class, adapter_invoked,
                external_dispatch_state, requested_output_ceiling,
                effective_output_ceiling, normalized_finish_reason,
                normalized_usage_source, accounting_basis,
                accounted_provider_spend_usd_decimal, outcome_reason,
                capability_version, accounting_version
            ) VALUES (
                ?, ?, ?, 'synthesis', 'local:fake', 'local:fake',
                'fake', 'fake-v0', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'not_applicable', 128, 128, ?, ?, ?, '0', ?,
                'provider-registry-v1', 'token-flow-v0'
            )
            """,
            (
                attempt_id,
                utc_now(),
                status,
                '{"decision_reason":"bound:local:fake","fallback_attempt_index":0}',
                canonical_digest({"text": body}) if body is not None else None,
                4 if successful else None,
                6 if successful else None,
                flow_id,
                index,
                parent_attempt_id,
                continuation_index,
                "synthetic" if successful else "none",
                1 if successful else 0,
                finish_reason,
                "estimated" if successful else "none",
                "synthetic_not_economic" if successful else "no_execution",
                "success" if successful else "continuation_error",
            ),
        )
        connection.commit()


def _flow_with_two_successful_segments(
    *,
    final_finish_reason: str = "stop",
) -> str:
    flow = create_flow(
        task_kind="synthesis",
        requested_route_class="local:fake",
    )
    flow_id = str(flow["id"])
    _insert_attempt(
        flow_id=flow_id,
        attempt_id="parent",
        index=0,
        status="success",
        body=FIRST,
        finish_reason="length",
    )
    store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id="parent",
        body_text=FIRST,
        effective_sensitivity_level="S1",
        workspace_id=None,
        now=NOW,
    )
    _insert_attempt(
        flow_id=flow_id,
        attempt_id="child",
        index=1,
        status="success",
        body=SECOND,
        finish_reason=final_finish_reason,
        parent_attempt_id="parent",
        continuation_index=1,
    )
    store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id="child",
        body_text=SECOND,
        effective_sensitivity_level="S1",
        workspace_id=None,
        now=NOW + timedelta(minutes=1),
    )
    return flow_id


def test_complete_flow_persists_assembled_not_last_attempt_digest(
    initialized_database,
) -> None:
    flow_id = _flow_with_two_successful_segments()

    flow, assembled = terminalize_assembled_output(
        flow_id=flow_id,
        terminal_attempt_id="child",
        new_state="complete",
        terminal_reason="completed",
        workspace_id=None,
        expected_sensitivity_level="S1",
        now=NOW + timedelta(minutes=2),
    )

    combined_digest = canonical_digest({"text": FIRST + SECOND})
    assert assembled.body_text == FIRST + SECOND
    assert assembled.body_digest == combined_digest
    assert assembled.body_digest != canonical_digest({"text": SECOND})
    assert flow["state"] == "complete"
    assert flow["terminal_attempt_id"] == "child"
    assert flow["final_output_digest"] == combined_digest
    assert flow["final_accounting_digest"] is not None

    recomputed = recompute_flow_aggregates(flow_id)
    assert recomputed["final_output_digest"] == combined_digest
    assert recomputed["final_accounting_digest"] == flow["final_accounting_digest"]


def test_failed_latest_attempt_terminalizes_prior_output_as_partial(
    initialized_database,
) -> None:
    flow = create_flow(
        task_kind="synthesis",
        requested_route_class="local:fake",
    )
    flow_id = str(flow["id"])
    _insert_attempt(
        flow_id=flow_id,
        attempt_id="parent",
        index=0,
        status="success",
        body=FIRST,
        finish_reason="length",
    )
    store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id="parent",
        body_text=FIRST,
        effective_sensitivity_level="S1",
        workspace_id=None,
        now=NOW,
    )
    _insert_attempt(
        flow_id=flow_id,
        attempt_id="failed-child",
        index=1,
        status="config_error",
        body=None,
        finish_reason="error",
        parent_attempt_id="parent",
        continuation_index=1,
    )

    terminal, assembled = terminalize_assembled_output(
        flow_id=flow_id,
        terminal_attempt_id="failed-child",
        new_state="partial_terminal",
        terminal_reason="continuation_error",
        workspace_id=None,
        expected_sensitivity_level="S1",
        now=NOW + timedelta(minutes=1),
    )

    assert terminal["state"] == "partial_terminal"
    assert terminal["terminal_attempt_id"] == "failed-child"
    assert assembled.body_text == FIRST
    assert terminal["final_output_digest"] == canonical_digest({"text": FIRST})
    assert recompute_flow_aggregates(flow_id)["final_output_digest"] == terminal[
        "final_output_digest"
    ]


def test_complete_rejects_length_terminal_attempt_without_mutation(
    initialized_database,
) -> None:
    flow_id = _flow_with_two_successful_segments(final_finish_reason="length")

    with pytest.raises(TokenFlowConflictError, match="exact stop"):
        terminalize_assembled_output(
            flow_id=flow_id,
            terminal_attempt_id="child",
            new_state="complete",
            terminal_reason="completed",
            workspace_id=None,
            expected_sensitivity_level="S1",
            now=NOW + timedelta(minutes=2),
        )

    flow = get_flow(flow_id)
    assert flow["state"] == "running"
    assert flow["final_output_digest"] is None


def test_segment_tampering_rolls_back_terminalization(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    flow_id = _flow_with_two_successful_segments()
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_flow_segments SET body_text = 'tampered' "
            "WHERE flow_id = ? AND segment_index = 1",
            (flow_id,),
        )
        connection.commit()

    with pytest.raises(TokenFlowConflictError, match="digest evidence"):
        terminalize_assembled_output(
            flow_id=flow_id,
            terminal_attempt_id="child",
            new_state="complete",
            terminal_reason="completed",
            workspace_id=None,
            expected_sensitivity_level="S1",
            now=NOW + timedelta(minutes=2),
        )

    flow = get_flow(flow_id)
    assert flow["state"] == "running"
    assert flow["terminal_attempt_id"] is None
    assert flow["final_output_digest"] is None
