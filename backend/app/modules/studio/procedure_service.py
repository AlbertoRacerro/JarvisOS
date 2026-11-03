"""Test, Apply, link, run and bench support for Studio procedures (spec 191). Simulation only; no runtime call."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from app.core.database import open_sqlite_connection
from app.modules.events.service import utc_now
from app.modules.process_stack import draft, dynamic_engine, dynamic_jobs
from app.modules.process_stack.draft_models import SetScenario, SetSchedule
from app.modules.runner.safety import model_implementation_root
from app.modules.studio.models import StudioError
from app.modules.studio.procedure import (
    DEFAULT_SCHEDULE_ID,
    NOTICE,
    PROCEDURE_KIND,
    SCHEDULE_ID_PATTERN,
    Expectation,
    ProcedureCase,
    ProcedureTestRequest,
    canonical_json,
    compile_procedure,
    compiled_sha256,
    schedule_value,
    sha256_text,
    steps_of,
)

ARTIFACT_NAME = "studio_procedure.json"
_TERMINAL = {"succeeded", "failed", "cancelled"}


# ---------------------------------------------------------------- draft access and scratch validation


def _wrap(exc: Exception) -> StudioError:
    if isinstance(exc, StudioError):
        return exc
    if isinstance(exc, draft.DraftError):
        return StudioError(exc.code, str(exc), dict(exc.detail), exc.status)
    if isinstance(exc, dynamic_engine.DynamicError):
        return StudioError(exc.code, exc.message, dict(exc.detail))
    if isinstance(exc, dynamic_jobs.DynamicJobError):
        return StudioError(exc.code, str(exc), dict(exc.detail), exc.status)
    raise exc


def _document(workspace_id: str, draft_id: str) -> tuple[dict[str, Any], str]:
    try:
        directory = draft.draft_dir(workspace_id, draft_id)
        head = draft._head(directory)
        return draft.load_revision(directory, head["revision"])["document"], head["revision"]
    except draft.DraftError as exc:
        if exc.code == "draft_not_found":
            raise StudioError("studio_procedure_draft_not_found", "Process draft not found.") from exc
        raise _wrap(exc) from exc


def _ops(document: dict[str, Any], scenario_id: str, schedule_id: str, value: dict[str, Any]) -> list[Any]:
    scenario = document.get("scenarios", {}).get(scenario_id)
    if scenario is None:
        raise StudioError("SCENARIO_NOT_FOUND", "Dynamic scenario was not found.", {"scenario_id": scenario_id})
    ops: list[Any] = [SetSchedule(op="set_schedule", id=schedule_id, value=value)]
    if scenario.get("schedule_id") != schedule_id:
        ops.append(SetScenario(op="set_scenario", id=scenario_id, value={**scenario, "schedule_id": schedule_id}))
    return ops


@contextmanager
def _scratch(workspace_id: str, document: dict[str, Any], revision: str, ops: list[Any]) -> Iterator[str]:
    """A throwaway copy of the draft with ``ops`` applied; the target draft is never touched."""
    scratch_id = str(uuid4())
    directory = draft.drafts_root(workspace_id) / scratch_id
    try:
        record = draft._write_revision(directory, document, parent=None, actor="studio-scratch", ops=[])
        draft.patch(workspace_id, scratch_id, record["revision"], ops, actor="studio-scratch")
        yield scratch_id
    except (draft.DraftError, dynamic_engine.DynamicError) as exc:
        raise _wrap(exc) from exc
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _prepare(workspace_id: str, scratch_id: str, scenario_id: str) -> dynamic_engine.Snapshot:
    try:
        return dynamic_engine.prepare(workspace_id, scratch_id, scenario_id)
    except dynamic_engine.DynamicError as exc:
        raise _wrap(exc) from exc


def _prepare_summary(snapshot: dynamic_engine.Snapshot) -> dict[str, Any]:
    events = snapshot.payload["schedule"]["events"]
    timed = [event for event in events if not event.get("condition")]
    return {"ok": True, "expanded_event_count": len(events), "time_triggered_events": len(timed),
            "state_triggered_events": len(events) - len(timed),
            "first_event_time_s": events[0]["time_s"] if events else None,
            "last_event_time_s": events[-1]["time_s"] if events else None,
            "duration_s": snapshot.payload["end_epoch"] - snapshot.payload["start_epoch"],
            "expanded_schedule_sha256": sha256_text(canonical_json(events))}


# ---------------------------------------------------------------- Test


def _fires(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in manifest.get("event_log", []) if entry.get("trigger") == "state"]


def _close(actual: float, expect: Expectation) -> bool:
    assert expect.value is not None
    limit = expect.tolerance * abs(expect.value) if expect.relative else expect.tolerance
    return abs(actual - expect.value) <= limit


def _expectation(expect: Expectation, result: dynamic_engine.EngineResult) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": expect.kind, "passed": False, "actual": None, "expected": expect.value, "message": ""}
    manifest, series = result.manifest, result.series
    if expect.kind == "balances_closed":
        actions = [entry for entry in manifest.get("event_log", []) if entry.get("type") == "actions"]
        closed = bool(manifest.get("balances", {}).get("asserted")) and all(
            entry.get("balance", {}).get("closed") for entry in actions)
        return {**out, "passed": closed, "actual": closed, "expected": True,
                "message": "" if closed else "A balance is not closed."}
    if expect.value is None:
        return {**out, "message": "value is required."}
    if expect.kind == "fire_time":
        fires = _fires(manifest)
        if expect.fire_index is None or expect.fire_index > len(fires):
            return {**out, "message": f"Only {len(fires)} state-triggered fires occurred."}
        actual = float(fires[expect.fire_index - 1]["time_s"])
    else:
        if expect.channel is None or expect.time_s is None or expect.channel not in series:
            return {**out, "message": "A known channel and time_s are required."}
        times = series["t_s"]
        phases = series.get("event_phase", np.zeros(len(times)))
        wanted = {"pre": -1, "post": 1, "sample": 0}[expect.phase]
        selected = (np.abs(times - expect.time_s) <= 1e-9) & (phases == wanted)
        if expect.event_order is not None:
            selected &= series.get("event_order", np.full(len(times), -1.0)) == expect.event_order
        rows = np.flatnonzero(selected)
        if not len(rows):
            return {**out, "message": f"No {expect.phase} row at t={expect.time_s} s."}
        actual = float(series[expect.channel][rows[-1] if expect.phase == "post" else rows[0]])
    ok = _close(actual, expect)
    return {**out, "passed": ok, "actual": actual, "message": "" if ok else "Outside tolerance."}


def _simulate(snapshot: dynamic_engine.Snapshot) -> dynamic_engine.EngineResult:
    sampler = None
    if snapshot.payload.get("scenario", {}).get("downstream_cadence_s"):
        from app.modules.process_stack.dynamic_downstream import build_sampler

        sampler = build_sampler(snapshot)
    return dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _value: None, sampler=sampler)


def _case(procedure: dict[str, Any], workspace_id: str, case: ProcedureCase) -> dict[str, Any]:
    out: dict[str, Any] = {"name": case.name, "passed": False, "code": None, "message": None, "detail": None,
                           "compiled_sha256": None, "compiled": None, "prepare": None, "simulation": None,
                           "expectations": [], "errors": []}
    try:
        if case.expect and not case.simulate:
            raise StudioError("studio_procedure_invalid", "Expectations need simulate=true.")
        document, revision = _document(workspace_id, case.draft_id)
        schedule = compile_procedure(procedure, case.parameter_values, document, case.schedule_id)
        value = schedule_value(schedule)
        out.update(compiled=value, compiled_sha256=compiled_sha256(value))
        with _scratch(workspace_id, document, revision, _ops(document, case.scenario_id, case.schedule_id, value)) as scratch:
            snapshot = _prepare(workspace_id, scratch, case.scenario_id)
            out["prepare"] = _prepare_summary(snapshot)
            if not case.simulate:
                out["passed"] = True
                return out
            result = _simulate(snapshot)
        manifest = result.manifest
        out["simulation"] = {"status": result.status, "error": result.error,
                             "balances_asserted": bool(manifest.get("balances", {}).get("asserted")),
                             "event_count": len([e for e in manifest.get("event_log", []) if e.get("type") == "actions"]),
                             "state_fires": [{"time_s": fire["time_s"]} for fire in _fires(manifest)]}
        if result.status != "succeeded":
            error = result.error or {}
            raise StudioError(error.get("code", "DYNAMIC_FAILED"), error.get("message", "Simulation failed."),
                              error.get("detail") or {})
        out["expectations"] = [_expectation(item, result) for item in case.expect]
        out["errors"] = [f"{item['kind']}: {item['message']}" for item in out["expectations"] if not item["passed"]]
        out["passed"] = not out["errors"]
    except (StudioError, draft.DraftError, dynamic_engine.DynamicError) as raw:
        exc = _wrap(raw)
        out.update(passed=False, code=exc.code, message=exc.message, detail=exc.detail or None)
        out["errors"] = [f"{exc.code}: {exc.message}"]
    return out


def run_tests(workspace_id: str, row: Any, data: ProcedureTestRequest) -> dict[str, Any]:
    contract = json.loads(row["contract_json"])
    procedure = {"parameters": contract["parameters"], "steps": steps_of(row["source"])}
    cases = [_case(procedure, workspace_id, case) for case in data.cases]
    return {"source_sha256": row["source_sha256"], "contract_sha256": row["contract_sha256"],
            "passed": all(case["passed"] for case in cases), "cases": cases, "notice": NOTICE}


# ---------------------------------------------------------------- published versions


def published_procedure(workspace_id: str, version_id: str) -> dict[str, Any]:
    """One published procedure version with its verified artifact; refuses a tampered artifact."""
    with open_sqlite_connection() as connection:
        row = connection.execute("""SELECT mv.id, mv.model_spec_id, mv.version_label, sp.contract_json, sp.source_sha256,
            sp.contract_sha256, a.stored_path, a.sha256 AS artifact_sha256 FROM studio_published_versions sp
            JOIN model_versions mv ON mv.id = sp.version_id JOIN artifacts a ON a.id = mv.implementation_artifact_id
            WHERE mv.id = ? AND mv.workspace_id = ? AND mv.implementation_kind = ?""",
                               (version_id, workspace_id, PROCEDURE_KIND)).fetchone()
        if row is None:
            raise StudioError("studio_procedure_version_not_found", "Published procedure version not found.")
        number = connection.execute("""SELECT COUNT(*) FROM studio_published_versions WHERE model_spec_id = ?
            AND rowid <= (SELECT rowid FROM studio_published_versions WHERE version_id = ?)""",
                                    (row["model_spec_id"], version_id)).fetchone()[0]
    contract = json.loads(row["contract_json"])
    path = Path(row["stored_path"])
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = None
    if (text is None or path != model_implementation_root(workspace_id) / version_id / ARTIFACT_NAME
            or sha256_text(text) != row["source_sha256"] or row["artifact_sha256"] != row["source_sha256"]
            or sha256_text(canonical_json(contract)) != row["contract_sha256"]):
        raise StudioError("studio_artifact_hash_mismatch", "Published procedure artifact is invalid.", status=409)
    return {"version_id": version_id, "model_spec_id": row["model_spec_id"], "version_label": row["version_label"],
            "version_number": number, "contract": contract, "steps": steps_of(text),
            "source_sha256": row["source_sha256"], "contract_sha256": row["contract_sha256"]}


# ---------------------------------------------------------------- links


def _latest(connection: Any, model_spec_id: str) -> Any:
    return connection.execute("""SELECT mv.id, mv.version_label FROM studio_published_versions sp
        JOIN model_versions mv ON mv.id = sp.version_id WHERE sp.model_spec_id = ?
        ORDER BY sp.rowid DESC LIMIT 1""", (model_spec_id,)).fetchone()


def _link_state(workspace_id: str, row: Any) -> str:
    try:
        document, _ = _document(workspace_id, row["draft_id"])
    except StudioError:
        return "missing"
    schedule = document.get("schedules", {}).get(row["schedule_id"])
    if schedule is None:
        return "missing"
    return "applied" if compiled_sha256(schedule) == row["compiled_sha256"] else "modified"


_STATE_TEXT = {"applied": "Applied", "modified": "Modified, no longer the pinned procedure.",
               "missing": "Schedule no longer in the draft."}


def _link_view(connection: Any, workspace_id: str, row: Any) -> dict[str, Any]:
    state = _link_state(workspace_id, row)
    label = connection.execute("SELECT version_label FROM model_versions WHERE id = ?", (row["version_id"],)).fetchone()
    number = connection.execute("""SELECT COUNT(*) FROM studio_published_versions WHERE model_spec_id = ?
        AND rowid <= (SELECT rowid FROM studio_published_versions WHERE version_id = ?)""",
                                (row["model_spec_id"], row["version_id"])).fetchone()[0]
    latest = _latest(connection, row["model_spec_id"])
    return {"id": row["id"], "workspace_id": row["workspace_id"], "draft_id": row["draft_id"],
            "scenario_id": row["scenario_id"], "schedule_id": row["schedule_id"],
            "model_spec_id": row["model_spec_id"], "version_id": row["version_id"],
            "version_label": label["version_label"], "version_number": number,
            "source_sha256": row["source_sha256"], "contract_sha256": row["contract_sha256"],
            "parameter_values": json.loads(row["parameter_values_json"]), "compiled_sha256": row["compiled_sha256"],
            "applied_at": row["applied_at"], "applied_draft_revision": row["applied_draft_revision"],
            "state": state, "state_text": _STATE_TEXT[state], "latest_version_id": latest["id"],
            "latest_version_label": latest["version_label"], "update_available": latest["id"] != row["version_id"],
            "notice": NOTICE}


def _link_row(connection: Any, workspace_id: str, link_id: str) -> Any:
    row = connection.execute("SELECT * FROM studio_procedure_links WHERE id = ? AND workspace_id = ?",
                             (link_id, workspace_id)).fetchone()
    if row is None:
        raise StudioError("studio_procedure_link_not_found", "Procedure link not found.")
    return row


def list_links(workspace_id: str, draft_id: str | None = None) -> list[dict[str, Any]]:
    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM studio_procedure_links WHERE workspace_id = ? AND (? IS NULL OR draft_id = ?) "
            "ORDER BY applied_at DESC, rowid DESC", (workspace_id, draft_id, draft_id)).fetchall()
        return [_link_view(connection, workspace_id, row) for row in rows]


def get_link(workspace_id: str, link_id: str) -> dict[str, Any]:
    with open_sqlite_connection() as connection:
        return _link_view(connection, workspace_id, _link_row(connection, workspace_id, link_id))


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: str = Field(min_length=1, max_length=64)
    draft_id: str = Field(min_length=1, max_length=64)
    scenario_id: str = Field(min_length=1, max_length=128)
    schedule_id: str = Field(default=DEFAULT_SCHEDULE_ID, pattern=SCHEDULE_ID_PATTERN)
    parameter_values: dict[str, float] = Field(default_factory=dict)
    overwrite: bool = False


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: str | None = Field(default=None, min_length=1, max_length=64)
    parameter_values: dict[str, float] | None = None
    overwrite: bool = False


def apply(workspace_id: str, request: ApplyRequest) -> dict[str, Any]:
    version = published_procedure(workspace_id, request.version_id)
    procedure = {"parameters": version["contract"]["parameters"], "steps": version["steps"]}
    document, revision = _document(workspace_id, request.draft_id)
    with open_sqlite_connection() as connection:
        existing = connection.execute(
            "SELECT * FROM studio_procedure_links WHERE workspace_id = ? AND draft_id = ? AND schedule_id = ?",
            (workspace_id, request.draft_id, request.schedule_id)).fetchone()
    if existing is not None:
        if _link_state(workspace_id, existing) == "modified" and not request.overwrite:
            raise StudioError("studio_procedure_link_modified",
                              "The applied schedule was edited by hand; overwrite to replace it.", status=409)
    elif request.schedule_id in document.get("schedules", {}) and not request.overwrite:
        raise StudioError("studio_procedure_schedule_exists",
                          "A schedule with this id already exists in the draft and is not a procedure link.", status=409)
    scenario = document.get("scenarios", {}).get(request.scenario_id)
    if scenario is not None and scenario.get("schedule_id") not in (None, request.schedule_id) and not request.overwrite:
        raise StudioError("studio_procedure_scenario_schedule_conflict",
                          "The scenario already uses another schedule; overwrite to switch it.",
                          {"schedule_id": scenario.get("schedule_id")}, 409)
    schedule = compile_procedure(procedure, request.parameter_values, document, request.schedule_id)
    value = schedule_value(schedule)
    ops = _ops(document, request.scenario_id, request.schedule_id, value)
    with _scratch(workspace_id, document, revision, ops) as scratch:
        _prepare(workspace_id, scratch, request.scenario_id)
    provenance = {"studio_procedure": {"version_id": version["version_id"], "source_sha256": version["source_sha256"],
                                       "contract_sha256": version["contract_sha256"],
                                       "compiled_sha256": compiled_sha256(value)}}
    try:
        patched = draft.patch(workspace_id, request.draft_id, revision, ops, actor="studio-procedure",
                              provenance=provenance)
    except draft.DraftError as exc:
        raise _wrap(exc) from exc
    now = utc_now()
    values = {name: float(number) for name, number in sorted(request.parameter_values.items())}
    with open_sqlite_connection() as connection:
        link_id = existing["id"] if existing is not None else str(uuid4())
        fields = (request.scenario_id, version["model_spec_id"], version["version_id"], version["source_sha256"],
                  version["contract_sha256"], canonical_json(values), compiled_sha256(value), patched["revision"], now)
        if existing is not None:
            connection.execute("""UPDATE studio_procedure_links SET scenario_id = ?, model_spec_id = ?, version_id = ?,
                source_sha256 = ?, contract_sha256 = ?, parameter_values_json = ?, compiled_sha256 = ?,
                applied_draft_revision = ?, applied_at = ? WHERE id = ?""", (*fields, link_id))
        else:
            connection.execute("""INSERT INTO studio_procedure_links (scenario_id, model_spec_id, version_id,
                source_sha256, contract_sha256, parameter_values_json, compiled_sha256, applied_draft_revision,
                applied_at, id, workspace_id, draft_id, schedule_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                               (*fields, link_id, workspace_id, request.draft_id, request.schedule_id))
        connection.commit()
        return _link_view(connection, workspace_id, _link_row(connection, workspace_id, link_id))


