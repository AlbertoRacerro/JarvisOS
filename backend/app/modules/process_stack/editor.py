"""Revision store and DWSIM-backed editor commands."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from xml.etree import ElementTree

from app.core.paths import build_paths
from app.modules.engineering.refs import Quantity
from app.modules.process_stack._common import EvaluationRefusal, magnitude
from app.modules.process_stack.dwsim import _configured_runtime, _connector_records, _mass_balance, _version
from app.modules.process_stack.dwsim_mcp import DwsimMcpClient, DwsimMcpError
from app.modules.process_stack.editor_models import (
    AddCompounds,
    Connect,
    ControllerSet,
    CreateEnergyStream,
    CreateMaterialStream,
    CreateUnit,
    DeleteObject,
    Disconnect,
    DynamicsProjectionRead,
    DynamicsRun,
    EditorCaseRead,
    EditorCommand,
    EditorConnectionRead,
    EditorObjectRead,
    EditorProjectionRead,
    EditorQuantity,
    EventAdd,
    EventRemove,
    Move,
    Rename,
    RevisionRead,
    SetPropertyPackage,
    SetStreamConditions,
    SetUnitProperties,
    Solve,
    StateRestore,
    StateSave,
)
from app.modules.workspaces.service import get_workspace

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_TOL = 1e-8
MAX_IMPORT_BYTES = 64 * 1024 * 1024
MAX_DYNAMIC_DURATION_S = 3600
MAX_DYNAMIC_WALL_TIME_S = 120
MAX_DYNAMIC_STEPS = 20000
MAX_DYNAMIC_POINTS = 200
SUPPORTED = [
    "create_unit",
    "create_material_stream",
    "create_energy_stream",
    "connect",
    "move",
    "rename",
    "set_stream_conditions",
    "set_unit_properties",
    "add_compounds",
    "set_property_package",
    "solve",
    "controller_set",
    "event_add",
    "event_remove",
    "dynamics_run",
    "state_save",
    "state_restore",
]
UNSUPPORTED = {
    "delete_object": "DWSIM MCP has no simulation-object delete API; graphic_remove leaves an orphaned simulation object.",
    "disconnect": "DWSIM MCP has no connector detach API; graphic edits do not safely detach the process graph.",
}


class EditorError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 422, current_revision: str | None = None):
        super().__init__(message)
        self.code, self.status, self.current_revision = code, status, current_revision


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _directory(workspace_id: str, case_id: str | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", workspace_id) or workspace_id in {".", ".."} or (
        get_workspace(workspace_id) is None
    ):
        raise EditorError("workspace_not_found", "Workspace was not found", 404)
    if case_id is not None and not re.fullmatch(r"[a-f0-9-]{36}", case_id):
        raise EditorError("case_not_found", "DWSIM case was not found", 404)
    path = build_paths().workspaces_dir / workspace_id / "process" / "dwsim"
    return path / case_id if case_id else path


@contextmanager
def _lock(directory: Path) -> Iterator[None]:
    key = str(directory.resolve())
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        directory.mkdir(parents=True, exist_ok=True)
        handle = (directory / ".writer.lock").open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _runtime() -> tuple[Path, str, str]:
    path, digest, reason = _configured_runtime()
    if reason or path is None:
        raise EditorError(reason or "DWSIM_MCP_EXECUTABLE_UNAVAILABLE", "DWSIM MCP runtime is unavailable", 503)
    return path, digest, _version(path)


def _client() -> tuple[DwsimMcpClient, str, str]:
    path, digest, version = _runtime()
    return DwsimMcpClient(path, digest), digest, version


def _head(directory: Path) -> dict[str, Any]:
    try:
        return json.loads((directory / "head.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EditorError("case_not_found", "DWSIM case was not found", 404) from exc


def _revision(head: dict[str, Any]) -> str:
    return f"{head['case_sha256']}:{head['seq']}"


def _case_file(directory: Path, seq: int, digest: str) -> Path:
    found = list((directory / "revisions").glob(f"{seq}-{digest}.*"))
    if not found:
        raise EditorError("revision_not_found", "DWSIM revision file is missing", 404)
    if _sha(found[0]) != digest:
        raise EditorError("revision_integrity_error", "DWSIM revision file failed its content hash check", 409)
    return found[0]


def _write_revision(
    directory: Path,
    source: Path,
    *,
    command: str,
    parent: str | None,
    readback: dict[str, Any],
    version: str,
    mcp_sha: str,
    command_digest: str | None = None,
) -> dict[str, Any]:
    prior = _head(directory) if (directory / "head.json").exists() else None
    seq = prior["seq"] + 1 if prior else 1
    digest = _sha(source)
    suffix = ".dwxml" if source.suffix.lower() == ".dwxml" else ".dwxmz"
    revisions = directory / "revisions"
    revisions.mkdir(parents=True, exist_ok=True)
    destination = revisions / f"{seq}-{digest}{suffix}"
    if not destination.exists():
        with source.open("rb") as incoming, tempfile.NamedTemporaryFile(dir=revisions, delete=False) as outgoing:
            tmp = outgoing.name
            while block := incoming.read(1024 * 1024):
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(tmp, destination)
    record = {
        "seq": seq,
        "case_sha256": digest,
        "revision": f"{digest}:{seq}",
        "parent_revision": parent,
        "command_kind": command,
        "command_sha256": command_digest or hashlib.sha256(command.encode()).hexdigest(),
        "readback": readback,
        "created_at": datetime.now(UTC).isoformat(),
        "dwsim_version": version,
        "mcp_sha256": mcp_sha,
        "last_solve": readback if command == "solve" else (prior.get("last_solve") if prior else None),
        "last_dynamic_run": readback if command == "dynamics_run" else (prior.get("last_dynamic_run") if prior else None),
    }
    record_dir = directory / "records"
    record_dir.mkdir(exist_ok=True)
    rec_tmp = record_dir / f".{seq}.tmp"
    rec_tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.replace(rec_tmp, record_dir / f"{seq}.json")
    head_tmp = directory / f".head-{uuid4().hex}.tmp"
    head_tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    with head_tmp.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(head_tmp, directory / "head.json")
    return record


def _load(client: DwsimMcpClient, case: Path) -> str:
    result = client.call("dwsim_flowsheet_load", {"filepath": str(case)}, 60)
    if not isinstance(result.get("flowsheet_id"), str):
        raise EditorError("DWSIM_LOAD_FAILED", "DWSIM did not return a flowsheet handle", 502)
    return result["flowsheet_id"]


def _save(client: DwsimMcpClient, flow: str, path: Path) -> Path:
    client.call(
        "dwsim_flowsheet_save", {"flowsheet_id": flow, "filepath": str(path), "compressed": path.suffix == ".dwxmz"}, 60
    )
    if not path.is_file() or path.stat().st_size == 0:
        raise EditorError("DWSIM_SAVE_FAILED", "DWSIM did not save a case", 502)
    return path


def _objects(client: DwsimMcpClient, flow: str) -> list[dict[str, Any]]:
    result = client.call("dwsim_flowsheet_list_objects", {"flowsheet_id": flow}, 30)
    if not isinstance(result.get("objects"), list):
        raise EditorError("DWSIM_OBJECT_READBACK_INVALID", "DWSIM object read-back is malformed", 502)
    return [item for item in result["objects"] if isinstance(item, dict)]


def _object(client: DwsimMcpClient, flow: str, tag: str, typ: str | None = None) -> dict[str, Any]:
    found = [item for item in _objects(client, flow) if item.get("name") == tag]
    if len(found) != 1 or (typ and found[0].get("type") != typ):
        raise EditorError("DWSIM_READBACK_MISMATCH", "DWSIM object read-back did not match", 502)
    return found[0]


def _verify_position(item: dict[str, Any], x: int, y: int) -> None:
    if float(item.get("x", -1)) != x or float(item.get("y", -1)) != y:
        raise EditorError("DWSIM_READBACK_MISMATCH", "DWSIM position read-back did not match", 502)


def _verify_stream(readback: dict[str, Any], args: dict[str, Any]) -> None:
    for key, expected in args.items():
        if key.endswith(("_K", "_Pa", "_kg_s", "_mol_s")) and (
            not isinstance(readback.get(key), (int, float))
            or abs(float(readback[key]) - expected) > max(1e-9, abs(expected) * _TOL)
        ):
            raise EditorError(
                "DWSIM_READBACK_MISMATCH",
                f"DWSIM {key} read-back did not match (requested {expected!r}, received {readback.get(key)!r})",
                502,
            )


def _q(value: EditorQuantity | None, unit: str) -> float | None:
    if value is None:
        return None
    try:
        return magnitude(Quantity.model_validate(value.model_dump()), unit)
    except EvaluationRefusal as exc:
        raise EditorError("quantity_invalid", "Quantity unit is unknown or dimensionally incompatible", 422) from exc


def _same(actual: object, expected: object) -> bool:
    if isinstance(expected, bool) or not isinstance(expected, (int, float, str)):
        return actual == expected
    if not isinstance(actual, (int, float, str)) or isinstance(actual, bool):
        return actual == expected
    try:
        return math.isclose(float(actual), float(expected), rel_tol=_TOL, abs_tol=1e-9)
    except ValueError:
        return actual == expected


def _controller_mismatch(controllers: object, tag: str, requested: dict[str, Any]) -> str | None:
    item = next(
        (entry for entry in controllers if isinstance(entry, dict) and entry.get("tag") == tag),
        None,
    ) if isinstance(controllers, list) else None
    if item is None:
        return "controller"
    return next(
        (key for key, value in requested.items() if not _same(item.get("manual" if key == "manual_override" else key), value)),
        None,
    )


def _snapshot_mismatch(actual: dict[str, dict[str, str]], expected: dict[str, dict[str, str]]) -> str | None:
    for tag, properties in expected.items():
        for property_id, value in properties.items():
            if not _same(actual.get(tag, {}).get(property_id), value):
                return f"{tag}.{property_id}"
    return None


def _event_list(client: DwsimMcpClient, flow: str, event_set: str, schedule: str | None) -> list[Any]:
    listed = client.call(
        "dwsim_dynamics_event",
        {"flowsheet_id": flow, "action": "list", "event_set": event_set, **({"schedule": schedule} if schedule else {})},
        30,
    )
    events = listed.get("events")
    if not isinstance(events, list):
        raise EditorError("DWSIM_READBACK_MISMATCH", "DWSIM event list is unavailable", 502)
    return events


def _event_count(events: list[Any], description: str) -> int:
    return sum(description in str(item) for item in events)


def _dynamic_values(client: DwsimMcpClient, flow: str, tags: list[str]) -> dict[str, dict[str, str]]:
    values: dict[str, dict[str, str]] = {}
    for tag in tags:
        response = client.call("dwsim_dynamics_properties", {"flowsheet_id": flow, "tag": tag}, 30)
        properties = response.get("properties")
        if not isinstance(properties, list):
            raise EditorError("DWSIM_DYNAMIC_READBACK_INVALID", "DWSIM dynamic properties are unavailable", 502)
        values[tag] = {
            str(item["id"]): str(item["value"])
            for item in properties
            if isinstance(item, dict) and item.get("id") is not None and item.get("value") is not None
        }
    return values


def _saved_state_snapshot(directory: Path, name: str) -> dict[str, dict[str, str]] | None:
    head = _head(directory)
    for seq in range(head["seq"], 0, -1):
        record_path = directory / "records" / f"{seq}.json"
        if not record_path.exists():
            continue
        try:
            readback = json.loads(record_path.read_text(encoding="utf-8")).get("readback", {})
        except (OSError, json.JSONDecodeError):
            continue
        if readback.get("state_name") == name and isinstance(readback.get("state_snapshot"), dict):
            return readback["state_snapshot"]
    return None


def _dynamic_projection(client: DwsimMcpClient, flow: str, record: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"controllers": [], "event_sets": [], "saved_states": [], "last_dynamic_run": None}
    try:
        config = client.call("dwsim_dynamics_inspect", {"flowsheet_id": flow, "detail": "full"}, 30)
        controllers = client.call("dwsim_dynamics_controller", {"flowsheet_id": flow, "action": "list"}, 30)
        states = client.call("dwsim_dynamics_state", {"flowsheet_id": flow, "action": "list"}, 30)
        if any("error" in response for response in (config, controllers, states)):
            raise DwsimMcpError("DWSIM dynamics read returned an error")
        missing: list[str] = []
        if not isinstance(controllers.get("controllers"), list):
            missing.append("controllers")
        if not isinstance(states.get("stored_states"), list):
            missing.append("saved states")
        if not isinstance(config.get("event_sets"), list):
            missing.append("event sets")
        if isinstance(controllers.get("controllers"), list):
            result["controllers"] = controllers["controllers"]
        if isinstance(states.get("stored_states"), list):
            result["saved_states"] = [str(item) for item in states["stored_states"]]
        schedule = config.get("current_schedule")
        result["current_schedule"] = schedule if isinstance(schedule, str) else None
        sets = config.get("event_sets", [])
        if isinstance(sets, list):
            for event_set in sets[:25]:
                if isinstance(event_set, str):
                    events = client.call("dwsim_dynamics_event", {
                        "flowsheet_id": flow, "action": "list", "event_set": event_set,
                        **({"schedule": schedule} if isinstance(schedule, str) else {}),
                    }, 30)
                    result["event_sets"].append(events)
        result["last_dynamic_run"] = record.get("last_dynamic_run")
        if missing:
            result["unavailable_reason"] = "DWSIM did not provide: " + ", ".join(missing)
        return result
    except DwsimMcpError as exc:
        result["unavailable_reason"] = f"DWSIM dynamics projection unavailable ({type(exc).__name__})"
        return result


def _apply(client: DwsimMcpClient, flow: str, command: EditorCommand, scratch: Path, directory: Path) -> dict[str, Any]:
    readback: dict[str, Any]
    if isinstance(command, CreateUnit):
        client.call("dwsim_unitop_add", {"flowsheet_id": flow, "type": command.unit_type, "name": command.tag}, 30)
        client.call(
            "dwsim_graphic_edit", {"flowsheet_id": flow, "name": command.tag, "x": command.x, "y": command.y}, 30
        )
        readback = _object(client, flow, command.tag)
        _verify_position(readback, command.x, command.y)
    elif isinstance(command, CreateMaterialStream):
        args: dict[str, Any] = {"flowsheet_id": flow, "name": command.tag}
        for value, unit, key in (
            (command.temperature, "K", "temperature_K"),
            (command.pressure, "Pa", "pressure_Pa"),
            (command.mass_flow, "kg/s", "mass_flow_kg_s"),
        ):
            converted = _q(value, unit)
            if converted is not None:
                args[key] = converted
        if command.composition is not None:
            args["composition"] = command.composition
        client.call("dwsim_stream_add_material", args, 30)
        client.call(
            "dwsim_graphic_edit", {"flowsheet_id": flow, "name": command.tag, "x": command.x, "y": command.y}, 30
        )
        readback = _object(client, flow, command.tag, "MaterialStream")
        _verify_position(readback, command.x, command.y)
        readback.update(client.call("dwsim_stream_get_results", {"flowsheet_id": flow, "name": command.tag}, 30))
        _verify_stream(readback, args)
    elif isinstance(command, CreateEnergyStream):
        client.call("dwsim_stream_add_energy", {"flowsheet_id": flow, "name": command.tag}, 30)
        client.call(
            "dwsim_graphic_edit", {"flowsheet_id": flow, "name": command.tag, "x": command.x, "y": command.y}, 30
        )
        readback = _object(client, flow, command.tag, "EnergyStream")
        _verify_position(readback, command.x, command.y)
    elif isinstance(command, Move):
        client.call(
            "dwsim_graphic_edit", {"flowsheet_id": flow, "name": command.object, "x": command.x, "y": command.y}, 30
        )
        readback = _object(client, flow, command.object)
        if (int(readback.get("x", -1)), int(readback.get("y", -1))) != (command.x, command.y):
            raise EditorError("DWSIM_READBACK_MISMATCH", "DWSIM position read-back did not match", 502)
    elif isinstance(command, Rename):
        client.call(
            "dwsim_object_rename", {"flowsheet_id": flow, "name": command.object, "new_name": command.new_tag}, 30
        )
        readback = _object(client, flow, command.new_tag)
        if command.new_tag != command.object and any(
            item.get("name") == command.object for item in _objects(client, flow)
        ):
            raise EditorError("DWSIM_READBACK_MISMATCH", "DWSIM still reports the previous tag", 502)
    elif isinstance(command, Connect):
        connector_fields = {
            "feed": ("feed_stream", "feed_port"),
            "product": ("product_stream", "product_port"),
            "energy_feed": ("energy_feed", "energy_feed_port"),
            "energy_product": ("energy_product", "energy_product_port"),
        }[command.role]
        client.call(
            "dwsim_unitop_connect",
            {
                "flowsheet_id": flow,
                "unitop": command.unit,
                connector_fields[0]: command.stream,
                connector_fields[1]: command.port,
            },
            30,
        )
        readback = {"unit": _object(client, flow, command.unit), "stream": _object(client, flow, command.stream)}
    elif isinstance(command, SetStreamConditions):
        args = {"flowsheet_id": flow, "name": command.stream}
        for value, unit, key in (
            (command.temperature, "K", "temperature_K"),
            (command.pressure, "Pa", "pressure_Pa"),
            (command.mass_flow, "kg/s", "mass_flow_kg_s"),
            (command.molar_flow, "mol/s", "molar_flow_mol_s"),
        ):
            converted = _q(value, unit)
            if converted is not None:
                args[key] = converted
        if command.composition is not None:
            args["composition"] = command.composition
        if len(args) == 2:
            raise EditorError("empty_command", "At least one stream value is required")
        client.call("dwsim_stream_set_conditions", args, 30)
        readback = client.call("dwsim_stream_get_results", {"flowsheet_id": flow, "name": command.stream}, 30)
        _verify_stream(readback, args)
    elif isinstance(command, SetUnitProperties):
        client.call(
            "dwsim_unitop_set", {"flowsheet_id": flow, "name": command.unit, "properties": command.properties}, 30
        )
        readback = client.call("dwsim_unitop_get_results", {"flowsheet_id": flow, "name": command.unit}, 30)
        if any(readback.get(key) != value for key, value in command.properties.items()):
            raise EditorError("DWSIM_READBACK_MISMATCH", "DWSIM unit property read-back did not match", 502)
    elif isinstance(command, AddCompounds):
        readback = client.call("dwsim_thermo_add_compounds", {"flowsheet_id": flow, "names": command.compounds}, 30)
    elif isinstance(command, SetPropertyPackage):
        client.call("dwsim_thermo_set_property_package", {"flowsheet_id": flow, "name": command.name}, 30)
        readback = {"property_packages": client.call("dwsim_thermo_list_property_packages", {"flowsheet_id": flow}, 30)}
    elif isinstance(command, Solve):
        readback = {
            "check": client.call("dwsim_flowsheet_check", {"flowsheet_id": flow}, 30),
            "solve": client.call("dwsim_solve_run", {"flowsheet_id": flow}, 180),
        }
    elif isinstance(command, ControllerSet):
        requested = {key: value for key, value in command.model_dump(exclude={"kind", "expected_revision", "tag"}).items() if value is not None}
        if not requested:
            raise EditorError("empty_command", "At least one controller field is required")
        client.call("dwsim_dynamics_controller", {"flowsheet_id": flow, "action": "set", "tag": command.tag, **requested}, 30)
        listed = client.call("dwsim_dynamics_controller", {"flowsheet_id": flow, "action": "list"}, 30).get("controllers")
        mismatch = _controller_mismatch(listed, command.tag, requested)
        if mismatch:
            raise EditorError("DWSIM_READBACK_MISMATCH", f"Controller {mismatch} read-back did not match", 502)
        assert isinstance(listed, list)
        match = next(item for item in listed if isinstance(item, dict) and item.get("tag") == command.tag)
        readback = {"controller": match, "requested": requested}
    elif isinstance(command, EventAdd | EventRemove):
        adding = isinstance(command, EventAdd)
        args = {"flowsheet_id": flow, "action": "add" if adding else "remove", "event_set": command.event_set}
        if command.schedule:
            args["schedule"] = command.schedule
        if isinstance(command, EventAdd):
            description: str = command.description or f"{command.tag}.{command.property} at {command.at_s:g} s"
            args.update({"tag": command.tag, "property": command.property, "value": command.value, "at_s": command.at_s,
                         "transition": command.transition, "description": description})
            if command.units:
                args["units"] = command.units
        else:
            assert isinstance(command, EventRemove)
            description = command.description
            args["description"] = description
        before = _event_count(_event_list(client, flow, command.event_set, command.schedule), description)
        if not adding and before == 0:
            raise EditorError("event_not_found", "No event with this description exists in the event set", 404)
        client.call("dwsim_dynamics_event", args, 30)
        events = _event_list(client, flow, command.event_set, command.schedule)
        after = _event_count(events, description)
        if (adding and after != before + 1) or (not adding and after >= before):
            raise EditorError("DWSIM_READBACK_MISMATCH", "DWSIM event list did not match the requested change", 502)
        readback = {"event_set": command.event_set, "schedule": command.schedule,
                    "description" if adding else "removed": description, "matching_events": after, "events": events}
    elif isinstance(command, DynamicsRun):
        if command.step_s is not None or command.integrator is not None or command.method is not None:
            schedule = command.schedule
            config = client.call("dwsim_dynamics_inspect", {"flowsheet_id": flow, "detail": "config"}, 30)
            schedule = schedule or config.get("current_schedule")
            if not isinstance(schedule, str) or not schedule:
                raise EditorError("DWSIM_DYNAMIC_SCHEDULE_UNAVAILABLE", "No current dynamics schedule is available", 422)
            checked = client.call("dwsim_dynamics_check", {"flowsheet_id": flow, "schedule": schedule}, 30)
            step_s = command.step_s if command.step_s is not None else checked.get("step_s")
            if not isinstance(step_s, (int, float)) or step_s <= 0:
                raise EditorError("DWSIM_DYNAMIC_STEP_UNAVAILABLE", "DWSIM did not report a valid integration step", 422)
            setup = {"flowsheet_id": flow, "schedule": schedule,
                     "step_s": step_s, "duration_s": command.duration_s,
                     "enable_dynamic_mode": True, "make_current": True}
            if command.integrator:
                setup["integrator"] = command.integrator
            if command.method:
                setup["method"] = command.method
            client.call("dwsim_dynamics_setup", setup, 30)
        args = {"flowsheet_id": flow, "duration_s": command.duration_s, "max_wall_time_s": command.max_wall_time_s,
                "max_steps": command.max_steps, "wait": True}
        if command.schedule:
            args["schedule"] = command.schedule
        started = client.call("dwsim_dynamics_run", args, command.max_wall_time_s + 30)
        run_id = started.get("run_id")
        if not isinstance(run_id, str):
            raise EditorError("DWSIM_DYNAMIC_RUN_INVALID", "DWSIM did not return a dynamic run id", 502)
        status = client.call("dwsim_dynamics_status", {"run_id": run_id, "include_summary": True}, 30)
        series_args: dict[str, Any] = {"run_id": run_id, "max_points": MAX_DYNAMIC_POINTS}
        if command.variables:
            series_args["variables"] = command.variables
        series = client.call("dwsim_dynamics_series", series_args, 30)
        raw_times = series.get("t_s", [])
        times = [float(value) for value in raw_times if isinstance(value, (str, int, float))]
        if not times or len(times) > MAX_DYNAMIC_POINTS:
            raise EditorError("DWSIM_READBACK_MISMATCH", "DWSIM returned no bounded dynamic time-series", 502)
        state = status.get("state", started.get("state"))
        if not isinstance(state, str):
            raise EditorError("DWSIM_READBACK_MISMATCH", "DWSIM returned no dynamic run status", 502)
        summary = status.get("summary") or started.get("summary") or {}
        summary_end = summary.get("simulated_s") if isinstance(summary, dict) else None
        series_end = times[-1] if times else None
        readback = {"run_id": run_id, "status": state,
                    "requested_duration_s": command.duration_s, "simulated_end_s": status.get("simulated_s", started.get("simulated_s")),
                    "summary_simulated_end_s": summary_end, "series_end_s": series_end,
                    "time_reconciliation": "match" if series_end is not None and summary_end is not None and math.isclose(series_end, float(summary_end), abs_tol=1e-9) else "mismatch_or_unavailable",
                    "summary": summary, "series": series,
                    "objects": [{"tag": item.get("name"), "calculated": item.get("calculated"), "errors": item.get("error", "")} for item in _objects(client, flow)]}
    elif isinstance(command, StateSave):
        config = client.call("dwsim_dynamics_inspect", {"flowsheet_id": flow, "detail": "objects"}, 30)
        tags = [str(item["tag"]) for item in config.get("objects", []) if isinstance(item, dict) and item.get("tag")]
        snapshot = _dynamic_values(client, flow, tags)
        client.call("dwsim_dynamics_state", {"flowsheet_id": flow, "action": "save", "name": command.name}, 30)
        listed = client.call("dwsim_dynamics_state", {"flowsheet_id": flow, "action": "list"}, 30)
        if command.name not in listed.get("stored_states", []):
            raise EditorError("DWSIM_READBACK_MISMATCH", "Saved state was absent from DWSIM state list", 502)
        readback = {"state_name": command.name, "state_snapshot": snapshot, "stored_states": listed.get("stored_states", [])}
    elif isinstance(command, StateRestore):
        expected = _saved_state_snapshot(directory, command.name)
        if expected is None:
            raise EditorError("state_snapshot_unavailable", "No revisioned read-back snapshot exists for this state", 422)
        client.call("dwsim_dynamics_state", {"flowsheet_id": flow, "action": "restore", "name": command.name}, 30)
        actual = _dynamic_values(client, flow, list(expected))
        mismatch = _snapshot_mismatch(actual, expected)
        if mismatch:
            raise EditorError("DWSIM_READBACK_MISMATCH", f"Restored state did not restore {mismatch}", 502)
        readback = {"restored_state": command.name, "state_snapshot": actual, "verified_objects": len(expected)}
    elif isinstance(command, DeleteObject | Disconnect):
        raise EditorError("unsupported_upstream", UNSUPPORTED[command.kind], 422)
    else:
        raise EditorError("unsupported_upstream", "This command is unsupported by the DWSIM MCP runtime", 422)
    _save(client, flow, scratch)
    if isinstance(command, Connect):
        _native, connector_tags, available = _connector_records(scratch)
        objects = _objects(client, flow)
        unit_id = next((item.get("id") for item in objects if item.get("name") == command.unit), None)
        stream_id = next((item.get("id") for item in objects if item.get("name") == command.stream), None)
        connector_group = "input" if command.role == "feed" else "output" if command.role == "product" else "energy"
        direct = (connector_tags.get(command.unit) or {}).get(connector_group, [])
        opposite = (connector_tags.get(command.stream) or {}).get("output" if command.role == "feed" else "input", [])
        attached = direct[command.port] if command.port < len(direct) else None
        reciprocal = next(
            (
                item
                for item in opposite
                if item.get("native_object_id") == unit_id and item.get("native_connector_index") == command.port
            ),
            None,
        )
        connected = bool(attached and attached["attached"] and attached.get("native_object_id") == stream_id) or bool(
            reciprocal and reciprocal["attached"]
        )
        if not available or not unit_id or not stream_id or not connected:
            raise EditorError(
                "DWSIM_READBACK_MISMATCH", "Native connector read-back did not show the requested connection", 502
            )
        readback["native_connector_readback"] = True
    if isinstance(command, CreateUnit):
        metadata = _native_metadata(scratch).get(str(readback.get("id")))
        if not metadata or not str(metadata.get("simulation_type", "")).endswith(f".{command.unit_type}"):
            raise EditorError("DWSIM_READBACK_MISMATCH", "Saved DWSIM unit type did not match", 502)
    if isinstance(command, SetPropertyPackage):
        identity = _package(scratch)
        if not identity or command.name.casefold() not in identity.casefold():
            raise EditorError("DWSIM_READBACK_MISMATCH", "Saved property-package identity did not match", 502)
        readback["property_package"] = identity
    if isinstance(command, AddCompounds):
        compounds = _compound_names(scratch)
        if not set(command.compounds).issubset(compounds):
            raise EditorError("DWSIM_READBACK_MISMATCH", "Saved native compound identity did not match", 502)
        readback["saved_compounds"] = compounds
    if isinstance(command, Solve):
        objects = _objects(client, flow)
        try:
            # list_objects names the tag "name"; the shared balance helper keys objects by "tag".
            tagged = [{**item, "tag": item.get("name")} for item in objects]
            residual, boundary = _mass_balance(client, flow, scratch, tagged)
            readback["mass_balance_residual_kg_s"] = residual
            readback["boundary_mass_flows_kg_s"] = boundary
            readback["mass_balance_status"] = "calculated"
        except Exception as exc:
            readback["mass_balance_status"] = "unavailable"
            readback["mass_balance_error"] = getattr(exc, "code", type(exc).__name__)
        solve_result = readback["solve"]
        diagnostics = [solve_result.get(key) for key in ("blockers", "errors")]
        result_objects = solve_result.get("objects", [])
        object_failed = isinstance(result_objects, list) and any(
            isinstance(item, dict) and (item.get("calculated") is False or item.get("error")) for item in result_objects
        )
        readback["solve_status"] = (
            "completed"
            if solve_result.get("ok") is True
            and not any(isinstance(items, list) and items for items in diagnostics)
            and not object_failed
            else "failed"
        )
        readback["object_calculation"] = [
            {"tag": item.get("name"), "calculated": item.get("calculated"), "errors": item.get("error", "")}
            for item in objects
        ]
    return readback


def _package(path: Path) -> str | None:
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                xml = next(n for n in archive.namelist() if n.lower().endswith(".xml"))
                root = ElementTree.fromstring(archive.read(xml))
        else:
            root = ElementTree.parse(path).getroot()
        node = root.find(".//PropertyPackages/*")
        return (
            (node.get("Name") or node.findtext("Name") or node.findtext("ComponentName") or node.findtext("Tag"))
            if node is not None
            else None
        )
    except (OSError, zipfile.BadZipFile, StopIteration, ElementTree.ParseError):
        return None


def _compound_names(path: Path) -> list[str]:
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                xml = next(n for n in archive.namelist() if n.lower().endswith(".xml"))
                root = ElementTree.fromstring(archive.read(xml))
        else:
            root = ElementTree.parse(path).getroot()
        return [name.text for name in root.findall("./Compounds/Compound/Name") if name.text]
    except (OSError, zipfile.BadZipFile, StopIteration, ElementTree.ParseError):
        return []


def _native_metadata(path: Path) -> dict[str, dict[str, Any]]:
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                xml = next(n for n in archive.namelist() if n.lower().endswith(".xml"))
                root = ElementTree.fromstring(archive.read(xml))
        else:
            root = ElementTree.parse(path).getroot()
        simulation_types = {
            node.findtext("ComponentName"): node.findtext("Type")
            for node in root.findall("./SimulationObjects/SimulationObject")
            if node.findtext("ComponentName")
        }
        result = {}
        for node in root.findall("./GraphicObjects/GraphicObject"):
            native_id = node.findtext("Name")
            if native_id:
                result[native_id] = {
                    "tag": node.findtext("Tag"),
                    "graphic_type": node.findtext("Type"),
                    "simulation_type": simulation_types.get(native_id),
                    "x": float(node.findtext("X") or 0),
                    "y": float(node.findtext("Y") or 0),
                    "width": float(node.findtext("Width") or 0),
                    "height": float(node.findtext("Height") or 0),
                }
        return result
    except (OSError, zipfile.BadZipFile, StopIteration, ElementTree.ParseError, ValueError):
        return {}


def _projection(
    workspace_id: str, case_id: str, path: Path, record: dict[str, Any], client: DwsimMcpClient
) -> EditorProjectionRead:
    flow = _load(client, path)
    by_id, _by_tag, _ok = _connector_records(path)
    metadata = _native_metadata(path)
    projected: list[EditorObjectRead] = []
    connections: list[EditorConnectionRead] = []
    for item in _objects(client, flow):
        native = metadata.get(str(item.get("id")), {})
        typ = str(native.get("simulation_type") or item.get("type") or "")
        typ = typ.rsplit(".", 1)[-1]
        category: Literal["unit", "material_stream", "energy_stream", "other"] = (
            "material_stream"
            if typ == "MaterialStream"
            else "energy_stream"
            if typ == "EnergyStream"
            else "unit"
            if typ and "Stream" not in typ
            else "other"
        )
        result = None
        if category == "material_stream":
            result = client.call("dwsim_stream_get_results", {"flowsheet_id": flow, "name": item.get("name")}, 30)
        elif category == "unit":
            try:
                result = client.call("dwsim_unitop_get_results", {"flowsheet_id": flow, "name": item.get("name")}, 30)
            except DwsimMcpError:
                pass
        projected.append(
            EditorObjectRead(
                native_id=item.get("id"),
                tag=item.get("name"),
                type=typ,
                category=category,
                x=native.get("x", item.get("x")),
                y=native.get("y", item.get("y")),
                width=native.get("width", item.get("width")),
                height=native.get("height", item.get("height")),
                calculated=item.get("calculated"),
                errors=str(item.get("error") or ""),
                results=result,
            )
        )
    emitted: set[tuple[str, int, str, int]] = set()
    for source_id, groups in by_id.items():
        for direction in ("input", "output", "energy"):
            for conn in groups.get(direction, []):
                target_id = conn.get("native_object_id")
                target_port = conn.get("native_connector_index")
                if not conn["attached"] or not target_id:
                    continue
                if direction == "input":
                    edge = (target_id, target_port or 0, source_id, conn["port_index"])
                else:
                    edge = (source_id, conn["port_index"], target_id, target_port or 0)
                reverse = (edge[2], edge[3], edge[0], edge[1])
                key = min(edge, reverse)
                if key in emitted:
                    continue
                emitted.add(key)
                connections.append(
                    EditorConnectionRead(
                        source_native_id=source_id,
                        source_port=conn["port_index"],
                        target_native_id=target_id,
                        target_port=target_port or 0,
                        kind="energy" if conn["energy"] or direction == "energy" else "material",
                    )
                )
    summary = client.call("dwsim_flowsheet_summary", {"flowsheet_id": flow}, 30)
    compounds = _compound_names(path) or summary.get("compounds", [])
    compounds = compounds if isinstance(compounds, list) else []
    return EditorProjectionRead(
        workspace_id=workspace_id,
        case_id=case_id,
        revision=record["revision"],
        case_sha256=record["case_sha256"],
        dwsim_version=record["dwsim_version"],
        mcp_sha256=record["mcp_sha256"],
        objects=projected,
        connections=connections,
        compounds=[str(x) for x in compounds],
        property_package=_package(path),
        last_solve=record.get("last_solve"),
        editable_commands=SUPPORTED,
        unsupported_commands=UNSUPPORTED,
        dynamics=DynamicsProjectionRead.model_validate(_dynamic_projection(client, flow, record)),
    )


def _case_read(workspace_id: str, case_id: str, row: dict[str, Any]) -> EditorCaseRead:
    return EditorCaseRead(
        workspace_id=workspace_id,
        case_id=case_id,
        revision=row["revision"],
        case_sha256=row["case_sha256"],
        dwsim_version=row["dwsim_version"],
        created_at=row["created_at"],
    )


def create_case(workspace_id: str, name: str = "DWSIM case") -> EditorCaseRead:
    case_id = str(uuid4())
    directory = _directory(workspace_id, case_id)
    client, digest, version = _client()
    try:
        with client:
            result = client.call("dwsim_flowsheet_create", {"name": name}, 30)
            flow = result.get("flowsheet_id")
            if not isinstance(flow, str):
                raise EditorError("DWSIM_CREATE_FAILED", "DWSIM did not create a flowsheet", 502)
            with tempfile.TemporaryDirectory(prefix="jarvis-dwsim-") as tmp:
                path = _save(client, flow, Path(tmp) / "case.dwxmz")
                with _lock(directory):
                    row = _write_revision(
                        directory,
                        path,
                        command="create_case",
                        parent=None,
                        readback={"object_count": len(_objects(client, flow))},
                        version=version,
                        mcp_sha=digest,
                    )
    except DwsimMcpError as exc:
        raise EditorError("DWSIM_RUNTIME_ERROR", "DWSIM failed to create the case", 502) from exc
    return _case_read(workspace_id, case_id, row)


def import_case(workspace_id: str, filename: str, content: bytes) -> EditorCaseRead:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".dwxml", ".dwxmz"}:
        raise EditorError("invalid_case_format", "Upload a .dwxml or .dwxmz case", 422)
    if not content or len(content) > MAX_IMPORT_BYTES:
        raise EditorError("case_size_invalid", "Uploaded case is empty or larger than 64 MiB", 413)
    case_id = str(uuid4())
    directory = _directory(workspace_id, case_id)
    client, digest, version = _client()
    with tempfile.TemporaryDirectory(prefix="jarvis-dwsim-import-") as tmp:
        original = Path(tmp) / f"upload{suffix}"
        original.write_bytes(content)
        try:
            with client:
                flow = _load(client, original)
                count = len(_objects(client, flow))
                saved = _save(client, flow, Path(tmp) / f"validated{suffix}")
            with _lock(directory):
                row = _write_revision(
                    directory,
                    saved,
                    command="import_case",
                    parent=None,
                    readback={"object_count": count},
                    version=version,
                    mcp_sha=digest,
                )
        except DwsimMcpError as exc:
            raise EditorError("DWSIM_IMPORT_INVALID", "DWSIM could not load the uploaded case", 422) from exc
    return _case_read(workspace_id, case_id, row)


def list_cases(workspace_id: str) -> list[EditorCaseRead]:
    base = _directory(workspace_id)
    rows = []
    for directory in sorted(base.iterdir() if base.exists() else []):
        if directory.is_dir() and (directory / "head.json").exists():
            rows.append(_case_read(workspace_id, directory.name, _head(directory)))
    return rows


def projection(workspace_id: str, case_id: str) -> EditorProjectionRead:
    directory = _directory(workspace_id, case_id)
    row = _head(directory)
    path = _case_file(directory, row["seq"], row["case_sha256"])
    client, _, _ = _client()
    try:
        with client:
            return _projection(workspace_id, case_id, path, row, client)
    except DwsimMcpError as exc:
        raise EditorError("DWSIM_RUNTIME_ERROR", "DWSIM projection read failed", 502) from exc


def list_revisions(workspace_id: str, case_id: str) -> list[RevisionRead]:
    directory = _directory(workspace_id, case_id)
    head = _head(directory)
    result = []
    for seq in range(1, head["seq"] + 1):
        manifest = directory / "records" / f"{seq}.json"
        if not manifest.exists():
            continue
        row = RevisionRead.model_validate_json(manifest.read_text(encoding="utf-8"))
        if list((directory / "revisions").glob(f"{seq}-{row.case_sha256}.*")):
            result.append(row)
    return result


def download_revision(workspace_id: str, case_id: str, revision: str) -> Path:
    directory = _directory(workspace_id, case_id)
    _head(directory)
    match = re.fullmatch(r"([0-9a-f]{64}):(\d+)", revision)
    if not match:
        raise EditorError("revision_not_found", "DWSIM revision was not found", 404)
    return _case_file(directory, int(match.group(2)), match.group(1))


def restore(workspace_id: str, case_id: str, expected_revision: str, source_revision: str) -> dict[str, Any]:
    directory = _directory(workspace_id, case_id)
    with _lock(directory):
        current = _head(directory)
        if expected_revision != current["revision"]:
            raise EditorError("revision_conflict", "Expected revision is stale", 409, current["revision"])
        source = download_revision(workspace_id, case_id, source_revision)
        client, digest, version = _client()
        try:
            with client:
                flow = _load(client, source)
                readback = {"restored_revision": source_revision, "object_count": len(_objects(client, flow))}
        except DwsimMcpError as exc:
            raise EditorError("DWSIM_IMPORT_INVALID", "DWSIM could not reload the selected revision", 422) from exc
        row = _write_revision(
            directory,
            source,
            command="restore",
            parent=current["revision"],
            readback=readback,
            version=version,
            mcp_sha=digest,
        )
    client, _, _ = _client()
    with client:
        projected = _projection(workspace_id, case_id, source, row, client)
    return {"case": _case_read(workspace_id, case_id, row), "projection": projected, "readback": readback}


def _verify_persisted(client: DwsimMcpClient, target: Path, command: EditorCommand, readback: dict[str, Any]) -> None:
    """Reload the saved case and prove the dynamic change survived DWSIM persistence."""
    flow = _load(client, target)
    if isinstance(command, ControllerSet):
        controllers = client.call("dwsim_dynamics_controller", {"flowsheet_id": flow, "action": "list"}, 30).get("controllers")
        if _controller_mismatch(controllers, command.tag, readback["requested"]):
            raise EditorError("DWSIM_PERSISTENCE_MISMATCH", "Saved case did not preserve controller settings", 502)
    elif isinstance(command, EventAdd | EventRemove):
        description = readback.get("description", readback.get("removed", ""))
        events = _event_list(client, flow, command.event_set, command.schedule)
        if _event_count(events, description) != readback["matching_events"]:
            raise EditorError("DWSIM_PERSISTENCE_MISMATCH", "Saved case did not preserve the event change", 502)
    elif isinstance(command, StateSave | StateRestore):
        states = client.call("dwsim_dynamics_state", {"flowsheet_id": flow, "action": "list"}, 30).get("stored_states", [])
        if command.name not in states:
            raise EditorError("DWSIM_PERSISTENCE_MISMATCH", "Saved case did not preserve the dynamic state", 502)
    if isinstance(command, DynamicsRun):
        objects = _objects(client, flow)
        readback["persisted_objects"] = [
            {"tag": item.get("name"), "calculated": item.get("calculated"), "errors": item.get("error", "")}
            for item in objects
        ]
        tags = [str(item["name"]) for item in objects if item.get("name")]
        readback["persisted_dynamic_values"] = _dynamic_values(client, flow, tags)
    if isinstance(command, StateRestore):
        persisted = _dynamic_values(client, flow, list(readback["state_snapshot"]))
        mismatch = _snapshot_mismatch(persisted, readback["state_snapshot"])
        if mismatch:
            raise EditorError("DWSIM_PERSISTENCE_MISMATCH", f"Saved restore did not preserve {mismatch}", 502)
        readback["persisted_state_snapshot"] = persisted


def execute(workspace_id: str, case_id: str, command: EditorCommand) -> dict[str, Any]:
    if isinstance(command, DynamicsRun) and (
        command.duration_s > MAX_DYNAMIC_DURATION_S
        or command.max_wall_time_s > MAX_DYNAMIC_WALL_TIME_S
        or command.max_steps > MAX_DYNAMIC_STEPS
    ):
        raise EditorError("dynamic_run_cap_exceeded", "Dynamic run exceeds a server-side duration, wall-time, or step cap", 422)
    directory = _directory(workspace_id, case_id)
    with _lock(directory):
        current = _head(directory)
        if command.expected_revision != current["revision"]:
            raise EditorError("revision_conflict", "Expected revision is stale", 409, current["revision"])
        source = _case_file(directory, current["seq"], current["case_sha256"])
        client, digest, version = _client()
        with tempfile.TemporaryDirectory(prefix="jarvis-dwsim-edit-") as tmp:
            target = Path(tmp) / f"case{source.suffix}"
            try:
                with client:
                    flow = _load(client, source)
                    readback = _apply(client, flow, command, target, directory)
                    if isinstance(command, ControllerSet | EventAdd | EventRemove | StateSave | StateRestore | DynamicsRun):
                        _verify_persisted(client, target, command, readback)
                row = _write_revision(
                    directory,
                    target,
                    command=command.kind,
                    parent=current["revision"],
                    readback=readback,
                    version=version,
                    mcp_sha=digest,
                    command_digest=hashlib.sha256(
                        json.dumps(command.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                )
            except DwsimMcpError as exc:
                raise EditorError("DWSIM_RUNTIME_ERROR", "DWSIM command failed", 502) from exc
        client, _, _ = _client()
        path = _case_file(directory, row["seq"], row["case_sha256"])
        with client:
            projected = _projection(workspace_id, case_id, path, row, client)
        return {"case": _case_read(workspace_id, case_id, row), "projection": projected, "readback": readback}
