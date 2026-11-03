from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.token_flow_continuation import (
    apply_continuation_lineage,
    evaluate_direct_continuation,
)
from app.modules.ai.token_flow_evidence import AttemptEvidence
from app.modules.ai.token_flow_segments import store_protected_segment
from app.modules.ai.token_flow_service import (
    TokenFlowConflictError,
    create_flow,
)

BODY = "A bounded response stopped only because of the output limit."
NOW = datetime(2026, 7, 19, 9, 0, tzinfo=UTC)


@pytest.fixture
def initialized_database():
    from app.core.database import initialize_database
    from app.modules.ai.settings import ensure_ai_settings

    initialize_database()
    ensure_ai_settings()


def _workspace(workspace_id: str) -> None:
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


def _flow(*, workspace_id: str | None = None, snapshot: int = 8) -> str:
    if workspace_id is not None:
        _workspace(workspace_id)
    created = create_flow(
        task_kind="synthesis",
        requested_route_class="local:fake",
        workspace_id=workspace_id,
    )
    flow_id = str(created["id"])
    if snapshot != 8:
        from app.core.database import open_sqlite_connection

        with open_sqlite_connection() as connection:
            connection.execute(
                "UPDATE ai_flows SET max_direct_continuations_snapshot = ? "
                "WHERE id = ?",
                (snapshot, flow_id),
            )
            connection.commit()
    return flow_id