def update(workspace_id: str, link_id: str, request: UpdateRequest) -> dict[str, Any]:
    with open_sqlite_connection() as connection:
        row = _link_row(connection, workspace_id, link_id)
        version_id = request.version_id or _latest(connection, row["model_spec_id"])["id"]
        if connection.execute("SELECT 1 FROM studio_published_versions WHERE version_id = ? AND model_spec_id = ?",
                              (version_id, row["model_spec_id"])).fetchone() is None:
            raise StudioError("studio_procedure_version_not_found", "That version does not belong to this procedure.")
    values = request.parameter_values if request.parameter_values is not None else json.loads(row["parameter_values_json"])
    return apply(workspace_id, ApplyRequest(
        version_id=version_id, draft_id=row["draft_id"], scenario_id=row["scenario_id"], schedule_id=row["schedule_id"],
        parameter_values=values, overwrite=request.overwrite))


# ---------------------------------------------------------------- runs


def start_run(workspace_id: str, link_id: str) -> dict[str, Any]:
    with open_sqlite_connection() as connection:
        row = _link_row(connection, workspace_id, link_id)
        view = _link_view(connection, workspace_id, row)
    if view["state"] != "applied":
        code = "studio_procedure_link_modified" if view["state"] == "modified" else "studio_procedure_link_missing"
        raise StudioError(code, view["state_text"], status=409)
    document, _ = _document(workspace_id, row["draft_id"])
    if document.get("scenarios", {}).get(row["scenario_id"], {}).get("schedule_id") != row["schedule_id"]:
        raise StudioError("studio_procedure_scenario_unlinked", "The scenario no longer uses the applied schedule.",
                          status=409)
    try:
        job = dynamic_jobs.start(workspace_id, row["draft_id"], row["scenario_id"])
    except dynamic_jobs.DynamicJobError as exc:
        raise _wrap(exc) from exc
    with open_sqlite_connection() as connection:
        connection.execute("""INSERT INTO studio_procedure_runs (job_id, workspace_id, draft_id, link_json, created_at)
            VALUES (?, ?, ?, ?, ?)""", (job["job_id"], workspace_id, row["draft_id"], canonical_json(view), utc_now()))
        connection.commit()
    return {**job, "procedure_link": view, "notice": NOTICE}


