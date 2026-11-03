"""Fresh-flowsheet sequential modular execution for mixed Process drafts."""

from __future__ import annotations

import copy
import json
import math
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, cast

# pbr_unit is imported eagerly on purpose (spec 170): an import inside a Run counts against the 168 wall budget.
from app.modules.process_stack import culture, draft_compiler, mixed, pbr_unit, studio_unit, tear_solver
from app.modules.process_stack.draft_models import spec_for
from app.modules.process_stack.dwsim_mcp import DwsimMcpClient


class SegmentFailure(RuntimeError):
    def __init__(self, segment: str, detail: Any, units: list[str] | None = None) -> None:
        super().__init__(str(detail))
        self.segment = segment
        self.detail = detail
        # Operator-visible unit (or stream) tags; the segment label alone is not meaningful to an operator.
        self.units = units


def _named_units(tags: list[str], detail: Any) -> list[str]:
    """Units the failure names (DWSIM errors name the unit); every unit of the segment otherwise."""
    text = json.dumps(detail, default=str)
    named = [tag for tag in tags if re.search(rf"(?<![A-Za-z0-9_]){re.escape(tag)}(?![A-Za-z0-9_])", text)]
    return named or list(tags)


def _failure_message(detail: Any) -> str | None:
    """One operator sentence from a failure detail: check findings, mismatch diffs, DWSIM errors."""
    if isinstance(detail, str):
        return detail
    if not isinstance(detail, dict):
        return None
    if isinstance(detail.get("detail"), (dict, str)) and not detail.get("message"):
        inner = _failure_message(detail["detail"])
        if inner:
            return inner
    for key in ("findings", "diffs", "errors"):
        rows = detail.get(key)
        if not isinstance(rows, list):
            continue
        parts: list[str] = []
        for row in rows[:3]:
            if isinstance(row, str):
                parts.append(row)
            elif isinstance(row, dict):
                if key == "diffs" and "path" in row:
                    parts.append(f"{row['path']}: draft expects {row.get('expected')}, DWSIM holds {row.get('actual')}")
                else:
                    text = row.get("message") or row.get("code")
                    if text:
                        parts.append(f"{row['object']}: {text}" if row.get("object") else str(text))
        if parts:
            return "; ".join(parts)
    for key in ("message", "dwsim_message", "error", "code"):
        if isinstance(detail.get(key), str) and detail[key]:
            return detail[key]
    return None


def finite_json(value: Any) -> Any:
    """Replace every non-finite float with null so records and responses stay valid JSON."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json(item) for item in value]
    return value


def _history_record(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """History as stored: infinity stays internal; a row says explicitly that it had none."""
    rows: list[dict[str, Any]] = []
    for row in history:
        record = copy.deepcopy(row)
        mismatched = sorted(f"{tag}.{name}" for tag, fields in record.get("normalized_residuals", {}).items()
                            for name, value in fields.items()
                            if isinstance(value, float) and math.isinf(value))
        peak = record.get("max_normalized_residual")
        if isinstance(peak, float) and not math.isfinite(peak):
            record["non_finite"] = "inf" if peak > 0 else "nan"
            record["pattern_mismatch_fields"] = mismatched
        rows.append(finite_json(record))
    return rows


def _partition_record(document: dict[str, Any], part: dict[str, Any],
                      segments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Tag-based partition, identical in shape for success, failure and validation records."""
    objects = document["objects"]
    tags = [[objects[uid]["tag"] for uid in ids] for ids in part["segments"]]
    rows = segments if segments is not None else [
        {"id": index, "units": unit_tags, "level": part["levels"][part["segments"][index][0]]}
        for index, unit_tags in enumerate(tags)]
    return {"consumed": [objects[uid]["tag"] for uid in part["consumed"]], "segments": rows,
            "jarvis_units": [objects[uid]["tag"] for uid in part["jarvis_units"]],
            "levels": {objects[uid]["tag"]: level for uid, level in part["levels"].items()}}


