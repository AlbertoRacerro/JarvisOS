import sqlite3
from collections.abc import Mapping, Sequence
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.core.repository import optional_row_to_model, row_to_model, rows_to_models
from app.modules.events.service import log_event, utc_now
from app.modules.memory.replacement import validate_parameter_replacement_proposal
from app.modules.modeling.models import (
    AssumptionCreate,
    AssumptionRead,
    DecisionCreate,
    DecisionRead,
    ModelSpecCreate,
    ModelSpecRead,
    ParameterCreate,
    ParameterRead,
    RequirementCreate,
    RequirementRead,
    RequirementUpdate,
    SimulationRunCreate,
    SimulationRunRead,
)
from app.modules.modeling.project_knowledge_owner import (
    create_decision_in_transaction,
    create_requirement_in_transaction,
)


def _workspace_exists(connection: sqlite3.Connection, workspace_id: str) -> bool:
    row = connection.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return row is not None


def _require_workspace(connection: sqlite3.Connection, workspace_id: str) -> None:
    if not _workspace_exists(connection, workspace_id):
        raise ValueError("Workspace not found.")


def _log_creation(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    target_type: str,
    target_id: str,
    workspace_id: str,
    payload: dict[str, object],
) -> None:
    log_event(
        connection,
        event_type=event_type,
        actor="local-user",
        target_type=target_type,
        target_id=target_id,
        workspace_id=workspace_id,
        payload=payload,
    )


CONTEXT_RECORD_KINDS = ("decision", "assumption", "parameter", "requirement")
_CONTEXT_STATUS_COLUMNS = {
    "decision": "status",
    "assumption": "status",
    "parameter": "value_status",
    "requirement": "status",
}
_CONTEXT_MODELS = {
    "decision": DecisionRead,
    "assumption": AssumptionRead,
    "parameter": ParameterRead,
    "requirement": RequirementRead,
}
_CONTEXT_TABLES = {
    "decision": "decisions",
    "assumption": "assumptions",
    "parameter": "parameters",
    "requirement": "requirements",
}
_CONTEXT_TEXT_COLUMNS = {
    "decision": ("title", "decision_text", "rationale", "notes"),
    "assumption": ("statement", "notes"),
    "parameter": ("name", "symbol", "notes"),
    "requirement": ("statement", "rationale", "notes"),
}


def sqlite_fts5_available(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.jarvisos_fts5_probe USING fts5(x)")
        connection.execute("DROP TABLE IF EXISTS temp.jarvisos_fts5_probe")
    except sqlite3.OperationalError:
        return False
    return True


def _query_requires_literal_like(query: str) -> bool:
    return any(character in query for character in ("+", ".", "-", '"', "'", "%", "_", "\\"))


def _escape_like_literal(query: str) -> str:
    return query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_clause(kind: str, query: str, values: list[object]) -> str:
    pattern = f"%{_escape_like_literal(query)}%"
    terms = []
    for column in _CONTEXT_TEXT_COLUMNS[kind]:
        terms.append(f"LOWER(COALESCE({column}, '')) LIKE ? ESCAPE '\\'")
        values.append(pattern)
    return "(" + " OR ".join(terms) + ")"


def _fts_ids(connection: sqlite3.Connection, workspace_id: str, kind: str, query: str) -> set[str]:
    if _query_requires_literal_like(query):
        return set()
    escaped = query.replace('"', '""')
    rows = connection.execute(
        """
        SELECT record_id FROM context_records_fts
        WHERE context_records_fts MATCH ? AND workspace_id = ? AND kind = ?
        """,
        (f'"{escaped}"', workspace_id, kind),
    ).fetchall()
    return {row["record_id"] for row in rows}


def select_context_records(
    workspace_id: str,
    *,
    kinds: list[str],
    statuses_by_kind: Mapping[str, Sequence[str] | None],
    ids: list[str] | None,
    query: str | None,
    max_items_per_kind: int,
    connection: sqlite3.Connection | None = None,
) -> dict[str, list[object]]:
    if connection is None:
        with open_sqlite_connection() as owned_connection:
            return select_context_records(
                workspace_id,
                kinds=kinds,
                statuses_by_kind=statuses_by_kind,
                ids=ids,
                query=query,
                max_items_per_kind=max_items_per_kind,
                connection=owned_connection,
            )

    selected_ids = set(ids or [])
    normalized_query = query.strip() if query else None
    results: dict[str, list[object]] = {}
    _require_workspace(connection, workspace_id)
    fts_available = (
        bool(normalized_query)
        and sqlite_fts5_available(connection)
        and not _query_requires_literal_like(normalized_query)
    )
    for kind in kinds:
        table = _CONTEXT_TABLES[kind]
        status_column = _CONTEXT_STATUS_COLUMNS[kind]
        values: list[object] = [workspace_id]
        clauses = ["workspace_id = ?"]
        if kind == "parameter":
            # Lifecycle-current filtering belongs in the shared selector so it
            # applies before ordering/LIMIT and explicit IDs cannot bypass it.
            clauses.append("lifecycle_state = 'active'")
        if selected_ids:
            placeholders = ", ".join("?" for _ in selected_ids)
            clauses.append(f"id IN ({placeholders})")
            values.extend(sorted(selected_ids))
        else:
            statuses = statuses_by_kind[kind]
            if statuses is not None:
                if not statuses:
                    results[kind] = []
                    continue
                placeholders = ", ".join("?" for _ in statuses)
                clauses.append(f"{status_column} IN ({placeholders})")
                values.extend(statuses)
        if normalized_query:
            if fts_available:
                matched_ids = _fts_ids(connection, workspace_id, kind, normalized_query)
                if not matched_ids:
                    results[kind] = []
                    continue
                placeholders = ", ".join("?" for _ in matched_ids)
                clauses.append(f"id IN ({placeholders})")
                values.extend(sorted(matched_ids))
            else:
                clauses.append(_like_clause(kind, normalized_query, values))
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id ASC LIMIT ?",
            (*values, max_items_per_kind),
        ).fetchall()
        results[kind] = rows_to_models(rows, _CONTEXT_MODELS[kind])
    return results