def _run_link(workspace_id: str, draft_id: str, job_id: str) -> dict[str, Any]:
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT link_json FROM studio_procedure_runs WHERE job_id = ? AND workspace_id = ? "
                                 "AND draft_id = ?", (job_id, workspace_id, draft_id)).fetchone()
    if row is None:
        raise StudioError("studio_procedure_run_not_found", "No procedure run with this id.")
    return json.loads(row["link_json"])


def _job(workspace_id: str, draft_id: str, job_id: str) -> dict[str, Any]:
    try:
        return dynamic_jobs.status(workspace_id, draft_id, job_id)
    except dynamic_jobs.DynamicJobError as exc:
        raise _wrap(exc) from exc


def get_run(workspace_id: str, draft_id: str, job_id: str) -> dict[str, Any]:
    link = _run_link(workspace_id, draft_id, job_id)
    return {"job": _job(workspace_id, draft_id, job_id), "procedure_link": link, "notice": NOTICE}


def list_runs(workspace_id: str, draft_id: str) -> list[dict[str, Any]]:
    with open_sqlite_connection() as connection:
        rows = connection.execute("SELECT job_id FROM studio_procedure_runs WHERE workspace_id = ? AND draft_id = ? "
                                  "ORDER BY created_at DESC, rowid DESC", (workspace_id, draft_id)).fetchall()
    return [get_run(workspace_id, draft_id, row["job_id"]) for row in rows]


