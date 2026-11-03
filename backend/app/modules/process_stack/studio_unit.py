"""Studio custom process units in the Process draft and solve (spec 190).

User code runs only through the 187 runtime. This module resolves pinned published versions, evaluates one unit
per call with the shared host checks, caches within a Process Run and records provenance. It never imports the
mixed runtime, which adapts the plain result below.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.modules.process_stack.draft_models import STUDIO_UNIT, StudioPinError, spec_for
from app.modules.studio import service
from app.modules.studio.unit_checks import (
    HONEST_LIMITS,
    UnitCheckError,
    check_result,
    classification_code,
    host_stream,
)
from app.modules.studio_runtime import runtime

Resolver = Callable[[str], dict[str, Any]]
_ERROR_CODES = {"studio_unit_version_not_found": "STUDIO_UNIT_VERSION_NOT_FOUND",
                "studio_artifact_hash_mismatch": "STUDIO_UNIT_ARTIFACT_MISMATCH"}


class StudioUnitError(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


@dataclass
class Evaluation:
    outlets: dict[str, dict[str, Any]]
    culture_generation: dict[str, float] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)


def studio_units(document: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted((item for item in document["objects"].values()
                   if item["kind"] == "unit" and item["type"] == STUDIO_UNIT), key=lambda item: item["tag"])


def resolver(workspace_id: str) -> Resolver:
    def resolve(version_id: str) -> dict[str, Any]:
        try:
            return service.published_unit(workspace_id, version_id)
        except service.StudioError as exc:
            raise StudioUnitError(_ERROR_CODES.get(exc.code, "STUDIO_UNIT_VERSION_NOT_FOUND"), exc.message) from exc

    return resolve


def pin_fields(resolved: dict[str, Any]) -> dict[str, Any]:
    """What a placed unit stores: the pin and the verified contract it was placed with."""
    return {"model": {"version_id": resolved["version_id"], "source_sha256": resolved["source_sha256"],
                      "contract_sha256": resolved["contract_sha256"]},
            "studio": {"model_spec_id": resolved["model_spec_id"], "name": resolved["name"],
                       "version_label": resolved["version_label"], "version_number": resolved["version_number"],
                       "contract": copy.deepcopy(resolved["contract"])}}


def pin_findings(document: dict[str, Any], workspace_id: str | None, resolve: Resolver | None) -> list[dict[str, Any]]:
    """Per placed StudioUnit: blockers for a pin that does not verify, plus honest-limits and update info."""
    units = studio_units(document)
    findings: list[dict[str, Any]] = []
    if not units:
        return findings
    resolve = resolve or (resolver(workspace_id) if workspace_id else None)
    latest: dict[str, int] = {}
    if workspace_id:
        for version in service.published_units_lineage(workspace_id):
            latest[version["model_spec_id"]] = max(latest.get(version["model_spec_id"], 0), version["version_number"])

    def add(severity: str, code: str, unit: dict[str, Any], message: str) -> None:
        findings.append({"severity": severity, "code": code, "object": unit["tag"], "field": "model",
                         "message": message, "source": "jarvis"})

    for unit in units:
        pin = unit.get("model") or {}
        try:
            spec_for(unit)
        except StudioPinError as exc:
            add("blocker", "STUDIO_UNIT_PIN_INVALID", unit, str(exc))
            continue
        if resolve is not None:
            try:
                resolved = resolve(str(pin.get("version_id")))
            except StudioUnitError as exc:
                add("blocker", exc.code, unit, exc.message)
                continue
            if (resolved["source_sha256"], resolved["contract_sha256"]) != (
                    pin.get("source_sha256"), pin.get("contract_sha256")):
                add("blocker", "STUDIO_UNIT_PIN_MISMATCH", unit,
                    "The pinned hashes do not match the published version's stored artifact.")
                continue
        add("info", "STUDIO_UNIT_LIMITS", unit, HONEST_LIMITS + ".")
        studio = unit.get("studio") or {}
        newer = latest.get(str(studio.get("model_spec_id") or ""), 0)
        if newer > int(studio.get("version_number", 0)):
            add("info", "STUDIO_UNIT_UPDATE_AVAILABLE", unit,
                f"Version {newer} of {studio.get('name')} is published; this unit stays on "
                f"v{studio.get('version_number')} until it is updated.")
    return findings


def preflight(document: dict[str, Any]) -> None:
    """Refuse before any DWSIM work when a StudioUnit is present and the isolated runtime cannot run it."""
    units = studio_units(document)
    state = runtime.status()
    if units and not (state["enabled"] and state["available"]):
        raise StudioUnitError(
            "STUDIO_RUNTIME_UNAVAILABLE",
            f"The Studio runtime is not available ({state['reason']}); {', '.join(u['tag'] for u in units)} "
            "cannot run. Enable it as described in the Studio runtime settings.",
            {"reason": state["reason"], "units": [u["tag"] for u in units]})


def projection(document: dict[str, Any], workspace_id: str) -> dict[str, Any]:
    """Pin status per placed unit for the inspector: pinned version, latest version and whether an update exists."""
    units = studio_units(document)
    if not units:
        return {}
    versions = service.published_units_lineage(workspace_id)
    result: dict[str, Any] = {}
    for unit in units:
        studio = unit.get("studio") or {}
        same = [item for item in versions if item["model_spec_id"] == studio.get("model_spec_id")]
        newest = max(same, key=lambda item: item["version_number"], default=None)
        pinned = int(studio.get("version_number", 0))
        result[unit["id"]] = {
            "tag": unit["tag"], "name": studio.get("name"), "version_label": studio.get("version_label"),
            "version_number": pinned, "version_id": (unit.get("model") or {}).get("version_id"),
            "latest_version_id": newest["version_id"] if newest else None,
            "latest_version_number": newest["version_number"] if newest else None,
            "update_available": bool(newest and newest["version_number"] > pinned),
            "limits": HONEST_LIMITS}
    return result


def _load(unit: dict[str, Any], cache: dict[str, Any], workspace_id: str | None) -> dict[str, Any]:
    pin = unit["model"]
    loaded = cache.setdefault("studio_unit_sources", {})
    if pin["version_id"] not in loaded:
        if workspace_id is None:
            raise StudioUnitError("STUDIO_UNIT_VERSION_NOT_FOUND", "A workspace is required to resolve a Studio unit.")
        resolved = resolver(workspace_id)(pin["version_id"])
        if (resolved["source_sha256"], resolved["contract_sha256"]) != (pin["source_sha256"], pin["contract_sha256"]):
            raise StudioUnitError("STUDIO_UNIT_PIN_MISMATCH",
                                  "The pinned hashes do not match the published version's stored artifact.")
        loaded[pin["version_id"]] = resolved
    return loaded[pin["version_id"]]


def evaluate(unit: dict[str, Any], inlets: dict[str, dict[str, Any]], *, compounds: list[str], cache: dict[str, Any],
             workspace_id: str | None, validation: bool = False) -> Evaluation:
    """One sandboxed, host-checked evaluation; cached for the Process Run by source, parameters and inlet states."""
    try:
        spec = spec_for(unit)
    except StudioPinError as exc:
        raise StudioUnitError("STUDIO_UNIT_PIN_INVALID", str(exc)) from exc
    if set(inlets) != set(spec.inlets):
        raise StudioUnitError("STUDIO_UNIT_INLET_MISSING", f"{unit['tag']} needs every declared inlet connected.")
    if validation:
        # Validate only needs boundary guesses for DWSIM; user code runs in the solve, not here.
        guess = inlets[spec.inlets[0]]
        return Evaluation(outlets={name: copy.deepcopy(guess) for name in spec.outlets},
                          result={"owner": "jarvis_bio", "calculated": False, "evaluator": "studio_unit_v1",
                                  "version": 1, "validation_guess": True})
    loaded = _load(unit, cache, workspace_id)
    contract = loaded["contract"]
    try:
        params = {item["name"]: float(unit["params"][item["name"]]["value"]) for item in contract["parameters"]}
    except (KeyError, TypeError, ValueError) as exc:
        raise StudioUnitError("STUDIO_UNIT_PARAMETER_MISSING", f"{unit['tag']} is missing a parameter value.") from exc
    streams = {name: host_stream(state) for name, state in inlets.items()}
    key = hashlib.sha256(json.dumps({"source": loaded["source_sha256"], "contract": loaded["contract_sha256"],
                                    "params": params, "inlets": streams},
                                    sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    stats = cache.setdefault("studio_unit_stats", {}).setdefault(unit["tag"], {
        "call_count": 0, "cache_hits": 0, "runtime_classifications": [], "duration_s": 0.0,
        "sandbox_seconds": 0.0})
    memo = cache.setdefault("studio_unit_results", {})
    if key in memo:
        stats["cache_hits"] += 1
        checked = memo[key]
    else:
        record = runtime.run(runtime.RunRequest(source=loaded["source"], inputs={"inlets": streams, "params": params}))
        stats["call_count"] += 1
        stats["duration_s"] += record.duration_seconds
        stats["sandbox_seconds"] += record.duration_seconds
        if record.classification not in stats["runtime_classifications"]:
            stats["runtime_classifications"].append(record.classification)
        if record.classification != "ok":
            raise StudioUnitError(classification_code(record.classification),
                                  f"{unit['tag']}: runtime {record.classification}: {record.stderr.strip()[:300]}",
                                  {"classification": record.classification})
        try:
            checked = check_result(record.result, inlets=streams, outlet_names=list(spec.outlets),
                                   compounds=compounds, generation=contract["culture_generation"])
        except UnitCheckError as exc:
            raise StudioUnitError(exc.code, f"{unit['tag']}: {exc.message}") from exc
        memo[key] = checked
    return Evaluation(
        outlets=copy.deepcopy(checked.outlets), culture_generation=dict(checked.culture_generation),
        result={"owner": "jarvis_bio", "calculated": True, "evaluator": "studio_unit_v1", "version": 1,
                "fidelity": "user-authored steady model — unqualified", "caveats": [HONEST_LIMITS + "."],
                "studio": {"version_id": unit["model"]["version_id"], "version_label": loaded["version_label"],
                           "source_sha256": loaded["source_sha256"], "contract_sha256": loaded["contract_sha256"],
                           **copy.deepcopy(stats)}})


def run_provenance(run: dict[str, Any], document: dict[str, Any], workspace_id: str) -> dict[str, Any]:
    """Provenance of each custom unit in a stored run; superseded is computed at read time, the run is never changed."""
    units = studio_units(document)
    if not units:
        return {}
    latest: dict[str, int] = {}
    for version in service.published_units_lineage(workspace_id):
        latest[version["model_spec_id"]] = max(latest.get(version["model_spec_id"], 0), version["version_number"])
    result: dict[str, Any] = {}
    for unit in units:
        recorded = ((run.get("units") or {}).get(unit["tag"]) or {}).get("studio")
        if recorded is None:
            continue
        studio = unit.get("studio") or {}
        newest = latest.get(str(studio.get("model_spec_id") or ""), 0)
        result[unit["tag"]] = {**recorded, "version_number": studio.get("version_number"), "name": studio.get("name"),
                               "superseded": newest > int(studio.get("version_number", 0)),
                               "latest_version_number": newest or None, "limits": HONEST_LIMITS}
    return result
