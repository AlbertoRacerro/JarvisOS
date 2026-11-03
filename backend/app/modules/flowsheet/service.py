from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from app.core.database import open_sqlite_connection
from app.core.topology import TopologyError, deterministic_topological_order
from app.modules.flowsheet.models import (
    FlowsheetDiagnosticsRead,
    FlowsheetEdgeRead,
    FlowsheetGraphRead,
    FlowsheetNodeRead,
    FlowsheetUnresolvedReferenceRead,
)

MAX_GRAPH_NODES = 1000
MAX_GRAPH_EDGES = 3000
MAX_GRAPH_DIAGNOSTICS = 200
MAX_REFERENCE_CHARS = 512
MAX_LABEL_CHARS = 120
MAX_CYCLES = 20
MAX_CYCLE_NODES = 50

_CANONICAL_KINDS = frozenset(
    {
        "model_spec",
        "model_version",
        "simulation_run",
        "runner_job",
        "artifact",
        "assumption",
        "parameter",
        "decision",
        "requirement",
        "ai_job",
        "bluecad_candidate",
        "bluecad_attempt",
        "evidence",
    }
)
_KIND_ALIASES = {"evidence_record": "evidence"}
_CONTEXT_KINDS = frozenset({"decision", "assumption", "parameter", "requirement", "evidence"})
_BLUECAD_ATTEMPT_REF_RE = re.compile(r"^bluecad_candidate:([^:]{1,256}):attempt:([1-9][0-9]*)$")
_BLUECAD_SIM_REF_RE = re.compile(r"^bluecad_candidate:([^:]{1,256}):attempt:([1-9][0-9]*):sim:([^:]{1,256})$")


class FlowsheetError(ValueError):
    def __init__(self, code: str, message: str, *, bound: str | None = None, observed_count: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.bound = bound
        self.observed_count = observed_count


@dataclass
class _EdgeAggregate:
    upstream_ref: str
    downstream_ref: str
    relation: str
    edge_class: str
    authorities: set[str] = field(default_factory=set)
    source_fields: set[str] = field(default_factory=set)


class _GraphBuilder:
    def __init__(self, nodes: dict[str, FlowsheetNodeRead]):
        self.nodes = nodes
        self.edges: dict[tuple[str, str, str, str], _EdgeAggregate] = {}
        self.diagnostics: dict[tuple[str, str, str, str], FlowsheetUnresolvedReferenceRead] = {}
        self.manual_binding_count = 0

    def add_diagnostic(
        self,
        *,
        owner_ref: str,
        source_field: str,
        code: str,
        raw_ref: object | None = None,
    ) -> None:
        rendered = None if raw_ref is None else _truncate(str(raw_ref), 256)
        key = (owner_ref, source_field, code, rendered or "")
        if key not in self.diagnostics:
            self.diagnostics[key] = FlowsheetUnresolvedReferenceRead(
                owner_ref=owner_ref,
                source_field=_truncate(source_field, 160),
                code=code,
                raw_ref=rendered,
            )

    def add_edge(
        self,
        upstream_ref: str,
        downstream_ref: str,
        relation: str,
        edge_class: str,
        authority: str,
        source_field: str,
        *,
        owner_ref: str | None = None,
    ) -> None:
        owner = owner_ref or downstream_ref
        if upstream_ref not in self.nodes:
            self.add_diagnostic(
                owner_ref=owner,
                source_field=source_field,
                code="dangling_reference",
                raw_ref=upstream_ref,
            )
            return
        if downstream_ref not in self.nodes:
            self.add_diagnostic(
                owner_ref=owner,
                source_field=source_field,
                code="dangling_reference",
                raw_ref=downstream_ref,
            )
            return
        key = (upstream_ref, downstream_ref, relation, edge_class)
        aggregate = self.edges.get(key)
        if aggregate is None:
            aggregate = _EdgeAggregate(upstream_ref, downstream_ref, relation, edge_class)
            self.edges[key] = aggregate
        aggregate.authorities.add(authority)
        aggregate.source_fields.add(_truncate(source_field, 160))


def get_flowsheet_node(workspace_id: str, node_ref: str) -> FlowsheetNodeRead:
    with open_sqlite_connection() as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        try:
            return resolve_flowsheet_node_from_connection(connection, workspace_id, node_ref)
        finally:
            connection.rollback()


def resolve_flowsheet_node_from_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
    node_ref: str,
) -> FlowsheetNodeRead:
    _require_workspace(connection, workspace_id)
    kind, record_id = _parse_node_ref(node_ref)
    row = _select_node_row(connection, workspace_id, kind, record_id)
    if row is None:
        raise FlowsheetError("flowsheet_node_not_found", "Flowsheet node not found.")
    return _node_from_row(kind, row)


def get_flowsheet_graph(workspace_id: str) -> FlowsheetGraphRead:
    with open_sqlite_connection() as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        try:
            return build_flowsheet_graph_from_connection(connection, workspace_id)
        finally:
            connection.rollback()


