"""Background Process draft solves with a live, durable iteration log (spec 210).

A background solve is an ordinary draft ``run`` (same ``run.json``, same results view) performed off the
request thread. While it runs, every tear-controller history row is appended to ``events.jsonl`` in the
run directory at the moment the controller produces it, stamped with its emission time; readers follow it
by cursor. ``solve.json`` holds the job state. A stop is cooperative: the controller sees it at the next
iteration boundary, so no DWSIM build or Studio sandbox evaluation is interrupted. Nothing here replays a
finished history as if it were live: ``live`` is true only while this process is running the solve.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.paths import build_paths
from app.modules.process_stack import draft, mixed, tear_solver

MAX_ACTIVE_GLOBAL = 2
MAX_EVENTS = 2000
MAX_EVENT_BYTES = 4 * 1024 * 1024
MAX_EVENTS_PER_READ = 500
_RUN_RE = re.compile(r"[a-f0-9]{32}")
_lock = threading.RLock()
_executor = ThreadPoolExecutor(max_workers=MAX_ACTIVE_GLOBAL, thread_name_prefix="process-solve")
_active: dict[str, threading.Event] = {}  # run_id -> stop request, only for solves this process is running
_STREAM_FIELDS = (("temperature_K", "temperature", "degC"), ("pressure_Pa", "pressure", "bar"),
                  ("mass_flow_kg_s", "mass_flow", "kg/h"))


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def _run_dir(workspace_id: str, draft_id: str, run_id: str) -> Path:
    directory = draft.draft_dir(workspace_id, draft_id)
    if not _RUN_RE.fullmatch(run_id):
        raise draft.DraftError("solve_not_found", "Solve was not found", 404)
    return draft.runs_dir(directory) / run_id


def _read_solve(path: Path) -> dict[str, Any]:
    try:
        value = json.loads((path / "solve.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise draft.DraftError("solve_not_found", "Solve was not found", 404) from exc
    if not isinstance(value, dict):
        raise draft.DraftError("solve_not_found", "Solve was not found", 404)
    return value


def _settled(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """A record claiming to run that this process does not run was cut off by a restart: say so, invent nothing."""
    with _lock:
        if record.get("state") in {"running", "stopping"} and record["run_id"] not in _active:
            # The worker can finish between the caller's read and this lock.
            # Read the authoritative file again before declaring its result lost.
            record = _read_solve(path)
            if record.get("state") in {"running", "stopping"} and record["run_id"] not in _active:
                record = {**record, "state": "interrupted", "finished_at": _now(),
                          "message": "The backend stopped while this solve was running; no result was recorded."}
                _write(path / "solve.json", record)
    return record


def recover_interrupted() -> int:
    """Mark unfinished Process solves interrupted when the backend starts, without creating a result."""
    root = build_paths().workspaces_dir
    recovered = 0
    with _lock:
        for path in root.glob("*/process/drafts/*/runs/*/solve.json"):
            try:
                record = _read_solve(path.parent)
            except draft.DraftError:
                continue
            if record.get("state") not in {"running", "stopping"}:
                continue
            record.update(state="interrupted", finished_at=_now(),
                          message="The backend stopped while this solve was running; no result was recorded.")
            _write(path, record)
            recovered += 1
    return recovered


def _settings(document: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return tear_solver.parse_settings(document.get("solver")).record()
    except ValueError:
        return None


def _flat(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict) and value:
        rows: dict[str, Any] = {}
        for key in sorted(value):
            rows.update(_flat(value[key], f"{prefix}{key}."))
        return rows
    return {prefix.rstrip("."): value}


def settings_diff(a: dict[str, Any] | None, b: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Solver settings that differ, as dotted keys; a missing side is null."""
    left, right = _flat(a or {}), _flat(b or {})
    return [{"key": key, "a": left.get(key), "b": right.get(key)}
            for key in sorted(set(left) | set(right)) if left.get(key) != right.get(key)]


class _Observer:
    def __init__(self, path: Path, stop: threading.Event) -> None:
        self.path, self.stop, self.count, self.size, self.truncated = path / "events.jsonl", stop, 0, 0, False

    def stop_requested(self) -> bool:
        return self.stop.is_set()

    def iteration(self, row: dict[str, Any]) -> None:
        if self.count >= MAX_EVENTS or self.truncated:
            self.truncated = True
            return
        event = {"seq": self.count + 1, "emitted_at": _now(), **row}
        line = json.dumps(event, sort_keys=True, allow_nan=False) + "\n"
        if self.size + len(line) > MAX_EVENT_BYTES:
            self.truncated = True
            return
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        self.count += 1
        self.size += len(line)


