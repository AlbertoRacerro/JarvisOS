"""Graph partition and numerical primitives for the Jarvis/DWSIM process solve."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
from collections import defaultdict
from typing import Any

from app.modules.process_stack.draft_models import spec_for

MIXED_SOLVE_VERSION = 1
MAX_ITERATIONS = 25
WALL_BUDGET_S = 90.0
CULTURE_FIELDS = ("biomass", "nitrogen", "phosphorus", "oxygen", "dic", "salinity")


def _units(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in document["objects"].values() if item["kind"] == "unit"}


def _edges(document: dict[str, Any], *, energy: bool = True) -> list[tuple[str, str, dict[str, Any]]]:
    return [(stream["source"]["unit"], stream["target"]["unit"], stream)
            for stream in document["objects"].values()
            if stream["kind"] == "stream" and stream.get("source") and stream.get("target")
            and (energy or stream["type"] != "EnergyStream")]


def components(nodes: set[str], edges: list[tuple[str, str, Any]]) -> list[set[str]]:
    """Tarjan SCCs in stable order; also used by the instant cycle check."""
    graph: dict[str, list[str]] = {node: [] for node in nodes}
    for source, target, _ in edges:
        graph[source].append(target)
    for targets in graph.values():
        targets.sort()
    index = 0
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[set[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph[node]:
            if target not in indices:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], indices[target])
        if low[node] == indices[node]:
            group: set[str] = set()
            while True:
                member = stack.pop()
                on_stack.remove(member)
                group.add(member)
                if member == node:
                    break
            result.append(group)

    for node in sorted(nodes):
        if node not in indices:
            visit(node)
    return result


def recycle_free_cycles(document: dict[str, Any]) -> list[list[str]]:
    units = _units(document)
    edges = [(a, b, stream) for a, b, stream in _edges(document)
             if units[a]["type"] != "Recycle"]
    loops = [group for group in components(set(units), edges)
             if len(group) > 1 or any(a == b and a in group for a, b, _ in edges)]
    return sorted(sorted(units[uid]["tag"] for uid in group) for group in loops)


def partition(document: dict[str, Any]) -> dict[str, Any]:
    units = _units(document)
    edges = _edges(document)
    material = _edges(document, energy=False)
    sccs = components(set(units), material)
    member_group = {uid: index for index, group in enumerate(sccs) for uid in group}
    consumed = {uid for uid, unit in units.items() if unit["type"] == "Recycle"
                and any(spec_for(units[other]).owner == "jarvis_bio"
                        for other in sccs[member_group[uid]])
                and (len(sccs[member_group[uid]]) > 1 or any(a == b == uid for a, b, _ in material))}
    cut = [(a, b, stream) for a, b, stream in edges if a not in consumed]
    # A native Recycle is allowed only inside one DWSIM SCC, and not on the consumed cycle.
    native_sccs = components(set(units), cut)
    group_id = {uid: index for index, group in enumerate(native_sccs) for uid in group}
    dag: dict[int, set[int]] = defaultdict(set)
    indegree = {index: 0 for index in range(len(native_sccs))}
    for a, b, _ in cut:
        source, target = group_id[a], group_id[b]
        if source != target and target not in dag[source]:
            dag[source].add(target)
            indegree[target] += 1
    ready = sorted(index for index, degree in indegree.items() if degree == 0)
    order: list[int] = []
    level = {index: 0 for index in indegree}
    while ready:
        source = ready.pop(0)
        order.append(source)
        jarvis_count = int(any(spec_for(units[uid]).owner == "jarvis_bio"
                               for uid in native_sccs[source]))
        for target in sorted(dag[source]):
            level[target] = max(level[target], level[source] + jarvis_count)
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    if len(order) != len(native_sccs):
        raise ValueError("partition remains cyclic after consumed tears are cut")
    levels = {uid: level[group_id[uid]] for uid in units}
    dwsim = {uid for uid, unit in units.items() if spec_for(unit).owner == "dwsim" and uid not in consumed}
    neighbors: dict[str, set[str]] = {uid: set() for uid in dwsim}
    for a, b, stream in cut:
        if a in dwsim and b in dwsim and levels[a] == levels[b]:
            neighbors[a].add(b)
            neighbors[b].add(a)
        elif stream["type"] == "EnergyStream" and levels[a] != levels[b]:
            raise ValueError("ENERGY_STREAM_CROSSES_JARVIS_LEVEL")
    segments: list[list[str]] = []
    pending = set(dwsim)
    while pending:
        group = {min(pending)}
        fringe = list(group)
        while fringe:
            for other in sorted(neighbors[fringe.pop()] - group):
                group.add(other)
                fringe.append(other)
        pending -= group
        segments.append(sorted(group, key=lambda uid: units[uid]["tag"]))
    segments.sort(key=lambda group: (levels[group[0]], units[group[0]]["tag"]))
    # Exclude native Recycles in a DWSIM component on an outer Jarvis cycle.
    for segment in segments:
        if any(units[uid]["type"] == "Recycle" for uid in segment):
            if any(member_group[uid] == member_group[tear] for uid in segment for tear in consumed):
                raise ValueError("NATIVE_RECYCLE_IN_JARVIS_LOOP")
    return {"consumed": sorted(consumed, key=lambda uid: units[uid]["tag"]),
            "segments": segments, "levels": levels,
            "jarvis_units": sorted((uid for uid, unit in units.items()
                                    if spec_for(unit).owner == "jarvis_bio"),
                                   key=lambda uid: units[uid]["tag"])}


def has_jarvis_unit(document: dict[str, Any]) -> bool:
    return any(item["kind"] == "unit" and spec_for(item).owner == "jarvis_bio"
               for item in document["objects"].values())


def needs_mixed_solve(document: dict[str, Any]) -> bool:
    """Keep the legacy byte-identical path unless a Jarvis unit or culture loop needs iteration."""
    if has_jarvis_unit(document):
        return True
    units = _units(document)
    edges = _edges(document, energy=False)
    graph: dict[str, set[str]] = defaultdict(set)
    for source, target, _stream in edges:
        graph[source].add(target)
    culture_starts = {stream["target"]["unit"] for stream in document["objects"].values()
                      if stream["kind"] == "stream" and stream.get("source") is None
                      and stream.get("target") and stream.get("spec", {}).get("culture") is not None}
    reachable: set[str] = set()
    pending = list(culture_starts)
    while pending:
        current = pending.pop()
        if current not in reachable:
            reachable.add(current)
            pending.extend(graph.get(current, ()))
    groups = components(set(units), edges)
    for group in groups:
        has_cycle = len(group) > 1 or any(a == b and a in group for a, b, _ in edges)
        if not has_cycle or not any(units[uid]["type"] == "Recycle" for uid in group):
            continue
        if group & reachable:
            return True
    return False


def validation_findings(document: dict[str, Any]) -> list[dict[str, Any]]:
    objects = document["objects"]
    units = _units(document)
    findings: list[dict[str, Any]] = []

    def add(severity: str, code: str, tag: str, message: str) -> None:
        findings.append({"severity": severity, "code": code, "object": tag, "field": "connections",
                         "message": message, "source": "jarvis"})

    if not has_jarvis_unit(document):
        return findings
    cultured = {item["id"] for item in objects.values() if item["kind"] == "stream"
                and item.get("source") is None and item.get("spec", {}).get("culture") is not None}
    queue = list(cultured)
    while queue:
        stream = objects[queue.pop()]
        target = (stream.get("target") or {}).get("unit")
        if target is None:
            continue
        for downstream in objects.values():
            if downstream["kind"] == "stream" and (downstream.get("source") or {}).get("unit") == target:
                if downstream["id"] not in cultured:
                    cultured.add(downstream["id"])
                    queue.append(downstream["id"])
    for unit in units.values():
        if unit["type"] != "SpecifiedSeparator":
            continue
        incoming = [stream for stream in objects.values() if stream["kind"] == "stream"
                    and (stream.get("target") or {}).get("unit") == unit["id"]]
        if not incoming or incoming[0]["id"] not in cultured:
            add("blocker", "SEPARATOR_REQUIRES_CULTURE", unit["tag"],
                f"Specified separator {unit['tag']} needs a culture-carrying inlet.")
        factor = float(unit.get("params", {}).get("concentration_factor", {}).get("si", 0))
        feed_values = [float((stream.get("spec", {}).get("culture") or {}).get("biomass", {}).get("si", 0))
                       for stream in objects.values() if stream["kind"] == "stream" and stream["id"] in cultured
                       and stream.get("source") is None]
        if feed_values and factor * max(feed_values) / 1000 > 0.25:
            add("warning", "SEPARATOR_CONCENTRATE_IMPLAUSIBLE", unit["tag"],
                "Specified concentrate exceeds the 0.25 kg/kg screening bound.")
    try:
        value = partition(document)
    except ValueError as exc:
        code = str(exc)
        if code in {"ENERGY_STREAM_CROSSES_JARVIS_LEVEL", "NATIVE_RECYCLE_IN_JARVIS_LOOP"}:
            add("blocker", code, "", code.replace("_", " ").capitalize() + ".")
        else:
            add("blocker", "MIXED_PARTITION_INVALID", "", f"The process cannot be partitioned for a mixed solve: {code}.")
    else:
        for uid in value["consumed"]:
            add("info", "TEAR_CONSUMED", units[uid]["tag"],
                f"Jarvis converges Recycle {units[uid]['tag']} as a cross-engine tear.")
            outputs = [stream for stream in objects.values() if stream["kind"] == "stream"
                       and (stream.get("source") or {}).get("unit") == uid]
            if any((stream.get("target") or {}).get("unit") in units
                   and units[stream["target"]["unit"]]["type"] == "HeatExchanger" for stream in outputs):
                add("info", "TEAR_CONSUMER_ZERO_FLOW", units[uid]["tag"],
                    "HeatExchanger needs a larger initial tear flow; use 0.001 times total feed flow.")
    return findings


def _pbr_set_digest(workspace_id: str | None, unit: dict[str, Any]) -> str:
    """Resolved parameter-set digest behind a unit's model pin; a stable sentinel when it cannot resolve."""
    pin = unit.get("model") or {}
    if workspace_id is None or not pin:
        return "unresolved"
    try:
        from app.modules.bio_models import service as bio_models

        resolved = bio_models.resolve_growth_model(
            workspace_id, pin["card_id"], pin["card_revision"], pin["card_digest"])
        return str(resolved["set"]["digest"])
    except Exception:  # noqa: BLE001 - any resolution failure is one stable sentinel, never a crash
        return "unresolved"


