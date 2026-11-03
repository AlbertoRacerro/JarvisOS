from __future__ import annotations

import sqlite3
from uuid import uuid4

from pydantic import ValidationError

from app.core.database import open_sqlite_connection
from app.core.repository import row_to_model
from app.modules.events.service import log_event, utc_now
from app.modules.modeling.models import (
    AssumptionProjectUpdate,
    DecisionCreate,
    DecisionProjectUpdate,
    ModelSpecCreate,
    ModelSpecProjectUpdate,
    RequirementCreate,
    RequirementProjectUpdate,
    RequirementRead,
)


class ProjectKnowledgeOwnerError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _row(
    connection: sqlite3.Connection,
    table: str,
    workspace_id: str,
    record_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        f"SELECT * FROM {table} WHERE id = ? AND workspace_id = ?",
        (record_id, workspace_id),
    ).fetchone()
    if row is None:
        raise ProjectKnowledgeOwnerError("owner_not_found", "Canonical owner was not found in workspace.")
    return row


def _cas_update(
    connection: sqlite3.Connection,
    *,
    table: str,
    target_type: str,
    event_type: str,
    record_id: str,
    workspace_id: str,
    expected_updated_at: str,
    updates: dict[str, object],
) -> sqlite3.Row:
    row = _row(connection, table, workspace_id, record_id)
    if str(row["updated_at"]) != expected_updated_at:
        raise ProjectKnowledgeOwnerError("owner_stale", "Canonical owner changed since it was reviewed.")
    changed = {key: value for key, value in updates.items() if key in row.keys() and row[key] != value}
    if not changed:
        return row
    now = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in changed)
    cursor = connection.execute(
        f"UPDATE {table} SET {assignments}, updated_at = ? WHERE id = ? AND workspace_id = ? AND updated_at = ?",
        (*changed.values(), now, record_id, workspace_id, expected_updated_at),
    )
    if cursor.rowcount != 1:
        raise ProjectKnowledgeOwnerError("owner_stale", "Canonical owner changed before commit.")
    log_event(
        connection,
        event_type=event_type,
        actor="local-user",
        target_type=target_type,
        target_id=record_id,
        workspace_id=workspace_id,
        payload={
            "operation": "edit",
            "prior": {key: row[key] for key in changed},
            "result": changed,
        },
    )
    return _row(connection, table, workspace_id, record_id)


def create_requirement_in_transaction(
    connection: sqlite3.Connection,
    workspace_id: str,
    payload: RequirementCreate,
    *,
    record_id: str | None = None,
) -> sqlite3.Row:
    record_id = record_id or str(uuid4())
    now = utc_now()
    connection.execute(
        """
        INSERT INTO requirements (
            id, workspace_id, statement, rationale, status, notes, schema_version,
            created_at, updated_at, basis_kind, reconciliation_gate,
            criterion_output_name, criterion_operator, criterion_expected_value,
            criterion_expected_unit, criterion_rule_version
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            workspace_id,
            payload.statement,
            payload.rationale,
            payload.status,
            payload.notes,
            now,
            now,
            payload.basis_kind,
            payload.reconciliation_gate,
            payload.criterion_output_name,
            payload.criterion_operator,
            payload.criterion_expected_value,
            payload.criterion_expected_unit,
            payload.criterion_rule_version,
        ),
    )
    log_event(
        connection,
        event_type="RequirementCreated",
        actor="local-user",
        target_type="Requirement",
        target_id=record_id,
        workspace_id=workspace_id,
        payload={"statement": payload.statement[:160], "status": payload.status, "basis_kind": payload.basis_kind},
    )
    return _row(connection, "requirements", workspace_id, record_id)


def update_requirement_in_transaction(
    connection: sqlite3.Connection,
    requirement_id: str,
    payload: RequirementProjectUpdate,
) -> sqlite3.Row:
    current = _row(connection, "requirements", payload.workspace_id, requirement_id)
    updates = payload.model_dump(exclude_unset=True, exclude={"workspace_id", "expected_updated_at"})
    candidate = {field: current[field] for field in RequirementCreate.model_fields}
    candidate.update(updates)
    try:
        RequirementCreate.model_validate(candidate)
    except ValidationError as exc:
        raise ProjectKnowledgeOwnerError(
            "requirement_invalid",
            "Requirement update would violate canonical invariants.",
        ) from exc
    return _cas_update(
        connection,
        table="requirements",
        target_type="Requirement",
        event_type="RequirementCanonicalEdited",
        record_id=requirement_id,
        workspace_id=payload.workspace_id,
        expected_updated_at=payload.expected_updated_at,
        updates=updates,
    )


def update_requirement(
    requirement_id: str,
    payload: RequirementProjectUpdate,
) -> RequirementRead:
    with open_sqlite_connection() as connection:
        row = update_requirement_in_transaction(connection, requirement_id, payload)
        connection.commit()
    return row_to_model(row, RequirementRead)


def retire_requirement_in_transaction(
    connection: sqlite3.Connection,
    requirement_id: str,
    *,
    workspace_id: str,
    expected_updated_at: str,
    reason: str | None,
) -> sqlite3.Row:
    row = _row(connection, "requirements", workspace_id, requirement_id)
    if row["status"] != "active":
        raise ProjectKnowledgeOwnerError("requirement_not_active", "Only an active Requirement may be retired.")
    result = _cas_update(
        connection,
        table="requirements",
        target_type="Requirement",
        event_type="RequirementRetired",
        record_id=requirement_id,
        workspace_id=workspace_id,
        expected_updated_at=expected_updated_at,
        updates={"status": "retired"},
    )
    if reason:
        log_event(
            connection,
            event_type="RequirementRetirementReason",
            actor="local-user",
            target_type="Requirement",
            target_id=requirement_id,
            workspace_id=workspace_id,
            payload={"reason": reason[:500]},
        )
    return result


def create_decision_in_transaction(
    connection: sqlite3.Connection,
    workspace_id: str,
    payload: DecisionCreate,
    *,
    record_id: str | None = None,
) -> sqlite3.Row:
    record_id = record_id or str(uuid4())
    now = utc_now()
    connection.execute(
        """
        INSERT INTO decisions (
            id, workspace_id, title, decision_text, rationale, status, linked_run_id,
            created_at, updated_at, notes, basis_lifecycle_state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            workspace_id,
            payload.title,
            payload.decision_text,
            payload.rationale,
            payload.status,
            payload.linked_run_id,
            now,
            now,
            payload.notes,
            payload.basis_lifecycle_state,
        ),
    )
    log_event(
        connection,
        event_type="DecisionCreated",
        actor="local-user",
        target_type="Decision",
        target_id=record_id,
        workspace_id=workspace_id,
        payload={"title": payload.title, "status": payload.status},
    )
    return _row(connection, "decisions", workspace_id, record_id)