def build_flowsheet_graph_from_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> FlowsheetGraphRead:
    _require_workspace(connection, workspace_id)
    rows = _load_workspace_rows(connection, workspace_id)
    nodes = _build_nodes(rows)
    _enforce_bound("nodes", len(nodes), MAX_GRAPH_NODES)
    builder = _GraphBuilder(nodes)
    _add_foreign_key_edges(builder, rows)
    run_payloads = _add_payload_edges(builder, rows)
    _add_source_reference_edges(builder, rows, run_payloads)
    _add_ai_context_edges(builder, rows)
    edges = _materialize_edges(builder.edges)
    _enforce_bound("edges", len(edges), MAX_GRAPH_EDGES)
    unresolved = sorted(
        builder.diagnostics.values(),
        key=lambda item: (item.owner_ref, item.source_field, item.code, item.raw_ref or ""),
    )
    if len(unresolved) > MAX_GRAPH_DIAGNOSTICS:
        raise FlowsheetError(
            "flowsheet_diagnostics_limit_exceeded",
            "Flowsheet unresolved-reference diagnostics exceed the V0 limit.",
            bound="diagnostics",
            observed_count=len(unresolved),
        )
    node_list = sorted(nodes.values(), key=lambda item: (item.kind, item.id))
    is_acyclic, order, cycles = _topological_projection(nodes, edges)
    diagnostics = FlowsheetDiagnosticsRead(
        unsupported_reference_count=sum(item.code == "unsupported_reference" for item in unresolved),
        malformed_reference_count=sum(item.code == "malformed_reference" for item in unresolved),
        dangling_reference_count=sum(item.code == "dangling_reference" for item in unresolved),
        cycle_count=len(cycles),
        manual_binding_count=builder.manual_binding_count,
        unresolved_references=unresolved,
        cycles=cycles,
    )
    return FlowsheetGraphRead(
        workspace_id=workspace_id,
        nodes=node_list,
        edges=edges,
        topological_order=order,
        is_acyclic=is_acyclic,
        diagnostics=diagnostics,
    )


def _require_workspace(connection: sqlite3.Connection, workspace_id: str) -> None:
    row = connection.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if row is None:
        raise FlowsheetError("flowsheet_workspace_not_found", "Workspace not found.")


def _parse_node_ref(node_ref: str) -> tuple[str, str]:
    if not isinstance(node_ref, str) or not node_ref or len(node_ref) > MAX_REFERENCE_CHARS:
        raise FlowsheetError("flowsheet_ref_invalid", "Flowsheet reference is invalid.")
    if node_ref.count(":") != 1:
        raise FlowsheetError("flowsheet_ref_invalid", "Flowsheet reference must be <kind>:<id>.")
    raw_kind, record_id = node_ref.split(":", 1)
    kind = _KIND_ALIASES.get(raw_kind, raw_kind)
    if kind not in _CANONICAL_KINDS or not record_id or len(record_id) > 256:
        raise FlowsheetError("flowsheet_ref_invalid", "Flowsheet reference is invalid.")
    return kind, record_id


def _select_node_row(
    connection: sqlite3.Connection,
    workspace_id: str,
    kind: str,
    record_id: str,
) -> dict[str, Any] | None:
    direct_queries = {
        "model_spec": "SELECT id, title, status, created_at FROM model_specs WHERE workspace_id = ? AND id = ?",
        "model_version": (
            "SELECT id, version_label, status, implementation_kind, created_at "
            "FROM model_versions WHERE workspace_id = ? AND id = ?"
        ),
        "simulation_run": (
            "SELECT id, run_label, status, created_at FROM simulation_runs WHERE workspace_id = ? AND id = ?"
        ),
        "runner_job": (
            "SELECT id, status, implementation_kind, created_at FROM runner_jobs WHERE workspace_id = ? AND id = ?"
        ),
        "artifact": (
            "SELECT id, filename, status, artifact_type, mime_type, created_at "
            "FROM artifacts WHERE workspace_id = ? AND id = ?"
        ),
        "assumption": (
            "SELECT id, statement, status, origin, created_at FROM assumptions WHERE workspace_id = ? AND id = ?"
        ),
        "parameter": (
            "SELECT id, name, status, origin, unit, value_status, created_at "
            "FROM parameters WHERE workspace_id = ? AND id = ?"
        ),
        "decision": ("SELECT id, title, status, origin, created_at FROM decisions WHERE workspace_id = ? AND id = ?"),
        "requirement": ("SELECT id, statement, status, created_at FROM requirements WHERE workspace_id = ? AND id = ?"),
        "bluecad_candidate": (
            "SELECT id, status, origin, created_at FROM bluecad_candidates WHERE workspace_id = ? AND id = ?"
        ),
        "evidence": ("SELECT id, kind, verdict, created_at FROM evidence_records WHERE workspace_id = ? AND id = ?"),
    }
    if kind in direct_queries:
        row = connection.execute(direct_queries[kind], (workspace_id, record_id)).fetchone()
        return dict(row) if row is not None else None
    if kind == "bluecad_attempt":
        row = connection.execute(
            """
            SELECT ba.id, ba.attempt_no, ba.route_class, ba.proposal_outcome,
                   ba.validation_verdict, ba.started_at AS created_at
            FROM bluecad_attempts ba
            JOIN bluecad_candidates bc ON bc.id = ba.candidate_id
            WHERE bc.workspace_id = ? AND ba.id = ?
            """,
            (workspace_id, record_id),
        ).fetchone()
        return dict(row) if row is not None else None
    if kind == "ai_job":
        row = connection.execute(
            """
            SELECT aj.id, aj.status, aj.task_kind, aj.created_at
            FROM ai_jobs aj
            WHERE aj.id = ? AND (
                EXISTS (SELECT 1 FROM assumptions a WHERE a.workspace_id = ? AND a.source_ai_job_id = aj.id)
                OR EXISTS (SELECT 1 FROM parameters p WHERE p.workspace_id = ? AND p.source_ai_job_id = aj.id)
                OR EXISTS (SELECT 1 FROM decisions d WHERE d.workspace_id = ? AND d.source_ai_job_id = aj.id)
                OR EXISTS (
                    SELECT 1 FROM bluecad_attempts ba
                    JOIN bluecad_candidates bc ON bc.id = ba.candidate_id
                    WHERE bc.workspace_id = ? AND ba.proposal_ai_job_id = aj.id
                )
            )
            """,
            (record_id, workspace_id, workspace_id, workspace_id, workspace_id),
        ).fetchone()
        return dict(row) if row is not None else None
    raise FlowsheetError("flowsheet_ref_invalid", "Flowsheet reference kind is unsupported.")


