"""Typed evidence records for BLUECAD result artifacts."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.core.database import open_sqlite_connection
from app.core.repository import optional_row_to_model, row_to_model, rows_to_models
from app.modules.events.service import utc_now

EvidenceKind = Literal["fem_static_v0", "mesh_quality_v0", "validation_v0"]
_ALLOWED_KINDS = {"fem_static_v0", "mesh_quality_v0", "validation_v0"}
_METRIC_ORDER = {
    "mesh_quality_v0": ("elements_total", "nodes_total", "empty_groups", "attempts"),
    "fem_static_v0": (
        "max_displacement_value",
        "max_von_mises_value",
        "solver_error_code",
        "t3_checks_total",
        "t3_checks_failed",
    ),
    "validation_v0": ("checks_total", "checks_failed", "tier_max", "errors_total"),
}
_MAX_LINE_CHARS = 300


class EvidenceRecordCreate(BaseModel):
    workspace_id: str = Field(min_length=1)
    kind: EvidenceKind
    verdict: str = Field(min_length=1)
    metrics_json: str = Field(min_length=2)
    source_run_id: str | None = None
    candidate_id: str | None = None
    attempt_id: str | None = None
    report_artifact_id: str = Field(min_length=1)


class EvidenceRecord(EvidenceRecordCreate):
    id: str
    created_at: str


def _json_metrics(metrics: dict[str, Any]) -> str:
    return json.dumps(metrics, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_verdict(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("verdict must be a non-empty string")
    return value


def map_mesh_quality_evidence(
    workspace_id: str,
    result: dict[str, Any],
    *,
    source_run_id: str | None,
    report_artifact_id: str,
) -> EvidenceRecordCreate:
    verdict = _require_verdict(result.get("verdict"))
    attempts = result.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("mesh result requires at least one attempt")
    last_attempt = _require_mapping(attempts[-1], "last mesh attempt")
    requires_totals = verdict == "pass"
    if requires_totals:
        counts = _require_mapping(last_attempt.get("counts"), "mesh counts")
    else:
        counts = last_attempt.get("counts", {})
        if counts is None:
            counts = {}
        counts = _require_mapping(counts, "mesh counts")
    errors = result.get("errors", [])
    if not isinstance(errors, list):
        raise ValueError("mesh errors must be a list")
    empty_groups = []
    for error in errors:
        if isinstance(error, dict) and error.get("code") == "MESH_GROUP_EMPTY":
            detail = error.get("detail", {})
            if isinstance(detail, dict) and isinstance(detail.get("group"), str):
                empty_groups.append(detail["group"])
    if requires_totals:
        elements_total = counts["elements_total"]
        nodes_total = counts["nodes_total"]
    else:
        elements_total = counts.get("elements_total")
        nodes_total = counts.get("nodes_total")
    metrics = {
        "elements_total": None if elements_total is None else int(elements_total),
        "nodes_total": None if nodes_total is None else int(nodes_total),
        "empty_groups": empty_groups,
        "attempts": len(attempts),
    }
    return EvidenceRecordCreate(
        workspace_id=workspace_id,
        kind="mesh_quality_v0",
        verdict=verdict,
        metrics_json=_json_metrics(metrics),
        source_run_id=source_run_id,
        report_artifact_id=report_artifact_id,
    )


def map_fem_static_evidence(
    workspace_id: str,
    result_summary: dict[str, Any],
    report: dict[str, Any] | None,
    *,
    source_run_id: str | None,
    report_artifact_id: str,
) -> EvidenceRecordCreate:
    verdict = _require_verdict((report or result_summary).get("verdict"))
    is_error = result_summary.get("verdict") == "error"
    if is_error:
        max_displacement = result_summary.get("max_displacement")
        max_von_mises = result_summary.get("max_von_mises")
        if max_displacement is not None:
            max_displacement = _require_mapping(max_displacement, "max_displacement")
        if max_von_mises is not None:
            max_von_mises = _require_mapping(max_von_mises, "max_von_mises")
    else:
        max_displacement = _require_mapping(result_summary.get("max_displacement"), "max_displacement")
        max_von_mises = _require_mapping(result_summary.get("max_von_mises"), "max_von_mises")
    errors = result_summary.get("errors", [])
    if not isinstance(errors, list):
        raise ValueError("FEM errors must be a list")
    checks = [] if report is None else report.get("checks", [])
    if not isinstance(checks, list):
        raise ValueError("FEM report checks must be a list")
    solver_error_code = None
    if result_summary.get("verdict") == "error" and errors and isinstance(errors[0], dict):
        solver_error_code = errors[0].get("code")
    metrics = {
        "max_displacement_value": None if max_displacement is None else float(max_displacement["value"]),
        "max_von_mises_value": None if max_von_mises is None else float(max_von_mises["value"]),
        "solver_error_code": solver_error_code,
        "t3_checks_total": len(checks),
        "t3_checks_failed": sum(1 for check in checks if not isinstance(check, dict) or check.get("status") != "pass"),
    }
    return EvidenceRecordCreate(
        workspace_id=workspace_id,
        kind="fem_static_v0",
        verdict=verdict,
        metrics_json=_json_metrics(metrics),
        source_run_id=source_run_id,
        report_artifact_id=report_artifact_id,
    )


def map_validation_evidence(
    workspace_id: str,
    candidate_id: str,
    attempt_id: str,
    report: dict[str, Any],
    *,
    report_artifact_id: str,
) -> EvidenceRecordCreate:
    verdict = _require_verdict(report.get("verdict"))
    checks = report.get("checks", [])
    errors = report.get("errors", [])
    if not isinstance(checks, list) or not isinstance(errors, list):
        raise ValueError("validation report checks and errors must be lists")
    tiers = [int(check.get("tier", 0)) for check in checks if isinstance(check, dict)]
    metrics = {
        "checks_total": len(checks),
        "checks_failed": sum(1 for check in checks if not isinstance(check, dict) or check.get("status") != "pass"),
        "tier_max": max(tiers, default=0),
        "errors_total": len(errors),
    }
    return EvidenceRecordCreate(
        workspace_id=workspace_id,
        kind="validation_v0",
        verdict=verdict,
        metrics_json=_json_metrics(metrics),
        candidate_id=candidate_id,
        attempt_id=attempt_id,
        report_artifact_id=report_artifact_id,
    )


def create_evidence_record(payload: EvidenceRecordCreate) -> EvidenceRecord:
    record_id = str(uuid4())
    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO evidence_records (
                id, workspace_id, kind, verdict, metrics_json, source_run_id,
                candidate_id, attempt_id, report_artifact_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                payload.workspace_id,
                payload.kind,
                payload.verdict,
                payload.metrics_json,
                payload.source_run_id,
                payload.candidate_id,
                payload.attempt_id,
                payload.report_artifact_id,
                now,
            ),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM evidence_records WHERE id = ?", (record_id,)).fetchone()
    return row_to_model(row, EvidenceRecord)


def get_evidence_record(record_id: str) -> EvidenceRecord | None:
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT * FROM evidence_records WHERE id = ?", (record_id,)).fetchone()
    return optional_row_to_model(row, EvidenceRecord)


def record_mesh_quality_evidence(
    workspace_id: str,
    result: dict[str, Any],
    *,
    source_run_id: str | None,
    report_artifact_id: str,
) -> str:
    return create_evidence_record(
        map_mesh_quality_evidence(
            workspace_id,
            result,
            source_run_id=source_run_id,
            report_artifact_id=report_artifact_id,
        )
    ).id


def record_fem_static_evidence(
    workspace_id: str,
    result_summary: dict[str, Any],
    report: dict[str, Any] | None,
    *,
    source_run_id: str | None,
    report_artifact_id: str,
) -> str:
    return create_evidence_record(
        map_fem_static_evidence(
            workspace_id,
            result_summary,
            report,
            source_run_id=source_run_id,
            report_artifact_id=report_artifact_id,
        )
    ).id


def record_validation_evidence(workspace_id: str, candidate_id: str, attempt_id: str, report: dict[str, Any], *, report_artifact_id: str) -> str:
    return create_evidence_record(map_validation_evidence(workspace_id, candidate_id, attempt_id, report, report_artifact_id=report_artifact_id)).id


def select_evidence_records(
    workspace_id: str,
    *,
    statuses: list[str],
    ids: list[str] | None,
    query: str | None,
    max_items: int,
    connection: sqlite3.Connection | None = None,
) -> list[EvidenceRecord]:
    if connection is None:
        with open_sqlite_connection() as owned_connection:
            return select_evidence_records(
                workspace_id,
                statuses=statuses,
                ids=ids,
                query=query,
                max_items=max_items,
                connection=owned_connection,
            )

    values: list[object] = [workspace_id]
    clauses = ["workspace_id = ?"]
    selected_ids = set(ids or [])
    if selected_ids:
        placeholders = ", ".join("?" for _ in selected_ids)
        clauses.append(f"id IN ({placeholders})")
        values.extend(sorted(selected_ids))
    else:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        clauses.append(f"verdict IN ({placeholders})")
        values.extend(statuses)
    normalized_query = query.strip().lower() if query else None
    if normalized_query:
        clauses.append("(LOWER(kind) LIKE ? ESCAPE '\\' OR LOWER(verdict) LIKE ? ESCAPE '\\')")
        escaped_query = normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped_query}%"
        values.extend([pattern, pattern])
    rows = connection.execute(
        f"SELECT * FROM evidence_records WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id ASC LIMIT ?",
        (*values, max_items),
    ).fetchall()
    return rows_to_models(rows, EvidenceRecord)


def select_candidate_evidence_records(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    candidate_id: str,
    attempt_ids: list[str],
) -> list[EvidenceRecord]:
    """Select only evidence explicitly owned by this candidate or one of its attempts."""
    values: list[object] = [workspace_id, candidate_id]
    ownership = ["candidate_id = ?"]
    if attempt_ids:
        ordered_attempt_ids = sorted(set(attempt_ids))
        placeholders = ", ".join("?" for _ in ordered_attempt_ids)
        ownership.append(f"attempt_id IN ({placeholders})")
        values.extend(ordered_attempt_ids)
    rows = connection.execute(
        f"""
        SELECT *
        FROM evidence_records
        WHERE workspace_id = ? AND ({' OR '.join(ownership)})
        ORDER BY id ASC
        """,
        tuple(values),
    ).fetchall()
    return rows_to_models(rows, EvidenceRecord)


def evidence_pack_line(record: EvidenceRecord) -> str:
    metrics = json.loads(record.metrics_json)
    if not isinstance(metrics, dict):
        raise ValueError("metrics_json must decode to an object")
    prefix = f"evidence:{record.kind} id={record.id[:8]} verdict={record.verdict}"
    suffix = f"src={record.report_artifact_id[:8]}"
    metric_parts = [_metric_part(key, metrics[key]) for key in _ordered_metric_keys(record.kind, metrics) if key in metrics]
    return _fit_line(prefix, metric_parts, suffix)


def _ordered_metric_keys(kind: str, metrics: dict[str, Any]) -> list[str]:
    known = list(_METRIC_ORDER.get(kind, ()))
    extras = sorted(key for key in metrics if key not in known)
    return known + extras


def _metric_part(key: str, value: Any) -> str:
    if isinstance(value, float):
        rendered = f"{value:.6f}"
    elif isinstance(value, list):
        rendered = "[" + ",".join(str(item) for item in value) + "]"
    elif value is None:
        rendered = "None"
    else:
        rendered = str(value)
    return f"{key}={rendered}"


def _fit_line(prefix: str, metric_parts: list[str], suffix: str) -> str:
    parts = [prefix]
    for part in metric_parts:
        candidate = " ".join([*parts, part, suffix])
        if len(candidate) <= _MAX_LINE_CHARS:
            parts.append(part)
        else:
            marker_candidate = " ".join([*parts, "...", suffix])
            if len(marker_candidate) <= _MAX_LINE_CHARS:
                parts.append("...")
            break
    line = " ".join([*parts, suffix])
    if len(line) > _MAX_LINE_CHARS:
        raise ValueError("evidence prefix exceeds pack line limit")
    return line
