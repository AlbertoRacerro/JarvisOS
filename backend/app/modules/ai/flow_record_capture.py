from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from app.core.database import open_sqlite_connection
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.record_capture import (
    RECORD_CAPTURE_TASK_KINDS,
    parse_jarvis_records_block,
)
from app.modules.events.service import utc_now
from app.modules.memory import service as memory_service
from app.modules.memory.models import MemoryProposalCreate


class FlowRecordCaptureConflictError(RuntimeError):
    """A terminal flow record capture did not match canonical flow state."""


@dataclass(frozen=True, slots=True)
class FlowRecordCaptureResult:
    proposal_ids: tuple[str, ...]
    parse_error: str | None
    captured: bool
    replayed: bool


def capture_final_flow_records(
    *,
    task_kind: str,
    response_text: str | None,
    terminal_attempt_id: str,
    workspace_id: str | None,
) -> FlowRecordCaptureResult | None:
    """Capture one complete flow's assembled record block atomically and idempotently.

    ``None`` preserves the legacy non-flow boundary. A non-complete flow returns a
    non-captured result and never creates a receipt or proposal.
    """

    if task_kind not in RECORD_CAPTURE_TASK_KINDS or response_text is None:
        return FlowRecordCaptureResult((), None, False, False)

    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            job = connection.execute(
                "SELECT flow_id FROM ai_jobs WHERE id = ?",
                (terminal_attempt_id,),
            ).fetchone()
            if job is None:
                raise FlowRecordCaptureConflictError(
                    "record capture terminal attempt was not found"
                )
            flow_id = job["flow_id"]
            if flow_id is None:
                connection.rollback()
                return None

            result = capture_final_flow_records_in_transaction(
                connection,
                flow_id=str(flow_id),
                task_kind=task_kind,
                response_text=response_text,
                terminal_attempt_id=terminal_attempt_id,
                workspace_id=workspace_id,
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise


def capture_final_flow_records_in_transaction(
    connection: sqlite3.Connection,
    *,
    flow_id: str,
    task_kind: str,
    response_text: str | None,
    terminal_attempt_id: str,
    workspace_id: str | None,
) -> FlowRecordCaptureResult:
    """Create or replay one terminal flow receipt inside the caller's transaction."""

    if task_kind not in RECORD_CAPTURE_TASK_KINDS or response_text is None:
        return FlowRecordCaptureResult((), None, False, False)

    parsed = parse_jarvis_records_block(response_text)
    response_digest = canonical_digest({"text": response_text})
    job = connection.execute(
        "SELECT flow_id FROM ai_jobs WHERE id = ?",
        (terminal_attempt_id,),
    ).fetchone()
    if job is None:
        raise FlowRecordCaptureConflictError(
            "record capture terminal attempt was not found"
        )
    if job["flow_id"] != flow_id:
        raise FlowRecordCaptureConflictError(
            "record capture terminal attempt does not belong to flow"
        )

    flow = connection.execute(
        """
        SELECT state, task_kind, workspace_id, terminal_attempt_id,
               final_output_digest
        FROM ai_flows
        WHERE id = ?
        """,
        (flow_id,),
    ).fetchone()
    if flow is None:
        raise FlowRecordCaptureConflictError("record capture flow was not found")
    if flow["state"] != "complete":
        return FlowRecordCaptureResult((), None, False, False)
    if flow["task_kind"] != task_kind:
        raise FlowRecordCaptureConflictError(
            "record capture task kind does not match flow"
        )
    if flow["terminal_attempt_id"] != terminal_attempt_id:
        raise FlowRecordCaptureConflictError(
            "record capture attempt is not the canonical terminal attempt"
        )
    if flow["final_output_digest"] != response_digest:
        raise FlowRecordCaptureConflictError(
            "record capture response does not match final assembled output"
        )
    flow_workspace_id = flow["workspace_id"]
    if (
        flow_workspace_id is not None
        and workspace_id is not None
        and workspace_id != flow_workspace_id
    ):
        raise FlowRecordCaptureConflictError(
            "record capture workspace does not match flow"
        )

    existing = connection.execute(
        """
        SELECT terminal_attempt_id, final_output_digest, proposal_ids_json,
               parse_error
        FROM ai_flow_record_captures
        WHERE flow_id = ?
        """,
        (flow_id,),
    ).fetchone()
    if existing is not None:
        if (
            existing["terminal_attempt_id"] != terminal_attempt_id
            or existing["final_output_digest"] != response_digest
        ):
            raise FlowRecordCaptureConflictError(
                "record capture receipt changed for terminal flow"
            )
        return FlowRecordCaptureResult(
            _proposal_ids(existing["proposal_ids_json"]),
            existing["parse_error"],
            True,
            True,
        )

    proposal_ids: list[str] = []
    errors: list[str] = [parsed.error] if parsed.error else []
    if parsed.records and (
        not isinstance(flow_workspace_id, str) or not flow_workspace_id.strip()
    ):
        errors.append("records_workspace_error: workspace_id is required")
    elif parsed.records:
        for index, record in enumerate(parsed.records):
            try:
                payload = MemoryProposalCreate(
                    workspace_id=flow_workspace_id,
                    source_ai_job_id=terminal_attempt_id,
                    **record,
                )
                created = memory_service._create_proposal_in_transaction(
                    connection, payload
                )
            except ValueError as exc:
                errors.append(f"record_create_error[{index}]: {exc}")
                continue
            proposal_ids.append(created.id)

    parse_error = "; ".join(errors) if errors else None
    connection.execute(
        """
        INSERT INTO ai_flow_record_captures (
            flow_id, terminal_attempt_id, final_output_digest,
            proposal_ids_json, parse_error, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            flow_id,
            terminal_attempt_id,
            response_digest,
            json.dumps(proposal_ids, separators=(",", ":")),
            parse_error,
            utc_now(),
        ),
    )
    return FlowRecordCaptureResult(
        tuple(proposal_ids),
        parse_error,
        True,
        False,
    )


def _proposal_ids(payload: object) -> tuple[str, ...]:
    try:
        values = json.loads(str(payload))
    except json.JSONDecodeError as exc:
        raise FlowRecordCaptureConflictError(
            "record capture receipt proposal ids are malformed"
        ) from exc
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise FlowRecordCaptureConflictError(
            "record capture receipt proposal ids are malformed"
        )
    return tuple(values)
