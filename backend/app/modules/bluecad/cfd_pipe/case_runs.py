"""Evaluator-owned background pipe-case runs for a Design component (spec 216 contract 4).

A run executes one bounded pipe case (``execute_pipe_case``) off the request thread. Tool log lines and solver
residual rows are appended to ``events.jsonl`` at the moment they are produced, stamped with their emission time;
readers follow it by cursor. ``run.json`` holds the state. A stop is cooperative: the pinned tools see the
cancel event and end their process safely, and a stopped run is never reported as finished. ``live`` is true only
while this process runs the case; a record that claims to run but is not owned by this process was cut off by a
restart and is reported ``interrupted``. A retry is a new versioned run linked by ``retry_of``.

A finished run also records a server-measured ``measurement`` (developed-section pressure gradient read from the
run's own probe files, or ``unmeasured`` with a plain reason) and keeps its exact ``contract.json``. A run record
grants no study qualification and no reduced-model publication authority (``qualifies_publication`` is always
false); publication is gated elsewhere, from these server records only.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.paths import build_paths
from app.modules.bluecad.cfd_pipe.case_execution import execute_pipe_case
from app.modules.bluecad.cfd_pipe.casegen import MAX_PROCESSORS, SYMMETRY, manifest_digest
from app.modules.bluecad.cfd_pipe.contract import PipeStudyContract, contract_digest
from app.modules.bluecad.cfd_pipe.field_export import read_field_slice, read_field_surfaces_3d
from app.modules.bluecad.cfd_pipe.probe_results import ProbeResultError, laminar_comparison, turbulent_comparison
from app.modules.bluecad.cfd_pipe.residuals import IterationResiduals

MAX_ACTIVE_GLOBAL = 1
MAX_EVENTS = 20000
MAX_EVENT_BYTES = 8 * 1024 * 1024
MAX_EVENTS_PER_READ = 500
MAX_LINE_CHARS = 1000
RUN_RE = re.compile(r"[a-f0-9]{32}")
ACTIVE_STATES = {"queued", "running", "stopping"}
_INTERRUPTED = "The backend stopped while this case was running; no result was recorded."
_PATH_TEXT = re.compile(r"(?:[A-Za-z]:)?[\\/][^\s'\"]+")
_lock = threading.RLock()
_executor = ThreadPoolExecutor(max_workers=MAX_ACTIVE_GLOBAL, thread_name_prefix="cfd-case")
_active: dict[str, threading.Event] = {}  # run_id -> stop request, only for runs this process is running


class CaseRunError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def _plain(value: str, limit: int = 300) -> str:
    text = " ".join(_PATH_TEXT.sub("<path>", value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _component_dir(workspace_id: str, component_id: str) -> Path:
    return build_paths().workspaces_dir / workspace_id / "design_cfd_runs" / component_id


def _run_dir(workspace_id: str, component_id: str, run_id: str) -> Path:
    if not RUN_RE.fullmatch(run_id):
        raise CaseRunError("cfd_run_not_found", "CFD run was not found", 404)
    return _component_dir(workspace_id, component_id) / run_id


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads((path / "run.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseRunError("cfd_run_not_found", "CFD run was not found", 404) from exc
    if not isinstance(value, dict):
        raise CaseRunError("cfd_run_not_found", "CFD run was not found", 404)
    return value


def _interrupt(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    record = {**record, "state": "interrupted", "finished_at": _now(), "message": _INTERRUPTED}
    _write(path / "run.json", record)
    return record


def _settled(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """A record claiming to run that this process does not run was cut off by a restart: say so, invent nothing."""
    with _lock:
        if record.get("state") in ACTIVE_STATES and record["run_id"] not in _active:
            record = _read(path)  # the worker may have finished between the caller's read and this lock
            if record.get("state") in ACTIVE_STATES and record["run_id"] not in _active:
                record = _interrupt(path, record)
    return record


def recover_interrupted() -> int:
    """Mark unfinished runs interrupted when the backend starts, without creating a result."""
    recovered = 0
    with _lock:
        for path in build_paths().workspaces_dir.glob("*/design_cfd_runs/*/*/run.json"):
            try:
                record = _read(path.parent)
            except CaseRunError:
                continue
            if record.get("state") in ACTIVE_STATES:
                _interrupt(path.parent, record)
                recovered += 1
    return recovered


class _Events:
    def __init__(self, path: Path) -> None:
        self.path, self.count, self.size, self.truncated = path / "events.jsonl", 0, 0, False
        self._lock = threading.Lock()  # tool output may arrive from several reader threads

    def emit(self, kind: str, **fields: Any) -> None:
        with self._lock:
            if self.count >= MAX_EVENTS or self.truncated:
                self.truncated = True
                return
            event = {"seq": self.count + 1, "kind": kind, "emitted_at": _now(), **fields}
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

    def log(self, stream: str, line: str) -> None:
        self.emit("log", stream=stream[:20], line=line[:MAX_LINE_CHARS])

    def residual(self, row: IterationResiduals) -> None:
        # A non-finite residual (a diverging solver prints nan) is not valid JSON: report null, invent no number.
        value = _finite(row.model_dump(mode="json"))
        self.emit("residual", **value, max_normalized_residual=_finite(row.max_normalized_residual))


def _finite(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    return value


PROBE_DIR = Path("postProcessing") / "pipe_probes"


def _unmeasured(reason: str) -> dict[str, Any]:
    return {"status": "unmeasured", "reason": _plain(reason) or "no result was measured"}


def _probe_texts(case: Path) -> tuple[str, str]:
    """The final-time pressure and velocity probe files of the case's own ``pipe_probes`` function object."""
    times = []
    for directory in (case / PROBE_DIR).glob("*") if (case / PROBE_DIR).is_dir() else []:
        try:
            times.append((float(directory.name), directory))
        except ValueError:
            continue
    for _, directory in sorted(times, key=lambda item: item[0], reverse=True):
        if (directory / "p").is_file() and (directory / "U").is_file():
            return (directory / "p").read_text(encoding="utf-8"), (directory / "U").read_text(encoding="utf-8")
    raise ProbeResultError("the case wrote no pressure and velocity probe files")