@dataclass
class JarvisUnitContext:
    """Inputs shared by one Jarvis evaluation; cache lives for one Process Run."""

    inlet_density_kg_m3: float
    deadline: float
    cache: dict[str, Any]
    validation: bool = False
    workspace_id: str | None = None
    # Spec 190: every incoming material stream by port name, and the draft compounds; built-ins use `inlet` only.
    inlets: dict[str, dict[str, Any]] = dataclass_field(default_factory=dict)
    compounds: list[str] = dataclass_field(default_factory=list)

    def remaining_s(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


@dataclass
class JarvisUnitEvaluation:
    outlets: dict[str, dict[str, Any]]
    result: dict[str, Any]
    # Rates are signed production, in kg/s except DIC in mol/s.
    culture_generation: dict[str, float] = dataclass_field(default_factory=dict)
    culture_generation_units: dict[str, str] = dataclass_field(default_factory=dict)
    culture_generation_allowance: dict[str, float] = dataclass_field(default_factory=dict)


JarvisEvaluator = Callable[[dict[str, Any], dict[str, Any], JarvisUnitContext], JarvisUnitEvaluation]


def _evaluate_separator(unit: dict[str, Any], inlet: dict[str, Any],
                        context: JarvisUnitContext) -> JarvisUnitEvaluation:
    del context
    recovery = float(unit["params"]["biomass_recovery"]["si"])
    factor = float(unit["params"]["concentration_factor"]["si"])
    concentrate, clarified = mixed.separator(inlet, recovery, factor)
    return JarvisUnitEvaluation(
        outlets={"concentrate": concentrate, "clarified": clarified},
        result={"owner": "jarvis_bio", "calculated": True,
                "evaluator": "jarvis.specified_separator", "version": 1,
                "fidelity": "screening — specified performance, not a mechanistic separator",
                "caveats": ["Dissolved species follow the carrier.", "No energy or pressure effect."],
                "reported": {"concentrate_flow_kg_s": {"value": concentrate["mass_flow_kg_s"], "units": "kg/s"},
                             "clarified_flow_kg_s": {"value": clarified["mass_flow_kg_s"], "units": "kg/s"}}},
    )


GENERATION_UNITS = {name: "mol/s" if name == "dic" else "kg/s" for name in mixed.CULTURE_FIELDS}


def _evaluate_studio_unit(unit: dict[str, Any], inlet: dict[str, Any],
                          context: JarvisUnitContext) -> JarvisUnitEvaluation:
    del inlet
    evaluation = studio_unit.evaluate(unit, context.inlets, compounds=context.compounds, cache=context.cache,
                                      workspace_id=context.workspace_id, validation=context.validation)
    return JarvisUnitEvaluation(
        outlets=evaluation.outlets, result=evaluation.result, culture_generation=evaluation.culture_generation,
        culture_generation_units={name: GENERATION_UNITS[name] for name in evaluation.culture_generation})


JARVIS_EVALUATORS: dict[str, JarvisEvaluator] = {
    "SpecifiedSeparator": _evaluate_separator, "PhotobioreactorT1": pbr_unit.evaluate_pbr,
    "StudioUnit": _evaluate_studio_unit,
}


def _incoming_material(document: dict[str, Any], unit_id: str) -> list[dict[str, Any]]:
    return sorted((stream for stream in document["objects"].values() if stream["kind"] == "stream"
                   and stream["type"] != "EnergyStream" and (stream.get("target") or {}).get("unit") == unit_id),
                  key=lambda stream: stream["target"]["port"])


def _check_evaluation(unit: dict[str, Any], evaluation: JarvisUnitEvaluation) -> None:
    if set(evaluation.outlets) != set(spec_for(unit).outlets):
        raise SegmentFailure(unit["tag"], {"code": "JARVIS_OUTLETS_MISMATCH"})
    if (any(name not in mixed.CULTURE_FIELDS or not isinstance(rate, (int, float))
            or not math.isfinite(rate) for name, rate in evaluation.culture_generation.items())
            or any(name not in mixed.CULTURE_FIELDS or not isinstance(rate, (int, float))
                   or not math.isfinite(rate) or rate < 0 for name, rate in evaluation.culture_generation_allowance.items())
            or evaluation.culture_generation_units != {
                name: GENERATION_UNITS[name] for name in evaluation.culture_generation
                if name in GENERATION_UNITS}):
        raise SegmentFailure(unit["tag"], {"code": "JARVIS_GENERATION_INVALID"})


def _call_evaluator(unit: dict[str, Any], inlet: dict[str, Any],
                    context: JarvisUnitContext) -> JarvisUnitEvaluation:
    evaluator = JARVIS_EVALUATORS.get(unit["type"])
    if evaluator is None:
        raise SegmentFailure(unit["tag"], {"code": "JARVIS_EVALUATOR_MISSING", "type": unit["type"]})
    timeout_code = "STUDIO_UNIT_TIMEOUT" if unit["type"] == "StudioUnit" else "JARVIS_UNIT_TIMEOUT"
    if context.remaining_s() <= 0:
        raise SegmentFailure(unit["tag"], {"code": timeout_code})
    try:
        evaluation = evaluator(unit, inlet, context)
    except Exception as exc:
        if isinstance(exc, SegmentFailure):
            raise
        code = timeout_code if isinstance(exc, TimeoutError) else str(
            getattr(exc, "code", "JARVIS_UNIT_FAILED"))
        detail = getattr(exc, "detail", None)
        raise SegmentFailure(unit["tag"], {"code": code, "error_type": type(exc).__name__,
                                            "message": str(exc)[:600], "detail": detail}) from exc
    if context.remaining_s() <= 0:
        raise SegmentFailure(unit["tag"], {"code": timeout_code})
    _check_evaluation(unit, evaluation)
    return evaluation


class _ClosingClient:
    """Keep the unchanged compiler path while closing the mixed path's fresh handle."""

    def __init__(self, client: DwsimMcpClient, deadline: float | None = None) -> None:
        self.client = client
        self.deadline = deadline
        self.flowsheets: list[str] = []

    def call(self, name: str, args: dict[str, Any], timeout: float) -> Any:
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining < 1:
                raise TimeoutError("mixed-solve wall budget exhausted")
            timeout = min(timeout, remaining)
            if name == "dwsim_solve_run" and isinstance(args.get("timeout_s"), (int, float)):
                args = {**args, "timeout_s": min(float(args["timeout_s"]), remaining)}
        result = self.client.call(name, args, timeout)
        if name == "dwsim_flowsheet_create":
            self.flowsheets.append(result["flowsheet_id"])
        elif name == "dwsim_flowsheet_load":
            self.flowsheets.append(result["flowsheet_id"])
        return result

    def close(self) -> None:
        for flow in self.flowsheets:
            try:
                timeout = min(30.0, max(1.0, self.deadline - time.monotonic())) if self.deadline else 30.0
                self.client.call("dwsim_flowsheet_close", {"flowsheet_id": flow}, timeout)
            except Exception:  # noqa: BLE001 - the process closes after the Run
                pass


def _light(document: dict[str, Any], client: DwsimMcpClient, *, label: str,
           remaining_s: float, allow_isolated_feed_flash: bool = False) -> dict[str, Any]:
    """Same verified build as materialize, skipping only full result catalogue work."""
    deadline = time.monotonic() + max(0.0, remaining_s)
    wrapper = _ClosingClient(client, deadline)
    exp = draft_compiler.expected(document)
    flow = ""
    started = time.monotonic()
    try:
        build_started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="jarvis-mixed-") as temp:
            case = Path(temp) / "segment.dwxml"
            flow = wrapper.call("dwsim_flowsheet_create", {"name": label}, min(30, remaining_s))["flowsheet_id"]
            for name, args in draft_compiler.plan(document):
                wrapper.call(name, {"flowsheet_id": flow, **args}, min(60, max(0.1, remaining_s - (time.monotonic() - started))))
            wrapper.call("dwsim_flowsheet_save", {"flowsheet_id": flow, "filepath": str(case),
                                                       "compressed": False}, min(60, remaining_s))
            # The same patched build as materialize: reactions, script kinetics and column details
            # exist only after the native patch and reload (spec 180 fact 7).
            flow = draft_compiler.reload_native_patch(cast(DwsimMcpClient, wrapper), flow, case, document)
            actual = draft_compiler.read_back(cast(DwsimMcpClient, wrapper), flow, case, exp)
            diffs = draft_compiler.compare(exp, actual)
            if diffs:
                raise SegmentFailure(label, {"code": "materialization_mismatch", "diffs": diffs})
            check = wrapper.call("dwsim_flowsheet_check", {"flowsheet_id": flow}, min(30, remaining_s))
            flash_exception = draft_compiler.isolated_feed_flash_check(
                document, "run", check, allowed=allow_isolated_feed_flash)
            if not check.get("ready") and not flash_exception:
                raise SegmentFailure(label, {"code": "check_failed", "findings": check.get("findings", [])})
            build_seconds = time.monotonic() - build_started
            solve_started = time.monotonic()
            solve = wrapper.call("dwsim_solve_run", {"flowsheet_id": flow,
                                                      "timeout_s": min(120, max(1, remaining_s))},
                                 min(150, max(1, remaining_s)))
            solve_seconds = time.monotonic() - solve_started
            if solve.get("ok") is not True or solve.get("errors"):
                raise SegmentFailure(label, {"code": "solve_failed", "errors": solve.get("errors", [])})
            streams = {stream["tag"]: draft_compiler._stream_result(wrapper.call(
                "dwsim_stream_get_results", {"flowsheet_id": flow, "name": stream["tag"]},
                min(30, remaining_s))) for stream in document["objects"].values()
                if stream["kind"] == "stream" and stream["type"] != "EnergyStream"}
            # Native Recycle errors bound the carrier gap the culture balances may allow (no other unit results).
            units = {}
            for unit in document["objects"].values():
                if unit["kind"] == "unit" and unit["type"] == "Recycle":
                    reported = wrapper.call("dwsim_unitop_get_results", {"flowsheet_id": flow, "name": unit["tag"]},
                                            min(30, remaining_s))
                    units[unit["tag"]] = {"calculated": reported.get("calculated"), "error": reported.get("error", ""),
                                          "reported": reported.get("properties", {})}
            return {"status": "completed", "streams": streams, "units": units,
                    "dwsim_check": {"ready": check.get("ready"), "findings": check.get("findings", []),
                                    "intentional_isolated_feed_exception": flash_exception},
                    "elapsed_s": time.monotonic() - started,
                    "elapsed_s_by_phase": {"build": build_seconds, "solve": solve_seconds,
                                            "culture": max(0.0, time.monotonic() - started
                                                            - build_seconds - solve_seconds)}}
    finally:
        wrapper.close()


def _full(document: dict[str, Any], client: DwsimMcpClient, *, label: str, keep_case: Path | None,
          dwsim_version: str, mcp_sha256: str, action: str = "run",
          deadline: float | None = None, allow_isolated_feed_flash: bool = False) -> dict[str, Any]:
    wrapper = _ClosingClient(client, deadline)
    try:
        result = draft_compiler.materialize(document, action=action, client=cast(DwsimMcpClient, wrapper),
                                            dwsim_version=dwsim_version, mcp_sha256=mcp_sha256,
                                            label=label, keep_case=keep_case,
                                            allow_isolated_feed_flash=allow_isolated_feed_flash)
        if result["status"] not in {"completed", "validated"}:
            failed = next((tag for tag, unit in result.get("units", {}).items()
                           if unit.get("calculated") is False or unit.get("error")), label)
            raise SegmentFailure(failed, result)
        return result
    finally:
        wrapper.close()


