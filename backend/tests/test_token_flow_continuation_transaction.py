from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.token_flow_continuation import (
    apply_continuation_lineage,
    evaluate_direct_continuation,
)
from app.modules.ai.token_flow_continuation_transaction import (
    record_continuation_attempt_evidence,
)
from app.modules.ai.token_flow_evidence import AttemptEvidence
from app.modules.ai.token_flow_segments import store_protected_segment
from app.modules.ai.token_flow_service import (
    TokenFlowConflictError,
    create_flow,
)

PARENT_BODY = "Parent output stopped at the exact output limit."
CHILD_BODY = "Fresh continuation output."
NOW = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)


@pytest.fixture
def initialized_database():
    from app.core.database import initialize_database
    from app.modules.ai.settings import ensure_ai_settings

    initialize_database()
    ensure_ai_settings()


def _insert_linked_attempt(
    *,
    attempt_id: str,
    flow_id: str,
    flow_attempt_index: int,
    continuation_index: int | None,
    body: str,
    finish_reason: str,
    parent_attempt_id: str | None = None,
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
                flow_id, flow_attempt_index, parent_attempt_id, continuation_index,
                execution_class, adapter_invoked, external_dispatch_state,
                requested_output_ceiling, effective_output_ceiling,
                normalized_finish_reason, normalized_usage_source,
                accounting_basis, accounted_provider_spend_usd_decimal,
                outcome_reason, capability_version, accounting_version
            ) VALUES (
                ?, ?, 'success', 'synthesis', 'local:fake', 'local:fake',
                'fake', 'fake-v0', 0, ?, ?, 5, 8,
                ?, ?, ?, ?, 'synthetic', 1, 'not_applicable',
                128, 128, ?, 'estimated', 'synthetic_not_economic',
                '0', 'success', 'provider-registry-v1', 'token-flow-v0'
            )
            """,
            (
                attempt_id,
                utc_now(),
                '{"decision_reason":"bound:local:fake","fallback_attempt_index":0}',
                canonical_digest({"text": body}),
                flow_id,
                flow_attempt_index,
                parent_attempt_id,
                continuation_index,
                finish_reason,
            ),
        )
        connection.commit()


def _insert_unlinked_target(attempt_id: str = "child") -> None:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import utc_now

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, requested_route_class,
                selected_route_class, provider_id, model_id, fallback_index,
                route_reason_json, output_digest, input_tokens, output_tokens
            ) VALUES (
                ?, ?, 'success', 'synthesis', 'local:fake', 'local:fake',
                'fake', 'fake-v0', 0, ?, ?, 3, 6
            )
            """,
            (
                attempt_id,
                utc_now(),
                '{"decision_reason":"bound:local:fake","fallback_attempt_index":0}',
                canonical_digest({"text": CHILD_BODY}),
            ),
        )
        connection.commit()


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


def _prepared() -> tuple[str, object, AttemptEvidence]:
    flow = create_flow(
        task_kind="synthesis",
        requested_route_class="local:fake",
    )
    flow_id = str(flow["id"])
    _insert_linked_attempt(
        attempt_id="parent",
        flow_id=flow_id,
        flow_attempt_index=0,
        continuation_index=None,
        body=PARENT_BODY,
        finish_reason="length",
    )
    store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id="parent",
        body_text=PARENT_BODY,
        effective_sensitivity_level="S1",
        workspace_id=None,
        now=NOW,
    )
    decision = evaluate_direct_continuation(
        flow_id=flow_id,
        workspace_id=None,
        expected_sensitivity_level="S1",
        now=NOW,
    )
    evidence = apply_continuation_lineage(_base_evidence(), decision)
    _insert_unlinked_target()
    return flow_id, decision, evidence