def _load_workspace_rows(connection: sqlite3.Connection, workspace_id: str) -> dict[str, list[dict[str, Any]]]:
    queries = {
        "model_spec": "SELECT id, title, status, created_at FROM model_specs WHERE workspace_id = ?",
        "model_version": (
            "SELECT id, model_spec_id, implementation_artifact_id, version_label, status, "
            "implementation_kind, created_at FROM model_versions WHERE workspace_id = ?"
        ),
        "simulation_run": (
            "SELECT id, model_version_id, run_label, status, input_payload, created_at "
            "FROM simulation_runs WHERE workspace_id = ?"
        ),
        "runner_job": (
            "SELECT id, simulation_run_id, status, implementation_kind, created_at "
            "FROM runner_jobs WHERE workspace_id = ?"
        ),
        "artifact": (
            "SELECT id, filename, status, artifact_type, mime_type, source_ref, created_at "
            "FROM artifacts WHERE workspace_id = ?"
        ),
        "assumption": (
            "SELECT id, statement, status, origin, source_ref, source_ai_job_id, created_at "
            "FROM assumptions WHERE workspace_id = ?"
        ),
        "parameter": (
            "SELECT id, name, status, origin, unit, value_status, source_ref, source_ai_job_id, created_at "
            "FROM parameters WHERE workspace_id = ?"
        ),
        "decision": (
            "SELECT id, title, status, origin, linked_run_id, source_ai_job_id, created_at "
            "FROM decisions WHERE workspace_id = ?"
        ),
        "requirement": ("SELECT id, statement, status, created_at FROM requirements WHERE workspace_id = ?"),
        "bluecad_candidate": (
            "SELECT id, status, origin, parent_candidate_id, spec_artifact_id, glb_artifact_id, "
            "report_artifact_id, promoted_decision_id, created_at "
            "FROM bluecad_candidates WHERE workspace_id = ?"
        ),
        "evidence": (
            "SELECT id, kind, verdict, source_run_id, candidate_id, attempt_id, report_artifact_id, created_at "
            "FROM evidence_records WHERE workspace_id = ?"
        ),
        "run_artifact": ("SELECT simulation_run_id, artifact_id, role FROM run_artifacts WHERE workspace_id = ?"),
        "cad_link": (
            "SELECT source_simulation_run_id, child_candidate_id, transformation_version "
            "FROM bluecad_cad_links WHERE workspace_id = ?"
        ),
    }
    rows = {
        kind: [dict(row) for row in connection.execute(query, (workspace_id,)).fetchall()]
        for kind, query in queries.items()
    }
    rows["bluecad_attempt"] = [
        dict(row)
        for row in connection.execute(
            """
            SELECT ba.id, ba.candidate_id, ba.attempt_no, ba.route_class, ba.proposal_ai_job_id,
                   ba.proposal_outcome, ba.validation_verdict, ba.spec_artifact_id,
                   ba.report_artifact_id, ba.manifest_artifact_id, ba.started_at AS created_at
            FROM bluecad_attempts ba
            JOIN bluecad_candidates bc ON bc.id = ba.candidate_id
            WHERE bc.workspace_id = ?
            """,
            (workspace_id,),
        ).fetchall()
    ]
    ai_ids = {
        str(row[field])
        for kind, field in (
            ("assumption", "source_ai_job_id"),
            ("parameter", "source_ai_job_id"),
            ("decision", "source_ai_job_id"),
            ("bluecad_attempt", "proposal_ai_job_id"),
        )
        for row in rows[kind]
        if row.get(field)
    }
    rows["ai_job"] = _load_ai_jobs(connection, ai_ids)
    return rows


