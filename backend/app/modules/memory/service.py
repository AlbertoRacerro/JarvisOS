import sqlite3
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.core.repository import row_to_model, rows_to_models
from app.modules.events.service import log_event, utc_now
from app.modules.flowsheet.freshness import (
    invalidation_summary_from_connection,
    persist_freshness_invalidation,
    prepare_freshness_invalidation,
)
from app.modules.memory.models import (
    CalcParameterProposalCreate,
    MemoryProposalCreate,
    MemoryRecordRead,
    ParameterReplacementInvalidationRead,
    ParameterReplacementRead,
)
from app.modules.memory.replacement import (
    ParameterReplacementError,
    validate_parameter_replacement_proposal,
)

_ALLOWED_STATUSES = {"proposed", "accepted", "rejected", "superseded"}
_TABLE_BY_KIND = {
    "assumption": "assumptions",
    "parameter": "parameters",
    "decision": "decisions",
}
_TARGET_BY_KIND = {
    "assumption": "Assumption",
    "parameter": "Parameter",
    "decision": "Decision",
}


def _require_workspace(connection: sqlite3.Connection, workspace_id: str) -> None:
    row = connection.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if row is None:
        raise ValueError("Workspace not found.")


def _require_ai_job(connection: sqlite3.Connection, source_ai_job_id: str | None) -> str:
    if source_ai_job_id is None or not source_ai_job_id.strip():
        raise ValueError("source_ai_job_id is required for AI proposals.")
    source_ai_job_id = source_ai_job_id.strip()
    row = connection.execute("SELECT id FROM ai_jobs WHERE id = ?", (source_ai_job_id,)).fetchone()
    if row is None:
        raise ValueError("source_ai_job_id does not reference an existing AI job.")
    return source_ai_job_id


def _normalize_status(status: str) -> str:
    if status in _ALLOWED_STATUSES:
        return status
    return "proposed"


def _select_record_sql(kind: str) -> str:
    if kind == "assumption":
        return """
            SELECT id, 'assumption' AS record_kind, workspace_id,
                CASE WHEN status IN ('proposed', 'accepted', 'rejected', 'superseded') THEN status ELSE 'proposed' END AS status,
                origin, source_ai_job_id, promoted_at, created_at, updated_at,
                NULL AS title, statement, NULL AS decision_text, NULL AS name, source_ref, notes,
                NULL AS supersedes_parameter_id,
                scope, confidence, NULL AS symbol, NULL AS value, NULL AS unit, NULL AS value_status,
                NULL AS value_min, NULL AS value_max, NULL AS rationale, NULL AS linked_run_id
            FROM assumptions
        """
    if kind == "parameter":
        return """
            SELECT id, 'parameter' AS record_kind, workspace_id,
                CASE WHEN status IN ('proposed', 'accepted', 'rejected', 'superseded') THEN status ELSE 'proposed' END AS status,
                origin, source_ai_job_id, promoted_at, created_at, updated_at,
                NULL AS title, NULL AS statement, NULL AS decision_text, name, source_ref, notes,
                supersedes_parameter_id,
                NULL AS scope, confidence, symbol, value, unit, value_status,
                value_min, value_max, NULL AS rationale, NULL AS linked_run_id
            FROM parameters
        """
    if kind == "decision":
        return """
            SELECT id, 'decision' AS record_kind, workspace_id,
                CASE WHEN status IN ('proposed', 'accepted', 'rejected', 'superseded') THEN status ELSE 'proposed' END AS status,
                origin, source_ai_job_id, promoted_at, created_at, updated_at,
                title, NULL AS statement, decision_text, NULL AS name, NULL AS source_ref, notes,
                NULL AS supersedes_parameter_id,
                NULL AS scope, NULL AS confidence, NULL AS symbol, NULL AS value, NULL AS unit, NULL AS value_status,
                NULL AS value_min, NULL AS value_max, rationale, linked_run_id
            FROM decisions
        """
    raise ValueError("Unsupported memory record kind.")


