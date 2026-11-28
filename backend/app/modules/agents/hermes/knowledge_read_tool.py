"""Strictly read-only JarvisOS Second Brain, literature, calculation, and database view tool."""
from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.core.database import open_sqlite_connection
from app.modules.agents.hermes.ip_boundary import (
    CLOUD_IP_MODE_NO_CLOUD,
    CLOUD_IP_MODE_SANITIZED,
    is_local_route,
    log_cloud_boundary_event,
    resolve_cloud_ip_mode,
    sanitize_record,
)
from app.modules.ai.agent_contracts import CapabilityGrantRef, StructuredToolCall
from app.modules.economics.service import get_workspace_economics_response
from app.modules.flowsheet.freshness import (
    FreshnessError,
    find_upstream_levers_from_connection,
    preview_change_impact_from_connection,
)
from app.modules.flowsheet.service import FlowsheetError

_ALLOWED_VIEWS = frozenset({
    "workspaces",
    "memory_records",
    "literature_sources",
    "literature_entries",
    "project_revisions",
    "calc_runs",
    "ai_jobs_recent",
    "table_counts",
    "change_impact",
    "upstream_levers",
    "economics_summary",
})

_VIEW_ALLOWED_KEYS = {
    "workspaces": {"view", "workspace_id", "limit"},
    "memory_records": {"view", "workspace_id", "limit", "status", "record_type", "query"},
    "literature_sources": {"view", "workspace_id", "limit", "query", "state"},
    "literature_entries": {"view", "workspace_id", "limit", "source_id", "status", "query"},
    "project_revisions": {"view", "workspace_id", "limit"},
    "calc_runs": {"view", "workspace_id", "limit", "calculator_id"},
    "ai_jobs_recent": {"view", "workspace_id", "limit"},
    "table_counts": {"view", "workspace_id"},
    "change_impact": {"view", "workspace_id", "node_ref"},
    "upstream_levers": {"view", "workspace_id", "node_ref"},
    "economics_summary": {"view", "workspace_id"},
}

_VIEW_LIMIT_BOUNDS = {
    "workspaces": (1, 100, 50),
    "memory_records": (1, 100, 30),
    "literature_sources": (1, 100, 30),
    "literature_entries": (1, 100, 30),
    "project_revisions": (1, 50, 20),
    "calc_runs": (1, 50, 20),
    "ai_jobs_recent": (1, 50, 20),
}


_FILE_URL_RE = re.compile(r"(?i)file://[^\s,;\"'<>(){}\[\]]+")
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w:/~])/(?:[^\s,;\"'<>(){}\[\]/]+/)+[^\s,;\"'<>(){}\[\]/]+/*"
)
_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s,;\"'<>(){}\[\]]+")
_KNOWN_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}|Bearer\s+[A-Za-z0-9_.-]+)"
)
_KV_SECRET_RE = re.compile(
    r'(?i)(["\']?)([a-z0-9_.-]{0,64}(?:password|passwd|secret|token|api_key|api-key|apikey)[a-z0-9_.-]*+)\1\s*([:=])\s*([^\s,;\"\'<>(){}\[\]]++|"[^"\r\n]+"|\'[^\'\r\n]+\')'
)
_TAIL_SECRET_RE = re.compile(
    r'(?i)(?:(?:password|passwd|secret|token|api_key|api-key|apikey)[\w.-]*["\']?\s*[:=]\s*(?:"[^"]*|\'[^\']*|\S*)|sk-[\w-]*|gh[pousr]_\w*|AIza[\w-]*|Bearer\s+\S*)$'
)


def _redact_kv_secret(match: re.Match[str]) -> str:
    quote = match.group(1)
    key = match.group(2)
    delim = match.group(3)
    if delim == "=":
        return f"{quote}{key}{quote}=[REDACTED_SECRET]"
    return f"{quote}{key}{quote}: [REDACTED_SECRET]"


def _scrub_string(val: str, home_str: str) -> str:
    val = _FILE_URL_RE.sub("[REDACTED_PATH]", val)
    if home_str and home_str in val:
        val = val.replace(home_str, "~")
    val = re.sub(r"/home/[a-zA-Z0-9._-]+", "~", val)
    val = _ABSOLUTE_PATH_RE.sub("[REDACTED_PATH]", val)
    val = _WINDOWS_PATH_RE.sub("[REDACTED_PATH]", val)
    val = _KNOWN_SECRET_RE.sub("[REDACTED_SECRET]", val)
    val = _KV_SECRET_RE.sub(_redact_kv_secret, val)
    return val


def _scrub_value(val: Any, home_str: str) -> Any:
    if isinstance(val, str):
        capped = _cap_str(val)
        text = _scrub_string(capped, home_str)
        if len(val) > 300 or len(capped) >= 300:
            text = _TAIL_SECRET_RE.sub("[REDACTED_SECRET]", text)
        return text
    if isinstance(val, list):
        return [_scrub_value(item, home_str) for item in val]
    if isinstance(val, dict):
        scrubbed: dict[str, Any] = {}
        for k, v in val.items():
            k_lower = str(k).lower()
            if any(secret_key in k_lower for secret_key in ("token", "api_key", "secret", "password", "credential")):
                continue
            scrubbed[str(k)] = _scrub_value(v, home_str)
        return scrubbed
    return val


def _cap_str(val: Any, max_len: int = 300) -> Any:
    if isinstance(val, str):
        return val[:max_len]
    return val


