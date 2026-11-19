"""Read-only process-run value resolution for case readiness and verdicts."""

from __future__ import annotations

import math
import re
from typing import Any

from app.modules.process_stack import draft, results_view

DRAFT_ID_PATTERN = re.compile(r"[a-f0-9-]{36}\Z")
RUN_ID_PATTERN = re.compile(r"[a-f0-9]{32}\Z")


def parse_process_reference(reference: str) -> tuple[str, str] | None:
    if not isinstance(reference, str):
        return None
    match = re.fullmatch(r"process:([^:]+):([^:]+)", reference)
    if match is None:
        return None
    draft_id, run_id = match.groups()
    if DRAFT_ID_PATTERN.fullmatch(draft_id) is None or RUN_ID_PATTERN.fullmatch(run_id) is None:
        return None
    return draft_id, run_id


def _usable_run(run: dict[str, Any]) -> bool:
    outcome = run.get("outcome")
    return (
        run.get("action") == "run"
        and run.get("status") == "completed"
        and isinstance(outcome, dict)
        and outcome.get("state") == "converged"
    )


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _quantity_from_results(results: dict[str, Any], quantity: str) -> tuple[float, str] | None:
    for kpi in results.get("kpis", []) if isinstance(results.get("kpis"), list) else []:
        if not isinstance(kpi, dict):
            continue
        if kpi.get("id") == quantity and kpi.get("status") == "available":
            value = _finite_number(kpi.get("value"))
            unit = kpi.get("unit")
            if value is not None and isinstance(unit, str) and unit.strip():
                return value, unit.strip()

    if "." in quantity:
        tag, key = quantity.split(".", 1)
        units = results.get("units")
        if isinstance(units, dict):
            unit_result = units.get(tag)
            if isinstance(unit_result, dict):
                quantities = unit_result.get("quantities")
                if isinstance(quantities, dict):
                    item = quantities.get(key)
                    if isinstance(item, dict):
                        value = _finite_number(item.get("value"))
                        unit = item.get("unit")
                        if value is not None and isinstance(unit, str) and unit.strip():
                            return value, unit.strip()

        streams = results.get("streams")
        if isinstance(streams, dict):
            stream_result = streams.get(tag)
            if isinstance(stream_result, dict):
                item = stream_result.get(key)
                if isinstance(item, dict):
                    value = _finite_number(item.get("value"))
                    unit = item.get("unit")
                    if value is not None and isinstance(unit, str) and unit.strip():
                        return value, unit.strip()
    return None


def _process_results(workspace_id: str, draft_id: str, run: dict[str, Any]) -> dict[str, Any] | None:
    try:
        directory = draft.draft_dir(workspace_id, draft_id)
        revision = run.get("draft_revision")
        if not isinstance(revision, str):
            return None
        document = draft.load_revision(directory, revision)["document"]
        run_for_view = dict(run)
        run_for_view["outcome"] = draft.run_outcome(run_for_view, document)
        results = results_view.build(run_for_view, document)
    except (draft.DraftError, OSError, ValueError, KeyError):
        return None
    return results if isinstance(results, dict) else None


def list_process_run_values(
    workspace_id: str, quantity: str
) -> list[tuple[str, str, float, str, str]]:
    """List usable process runs carrying quantity, newest first by started_at."""
    values: list[tuple[str, str, float, str, str]] = []
    try:
        drafts = draft.list_drafts(workspace_id)[:20]
    except (draft.DraftError, OSError, ValueError, KeyError):
        return values

    for draft_row in drafts:
        if not isinstance(draft_row, dict):
            continue
        draft_id = draft_row.get("draft_id")
        draft_name = draft_row.get("name")
        if not isinstance(draft_id, str) or DRAFT_ID_PATTERN.fullmatch(draft_id) is None:
            continue
        try:
            directory = draft.draft_dir(workspace_id, draft_id)
            runs = draft.list_runs(directory)
        except (draft.DraftError, OSError, ValueError, KeyError):
            continue
        for run in runs:
            if not isinstance(run, dict) or not _usable_run(run):
                continue
            run_id = run.get("run_id")
            if not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None:
                continue
            results = _process_results(workspace_id, draft_id, run)
            if results is None:
                continue
            resolved = _quantity_from_results(results, quantity)
            if resolved is None:
                continue
            value, unit = resolved
            label = f'Diagramma "{draft_name}"' if isinstance(draft_name, str) and draft_name else "Diagramma"
            started_at = run.get("started_at")
            values.append((str(started_at or ""), f"process:{draft_id}:{run_id}", label, value, unit))
    values.sort(key=lambda row: row[0], reverse=True)
    return values


def has_usable_process_run(workspace_id: str) -> bool:
    """Return whether the workspace has any converged process run in its newest drafts."""
    try:
        drafts = draft.list_drafts(workspace_id)[:20]
    except (draft.DraftError, OSError, ValueError, KeyError):
        return False
    for draft_row in drafts:
        if not isinstance(draft_row, dict):
            continue
        draft_id = draft_row.get("draft_id")
        if not isinstance(draft_id, str) or DRAFT_ID_PATTERN.fullmatch(draft_id) is None:
            continue
        try:
            runs = draft.list_runs(draft.draft_dir(workspace_id, draft_id))
        except (draft.DraftError, OSError, ValueError, KeyError):
            continue
        if any(isinstance(run, dict) and _usable_run(run) for run in runs):
            return True
    return False


def resolve_process_run_reference(
    workspace_id: str, reference: str, quantity: str
) -> tuple[str, tuple[float, str] | None]:
    """Return an existing verdict code and, on success, the resolved (value, unit)."""
    parsed = parse_process_reference(reference)
    if parsed is None:
        return "run_not_found", None
    draft_id, run_id = parsed
    try:
        run = draft.get_run(workspace_id, draft_id, run_id)
    except (draft.DraftError, OSError, ValueError, KeyError):
        return "run_not_found", None
    if not _usable_run(run):
        return "run_not_usable", None
    results = _process_results(workspace_id, draft_id, run)
    if results is None:
        return "run_not_usable", None
    resolved = _quantity_from_results(results, quantity)
    if resolved is None:
        return "run_lacks_quantity", None
    return "ok", resolved