def create_model_spec(workspace_id: str, payload: ModelSpecCreate) -> ModelSpecRead:
    now = utc_now()
    record_id = str(uuid4())
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        connection.execute(
            """
            INSERT INTO model_specs (
                id, workspace_id, title, engineering_question, scope, status,
                maturity_status, assumptions_summary, inputs_summary, outputs_summary,
                raw_payload, schema_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                1,
                now,
                now,
            ),
        )
        _log_creation(
            connection,
            event_type="ModelSpecCreated",
            target_type="ModelSpec",
            target_id=record_id,
            workspace_id=workspace_id,
            payload={"title": payload.title},
        )
        connection.commit()
        row = connection.execute("SELECT * FROM model_specs WHERE id = ?", (record_id,)).fetchone()
    return row_to_model(row, ModelSpecRead)


def list_model_specs(workspace_id: str) -> list[ModelSpecRead]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        rows = connection.execute(
            "SELECT * FROM model_specs WHERE workspace_id = ? ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
    return rows_to_models(rows, ModelSpecRead)


def get_model_spec(model_spec_id: str) -> ModelSpecRead | None:
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT * FROM model_specs WHERE id = ?", (model_spec_id,)).fetchone()
    return optional_row_to_model(row, ModelSpecRead)


def create_assumption(workspace_id: str, payload: AssumptionCreate) -> AssumptionRead:
    now = utc_now()
    record_id = str(uuid4())
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        connection.execute(
            """
            INSERT INTO assumptions (
                id, workspace_id, statement, scope, confidence, status,
                source_ref, created_at, updated_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                workspace_id,
                payload.statement,
                payload.scope,
                payload.confidence,
                payload.status,
                payload.source_ref,
                now,
                now,
                payload.notes,
            ),
        )
        _log_creation(
            connection,
            event_type="AssumptionCreated",
            target_type="Assumption",
            target_id=record_id,
            workspace_id=workspace_id,
            payload={"statement": payload.statement[:160], "status": payload.status},
        )
        connection.commit()
        row = connection.execute("SELECT * FROM assumptions WHERE id = ?", (record_id,)).fetchone()
    return row_to_model(row, AssumptionRead)


def list_assumptions(workspace_id: str) -> list[AssumptionRead]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        rows = connection.execute(
            """
            SELECT
                id, workspace_id, statement, scope,
                CASE
                    WHEN confidence IN ('low', 'medium', 'high') THEN confidence
                    ELSE NULL
                END AS confidence,
                CASE
                    WHEN status IN ('proposed', 'accepted', 'rejected', 'superseded') THEN status
                    ELSE 'proposed'
                END AS status,
                source_ref, created_at, updated_at, notes
            FROM assumptions
            WHERE workspace_id = ?
            ORDER BY created_at DESC
            """,
            (workspace_id,),
        ).fetchall()
    return rows_to_models(rows, AssumptionRead)


