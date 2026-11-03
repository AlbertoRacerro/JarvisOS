from __future__ import annotations

import hashlib
import heapq
import json
import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.flowsheet.models import (
    FlowsheetFreshnessInvalidationDetailRead,
    FlowsheetFreshnessInvalidationSummaryRead,
    FlowsheetFreshnessMarkRead,
    FlowsheetGraphRead,
    FlowsheetNodeFreshnessRead,
)
from app.modules.flowsheet.service import (
    build_flowsheet_graph_from_connection,
    resolve_flowsheet_node_from_connection,
)

MAX_FRESHNESS_PATH_NODES = 100
MAX_FRESHNESS_MARKS_PER_INVALIDATION = 1000
_REASON_CODE = "upstream_parameter_superseded"
_BATCH_REF_LIMIT = 400


class FreshnessError(ValueError):
    def __init__(self, code: str, message: str, *, bound: str | None = None, observed_count: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.bound = bound
        self.observed_count = observed_count


@dataclass(frozen=True)
class PreparedFreshnessMark:
    id: str
    record_ref: str
    record_kind: str
    record_id: str
    path_json: str
    path_digest: str


@dataclass(frozen=True)
class PreparedFreshnessInvalidation:
    id: str
    workspace_id: str
    superseded_parameter_id: str
    replacement_parameter_id: str
    source_graph_digest: str
    unresolved_diagnostic_count: int
    cycle_count: int
    created_at: str
    marks: tuple[PreparedFreshnessMark, ...]

    @property
    def affected_count(self) -> int:
        return len(self.marks)


def prepare_freshness_invalidation(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    superseded_parameter_id: str,
    replacement_parameter_id: str,
    created_at: str,
) -> PreparedFreshnessInvalidation:
    graph = build_flowsheet_graph_from_connection(connection, workspace_id)
    source_ref = f"parameter:{superseded_parameter_id}"
    replacement_ref = f"parameter:{replacement_parameter_id}"
    if source_ref not in {node.ref for node in graph.nodes}:
        raise FreshnessError(
            "parameter_replacement_not_found", "Superseded Parameter is absent from the workspace graph."
        )

    adjacency: dict[str, list[tuple[str, str, str]]] = {}
    for edge in graph.edges:
        if edge.edge_class == "dependency" or edge.relation == "executed_by":
            adjacency.setdefault(edge.upstream_ref, []).append((edge.downstream_ref, edge.relation, edge.edge_class))
    for items in adjacency.values():
        items.sort()

    best_paths: dict[str, tuple[str, ...]] = {}
    pending: list[tuple[int, tuple[str, ...], str]] = [(1, (source_ref,), source_ref)]
    while pending:
        _length, path, current_ref = heapq.heappop(pending)
        if current_ref in best_paths:
            continue
        if len(path) > MAX_FRESHNESS_PATH_NODES:
            raise FreshnessError(
                "freshness_path_limit_exceeded",
                "A freshness path exceeds the V0 limit.",
                bound="path_nodes",
                observed_count=len(path),
            )
        best_paths[current_ref] = path
        for downstream_ref, _relation, _edge_class in adjacency.get(current_ref, []):
            if downstream_ref not in best_paths:
                next_path = (*path, downstream_ref)
                heapq.heappush(pending, (len(next_path), next_path, downstream_ref))

    affected_refs = sorted(set(best_paths) - {source_ref, replacement_ref})
    if len(affected_refs) > MAX_FRESHNESS_MARKS_PER_INVALIDATION:
        raise FreshnessError(
            "freshness_mark_limit_exceeded",
            "The freshness closure exceeds the V0 limit.",
            bound="marks",
            observed_count=len(affected_refs),
        )

    closure = set(best_paths)
    for diagnostic in graph.diagnostics.unresolved_references:
        if diagnostic.owner_ref in closure or diagnostic.raw_ref == source_ref:
            raise FreshnessError(
                "freshness_lineage_incomplete",
                "Supported downstream lineage is incomplete.",
            )

    marks: list[PreparedFreshnessMark] = []
    for record_ref in affected_refs:
        record_kind, record_id = record_ref.split(":", 1)
        path_json = _canonical_json(list(best_paths[record_ref]))
        marks.append(
            PreparedFreshnessMark(
                id=str(uuid4()),
                record_ref=record_ref,
                record_kind=record_kind,
                record_id=record_id,
                path_json=path_json,
                path_digest=_sha256_prefixed(path_json),
            )
        )

    return PreparedFreshnessInvalidation(
        id=str(uuid4()),
        workspace_id=workspace_id,
        superseded_parameter_id=superseded_parameter_id,
        replacement_parameter_id=replacement_parameter_id,
        source_graph_digest=_graph_digest(graph),
        unresolved_diagnostic_count=len(graph.diagnostics.unresolved_references),
        cycle_count=sum(any(record_ref in closure for record_ref in cycle) for cycle in graph.diagnostics.cycles),
        created_at=created_at,
        marks=tuple(marks),
    )


def persist_freshness_invalidation(connection: sqlite3.Connection, prepared: PreparedFreshnessInvalidation) -> None:
    connection.execute(
        """
        INSERT INTO freshness_invalidations (
            id, workspace_id, superseded_parameter_id, replacement_parameter_id,
            source_graph_digest, affected_count, unresolved_diagnostic_count,
            cycle_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            prepared.id,
            prepared.workspace_id,
            prepared.superseded_parameter_id,
            prepared.replacement_parameter_id,
            prepared.source_graph_digest,
            prepared.affected_count,
            prepared.unresolved_diagnostic_count,
            prepared.cycle_count,
            prepared.created_at,
        ),
    )
    connection.executemany(
        """
        INSERT INTO freshness_marks (
            id, workspace_id, invalidation_id, record_ref, record_kind, record_id,
            reason_code, path_json, path_digest, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                mark.id,
                prepared.workspace_id,
                prepared.id,
                mark.record_ref,
                mark.record_kind,
                mark.record_id,
                _REASON_CODE,
                mark.path_json,
                mark.path_digest,
                prepared.created_at,
            )
            for mark in prepared.marks
        ],
    )


