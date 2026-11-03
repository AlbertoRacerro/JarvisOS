from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app.core.database import open_sqlite_connection
from app.modules.ai.token_flow_continuation import ContinuationDecision
from app.modules.ai.token_flow_evidence import AttemptEvidence
from app.modules.ai.token_flow_segments import (
    _continuation_guard_digest,
    _parse_datetime,
    _policy_binding_digest,
)
from app.modules.ai.token_flow_service import (
    ID_RE,
    Flow,
    TokenFlowConflictError,
    TokenFlowNotFoundError,
    _require_flow,
    _safe,
)
from app.modules.ai.token_flow_transaction import (
    record_attempt_evidence_in_transaction,
)


def record_continuation_attempt_evidence(
    *,
    flow_id: str,
    attempt_id: str,
    evidence: AttemptEvidence,
    decision: ContinuationDecision,
    now: datetime | None = None,
) -> Flow:
    """Atomically revalidate and record one fresh continuation attempt.

    Eligibility decisions are deliberately side-effect free. This transaction binds a
    decision to the current parent, protected segment, guard snapshot, and target job
    before delegating the final write to the merged 061a attempt-evidence authority.
    Exact replay remains idempotent.
    """

    flow_id = _safe(flow_id, ID_RE, "flow_id")
    attempt_id = _safe(attempt_id, ID_RE, "attempt_id")
    current = _utc_datetime(now)
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = record_continuation_attempt_evidence_in_transaction(
                connection,
                flow_id=flow_id,
                attempt_id=attempt_id,
                evidence=evidence,
                decision=decision,
                now=current,
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise


def record_continuation_attempt_evidence_in_transaction(
    connection: sqlite3.Connection,
    *,
    flow_id: str,
    attempt_id: str,
    evidence: AttemptEvidence,
    decision: ContinuationDecision,
    now: datetime | None = None,
) -> Flow:
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    flow_id = _safe(flow_id, ID_RE, "flow_id")
    attempt_id = _safe(attempt_id, ID_RE, "attempt_id")
    if not isinstance(evidence, AttemptEvidence):
        raise TypeError("evidence must be AttemptEvidence")
    _require_eligible_decision(flow_id, decision)
    current = _utc_datetime(now)

    flow = _require_flow(connection, flow_id)
    target = _require_target(connection, attempt_id)
    replay = target["flow_id"] is not None or target["flow_attempt_index"] is not None
    if replay:
        if target["flow_id"] != flow_id or target["flow_attempt_index"] is None:
            raise TokenFlowConflictError(
                "continuation target is linked to another flow"
            )
    elif flow["state"] != "running":
        raise TokenFlowConflictError(
            "only running flows can accept a continuation attempt"
        )

    parent_id = str(decision.parent_attempt_id)
    parent = _require_parent(connection, parent_id, flow_id)
    parent_index = int(parent["flow_attempt_index"])
    if parent_index != decision.parent_flow_attempt_index:
        raise TokenFlowConflictError(
            "continuation parent index changed after eligibility"
        )
    next_continuation = int(decision.next_continuation_index)
    expected_continuation = _parent_continuation_index(parent) + 1
    if next_continuation != expected_continuation:
        raise TokenFlowConflictError(
            "continuation index is not the exact parent successor"
        )
    if next_continuation > int(flow["max_direct_continuations_snapshot"]):
        raise TokenFlowConflictError(
            "continuation index exceeds the flow guard snapshot"
        )
    if evidence.parent_attempt_id != parent_id:
        raise TokenFlowConflictError(
            "continuation evidence parent does not match the decision"
        )
    if evidence.continuation_index != next_continuation:
        raise TokenFlowConflictError(
            "continuation evidence index does not match the decision"
        )

    if replay:
        if int(target["flow_attempt_index"]) != parent_index + 1:
            raise TokenFlowConflictError(
                "continuation target is not the immediate child of its parent"
            )
    else:
        latest = connection.execute(
            """
            SELECT id, flow_attempt_index, continuation_index
            FROM ai_jobs WHERE flow_id = ?
            ORDER BY flow_attempt_index DESC LIMIT 1
            """,
            (flow_id,),
        ).fetchone()
        if latest is None or str(latest["id"]) != parent_id:
            raise TokenFlowConflictError(
                "continuation parent is no longer the latest attempt"
            )
        if int(latest["flow_attempt_index"]) != parent_index:
            raise TokenFlowConflictError(
                "continuation parent ordering changed after eligibility"
            )
        if int(flow["continuation_count"]) != _parent_continuation_index(parent):
            raise TokenFlowConflictError(
                "continuation aggregate changed after eligibility"
            )

    _require_parent_is_length_stop(parent)
    _revalidate_segment(
        connection,
        flow=flow,
        parent=parent,
        segment_index=int(decision.protected_segment_index),
        current=current,
    )
    return record_attempt_evidence_in_transaction(
        connection,
        flow_id=flow_id,
        attempt_id=attempt_id,
        evidence=evidence,
    )


def _require_eligible_decision(
    flow_id: str, decision: ContinuationDecision
) -> None:
    if not isinstance(decision, ContinuationDecision):
        raise TypeError("decision must be ContinuationDecision")
    if not decision.eligible or decision.reason != "eligible":
        raise TokenFlowConflictError(
            "continuation transaction requires an eligible decision"
        )
    if decision.flow_id != flow_id:
        raise TokenFlowConflictError(
            "continuation decision does not match the flow"
        )
    if (
        decision.parent_attempt_id is None
        or decision.parent_flow_attempt_index is None
        or decision.next_continuation_index is None
        or decision.protected_segment_index is None
    ):
        raise TokenFlowConflictError(
            "eligible continuation decision omitted authority evidence"
        )


def _require_target(
    connection: sqlite3.Connection, attempt_id: str
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT id, flow_id, flow_attempt_index, parent_attempt_id,
               continuation_index
        FROM ai_jobs WHERE id = ?
        """,
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise TokenFlowNotFoundError(f"ai_job {attempt_id} does not exist")
    if (row["flow_id"] is None) != (row["flow_attempt_index"] is None):
        raise TokenFlowConflictError(
            "continuation target has partial flow linkage"
        )
    return row


def _require_parent(
    connection: sqlite3.Connection, parent_id: str, flow_id: str
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT id, flow_id, flow_attempt_index, continuation_index, status,
               adapter_invoked, normalized_finish_reason, output_digest,
               output_tokens, requested_route_class, selected_route_class,
               provider_id, model_id, fallback_index, route_reason_json
        FROM ai_jobs WHERE id = ?
        """,
        (parent_id,),
    ).fetchone()
    if (
        row is None
        or row["flow_id"] != flow_id
        or row["flow_attempt_index"] is None
    ):
        raise TokenFlowConflictError(
            "continuation parent does not belong to the flow"
        )
    return row


def _require_parent_is_length_stop(parent: sqlite3.Row) -> None:
    if parent["status"] != "success":
        raise TokenFlowConflictError(
            "continuation parent is no longer successful"
        )
    if parent["adapter_invoked"] != 1:
        raise TokenFlowConflictError(
            "continuation parent no longer proves adapter invocation"
        )
    if parent["normalized_finish_reason"] != "length":
        raise TokenFlowConflictError(
            "continuation parent is no longer an exact length stop"
        )


def _parent_continuation_index(parent: sqlite3.Row) -> int:
    value = parent["continuation_index"]
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TokenFlowConflictError(
            "continuation parent index is malformed"
        )
    return value


def _revalidate_segment(
    connection: sqlite3.Connection,
    *,
    flow: sqlite3.Row,
    parent: sqlite3.Row,
    segment_index: int,
    current: datetime,
) -> None:
    row = connection.execute(
        """
        SELECT * FROM ai_flow_segments
        WHERE flow_id = ? AND segment_index = ?
        """,
        (flow["id"], segment_index),
    ).fetchone()
    if row is None:
        raise TokenFlowConflictError(
            "continuation protected segment disappeared"
        )
    if row["originating_attempt_id"] != parent["id"]:
        raise TokenFlowConflictError(
            "continuation protected segment changed parent"
        )
    if row["body_digest"] != parent["output_digest"]:
        raise TokenFlowConflictError(
            "continuation protected segment digest changed"
        )
    if row["token_count"] != parent["output_tokens"]:
        raise TokenFlowConflictError(
            "continuation protected segment token count changed"
        )
    if row["policy_binding_digest"] != _policy_binding_digest(flow, parent):
        raise TokenFlowConflictError(
            "continuation protected segment policy binding changed"
        )
    if row["continuation_guard_digest"] != _continuation_guard_digest(flow):
        raise TokenFlowConflictError(
            "continuation protected segment guard binding changed"
        )
    if _parse_datetime(row["expires_at"], "expires_at") <= current:
        raise TokenFlowConflictError(
            "continuation protected segment has expired"
        )


def _utc_datetime(value: datetime | None) -> datetime:
    current = value if value is not None else datetime.now(UTC)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise TypeError("now must be a timezone-aware datetime")
    return current.astimezone(UTC)