def _perform(prepared: draft.PreparedRun, path: Path, stop: threading.Event) -> None:
    observer = _Observer(path, stop)
    run_id = prepared.run["run_id"]
    try:
        result = draft.perform(prepared, observer=observer)["run"]
        final = {"state": "finished", "status": result.get("status"),
                 "reason": (result.get("mixed_solve") or {}).get("reason")}
    except Exception as exc:  # noqa: BLE001 - an unexpected failure ends the job truthfully
        final = {"state": "failed", "status": "runtime_failed", "message": draft.plain_text(str(exc)) or type(exc).__name__}
    with _lock:
        record = _read_solve(path)
        record.update(final, finished_at=_now(), events=observer.count, events_truncated=observer.truncated)
        _write(path / "solve.json", record)
        _active.pop(run_id, None)


def start(workspace_id: str, draft_id: str, revision: str, retry_of: str | None = None) -> dict[str, Any]:
    directory = draft.draft_dir(workspace_id, draft_id)
    parent = draft.get_run(workspace_id, draft_id, retry_of) if retry_of else None
    if parent is not None and parent.get("action") != "run":
        raise draft.DraftError("retry_parent_invalid", "Only a Process run can be retried", 422)
    # Typed refusals (invalid draft, Studio preflight, DWSIM unavailable) happen before anything is recorded.
    prepared = draft.prepare(workspace_id, draft_id, revision, "run")
    document = prepared.record["document"]
    settings = _settings(document)
    with _lock:
        for record in list_solves(workspace_id, draft_id):
            if record["state"] in {"running", "stopping"}:
                raise draft.DraftError("solve_active", "A solve of this draft is already running", 409,
                                       run_id=record["run_id"])
        if len(_active) >= MAX_ACTIVE_GLOBAL:
            raise draft.DraftError("solve_capacity", "Too many Process solves are running; try again shortly", 429)
        run_id = prepared.run["run_id"]
        if parent is not None:
            parent_document = draft.load_revision(directory, parent["draft_revision"])["document"]
            prepared.run["retry_of"] = retry_of
            prepared.run["retry_settings_diff"] = settings_diff(
                (parent.get("mixed_solve") or {}).get("solver") or _settings(parent_document), settings)
        path = draft.runs_dir(directory) / run_id
        path.mkdir(parents=True, exist_ok=False)
        record = {"run_id": run_id, "draft_id": draft_id, "draft_revision": revision, "state": "running",
                  "started_at": prepared.run["started_at"], "iterations_observable": mixed.needs_mixed_solve(document),
                  "solver": settings, **({"retry_of": retry_of,
                                          "retry_settings_diff": prepared.run["retry_settings_diff"]}
                                         if parent is not None else {})}
        _write(path / "solve.json", record)
        (path / "events.jsonl").touch()
        stop = threading.Event()
        _active[run_id] = stop
        try:
            _executor.submit(_perform, prepared, path, stop)
        except RuntimeError:
            _active.pop(run_id, None)
            raise
    return status(workspace_id, draft_id, run_id)