def get_node_freshness(workspace_id: str, node_ref: str) -> FlowsheetNodeFreshnessRead:
    with open_sqlite_connection() as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        try:
            return get_node_freshness_from_connection(connection, workspace_id, node_ref)
        finally:
            connection.rollback()


def get_node_freshness_from_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
    node_ref: str,
) -> FlowsheetNodeFreshnessRead:
    node = resolve_flowsheet_node_from_connection(connection, workspace_id, node_ref)
    count = int(
        connection.execute(
            "SELECT COUNT(*) AS count FROM freshness_marks WHERE workspace_id = ? AND record_ref = ?",
            (workspace_id, node.ref),
        ).fetchone()["count"]
    )
    latest = connection.execute(
        """
        SELECT fi.id AS invalidation_id, fi.superseded_parameter_id,
               fi.replacement_parameter_id, fi.created_at, fm.reason_code,
               fm.path_json, fm.path_digest
        FROM freshness_marks fm
        JOIN freshness_invalidations fi ON fi.id = fm.invalidation_id
        WHERE fm.workspace_id = ? AND fm.record_ref = ?
        ORDER BY fi.created_at DESC, fi.id DESC
        LIMIT 1
        """,
        (workspace_id, node.ref),
    ).fetchone()
    return FlowsheetNodeFreshnessRead(
        record_ref=node.ref,
        state="fresh" if count == 0 else "stale",
        invalidation_count=count,
        latest_invalidation=None if latest is None else _latest_summary(latest),
    )