def fingerprint(document: dict[str, Any], partition_value: dict[str, Any], workspace_id: str | None = None) -> str:
    """Layout-free process fingerprint.

    Photobioreactor parameters and the model pin are already part of the document meaning. When the draft has a
    PBR the fingerprint additionally binds the evaluator id and model version and the resolved set digest, so a
    re-pin or a changed evaluator stales results. Drafts without a PBR keep their earlier fingerprints exactly.
    """
    meaning = copy.deepcopy(document)
    meaning.pop("name", None)
    for item in meaning["objects"].values():
        for field in ("x", "y", "route", "orientation", "flip_x", "flip_y"):
            item.pop(field, None)
    payload: dict[str, Any] = {"document": meaning, "partition": partition_value, "version": MIXED_SOLVE_VERSION,
                               "culture_schema": "jarvis_culture_result/1",
                               "culture_propagation": "jarvis_culture_propagation/1",
                               "evaluators": {"SpecifiedSeparator": 1}}
    if any(reaction.get("rate_law") for reaction in (meaning.get("reactions") or {}).values()):
        from app.modules.process_stack.kinetics import KINETICS_VERSION

        payload["kinetics_version"] = KINETICS_VERSION
    pbr_units = {uid: item for uid, item in meaning["objects"].items()
                 if item["kind"] == "unit" and item["type"] == "PhotobioreactorT1"}
    if pbr_units:
        from app.modules.process_stack import pbr_unit

        payload["evaluators"]["PhotobioreactorT1"] = {"id": pbr_unit.EVALUATOR_ID, "version": pbr_unit.MODEL_VERSION}
        payload["pbr_model_sets"] = {uid: _pbr_set_digest(workspace_id, item) for uid, item in sorted(pbr_units.items())}
    studio_pins = {uid: item.get("model") for uid, item in meaning["objects"].items()
                   if item["kind"] == "unit" and item["type"] == "StudioUnit"}
    if studio_pins:
        # Spec 190: the pinned version and both hashes are part of the process meaning; flowsheets without one are unchanged.
        payload["evaluators"]["StudioUnit"] = {"id": "studio_unit_v1", "host_checks": 1}
        payload["studio_unit_pins"] = dict(sorted(studio_pins.items()))
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def tolerance(name: str, value: float | None) -> float:
    if name == "mass_flow_kg_s":
        return 1e-5 * max(abs(value or 0.0), 1e-6)
    if name == "temperature_K":
        return 0.01
    if name == "pressure_Pa":
        return 1e-6 * max(abs(value or 0.0), 1.0)
    if name.startswith("mass_fraction."):
        return 1e-7
    return 1e-5 * abs(value or 0.0) + 1e-12


