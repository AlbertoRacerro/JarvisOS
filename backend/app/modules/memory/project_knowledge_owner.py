from __future__ import annotations

import sqlite3

from app.modules.events.service import log_event, utc_now
from app.modules.flowsheet.freshness import persist_freshness_invalidation, prepare_freshness_invalidation
from app.modules.memory.replacement import ParameterReplacementError, validate_parameter_replacement_proposal
from app.modules.memory.service import _get_record, _log_memory_event, _normalize_status


def transition_proposal_in_transaction(
    connection: sqlite3.Connection,
    *,
    kind: str,
    record_id: str,
    workspace_id: str,
    target_status: str = "accepted",
) -> None:
    record = _get_record(connection, kind, record_id)
    if record is None or record.workspace_id != workspace_id:
        raise ValueError("Memory proposal not found in workspace.")
    current_status = _normalize_status(record.status)
    if current_status != "proposed":
        raise ValueError(f"Cannot transition {current_status} record to {target_status}.")
    if kind == "parameter" and target_status == "accepted" and record.supersedes_parameter_id:
        raise ParameterReplacementError(
            "parameter_replacement_promotion_required",
            "Configured Parameter replacements require replacement promotion.",
        )
    now = utc_now()
    promoted_at = now if target_status == "accepted" else record.promoted_at
    connection.execute(
        {"assumption": "UPDATE assumptions SET status = ?, promoted_at = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
         "parameter": "UPDATE parameters SET status = ?, promoted_at = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
         "decision": "UPDATE decisions SET status = ?, promoted_at = ?, updated_at = ? WHERE id = ? AND workspace_id = ?"}[kind],
        (target_status, promoted_at, now, record_id, workspace_id),
    )
    updated = _get_record(connection, kind, record_id)
    if updated is None:
        raise ValueError("Memory proposal disappeared during transition.")
    _log_memory_event(connection, f"MemoryProposal{target_status.title()}", updated)


def promote_parameter_replacement_in_transaction(
    connection: sqlite3.Connection,
    *,
    record_id: str,
    workspace_id: str,
) -> None:
    existing = connection.execute(
        "SELECT id FROM freshness_invalidations WHERE replacement_parameter_id = ?",
        (record_id,),
    ).fetchone()
    if existing is not None:
        replacement = connection.execute(
            "SELECT status, lifecycle_state, workspace_id FROM parameters WHERE id = ?",
            (record_id,),
        ).fetchone()
        if (
            replacement is None
            or str(replacement["workspace_id"]) != workspace_id
            or replacement["status"] != "accepted"
            or replacement["lifecycle_state"] != "active"
        ):
            raise ParameterReplacementError(
                "parameter_replacement_state_inconsistent",
                "Stored Parameter replacement state is inconsistent.",
            )
        return

    replacement = connection.execute(
        """
        SELECT id, workspace_id, status, origin, value, unit, supersedes_parameter_id
        FROM parameters WHERE id = ? AND workspace_id = ?
        """,
        (record_id, workspace_id),
    ).fetchone()
    if replacement is None:
        raise ParameterReplacementError("parameter_replacement_not_found", "Parameter replacement proposal was not found.")
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
        raise ParameterReplacementError("parameter_replacement_not_configured", "Parameter replacement is not configured.")
    superseded_id = str(superseded_id)
    if connection.execute(
        "SELECT id FROM freshness_invalidations WHERE superseded_parameter_id = ?",
        (superseded_id,),
    ).fetchone() is not None:
        raise ParameterReplacementError("parameter_already_replaced", "The accepted Parameter already has an accepted replacement.")

    validate_parameter_replacement_proposal(
        connection,
        workspace_id=workspace_id,
        supersedes_parameter_id=superseded_id,
        replacement_parameter_id=record_id,
        unit=str(replacement["unit"]),
        value=None if replacement["value"] is None else str(replacement["value"]),
    )
    now = utc_now()
    prepared = prepare_freshness_invalidation(
        connection,
        workspace_id=workspace_id,
        superseded_parameter_id=superseded_id,
        replacement_parameter_id=record_id,
        created_at=now,
    )
    connection.execute(
        "UPDATE parameters SET status = 'superseded', lifecycle_state = 'superseded', updated_at = ? WHERE id = ? AND workspace_id = ?",
        (now, superseded_id, workspace_id),
    )
    connection.execute(
        "UPDATE parameters SET status = 'accepted', lifecycle_state = 'active', promoted_at = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
        (now, now, record_id, workspace_id),
    )
    persist_freshness_invalidation(connection, prepared)
    log_event(
        connection,
        event_type="ParameterReplacementAccepted",
        actor="local-user",
        target_type="Parameter",
        target_id=record_id,
        workspace_id=workspace_id,
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
