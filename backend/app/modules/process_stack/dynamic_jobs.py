"""Durable, workspace-scoped jobs for Process dynamic simulations (spec 172)."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from app.core.paths import build_paths
from app.modules.process_stack import draft

MAX_ACTIVE_PER_WORKSPACE = 2
MAX_ACTIVE_GLOBAL = 8
MAX_SERIES_POINTS = 5000
DEFAULT_SERIES_POINTS = 2000
MAX_SERIES_BYTES = 8 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_lock = threading.RLock()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="process-dynamic")
_futures: dict[str, Future[None]] = {}
_cancel_events: dict[str, threading.Event] = {}


class DynamicJobError(ValueError):
    def __init__(self, code: str, message: str, status: int = 404,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code, self.status, self.detail = code, status, detail or {}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _workspace_root(workspace_id: str) -> Path:
    if not _ID_RE.fullmatch(workspace_id) or workspace_id in {".", ".."}:
        raise DynamicJobError("workspace_not_found", "Workspace was not found")
    try:
        draft.drafts_root(workspace_id)
    except draft.DraftError as exc:
        raise DynamicJobError(exc.code, str(exc), exc.status) from exc
    return build_paths().data_root / "process_dynamic" / workspace_id


def _job_dir(workspace_id: str, job_id: str) -> Path:
    if not _ID_RE.fullmatch(job_id) or job_id in {".", ".."}:
        raise DynamicJobError("job_not_found", "Dynamic run was not found")
    return _workspace_root(workspace_id) / job_id


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DynamicJobError("ARTIFACT_CORRUPT", "Dynamic run record is unreadable", 409) from exc
    if not isinstance(value, dict):
        raise DynamicJobError("ARTIFACT_CORRUPT", "Dynamic run record is invalid", 409)
    return value


def _record(workspace_id: str, job_id: str) -> tuple[Path, dict[str, Any]]:
    directory = _job_dir(workspace_id, job_id)
    record = _read_json(directory / "job.json")
    if record.get("workspace_id") != workspace_id:
        raise DynamicJobError("job_not_found", "Dynamic run was not found")
    return directory, record


def _engine() -> Any:
    # Resolve through sys.modules (not the package attribute) so the engine can be substituted in tests.
    return importlib.import_module("app.modules.process_stack.dynamic_engine")


def _error_payload(exc: BaseException) -> dict[str, Any]:
    return {"code": getattr(exc, "code", "DYNAMIC_RUN_FAILED"), "message": str(exc),
            "detail": getattr(exc, "detail", {})}


def _write_artifacts(directory: Path, series: dict[str, np.ndarray], manifest: dict[str, Any]) -> dict[str, str | int] | None:
    arrays: dict[str, Any] = {str(name): np.asarray(values, dtype=np.float64) for name, values in series.items()}
    lengths = {len(values) for values in arrays.values()}
    if len(lengths) > 1:
        raise ValueError("Engine returned unequal series lengths")
    rows = next(iter(lengths), 0)
    if rows <= 0:
        return None
    npz_path = directory / "series.npz"
    tmp = directory / f".series.{uuid4().hex}.tmp"
    try:
        with tmp.open("wb") as stream:
            np.savez_compressed(stream, **arrays)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, npz_path)
    finally:
        tmp.unlink(missing_ok=True)
    manifest_path = directory / "manifest.json"
    _atomic_json(manifest_path, manifest)
    return {"series_sha256": hashlib.sha256(npz_path.read_bytes()).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(), "rows": rows}


def _run_job(workspace_id: str, job_id: str, snapshot_digest: str) -> None:
    directory, _ = _record(workspace_id, job_id)
    event = _cancel_events[job_id]
    try:
        engine = _engine()
        snapshot = engine.Snapshot(_read_json(directory / "input.json"), snapshot_digest)
        with _lock:
            record = _read_json(directory / "job.json")
            if record["status"] == "cancelled":
                return
            record["status"] = "running"
            record["started_at"] = _now()
            _atomic_json(directory / "job.json", record)

        written = [0.0]

        def progress(value: float) -> None:
            value = min(1.0, max(0.0, float(value)))
            if value - written[0] < 0.01:  # each write fsyncs job.json; a 30-day run has >1000 segments
                return
            with _lock:
                current = _read_json(directory / "job.json")
                if current["status"] == "running":
                    current["progress"] = value
                    _atomic_json(directory / "job.json", current)
                    written[0] = value

        sampler = None
        if getattr(snapshot, "payload", {}).get("scenario", {}).get("downstream_cadence_s"):
            from app.modules.process_stack.dynamic_downstream import build_sampler

            sampler = build_sampler(snapshot)
        result = _engine().run(snapshot, cancelled=event.is_set, progress=progress, sampler=sampler)
        status = result.status
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(f"Invalid dynamic engine status: {status}")
        diagnostic = status != "succeeded"
        manifest = dict(result.manifest)
        if diagnostic:
            manifest["artifact_label"] = f"diagnostic_{status}"
        artifact = _write_artifacts(directory, result.series, manifest)
        with _lock:
            current = _read_json(directory / "job.json")
            # Engine terminal truth wins over a late cancellation request.
            current.update(status=status, progress=1.0 if status == "succeeded" else current.get("progress", 0.0),
                           finished_at=_now(), error=result.error)
            if current.get("cancel_requested") and status == "succeeded":
                current["cancel_outcome"] = "too_late"
            if artifact:
                current["artifacts"] = artifact
            _atomic_json(directory / "job.json", current)
    except BaseException as exc:  # persist worker failure as a terminal job outcome
        with _lock:
            current = _read_json(directory / "job.json")
            if current["status"] not in {"cancelled", "succeeded"}:
                current.update(status="failed", finished_at=_now(), error=_error_payload(exc))
                _atomic_json(directory / "job.json", current)
    finally:
        with _lock:
            _futures.pop(job_id, None)
            _cancel_events.pop(job_id, None)


def start(workspace_id: str, draft_id: str, scenario_id: str) -> dict[str, Any]:
    root = _workspace_root(workspace_id)
    engine = _engine()
    try:
        snapshot = engine.prepare(workspace_id, draft_id, scenario_id)
    except engine.DynamicError as exc:
        raise DynamicJobError(exc.code, str(exc), 422, getattr(exc, "detail", {})) from exc
    with _lock:
        active = 0
        global_active = 0
        for path in root.glob("*/job.json"):
            try:
                record = _read_json(path)
            except DynamicJobError:
                continue
            active += record.get("status") in {"queued", "running"}
            global_active += record.get("status") in {"queued", "running"}
        if active >= MAX_ACTIVE_PER_WORKSPACE:
            raise DynamicJobError("ACTIVE_JOB_LIMIT", "Workspace already has the maximum active dynamic runs", 409)
        for path in build_paths().data_root.glob("process_dynamic/*/*/job.json"):
            if path.parent.parent.name == workspace_id:
                continue
            try:
                record = _read_json(path)
            except DynamicJobError:
                continue
            global_active += record.get("status") in {"queued", "running"}
        if global_active >= MAX_ACTIVE_GLOBAL:
            raise DynamicJobError("GLOBAL_ACTIVE_JOB_LIMIT", "Dynamic run capacity is full", 409)
        job_id = str(uuid4())
        directory = root / job_id
        payload = getattr(snapshot, "payload", {})
        identity = {
            "workspace_id": workspace_id, "draft_id": draft_id,
            "draft_revision": payload.get("revision"),
            "content_digest": payload.get("content_digest"),
            "snapshot_digest": getattr(snapshot, "digest", None),
            "scenario_id": scenario_id,
            "card_refs": [{"tag": unit.get("tag"), "card_id": unit.get("model", {}).get("card", {}).get("id"),
                           "card_revision": unit.get("model", {}).get("card", {}).get("revision"),
                           "card_digest": unit.get("model", {}).get("card", {}).get("digest"),
                           "parameter_set_digest": unit.get("model", {}).get("set", {}).get("digest")}
                          for unit in payload.get("units", [])],
            "profile_refs": [{"profile_id": item.get("ref", {}).get("profile_id"),
                              "digest": item.get("ref", {}).get("digest")}
                             for item in payload.get("profiles", [])],
        }
        _atomic_json(directory / "input.json", payload)
        record = {"job_id": job_id, "workspace_id": workspace_id, "draft_id": draft_id,
                  "scenario_id": scenario_id, "status": "queued", "progress": 0.0,
                  "created_at": _now(), "snapshot_digest": getattr(snapshot, "digest", None),
                  "identity": identity}
        _atomic_json(directory / "job.json", record)
        _cancel_events[job_id] = threading.Event()
        _futures[job_id] = _executor.submit(_run_job, workspace_id, job_id, getattr(snapshot, "digest", ""))
    return {"job_id": job_id, "status": "queued"}


def _owned(workspace_id: str, draft_id: str, job_id: str) -> tuple[Path, dict[str, Any]]:
    directory, record = _record(workspace_id, job_id)
    if record.get("draft_id") != draft_id:
        raise DynamicJobError("job_not_found", "Dynamic run was not found")
    return directory, record


def _current(workspace_id: str, draft_id: str, record: dict[str, Any]) -> bool | None:
    try:
        digest = draft.current_digest(workspace_id, draft_id)
    except draft.DraftError as exc:
        if exc.code in {"draft_not_found", "workspace_not_found"}:
            return False
        raise
    identity = record.get("identity", {})
    bound = identity.get("content_digest")
    return None if bound is None else digest == bound


def status(workspace_id: str, draft_id: str, job_id: str) -> dict[str, Any]:
    _, record = _owned(workspace_id, draft_id, job_id)
    return {**record, "current": _current(workspace_id, draft_id, record)}


def list_jobs(workspace_id: str, draft_id: str, limit: int = 50) -> list[dict[str, Any]]:
    if limit < 1:
        raise DynamicJobError("INVALID_LIMIT", "limit must be positive", 422)
    limit = min(limit, 200)
    root = _workspace_root(workspace_id)
    rows = []
    for path in root.glob("*/job.json"):
        try:
            record = _read_json(path)
        except DynamicJobError:
            continue
        if record.get("draft_id") == draft_id and record.get("workspace_id") == workspace_id:
            rows.append(record)
    rows.sort(key=lambda item: (item.get("created_at", ""), item.get("job_id", "")), reverse=True)
    return rows[:limit]


def cancel(workspace_id: str, draft_id: str, job_id: str) -> dict[str, Any]:
    with _lock:
        directory, record = _owned(workspace_id, draft_id, job_id)
        if record["status"] in {"succeeded", "failed", "cancelled"}:
            if record["status"] == "succeeded":
                record["cancel_requested"] = True
                record["cancel_outcome"] = "too_late"
                _atomic_json(directory / "job.json", record)
            return record
        record["cancel_requested"] = True
        if record["status"] == "queued":
            record.update(status="cancelled", finished_at=_now(),
                          error={"code": "CANCELLED", "message": "Run cancelled before starting", "detail": {}})
        _atomic_json(directory / "job.json", record)
        event = _cancel_events.get(job_id)
        if event:
            event.set()
        return record


def _verify(directory: Path, record: dict[str, Any], kind: str) -> Path:
    artifacts = record.get("artifacts", {})
    filename = "series.npz" if kind == "series" else "manifest.json"
    digest_key = "series_sha256" if kind == "series" else "manifest_sha256"
    path = directory / filename
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise DynamicJobError("ARTIFACT_NOT_FOUND", "Dynamic run artifact is unavailable", 404) from exc
    if digest != artifacts.get(digest_key):
        raise DynamicJobError("ARTIFACT_DIGEST_MISMATCH", "Dynamic run artifact failed digest verification", 409)
    return path


def read_manifest(workspace_id: str, draft_id: str, job_id: str) -> dict[str, Any]:
    directory, record = _owned(workspace_id, draft_id, job_id)
    _verify(directory, record, "manifest")
    return _read_json(directory / "manifest.json")


def read_series(workspace_id: str, draft_id: str, job_id: str, *, channels: list[str] | None = None,
                start_s: float | None = None, end_s: float | None = None,
                max_points: int = DEFAULT_SERIES_POINTS) -> dict[str, Any]:
    if max_points < 1:
        raise DynamicJobError("INVALID_SERIES_QUERY", "max_points must be positive", 422)
    max_points = min(max_points, MAX_SERIES_POINTS)
    directory, record = _owned(workspace_id, draft_id, job_id)
    path = _verify(directory, record, "series")
    try:
        with np.load(path, allow_pickle=False) as loaded:
            names = list(loaded.files)
            selected = channels or names
            if any(name not in names for name in selected):
                raise DynamicJobError("UNKNOWN_CHANNEL", "Requested dynamic series channel does not exist", 422)
            if "t_s" not in selected:
                selected = ["t_s", *selected]
            arrays = {name: loaded[name] for name in selected}
    except (OSError, ValueError) as exc:
        if isinstance(exc, DynamicJobError):
            raise
        raise DynamicJobError("ARTIFACT_CORRUPT", "Dynamic series artifact is unreadable", 409) from exc
    t = arrays["t_s"]
    mask = np.ones(len(t), dtype=bool)
    if start_s is not None:
        mask &= t >= start_s
    if end_s is not None:
        mask &= t <= end_s
    indices = np.flatnonzero(mask)
    stride = max(1, int(np.ceil(len(indices) / max_points)))
    indices = indices[::stride]
    response = {name: values[indices].tolist() for name, values in arrays.items()}
    encoded_size = sum(len(json.dumps(values, separators=(",", ":"))) for values in response.values())
    if encoded_size > MAX_SERIES_BYTES:
        raise DynamicJobError("SERIES_TOO_LARGE", "Requested dynamic series exceeds the response byte cap", 413)
    return {"job_id": job_id, "channels": response, "returned_points": len(indices),
            "total_points": int(mask.sum()), "stride": stride}


def recover_interrupted() -> int:
    """Mark persisted nonterminal records interrupted after process restart."""
    root = build_paths().data_root / "process_dynamic"
    recovered = 0
    with _lock:
        for path in root.glob("*/*/job.json"):
            try:
                record = _read_json(path)
            except DynamicJobError:
                continue
            if record.get("status") not in {"queued", "running"}:
                continue
            record.update(status="failed", finished_at=_now(),
                          error={"code": "JOB_INTERRUPTED_BY_RESTART",
                                 "message": "Dynamic run was interrupted by process restart", "detail": {}})
            record.pop("artifacts", None)
            (path.parent / "series.npz").unlink(missing_ok=True)
            (path.parent / "manifest.json").unlink(missing_ok=True)
            _atomic_json(path, record)
            recovered += 1
    return recovered