def residual(guess: dict[str, Any], output: dict[str, Any]) -> tuple[float, str, dict[str, float]]:
    fields: dict[str, float] = {}
    for name in sorted(set(guess) | set(output)):
        a, b = guess.get(name), output.get(name)
        if a is None and b is None:
            normalized = 0.0
        elif a is None or b is None:
            normalized = math.inf
        else:
            normalized = abs(float(b) - float(a)) / tolerance(name, float(a))
        fields[name] = normalized
    worst = max(fields, key=fields.__getitem__) if fields else ""
    return fields.get(worst, 0.0), worst, fields


def residual_values(guess: dict[str, Any], output: dict[str, Any]) -> dict[str, float | None]:
    """Signed, undamped physical residuals, kept separately from normalized convergence values."""
    values: dict[str, float | None] = {}
    for name in sorted(set(guess) | set(output)):
        before, after = guess.get(name), output.get(name)
        values[name] = None if before is None or after is None else float(after) - float(before)
    return values


def iterate(initial: dict[str, dict[str, Any]], evaluate: Any, *,
            before_iteration: Any = None, on_history: Any = None,
            max_iterations: int | None = None, settings: Any = None) -> dict[str, Any]:
    """Engine-neutral tear controller; evaluate returns g(x_k). The method and limits come from `settings` (183)."""
    from app.modules.process_stack import tear_solver

    if settings is None:
        settings = tear_solver.Settings()
    if max_iterations is not None:
        # A caller budget (the dynamic sampler) can only lower the operator limit.
        settings = dataclasses.replace(settings, max_iterations=max(1, min(int(max_iterations), settings.max_iterations)))
    return tear_solver.solve(initial, evaluate, settings=settings, before_iteration=before_iteration,
                             on_history=on_history)