def get_resolved_node_stale_states_from_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
    node_refs: list[str] | set[str],
) -> dict[str, bool]:
    """Return 051 stale state for canonical refs already resolved in the caller's graph snapshot.

    This helper deliberately does not resolve references again. Callers must derive the refs
    from the canonical flowsheet graph first. Missing freshness marks mean fresh under 051.
    Queries are batched under a fixed bound instead of growing one query per reference.
    """
    ordered_refs = sorted(set(node_refs))
    states = {ref: False for ref in ordered_refs}
    for start in range(0, len(ordered_refs), _BATCH_REF_LIMIT):
        batch = ordered_refs[start : start + _BATCH_REF_LIMIT]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""
            SELECT record_ref, COUNT(*) AS count
            FROM freshness_marks
            WHERE workspace_id = ? AND record_ref IN ({placeholders})
            GROUP BY record_ref
            """,
            (workspace_id, *batch),
        ).fetchall()
        for row in rows:
            ref = str(row["record_ref"])
            if ref in states:
                states[ref] = int(row["count"]) > 0
    return states


def get_freshness_invalidation(workspace_id: str, invalidation_id: str) -> FlowsheetFreshnessInvalidationDetailRead:
    with open_sqlite_connection() as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        try:
            if connection.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone() is None:
                raise FreshnessError("flowsheet_workspace_not_found", "Workspace not found.")
            batch = connection.execute(
                "SELECT * FROM freshness_invalidations WHERE workspace_id = ? AND id = ?",
                (workspace_id, invalidation_id),
            ).fetchone()
            if batch is None:
                raise FreshnessError("freshness_invalidation_not_found", "Freshness invalidation was not found.")
            rows = connection.execute(
                """
                SELECT record_ref, record_kind, record_id, reason_code,
                       path_json, path_digest, created_at
                FROM freshness_marks
                WHERE workspace_id = ? AND invalidation_id = ?
                ORDER BY record_ref ASC
                """,
                (workspace_id, invalidation_id),
            ).fetchall()
            marks = [
                FlowsheetFreshnessMarkRead(
                    record_ref=str(row["record_ref"]),
                    record_kind=str(row["record_kind"]),
                    record_id=str(row["record_id"]),
                    reason_code=str(row["reason_code"]),
                    path=_parse_path(str(row["path_json"])),
                    path_digest=str(row["path_digest"]),
                    created_at=str(row["created_at"]),
                )
                for row in rows
            ]
            if len(marks) != int(batch["affected_count"]):
                raise FreshnessError("freshness_persistence_inconsistent", "Freshness mark count is inconsistent.")
            return FlowsheetFreshnessInvalidationDetailRead(
                id=str(batch["id"]),
                workspace_id=str(batch["workspace_id"]),
                source_ref=f"parameter:{batch['superseded_parameter_id']}",
                replacement_ref=f"parameter:{batch['replacement_parameter_id']}",
                source_graph_digest=str(batch["source_graph_digest"]),
                affected_count=int(batch["affected_count"]),
                unresolved_diagnostic_count=int(batch["unresolved_diagnostic_count"]),
                cycle_count=int(batch["cycle_count"]),
                created_at=str(batch["created_at"]),
                marks=marks,
            )
        finally:
            connection.rollback()


def invalidation_summary_from_connection(
    connection: sqlite3.Connection,
    invalidation_id: str,
) -> FlowsheetFreshnessInvalidationSummaryRead:
    row = connection.execute(
        """
        SELECT id, superseded_parameter_id, replacement_parameter_id,
               affected_count, source_graph_digest, created_at
        FROM freshness_invalidations WHERE id = ?
        """,
        (invalidation_id,),
    ).fetchone()
    if row is None:
        raise FreshnessError("freshness_persistence_inconsistent", "Freshness invalidation is missing.")
    return FlowsheetFreshnessInvalidationSummaryRead(
        id=str(row["id"]),
        source_ref=f"parameter:{row['superseded_parameter_id']}",
        replacement_ref=f"parameter:{row['replacement_parameter_id']}",
        affected_count=int(row["affected_count"]),
        graph_digest=str(row["source_graph_digest"]),
        created_at=str(row["created_at"]),
    )


def _latest_summary(row: sqlite3.Row) -> FlowsheetFreshnessInvalidationSummaryRead:
    return FlowsheetFreshnessInvalidationSummaryRead(
        id=str(row["invalidation_id"]),
        source_ref=f"parameter:{row['superseded_parameter_id']}",
        replacement_ref=f"parameter:{row['replacement_parameter_id']}",
        reason_code=str(row["reason_code"]),
        path=_parse_path(str(row["path_json"])),
        path_digest=str(row["path_digest"]),
        created_at=str(row["created_at"]),
    )


def _graph_digest(graph: FlowsheetGraphRead) -> str:
    payload = {
        "nodes": sorted(node.ref for node in graph.nodes),
        "edges": [
            {
                "upstream_ref": edge.upstream_ref,
                "downstream_ref": edge.downstream_ref,
                "relation": edge.relation,
                "edge_class": edge.edge_class,
                "authorities": sorted(edge.authorities),
                "source_fields": sorted(edge.source_fields),
            }
            for edge in graph.edges
        ],
        "unresolved_references": [
            {
                "owner_ref": item.owner_ref,
                "source_field": item.source_field,
                "code": item.code,
                "raw_ref": item.raw_ref,
            }
            for item in graph.diagnostics.unresolved_references
        ],
    }
    return _sha256_prefixed(_canonical_json(payload))


def _parse_path(payload: str) -> list[str]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise FreshnessError("freshness_persistence_inconsistent", "Stored freshness path is invalid.") from exc
    if not isinstance(parsed, list) or not parsed or not all(isinstance(item, str) for item in parsed):
        raise FreshnessError("freshness_persistence_inconsistent", "Stored freshness path is invalid.")
    return parsed


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_prefixed(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
