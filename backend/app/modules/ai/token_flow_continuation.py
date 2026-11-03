from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from app.core.database import open_sqlite_connection
from app.modules.ai.token_flow_evidence import AttemptEvidence
from app.modules.ai.token_flow_segments import read_protected_segment
from app.modules.ai.token_flow_service import (
    ID_RE,
    TokenFlowConflictError,
    _require_flow,
    _safe,
)

ContinuationReason = Literal[
    "eligible",
    "flow_not_running",
    "no_attempt",
    "attempt_not_successful",
    "adapter_not_invoked",
    "finish_reason_not_length",
    "segment_missing",
    "guard_exhausted",
]


@dataclass(frozen=True, slots=True)
class ContinuationDecision:
    eligible: bool
    reason: ContinuationReason
    flow_id: str
    parent_attempt_id: str | None
    parent_flow_attempt_index: int | None
    next_continuation_index: int | None
    protected_segment_index: int | None


def evaluate_direct_continuation(
    *,
    flow_id: str,
    workspace_id: str | None,
    expected_sensitivity_level: str,
    now: datetime | None = None,
) -> ContinuationDecision:
    """Evaluate the exact 061b direct-continuation trigger without side effects.

    This slice owns only finish/segment/guard eligibility. Fresh route, capability,
    context-capacity, policy, budget, and 059b checks remain mandatory before a later
    runtime slice may create or dispatch the continuation attempt.
    """

    flow_id = _safe(flow_id, ID_RE, "flow_id")
    with open_sqlite_connection() as connection:
        flow = _require_flow(connection, flow_id)
        if flow["state"] != "running":
            return _decision(False, "flow_not_running", flow_id)
        attempt = _latest_attempt(connection, flow_id)
        if attempt is None:
            return _decision(False, "no_attempt", flow_id)
        parent_id = str(attempt["id"])
        parent_index = int(attempt["flow_attempt_index"])
        if attempt["status"] != "success":
            return _decision(
                False,
                "attempt_not_successful",
                flow_id,
                parent_id=parent_id,
                parent_index=parent_index,
            )
        if attempt["adapter_invoked"] != 1:
            return _decision(
                False,
                "adapter_not_invoked",
                flow_id,
                parent_id=parent_id,
                parent_index=parent_index,
            )
        if attempt["normalized_finish_reason"] != "length":
            return _decision(
                False,
                "finish_reason_not_length",
                flow_id,
                parent_id=parent_id,
                parent_index=parent_index,
            )

        current_continuation = _continuation_index(
            attempt["continuation_index"]
        )
        if int(flow["continuation_count"]) != current_continuation:
            raise TokenFlowConflictError(
                "flow continuation aggregate does not match latest attempt"
            )
        next_continuation = current_continuation + 1
        if next_continuation > int(flow["max_direct_continuations_snapshot"]):
            return _decision(
                False,
                "guard_exhausted",
                flow_id,
                parent_id=parent_id,
                parent_index=parent_index,
                next_index=next_continuation,
            )

        segments = connection.execute(
            """
            SELECT segment_index FROM ai_flow_segments
            WHERE flow_id = ? AND originating_attempt_id = ?
            ORDER BY segment_index
            """,
            (flow_id, parent_id),
        ).fetchall()
        if not segments:
            return _decision(
                False,
                "segment_missing",
                flow_id,
                parent_id=parent_id,
                parent_index=parent_index,
                next_index=next_continuation,
            )
        if len(segments) != 1:
            raise TokenFlowConflictError(
                "latest attempt must bind exactly one protected segment"
            )
        segment_index = int(segments[0]["segment_index"])

    validated = read_protected_segment(
        flow_id=flow_id,
        segment_index=segment_index,
        workspace_id=workspace_id,
        expected_sensitivity_level=expected_sensitivity_level,
        now=now,
    )
    if validated.metadata.originating_attempt_id != parent_id:
        raise TokenFlowConflictError(
            "protected segment no longer matches latest attempt"
        )
    return _decision(
        True,
        "eligible",
        flow_id,
        parent_id=parent_id,
        parent_index=parent_index,
        next_index=next_continuation,
        segment_index=segment_index,
    )


def apply_continuation_lineage(
    evidence: AttemptEvidence,
    decision: ContinuationDecision,
) -> AttemptEvidence:
    """Bind one fresh attempt evidence object to an eligible parent decision."""

    if not isinstance(evidence, AttemptEvidence):
        raise TypeError("evidence must be AttemptEvidence")
    if not isinstance(decision, ContinuationDecision):
        raise TypeError("decision must be ContinuationDecision")
    if not decision.eligible or decision.reason != "eligible":
        raise TokenFlowConflictError(
            "continuation lineage requires an eligible decision"
        )
    if (
        decision.parent_attempt_id is None
        or decision.next_continuation_index is None
    ):
        raise TokenFlowConflictError(
            "eligible continuation decision omitted lineage"
        )
    if evidence.parent_attempt_id is not None or evidence.continuation_index is not None:
        raise TokenFlowConflictError(
            "continuation evidence already carries lineage"
        )
    return replace(
        evidence,
        parent_attempt_id=decision.parent_attempt_id,
        continuation_index=decision.next_continuation_index,
    )


def _latest_attempt(
    connection: sqlite3.Connection, flow_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, status, flow_attempt_index, continuation_index,
               adapter_invoked, normalized_finish_reason
        FROM ai_jobs
        WHERE flow_id = ?
        ORDER BY flow_attempt_index DESC
        LIMIT 1
        """,
        (flow_id,),
    ).fetchone()


def _continuation_index(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TokenFlowConflictError(
            "latest attempt continuation index is malformed"
        )
    return value


def _decision(
    eligible: bool,
    reason: ContinuationReason,
    flow_id: str,
    *,
    parent_id: str | None = None,
    parent_index: int | None = None,
    next_index: int | None = None,
    segment_index: int | None = None,
) -> ContinuationDecision:
    return ContinuationDecision(
        eligible=eligible,
        reason=reason,
        flow_id=flow_id,
        parent_attempt_id=parent_id,
        parent_flow_attempt_index=parent_index,
        next_continuation_index=next_index,
        protected_segment_index=segment_index,
    )