def _cap_strings_recursive(obj: Any, max_len: int = 300) -> Any:
    if isinstance(obj, str):
        return obj[:max_len]
    if isinstance(obj, list):
        return [_cap_strings_recursive(item, max_len) for item in obj]
    if isinstance(obj, dict):
        return {k: _cap_strings_recursive(v, max_len) for k, v in obj.items()}
    return obj


def _enforce_payload_bounds(payload: dict[str, Any], items_key: str | None = None) -> dict[str, Any]:
    def _is_bounded(p: dict[str, Any]) -> bool:
        return len(json.dumps(p, ensure_ascii=False)) <= 16_000 and len(json.dumps(p)) <= 16_000

    payload["truncated"] = bool(payload.get("truncated", False))
    if _is_bounded(payload):
        return payload

    payload["truncated"] = True
    if items_key and isinstance(payload.get(items_key), list):
        items = list(payload[items_key])
        while items and not _is_bounded(payload):
            items.pop()
            payload[items_key] = items
            payload["count"] = len(items)
            if items_key == "affected_nodes":
                payload["affected_count"] = len(items)
            elif items_key == "levers":
                payload["lever_count"] = len(items)

    if _is_bounded(payload):
        return payload

    payload = _cap_strings_recursive(payload, 300)
    payload["truncated"] = True
    if items_key and isinstance(payload.get(items_key), list):
        payload["count"] = len(payload[items_key])
        if items_key == "affected_nodes":
            payload["affected_count"] = len(payload[items_key])
        elif items_key == "levers":
            payload["lever_count"] = len(payload[items_key])

    if _is_bounded(payload):
        return payload

    minimal: dict[str, Any] = {
        "view": payload.get("view", ""),
        "count": 0,
        "truncated": True,
    }
    if "workspace_id" in payload:
        minimal["workspace_id"] = payload["workspace_id"]
    if items_key:
        minimal[items_key] = []
    return minimal


def _handle_workspaces(connection: Any, raw_args: dict[str, Any]) -> dict[str, Any]:
    min_limit, max_limit, default_limit = _VIEW_LIMIT_BOUNDS["workspaces"]
    limit = raw_args.get("limit", default_limit)
    requested_ws = raw_args.get("workspace_id")

    params: list[Any] = []
    where_clauses: list[str] = []
    if requested_ws is not None:
        where_clauses.append("id = ?")
        params.append(requested_ws)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    try:
        count_row = connection.execute(
            f"SELECT COUNT(*) FROM workspaces {where_sql}", params
        ).fetchone()
        total = int(count_row[0]) if count_row else 0
    except Exception:
        total = 0

    query_params = list(params)
    query_params.append(limit)
    try:
        rows = connection.execute(
            f"SELECT id, name, description, created_at, updated_at FROM workspaces {where_sql} "
            f"ORDER BY updated_at DESC, id ASC LIMIT ?",
            query_params,
        ).fetchall()
    except Exception:
        rows = []

    items = [
        {
            "id": _cap_str(row["id"]),
            "name": _cap_str(row["name"]),
            "description": _cap_str(row["description"]),
            "created_at": _cap_str(row["created_at"]),
            "updated_at": _cap_str(row["updated_at"]),
        }
        for row in rows
    ]
    payload = {
        "view": "workspaces",
        "count": len(items),
        "total": total,
        "truncated": False,
        "workspaces": items,
    }
    return _enforce_payload_bounds(payload, "workspaces")


def _handle_memory_records(
    connection: Any, workspace_id: str, raw_args: dict[str, Any]
) -> dict[str, Any]:
    min_limit, max_limit, default_limit = _VIEW_LIMIT_BOUNDS["memory_records"]
    limit = raw_args.get("limit", default_limit)
    status_filter = raw_args.get("status")
    record_type_filter = raw_args.get("record_type")
    query_filter = raw_args.get("query")

    assumption_sql = """
        SELECT id, 'assumption' AS record_type, statement AS title,
               COALESCE(notes, statement) AS summary, status, confidence,
               scope AS raw_tag, updated_at
        FROM assumptions
        WHERE workspace_id = ?
    """
    parameter_sql = """
        SELECT id, 'parameter' AS record_type, name AS title,
               CASE
                   WHEN value IS NOT NULL OR unit IS NOT NULL
                   THEN trim(COALESCE(symbol, '') || ' = ' || COALESCE(value, '') || ' ' || COALESCE(unit, ''))
                   ELSE COALESCE(notes, '')
               END AS summary,
               status, confidence, symbol AS raw_tag, updated_at
        FROM parameters
        WHERE workspace_id = ?
    """
    decision_sql = """
        SELECT id, 'decision' AS record_type, title,
               COALESCE(decision_text, rationale, '') AS summary,
               status, NULL AS confidence, NULL AS raw_tag, updated_at
        FROM decisions
        WHERE workspace_id = ?
    """

    queries: list[str] = []
    params: list[Any] = []

    if record_type_filter is None or record_type_filter.lower() == "assumption":
        queries.append(assumption_sql)
        params.append(workspace_id)
    if record_type_filter is None or record_type_filter.lower() == "parameter":
        queries.append(parameter_sql)
        params.append(workspace_id)
    if record_type_filter is None or record_type_filter.lower() == "decision":
        queries.append(decision_sql)
        params.append(workspace_id)

    if not queries:
        return _enforce_payload_bounds({
            "view": "memory_records",
            "workspace_id": workspace_id,
            "count": 0,
            "total": 0,
            "truncated": False,
            "memory_records": [],
        }, "memory_records")

    union_sql = " UNION ALL ".join(queries)
    outer_where: list[str] = []
    outer_params: list[Any] = []

    if status_filter is not None:
        outer_where.append("LOWER(status) = LOWER(?)")
        outer_params.append(status_filter)
    if query_filter is not None:
        outer_where.append("(LOWER(title) LIKE ? OR LOWER(summary) LIKE ?)")
        like_pattern = f"%{query_filter.lower()}%"
        outer_params.extend([like_pattern, like_pattern])

    where_clause = f"WHERE {' AND '.join(outer_where)}" if outer_where else ""

    all_params = list(params) + list(outer_params)
    try:
        count_row = connection.execute(
            f"SELECT COUNT(*) FROM ({union_sql}) {where_clause}", all_params
        ).fetchone()
        total = int(count_row[0]) if count_row else 0
    except Exception:
        total = 0

    all_params.append(limit)
    try:
        rows = connection.execute(
            f"SELECT id, record_type, title, summary, status, confidence, raw_tag, updated_at "
            f"FROM ({union_sql}) {where_clause} ORDER BY updated_at DESC, id ASC LIMIT ?",
            all_params,
        ).fetchall()
    except Exception:
        rows = []

    items = []
    for r in rows:
        tag_val = r["raw_tag"]
        tags = [_cap_str(str(tag_val))] if tag_val is not None and str(tag_val).strip() else []
        items.append({
            "id": _cap_str(r["id"]),
            "record_type": _cap_str(r["record_type"]),
            "title": _cap_str(r["title"]),
            "summary": _cap_str(r["summary"]),
            "status": _cap_str(r["status"]),
            "confidence": r["confidence"],
            "tags": tags,
            "updated_at": _cap_str(r["updated_at"]),
        })

    payload = {
        "view": "memory_records",
        "workspace_id": workspace_id,
        "count": len(items),
        "total": total,
        "truncated": False,
        "memory_records": items,
    }
    return _enforce_payload_bounds(payload, "memory_records")


