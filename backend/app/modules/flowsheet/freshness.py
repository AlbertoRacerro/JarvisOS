from __future__ import annotations

import hashlib
import heapq
import json
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.flowsheet.models import (
    FlowsheetAffectedNodeRead,
    FlowsheetChangeImpactRead,
    FlowsheetFreshnessInvalidationDetailRead,
    FlowsheetFreshnessInvalidationSummaryRead,
    FlowsheetFreshnessMarkRead,
    FlowsheetGraphRead,
    FlowsheetNodeFreshnessRead,
    FlowsheetUpstreamLeverRead,
    FlowsheetUpstreamLeversRead,
    NodeKind,
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


def _parse_ts(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        try:
            return float(val)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return None
    return None


def _calculate_estimated_rerun_seconds(
    connection: sqlite3.Connection,
    workspace_id: str,
    required_reruns_count: int,
) -> float | None:
    if required_reruns_count <= 0:
        return None

    durations: list[float] = []

    # Defensively inspect runner_jobs columns first so we never hit OperationalError
    rj_info = connection.execute("PRAGMA table_info(runner_jobs)").fetchall()
    rj_cols = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in rj_info}

    if "workspace_id" in rj_cols:
        start_col = "started_at" if "started_at" in rj_cols else ("created_at" if "created_at" in rj_cols else None)
        end_col = (
            "completed_at"
            if "completed_at" in rj_cols
            else ("finished_at" if "finished_at" in rj_cols else ("updated_at" if "updated_at" in rj_cols else None))
        )
        if start_col and end_col:
            query = f"SELECT {start_col}, {end_col} FROM runner_jobs WHERE workspace_id = ? AND {start_col} IS NOT NULL AND {end_col} IS NOT NULL"
            rows = connection.execute(query, (workspace_id,)).fetchall()
            for row in rows:
                t0 = _parse_ts(row[0])
                t1 = _parse_ts(row[1])
                if t0 is not None and t1 is not None and t1 > t0:
                    durations.append(t1 - t0)

    # If no positive durations found on runner_jobs, check simulation_runs
    if not durations:
        sr_info = connection.execute("PRAGMA table_info(simulation_runs)").fetchall()
        sr_cols = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in sr_info}
        if "workspace_id" in sr_cols:
            start_col = "started_at" if "started_at" in sr_cols else ("created_at" if "created_at" in sr_cols else None)
            end_col = "completed_at" if "completed_at" in sr_cols else None
            if start_col and end_col:
                query = f"SELECT {start_col}, {end_col} FROM simulation_runs WHERE workspace_id = ? AND {start_col} IS NOT NULL AND {end_col} IS NOT NULL"
                rows = connection.execute(query, (workspace_id,)).fetchall()
                for row in rows:
                    t0 = _parse_ts(row[0])
                    t1 = _parse_ts(row[1])
                    if t0 is not None and t1 is not None and t1 > t0:
                        durations.append(t1 - t0)

    if not durations:
        return None

    median_s = float(statistics.median(durations))
    return round(required_reruns_count * median_s, 2)


def preview_change_impact_from_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
    node_ref: str,
) -> FlowsheetChangeImpactRead:
    source_node = resolve_flowsheet_node_from_connection(connection, workspace_id, node_ref)
    graph = build_flowsheet_graph_from_connection(connection, workspace_id)
    nodes_by_ref = {node.ref: node for node in graph.nodes}
    if source_node.ref not in nodes_by_ref:
        nodes_by_ref[source_node.ref] = source_node

    adjacency: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge.edge_class == "dependency" or edge.relation == "executed_by":
            adjacency.setdefault(edge.upstream_ref, []).append(edge.downstream_ref)
    for items in adjacency.values():
        items.sort()

    best_paths: dict[str, tuple[str, ...]] = {}
    pending: list[tuple[int, tuple[str, ...], str]] = [(1, (source_node.ref,), source_node.ref)]
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
        for downstream_ref in adjacency.get(current_ref, []):
            if downstream_ref not in best_paths:
                next_path = (*path, downstream_ref)
                heapq.heappush(pending, (len(next_path), next_path, downstream_ref))

    affected_refs = sorted(set(best_paths) - {source_node.ref})
    if len(affected_refs) > MAX_FRESHNESS_MARKS_PER_INVALIDATION:
        raise FreshnessError(
            "freshness_mark_limit_exceeded",
            "The freshness closure exceeds the V0 limit.",
            bound="marks",
            observed_count=len(affected_refs),
        )

    rerun_kinds = {"simulation_run", "model_version", "runner_job", "bluecad_attempt"}
    affected_nodes: list[FlowsheetAffectedNodeRead] = []
    counts_by_kind: dict[str, int] = {}
    required_reruns: list[str] = []
    affected_decisions: list[str] = []

    for ref in affected_refs:
        node = nodes_by_ref.get(ref)
        if node is None:
            raw_kind, record_id = ref.split(":", 1)
            kind: NodeKind = cast(NodeKind, raw_kind)
            label = ref
        else:
            kind = node.kind
            record_id = node.id
            label = node.label

        node_path = list(best_paths[ref])
        distance = len(node_path) - 1
        requires_rerun = kind in rerun_kinds
        if requires_rerun:
            required_reruns.append(ref)
        if kind == "decision":
            affected_decisions.append(ref)

        counts_by_kind[kind] = counts_by_kind.get(kind, 0) + 1
        affected_nodes.append(
            FlowsheetAffectedNodeRead(
                ref=ref,
                kind=kind,
                id=record_id,
                label=label,
                distance=distance,
                path=node_path,
                requires_rerun=requires_rerun,
            )
        )

    affected_nodes.sort(key=lambda item: (item.distance, item.kind, item.ref))
    required_reruns.sort()
    affected_decisions.sort()
    counts_by_kind = dict(sorted(counts_by_kind.items()))

    closure = set(best_paths)
    cycle_count = sum(any(record_ref in closure for record_ref in cycle) for cycle in graph.diagnostics.cycles)
    unresolved_diagnostic_count = len(graph.diagnostics.unresolved_references)

    estimated_rerun_seconds = _calculate_estimated_rerun_seconds(
        connection,
        workspace_id,
        len(required_reruns),
    )

    return FlowsheetChangeImpactRead(
        workspace_id=workspace_id,
        source_ref=source_node.ref,
        source_kind=source_node.kind,
        source_label=source_node.label,
        affected_count=len(affected_nodes),
        counts_by_kind=counts_by_kind,
        affected_nodes=affected_nodes,
        required_reruns=required_reruns,
        affected_decisions=affected_decisions,
        estimated_rerun_seconds=estimated_rerun_seconds,
        cycle_count=cycle_count,
        unresolved_diagnostic_count=unresolved_diagnostic_count,
    )