def _load_ai_jobs(connection: sqlite3.Connection, ai_ids: set[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    ordered = sorted(ai_ids)
    for start in range(0, len(ordered), 400):
        batch = ordered[start : start + 400]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        result.extend(
            dict(row)
            for row in connection.execute(
                f"SELECT id, status, task_kind, context_sources_json, created_at FROM ai_jobs WHERE id IN ({placeholders})",
                batch,
            ).fetchall()
        )
    return result


def _build_nodes(rows: dict[str, list[dict[str, Any]]]) -> dict[str, FlowsheetNodeRead]:
    nodes: dict[str, FlowsheetNodeRead] = {}
    for kind in sorted(_CANONICAL_KINDS):
        for row in rows.get(kind, []):
            node = _node_from_row(kind, row)
            nodes[node.ref] = node
    return nodes


def _node_from_row(kind: str, row: dict[str, Any]) -> FlowsheetNodeRead:
    record_id = str(row["id"])
    metadata: dict[str, str | int | float | bool | None] = {}
    status = row.get("status")
    origin = row.get("origin")
    created_at = row.get("created_at")
    if kind == "model_spec":
        label = str(row.get("title") or f"Model spec {record_id[:8]}")
    elif kind == "model_version":
        label = str(row.get("version_label") or f"Model version {record_id[:8]}")
        metadata["implementation_kind"] = row.get("implementation_kind")
    elif kind == "simulation_run":
        label = str(row.get("run_label") or f"Simulation run {record_id[:8]}")
        metadata["run_label"] = row.get("run_label")
    elif kind == "runner_job":
        label = f"Runner job {record_id[:8]}"
        metadata["implementation_kind"] = row.get("implementation_kind")
    elif kind == "artifact":
        label = str(row.get("filename") or f"Artifact {record_id[:8]}")
        metadata["artifact_type"] = row.get("artifact_type")
        metadata["mime_type"] = row.get("mime_type")
    elif kind == "assumption":
        label = f"Assumption {record_id[:8]}"
    elif kind == "parameter":
        label = str(row.get("name") or f"Parameter {record_id[:8]}")
        metadata["unit"] = row.get("unit")
        metadata["value_status"] = row.get("value_status")
    elif kind == "decision":
        label = str(row.get("title") or f"Decision {record_id[:8]}")
    elif kind == "requirement":
        label = f"Requirement {record_id[:8]}"
    elif kind == "ai_job":
        label = f"AI job: {row.get('task_kind') or record_id[:8]}"
        metadata["task_kind"] = row.get("task_kind")
    elif kind == "bluecad_candidate":
        label = f"BLUECAD candidate {record_id[:8]}"
    elif kind == "bluecad_attempt":
        label = f"BLUECAD attempt {row.get('attempt_no')}"
        metadata["attempt_no"] = row.get("attempt_no")
        metadata["route_class"] = row.get("route_class")
        metadata["validation_verdict"] = row.get("validation_verdict")
        status = row.get("proposal_outcome")
    elif kind == "evidence":
        label = f"{row.get('kind') or 'Evidence'} evidence"
        metadata["evidence_kind"] = row.get("kind")
        metadata["verdict"] = row.get("verdict")
        status = row.get("verdict")
    else:  # pragma: no cover - protected by canonical kind registry.
        raise FlowsheetError("flowsheet_ref_invalid", "Unsupported flowsheet node kind.")
    return FlowsheetNodeRead(
        ref=_ref(kind, record_id),
        kind=kind,
        id=record_id,
        label=_truncate(label, MAX_LABEL_CHARS),
        status=None if status is None else str(status),
        origin=None if origin is None else str(origin),
        created_at=None if created_at is None else str(created_at),
        metadata={key: value for key, value in metadata.items() if value is not None},
    )


def _add_foreign_key_edges(builder: _GraphBuilder, rows: dict[str, list[dict[str, Any]]]) -> None:
    for row in rows["model_version"]:
        downstream = _ref("model_version", row["id"])
        _add_typed_edge(
            builder,
            "model_spec",
            row.get("model_spec_id"),
            downstream,
            "has_version",
            "dependency",
            "model_versions.model_spec_id",
        )
        _add_typed_edge(
            builder,
            "artifact",
            row.get("implementation_artifact_id"),
            downstream,
            "implementation_artifact",
            "dependency",
            "model_versions.implementation_artifact_id",
        )
    for row in rows["simulation_run"]:
        if row.get("model_version_id"):
            _add_typed_edge(
                builder,
                "model_version",
                row["model_version_id"],
                _ref("simulation_run", row["id"]),
                "configured_run",
                "dependency",
                "simulation_runs.model_version_id",
            )
    for row in rows["runner_job"]:
        _add_typed_edge(
            builder,
            "simulation_run",
            row.get("simulation_run_id"),
            _ref("runner_job", row["id"]),
            "executed_by",
            "provenance",
            "runner_jobs.simulation_run_id",
        )
    for row in rows["run_artifact"]:
        role = _truncate(str(row.get("role") or "other"), 80)
        _add_typed_edge(
            builder,
            "simulation_run",
            row.get("simulation_run_id"),
            _ref("artifact", row.get("artifact_id")),
            "produced_artifact",
            "dependency",
            f"run_artifacts.artifact_id[role={role}]",
        )
    for row in rows["decision"]:
        downstream = _ref("decision", row["id"])
        if row.get("linked_run_id"):
            _add_typed_edge(
                builder,
                "simulation_run",
                row["linked_run_id"],
                downstream,
                "informed_decision",
                "dependency",
                "decisions.linked_run_id",
            )
        _add_ai_proposal_edge(builder, row, downstream, "decisions.source_ai_job_id")
    for kind, source_field in (
        ("assumption", "assumptions.source_ai_job_id"),
        ("parameter", "parameters.source_ai_job_id"),
    ):
        for row in rows[kind]:
            _add_ai_proposal_edge(builder, row, _ref(kind, row["id"]), source_field)
    for row in rows["cad_link"]:
        relation = "m0_geometry_link"
        _add_typed_edge(
            builder,
            "simulation_run",
            row.get("source_simulation_run_id"),
            _ref("bluecad_candidate", row.get("child_candidate_id")),
            relation,
            "dependency",
            "bluecad_cad_links.source_simulation_run_id",
        )
    for row in rows["bluecad_candidate"]:
        candidate_ref = _ref("bluecad_candidate", row["id"])
        if row.get("parent_candidate_id"):
            _add_typed_edge(
                builder,
                "bluecad_candidate",
                row["parent_candidate_id"],
                candidate_ref,
                "parent_candidate",
                "dependency",
                "bluecad_candidates.parent_candidate_id",
            )
        for field_name in ("spec_artifact_id", "glb_artifact_id", "report_artifact_id"):
            if row.get(field_name):
                _add_typed_edge(
                    builder,
                    "bluecad_candidate",
                    row["id"],
                    _ref("artifact", row[field_name]),
                    "process_link_artifact"
                    if row.get("origin") == "process_linked"
                    else "current_candidate_artifact",
                    "dependency"
                    if row.get("origin") == "process_linked"
                    else "provenance",
                    f"bluecad_candidates.{field_name}",
                )
        if row.get("promoted_decision_id"):
            _add_typed_edge(
                builder,
                "bluecad_candidate",
                row["id"],
                _ref("decision", row["promoted_decision_id"]),
                "promoted_as",
                "provenance",
                "bluecad_candidates.promoted_decision_id",
            )
    candidate_origins = {
        candidate.get("id"): candidate.get("origin") for candidate in rows["bluecad_candidate"]
    }
    for row in rows["bluecad_attempt"]:
        attempt_ref = _ref("bluecad_attempt", row["id"])
        _add_typed_edge(
            builder,
            "bluecad_candidate",
            row.get("candidate_id"),
            attempt_ref,
            "process_link_build"
            if candidate_origins.get(row.get("candidate_id")) == "process_linked"
            else "has_attempt",
            "dependency"
            if candidate_origins.get(row.get("candidate_id")) == "process_linked"
            else "provenance",
            "bluecad_attempts.candidate_id",
        )
        if row.get("proposal_ai_job_id"):
            _add_typed_edge(
                builder,
                "ai_job",
                row["proposal_ai_job_id"],
                attempt_ref,
                "proposed_attempt",
                "provenance",
                "bluecad_attempts.proposal_ai_job_id",
            )
        for field_name in ("spec_artifact_id", "report_artifact_id", "manifest_artifact_id"):
            if row.get(field_name):
                _add_typed_edge(
                    builder,
                    "bluecad_attempt",
                    row["id"],
                    _ref("artifact", row[field_name]),
                    "attempt_artifact",
                    "dependency",
                    f"bluecad_attempts.{field_name}",
                )
    for row in rows["evidence"]:
        evidence_ref = _ref("evidence", row["id"])
        mappings = (
            ("simulation_run", "source_run_id", "supports_evidence", "dependency"),
            ("bluecad_candidate", "candidate_id", "candidate_evidence", "provenance"),
            ("bluecad_attempt", "attempt_id", "attempt_evidence", "provenance"),
            ("artifact", "report_artifact_id", "reported_evidence", "dependency"),
        )
        for kind, field_name, relation, edge_class in mappings:
            if row.get(field_name):
                _add_typed_edge(
                    builder,
                    kind,
                    row[field_name],
                    evidence_ref,
                    relation,
                    edge_class,
                    f"evidence_records.{field_name}",
                )


def _add_ai_proposal_edge(builder: _GraphBuilder, row: dict[str, Any], downstream: str, source_field: str) -> None:
    if row.get("source_ai_job_id"):
        _add_typed_edge(
            builder,
            "ai_job",
            row["source_ai_job_id"],
            downstream,
            "proposed_record",
            "provenance",
            source_field,
        )


def _add_typed_edge(
    builder: _GraphBuilder,
    upstream_kind: str,
    upstream_id: object | None,
    downstream_ref: str,
    relation: str,
    edge_class: str,
    source_field: str,
) -> None:
    if upstream_id is None:
        return
    builder.add_edge(
        _ref(upstream_kind, upstream_id),
        downstream_ref,
        relation,
        edge_class,
        "foreign_key",
        source_field,
        owner_ref=downstream_ref,
    )


def _add_payload_edges(
    builder: _GraphBuilder,
    rows: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any] | None]:
    model_versions = {str(row["id"]): row for row in rows["model_version"]}
    attempts = {str(row["id"]): row for row in rows["bluecad_attempt"]}
    payloads: dict[str, dict[str, Any] | None] = {}
    for run in rows["simulation_run"]:
        run_id = str(run["id"])
        run_ref = _ref("simulation_run", run_id)
        payload = _decode_payload(builder, run_ref, "simulation_runs.input_payload", run.get("input_payload"))
        payloads[run_id] = payload
        model_version = model_versions.get(str(run.get("model_version_id"))) if run.get("model_version_id") else None
        implementation_kind = None if model_version is None else model_version.get("implementation_kind")
        if implementation_kind == "calc_v0" and payload is not None:
            for variable_name, item in sorted(payload.items()):
                source_field = f"simulation_runs.input_payload:{_truncate(str(variable_name), 80)}"
                if not isinstance(item, dict):
                    builder.add_diagnostic(
                        owner_ref=run_ref,
                        source_field=source_field,
                        code="payload_invalid",
                    )
                    continue
                source_parameter_id = item.get("source_parameter_id")
                if source_parameter_id is None:
                    builder.manual_binding_count += 1
                    continue
                if not isinstance(source_parameter_id, str) or not source_parameter_id:
                    builder.add_diagnostic(
                        owner_ref=run_ref,
                        source_field=source_field,
                        code="payload_reference_invalid",
                        raw_ref=source_parameter_id,
                    )
                    continue
                builder.add_edge(
                    _ref("parameter", source_parameter_id),
                    run_ref,
                    "bound_input",
                    "dependency",
                    "payload_binding",
                    source_field,
                    owner_ref=run_ref,
                )
        elif implementation_kind == "batch_growth_v0" and payload is not None:
            artifact_ids = payload.get("input_artifact_ids", [])
            if not isinstance(artifact_ids, list) or not all(isinstance(item, str) and item for item in artifact_ids):
                builder.add_diagnostic(
                    owner_ref=run_ref,
                    source_field="simulation_runs.input_payload:input_artifact_ids",
                    code="payload_reference_invalid",
                )
            else:
                for artifact_id in sorted(set(artifact_ids)):
                    builder.add_edge(
                        _ref("artifact", artifact_id),
                        run_ref,
                        "input_artifact",
                        "dependency",
                        "payload_binding",
                        "simulation_runs.input_payload:input_artifact_ids",
                        owner_ref=run_ref,
                    )
        elif model_version is None and payload is not None and ("candidate_id" in payload or "attempt_id" in payload):
            candidate_id = payload.get("candidate_id")
            attempt_id = payload.get("attempt_id")
            if (
                not isinstance(candidate_id, str)
                or not candidate_id
                or not isinstance(attempt_id, str)
                or not attempt_id
            ):
                builder.add_diagnostic(
                    owner_ref=run_ref,
                    source_field="simulation_runs.input_payload",
                    code="payload_reference_invalid",
                )
                continue
            attempt = attempts.get(attempt_id)
            if attempt is None or str(attempt.get("candidate_id")) != candidate_id:
                builder.add_diagnostic(
                    owner_ref=run_ref,
                    source_field="simulation_runs.input_payload",
                    code="payload_reference_invalid",
                    raw_ref=f"bluecad_candidate:{candidate_id}:attempt_id:{attempt_id}",
                )
                continue
            builder.add_edge(
                _ref("bluecad_candidate", candidate_id),
                run_ref,
                "candidate_simulation",
                "dependency",
                "payload_binding",
                "simulation_runs.input_payload:candidate_id",
                owner_ref=run_ref,
            )
            builder.add_edge(
                _ref("bluecad_attempt", attempt_id),
                run_ref,
                "attempt_simulation",
                "dependency",
                "payload_binding",
                "simulation_runs.input_payload:attempt_id",
                owner_ref=run_ref,
            )
    return payloads


def _decode_payload(
    builder: _GraphBuilder,
    owner_ref: str,
    source_field: str,
    raw_payload: object | None,
) -> dict[str, Any] | None:
    if raw_payload is None:
        return None
    try:
        payload = json.loads(str(raw_payload))
    except (TypeError, ValueError, json.JSONDecodeError):
        builder.add_diagnostic(owner_ref=owner_ref, source_field=source_field, code="payload_invalid")
        return None
    if not isinstance(payload, dict):
        builder.add_diagnostic(owner_ref=owner_ref, source_field=source_field, code="payload_invalid")
        return None
    return payload


def _add_source_reference_edges(
    builder: _GraphBuilder,
    rows: dict[str, list[dict[str, Any]]],
    run_payloads: dict[str, dict[str, Any] | None],
) -> None:
    attempt_by_candidate_number = {
        (str(row["candidate_id"]), int(row["attempt_no"])): row for row in rows["bluecad_attempt"]
    }
    for kind, table_field in (
        ("assumption", "assumptions.source_ref"),
        ("parameter", "parameters.source_ref"),
        ("artifact", "artifacts.source_ref"),
    ):
        for row in rows[kind]:
            raw_ref = row.get("source_ref")
            if raw_ref is None or str(raw_ref).strip() == "":
                continue
            owner_ref = _ref(kind, row["id"])
            target_ref = _normalize_source_reference(
                builder,
                owner_ref=owner_ref,
                source_field=table_field,
                raw_ref=str(raw_ref).strip(),
                attempt_by_candidate_number=attempt_by_candidate_number,
                run_payloads=run_payloads,
            )
            if target_ref is not None:
                builder.add_edge(
                    target_ref,
                    owner_ref,
                    "source_reference",
                    "dependency",
                    "source_ref",
                    table_field,
                    owner_ref=owner_ref,
                )


def _normalize_source_reference(
    builder: _GraphBuilder,
    *,
    owner_ref: str,
    source_field: str,
    raw_ref: str,
    attempt_by_candidate_number: dict[tuple[str, int], dict[str, Any]],
    run_payloads: dict[str, dict[str, Any] | None],
) -> str | None:
    if not raw_ref or len(raw_ref) > MAX_REFERENCE_CHARS:
        builder.add_diagnostic(
            owner_ref=owner_ref,
            source_field=source_field,
            code="malformed_reference",
            raw_ref=raw_ref,
        )
        return None
    sim_match = _BLUECAD_SIM_REF_RE.fullmatch(raw_ref)
    if sim_match:
        candidate_id, attempt_number_text, run_id = sim_match.groups()
        attempt = attempt_by_candidate_number.get((candidate_id, int(attempt_number_text)))
        if attempt is None or _ref("bluecad_candidate", candidate_id) not in builder.nodes:
            builder.add_diagnostic(
                owner_ref=owner_ref,
                source_field=source_field,
                code="dangling_reference",
                raw_ref=raw_ref,
            )
            return None
        run_ref = _ref("simulation_run", run_id)
        payload = run_payloads.get(run_id)
        if run_ref not in builder.nodes:
            builder.add_diagnostic(
                owner_ref=owner_ref,
                source_field=source_field,
                code="dangling_reference",
                raw_ref=raw_ref,
            )
            return None
        if payload is None or payload.get("candidate_id") != candidate_id or payload.get("attempt_id") != attempt["id"]:
            builder.add_diagnostic(
                owner_ref=owner_ref,
                source_field=source_field,
                code="payload_reference_invalid",
                raw_ref=raw_ref,
            )
            return None
        return run_ref
    attempt_match = _BLUECAD_ATTEMPT_REF_RE.fullmatch(raw_ref)
    if attempt_match:
        candidate_id, attempt_number_text = attempt_match.groups()
        attempt = attempt_by_candidate_number.get((candidate_id, int(attempt_number_text)))
        if attempt is None or _ref("bluecad_candidate", candidate_id) not in builder.nodes:
            builder.add_diagnostic(
                owner_ref=owner_ref,
                source_field=source_field,
                code="dangling_reference",
                raw_ref=raw_ref,
            )
            return None
        return _ref("bluecad_attempt", attempt["id"])
    if raw_ref.count(":") == 1:
        raw_kind, record_id = raw_ref.split(":", 1)
        kind = _KIND_ALIASES.get(raw_kind, raw_kind)
        if kind not in _CANONICAL_KINDS:
            builder.add_diagnostic(
                owner_ref=owner_ref,
                source_field=source_field,
                code="unsupported_reference",
                raw_ref=raw_ref,
            )
            return None
        if not record_id or len(record_id) > 256:
            builder.add_diagnostic(
                owner_ref=owner_ref,
                source_field=source_field,
                code="malformed_reference",
                raw_ref=raw_ref,
            )
            return None
        target_ref = _ref(kind, record_id)
        if target_ref not in builder.nodes:
            builder.add_diagnostic(
                owner_ref=owner_ref,
                source_field=source_field,
                code="dangling_reference",
                raw_ref=raw_ref,
            )
            return None
        return target_ref
    code = "malformed_reference" if raw_ref.startswith("bluecad_candidate:") else "unsupported_reference"
    builder.add_diagnostic(owner_ref=owner_ref, source_field=source_field, code=code, raw_ref=raw_ref)
    return None


def _add_ai_context_edges(builder: _GraphBuilder, rows: dict[str, list[dict[str, Any]]]) -> None:
    for row in rows["ai_job"]:
        raw_manifest = row.get("context_sources_json")
        if raw_manifest is None:
            continue
        owner_ref = _ref("ai_job", row["id"])
        try:
            manifest = json.loads(str(raw_manifest))
        except (TypeError, ValueError, json.JSONDecodeError):
            builder.add_diagnostic(
                owner_ref=owner_ref,
                source_field="ai_jobs.context_sources_json",
                code="context_manifest_invalid",
            )
            continue
        if not isinstance(manifest, list):
            builder.add_diagnostic(
                owner_ref=owner_ref,
                source_field="ai_jobs.context_sources_json",
                code="context_manifest_invalid",
            )
            continue
        for index, item in enumerate(manifest):
            source_field = f"ai_jobs.context_sources_json[{index}]"
            if not isinstance(item, dict):
                builder.add_diagnostic(
                    owner_ref=owner_ref,
                    source_field=source_field,
                    code="context_manifest_invalid",
                )
                continue
            kind = item.get("type")
            record_id = item.get("id")
            source = item.get("source")
            if kind not in _CONTEXT_KINDS or not isinstance(record_id, str) or not record_id:
                builder.add_diagnostic(
                    owner_ref=owner_ref,
                    source_field=source_field,
                    code="context_manifest_invalid",
                    raw_ref=source,
                )
                continue
            expected = _ref(str(kind), record_id)
            if source is not None and source != expected:
                builder.add_diagnostic(
                    owner_ref=owner_ref,
                    source_field=source_field,
                    code="context_manifest_invalid",
                    raw_ref=source,
                )
                continue
            builder.add_edge(
                expected,
                owner_ref,
                "context_for",
                "provenance",
                "context_manifest",
                source_field,
                owner_ref=owner_ref,
            )


def _materialize_edges(
    aggregates: dict[tuple[str, str, str, str], _EdgeAggregate],
) -> list[FlowsheetEdgeRead]:
    result: list[FlowsheetEdgeRead] = []
    for key in sorted(aggregates):
        aggregate = aggregates[key]
        digest_input = "|".join(
            [aggregate.upstream_ref, aggregate.downstream_ref, aggregate.relation, aggregate.edge_class]
        )
        edge_id = "sha256:" + hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
        result.append(
            FlowsheetEdgeRead(
                id=edge_id,
                upstream_ref=aggregate.upstream_ref,
                downstream_ref=aggregate.downstream_ref,
                relation=aggregate.relation,
                edge_class=aggregate.edge_class,
                authorities=sorted(aggregate.authorities),
                source_fields=sorted(aggregate.source_fields),
            )
        )
    return result


def _topological_projection(
    nodes: dict[str, FlowsheetNodeRead],
    edges: list[FlowsheetEdgeRead],
) -> tuple[bool, list[str] | None, list[list[str]]]:
    adjacency = {node_ref: set() for node_ref in nodes}
    dependency_edges: list[tuple[str, str]] = []
    for edge in edges:
        if edge.edge_class != "dependency":
            continue
        pair = (edge.upstream_ref, edge.downstream_ref)
        if edge.downstream_ref not in adjacency[edge.upstream_ref]:
            adjacency[edge.upstream_ref].add(edge.downstream_ref)
            dependency_edges.append(pair)
    try:
        order = deterministic_topological_order(
            nodes,
            dependency_edges,
            max_nodes=MAX_GRAPH_NODES,
            max_edges=MAX_GRAPH_EDGES,
        )
    except TopologyError as exc:
        if exc.code != "topology_cycle":
            raise FlowsheetError(
                "flowsheet_graph_invalid",
                "Flowsheet dependency graph is invalid.",
            ) from exc
        cyclic_nodes = set(exc.residual_nodes) or set(nodes)
        cycles = _find_directed_cycles(adjacency, cyclic_nodes)
        return False, None, cycles
    return True, list(order), []


def _find_directed_cycles(adjacency: dict[str, set[str]], candidates: set[str]) -> list[list[str]]:
    state: dict[str, int] = {}
    path: list[str] = []
    positions: dict[str, int] = {}
    found: set[tuple[str, ...]] = set()
    for start in sorted(candidates):
        if state.get(start, 0) != 0 or len(found) >= MAX_CYCLES:
            continue
        stack: list[tuple[str, int, list[str]]] = [(start, 0, sorted(adjacency[start] & candidates))]
        while stack and len(found) < MAX_CYCLES:
            node, index, neighbors = stack[-1]
            if state.get(node, 0) == 0:
                state[node] = 1
                positions[node] = len(path)
                path.append(node)
            if index < len(neighbors):
                downstream = neighbors[index]
                stack[-1] = (node, index + 1, neighbors)
                downstream_state = state.get(downstream, 0)
                if downstream_state == 0:
                    stack.append((downstream, 0, sorted(adjacency[downstream] & candidates)))
                elif downstream_state == 1:
                    cycle_nodes = path[positions[downstream] :]
                    if len(cycle_nodes) <= MAX_CYCLE_NODES:
                        found.add(_canonical_cycle(cycle_nodes))
                continue
            state[node] = 2
            stack.pop()
            positions.pop(node, None)
            if path and path[-1] == node:
                path.pop()
    return [list(cycle) + [cycle[0]] for cycle in sorted(found)]


def _canonical_cycle(cycle: list[str]) -> tuple[str, ...]:
    rotations = [tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))]
    return min(rotations)


def _enforce_bound(name: str, observed: int, maximum: int) -> None:
    if observed > maximum:
        raise FlowsheetError(
            "flowsheet_graph_limit_exceeded",
            "Flowsheet graph exceeds the V0 size limit.",
            bound=name,
            observed_count=observed,
        )


def _ref(kind: str, record_id: object) -> str:
    return f"{kind}:{record_id}"


def _truncate(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    return value[: maximum - 1] + "…"