def _get_record(connection: sqlite3.Connection, kind: str, record_id: str) -> MemoryRecordRead | None:
    row = connection.execute(f"{_select_record_sql(kind)} WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        return None
    return row_to_model(row, MemoryRecordRead)


def _log_memory_event(connection: sqlite3.Connection, event_type: str, record: MemoryRecordRead) -> None:
    log_event(
        connection,
        event_type=event_type,
        actor="local-user",
        target_type=_TARGET_BY_KIND[record.record_kind],
        target_id=record.id,
        workspace_id=record.workspace_id,
        payload={"record_kind": record.record_kind, "status": record.status, "origin": record.origin},
    )


def _create_proposal_in_transaction(
    connection: sqlite3.Connection,
    payload: MemoryProposalCreate,
) -> MemoryRecordRead:
    now = utc_now()
    record_id = str(uuid4())
    _require_workspace(connection, payload.workspace_id)
    source_ai_job_id = _require_ai_job(connection, payload.source_ai_job_id)
    if payload.record_kind == "assumption":
        if not payload.statement:
            raise ValueError("statement is required for assumption proposals.")
        connection.execute(
            """
            INSERT INTO assumptions (
                id, workspace_id, statement, scope, confidence, status, source_ref,
                created_at, updated_at, notes, origin, source_ai_job_id, promoted_at
            ) VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?, 'ai_proposed', ?, NULL)
            """,
            (
                record_id,
                payload.workspace_id,
                payload.statement,
                payload.scope,
                payload.confidence if isinstance(payload.confidence, str) else None,
                payload.source_ref,
                now,
                now,
                payload.notes,
                source_ai_job_id,
            ),
        )
    elif payload.record_kind == "parameter":
        if not payload.name:
            raise ValueError("name is required for parameter proposals.")
        validate_parameter_replacement_proposal(
            connection,
            workspace_id=payload.workspace_id,
            supersedes_parameter_id=payload.supersedes_parameter_id,
            replacement_parameter_id=record_id,
            unit=payload.unit,
            value=payload.value,
        )
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, symbol, value, unit, value_status, value_min,
                value_max, source_ref, confidence, status, created_at, updated_at, notes,
                origin, source_ai_job_id, promoted_at, supersedes_parameter_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, 'ai_proposed', ?, NULL, ?)
            """,
            (
                record_id,
                payload.workspace_id,
                payload.name,
                payload.symbol,
                payload.value,
                payload.unit,
                payload.value_status,
                payload.value_min,
                payload.value_max,
                payload.source_ref,
                payload.confidence if isinstance(payload.confidence, int | float) else None,
                now,
                now,
                payload.notes,
                source_ai_job_id,
                payload.supersedes_parameter_id,
            ),
        )
    elif payload.record_kind == "decision":
        if not payload.title or not payload.decision_text:
            raise ValueError("title and decision_text are required for decision proposals.")
        connection.execute(
            """
            INSERT INTO decisions (
                id, workspace_id, title, decision_text, rationale, status, linked_run_id,
                created_at, updated_at, notes, origin, source_ai_job_id, promoted_at
            ) VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?, 'ai_proposed', ?, NULL)
            """,
            (
                record_id,
                payload.workspace_id,
                payload.title,
                payload.decision_text,
                payload.rationale,
                payload.linked_run_id,
                now,
                now,
                payload.notes,
                source_ai_job_id,
            ),
        )
    record = _get_record(connection, payload.record_kind, record_id)
    if record is None:
        raise ValueError("Proposal was not created.")
    _log_memory_event(connection, "MemoryProposalCreated", record)
    return record


def create_proposal(payload: MemoryProposalCreate) -> MemoryRecordRead:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            record = _create_proposal_in_transaction(connection, payload)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return record


def create_calc_parameter_proposals(payloads: list[CalcParameterProposalCreate]) -> list[MemoryRecordRead]:
    if not payloads:
        return []
    now = utc_now()
    created: list[tuple[str, str]] = []
    with open_sqlite_connection() as connection:
        for payload in payloads:
            _require_workspace(connection, payload.workspace_id)
            record_id = str(uuid4())
            source_ref = f"runner_job:{payload.runner_job_id}"
            connection.execute(
                """
                INSERT INTO parameters (
                    id, workspace_id, name, symbol, value, unit, value_status, value_min,
                    value_max, source_ref, confidence, status, created_at, updated_at, notes,
                    origin, source_ai_job_id, promoted_at, supersedes_parameter_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, 'calc', NULL, NULL, NULL)
                """,
                (
                    record_id,
                    payload.workspace_id,
                    payload.name,
                    payload.symbol,
                    payload.value,
                    payload.unit,
                    payload.value_status,
                    payload.value_min,
                    payload.value_max,
                    source_ref,
                    payload.confidence,
                    now,
                    now,
                    payload.notes,
                ),
            )
            created.append((record_id, payload.workspace_id))
        records = [_get_record(connection, "parameter", record_id) for record_id, _ in created]
        result = [record for record in records if record is not None]
        for record in result:
            _log_memory_event(connection, "MemoryProposalCreated", record)
        connection.commit()
    return result


def _transition(kind: str, record_id: str, target_status: str) -> MemoryRecordRead:
    if kind not in _TABLE_BY_KIND:
        raise ValueError("Unsupported memory record kind.")
    with open_sqlite_connection() as connection:
        record = _get_record(connection, kind, record_id)
        if record is None:
            raise ValueError("Memory record not found.")
        current_status = _normalize_status(record.status)
        if current_status != "proposed":
            raise ValueError(f"Cannot transition {current_status} record to {target_status}.")
        if kind == "parameter" and target_status == "accepted" and record.supersedes_parameter_id:
            raise ParameterReplacementError(
                "parameter_replacement_promotion_required",
                "Configured Parameter replacements require the replacement promotion endpoint.",
            )
        now = utc_now()
        promoted_at = now if target_status == "accepted" else record.promoted_at
        connection.execute(
            f"UPDATE {_TABLE_BY_KIND[kind]} SET status = ?, promoted_at = ?, updated_at = ? WHERE id = ?",
            (target_status, promoted_at, now, record_id),
        )
        updated = _get_record(connection, kind, record_id)
        if updated is None:
            raise ValueError("Memory record not found.")
        _log_memory_event(connection, f"MemoryProposal{target_status.title()}", updated)
        connection.commit()
    return updated


def promote_parameter_replacement(record_id: str) -> ParameterReplacementRead:
    try:
        with open_sqlite_connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT fi.id, fi.workspace_id, fi.superseded_parameter_id, fi.replacement_parameter_id,
                           superseded.lifecycle_state AS superseded_lifecycle_state,
                           replacement.lifecycle_state AS replacement_lifecycle_state
                    FROM freshness_invalidations AS fi
                    LEFT JOIN parameters AS superseded ON superseded.id = fi.superseded_parameter_id
                    LEFT JOIN parameters AS replacement ON replacement.id = fi.replacement_parameter_id
                    WHERE fi.replacement_parameter_id = ?
                    """,
                    (record_id,),
                ).fetchone()
                if existing is not None:
                    accepted = _get_record(connection, "parameter", str(existing["replacement_parameter_id"]))
                    superseded = _get_record(connection, "parameter", str(existing["superseded_parameter_id"]))
                    if (
                        accepted is None
                        or superseded is None
                        or accepted.status != "accepted"
                        or superseded.status != "superseded"
                        or existing["replacement_lifecycle_state"] != "active"
                        or existing["superseded_lifecycle_state"] != "superseded"
                    ):
                        raise ParameterReplacementError(
                            "parameter_replacement_state_inconsistent",
                            "Stored Parameter replacement state is inconsistent.",
                        )
                    summary = invalidation_summary_from_connection(connection, str(existing["id"]))
                    connection.rollback()
                    return _replacement_response(accepted, superseded, summary)

                replacement = connection.execute(
                    """
                    SELECT id, workspace_id, status, origin, value, unit, supersedes_parameter_id
                    FROM parameters WHERE id = ?
                    """,
                    (record_id,),
                ).fetchone()
                if replacement is None:
                    raise ParameterReplacementError(
                        "parameter_replacement_not_found",
                        "Parameter replacement proposal was not found.",
                    )
                if str(replacement["origin"]) == "calc":
                    raise ParameterReplacementError(
                        "parameter_replacement_not_configured",
                        "Calculation-originated Parameters cannot configure replacement in V0.",
                    )
                if _normalize_status(str(replacement["status"])) != "proposed":
                    raise ParameterReplacementError(
                        "parameter_replacement_state_inconsistent",
                        "Parameter replacement proposal is not proposed.",
                    )
                superseded_id = replacement["supersedes_parameter_id"]
                if superseded_id is None:
                    raise ParameterReplacementError(
                        "parameter_replacement_not_configured",
                        "Parameter replacement is not configured.",
                    )
                superseded_id = str(superseded_id)
                already = connection.execute(
                    "SELECT id FROM freshness_invalidations WHERE superseded_parameter_id = ?",
                    (superseded_id,),
                ).fetchone()
                if already is not None:
                    raise ParameterReplacementError(
                        "parameter_already_replaced",
                        "The accepted Parameter already has an accepted replacement.",
                    )
                validate_parameter_replacement_proposal(
                    connection,
                    workspace_id=str(replacement["workspace_id"]),
                    supersedes_parameter_id=superseded_id,
                    replacement_parameter_id=record_id,
                    unit=str(replacement["unit"]),
                    value=None if replacement["value"] is None else str(replacement["value"]),
                )

                now = utc_now()
                prepared = prepare_freshness_invalidation(
                    connection,
                    workspace_id=str(replacement["workspace_id"]),
                    superseded_parameter_id=superseded_id,
                    replacement_parameter_id=record_id,
                    created_at=now,
                )
                connection.execute(
                    """
                    UPDATE parameters
                    SET status = 'superseded', lifecycle_state = 'superseded', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, superseded_id),
                )
                connection.execute(
                    """
                    UPDATE parameters
                    SET status = 'accepted', lifecycle_state = 'active', promoted_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, record_id),
                )
                persist_freshness_invalidation(connection, prepared)
                log_event(
                    connection,
                    event_type="ParameterReplacementAccepted",
                    actor="local-user",
                    target_type="Parameter",
                    target_id=record_id,
                    workspace_id=str(replacement["workspace_id"]),
                    payload={
                        "replacement_parameter_id": record_id,
                        "superseded_parameter_id": superseded_id,
                        "invalidation_id": prepared.id,
                        "affected_count": prepared.affected_count,
                        "graph_digest": prepared.source_graph_digest,
                        "cycle_count": prepared.cycle_count,
                        "unresolved_diagnostic_count": prepared.unresolved_diagnostic_count,
                    },
                )
                accepted = _get_record(connection, "parameter", record_id)
                superseded = _get_record(connection, "parameter", superseded_id)
                if accepted is None or superseded is None:
                    raise ParameterReplacementError(
                        "parameter_replacement_state_inconsistent",
                        "Parameter replacement records are missing after persistence.",
                    )
                summary = invalidation_summary_from_connection(connection, prepared.id)
                connection.commit()
                return _replacement_response(accepted, superseded, summary)
            except Exception:
                connection.rollback()
                raise
    except sqlite3.IntegrityError as exc:
        raise ParameterReplacementError(
            "parameter_already_replaced",
            "A competing Parameter replacement already succeeded.",
        ) from exc