def _flatten_state(state: dict[str, Any]) -> dict[str, Any]:
    row = {name: state.get(name) for name in ("temperature_K", "pressure_Pa", "mass_flow_kg_s")}
    row.update({"mass_fraction." + name: value for name, value in state.get("mass_fractions", {}).items()})
    row.update({name: state.get("culture", {}).get(name) for name in CULTURE_FIELDS})
    return row


def separator(inlet: dict[str, Any], recovery_percent: float, factor: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Specified performance in carrier mass; culture remains mass-specific."""
    if not 0 < recovery_percent <= 100 or not 1 < factor <= 1000:
        raise ValueError("separator parameters are outside their accepted range")
    if inlet.get("vapor_fraction") is None or inlet["vapor_fraction"] > 1e-6:
        raise ValueError("separator inlet must have a solved liquid DWSIM state")
    flow = float(inlet["mass_flow_kg_s"])
    recovery = recovery_percent / 100.0
    concentrate_flow = recovery / factor * flow
    clarified_flow = flow - concentrate_flow
    x = inlet.get("culture", {}).get("biomass")
    common = {key: copy.deepcopy(value) for key, value in inlet.items() if key != "culture"}
    culture = inlet.get("culture") or {}
    concentrate = {**common, "mass_flow_kg_s": concentrate_flow, "culture": dict(culture), "owner": "jarvis_bio"}
    clarified = {**common, "mass_flow_kg_s": clarified_flow, "culture": dict(culture), "owner": "jarvis_bio"}
    concentrate["culture"]["biomass"] = None if x is None else x if flow == 0 else factor * x
    clarified["culture"]["biomass"] = None if x is None else ((1 - recovery) * x * flow / clarified_flow if flow else x)
    return concentrate, clarified
