from __future__ import annotations

from datetime import datetime
from typing import Literal

from app.core.database import open_sqlite_connection
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.flow_record_capture import (
    capture_final_flow_records_in_transaction,
)
from app.modules.ai.token_flow_assembly import AssembledOutput
from app.modules.ai.token_flow_segments import (
    _sensitivity_level,
    _utc_datetime,
    _validate_segment_row,
    _workspace_id,
)
from app.modules.ai.token_flow_service import (
    ID_RE,
    REASON_RE,
    Flow,
    TokenFlowConflictError,
    _recompute,
    _require_flow,
    _safe,
    _validate_terminal_status,
)

TerminalAssemblyState = Literal["complete", "partial_terminal"]


def terminalize_assembled_output(
    *,
    flow_id: str,
    terminal_attempt_id: str,
    new_state: TerminalAssemblyState,
    terminal_reason: str,
    workspace_id: str | None,
    expected_sensitivity_level: str,
    now: datetime | None = None,
) -> tuple[Flow, AssembledOutput]:
    """Atomically validate protected segments and terminalize with their assembled digest."""

    flow_id = _safe(flow_id, ID_RE, "flow_id")
    terminal_attempt_id = _safe(
        terminal_attempt_id,
        ID_RE,
        "terminal_attempt_id",
    )
    terminal_reason = _safe(terminal_reason, REASON_RE, "terminal_reason")
    if new_state not in {"complete", "partial_terminal"}:
        raise ValueError("assembled terminalization supports complete or partial_terminal")
    workspace_id = _workspace_id(workspace_id)
    sensitivity = _sensitivity_level(expected_sensitivity_level)
    current = _utc_datetime(now)

    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            flow = _require_flow(connection, flow_id)
            if flow["state"] != "running":
                raise TokenFlowConflictError(
                    "only running flows can be terminalized from protected output"
                )
            if flow["workspace_id"] != workspace_id:
                raise TokenFlowConflictError(
                    "assembled terminalization workspace does not match flow"
                )

            terminal_attempt = connection.execute(
                """
                SELECT id, status, normalized_finish_reason, output_digest,
                       flow_attempt_index
                FROM ai_jobs
                WHERE id = ? AND flow_id = ?
                """,
                (terminal_attempt_id, flow_id),
            ).fetchone()
            if terminal_attempt is None:
                raise TokenFlowConflictError(
                    "terminal attempt does not belong to the flow"
                )
            latest = connection.execute(
                """
                SELECT id FROM ai_jobs
                WHERE flow_id = ?
                ORDER BY flow_attempt_index DESC
                LIMIT 1
                """,
                (flow_id,),
            ).fetchone()
            if latest is None or latest["id"] != terminal_attempt_id:
                raise TokenFlowConflictError(
                    "terminal attempt must be the latest ordered attempt"
                )
            _validate_terminal_status(new_state, terminal_attempt["status"])
            if new_state == "complete" and (
                terminal_attempt["status"] != "success"
                or terminal_attempt["normalized_finish_reason"] != "stop"
            ):
                raise TokenFlowConflictError(
                    "complete assembled output requires a successful exact stop"
                )

            rows = connection.execute(
                """
                SELECT * FROM ai_flow_segments
                WHERE flow_id = ?
                ORDER BY segment_index
                """,
                (flow_id,),
            ).fetchall()
            if not rows:
                raise TokenFlowConflictError(
                    "assembled terminalization requires protected output"
                )
            indexes = [int(row["segment_index"]) for row in rows]
            if indexes != list(range(len(rows))):
                raise TokenFlowConflictError(
                    "protected segment indexes must be contiguous"
                )

            validated = [
                _validate_segment_row(
                    connection,
                    row,
                    flow=flow,
                    workspace_id=workspace_id,
                    expected_sensitivity=sensitivity,
                    current=current,
                )
                for row in rows
            ]
            attempt_ids = tuple(
                item.metadata.originating_attempt_id for item in validated
            )
            if len(set(attempt_ids)) != len(attempt_ids):
                raise TokenFlowConflictError(
                    "assembled output contains duplicate originating attempts"
                )
            if terminal_attempt["output_digest"] is not None and (
                validated[-1].metadata.originating_attempt_id
                != terminal_attempt_id
            ):
                raise TokenFlowConflictError(
                    "latest output attempt does not own the final protected segment"
                )

            body_text = "".join(item.body_text for item in validated)
            if not body_text:
                raise TokenFlowConflictError(
                    "assembled terminalization produced an empty output"
                )
            assembled = AssembledOutput(
                flow_id=flow_id,
                body_digest=canonical_digest({"text": body_text}),
                byte_count=len(body_text.encode("utf-8")),
                token_count=sum(
                    item.metadata.token_count for item in validated
                ),
                segment_count=len(validated),
                segment_digests=tuple(
                    item.metadata.body_digest for item in validated
                ),
                originating_attempt_ids=attempt_ids,
                body_text=body_text,
            )

            updated = connection.execute(
                """
                UPDATE ai_flows
                SET state = ?, terminal_reason = ?, terminal_attempt_id = ?,
                    final_output_digest = ?, final_accounting_digest = NULL,
                    completed_at = ?, cancelled_at = NULL, updated_at = ?
                WHERE id = ? AND state = 'running'
                """,
                (
                    new_state,
                    terminal_reason,
                    terminal_attempt_id,
                    assembled.body_digest,
                    current.isoformat(),
                    current.isoformat(),
                    flow_id,
                ),
            )
            if updated.rowcount != 1:
                raise TokenFlowConflictError(
                    "assembled terminalization changed concurrently"
                )
            result = _recompute(connection, flow_id)
            if result["final_output_digest"] != assembled.body_digest:
                raise TokenFlowConflictError(
                    "assembled output digest was not preserved"
                )
            if new_state == "complete":
                capture_final_flow_records_in_transaction(
                    connection,
                    flow_id=flow_id,
                    task_kind=str(flow["task_kind"]),
                    response_text=assembled.body_text,
                    terminal_attempt_id=terminal_attempt_id,
                    workspace_id=workspace_id,
                )
            connection.commit()
            return result, assembled
        except Exception:
            connection.rollback()
            raise