def create_parameter(workspace_id: str, payload: ParameterCreate) -> ParameterRead:
    now = utc_now()
    record_id = str(uuid4())
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        validate_parameter_replacement_proposal(
            connection,
            workspace_id=workspace_id,
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
                supersedes_parameter_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                workspace_id,
                payload.name,
                payload.symbol,
                payload.value,
                payload.unit,
                payload.value_status,
                payload.value_min,
                payload.value_max,
                payload.source_ref,
                payload.confidence,
                payload.status,
                now,
                now,
                payload.notes,
                payload.supersedes_parameter_id,
            ),
        )
        _log_creation(
            connection,
            event_type="ParameterCreated",
            target_type="Parameter",
            target_id=record_id,
            workspace_id=workspace_id,
            payload={"name": payload.name, "symbol": payload.symbol, "unit": payload.unit},
        )
        connection.commit()
        row = connection.execute("SELECT * FROM parameters WHERE id = ?", (record_id,)).fetchone()
    return row_to_model(row, ParameterRead)


def list_parameters(workspace_id: str) -> list[ParameterRead]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        rows = connection.execute(
            """
            SELECT
                id, workspace_id, name, symbol, value,
                COALESCE(unit, 'unspecified') AS unit,
                COALESCE(value_status, 'candidate') AS value_status,
                value_min, value_max, source_ref, confidence, status,
                created_at, updated_at, notes, supersedes_parameter_id, lifecycle_state
            FROM parameters
            WHERE workspace_id = ?
            ORDER BY created_at DESC
            """,
            (workspace_id,),
        ).fetchall()
    return rows_to_models(rows, ParameterRead)


def create_requirement(workspace_id: str, payload: RequirementCreate) -> RequirementRead:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        row = create_requirement_in_transaction(connection, workspace_id, payload)
        connection.commit()
    return row_to_model(row, RequirementRead)


def get_requirement(requirement_id: str) -> RequirementRead | None:
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT * FROM requirements WHERE id = ?", (requirement_id,)).fetchone()
    return optional_row_to_model(row, RequirementRead)


def list_requirements(workspace_id: str) -> list[RequirementRead]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        rows = connection.execute(
            "SELECT * FROM requirements WHERE workspace_id = ? ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
    return rows_to_models(rows, RequirementRead)


def update_requirement(requirement_id: str, payload: RequirementUpdate) -> RequirementRead | None:
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return get_requirement(requirement_id)

    now = utc_now()
    assignments = [f"{field} = ?" for field in updates]
    values = list(updates.values())
    values.extend([now, requirement_id])
    with open_sqlite_connection() as connection:
        connection.execute(
            f"UPDATE requirements SET {', '.join(assignments)}, updated_at = ? WHERE id = ?",
            values,
        )
        connection.commit()
        row = connection.execute("SELECT * FROM requirements WHERE id = ?", (requirement_id,)).fetchone()
    return optional_row_to_model(row, RequirementRead)


def create_simulation_run(workspace_id: str, payload: SimulationRunCreate) -> SimulationRunRead:
    record_id = str(uuid4())
    created_at = utc_now()
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        if payload.project_knowledge_revision_id is not None:
            revision = connection.execute(
                """
                SELECT id FROM project_knowledge_revisions
                WHERE id = ? AND workspace_id = ? AND state = 'working'
                """,
                (payload.project_knowledge_revision_id, workspace_id),
            ).fetchone()
            if revision is None:
                raise ValueError("Project Knowledge working revision not found in workspace.")
        connection.execute(
            """
            INSERT INTO simulation_runs (
                id, workspace_id, model_version_id, run_label, status,
                input_payload, parameter_payload, output_payload, started_at,
                completed_at, created_at, notes, project_knowledge_revision_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                workspace_id,
                payload.model_version_id,
                payload.run_label,
                payload.status,
                payload.input_payload,
                payload.parameter_payload,
                payload.output_payload,
                payload.started_at,
                payload.completed_at,
                created_at,
                payload.notes,
                payload.project_knowledge_revision_id,
            ),
        )
        _log_creation(
            connection,
            event_type="SimulationRunCreated",
            target_type="SimulationRun",
            target_id=record_id,
            workspace_id=workspace_id,
            payload={
                "run_label": payload.run_label,
                "status": payload.status,
                "project_knowledge_revision_id": payload.project_knowledge_revision_id,
            },
        )
        connection.commit()
        row = connection.execute("SELECT * FROM simulation_runs WHERE id = ?", (record_id,)).fetchone()
    return row_to_model(row, SimulationRunRead)


def list_simulation_runs(workspace_id: str) -> list[SimulationRunRead]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        rows = connection.execute(
            "SELECT * FROM simulation_runs WHERE workspace_id = ? ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
    return rows_to_models(rows, SimulationRunRead)


def create_decision(workspace_id: str, payload: DecisionCreate) -> DecisionRead:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        row = create_decision_in_transaction(connection, workspace_id, payload)
        connection.commit()
    return row_to_model(row, DecisionRead)


def list_decisions(workspace_id: str) -> list[DecisionRead]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        rows = connection.execute(
            "SELECT * FROM decisions WHERE workspace_id = ? ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
    return rows_to_models(rows, DecisionRead)