def test_records_fresh_immediate_child_and_exact_replay(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    flow_id, decision, evidence = _prepared()

    first = record_continuation_attempt_evidence(
        flow_id=flow_id,
        attempt_id="child",
        evidence=evidence,
        decision=decision,
        now=NOW,
    )
    replay = record_continuation_attempt_evidence(
        flow_id=flow_id,
        attempt_id="child",
        evidence=evidence,
        decision=decision,
        now=NOW,
    )

    assert first["ordered_attempt_ids"] == ["parent", "child"]
    assert replay["ordered_attempt_ids"] == ["parent", "child"]
    assert replay["continuation_count"] == 1
    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT flow_id, flow_attempt_index, parent_attempt_id,
                   continuation_index
            FROM ai_jobs WHERE id = 'child'
            """
        ).fetchone()
    assert tuple(row) == (flow_id, 1, "parent", 1)


def test_stale_decision_cannot_skip_a_new_latest_attempt(
    initialized_database,
) -> None:
    flow_id, decision, evidence = _prepared()
    _insert_linked_attempt(
        attempt_id="interloper",
        flow_id=flow_id,
        flow_attempt_index=1,
        continuation_index=1,
        parent_attempt_id="parent",
        body="intervening output",
        finish_reason="length",
    )

    with pytest.raises(TokenFlowConflictError, match="no longer the latest"):
        record_continuation_attempt_evidence(
            flow_id=flow_id,
            attempt_id="child",
            evidence=evidence,
            decision=decision,
            now=NOW,
        )


def test_decision_and_evidence_cannot_skip_continuation_index(
    initialized_database,
) -> None:
    flow_id, decision, evidence = _prepared()
    forged_decision = replace(decision, next_continuation_index=2)
    forged_evidence = replace(evidence, continuation_index=2)

    with pytest.raises(TokenFlowConflictError, match="exact parent successor"):
        record_continuation_attempt_evidence(
            flow_id=flow_id,
            attempt_id="child",
            evidence=forged_evidence,
            decision=forged_decision,
            now=NOW,
        )


def test_decision_and_evidence_lineage_must_match(
    initialized_database,
) -> None:
    flow_id, decision, evidence = _prepared()

    with pytest.raises(TokenFlowConflictError, match="evidence parent"):
        record_continuation_attempt_evidence(
            flow_id=flow_id,
            attempt_id="child",
            evidence=replace(evidence, parent_attempt_id="other"),
            decision=decision,
            now=NOW,
        )
    with pytest.raises(TokenFlowConflictError, match="evidence index"):
        record_continuation_attempt_evidence(
            flow_id=flow_id,
            attempt_id="child",
            evidence=replace(evidence, continuation_index=2),
            decision=decision,
            now=NOW,
        )


def test_segment_mutation_after_decision_rolls_back_target(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    flow_id, decision, evidence = _prepared()
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_flow_segments SET body_digest = ? WHERE flow_id = ?",
            (canonical_digest({"text": "forged"}), flow_id),
        )
        connection.commit()

    with pytest.raises(TokenFlowConflictError, match="digest changed"):
        record_continuation_attempt_evidence(
            flow_id=flow_id,
            attempt_id="child",
            evidence=evidence,
            decision=decision,
            now=NOW,
        )
    with open_sqlite_connection() as connection:
        target = connection.execute(
            "SELECT flow_id, parent_attempt_id, continuation_index "
            "FROM ai_jobs WHERE id = 'child'"
        ).fetchone()
    assert tuple(target) == (None, None, None)


def test_target_linked_to_another_flow_is_rejected(
    initialized_database,
) -> None:
    from app.core.database import open_sqlite_connection

    flow_id, decision, evidence = _prepared()
    other = create_flow(task_kind="synthesis", requested_route_class="local:fake")
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_jobs SET flow_id = ?, flow_attempt_index = 0 "
            "WHERE id = 'child'",
            (other["id"],),
        )
        connection.commit()

    with pytest.raises(TokenFlowConflictError, match="another flow"):
        record_continuation_attempt_evidence(
            flow_id=flow_id,
            attempt_id="child",
            evidence=evidence,
            decision=decision,
            now=NOW,
        )


def test_exact_replay_remains_valid_after_a_later_attempt_exists(
    initialized_database,
) -> None:
    flow_id, decision, evidence = _prepared()
    record_continuation_attempt_evidence(
        flow_id=flow_id,
        attempt_id="child",
        evidence=evidence,
        decision=decision,
        now=NOW,
    )
    _insert_linked_attempt(
        attempt_id="later",
        flow_id=flow_id,
        flow_attempt_index=2,
        continuation_index=2,
        parent_attempt_id="child",
        body="later output",
        finish_reason="stop",
    )

    replay = record_continuation_attempt_evidence(
        flow_id=flow_id,
        attempt_id="child",
        evidence=evidence,
        decision=decision,
        now=NOW,
    )
    assert replay["ordered_attempt_ids"] == ["parent", "child", "later"]
    assert replay["continuation_count"] == 2