def _flash(state: dict[str, Any], tag: str, client: DwsimMcpClient, *, label: str,
           dwsim_version: str, mcp_sha256: str, property_package: str,
           full: bool, remaining_s: float, deadline: float | None = None,
           keep_case: Path | None = None, compounds: list[str] | None = None,
           spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Flash one stream through the ordinary verified DWSIM compiler path.

    A Jarvis outlet is flashed from its state; an operator feed passes its own ``spec`` so DWSIM
    reads back the real state for whichever spec keys (molar flow, vapor fraction, ...) were given.
    """
    stream = {"id": tag, "kind": "stream", "tag": tag, "type": "MaterialStream",
              "source": None, "target": None, "spec": spec if spec is not None else _feed_spec(state),
              "x": 0, "y": 0}
    document = {"schema_version": 1, "name": label,
                "compounds": list(compounds) if compounds is not None else list(state["mass_fractions"]),
                "property_package": property_package, "objects": {tag: stream},
                "reactions": {}}
    outcome = (_full(document, client, label=label, keep_case=keep_case,
                     dwsim_version=dwsim_version, mcp_sha256=mcp_sha256,
                     deadline=deadline, allow_isolated_feed_flash=True) if full
               else _light(document, client, label=label,
                           remaining_s=max(0.0, (deadline - time.monotonic()) if deadline else remaining_s),
                           allow_isolated_feed_flash=True))
    result = dict(outcome["streams"][tag])
    if outcome.get("dwsim_check", {}).get("intentional_isolated_feed_exception") is True:
        result["flash_check_exception"] = "STREAM_DANGLING"
    return result


def _feeds(document: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted((stream for stream in document["objects"].values() if stream["kind"] == "stream"
                   and stream["type"] != "EnergyStream" and stream.get("source") is None),
                  key=lambda item: item["tag"])


def _mass_fractions_from_spec(spec: dict[str, Any], compounds: list[str]) -> dict[str, float]:
    """Mass fractions over every document compound (0.0 for omitted ones)."""
    given = {name: float(value) for name, value in (spec.get("composition") or {}).items()}
    if spec.get("composition_basis") == "mole":
        missing = [name for name in given if name not in culture.MOLECULAR_WEIGHT_KG_PER_KMOL]
        if missing:
            raise SegmentFailure("initial_guess", f"Mole-basis feed needs a molecular weight for {', '.join(missing)}")
        given = {name: value * culture.MOLECULAR_WEIGHT_KG_PER_KMOL[name] for name, value in given.items()}
    total = sum(given.values())
    return {name: (given.get(name, 0.0) / total if total > 0 else 0.0) for name in compounds}


def _feed_mass_flow(spec: dict[str, Any], fractions: dict[str, float]) -> float:
    """Best mass flow of a feed in kg/s; a molar flow converts through the mixture molecular weight."""
    if "mass_flow" in spec:
        return float(spec["mass_flow"]["si"])
    if "molar_flow" in spec:
        moles = {name: value / culture.MOLECULAR_WEIGHT_KG_PER_KMOL[name]
                 for name, value in fractions.items() if value > 0 and name in culture.MOLECULAR_WEIGHT_KG_PER_KMOL}
        if len(moles) == sum(1 for value in fractions.values() if value > 0) and moles:
            mean_weight = 1.0 / sum(moles.values())  # kg/kmol
            return float(spec["molar_flow"]["si"]) * mean_weight / 1000.0
    return 0.0


def _state_from_spec(feed: dict[str, Any], compounds: list[str]) -> dict[str, Any]:
    """Approximate state used only for guesses and Validate; a Run flashes Jarvis-bound feeds."""
    spec = feed["spec"]
    fractions = _mass_fractions_from_spec(spec, compounds)
    return {"temperature_K": spec.get("temperature", {}).get("si", 298.15),
            "pressure_Pa": spec["pressure"]["si"], "mass_flow_kg_s": _feed_mass_flow(spec, fractions),
            "mass_fractions": fractions, "vapor_fraction": 0.0,
            "culture": {field: None for field in mixed.CULTURE_FIELDS} |
                       {"ph": (spec.get("culture") or {}).get("ph", {}).get("si")}}


def _feed_spec(state: dict[str, Any]) -> dict[str, Any]:
    fractions = state["mass_fractions"]
    total = sum(fractions.values())
    return {"temperature": {"si": float(state["temperature_K"])},
            "pressure": {"si": float(state["pressure_Pa"])},
            "mass_flow": {"si": float(state["mass_flow_kg_s"])},
            "composition_basis": "mass", "composition": {key: float(value) / total
                                                           for key, value in fractions.items()}}


def _segment_document(document: dict[str, Any], ids: list[str], known: dict[str, dict[str, Any]]) -> dict[str, Any]:
    selected = set(ids)
    objects: dict[str, dict[str, Any]] = {uid: copy.deepcopy(document["objects"][uid]) for uid in ids}
    for original in document["objects"].values():
        if original["kind"] != "stream":
            continue
        source = (original.get("source") or {}).get("unit")
        target = (original.get("target") or {}).get("unit")
        if source not in selected and target not in selected:
            continue
        stream = copy.deepcopy(original)
        if source is None:
            stream["_original_feed"] = True
        elif source not in selected:
            stream["source"] = None
            if original["tag"] not in known:
                raise SegmentFailure("boundary", f"Missing state for boundary feed {original['tag']}")
            stream["spec"] = _feed_spec(known[original["tag"]])
        if target not in selected:
            stream["target"] = None
        objects[stream["id"]] = stream
    return {"schema_version": document["schema_version"], "name": document.get("name", ""),
            "compounds": document["compounds"], "property_package": document["property_package"],
            "objects": objects, "reactions": document.get("reactions", {})}


def _seed(document: dict[str, Any], part: dict[str, Any],
          settings: tear_solver.Settings | None = None) -> dict[str, dict[str, Any]]:
    feeds = _feeds(document)
    if not feeds:
        raise SegmentFailure("initial_guess", "A mixed draft needs a feed")
    preferred = next((item for item in feeds if item.get("spec", {}).get("culture") is not None), feeds[0])
    compounds = document["compounds"]
    base = _state_from_spec(preferred, compounds)
    total = sum(_state_from_spec(item, compounds)["mass_flow_kg_s"] for item in feeds)
    if not total > 0:
        total = 1.0  # no feed flow is known in kg/s; the tear is still never exactly zero
    units = document["objects"]
    result = {}
    for uid in part["consumed"]:
        stream = next(item for item in document["objects"].values() if item["kind"] == "stream"
                      and (item.get("source") or {}).get("unit") == uid)
        target = (stream.get("target") or {}).get("unit")
        scale = 1e-3 if target is not None and units[target]["type"] == "HeatExchanger" else 1e-6
        feed_mode = settings is not None and settings.seed_mode == "feed"
        state = copy.deepcopy(base)
        state["mass_flow_kg_s"] = total if feed_mode else scale * total
        specification = preferred.get("spec", {}).get("culture") or {}
        for field in mixed.CULTURE_FIELDS:
            state["culture"][field] = 0.0
            if feed_mode and field in specification:
                # A seed is only an initial guess: the feed culture per kg carrier at an assumed 1000 kg/m3.
                value = float(specification[field]["si"])
                state["culture"][field] = value if field == "salinity" else value / 1000.0
        for feed in feeds:
            culture_spec = feed.get("spec", {}).get("culture")
            if culture_spec is not None:
                for field in mixed.CULTURE_FIELDS:
                    if field not in culture_spec:
                        state["culture"][field] = None
        state["culture"]["ph"] = base["culture"].get("ph")
        explicit = (settings.seeds if settings is not None else {}).get(units[uid]["tag"], {})
        for name, value in explicit.items():
            if name in mixed.CULTURE_FIELDS:
                if state["culture"].get(name) is not None:
                    state["culture"][name] = value
            else:
                state[name] = value
        result[stream["tag"]] = state
    return result


def _validation_states(document: dict[str, Any], part: dict[str, Any], workspace_id: str | None = None) -> dict[str, dict[str, Any]]:
    """Supply deterministic boundary guesses so Validate can build without solving a segment."""
    feeds = _feeds(document)
    if not feeds:
        return {}
    compounds = document["compounds"]
    reference = _state_from_spec(next((feed for feed in feeds if feed.get("spec", {}).get("culture")), feeds[0]),
                                 compounds)
    if not reference["mass_flow_kg_s"] > 0:
        reference["mass_flow_kg_s"] = 1.0
    known = {feed["tag"]: _state_from_spec(feed, compounds) for feed in feeds}
    segment_of = {uid: index for index, ids in enumerate(part["segments"]) for uid in ids}
    for stream in document["objects"].values():
        if stream["kind"] != "stream" or stream["type"] == "EnergyStream":
            continue
        source = (stream.get("source") or {}).get("unit")
        target = (stream.get("target") or {}).get("unit")
        # Jarvis outlets, and edges that cross between two DWSIM segments (the bypass case), are
        # boundary feeds whose solved state does not exist yet: Validate builds them at the reference.
        if source in part["jarvis_units"] or (
                source in segment_of and target is not None and segment_of.get(target) != segment_of[source]):
            known.setdefault(stream["tag"], copy.deepcopy(reference))
    for unit_id in part["jarvis_units"]:
        unit = document["objects"][unit_id]
        incoming = next(stream for stream in document["objects"].values() if stream["kind"] == "stream"
                        and (stream.get("target") or {}).get("unit") == unit_id)
        inlet = known.get(incoming["tag"], copy.deepcopy(reference))
        ports = spec_for(unit).inlets
        inlets = {ports[stream["target"]["port"]]: known.get(stream["tag"], copy.deepcopy(reference))
                  for stream in _incoming_material(document, unit_id)} if unit["type"] == "StudioUnit" else {}
        evaluation = _call_evaluator(unit, inlet, JarvisUnitContext(
            inlet_density_kg_m3=float(inlet.get("density_kg_m3") or 1000.0),
            deadline=time.monotonic() + mixed.WALL_BUDGET_S, cache={}, validation=True,
            workspace_id=workspace_id, inlets=inlets, compounds=list(compounds)))
        outgoing = sorted((stream for stream in document["objects"].values() if stream["kind"] == "stream"
                           and (stream.get("source") or {}).get("unit") == unit_id),
                          key=lambda stream: stream["source"]["port"])
        for stream in outgoing:
            port = spec_for(unit).outlets[stream["source"]["port"]]
            known[stream["tag"]] = evaluation.outlets[port]
    return known


def _flatten(state: dict[str, Any]) -> dict[str, float | None]:
    row = {name: state.get(name) for name in ("temperature_K", "pressure_Pa", "mass_flow_kg_s")}
    row.update({"mass_fraction." + name: value for name, value in state.get("mass_fractions", {}).items()})
    row.update({name: state.get("culture", {}).get(name) for name in mixed.CULTURE_FIELDS})
    return row


def _from_stream(result: dict[str, Any], prior: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"temperature_K": result.get("temperature_K"), "pressure_Pa": result.get("pressure_Pa"),
            "mass_flow_kg_s": result.get("mass_flow_kg_s"), "mass_fractions": result.get("mass_fractions", {}),
            "vapor_fraction": result.get("vapor_fraction"), "density_kg_m3": culture._density(result),
            "culture": copy.deepcopy((prior or {}).get("culture") or {})}


def _culture_for_feed(feed: dict[str, Any], result: dict[str, Any], prior: dict[str, Any] | None) -> dict[str, Any]:
    if prior is not None:
        return copy.deepcopy(prior.get("culture") or {})
    specification = feed.get("spec", {}).get("culture")
    if specification is None:
        return {}
    density = culture._density(result)
    if density is None:
        raise SegmentFailure(feed["tag"], "CULTURE_DENSITY_UNAVAILABLE")
    result_state = {name: (float(specification[name]["si"]) / density if name not in {"salinity", "ph"}
                           else float(specification[name]["si"])) if name in specification else None
                    for name in (*mixed.CULTURE_FIELDS, "ph")}
    return result_state


def _propagate_culture(segment: dict[str, Any], result: dict[str, Any],
                       boundary: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    streams = result["streams"]
    for feed in _feeds(segment):
        states[feed["tag"]] = _culture_for_feed(
            feed, streams[feed["tag"]], None if feed.get("_original_feed") else boundary.get(feed["tag"]))
    # A native DWSIM Recycle closes a culture loop after the carrier solve. Its
    # outlet needs an initial culture value before the first Mixer sweep.
    culture_feeds = sorted((feed for feed in _feeds(segment) if states[feed["tag"]]),
                           key=lambda item: item["tag"])
    if culture_feeds:
        first_culture = states[culture_feeds[0]["tag"]]
        reachable = {(feed.get("target") or {}).get("unit") for feed in culture_feeds
                     if (feed.get("target") or {}).get("unit") is not None}
        changed_reach = True
        while changed_reach:
            changed_reach = False
            for stream in segment["objects"].values():
                if stream["kind"] != "stream" or stream["type"] == "EnergyStream":
                    continue
                source = (stream.get("source") or {}).get("unit")
                target = (stream.get("target") or {}).get("unit")
                if source in reachable and target is not None and target not in reachable:
                    reachable.add(target)
                    changed_reach = True
        for stream in segment["objects"].values():
            source = (stream.get("source") or {}).get("unit")
            if (stream["kind"] == "stream" and source in reachable
                    and segment["objects"][source]["type"] == "Recycle"):
                states.setdefault(stream["tag"], copy.deepcopy(first_culture))
    units = sorted((unit for unit in segment["objects"].values() if unit["kind"] == "unit"),
                   key=lambda item: item["tag"])
    for _ in range(10000):
        changed = False
        for unit in units:
            incoming = sorted((stream for stream in segment["objects"].values() if stream["kind"] == "stream"
                               and (stream.get("target") or {}).get("unit") == unit["id"]
                               and stream["type"] != "EnergyStream"), key=lambda item: item["target"]["port"])
            outgoing = sorted((stream for stream in segment["objects"].values() if stream["kind"] == "stream"
                               and (stream.get("source") or {}).get("unit") == unit["id"]
                               and stream["type"] != "EnergyStream"), key=lambda item: item["source"]["port"])
            # 167 semantics: only culture-carrying inlets drive a unit; the others dilute as zero culture.
            cultured = [stream for stream in incoming if states.get(stream["tag"])]
            if not cultured:
                continue
            if unit["type"] in {"Flash", "DistillationColumn", "PFR"}:
                raise SegmentFailure(unit["tag"], "CULTURE_UNIT_UNSUPPORTED")
            if unit["type"] == "Mixer":
                flows = [float(streams[stream["tag"]]["mass_flow_kg_s"]) for stream in incoming]
                total = sum(flows)
                if total <= 0:
                    raise SegmentFailure(unit["tag"], "CULTURE_MIXER_FLOW_INVALID")
                mixed_specific, _reasons = culture.mix_mass_specific(
                    [(float(streams[stream["tag"]]["mass_flow_kg_s"]),
                      {"mass_specific": states[stream["tag"]], "reasons": {}}) for stream in cultured],
                    total, len(incoming), unit["tag"])
                outlet_states: list[dict[str, Any] | None] = [mixed_specific] * len(outgoing)
            elif unit["type"] == "HeatExchanger":
                by_port = {stream["target"]["port"]: states.get(stream["tag"]) or None for stream in incoming}
                outlet_states = [by_port.get(stream["source"]["port"]) for stream in outgoing]
            else:
                outlet_states = [states[cultured[0]["tag"]]] * len(outgoing)
            for stream, value in zip(outgoing, outlet_states, strict=True):
                if value is None:
                    continue
                tag = stream["tag"]
                vapor = streams[tag].get("vapor_fraction")
                if vapor is None or vapor > 1e-6 or culture._density(streams[tag]) is None:
                    raise SegmentFailure(tag, "CULTURE_PHASE_NOT_LIQUID" if vapor is not None else
                                         "CULTURE_DENSITY_UNAVAILABLE")
                old = states.get(tag)
                differs = old is None or any(
                    (old.get(field) is None) != (value.get(field) is None)
                    or (old.get(field) is not None and value.get(field) is not None
                        and abs(float(old[field] or 0.0) - float(value[field] or 0.0)) > min(
                            mixed.tolerance(field, float(old[field] or 0.0)),
                            1e-10 * max(abs(float(old[field] or 0.0)), 1e-12)))
                    for field in mixed.CULTURE_FIELDS
                ) or (old is not None and old.get("ph") != value.get("ph"))
                if differs:
                    states[tag] = copy.deepcopy(value)
                    changed = True
        if not changed:
            return states
    raise SegmentFailure("culture", "culture native recycle did not converge in 10000 sweeps")


def _culture_balances(document: dict[str, Any], known: dict[str, dict[str, Any]],
                      generation: dict[str, dict[str, float]],
                      allowances: dict[str, dict[str, float]] | None = None,
                      native_error_kg_s: float = 0.0) -> dict[str, dict[str, Any]]:
    objects = document["objects"]
    balances: dict[str, dict[str, Any]] = {}
    for unit in (item for item in objects.values() if item["kind"] == "unit" and item["type"] != "Recycle"):
        incoming = sorted((item for item in objects.values() if item["kind"] == "stream"
                           and (item.get("target") or {}).get("unit") == unit["id"]
                           and item["type"] != "EnergyStream"), key=lambda item: item["target"]["port"])
        outgoing = sorted((item for item in objects.values() if item["kind"] == "stream"
                           and (item.get("source") or {}).get("unit") == unit["id"]
                           and item["type"] != "EnergyStream"), key=lambda item: item["source"]["port"])
        # Inside a converged native Recycle loop DWSIM leaves a unit's outlet carrier short of its inlets by up
        # to the Recycle's mass-flow error. That gap times the largest concentration is allowed, never more.
        flows_in = sum(float(known.get(stream["tag"], {}).get("mass_flow_kg_s") or 0.0) for stream in incoming)
        flows_out = sum(float(known.get(stream["tag"], {}).get("mass_flow_kg_s") or 0.0) for stream in outgoing)
        carrier_gap = abs(flows_in - flows_out)
        native_gap = carrier_gap if carrier_gap <= native_error_kg_s + 1e-9 * max(flows_in, flows_out) else 0.0
        rows: dict[str, Any] = {}
        for field in mixed.CULTURE_FIELDS:
            values = [known.get(stream["tag"], {}).get("culture", {}) for stream in [*incoming, *outgoing]]
            if not any(values):
                continue
            if any(field in value and value[field] is None for value in values):
                continue
            factor = 0.001 if field == "salinity" else 1.0
            def amount(stream: dict[str, Any], field_name: str = field,
                       factor_value: float = factor) -> float:
                state = known.get(stream["tag"], {})
                value = state.get("culture", {}).get(field_name)
                if value is None:
                    return 0.0
                return float(state["mass_flow_kg_s"]) * float(value) * factor_value
            inbound = sum(amount(stream) for stream in incoming)
            outbound = sum(amount(stream) for stream in outgoing)
            produced = generation.get(unit["tag"], {}).get(field, 0.0)
            residual_value = inbound + produced - outbound
            allowance = (allowances or {}).get(unit["tag"], {}).get(field, 0.0)
            native_allowance = native_gap * max(
                abs(float(value.get(field) or 0.0)) for value in values) * factor
            tolerance_value = (1e-9 * max(abs(inbound + produced), abs(outbound)) + 1e-12 + allowance
                               + native_allowance)
            rows[field] = {"in": inbound, "out": outbound, "residual": residual_value,
                           "generated": produced, "generation_allowance": allowance,
                           "tolerance": tolerance_value, "unit": "mol/s" if field == "dic" else "kg/s",
                           "passed": abs(residual_value) <= tolerance_value}
            if native_allowance:
                rows[field]["native_recycle_allowance"] = native_allowance
        if rows:
            if any(not row["passed"] for row in rows.values()):
                raise SegmentFailure(unit["tag"], {"code": "CULTURE_BALANCE_FAILED", "unit_balances": rows})
            balances[unit["tag"]] = rows
    return balances


def _culture_results(document: dict[str, Any], final: dict[str, Any]) -> dict[str, Any]:
    feeds = [item for item in document["objects"].values() if item["kind"] == "stream"
             and item.get("source") is None and item.get("spec", {}).get("culture") is not None]
    feed_tag = feeds[0]["tag"] if feeds else "a culture feed"
    results: dict[str, Any] = {}
    for stream in (item for item in document["objects"].values() if item["kind"] == "stream"
                   and item["type"] != "EnergyStream"):
        state = final["known"].get(stream["tag"])
        mass_specific = (state or {}).get("culture") or {}
        has_culture = bool(mass_specific) or stream in feeds
        if not has_culture:
            continue
        solved = final["streams"].get(stream["tag"], {})
        density = culture._density(solved)
        if density is None:
            raise SegmentFailure(stream["tag"], "CULTURE_DENSITY_UNAVAILABLE")
        reasons = {name: f"not specified on feed {feed_tag}" for name in (*mixed.CULTURE_FIELDS, "ph")
                   if mass_specific.get(name) is None}
        values = culture._display_values({"mass_specific": mass_specific, "reasons": reasons}, density)
        source = (stream.get("source") or {}).get("unit")
        unit_tag = document["objects"].get(source, {}).get("tag") if source else None
        producer = document["objects"].get(source, {}) if source else {}
        jarvis_result = (final["units"].get(unit_tag, {}) if unit_tag and producer.get("type")
                         in JARVIS_EVALUATORS else {})
        caveats = ["DWSIM mixture density uses seawater-as-water approximation."]
        if jarvis_result:
            caveats.extend(jarvis_result.get("caveats", []))
        else:
            caveats.append("Dissolved O₂ is carried without solubility or degassing changes; heated streams may be supersaturated.")
        results[stream["tag"]] = {
            "owner": "jarvis", "propagation_version": culture.PROPAGATION_VERSION, "status": "completed",
            "density_kg_m3": density, "values": values,
            "unit_balances": final["culture_balances"].get(unit_tag, {}) if unit_tag else {},
            "fidelity": jarvis_result.get("fidelity", culture.FIDELITY),
            "caveats": caveats,
            **({"evaluator": jarvis_result["evaluator"], "version": jarvis_result.get("version")}
               if jarvis_result.get("evaluator") else {}),
            "pH_reason": reasons.get("ph"),
        }
    return results


_MASS_FLOW_TO_KG_S = {"kg/h": 1 / 3600.0, "kg/s": 1.0, "kg/min": 1 / 60.0, "g/s": 0.001}


def _native_mass_flow_error_kg_s(unit_result: dict[str, Any]) -> float | None:
    """DWSIM's native Recycle mass-flow error in kg/s, or None when absent or unparseable.

    DWSIM reports the value as a string (e.g. ``"0"``) in ``reported`` and as a number in
    ``properties``; either form is accepted, always with its stated unit."""
    candidates: list[tuple[Any, Any]] = []
    reported = unit_result.get("reported", {}).get("Mass Flow Error")
    if isinstance(reported, dict):
        candidates.append((reported.get("value"), reported.get("units")))
    candidates.extend((row.get("value"), row.get("unit")) for row in unit_result.get("properties", [])
                      if isinstance(row, dict) and row.get("name") == "Mass Flow Error")
    for raw, unit in candidates:
        factor = _MASS_FLOW_TO_KG_S.get(str(unit).strip()) if unit is not None else None
        if factor is None or isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return abs(value) * factor
    return None


def _whole_graph_balances(document: dict[str, Any], final: dict[str, Any],
                          tear: dict[str, dict[str, Any]]) -> dict[str, Any]:
    objects = document["objects"]
    feeds = [item for item in objects.values() if item["kind"] == "stream" and item["type"] != "EnergyStream"
             and item.get("source") is None]
    products = [item for item in objects.values() if item["kind"] == "stream" and item["type"] != "EnergyStream"
                and item.get("target") is None and item.get("source") is not None]
    def flow(stream: dict[str, Any]) -> float:
        return float(final["known"][stream["tag"]]["mass_flow_kg_s"])
    inbound, outbound = sum(flow(item) for item in feeds), sum(flow(item) for item in products)
    native_error = 0.0
    native_values: list[tuple[float, dict[str, Any]]] = []
    native_error_findings: list[str] = []
    for unit in (item for item in objects.values() if item["kind"] == "unit" and item["type"] == "Recycle"
                 and item["id"] not in mixed.partition(document)["consumed"]):
        error_kg_s = _native_mass_flow_error_kg_s(final["units"].get(unit["tag"], {}))
        if error_kg_s is not None:
            error = error_kg_s
        else:
            native_error_findings.append(unit["tag"])
            error = 0.0
        native_error += error
        stream = next((item for item in objects.values() if item["kind"] == "stream"
                       and (item.get("source") or {}).get("unit") == unit["id"]), None)
        native_values.append((error, final["known"].get(stream["tag"], {}) if stream else {}))
    tear_tolerance = sum(mixed.tolerance("mass_flow_kg_s", state["mass_flow_kg_s"])
                         for state in tear.values())
    carrier_residual = inbound - outbound
    carrier_tolerance = tear_tolerance + native_error + 1e-9 * max(abs(inbound), abs(outbound)) + 1e-12
    rows: dict[str, Any] = {"carrier_mass": {"in": inbound, "out": outbound, "residual": carrier_residual,
                                              "tolerance": carrier_tolerance, "unit": "kg/s",
                                              "passed": abs(carrier_residual) <= carrier_tolerance}}
    for field in mixed.CULTURE_FIELDS:
        def rate(stream: dict[str, Any], field_name: str = field) -> float | None:
            value = final["known"].get(stream["tag"], {}).get("culture", {}).get(field_name)
            if value is None:
                return 0.0 if not final["known"].get(stream["tag"], {}).get("culture") else None
            return flow(stream) * float(value) * (0.001 if field_name == "salinity" else 1.0)
        feed_rates = [rate(item) for item in feeds]
        product_rates = [rate(item) for item in products]
        if any(value is None for value in [*feed_rates, *product_rates]):
            continue
        amount_in = sum(float(value) for value in feed_rates if value is not None)
        amount_out = sum(float(value) for value in product_rates if value is not None)
        generated = sum(unit.get(field, 0.0) for unit in final.get("culture_generation", {}).values())
        generation_allowance = sum(unit.get(field, 0.0) for unit in final.get("culture_generation_allowance", {}).values())
        native_allowance = sum(error * float(state.get("culture", {}).get(field) or 0.0) *
                               (0.001 if field == "salinity" else 1.0)
                               for error, state in native_values)
        tear_allowance = 0.0
        for state in tear.values():
            concentration = state.get("culture", {}).get(field)
            if concentration is None:
                continue
            mass_flow = float(state["mass_flow_kg_s"])
            concentration = float(concentration)
            delta_flow = mixed.tolerance("mass_flow_kg_s", mass_flow)
            delta_concentration = mixed.tolerance(field, concentration)
            factor = 0.001 if field == "salinity" else 1.0
            tear_allowance += (abs(mass_flow) * delta_concentration
                               + abs(concentration) * delta_flow
                               + delta_flow * delta_concentration) * factor
        tolerance_value = tear_allowance + native_allowance + generation_allowance + 1e-9 * max(abs(amount_in), abs(amount_out)) + 1e-12
        residual_value = amount_in + generated - amount_out
        rows[field] = {"in": amount_in, "out": amount_out, "residual": residual_value,
                       "generated": generated, "generation_allowance": generation_allowance,
                       "tolerance": tolerance_value, "unit": "mol/s" if field == "dic" else "kg/s",
                       "passed": abs(residual_value) <= tolerance_value}
    passed = all(row["passed"] for row in rows.values()) and not native_error_findings
    return {"status": "calculated" if passed else "failed", "balances": rows,
            "missing_native_recycle_errors": native_error_findings}


def _light_full_mismatch(light: dict[str, dict[str, Any]],
                         full: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if set(light) != set(full):
        return {"code": "light_full_mismatch", "light_tears": sorted(light), "full_tears": sorted(full)}
    for tag in sorted(light):
        normalized, worst, _fields = mixed.residual(_flatten(light[tag]), _flatten(full[tag]))
        if normalized > 1:
            return {"code": "light_full_mismatch", "tear": tag,
                    "max_normalized_residual": normalized, "worst_field": worst}
    return None


def _final_status(status: str, reason: str, mismatch: dict[str, Any] | None,
                  balances: dict[str, Any]) -> tuple[str, str]:
    if mismatch is not None:
        return "segment_failed", "light_full_mismatch"
    if balances["status"] != "calculated" and status == "completed":
        return "unconverged", "balance"
    return status, reason


def _pressure_diagnosis(history: list[dict[str, Any]]) -> str | None:
    if len(history) < 3:
        return None
    tail = history[-3:]
    common = set.intersection(*(set(row.get("residuals", {})) for row in tail))
    for tag in sorted(common):
        values = [row["residuals"][tag].get("pressure_Pa") for row in tail]
        if all(isinstance(value, (int, float)) and value != 0 for value in values):
            signs = {value > 0 for value in values}
            if len(signs) == 1 and values[-1] < 0:
                drop = abs(float(values[-1]))
                return f"pressure falls by {drop:.6g} Pa per pass around the loop; add a pump"
    return None


def _evaluate(document: dict[str, Any], part: dict[str, Any], tear: dict[str, dict[str, Any]],
              *, client: DwsimMcpClient, full: bool, run_dir: Path, iteration: int,
              dwsim_version: str, mcp_sha256: str, remaining_s: float,
              run_cache: dict[str, Any] | None = None, workspace_id: str | None = None) -> dict[str, Any]:
    objects = document["objects"]
    started = time.monotonic()
    deadline = time.monotonic() + max(0.0, remaining_s)
    compounds = document["compounds"]
    known = {**{feed["tag"]: _state_from_spec(feed, compounds) for feed in _feeds(document)},
             **copy.deepcopy(tear)}
    stream_results: dict[str, Any] = {}
    unit_results: dict[str, Any] = {}
    culture_generation: dict[str, dict[str, float]] = {}
    culture_generation_allowance: dict[str, dict[str, float]] = {}
    mixed_findings: list[dict[str, Any]] = []
    segment_records: list[dict[str, Any]] = []
    max_build_seconds = 0.0
    phase_elapsed = {"build": 0.0, "solve": 0.0, "culture": 0.0}
    for feed in _feeds(document):
        target = (feed.get("target") or {}).get("unit")
        if target not in part["jarvis_units"]:
            continue
        feed_flushed = _flash(known[feed["tag"]], feed["tag"], client,
                              label=f"mixed-feed-{iteration}-{feed['tag']}",
                              dwsim_version=dwsim_version, mcp_sha256=mcp_sha256,
                              property_package=document["property_package"], full=full,
                              remaining_s=max(0.0, deadline - time.monotonic()), deadline=deadline,
                              compounds=compounds, spec=feed["spec"])
        known[feed["tag"]] = _from_stream(feed_flushed)
        known[feed["tag"]]["culture"] = _culture_for_feed(feed, feed_flushed, None)
        stream_results[feed["tag"]] = {**feed_flushed, "owner": "dwsim"}
    segments = part["segments"]
    max_level = max(part["levels"].values(), default=0)
    for level in range(max_level + 1):
        for index, ids in enumerate(segments):
            if part["levels"][ids[0]] != level:
                continue
            segment = _segment_document(document, ids, known)
            label = f"mixed-{iteration}-{index}"
            try:
                if full:
                    outcome = _full(segment, client, label=label,
                                    keep_case=run_dir / f"segment-{index}.dwxmz",
                                    dwsim_version=dwsim_version, mcp_sha256=mcp_sha256,
                                    deadline=deadline)
                else:
                    outcome = _light(segment, client, label=label,
                                     remaining_s=max(0.0, deadline - time.monotonic()))
            except SegmentFailure as exc:
                if exc.units is None:
                    tags = [objects[uid]["tag"] for uid in ids]
                    exc.units = [exc.segment] if exc.segment in tags else _named_units(tags, exc.detail)
                raise
            except Exception as exc:
                raise SegmentFailure(label, {"code": type(exc).__name__, "message": str(exc)},
                                     [objects[uid]["tag"] for uid in ids]) from exc
            try:
                states = _propagate_culture(segment, outcome, known)
            except SegmentFailure as exc:
                if exc.units is None:
                    exc.units = [objects[uid]["tag"] for uid in ids]
                raise
            phase = outcome.get("elapsed_s_by_phase", {})
            phase_elapsed["build"] += float(phase.get("build", 0.0))
            phase_elapsed["solve"] += float(phase.get("solve", 0.0))
            max_build_seconds = max(max_build_seconds, float(phase.get("build", 0.0)))
            for tag, result in outcome["streams"].items():
                original = next((item for item in objects.values() if item["kind"] == "stream" and item["tag"] == tag), None)
                if original is None:
                    continue
                prior = known.get(tag)
                state = _from_stream(result, prior)
                if tag in states:
                    state["culture"] = states[tag]
                known[tag] = state
                earlier = stream_results.get(tag, {})
                if earlier.get("owner") == "jarvis_bio":
                    # A Jarvis outlet re-read as this segment's boundary feed stays Jarvis-owned.
                    stream_results[tag] = {**result, "owner": "jarvis_bio",
                                           "state_source": earlier.get("state_source", "jarvis_unit")}
                elif tag in tear:
                    stream_results[tag] = {**result, "owner": "jarvis_bio",
                                           "state_source": "jarvis_tear"}
                else:
                    stream_results[tag] = {**result, "owner": "dwsim"}
            unit_results.update({tag: {**value, "owner": "dwsim"}
                                 for tag, value in outcome.get("units", {}).items()})
            segment_records.append({"id": index, "units": [objects[uid]["tag"] for uid in ids],
                                    "level": level, "materialization_fingerprint": outcome.get("materialization_fingerprint"),
                                    "solved_case_sha256": outcome.get("solved_case_sha256"),
                                    "elapsed_s_by_phase": phase})
        for uid in part["jarvis_units"]:
            if part["levels"][uid] != level:
                continue
            unit = objects[uid]
            incoming = next(stream for stream in objects.values() if stream["kind"] == "stream"
                            and (stream.get("target") or {}).get("unit") == uid)
            studio = unit["type"] == "StudioUnit"
            if studio:
                # Spec 190: every declared inlet must be known at this level; built-in units keep their one inlet.
                ports = spec_for(unit).inlets
                gathered = _incoming_material(document, uid)
                missing = [stream["tag"] for stream in gathered if stream["tag"] not in known]
                if missing or len(gathered) != len(ports):
                    raise SegmentFailure(unit["tag"], {"code": "STUDIO_UNIT_INLET_MISSING",
                                                       "message": f"Inlet state is not known for {missing or 'a port'}."})
                incoming = gathered[0]
            if incoming["tag"] not in known:
                raise SegmentFailure(unit["tag"], "Jarvis inlet has no DWSIM state")
            inlet = known[incoming["tag"]]
            density = inlet.get("density_kg_m3")
            if studio:
                density = math.nan  # the custom unit sees streams, not DWSIM densities
            elif density is None and incoming["tag"] in tear:
                # An unflashed tear iterate has no DWSIM density; evaluators that ignore it
                # (the separator) get NaN, and one that needs it must fail on the NaN itself.
                density = math.nan
            elif density is None or not math.isfinite(float(density)) or float(density) <= 0:
                if unit["type"] == "PhotobioreactorT1":
                    # Let the PBR return its operator-facing typed density failure.
                    density = math.nan
                else:
                    raise SegmentFailure(unit["tag"], "Jarvis inlet has no solved liquid DWSIM density")
            evaluation = _call_evaluator(unit, inlet, JarvisUnitContext(
                inlet_density_kg_m3=float(density), deadline=deadline,
                cache=run_cache if run_cache is not None else {}, workspace_id=workspace_id,
                inlets={ports[stream["target"]["port"]]: known[stream["tag"]] for stream in gathered}
                if studio else {}, compounds=list(compounds)))
            culture_generation[unit["tag"]] = evaluation.culture_generation
            culture_generation_allowance[unit["tag"]] = evaluation.culture_generation_allowance
            inlet_biomass = (inlet.get("culture") or {}).get("biomass")
            factor = (float(unit["params"]["concentration_factor"]["si"])
                      if unit["type"] == "SpecifiedSeparator" else 0.0)
            if unit["type"] == "SpecifiedSeparator" and inlet_biomass is not None and factor * float(inlet_biomass) > 0.25:
                mixed_findings.append({"severity": "warning", "code": "SEPARATOR_CONCENTRATE_IMPLAUSIBLE",
                                       "object": unit["tag"], "field": "concentration_factor",
                                       "message": "Converged separator concentrate exceeds the 0.25 kg/kg screening bound.",
                                       "source": "jarvis"})
            streams = sorted((stream for stream in objects.values() if stream["kind"] == "stream"
                              and (stream.get("source") or {}).get("unit") == uid),
                             key=lambda item: item["source"]["port"])
            for stream in streams:
                port = spec_for(unit).outlets[stream["source"]["port"]]
                state = copy.deepcopy(evaluation.outlets[port])
                downstream = (stream.get("target") or {}).get("unit")
                flashed: dict[str, Any] | None = None
                if ((full and (downstream is None or downstream in part["consumed"]))
                        or downstream in part["jarvis_units"]):
                    flashed = _flash(state, stream["tag"], client,
                                     label=f"mixed-flash-{iteration}-{stream['tag']}",
                                     dwsim_version=dwsim_version, mcp_sha256=mcp_sha256,
                                     property_package=document["property_package"],
                                     full=full, remaining_s=max(0.0, deadline - time.monotonic()),
                                     deadline=deadline,
                                     keep_case=(run_dir / f"flash-{stream['tag']}.dwxmz") if full else None,
                                     compounds=compounds)
                    state.update({key: value for key, value in flashed.items()
                                  if key not in {"display"}})
                    state["mass_fractions"] = flashed.get("mass_fractions", state.get("mass_fractions", {}))
                    state["mass_flow_kg_s"] = evaluation.outlets[port]["mass_flow_kg_s"]
                    state["culture"] = evaluation.outlets[port]["culture"]
                    state["state_source"] = "jarvis_unit → dwsim_flash"
                state["property_package"] = document.get("property_package")
                known[stream["tag"]] = state
                if flashed is not None:
                    stream_results[stream["tag"]] = {**flashed, "owner": "jarvis_bio",
                                                       "state_source": "jarvis_unit → dwsim_flash"}
                else:
                    stream_results[stream["tag"]] = {**state, "state_source": "jarvis_unit",
                                                      "owner": "jarvis_bio"}
            unit_results[unit["tag"]] = evaluation.result
    produced: dict[str, dict[str, Any]] = {}
    for uid in part["consumed"]:
        unit = objects[uid]
        inlet = next(stream for stream in objects.values() if stream["kind"] == "stream"
                     and (stream.get("target") or {}).get("unit") == uid)
        outlet = next(stream for stream in objects.values() if stream["kind"] == "stream"
                      and (stream.get("source") or {}).get("unit") == uid)
        if inlet["tag"] not in known:
            raise SegmentFailure(unit["tag"], "Consumed tear inlet has no solved state")
        produced[outlet["tag"]] = known[inlet["tag"]]
        solved, guessed = known[inlet["tag"]], tear[outlet["tag"]]
        # DWSIM's Recycle row shape: {value, units}; mass flow error in kg/h, as DWSIM reports it.
        unit_results[unit["tag"]] = {
            "owner": "jarvis_bio", "calculated": True,
            "label": "Converged by Jarvis (cross-engine tear)",
            "reported": {
                "Mass Flow Error": {"value": (solved["mass_flow_kg_s"] - guessed["mass_flow_kg_s"]) * 3600,
                                    "units": "kg/h"},
                "Temperature Error": {"value": solved["temperature_K"] - guessed["temperature_K"], "units": "K"},
                "Pressure Error": {"value": solved["pressure_Pa"] - guessed["pressure_Pa"], "units": "Pa"}}}
    native_error = sum(_native_mass_flow_error_kg_s(unit_results.get(unit["tag"], {})) or 0.0
                       for unit in objects.values() if unit["kind"] == "unit" and unit["type"] == "Recycle"
                       and unit["id"] not in part["consumed"])
    culture_balances = _culture_balances(document, known, culture_generation, culture_generation_allowance,
                                         native_error)
    for tag, balances in culture_balances.items():
        if unit_results.get(tag, {}).get("owner") == "jarvis_bio":
            unit_results[tag]["unit_balances"] = balances
    phase_elapsed["culture"] = max(0.0, time.monotonic() - started
                                   - phase_elapsed["build"] - phase_elapsed["solve"])
    return {"produced": produced, "streams": stream_results, "units": unit_results,
            "segments": segment_records, "known": known, "culture_balances": culture_balances,
            "culture_generation": culture_generation,
            "culture_generation_allowance": culture_generation_allowance,
            "max_build_seconds": max_build_seconds, "elapsed_s_by_phase": phase_elapsed,
            "mixed_findings": mixed_findings}


def _pbr_process_recycle_findings(document: dict[str, Any], part: dict[str, Any],
                                 unit_results: dict[str, Any], workspace_id: str | None) -> list[dict[str, Any]]:
    """Warn when a converged Jarvis tear makes a fast PBR process inlet represent circulation."""
    if not part["consumed"]:
        return []
    objects = document["objects"]
    units = {item["id"]: item for item in objects.values() if item.get("kind") == "unit"}
    edges = mixed._edges(document, energy=False)
    cycle_groups = [group for group in mixed.components(set(units), edges)
                    if set(part["consumed"]) & group]
    warnings: list[dict[str, Any]] = []
    for group in cycle_groups:
        for unit_id in sorted(group, key=lambda uid: units[uid]["tag"]):
            unit = units[unit_id]
            if unit["type"] != "PhotobioreactorT1":
                continue
            result = unit_results.get(unit["tag"], {})
            dilution = ((result.get("reported") or {}).get("dilution_h") or {}).get("value")
            pin = unit.get("model") or {}
            if (not workspace_id or not isinstance(dilution, (int, float)) or not math.isfinite(dilution)
                    or not pin):
                continue
            model = pbr_unit._resolve(workspace_id, pin)
            mu_max_h = model.get("mu_max_h")
            if not isinstance(mu_max_h, (int, float)) or not math.isfinite(mu_max_h):
                continue
            if dilution > 10.0 * mu_max_h:
                warnings.append({
                    "severity": "warning", "code": "PBR_LOOP_AS_PROCESS_RECYCLE", "object": unit["tag"],
                    "field": "dilution_h",
                    "message": (f"Process-inlet dilution is {dilution:.4g} 1/h, more than 10 times the pinned card's "
                                f"μ_max ({mu_max_h:.4g} 1/h). In this steady tear cycle the loop behaves as internal "
                                "circulation; use a dynamic culture loop to represent it."),
                    "source": "jarvis",
                })
    return warnings


def _failure_record(document: dict[str, Any], part: dict[str, Any], *, reason: str, iteration: int | None,
                    segment: str, units: list[str] | None, errors: Any, history: list[dict[str, Any]],
                    phase_totals: dict[str, float],
                    settings: tear_solver.Settings | None = None) -> dict[str, Any]:
    """A segment_failed result with the same tag-based partition as a success record."""
    settings = settings or tear_solver.Settings()
    record: dict[str, Any] = {
        "status": "segment_failed", "reason": reason, "iteration": iteration,
        "failed_segment": segment, "failed_units": list(units or []),
        "errors": errors, "message": _failure_message(errors),
        "history": _history_record(history), "partition": _partition_record(document, part),
        "limits": {"iterations": settings.max_iterations, "wall_s": settings.wall_s},
        "solver": settings.record(), "tolerances": _tolerance_record(settings), "elapsed_s": phase_totals}
    diagnosis = _pressure_diagnosis(history)
    if diagnosis:
        record["diagnosis"] = diagnosis
    return {"status": "segment_failed", "mixed_solve": finite_json(record)}


def run(document: dict[str, Any], *, action: str, client: DwsimMcpClient,
        dwsim_version: str, mcp_sha256: str, run_dir: Path, workspace_id: str | None = None,
        timeout_s: float | None = None, max_iterations: int | None = None,
        initial_tear: dict[str, dict[str, Any]] | None = None,
        include_tear_state: bool = False, observer: Any = None) -> dict[str, Any]:
    """``observer`` (spec 210), when given, has ``stop_requested() -> bool`` (checked only at iteration
    boundaries) and ``iteration(row)`` (called with each stored history row as the controller produces it)."""
    return cast(dict[str, Any], finite_json(_run(
        document, action=action, client=client, dwsim_version=dwsim_version,
        mcp_sha256=mcp_sha256, run_dir=run_dir, workspace_id=workspace_id,
        timeout_s=timeout_s, max_iterations=max_iterations, initial_tear=initial_tear,
        include_tear_state=include_tear_state, observer=observer)))


def _run(document: dict[str, Any], *, action: str, client: DwsimMcpClient,
         dwsim_version: str, mcp_sha256: str, run_dir: Path, workspace_id: str | None = None,
         timeout_s: float | None = None, max_iterations: int | None = None,
         initial_tear: dict[str, dict[str, Any]] | None = None,
         include_tear_state: bool = False, observer: Any = None) -> dict[str, Any]:
    part = mixed.partition(document)
    run_dir.mkdir(parents=True, exist_ok=True)
    settings = tear_solver.parse_settings(document.get("solver"))
    wall_s = settings.wall_s
    initial = _seed(document, part, settings)
    run_cache: dict[str, Any] = {}
    started = time.monotonic()
    deadline = started + timeout_s if timeout_s is not None else None
    if initial_tear is not None:
        initial = copy.deepcopy(initial_tear)
    if action == "validate":
        try:
            validation_known = _validation_states(document, part, workspace_id) | _seed(document, part)
            for index, ids in enumerate(part["segments"]):
                segment = _segment_document(document, ids, validation_known)
                _full(segment, client, label=f"mixed-validate-{index}", keep_case=None,
                      dwsim_version=dwsim_version, mcp_sha256=mcp_sha256, action="validate")
        except SegmentFailure as exc:
            return _failure_record(document, part, reason="validation_failed", iteration=None,
                                   segment=exc.segment, units=exc.units, errors=exc.detail,
                                   history=[], phase_totals={"build": 0.0, "solve": 0.0, "culture": 0.0},
                                   settings=settings)
        return {"status": "validated", "mixed_solve": {"status": "validated",
                                                       "partition": _partition_record(document, part)}}
    t_build = 3.0  # initial assumption; replaced by the slowest build actually seen
    builds_seen = False
    terminal_flashes = sum(1 for stream in document["objects"].values()
                           if stream["kind"] == "stream" and (stream.get("source") or {}).get("unit") in part["jarvis_units"]
                           and (((stream.get("target") or {}).get("unit") is None)
                                or (stream.get("target") or {}).get("unit") in part["jarvis_units"]
                                or (stream.get("target") or {}).get("unit") in part["consumed"]))
    in_iteration_flashes = sum(1 for stream in document["objects"].values()
                               if stream["kind"] == "stream" and (stream.get("source") or {}).get("unit") in part["jarvis_units"]
                               and (stream.get("target") or {}).get("unit") in part["jarvis_units"])
    direct_jarvis_feeds = sum(1 for stream in _feeds(document)
                              if (stream.get("target") or {}).get("unit") in part["jarvis_units"])
    reserve_builds = len(part["segments"]) + terminal_flashes + direct_jarvis_feeds
    iteration_builds = len(part["segments"]) + in_iteration_flashes + direct_jarvis_feeds
    reserve_s = max(3.0, reserve_builds * max(3.0, 1.5 * t_build))
    def before_iteration(_iteration: int) -> str | None:
        nonlocal reserve_s
        if observer is not None and observer.stop_requested():
            return "operator_stop"
        remaining = wall_s - (time.monotonic() - started)
        if deadline is not None:
            remaining = min(remaining, deadline - time.monotonic())
        reserve_s = max(3.0, reserve_builds * max(3.0, 1.5 * t_build))
        if remaining < iteration_builds * t_build + reserve_s:
            return "wall_budget"
        return None

    candidates: dict[int, dict[str, Any]] = {}
    history_rows: list[dict[str, Any]] = []
    phase_totals = {"build": 0.0, "solve": 0.0, "culture": 0.0}

    def evaluate(tear_guess: dict[str, dict[str, Any]], iteration: int) -> dict[str, dict[str, Any]]:
        nonlocal t_build, builds_seen
        remaining = wall_s - (time.monotonic() - started)
        if deadline is not None:
            remaining = min(remaining, deadline - time.monotonic())
        try:
            candidate = _evaluate(document, part, tear_guess, client=client, full=False, run_dir=run_dir,
                                  iteration=iteration, dwsim_version=dwsim_version,
                                  mcp_sha256=mcp_sha256, remaining_s=remaining, run_cache=run_cache,
                                  workspace_id=workspace_id)
        except SegmentFailure as exc:
            raise SegmentFailure(exc.segment, {"iteration": iteration, "detail": exc.detail}, exc.units) from exc
        except Exception as exc:  # noqa: BLE001 - preserve engine failures in the mixed run record
            raise SegmentFailure("mixed", {"iteration": iteration, "code": type(exc).__name__,
                                           "message": str(exc)[:600]}) from exc
        candidates[iteration] = candidate
        for name in phase_totals:
            phase_totals[name] += candidate["elapsed_s_by_phase"][name]
        slowest = float(candidate["max_build_seconds"])
        if slowest > 0:
            # The slowest build seen; the 3 s assumption holds only before the first build.
            t_build = slowest if not builds_seen else max(t_build, slowest)
            builds_seen = True
        return candidate["produced"]

    def on_history(row: dict[str, Any]) -> None:
        history_rows.append(row)
        if observer is not None:
            observer.iteration(_history_record([row])[0])

    try:
        controller = mixed.iterate(initial, evaluate, before_iteration=before_iteration,
                                   on_history=on_history,
                                   max_iterations=max_iterations, settings=settings)
    except SegmentFailure as exc:
        detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return _failure_record(document, part, reason="segment_failed", iteration=detail.get("iteration"),
                               segment=exc.segment, units=exc.units,
                               errors=detail.get("detail", detail), history=history_rows,
                               phase_totals=phase_totals, settings=settings)
    tear = controller["iterate"]
    status, reason, history = controller["status"], controller["reason"], controller["history"]
    if reason == "wall_budget" and studio_unit.studio_units(document):
        return _failure_record(
            document, part, reason=reason, iteration=len(history), segment="mixed", units=None,
            errors={"code": "STUDIO_UNIT_TIMEOUT", "message": "The Process solve wall budget is insufficient for another sandbox evaluation."},
            history=history, phase_totals=phase_totals, settings=settings)
    last_iteration = history[-1]["iteration"] if history else None
    last = candidates.get(last_iteration) if last_iteration is not None else None
    final_remaining_s = min(wall_s - (time.monotonic() - started),
                            deadline - time.monotonic() if deadline is not None else math.inf)
    if final_remaining_s <= 0:
        return _failure_record(
            document, part, reason="wall_budget", iteration=len(history), segment="mixed", units=None,
            errors={"code": "STUDIO_UNIT_TIMEOUT" if studio_unit.studio_units(document) else "MIXED_WALL_TIMEOUT",
                    "message": "The Process solve wall budget expired before the final sweep."},
            history=history, phase_totals=phase_totals, settings=settings)
    try:
        reserve_s = max(3.0, reserve_builds * max(3.0, 1.5 * t_build))
        final = _evaluate(document, part, tear, client=client, full=True, run_dir=run_dir,
                          iteration=last_iteration or 0, dwsim_version=dwsim_version,
                          mcp_sha256=mcp_sha256,
                          remaining_s=final_remaining_s,
                          run_cache=run_cache, workspace_id=workspace_id)
    except SegmentFailure as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        reason = ("wall_budget" if "timeout" in str(detail.get("code", "")).lower()
                  or "budget" in str(detail.get("message", "")).lower() else "full_path_failed")
        return _failure_record(document, part, reason=reason, iteration=len(history),
                               segment=exc.segment, units=exc.units, errors=detail,
                               history=history, phase_totals=phase_totals, settings=settings)
    except Exception as exc:  # noqa: BLE001 - preserve unexpected full-path failures in this record
        return _failure_record(
            document, part,
            reason="wall_budget" if "timeout" in type(exc).__name__.lower() else "full_path_failed",
            iteration=len(history), segment="mixed", units=None,
            errors={"code": type(exc).__name__, "message": str(exc)[:600]},
            history=history, phase_totals=phase_totals, settings=settings)
    for name in phase_totals:
        phase_totals[name] += final["elapsed_s_by_phase"][name]
    mismatch = (_light_full_mismatch(last["produced"], final["produced"])
                if last is not None and controller["last_input"] == tear else None)
    balances = _whole_graph_balances(document, final, tear)
    status, reason = _final_status(status, reason, mismatch, balances)
    if status == "completed":
        final["mixed_findings"].extend(_pbr_process_recycle_findings(document, part, final["units"], workspace_id))
    record = {"status": status, "streams": final["streams"], "units": final["units"],
            "culture": _culture_results(document, final),
            "culture_findings": [],
            "mixed_findings": final["mixed_findings"],
            "process_fingerprint": mixed.fingerprint(document, part, workspace_id),
            "mixed_solve": {"status": status, "reason": reason, "version": mixed.MIXED_SOLVE_VERSION,
                            "method": settings.method, "history": _history_record(history),
                            "solver": settings.record(), "diagnostics": finite_json(controller.get("diagnostics", {})),
                            "culture_only": not part["consumed"],
                            "partition": _partition_record(document, part, final["segments"]),
                            "limits": {"iterations": settings.max_iterations if max_iterations is None
                                       else min(settings.max_iterations, max_iterations), "wall_s": wall_s,
                                       "reserve_s": reserve_s},
                            "tolerances": _tolerance_record(settings),
                            "budget": {"t_build_s": t_build, "reserve_s": reserve_s,
                                       "builds_per_iteration": iteration_builds,
                                       "final_sweep_builds": terminal_flashes},
                            "elapsed_s": phase_totals, "balances": balances["balances"],
                            **({"consistency_failure": mismatch} if mismatch else {}),
                            **({"diagnosis": _pressure_diagnosis(history)}
                               if status != "completed" and _pressure_diagnosis(history) else {}),
                            "results_label": "current" if status == "completed" else "Not converged — last iterate"}}
    if include_tear_state:
        record["_tear_state"] = tear
        record["_last_attempt"] = {"guess": controller.get("last_input"), "output": controller.get("last")}
    return record


def _tolerance_record(settings: tear_solver.Settings | None = None) -> dict[str, str]:
    t = (settings or tear_solver.Settings()).tolerances
    return {"mass_flow_kg_s": f"{t['mass_flow_rel']:g} * max(abs(x), 1e-6)",
            "temperature_K": f"{t['temperature_abs']:g}", "pressure_Pa": f"{t['pressure_rel']:g} * max(abs(x), 1)",
            "mass_fraction": f"{t['mass_fraction_abs']:g}", "culture": f"{t['culture_rel']:g} * abs(x) + 1e-12"}
