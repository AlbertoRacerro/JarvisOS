from __future__ import annotations

import sqlite3
from typing import Any

from app.core.database import open_sqlite_connection
from app.core.repository import row_to_model, rows_to_models
from app.modules.events.service import log_event, utc_now
from app.modules.flowsheet.service import build_flowsheet_graph_from_connection
from app.modules.modeling.models import ParameterLifecycleCommand, ParameterRead, ParameterUpdate


class ParameterLifecycleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_AUTHORITY_FIELDS = {"value", "unit"}
_TARGETS = {
    "activate": {"inactive": "active"},
    "deactivate": {"active": "inactive"},
    "archive": {"active": "archived", "inactive": "archived"},
    "delete": {"active": "deleted", "inactive": "deleted", "archived": "deleted"},
}


def _parameter_row(connection: sqlite3.Connection, workspace_id: str, parameter_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM parameters WHERE id = ? AND workspace_id = ?",
        (parameter_id, workspace_id),
    ).fetchone()
    if row is None:
        raise ParameterLifecycleError("parameter_not_found", "Parameter not found in workspace.")
    return row


def list_lifecycle_parameters(workspace_id: str, *, include_noncurrent: bool = False) -> list[ParameterRead]:
    with open_sqlite_connection() as connection:
        if connection.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone() is None:
            raise ValueError("Workspace not found.")
        clause = "" if include_noncurrent else " AND lifecycle_state = 'active'"
        rows = connection.execute(
            """
            SELECT id, workspace_id, name, symbol, value,
                   COALESCE(unit, 'unspecified') AS unit,
                   COALESCE(value_status, 'candidate') AS value_status,
                   value_min, value_max, source_ref, confidence, status,
                   created_at, updated_at, notes, supersedes_parameter_id, lifecycle_state
            FROM parameters
            WHERE workspace_id = ?""" + clause + " ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
    return rows_to_models(rows, ParameterRead)


def _downstream_refs(connection: sqlite3.Connection, workspace_id: str, parameter_id: str) -> tuple[str, ...]:
    graph = build_flowsheet_graph_from_connection(connection, workspace_id)
    source = f"parameter:{parameter_id}"
    adjacency: dict[str, set[str]] = {}
    for edge in graph.edges:
        if edge.edge_class == "dependency" or edge.relation == "executed_by":
            adjacency.setdefault(edge.upstream_ref, set()).add(edge.downstream_ref)
    seen = {source}
    pending = [source]
    while pending:
        current = pending.pop()
        for downstream in sorted(adjacency.get(current, ())):
            if downstream not in seen:
                seen.add(downstream)
                pending.append(downstream)
    return tuple(sorted(seen - {source}))


def _require_no_downstream_dependents(connection: sqlite3.Connection, workspace_id: str, parameter_id: str) -> None:
    dependents = _downstream_refs(connection, workspace_id, parameter_id)
    if dependents:
        raise ParameterLifecycleError(
            "parameter_lifecycle_dependents_require_reconciliation",
            f"Parameter has {len(dependents)} downstream dependent record(s); use replacement/reconciliation authority.",
        )


def _event_payload(*, operation: str, prior: dict[str, Any], result: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"operation": operation, "prior": prior, "result": result}
    if reason:
        payload["reason"] = reason
    return payload


def update_parameter_in_transaction(
    connection: sqlite3.Connection,
    parameter_id: str,
    payload: ParameterUpdate,
    *,
    allow_dependency_change: bool = False,
) -> ParameterRead:
    updates = payload.model_dump(exclude_unset=True, exclude={"workspace_id", "expected_updated_at"})
    row = _parameter_row(connection, payload.workspace_id, parameter_id)
    if not updates:
        return row_to_model(row, ParameterRead)
    if row["updated_at"] != payload.expected_updated_at:
        raise ParameterLifecycleError("parameter_stale", "Parameter changed since it was reviewed.")
    if row["lifecycle_state"] != "active":
        raise ParameterLifecycleError("parameter_not_active", "Only an active Parameter may be edited in V0.")
    changed_authority = _AUTHORITY_FIELDS.intersection(updates)
    if (
        not allow_dependency_change
        and changed_authority
        and any(row[field] != updates[field] for field in changed_authority)
    ):
        _require_no_downstream_dependents(connection, payload.workspace_id, parameter_id)

    merged_min = updates.get("value_min", row["value_min"])
    merged_max = updates.get("value_max", row["value_max"])
    if merged_min is not None and merged_max is not None and merged_min > merged_max:
        raise ParameterLifecycleError("parameter_invalid_bounds", "value_min must be <= value_max.")

    changed = {field: value for field, value in updates.items() if row[field] != value}
    if not changed:
        return row_to_model(row, ParameterRead)

    now = utc_now()
    assignments = ", ".join(f"{field} = ?" for field in changed)
    cursor = connection.execute(
        f"UPDATE parameters SET {assignments}, updated_at = ? WHERE id = ? AND workspace_id = ? AND updated_at = ?",
        (*changed.values(), now, parameter_id, payload.workspace_id, payload.expected_updated_at),
    )
    if cursor.rowcount != 1:
        raise ParameterLifecycleError("parameter_stale", "Parameter changed before commit.")
    prior = {field: row[field] for field in changed}
    log_event(
        connection,
        event_type="ParameterCanonicalEdited",
        actor="local-user",
        target_type="Parameter",
        target_id=parameter_id,
        workspace_id=payload.workspace_id,
        payload=_event_payload(operation="edit", prior=prior, result=changed),
    )
    return row_to_model(_parameter_row(connection, payload.workspace_id, parameter_id), ParameterRead)


def update_parameter(parameter_id: str, payload: ParameterUpdate) -> ParameterRead:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = update_parameter_in_transaction(connection, parameter_id, payload)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise


def transition_parameter_in_transaction(
    connection: sqlite3.Connection,
    parameter_id: str,
    payload: ParameterLifecycleCommand,
    *,
    allow_dependency_change: bool = False,
) -> ParameterRead:
    row = _parameter_row(connection, payload.workspace_id, parameter_id)
    current = row["lifecycle_state"]
    if row["updated_at"] != payload.expected_updated_at or current != payload.expected_lifecycle_state:
        raise ParameterLifecycleError("parameter_stale", "Parameter lifecycle changed since it was reviewed.")
    target = _TARGETS[payload.action].get(current)
    if target is None:
        raise ParameterLifecycleError(
            "parameter_lifecycle_transition_invalid",
            f"Transition {payload.action} is not valid from {current}.",
        )
    if not allow_dependency_change and current == "active" and target != "active":
        _require_no_downstream_dependents(connection, payload.workspace_id, parameter_id)

    now = utc_now()
    cursor = connection.execute(
        "UPDATE parameters SET lifecycle_state = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND updated_at = ? AND lifecycle_state = ?",
        (target, now, parameter_id, payload.workspace_id, payload.expected_updated_at, current),
    )
    if cursor.rowcount != 1:
        raise ParameterLifecycleError("parameter_stale", "Parameter lifecycle changed before commit.")
    log_event(
        connection,
        event_type="ParameterLifecycleChanged",
        actor="local-user",
        target_type="Parameter",
        target_id=parameter_id,
        workspace_id=payload.workspace_id,
        payload=_event_payload(
            operation=payload.action,
            prior={"lifecycle_state": current},
            result={"lifecycle_state": target},
            reason=payload.reason,
        ),
    )
    return row_to_model(_parameter_row(connection, payload.workspace_id, parameter_id), ParameterRead)


def transition_parameter(parameter_id: str, payload: ParameterLifecycleCommand) -> ParameterRead:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = transition_parameter_in_transaction(connection, parameter_id, payload)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