def _events(path: Path, after: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with (path / "events.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    break  # a line still being written is read on the next poll
                event = json.loads(line)
                if event["seq"] > after:
                    rows.append(event)
                    if len(rows) >= MAX_EVENTS_PER_READ:
                        break
    except FileNotFoundError:
        return []
    return rows


def status(workspace_id: str, draft_id: str, run_id: str, after: int = 0) -> dict[str, Any]:
    path = _run_dir(workspace_id, draft_id, run_id)
    record = _settled(path, _read_solve(path))
    events = _events(path, max(0, int(after)))
    return {**record, "live": record["state"] in {"running", "stopping"},
            "events": events, "cursor": events[-1]["seq"] if events else max(0, int(after))}


def stop(workspace_id: str, draft_id: str, run_id: str) -> dict[str, Any]:
    path = _run_dir(workspace_id, draft_id, run_id)
    with _lock:
        record = _settled(path, _read_solve(path))
        if record["state"] == "running" and run_id in _active:
            _active[run_id].set()
            record.update(state="stopping", stop_requested_at=_now())
            _write(path / "solve.json", record)
            return {**record, "stop": "requested"}
    return {**record, "stop": "noop"}


def list_solves(workspace_id: str, draft_id: str, limit: int = 20) -> list[dict[str, Any]]:
    root = draft.runs_dir(draft.draft_dir(workspace_id, draft_id))
    rows = []
    for path in root.glob("*/solve.json") if root.exists() else []:
        try:
            rows.append(_settled(path.parent, _read_solve(path.parent)))
        except draft.DraftError:
            continue
    return sorted(rows, key=lambda row: row.get("started_at") or "", reverse=True)[:limit]


# ---------------------------------------------------------------- comparison


def _residual_series(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"iteration": row.get("iteration"), "max_normalized_residual": row.get("max_normalized_residual")}
            for row in (run.get("mixed_solve") or {}).get("history") or []]


def _finite(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _side(run: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    from app.modules.process_stack import results_view

    solve = run.get("mixed_solve") or {}
    history = _residual_series(run)
    outcome = results_view.outcome_of(run, document)
    return {"run_id": run["run_id"], "draft_revision": run.get("draft_revision"),
            "document_sha256": run.get("draft_document_sha256") or draft.content_digest(document),
            "started_at": run.get("started_at"),
            "status": run.get("status"), "outcome": outcome, "reason": solve.get("reason"),
            "method": solve.get("method"), "iterations": len(history),
            "final_max_normalized_residual": history[-1]["max_normalized_residual"] if history else None,
            "results_label": solve.get("results_label") or ("current" if outcome.get("state") == "converged"
                                                              else results_view.LAST_ITERATE_LABEL),
            "retry_of": run.get("retry_of"), "residuals": history}


def compare(workspace_id: str, draft_id: str, a: str, b: str) -> dict[str, Any]:
    """Server-side comparison of two runs of one draft; values are the runs' own, converted for display only."""
    directory = draft.draft_dir(workspace_id, draft_id)
    runs = [draft.get_run(workspace_id, draft_id, run_id) for run_id in (a, b)]
    if a == b or any(run.get("action") != "run" for run in runs):
        raise draft.DraftError("comparison_invalid", "Choose two different Process runs", 422)
    documents = [draft.load_revision(directory, run["draft_revision"])["document"] for run in runs]
    settings = [(run.get("mixed_solve") or {}).get("solver") or _settings(document)
                for run, document in zip(runs, documents, strict=True)]

    def process_digest(document: dict[str, Any]) -> str:
        value = copy.deepcopy(document)
        value.pop("solver", None)
        return draft.content_digest(value)

    process_digests = [process_digest(document) for document in documents]
    streams = []
    left, right = runs[0].get("streams") or {}, runs[1].get("streams") or {}
    for tag in sorted(set(left) & set(right)):
        fields = []
        for key, kind, unit in _STREAM_FIELDS:
            va, vb = _finite((left[tag] or {}).get(key)), _finite((right[tag] or {}).get(key))
            if va is None and vb is None:
                continue
            da = float(f"{draft.convert_si(va, kind, unit):.6g}") if va is not None else None
            db = float(f"{draft.convert_si(vb, kind, unit):.6g}") if vb is not None else None
            delta = float(f"{draft.convert_si(vb, kind, unit) - draft.convert_si(va, kind, unit):.6g}") \
                if va is not None and vb is not None else None
            if kind == "temperature" and va is not None and vb is not None:
                delta = float(f"{vb - va:.6g}")  # a temperature difference is the same in K and degC
            fields.append({"field": kind, "unit": unit, "a": da, "b": db, "delta": delta})
        if fields:
            streams.append({"stream": tag, "fields": fields})
    return {"a": _side(runs[0], documents[0]), "b": _side(runs[1], documents[1]),
            "same_revision": runs[0].get("draft_revision") == runs[1].get("draft_revision"),
            "process_content_digests": process_digests,
            "process_content_differs": process_digests[0] != process_digests[1],
            "settings_diff": settings_diff(settings[0], settings[1]), "streams": streams,
            "only_in_a": sorted(set(left) - set(right)), "only_in_b": sorted(set(right) - set(left))}
