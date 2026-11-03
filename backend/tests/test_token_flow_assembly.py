from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.token_flow_assembly import assemble_protected_output
from app.modules.ai.token_flow_segments import store_protected_segment
from app.modules.ai.token_flow_service import TokenFlowConflictError, create_flow

FIRST = "First validated output segment. "
SECOND = "Second and final validated segment."
NOW = datetime(2026, 7, 19, 11, 0, tzinfo=UTC)


@pytest.fixture
def initialized_database():
    from app.core.database import initialize_database
    from app.modules.ai.settings import ensure_ai_settings

    initialize_database()
    ensure_ai_settings()


def _attempt(
    *,
    flow_id: str,
    attempt_id: str,
    index: int,
    continuation_index: int | None,
    parent_attempt_id: str | None,
    body: str,
    finish_reason: str,
    output_tokens: int,
) -> None:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import utc_now

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, requested_route_class,
                selected_route_class, provider_id, model_id, fallback_index,
                route_reason_json, output_digest, input_tokens, output_tokens,
                flow_id, flow_attempt_index, parent_attempt_id,
                continuation_index, execution_class, adapter_invoked,
                external_dispatch_state, normalized_finish_reason,
                normalized_usage_source, accounting_basis,
                accounted_provider_spend_usd_decimal, outcome_reason,
                capability_version, accounting_version
            ) VALUES (
                ?, ?, 'success', 'synthesis', 'local:fake', 'local:fake',
                'fake', 'fake-v0', 0, ?, ?, 4, ?, ?, ?, ?, ?,
                'synthetic', 1, 'not_applicable', ?, 'estimated',
                'synthetic_not_economic', '0', 'success',
                'provider-registry-v1', 'token-flow-v0'
            )
            """,
            (
                attempt_id,
                utc_now(),
                '{"decision_reason":"bound:local:fake","fallback_attempt_index":0}',
                canonical_digest({"text": body}),
                output_tokens,
                flow_id,
                index,
                parent_attempt_id,
                continuation_index,
                finish_reason,
            ),
        )
        connection.commit()


def _two_segments() -> str:
    flow = create_flow(
        task_kind="synthesis",
        requested_route_class="local:fake",
    )
    flow_id = str(flow["id"])
    _attempt(
        flow_id=flow_id,
        attempt_id="attempt-0",
        index=0,
        continuation_index=None,
        parent_attempt_id=None,
        body=FIRST,
        finish_reason="length",
        output_tokens=6,
    )
    store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id="attempt-0",
        body_text=FIRST,
        effective_sensitivity_level="S1",
        workspace_id=None,
        now=NOW,
    )
    _attempt(
        flow_id=flow_id,
        attempt_id="attempt-1",
        index=1,
        continuation_index=1,
        parent_attempt_id="attempt-0",
        body=SECOND,
        finish_reason="stop",
        output_tokens=7,
    )
    store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id="attempt-1",
        body_text=SECOND,
        effective_sensitivity_level="S1",
        workspace_id=None,
        now=NOW + timedelta(minutes=1),
    )
    return flow_id


def test_assembly_is_ordered_deterministic_and_body_safe(
    initialized_database,
) -> None:
    flow_id = _two_segments()

    first = assemble_protected_output(
        flow_id=flow_id,
        workspace_id=None,
        expected_sensitivity_level="S1",
        now=NOW + timedelta(minutes=2),
    )
    second = assemble_protected_output(
        flow_id=flow_id,
        workspace_id=None,
        expected_sensitivity_level="S1",
        now=NOW + timedelta(minutes=3),
    )

    assert first == second
    assert first.body_text == FIRST + SECOND
    assert first.body_digest == canonical_digest({"text": FIRST + SECOND})
    assert first.segment_count == 2
    assert first.byte_count == len((FIRST + SECOND).encode("utf-8"))
    assert first.token_count == 13
    assert first.originating_attempt_ids == ("attempt-0", "attempt-1")
    assert FIRST not in repr(first)
    assert "body_text" in asdict(first)


def test_assembly_requires_at_least_one_segment(
    initialized_database,
) -> None:
    flow = create_flow(task_kind="synthesis", requested_route_class="local:fake")
    with pytest.raises(TokenFlowConflictError, match="at least one segment"):
        assemble_protected_output(
            flow_id=str(flow["id"]),
            workspace_id=None,
            expected_sensitivity_level="S1",
            now=NOW,
        )


def test_assembly_revalidates_body_and_sensitivity(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    flow_id = _two_segments()
    with pytest.raises(TokenFlowConflictError, match="sensitivity"):
        assemble_protected_output(
            flow_id=flow_id,
            workspace_id=None,
            expected_sensitivity_level="S0",
            now=NOW + timedelta(minutes=2),
        )

    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_flow_segments SET body_text = 'tampered' "
            "WHERE flow_id = ? AND segment_index = 1",
            (flow_id,),
        )
        connection.commit()
    with pytest.raises(TokenFlowConflictError, match="digest evidence"):
        assemble_protected_output(
            flow_id=flow_id,
            workspace_id=None,
            expected_sensitivity_level="S1",
            now=NOW + timedelta(minutes=2),
        )


def test_expired_segment_blocks_complete_assembly(
    initialized_database,
) -> None:
    flow_id = _two_segments()
    with pytest.raises(TokenFlowConflictError, match="expired"):
        assemble_protected_output(
            flow_id=flow_id,
            workspace_id=None,
            expected_sensitivity_level="S1",
            now=NOW + timedelta(hours=25),
        )