def run_results(workspace_id: str, draft_id: str, job_id: str) -> dict[str, Any]:
    link = _run_link(workspace_id, draft_id, job_id)
    job = _job(workspace_id, draft_id, job_id)
    manifest: dict[str, Any] = {}
    if job["status"] in _TERMINAL and job.get("artifacts"):
        try:
            manifest = dynamic_jobs.read_manifest(workspace_id, draft_id, job_id)
        except dynamic_jobs.DynamicJobError as exc:
            raise _wrap(exc) from exc
    return {"job_id": job_id, "status": job["status"], "error": job.get("error"), "procedure_link": link,
            "event_log": manifest.get("event_log", []), "balances": manifest.get("balances"),
            "campaign": manifest.get("campaign"), "notice": NOTICE}


# ---------------------------------------------------------------- bench


def _bench_row(workspace_id: str, draft_id: str, links: list[dict[str, Any]]) -> dict[str, Any]:
    document, revision = _document(workspace_id, draft_id)
    units = [{"tag": item["tag"], "type": item["type"]} for item in document.get("objects", {}).values()
             if item.get("kind") == "unit"]
    by_schedule = {link["schedule_id"]: link for link in links if link["draft_id"] == draft_id}
    return {"draft_id": draft_id, "name": document["name"], "revision": revision, "units": units,
            "tanks": [item["tag"] for item in units if item["type"] == "HoldupTank"],
            "scenarios": [{"id": key, "schedule_id": value.get("schedule_id"), "units": value.get("units", []),
                           "start_utc": value.get("start_utc"), "end_utc": value.get("end_utc")}
                          for key, value in document.get("scenarios", {}).items()],
            "schedules": [{"id": key, "event_count": len(value.get("events", [])), "link": by_schedule.get(key)}
                          for key, value in document.get("schedules", {}).items()],
            "notice": NOTICE}