def update_decision_in_transaction(
    connection: sqlite3.Connection,
    decision_id: str,
    payload: DecisionProjectUpdate,
) -> sqlite3.Row:
    updates = payload.model_dump(exclude_unset=True, exclude={"workspace_id", "expected_updated_at"})
    return _cas_update(
        connection,
        table="decisions",
        target_type="Decision",
        event_type="DecisionCanonicalEdited",
        record_id=decision_id,
        workspace_id=payload.workspace_id,
        expected_updated_at=payload.expected_updated_at,
        updates=updates,
    )


def retire_decision_in_transaction(
    connection: sqlite3.Connection,
    decision_id: str,
    *,
    workspace_id: str,
    expected_updated_at: str,
) -> sqlite3.Row:
    row = _row(connection, "decisions", workspace_id, decision_id)
    if row["basis_lifecycle_state"] != "active":
        raise ProjectKnowledgeOwnerError("decision_not_active", "Only an active Decision may be retired.")
    return _cas_update(
        connection,
        table="decisions",
        target_type="Decision",
        event_type="DecisionRetired",
        record_id=decision_id,
        workspace_id=workspace_id,
        expected_updated_at=expected_updated_at,
        updates={"basis_lifecycle_state": "retired"},
    )


def update_assumption_in_transaction(
    connection: sqlite3.Connection,
    assumption_id: str,
    payload: AssumptionProjectUpdate,
) -> sqlite3.Row:
    updates = payload.model_dump(exclude_unset=True, exclude={"workspace_id", "expected_updated_at"})
    return _cas_update(
        connection,
        table="assumptions",
        target_type="Assumption",
        event_type="AssumptionCanonicalEdited",
        record_id=assumption_id,
        workspace_id=payload.workspace_id,
        expected_updated_at=payload.expected_updated_at,
        updates=updates,
    )


def create_model_spec_in_transaction(
    connection: sqlite3.Connection,
    workspace_id: str,
    payload: ModelSpecCreate,
    *,
    actor: str = "local-user",
    event_payload: dict[str, object] | None = None,
) -> sqlite3.Row:
    record_id = str(uuid4())
    now = utc_now()
    connection.execute(
        """
        INSERT INTO model_specs (
            id, workspace_id, title, engineering_question, scope, status,
            maturity_status, assumptions_summary, inputs_summary, outputs_summary,
            raw_payload, schema_version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            record_id,
            workspace_id,
            payload.title,
            payload.engineering_question,
            payload.scope,
            payload.status,
            payload.maturity_status,
            payload.assumptions_summary,
            payload.inputs_summary,
            payload.outputs_summary,
            payload.raw_payload,
            now,
            now,
        ),
    )
    log_event(
        connection,
        event_type="ModelSpecCreated",
        actor=actor,
        target_type="ModelSpec",
        target_id=record_id,
        workspace_id=workspace_id,
        payload={"title": payload.title, **(event_payload or {})},
    )
    return _row(connection, "model_specs", workspace_id, record_id)


def update_model_spec_in_transaction(
    connection: sqlite3.Connection,
    model_spec_id: str,
    payload: ModelSpecProjectUpdate,
) -> sqlite3.Row:
    updates = payload.model_dump(exclude_unset=True, exclude={"workspace_id", "expected_updated_at"})
    return _cas_update(
        connection,
        table="model_specs",
        target_type="ModelSpec",
        event_type="ModelSpecCanonicalEdited",
        record_id=model_spec_id,
        workspace_id=payload.workspace_id,
        expected_updated_at=payload.expected_updated_at,
        updates=updates,
    )