def preview_change_impact(workspace_id: str, node_ref: str) -> FlowsheetChangeImpactRead:
    with open_sqlite_connection() as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        try:
            return preview_change_impact_from_connection(connection, workspace_id, node_ref)
        finally:
            connection.rollback()


def find_upstream_levers_from_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
    node_ref: str,
) -> FlowsheetUpstreamLeversRead:
    target_node = resolve_flowsheet_node_from_connection(connection, workspace_id, node_ref)
    graph = build_flowsheet_graph_from_connection(connection, workspace_id)
    nodes_by_ref = {node.ref: node for node in graph.nodes}
    if target_node.ref not in nodes_by_ref:
        nodes_by_ref[target_node.ref] = target_node

    rev_adjacency: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge.edge_class == "dependency" or edge.relation == "executed_by":
            rev_adjacency.setdefault(edge.downstream_ref, []).append(edge.upstream_ref)
    for items in rev_adjacency.values():
        items.sort()

    best_paths: dict[str, tuple[str, ...]] = {}
    pending: list[tuple[int, tuple[str, ...], str]] = [(1, (target_node.ref,), target_node.ref)]
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
        for upstream_ref in rev_adjacency.get(current_ref, []):
            if upstream_ref not in best_paths:
                next_path = (*path, upstream_ref)
                heapq.heappush(pending, (len(next_path), next_path, upstream_ref))

    lever_refs = sorted(set(best_paths) - {target_node.ref})
    if len(lever_refs) > MAX_FRESHNESS_MARKS_PER_INVALIDATION:
        raise FreshnessError(
            "freshness_mark_limit_exceeded",
            "The freshness closure exceeds the V0 limit.",
            bound="marks",
            observed_count=len(lever_refs),
        )

    levers: list[FlowsheetUpstreamLeverRead] = []
    counts_by_kind: dict[str, int] = {}

    for ref in lever_refs:
        node = nodes_by_ref.get(ref)
        if node is None:
            raw_kind, record_id = ref.split(":", 1)
            kind: NodeKind = cast(NodeKind, raw_kind)
            label = ref
            status = None
        else:
            kind = node.kind
            record_id = node.id
            label = node.label
            status = node.status

        rev_path = best_paths[ref]
        oriented_path = list(reversed(rev_path))
        distance = len(oriented_path) - 1

        counts_by_kind[kind] = counts_by_kind.get(kind, 0) + 1
        levers.append(
            FlowsheetUpstreamLeverRead(
                ref=ref,
                kind=kind,
                id=record_id,
                label=label,
                distance=distance,
                path=oriented_path,
                status=status,
            )
        )

    levers.sort(key=lambda item: (item.distance, item.kind, item.ref))
    counts_by_kind = dict(sorted(counts_by_kind.items()))

    return FlowsheetUpstreamLeversRead(
        workspace_id=workspace_id,
        target_ref=target_node.ref,
        target_kind=target_node.kind,
        target_label=target_node.label,
        lever_count=len(levers),
        counts_by_kind=counts_by_kind,
        levers=levers,
    )


def find_upstream_levers(workspace_id: str, node_ref: str) -> FlowsheetUpstreamLeversRead:
    with open_sqlite_connection() as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        try:
            return find_upstream_levers_from_connection(connection, workspace_id, node_ref)
        finally:
            connection.rollback()