def bench_drafts(workspace_id: str) -> list[dict[str, Any]]:
    try:
        heads = draft.list_drafts(workspace_id)
    except draft.DraftError as exc:
        raise _wrap(exc) from exc
    links = list_links(workspace_id)
    return [_bench_row(workspace_id, head["draft_id"], links) for head in heads]


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str = Field(default="bench", pattern=SCHEDULE_ID_PATTERN)
    profile_id: str = Field(min_length=1, max_length=200)
    days: float = Field(default=7.0, gt=0, le=120)
    output_cadence_s: float = Field(default=3600.0, ge=60)
    units: list[str] | None = Field(default=None, min_length=1, max_length=12)
    temperature_source: str = Field(default="unit_mean", pattern="^(sea_temperature|air_temperature|unit_mean)$")
    par_scale: float = Field(default=1.0, ge=0)
    circulation: dict[str, float] = Field(default_factory=dict)
    initial: dict[str, dict[str, float]] = Field(default_factory=dict)


def create_scenario(workspace_id: str, draft_id: str, request: ScenarioRequest) -> dict[str, Any]:
    from datetime import datetime, timedelta

    from app.modules.environment import profiles

    document, revision = _document(workspace_id, draft_id)
    if request.scenario_id in document.get("scenarios", {}):
        raise StudioError("studio_procedure_scenario_exists", "A scenario with this id already exists.", status=409)
    profile = next((item for item in profiles.list_profiles(workspace_id) if item["profile_id"] == request.profile_id), None)
    if profile is None or "integrity_error" in profile:
        raise StudioError("studio_procedure_profile_not_found", "Environment profile not found.")
    start = datetime.fromisoformat(profile["start"].replace("Z", "+00:00"))
    end = start + timedelta(days=request.days)
    tags = request.units or [item["tag"] for item in document.get("objects", {}).values()
                             if item.get("kind") == "unit" and item.get("type") in {"PhotobioreactorT1", "HoldupTank"}]
    value: dict[str, Any] = {
        "units": tags, "profiles": [{"profile_id": profile["profile_id"], "digest": profile["digest"]}],
        "start_utc": start.isoformat().replace("+00:00", "Z"), "end_utc": end.isoformat().replace("+00:00", "Z"),
        "output_cadence_s": request.output_cadence_s, "temperature_source": request.temperature_source,
        "par_scale": request.par_scale, "circulation": request.circulation, "initial": request.initial}
    try:
        draft.patch(workspace_id, draft_id, revision, [SetScenario(op="set_scenario", id=request.scenario_id, value=value)],
                    actor="studio-procedure")
    except draft.DraftError as exc:
        raise _wrap(exc) from exc
    row = _bench_row(workspace_id, draft_id, list_links(workspace_id, draft_id))
    try:
        dynamic_engine.prepare(workspace_id, draft_id, request.scenario_id)
        row["scenario_check"] = {"ok": True}
    except dynamic_engine.DynamicError as exc:
        row["scenario_check"] = {"ok": False, "code": exc.code, "message": exc.message}
    return row