def _replacement_response(
    accepted: MemoryRecordRead,
    superseded: MemoryRecordRead,
    summary: object,
) -> ParameterReplacementRead:
    return ParameterReplacementRead(
        accepted_parameter=accepted,
        superseded_parameter=superseded,
        invalidation=ParameterReplacementInvalidationRead(
            id=summary.id,
            source_ref=summary.source_ref,
            replacement_ref=summary.replacement_ref,
            affected_count=int(summary.affected_count),
            graph_digest=str(summary.graph_digest),
            created_at=summary.created_at,
        ),
    )


def promote_record(kind: str, record_id: str) -> MemoryRecordRead:
    return _transition(kind, record_id, "accepted")


def reject_record(kind: str, record_id: str) -> MemoryRecordRead:
    return _transition(kind, record_id, "rejected")


def list_proposals(workspace_id: str, status: str | None = None) -> list[MemoryRecordRead]:
    if status is not None and status not in _ALLOWED_STATUSES:
        raise ValueError("Unsupported memory status.")
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        union_sql = " UNION ALL ".join(_select_record_sql(kind) for kind in ("assumption", "parameter", "decision"))
        params: list[str] = [workspace_id]
        where = "WHERE workspace_id = ?"
        if status is not None:
            where += " AND status = ?"
            params.append(status)
        rows = connection.execute(
            f"SELECT * FROM ({union_sql}) {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
    return rows_to_models(rows, MemoryRecordRead)