def _parse_dated_timestamp(val: Any) -> datetime | None:
    if not isinstance(val, str) or not val.strip():
        return None
    val_clean = val.strip()
    try:
        dt = datetime.fromisoformat(val_clean.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except Exception:
        pass
    if len(val_clean) >= 10:
        try:
            d = date.fromisoformat(val_clean[:10])
            return datetime(d.year, d.month, d.day, tzinfo=UTC)
        except Exception:
            pass
    return None


def _build_dated_research_summary(
    dates: list[str | None],
    years: list[int | None],
    validated_count: int,
) -> dict[str, Any]:
    count = max(0, int(validated_count))
    valid_dts = [dt for d in dates if (dt := _parse_dated_timestamp(d)) is not None]
    latest_validated_date: str | None = None
    if valid_dts:
        latest_validated_date = max(valid_dts).date().isoformat()

    valid_years = [
        y for y in years
        if isinstance(y, int) and not isinstance(y, bool)
    ]
    latest_published_year: int | None = max(valid_years) if valid_years else None

    if count > 0:
        if latest_validated_date is not None:
            date_ref = latest_validated_date
        elif latest_published_year is not None:
            date_ref = str(latest_published_year)
        else:
            date_ref = None

        if date_ref is not None:
            guidance = (
                f"Reuse validated stored literature/documents first (latest validated: {date_ref}), "
                f"cite this date, offer at the end to search for anything newer than {date_ref}, "
                "and search afresh only when stored coverage is incomplete or the user asks."
            )
        else:
            guidance = (
                "Reuse validated stored literature/documents first, cite their date, "
                "offer at the end to search for anything newer than that date, "
                "and search afresh only when stored coverage is incomplete or the user asks."
            )
    else:
        guidance = (
            "No validated stored literature sources matched; "
            "fresh search or a labelled engineering estimate is permitted."
        )

    return {
        "validated_count": count,
        "latest_validated_date": latest_validated_date,
        "latest_published_year": latest_published_year,
        "reuse_guidance": guidance,
    }


def _handle_literature_sources(
    connection: Any, workspace_id: str, raw_args: dict[str, Any]
) -> dict[str, Any]:
    min_limit, max_limit, default_limit = _VIEW_LIMIT_BOUNDS["literature_sources"]
    limit = raw_args.get("limit", default_limit)
    query_filter = raw_args.get("query")
    state_filter = raw_args.get("state")

    params: list[Any] = [workspace_id]
    where_clauses = ["s.workspace_id = ?"]
    if state_filter is not None:
        where_clauses.append("LOWER(s.state) = ?")
        params.append(state_filter.lower())
    if query_filter is not None:
        where_clauses.append(
            "(LOWER(s.title) LIKE ? OR LOWER(COALESCE(s.citation, '')) LIKE ? OR LOWER(COALESCE(s.publisher, '')) LIKE ?)"
        )
        like_p = f"%{query_filter.lower()}%"
        params.extend([like_p, like_p, like_p])

    where_sql = f"WHERE {' AND '.join(where_clauses)}"
    try:
        count_row = connection.execute(
            f"SELECT COUNT(*) FROM literature_sources s {where_sql}", params
        ).fetchone()
        total = int(count_row[0]) if count_row else 0
    except Exception:
        total = 0

    query_params = list(params)
    query_params.append(limit)
    sql = f"""
        SELECT s.id, s.title, s.citation, s.publisher, s.published_year, s.source_kind,
               s.artifact_id, s.state, s.created_at, s.updated_at,
               (SELECT COUNT(*) FROM literature_entries e WHERE e.source_id = s.id AND e.workspace_id = s.workspace_id) AS entry_count
        FROM literature_sources s
        {where_sql}
        ORDER BY s.updated_at DESC, s.id ASC
        LIMIT ?
    """
    try:
        rows = connection.execute(sql, query_params).fetchall()
    except Exception:
        rows = []

    items = []
    for r in rows:
        authors = r["citation"] if r["citation"] else r["publisher"]
        items.append({
            "id": _cap_str(r["id"]),
            "title": _cap_str(r["title"]),
            "authors": _cap_str(authors) if authors else None,
            "year": r["published_year"],
            "source_type": _cap_str(r["source_kind"]),
            "locator": _cap_str(r["artifact_id"]) if r["artifact_id"] else None,
            "backing_state": _cap_str(r["state"]),
            "entry_count": int(r["entry_count"]) if r["entry_count"] is not None else 0,
            "created_at": _cap_str(r["created_at"]) if r["created_at"] is not None else None,
            "updated_at": _cap_str(r["updated_at"]) if r["updated_at"] is not None else None,
            "validated": bool(r["state"] == "accepted"),
        })

    validated_sources = [s for s in items if s.get("validated")]
    dates: list[str | None] = [s.get("updated_at") or s.get("created_at") for s in validated_sources]
    years: list[int | None] = [s.get("year") for s in validated_sources]
    dated_summary = _build_dated_research_summary(dates, years, len(validated_sources))

    payload = {
        "view": "literature_sources",
        "workspace_id": workspace_id,
        "count": len(items),
        "total": total,
        "truncated": False,
        "dated_research_summary": dated_summary,
        "literature_sources": items,
    }
    return _enforce_payload_bounds(payload, "literature_sources")


def _handle_literature_entries(
    connection: Any, workspace_id: str, raw_args: dict[str, Any]
) -> dict[str, Any]:
    min_limit, max_limit, default_limit = _VIEW_LIMIT_BOUNDS["literature_entries"]
    limit = raw_args.get("limit", default_limit)
    source_id_filter = raw_args.get("source_id")
    status_filter = raw_args.get("status")
    query_filter = raw_args.get("query")

    params: list[Any] = [workspace_id]
    where_clauses = ["e.workspace_id = ?"]
    if source_id_filter is not None:
        where_clauses.append("e.source_id = ?")
        params.append(source_id_filter)
    if status_filter is not None:
        where_clauses.append("LOWER(e.status) = LOWER(?)")
        params.append(status_filter)
    if query_filter is not None:
        where_clauses.append(
            "(LOWER(COALESCE(e.statement, '')) LIKE ? OR LOWER(COALESCE(e.value_text, '')) LIKE ? OR LOWER(COALESCE(e.context_text, '')) LIKE ?)"
        )
        like_p = f"%{query_filter.lower()}%"
        params.extend([like_p, like_p, like_p])

    where_sql = f"WHERE {' AND '.join(where_clauses)}"
    try:
        count_row = connection.execute(
            f"SELECT COUNT(*) FROM literature_entries e {where_sql}", params
        ).fetchone()
        total = int(count_row[0]) if count_row else 0
    except Exception:
        total = 0

    query_params = list(params)
    query_params.append(limit)
    sql = f"""
        SELECT e.id, e.source_id, e.entry_kind, e.statement, e.value_text, e.value_number, e.unit,
               e.status, e.locator_kind, e.locator_start, e.locator_end, e.context_text, e.updated_at,
               s.title AS source_title, s.state AS source_state, s.updated_at AS source_updated_at,
               s.published_year AS source_published_year
        FROM literature_entries e
        LEFT JOIN literature_sources s ON s.id = e.source_id AND s.workspace_id = e.workspace_id
        {where_sql}
        ORDER BY e.updated_at DESC, e.id ASC
        LIMIT ?
    """
    try:
        rows = connection.execute(sql, query_params).fetchall()
    except Exception:
        rows = []

    items = []
    for r in rows:
        entry_type = r["entry_kind"]
        claim_text = r["statement"] if r["statement"] else r["context_text"]
        val = r["value_number"] if r["value_number"] is not None else r["value_text"]

        loc_kind = r["locator_kind"]
        loc_start = r["locator_start"]
        loc_end = r["locator_end"]
        if loc_kind and loc_start is not None and loc_end is not None:
            locator_str = f"{loc_kind}:{loc_start}-{loc_end}"
        elif loc_kind and loc_start is not None:
            locator_str = f"{loc_kind}:{loc_start}"
        elif loc_kind:
            locator_str = str(loc_kind)
        else:
            locator_str = None

        items.append({
            "id": _cap_str(r["id"]),
            "source_id": _cap_str(r["source_id"]),
            "entry_type": _cap_str(entry_type),
            "claim_text": _cap_str(claim_text) if claim_text else None,
            "value": _cap_str(str(val)) if isinstance(val, str) else val,
            "unit": _cap_str(r["unit"]) if r["unit"] else None,
            "status": _cap_str(r["status"]),
            "locator": _cap_str(locator_str) if locator_str else None,
            "updated_at": _cap_str(r["updated_at"]),
            "source_title": _cap_str(r["source_title"]) if r["source_title"] else None,
            "source_state": _cap_str(r["source_state"]) if r["source_state"] else None,
            "source_updated_at": _cap_str(r["source_updated_at"]) if r["source_updated_at"] else None,
            "source_published_year": r["source_published_year"],
        })

    validated_entries = [
        e for e in items
        if e.get("status") == "accepted" or e.get("source_state") == "accepted"
    ]
    dates = []
    years = []
    for e in validated_entries:
        years.append(e.get("source_published_year"))
        entry_dates: list[str | None] = []
        if e.get("status") == "accepted" and e.get("updated_at"):
            entry_dates.append(e.get("updated_at"))
        if e.get("source_state") == "accepted" and e.get("source_updated_at"):
            entry_dates.append(e.get("source_updated_at"))
        if not entry_dates:
            entry_dates.append(e.get("source_updated_at") or e.get("updated_at"))
        dates.extend(entry_dates)

    dated_summary = _build_dated_research_summary(dates, years, len(validated_entries))

    payload = {
        "view": "literature_entries",
        "workspace_id": workspace_id,
        "count": len(items),
        "total": total,
        "truncated": False,
        "dated_research_summary": dated_summary,
        "literature_entries": items,
    }
    return _enforce_payload_bounds(payload, "literature_entries")


def _handle_project_revisions(
    connection: Any, workspace_id: str, raw_args: dict[str, Any]
) -> dict[str, Any]:
    min_limit, max_limit, default_limit = _VIEW_LIMIT_BOUNDS["project_revisions"]
    limit = raw_args.get("limit", default_limit)

    params: list[Any] = [workspace_id]
    try:
        count_row = connection.execute(
            "SELECT COUNT(*) FROM project_knowledge_revisions WHERE workspace_id = ?", params
        ).fetchone()
        total = int(count_row[0]) if count_row else 0
    except Exception:
        total = 0

    query_params = [workspace_id, limit]
    sql = """
        SELECT id, state, change_set_json, origin, created_at, accepted_at
        FROM project_knowledge_revisions
        WHERE workspace_id = ?
        ORDER BY created_at DESC, id ASC
        LIMIT ?
    """
    try:
        rows = connection.execute(sql, query_params).fetchall()
    except Exception:
        rows = []

    items = []
    for r in rows:
        rev_id = str(r["id"])
        raw_ops = r["change_set_json"]
        summary = ""
        if raw_ops:
            try:
                ops = json.loads(raw_ops)
                if isinstance(ops, list):
                    op_kinds = [str(item.get("op", item.get("kind", ""))) for item in ops if isinstance(item, dict)]
                    distinct_ops = sorted(set(filter(None, op_kinds)))
                    summary = f"{len(ops)} operations ({', '.join(distinct_ops[:3])})" if distinct_ops else f"{len(ops)} operations"
            except Exception:
                summary = "Revision change set"
        if not summary:
            summary = str(r["origin"] or "Project revision")

        # Revalidation status summary
        reval_summary: dict[str, Any] = {
            "status": "none",
            "pass": 0,
            "fail": 0,
            "total": 0,
        }
        try:
            val_rows = connection.execute(
                "SELECT outcome, COUNT(*) as cnt FROM project_knowledge_validation "
                "WHERE workspace_id = ? AND working_revision_id = ? GROUP BY outcome",
                (workspace_id, rev_id),
            ).fetchall()
            if val_rows:
                outcomes: dict[str, int] = {str(vr["outcome"]): int(vr["cnt"]) for vr in val_rows}
                pass_cnt = outcomes.get("pass", 0)
                fail_cnt = outcomes.get("fail", 0)
                tot = sum(outcomes.values())
                if fail_cnt > 0:
                    status_str = "fail"
                elif pass_cnt == tot and tot > 0:
                    status_str = "pass"
                elif tot > 0:
                    status_str = "mixed"
                else:
                    status_str = "none"
                reval_summary = {
                    "status": status_str,
                    "pass": pass_cnt,
                    "fail": fail_cnt,
                    "total": tot,
                    "outcomes": outcomes,
                }
        except Exception:
            pass

        items.append({
            "id": _cap_str(rev_id),
            "status": _cap_str(r["state"]),
            "summary": _cap_str(summary),
            "created_at": _cap_str(r["created_at"]),
            "revalidation_summary": reval_summary,
        })

    payload = {
        "view": "project_revisions",
        "workspace_id": workspace_id,
        "count": len(items),
        "total": total,
        "truncated": False,
        "project_revisions": items,
    }
    return _enforce_payload_bounds(payload, "project_revisions")


def _handle_calc_runs(
    connection: Any, workspace_id: str, raw_args: dict[str, Any]
) -> dict[str, Any]:
    min_limit, max_limit, default_limit = _VIEW_LIMIT_BOUNDS["calc_runs"]
    limit = raw_args.get("limit", default_limit)
    calculator_id_filter = raw_args.get("calculator_id")

    params: list[Any] = [workspace_id]
    where_clauses = ["r.workspace_id = ?"]
    if calculator_id_filter is not None:
        where_clauses.append("(r.model_version_id = ? OR r.run_label = ?)")
        params.extend([calculator_id_filter, calculator_id_filter])

    where_sql = f"WHERE {' AND '.join(where_clauses)}"
    try:
        count_row = connection.execute(
            f"SELECT COUNT(*) FROM simulation_runs r {where_sql}", params
        ).fetchone()
        total = int(count_row[0]) if count_row else 0
    except Exception:
        total = 0

    query_params = list(params)
    query_params.append(limit)
    sql = f"""
        SELECT r.id, r.model_version_id, r.run_label, r.status, r.created_at,
               r.input_payload, r.output_payload
        FROM simulation_runs r
        {where_sql}
        ORDER BY r.created_at DESC, r.id ASC
        LIMIT ?
    """
    try:
        rows = connection.execute(sql, query_params).fetchall()
    except Exception:
        rows = []

    items = []
    for r in rows:
        cid = r["model_version_id"] or r["run_label"] or ""
        in_raw = r["input_payload"]
        out_raw = r["output_payload"]

        in_sum = ""
        if in_raw:
            try:
                in_obj = json.loads(in_raw)
                if isinstance(in_obj, dict):
                    in_sum = f"inputs: {', '.join(sorted(in_obj.keys())[:8])}"
                else:
                    in_sum = str(in_raw)[:300]
            except Exception:
                in_sum = str(in_raw)[:300]

        out_sum = ""
        if out_raw:
            try:
                out_obj = json.loads(out_raw)
                if isinstance(out_obj, dict):
                    out_keys = sorted(out_obj.keys())
                    out_sum = f"outputs: {', '.join(out_keys[:8])}"
                else:
                    out_sum = str(out_raw)[:300]
            except Exception:
                out_sum = str(out_raw)[:300]

        items.append({
            "id": _cap_str(r["id"]),
            "calculator_id": _cap_str(cid),
            "status": _cap_str(r["status"]),
            "created_at": _cap_str(r["created_at"]),
            "inputs_summary": _cap_str(in_sum),
            "outputs_summary": _cap_str(out_sum),
        })

    payload = {
        "view": "calc_runs",
        "workspace_id": workspace_id,
        "count": len(items),
        "total": total,
        "truncated": False,
        "calc_runs": items,
    }
    return _enforce_payload_bounds(payload, "calc_runs")


def _handle_ai_jobs_recent(
    connection: Any, workspace_id: str, raw_args: dict[str, Any]
) -> dict[str, Any]:
    min_limit, max_limit, default_limit = _VIEW_LIMIT_BOUNDS["ai_jobs_recent"]
    limit = raw_args.get("limit", default_limit)

    # Check if workspace_id column exists directly on ai_jobs table
    try:
        ai_jobs_cols = {c[1] for c in connection.execute("PRAGMA table_info(ai_jobs)").fetchall()}
    except Exception:
        ai_jobs_cols = set()

    has_ws_col = "workspace_id" in ai_jobs_cols

    if has_ws_col:
        where_sql = "WHERE (j.workspace_id = ? OR f.workspace_id = ?)"
        count_params: list[Any] = [workspace_id, workspace_id]
        query_params: list[Any] = [workspace_id, workspace_id, limit]
    else:
        where_sql = "WHERE f.workspace_id = ?"
        count_params = [workspace_id]
        query_params = [workspace_id, limit]

    try:
        count_row = connection.execute(
            f"SELECT COUNT(*) FROM ai_jobs j LEFT JOIN ai_flows f ON f.id = j.flow_id {where_sql}",
            count_params,
        ).fetchone()
        total = int(count_row[0]) if count_row else 0
    except Exception:
        total = 0

    sql = f"""
        SELECT j.id, j.task_kind,
               COALESCE(j.selected_route_class, j.requested_route_class, '') AS route_class,
               COALESCE(j.provider_id, '') AS provider_id,
               COALESCE(j.model_id, '') AS model,
               j.status, j.created_at, f.completed_at,
               j.error_type AS error_code
        FROM ai_jobs j
        LEFT JOIN ai_flows f ON f.id = j.flow_id
        {where_sql}
        ORDER BY j.created_at DESC, j.id ASC
        LIMIT ?
    """
    try:
        rows = connection.execute(sql, query_params).fetchall()
    except Exception:
        rows = []

    items = [
        {
            "id": _cap_str(r["id"]),
            "task_type": _cap_str(r["task_kind"]),
            "route_class": _cap_str(r["route_class"]),
            "provider_id": _cap_str(r["provider_id"]),
            "model": _cap_str(r["model"]),
            "status": _cap_str(r["status"]),
            "created_at": _cap_str(r["created_at"]),
            "completed_at": _cap_str(r["completed_at"]) if r["completed_at"] else None,
            "error_code": _cap_str(r["error_code"]) if r["error_code"] else None,
        }
        for r in rows
    ]
    payload = {
        "view": "ai_jobs_recent",
        "workspace_id": workspace_id,
        "count": len(items),
        "total": total,
        "truncated": False,
        "ai_jobs": items,
    }
    return _enforce_payload_bounds(payload, "ai_jobs")


def _handle_table_counts(connection: Any, workspace_id: str) -> dict[str, Any]:
    counts: dict[str, int] = {
        "workspaces": 0,
        "memory_records": 0,
        "literature_sources": 0,
        "literature_entries": 0,
        "ai_threads": 0,
        "ai_jobs": 0,
        "calc_runs": 0,
        "artifacts": 0,
        "project_revisions": 0,
        "project_validations": 0,
    }

    # 1. workspaces
    try:
        row = connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()
        counts["workspaces"] = int(row[0]) if row else 0
    except Exception:
        pass

    # 2. memory_records
    try:
        row = connection.execute(
            """
            SELECT (
                (SELECT COUNT(*) FROM assumptions WHERE workspace_id = ?) +
                (SELECT COUNT(*) FROM parameters WHERE workspace_id = ?) +
                (SELECT COUNT(*) FROM decisions WHERE workspace_id = ?)
            )
            """,
            (workspace_id, workspace_id, workspace_id),
        ).fetchone()
        counts["memory_records"] = int(row[0]) if row else 0
    except Exception:
        pass

    # 3. literature_sources
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM literature_sources WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        counts["literature_sources"] = int(row[0]) if row else 0
    except Exception:
        pass

    # 4. literature_entries
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM literature_entries WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        counts["literature_entries"] = int(row[0]) if row else 0
    except Exception:
        pass

    # 5. ai_threads
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM ai_threads WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        counts["ai_threads"] = int(row[0]) if row else 0
    except Exception:
        pass

    # 6. ai_jobs
    try:
        ai_jobs_cols = {c[1] for c in connection.execute("PRAGMA table_info(ai_jobs)").fetchall()}
        if "workspace_id" in ai_jobs_cols:
            row = connection.execute(
                "SELECT COUNT(*) FROM ai_jobs j LEFT JOIN ai_flows f ON f.id = j.flow_id "
                "WHERE j.workspace_id = ? OR f.workspace_id = ?",
                (workspace_id, workspace_id),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*) FROM ai_jobs j JOIN ai_flows f ON f.id = j.flow_id "
                "WHERE f.workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        counts["ai_jobs"] = int(row[0]) if row else 0
    except Exception:
        pass

    # 7. calc_runs
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM simulation_runs WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        counts["calc_runs"] = int(row[0]) if row else 0
    except Exception:
        pass

    # 8. artifacts
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        counts["artifacts"] = int(row[0]) if row else 0
    except Exception:
        pass

    # 9. project_revisions
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM project_knowledge_revisions WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        counts["project_revisions"] = int(row[0]) if row else 0
    except Exception:
        pass

    # 10. project_validations
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM project_knowledge_validation WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        counts["project_validations"] = int(row[0]) if row else 0
    except Exception:
        pass

    payload = {
        "view": "table_counts",
        "workspace_id": workspace_id,
        "counts": counts,
        "truncated": False,
    }
    return _enforce_payload_bounds(payload)


def dispatch_knowledge_read_tool(
    call: StructuredToolCall,
    workspace_id: str,
    default_error: str,
    *,
    grant: CapabilityGrantRef | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Execute a strictly read-only knowledge SQL read operation for Hermes."""
    if call.capability_id != "jarvis.knowledge_read":
        return None, "capability_denied"

    raw_args = dict(call.arguments)
    raw_args.pop("tool_name", None)
    raw_args.pop("grant_id", None)

    view = raw_args.get("view")
    if not isinstance(view, str) or view not in _ALLOWED_VIEWS:
        return None, "unknown_view"

    allowed_keys = _VIEW_ALLOWED_KEYS[view]
    if set(raw_args.keys()) - allowed_keys:
        return None, "invalid_arguments"

    # Workspace scoping checks
    target_workspace = workspace_id
    requested_workspace = raw_args.get("workspace_id")
    if requested_workspace is not None:
        if not isinstance(requested_workspace, str) or not requested_workspace.strip():
            return None, "invalid_arguments"
        if view != "workspaces" and requested_workspace != workspace_id:
            return None, "cross_workspace_denied"
        target_workspace = requested_workspace

    # Bounds validation for limit
    if "limit" in raw_args:
        limit = raw_args["limit"]
        if isinstance(limit, bool) or not isinstance(limit, int):
            return None, "invalid_arguments"
        min_limit, max_limit, _ = _VIEW_LIMIT_BOUNDS[view]
        if not (min_limit <= limit <= max_limit):
            return None, "invalid_arguments"

    # Type validation for optional string filters
    for str_key in ("status", "record_type", "query", "source_id", "calculator_id", "state"):
        if str_key in raw_args:
            val = raw_args[str_key]
            if val is not None and not isinstance(val, str):
                return None, "invalid_arguments"

    if "state" in raw_args:
        state_val = raw_args["state"]
        if state_val is not None:
            if not isinstance(state_val, str) or state_val.lower() not in {"raw", "review", "accepted"}:
                return None, "invalid_arguments"

    node_ref: str | None = None
    if view in {"change_impact", "upstream_levers"}:
        node_ref_arg = raw_args.get("node_ref")
        if not isinstance(node_ref_arg, str) or not node_ref_arg.strip():
            return None, "invalid_arguments"
        node_ref = node_ref_arg.strip()

    home_str = str(Path.home())

    with open_sqlite_connection(read_only=True) as connection:
        connection.execute("PRAGMA query_only = ON")

        if view == "workspaces":
            result = _handle_workspaces(connection, raw_args)
        elif view == "memory_records":
            result = _handle_memory_records(connection, target_workspace, raw_args)
        elif view == "literature_sources":
            result = _handle_literature_sources(connection, target_workspace, raw_args)
        elif view == "literature_entries":
            result = _handle_literature_entries(connection, target_workspace, raw_args)
        elif view == "project_revisions":
            result = _handle_project_revisions(connection, target_workspace, raw_args)
        elif view == "calc_runs":
            result = _handle_calc_runs(connection, target_workspace, raw_args)
        elif view == "ai_jobs_recent":
            result = _handle_ai_jobs_recent(connection, target_workspace, raw_args)
        elif view == "table_counts":
            result = _handle_table_counts(connection, target_workspace)
        elif view == "change_impact":
            assert node_ref is not None
            try:
                impact = preview_change_impact_from_connection(connection, target_workspace, node_ref)
                result = impact.model_dump(mode="json")
            except FlowsheetError as exc:
                if exc.code in {"flowsheet_node_not_found", "flowsheet_workspace_not_found"}:
                    return None, "node_not_found"
                if exc.code == "flowsheet_ref_invalid":
                    return None, "invalid_arguments"
                return None, default_error
            except FreshnessError:
                return None, default_error
        elif view == "upstream_levers":
            assert node_ref is not None
            try:
                levers = find_upstream_levers_from_connection(connection, target_workspace, node_ref)
                result = levers.model_dump(mode="json")
            except FlowsheetError as exc:
                if exc.code in {"flowsheet_node_not_found", "flowsheet_workspace_not_found"}:
                    return None, "node_not_found"
                if exc.code == "flowsheet_ref_invalid":
                    return None, "invalid_arguments"
                return None, default_error
            except FreshnessError:
                return None, default_error
        elif view == "economics_summary":
            try:
                econ_resp = get_workspace_economics_response(connection, target_workspace)
            except HTTPException as exc:
                if exc.status_code == 404:
                    return None, "not_found"
                return None, default_error
            result = econ_resp.evaluation.model_dump(mode="json")
            result["view"] = "economics_summary"
            result["workspace_id"] = target_workspace
            result["updated_at"] = econ_resp.updated_at
            result["truncated"] = False
        else:
            return None, "unknown_view"

    if view in {"table_counts", "economics_summary"}:
        items_key = None
    elif view == "ai_jobs_recent":
        items_key = "ai_jobs"
    elif view == "change_impact":
        items_key = "affected_nodes"
    elif view == "upstream_levers":
        items_key = "levers"
    else:
        items_key = view
    raw_items = result.get(items_key, []) if items_key and isinstance(result, dict) else []
    scrubbed = _scrub_value(result, home_str)

    is_local = is_local_route(grant=grant)
    if not is_local and isinstance(scrubbed, dict):
        mode = resolve_cloud_ip_mode()
        if items_key and isinstance(scrubbed.get(items_key), list):
            kept_items = []
            withheld_count = 0
            for raw_item, item in zip(raw_items, scrubbed[items_key], strict=True):
                if mode == CLOUD_IP_MODE_NO_CLOUD:
                    raw_allowed, _, _ = sanitize_record(raw_item, mode)
                    if not raw_allowed:
                        withheld_count += 1
                        continue
                allowed, out_item, _ = sanitize_record(item, mode)
                if allowed:
                    kept_items.append(out_item)
                else:
                    withheld_count += 1
            scrubbed[items_key] = kept_items
            scrubbed["count"] = len(kept_items)
            if items_key == "affected_nodes":
                scrubbed["affected_count"] = len(kept_items)
            elif items_key == "levers":
                scrubbed["lever_count"] = len(kept_items)
            scrubbed["withheld_count"] = withheld_count
            scrubbed["egress_mode"] = mode
            if mode == CLOUD_IP_MODE_SANITIZED:
                scrubbed["sanitized"] = True
            if withheld_count > 0:
                reason_code = ("egress_pack_blocked" if mode == CLOUD_IP_MODE_NO_CLOUD
                               else "egress_sanitization_required")
                log_cloud_boundary_event(
                    tool_name="jarvis_knowledge_read",
                    op=view,
                    egress_mode=mode,
                    withheld_count=withheld_count,
                    reason_code=reason_code,
                    workspace_id=target_workspace,
                )
        elif view == "economics_summary":
            econ_list_keys = (
                "capex_breakdown",
                "opex_breakdown",
                "capex_hotspots",
                "opex_hotspots",
                "opex_subcategory_hotspots",
                "annualized_hotspots",
                "missing_items",
            )
            withheld_count = 0
            for list_key in econ_list_keys:
                if not isinstance(scrubbed.get(list_key), list):
                    continue
                raw_list = result.get(list_key, []) if isinstance(result, dict) else []
                kept_list = []
                for raw_item, item in zip(raw_list, scrubbed[list_key], strict=True):
                    if not isinstance(item, dict):
                        kept_list.append(item)
                        continue
                    if mode == CLOUD_IP_MODE_NO_CLOUD and isinstance(raw_item, dict):
                        raw_allowed, _, _ = sanitize_record(raw_item, mode)
                        if not raw_allowed:
                            withheld_count += 1
                            continue
                    allowed, out_item, _ = sanitize_record(item, mode)
                    if allowed:
                        kept_list.append(out_item)
                    else:
                        withheld_count += 1
                scrubbed[list_key] = kept_list
            for text_key in ("headline", "hotspot_summary"):
                raw_text = result.get(text_key) if isinstance(result, dict) else None
                cur_text = scrubbed.get(text_key)
                if isinstance(cur_text, str):
                    if mode == CLOUD_IP_MODE_NO_CLOUD and isinstance(raw_text, str):
                        raw_ok, _, _ = sanitize_record({"text": raw_text}, mode)
                        if not raw_ok:
                            scrubbed[text_key] = "[REDACTED]"
                            withheld_count += 1
                            continue
                    ok, out_rec, _ = sanitize_record({"text": cur_text}, mode)
                    if ok:
                        scrubbed[text_key] = out_rec.get("text", cur_text)
                    else:
                        scrubbed[text_key] = "[REDACTED]"
                        withheld_count += 1
            scrubbed["withheld_count"] = withheld_count
            scrubbed["egress_mode"] = mode
            if mode == CLOUD_IP_MODE_SANITIZED:
                scrubbed["sanitized"] = True
            if withheld_count > 0:
                reason_code = (
                    "egress_pack_blocked"
                    if mode == CLOUD_IP_MODE_NO_CLOUD
                    else "egress_sanitization_required"
                )
                log_cloud_boundary_event(
                    tool_name="jarvis_knowledge_read",
                    op=view,
                    egress_mode=mode,
                    withheld_count=withheld_count,
                    reason_code=reason_code,
                    workspace_id=target_workspace,
                )
        else:
            scrubbed["withheld_count"] = 0
            scrubbed["egress_mode"] = mode
            if mode == CLOUD_IP_MODE_SANITIZED:
                scrubbed["sanitized"] = True

    if isinstance(scrubbed, dict):
        scrubbed = _enforce_payload_bounds(scrubbed, items_key)
    return scrubbed, default_error