def measure_run(contract: PipeStudyContract, case: Path, execution: Any) -> dict[str, Any]:
    """Developed-section measurement of a finished case from its own probe files; never raises."""
    try:
        pressure, velocity = _probe_texts(case)
        compare = laminar_comparison if contract.regime == "laminar" else turbulent_comparison
        comparison = compare(contract, pressure, velocity)
        manifest = execution.mesh.case_manifest
        check = execution.mesh.check
        report = getattr(check, "report", None)
        cells = report.cells if report is not None else getattr(getattr(check, "plan", None), "cell_count", None)
        versions = {str(k): str(v) for k, v in ((manifest or {}).get("tool_versions") or {}).items()}
        if execution.solver is not None and execution.solver.solver_version:
            versions["openfoam_simplefoam"] = execution.solver.solver_version
        gradient = _finite(comparison["pressure_gradient_pa_m"])
        if gradient is None:
            raise ValueError("pressure gradient is not finite")
        return {"status": "measured", "reason": None, "regime": contract.regime,
                "pressure_gradient_pa_m": gradient,
                "wall_shear_pa": gradient * contract.diameter_m / 4.0,
                "wall_shear_method": "developed_pipe_force_balance",
                "reynolds": contract.reynolds,
                "mesh_level": contract.mesh_level, "cells": cells,
                "case_manifest_digest": manifest_digest(manifest) if manifest else None,
                "tool_versions": dict(sorted(versions.items())), "comparison": _finite(comparison)}
    except (ProbeResultError, OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return _unmeasured(f"probe results could not be read: {exc}")


def _perform(path: Path, run_id: str, contract: PipeStudyContract, stop: threading.Event,
             processors: int = 1) -> None:
    events = _Events(path)
    try:
        with _lock:
            record = _read(path)
            if stop.is_set():  # stopped while queued: never start tools
                final: dict[str, Any] = {"state": "stopped", "message": "Stopped before the case started."}
            else:
                _write(path / "run.json", {**record, "state": "running", "started_at": _now()})
                final = {}
        if not final:
            execution = execute_pipe_case(contract, path / "case", cancel=stop, on_log=events.log,
                                          on_residual=events.residual, processors=processors)
            summary = {"case_status": execution.status, "mesh_status": execution.mesh.status,
                       "mesh_reason": _plain(execution.mesh.reason or "") or None,
                       "solver_status": execution.solver.status if execution.solver else None,
                       "solver_reason": _plain(execution.solver.reason or "") or None if execution.solver else None,
                       "residual_converged": bool(execution.solver and execution.solver.residual_converged),
                       "iterations": len(execution.solver.iterations) if execution.solver else 0}
            state = "stopped" if stop.is_set() else execution.status
            final = {"state": state, **summary}
            if state == "finished":
                final["measurement"] = measure_run(contract, path / "case", execution)
    except Exception as exc:  # noqa: BLE001 - an unexpected failure ends the run truthfully, without a traceback
        final = {"state": "stopped" if stop.is_set() else "failed",
                 "message": _plain(str(exc)) or type(exc).__name__}
    final.setdefault("measurement", _unmeasured(f"the run is {final['state']}; it produced no measurement"))
    with _lock:
        record = _read(path)
        _write(path / "run.json", {**record, **final, "finished_at": _now(), "events": events.count,
                                   "events_truncated": events.truncated})
        _active.pop(run_id, None)


def start_run(workspace_id: str, component_id: str, regime: str, contract: PipeStudyContract, template_digest: str,
              retry_of: str | None = None, processors: int = 1) -> dict[str, Any]:
    """Queue one run; ``processors`` (MPI ranks) is an execution resource, never part of the contract digest."""
    if isinstance(processors, bool) or not isinstance(processors, int) or not 1 <= processors <= MAX_PROCESSORS:
        raise CaseRunError("cfd_processors_invalid", f"processors must be an integer from 1 to {MAX_PROCESSORS}", 422)
    if retry_of is not None:
        parent = _read(_run_dir(workspace_id, component_id, retry_of)) if RUN_RE.fullmatch(retry_of) else None
        if parent is None or parent.get("regime") != regime:
            raise CaseRunError("cfd_retry_parent_invalid", "retry_of must name an earlier run of this component "
                               "and regime", 422)
    with _lock:
        for record in list_runs(workspace_id, component_id):
            if record["state"] in ACTIVE_STATES:
                raise CaseRunError("cfd_run_active", "A CFD case of this component is already running", 409)
        if len(_active) >= MAX_ACTIVE_GLOBAL:
            raise CaseRunError("cfd_run_capacity", "Too many CFD cases are running; try again shortly", 429)
        run_id = uuid4().hex
        path = _component_dir(workspace_id, component_id) / run_id
        path.mkdir(parents=True, exist_ok=False)
        record = {"run_id": run_id, "component_id": component_id, "regime": regime, "state": "queued",
                  "template_digest": template_digest, "geometry_digest": contract.domain.geometry_digest,
                  "contract_digest": contract_digest(contract), "processors": processors, "queued_at": _now(), "retry_of": retry_of, "qualifies_publication": False,
                  "symmetry": SYMMETRY,
                  "authority": "evidence only; grants no study qualification or reduced-model publication"}
        _write(path / "contract.json", contract.model_dump(mode="json"))
        _write(path / "run.json", record)
        (path / "events.jsonl").touch()
        stop = threading.Event()
        _active[run_id] = stop
        try:
            _executor.submit(_perform, path, run_id, contract, stop, processors)
        except RuntimeError:
            _active.pop(run_id, None)
            raise
    return {**record, "live": True}


def read_events(workspace_id: str, component_id: str, run_id: str, cursor: int = 0) -> dict[str, Any]:
    path = _run_dir(workspace_id, component_id, run_id)
    record = _settled(path, _read(path))
    after = max(0, int(cursor))
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
        pass
    return {"run_id": run_id, "state": record["state"], "live": record["state"] in ACTIVE_STATES,
            "events": rows, "cursor": rows[-1]["seq"] if rows else after}


def read_run(workspace_id: str, component_id: str, run_id: str) -> dict[str, Any]:
    path = _run_dir(workspace_id, component_id, run_id)
    record = _settled(path, _read(path))
    return {**record, "live": record["state"] in ACTIVE_STATES}


def read_run_fields(workspace_id: str, component_id: str, run_id: str) -> dict[str, Any]:
    """Pressure and velocity slice of a finished run's own case; unavailable (never invented) otherwise."""
    path = _run_dir(workspace_id, component_id, run_id)
    record = _settled(path, _read(path))
    if record["state"] != "finished":
        return {"status": "unavailable", "run_id": run_id,
                "reason": f"the run is {record['state']}; fields exist only for a finished run"}
    case = (path / "case").resolve()
    if not case.is_relative_to(path.resolve()):
        return {"status": "unavailable", "run_id": run_id, "reason": "the run case directory is not inside the run"}
    return {"run_id": run_id, **read_field_slice(case)}


def read_run_fields_3d(workspace_id: str, component_id: str, run_id: str) -> dict[str, Any]:
    """Computed 3D surfaces of a finished, symmetry-qualified run."""
    path = _run_dir(workspace_id, component_id, run_id)
    record = _settled(path, _read(path))
    if record["state"] != "finished":
        return {"status": "unavailable", "run_id": run_id,
                "reason": f"the run is {record['state']}; fields exist only for a finished run"}
    symmetry = record.get("symmetry")
    if symmetry != SYMMETRY:
        return {"status": "unavailable", "run_id": run_id,
                "reason": "the run has no supported quarter-domain symmetry record"}
    case = (path / "case").resolve()
    if not case.is_relative_to(path.resolve()):
        return {"status": "unavailable", "run_id": run_id, "reason": "the run case directory is not inside the run"}
    return {"run_id": run_id, "symmetry": symmetry, **read_field_surfaces_3d(case)}


def read_run_contract(workspace_id: str, component_id: str, run_id: str) -> PipeStudyContract:
    """The exact contract the run was started with; raises ``CaseRunError`` when absent or unreadable."""
    path = _run_dir(workspace_id, component_id, run_id)
    try:
        return PipeStudyContract.model_validate_json((path / "contract.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaseRunError("cfd_run_contract_missing", "CFD run has no readable contract", 404) from exc


def request_stop(workspace_id: str, component_id: str, run_id: str) -> dict[str, Any]:
    path = _run_dir(workspace_id, component_id, run_id)
    with _lock:
        record = _settled(path, _read(path))
        if record["state"] in {"queued", "running"} and run_id in _active:
            _active[run_id].set()
            record = {**record, "state": "stopping", "stop_requested_at": _now()}
            _write(path / "run.json", record)
            return {**record, "live": True, "stop": "requested"}
    return {**record, "live": record["state"] in ACTIVE_STATES, "stop": "noop"}


def list_runs(workspace_id: str, component_id: str, limit: int = 20) -> list[dict[str, Any]]:
    root = _component_dir(workspace_id, component_id)
    rows = []
    for path in root.glob("*/run.json") if root.exists() else []:
        try:
            rows.append(_settled(path.parent, _read(path.parent)))
        except CaseRunError:
            continue
    rows = sorted(rows, key=lambda row: row.get("queued_at") or "", reverse=True)[:limit]
    return [{**row, "live": row["state"] in ACTIVE_STATES} for row in rows]
