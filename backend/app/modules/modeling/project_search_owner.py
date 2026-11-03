from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.core.database import open_sqlite_connection
from app.core.repository import rows_to_models
from app.core.search_text import register_search
from app.modules.modeling.models import AssumptionRead, DecisionRead, ParameterRead, RequirementRead

_TABLES = {
    "decision": "decisions",
    "assumption": "assumptions",
    "parameter": "parameters",
    "requirement": "requirements",
}
_STATUS_COLUMNS = {
    "decision": "status",
    "assumption": "status",
    "parameter": "value_status",
    "requirement": "status",
}
_MODELS = {
    "decision": DecisionRead,
    "assumption": AssumptionRead,
    "parameter": ParameterRead,
    "requirement": RequirementRead,
}
_TEXT_COLUMNS = {
    "decision": ("title", "decision_text", "rationale", "notes"),
    "assumption": ("statement", "notes"),
    "parameter": ("name", "symbol", "notes"),
    "requirement": ("statement", "rationale", "notes"),
}


def _escape_like_literal(value: str) -> str:
    return value.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _register_casefold(connection) -> None:
    connection.create_function(
        "JARVIS_CASEFOLD",
        1,
        lambda value: "" if value is None else str(value).casefold(),
        deterministic=True,
    )


def get_context_record_exact(workspace_id: str, kind: str, record_id: str) -> Any | None:
    """Return one exact Project Basis owner projection without search semantics."""
    table = _TABLES.get(kind)
    if table is None:
        return None
    with open_sqlite_connection() as connection:
        row = connection.execute(
            f"SELECT * FROM {table} WHERE id = ? AND workspace_id = ?",
            (record_id, workspace_id),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        if kind == "decision":
            return DecisionRead.model_validate(data)
        if kind == "assumption":
            return AssumptionRead.model_validate(data)
        if kind == "parameter":
            return ParameterRead.model_validate(data)
        if kind == "requirement":
            return RequirementRead.model_validate(data)
    return None


def search_context_records_literal(
    workspace_id: str,
    *,
    kinds: list[str],
    statuses_by_kind: Mapping[str, Sequence[str] | None],
    query: str,
    max_matches_per_kind: int,
) -> dict[str, list[Any]]:
    """Read bounded exact, normalized and token matches from canonical Project Basis owner tables.

    Eligibility and literal match tier are applied before the per-kind bound so
    retired/newer contains rows cannot hide an eligible exact or prefix match.
    """
    results: dict[str, list[Any]] = {}
    with open_sqlite_connection() as connection:
        register_search(connection, query)
        workspace = connection.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if workspace is None:
            raise ValueError("Workspace not found.")

        for kind in kinds:
            table = _TABLES[kind]
            values: list[object] = [workspace_id]
            clauses = ["workspace_id = ?"]
            if kind == "parameter":
                clauses.append("lifecycle_state = 'active'")
            elif kind == "decision":
                clauses.append("basis_lifecycle_state = 'active'")

            statuses = statuses_by_kind[kind]
            if statuses is not None:
                if not statuses:
                    results[kind] = []
                    continue
                placeholders = ", ".join("?" for _ in statuses)
                clauses.append(f"{_STATUS_COLUMNS[kind]} IN ({placeholders})")
                values.extend(statuses)

            # Eligibility and rank precede LIMIT, including token matches.
            text_columns = ("id", *_TEXT_COLUMNS[kind])
            rank = "MIN(" + ", ".join(f"JARVIS_MATCH({column})" for column in text_columns) + ")"
            clauses.append(f"{rank} < 99")

            rows = connection.execute(
                f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} "
                f"ORDER BY {rank}, id ASC LIMIT ?",
                (*values, max_matches_per_kind),
            ).fetchall()
            results[kind] = rows_to_models(rows, _MODELS[kind])
    return results