def _attempt(
    *,
    flow_id: str,
    attempt_id: str = "attempt-0",
    index: int = 0,
    continuation_index: int | None = None,
    adapter_invoked: int = 1,
    finish_reason: str = "length",
    status: str = "success",
    body: str = BODY,
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
                flow_id, flow_attempt_index, continuation_index,
                execution_class, adapter_invoked, external_dispatch_state,
                normalized_finish_reason, normalized_usage_source,
                accounting_basis, accounted_provider_spend_usd_decimal,
                outcome_reason, capability_version, accounting_version
            ) VALUES (
                ?, ?, ?, 'synthesis', 'local:fake', 'local:fake',
                'fake', 'fake-v0', 0, ?, ?, 4, 9, ?, ?, ?,
                'synthetic', ?, 'not_applicable', ?, 'estimated',
                'synthetic_not_economic', '0', 'success',
                'provider-registry-v1', 'token-flow-v0'
            )
            """,
            (
                attempt_id,
                utc_now(),
                status,
                '{"decision_reason":"bound:local:fake","fallback_attempt_index":0}',
                canonical_digest({"text": body}),
                flow_id,
                index,
                continuation_index,
                adapter_invoked,
                finish_reason,
            ),
        )
        connection.commit()


def _store(flow_id: str, *, workspace_id: str | None = None) -> None:
    store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id="attempt-0",
        body_text=BODY,
        effective_sensitivity_level="S1",
        workspace_id=workspace_id,
        now=NOW,
    )


def _base_evidence() -> AttemptEvidence:
    return AttemptEvidence(
        execution_class="synthetic",
        adapter_invoked=True,
        external_dispatch_state="not_applicable",
        normalized_usage_source="estimated",
        accounting_basis="synthetic_not_economic",
        accounted_provider_spend_usd_decimal="0",
        outcome_reason="success",
        accounting_version="token-flow-v0",
        provider_id="fake",
        model_id="fake-v0",
        selected_route_class="local:fake",
        fallback_index=0,
        requested_output_ceiling=128,
        effective_output_ceiling=128,
        normalized_finish_reason="stop",
        capability_version="provider-registry-v1",
    )


def test_exact_length_with_valid_segment_and_guard_is_eligible(
    initialized_database,
) -> None:
    flow_id = _flow(workspace_id="ws-continuation")
    _attempt(flow_id=flow_id)
    _store(flow_id, workspace_id="ws-continuation")

    decision = evaluate_direct_continuation(
        flow_id=flow_id,
        workspace_id="ws-continuation",
        expected_sensitivity_level="S1",
        now=NOW,
    )
    evidence = apply_continuation_lineage(_base_evidence(), decision)

    assert decision.eligible is True
    assert decision.reason == "eligible"
    assert decision.parent_attempt_id == "attempt-0"
    assert decision.parent_flow_attempt_index == 0
    assert decision.next_continuation_index == 1
    assert decision.protected_segment_index == 0
    assert evidence.parent_attempt_id == "attempt-0"
    assert evidence.continuation_index == 1


@pytest.mark.parametrize(
    ("adapter_invoked", "finish_reason", "status", "expected"),
    [
        (0, "length", "success", "adapter_not_invoked"),
        (1, "stop", "success", "finish_reason_not_length"),
        (1, "content_filter", "success", "finish_reason_not_length"),
        (1, "tool_call", "success", "finish_reason_not_length"),
        (1, "unknown", "success", "finish_reason_not_length"),
        (1, "error", "provider_error", "attempt_not_successful"),
    ],
)
def test_only_exact_length_success_after_adapter_invocation_is_eligible(
    initialized_database,
    adapter_invoked: int,
    finish_reason: str,
    status: str,
    expected: str,
) -> None:
    flow_id = _flow()
    _attempt(
        flow_id=flow_id,
        adapter_invoked=adapter_invoked,
        finish_reason=finish_reason,
        status=status,
    )

    decision = evaluate_direct_continuation(
        flow_id=flow_id,
        workspace_id=None,
        expected_sensitivity_level="S1",
        now=NOW,
    )
    assert decision.eligible is False
    assert decision.reason == expected


def test_valid_length_without_protected_segment_is_ineligible(
    initialized_database,
) -> None:
    flow_id = _flow()
    _attempt(flow_id=flow_id)

    decision = evaluate_direct_continuation(
        flow_id=flow_id,
        workspace_id=None,
        expected_sensitivity_level="S1",
        now=NOW,
    )
    assert decision.eligible is False
    assert decision.reason == "segment_missing"


def test_guard_zero_produces_honest_exhaustion_without_hidden_retry(
    initialized_database,
) -> None:
    flow_id = _flow(snapshot=0)
    _attempt(flow_id=flow_id)
    _store(flow_id)

    decision = evaluate_direct_continuation(
        flow_id=flow_id,
        workspace_id=None,
        expected_sensitivity_level="S1",
        now=NOW,
    )
    assert decision.eligible is False
    assert decision.reason == "guard_exhausted"
    assert decision.next_continuation_index == 1
    with pytest.raises(TokenFlowConflictError, match="eligible decision"):
        apply_continuation_lineage(_base_evidence(), decision)


def test_nonrunning_or_empty_flow_is_ineligible(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    empty = _flow()
    assert evaluate_direct_continuation(
        flow_id=empty,
        workspace_id=None,
        expected_sensitivity_level="S1",
        now=NOW,
    ).reason == "no_attempt"

    _attempt(flow_id=empty)
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_flows SET state = 'partial_terminal' WHERE id = ?",
            (empty,),
        )
        connection.commit()
    assert evaluate_direct_continuation(
        flow_id=empty,
        workspace_id=None,
        expected_sensitivity_level="S1",
        now=NOW,
    ).reason == "flow_not_running"


def test_segment_integrity_and_sensitivity_failures_propagate_fail_closed(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    flow_id = _flow()
    _attempt(flow_id=flow_id)
    _store(flow_id)

    with pytest.raises(TokenFlowConflictError, match="sensitivity"):
        evaluate_direct_continuation(
            flow_id=flow_id,
            workspace_id=None,
            expected_sensitivity_level="S0",
            now=NOW,
        )

    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_flow_segments SET body_text = 'tampered' WHERE flow_id = ?",
            (flow_id,),
        )
        connection.commit()
    with pytest.raises(TokenFlowConflictError, match="digest evidence"):
        evaluate_direct_continuation(
            flow_id=flow_id,
            workspace_id=None,
            expected_sensitivity_level="S1",
            now=NOW,
        )


def test_lineage_application_rejects_prepopulated_evidence(
    initialized_database,
) -> None:
    flow_id = _flow()
    _attempt(flow_id=flow_id)
    _store(flow_id)
    decision = evaluate_direct_continuation(
        flow_id=flow_id,
        workspace_id=None,
        expected_sensitivity_level="S1",
        now=NOW,
    )
    prepopulated = replace(
        _base_evidence(),
        parent_attempt_id="other",
        continuation_index=1,
    )

    with pytest.raises(TokenFlowConflictError, match="already carries lineage"):
        apply_continuation_lineage(prepopulated, decision)
