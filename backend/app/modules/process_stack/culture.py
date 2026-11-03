"""Jarvis-owned culture validation and post-DWSIM pass-through propagation (spec 167)."""

from __future__ import annotations

import math
from collections import deque
from typing import Any

from app.modules.process_stack.draft_models import spec_for

PROPAGATION_VERSION = "jarvis_culture_propagation/1"
RESULT_SCHEMA_VERSION = "jarvis_culture_result/1"
FIDELITY = "screening — pass-through, no reaction or gas transfer"
PH_REASON = "screening placeholder; mixed pH requires carbonate speciation (175)"
FIELD_KINDS = {
    "biomass": "mass_concentration", "nitrogen": "mass_concentration",
    "phosphorus": "mass_concentration", "oxygen": "mass_concentration",
    "dic": "molar_concentration", "ph": "ph", "salinity": "salinity",
}
CONSERVED = ("biomass", "nitrogen", "phosphorus", "oxygen", "dic", "salinity")
MOLECULAR_WEIGHT_KG_PER_KMOL = {
    "Water": 18.01528, "Methanol": 32.04186, "Ethanol": 46.06844, "Acetone": 58.080,
    "Benzene": 78.11184, "Toluene": 92.13842, "Nitrogen": 28.0134, "Oxygen": 31.9988,
    "Carbon dioxide": 44.0095, "Methane": 16.0425, "Ethane": 30.069, "Propane": 44.09562,
    "N-butane": 58.1222, "N-hexane": 86.17536, "Hydrogen": 2.01588, "Argon": 39.948,
    "Ammonia": 17.03052, "Acetic acid": 60.052, "Isopropanol": 60.09502,
    "Ethylene oxide": 44.05256, "Ethylene glycol": 62.06784,
}


def _common_fields(sets: list[set[str]]) -> set[str]:
    """Intersection of the known-field sets of every input stream."""
    common = set(sets[0])
    for other in sets[1:]:
        common &= other
    return common


def _add(items: list[dict[str, Any]], severity: str, code: str, tag: str, field: str, message: str) -> None:
    items.append({"severity": severity, "code": code, "object": tag, "field": field,
                  "message": message, "source": "jarvis"})


def _stream_inputs(document: dict[str, Any], unit: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted((stream for stream in document["objects"].values()
                   if stream["kind"] == "stream" and (stream.get("target") or {}).get("unit") == unit["id"]
                   and stream["type"] != "EnergyStream"), key=lambda item: item["target"]["port"])


def _stream_outputs(document: dict[str, Any], unit: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted((stream for stream in document["objects"].values()
                   if stream["kind"] == "stream" and (stream.get("source") or {}).get("unit") == unit["id"]
                   and stream["type"] != "EnergyStream"), key=lambda item: item["source"]["port"])


def _water_mass_fraction(feed: dict[str, Any]) -> float:
    composition = feed.get("spec", {}).get("composition") or {}
    if feed.get("spec", {}).get("composition_basis") != "mole":
        return float(composition.get("Water", 0.0))
    mass = {name: float(value) * MOLECULAR_WEIGHT_KG_PER_KMOL[name]
            for name, value in composition.items() if name in MOLECULAR_WEIGHT_KG_PER_KMOL}
    total = sum(mass.values())
    return mass.get("Water", 0.0) / total if total > 0 else 0.0


def known_culture_fields(document: dict[str, Any], feeds: list[dict[str, Any]],
                         consumed: set[str]) -> dict[str, set[str]]:
    """Statically known conserved culture fields per culture-carrying stream id (spec 170).

    Mirrors the 167/168 rules without solving: a feed knows exactly the fields it specifies (an explicit
    zero is known), a Mixer knows a field only when every culture inlet does, a consumed tear is seeded
    with the fields every culture feed specifies, and a Photobioreactor adds the three fields it computes.
    """
    objects = document["objects"]
    units = [item for item in objects.values() if item["kind"] == "unit"]
    known: dict[str, set[str]] = {feed["id"]: set(feed["spec"]["culture"]) & set(CONSERVED) for feed in feeds}
    seed = set(CONSERVED)
    for feed in feeds:
        seed &= known[feed["id"]]
    tear_ids = {stream["id"] for stream in objects.values() if stream["kind"] == "stream"
                and (stream.get("source") or {}).get("unit") in consumed}
    for _ in range(len(objects) + 2):
        previous = {key: set(value) for key, value in known.items()}
        updated: dict[str, set[str]] = {feed["id"]: previous[feed["id"]] for feed in feeds}
        for tear in tear_ids:
            updated[tear] = set(seed)
        for unit in sorted(units, key=lambda item: item["tag"]):
            inputs = [stream for stream in _stream_inputs(document, unit) if stream["id"] in previous]
            if not inputs:
                continue
            outputs = _stream_outputs(document, unit)
            if unit["id"] in consumed:
                for output in outputs:
                    updated[output["id"]] = set(seed) & _common_fields([previous[s["id"]] for s in inputs])
                continue
            if unit["type"] == "HeatExchanger":
                for output in outputs:
                    side = next((s for s in inputs if s["target"]["port"] == output["source"]["port"]), None)
                    if side is not None:
                        updated[output["id"]] = set(previous[side["id"]])
                continue
            fields = _common_fields([previous[s["id"]] for s in inputs])
            if spec_for(unit).culture_rule == "pbr":
                fields |= {"biomass", "nitrogen", "oxygen"}
            for output in outputs:
                updated[output["id"]] = set(fields)
        if updated == previous:
            break
        known = updated
    return known


_PBR_FIELD_CODES = (("biomass", "PBR_REQUIRES_BIOMASS"), ("nitrogen", "PBR_REQUIRES_NITROGEN"),
                    ("oxygen", "PBR_REQUIRES_OXYGEN"))


def culture_findings(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Instant input, carrier, unit-rule and cycle findings; empty for culture-free drafts."""
    objects = document["objects"]
    feeds = [item for item in objects.values() if item["kind"] == "stream" and item["type"] != "EnergyStream"
             and item.get("source") is None and item.get("spec", {}).get("culture") is not None]
    pbr_units = sorted((item for item in objects.values()
                        if item["kind"] == "unit" and spec_for(item).culture_rule == "pbr"),
                       key=lambda item: item["tag"])
    if not feeds and not pbr_units:
        return []
    findings: list[dict[str, Any]] = []
    culture_streams = {stream["id"]: stream for stream in feeds}
    from app.modules.process_stack import mixed

    try:
        consumed = set(mixed.partition(document)["consumed"]) if mixed.has_jarvis_unit(document) else set()
    except ValueError:
        # The mixed validator owns the partition refusal; findings must remain readable.
        consumed = set()
    tears = [stream for stream in objects.values() if stream["kind"] == "stream"
             and (stream.get("source") or {}).get("unit") in consumed]
    culture_streams.update({stream["id"]: stream for stream in tears})
    queue = deque([*feeds, *tears])
    visited_units: set[str] = set()
    while queue:
        stream = queue.popleft()
        culture = stream.get("spec", {}).get("culture") or {}
        if stream in feeds:
            if "biomass" not in culture:
                _add(findings, "blocker", "CULTURE_FIELD_REQUIRED", stream["tag"], "biomass",
                     f"Culture feed {stream['tag']} must specify biomass.")
            if "salinity" not in culture:
                _add(findings, "blocker", "CULTURE_FIELD_REQUIRED", stream["tag"], "salinity",
                     f"Culture feed {stream['tag']} must specify salinity.")
            wf = _water_mass_fraction(stream)
            if "Water" not in (stream.get("spec", {}).get("composition") or {}) or wf < 0.5:
                _add(findings, "blocker", "CULTURE_CARRIER_NOT_AQUEOUS", stream["tag"], "composition",
                     "A culture feed needs Water and at least 0.5 Water mass fraction.")
            if wf < 0.95:
                _add(findings, "warning", "CULTURE_CARRIER_IMPURE", stream["tag"], "composition",
                     "Water mass fraction is below 0.95; this is a screening bound, not a scientific limit.")
            if float((stream.get("spec", {}).get("vapor_fraction") or {}).get("si", 0.0)) > 0:
                _add(findings, "blocker", "CULTURE_FEED_NOT_LIQUID", stream["tag"], "vapor_fraction",
                     "Culture feeds must be specified as liquid.")
        for name, quantity in culture.items():
            if name not in FIELD_KINDS:
                continue
            value = float(quantity.get("si", math.nan))
            invalid = not math.isfinite(value) or name != "ph" and value < 0
            invalid |= name == "ph" and not 0 <= value <= 14
            invalid |= name == "salinity" and value > 300
            if invalid:
                _add(findings, "blocker", "CULTURE_VALUE_OUT_OF_RANGE", stream["tag"], name,
                     f"Culture field {name} is outside its accepted range.")
        endpoint = stream.get("target")
        if endpoint is None:
            continue
        unit = objects.get(endpoint["unit"])
        if unit is None or unit["id"] in visited_units:
            continue
        visited_units.add(unit["id"])
        rule = spec_for(unit).culture_rule
        if rule == "refuse":
            _add(findings, "blocker", "CULTURE_UNIT_UNSUPPORTED", unit["tag"], "culture",
                 f"Culture cannot pass through {unit['type']} {unit['tag']} because DWSIM VLE or reactors do not preserve biology.")
            continue
        if unit["type"] == "Recycle" and not _on_material_cycle(document, unit["id"]):
            # 168 lets culture ride a Recycle only as a loop's tear; off any loop it is still refused.
            _add(findings, "blocker", "CULTURE_UNIT_UNSUPPORTED", unit["tag"], "culture",
                 f"Culture cannot pass through {unit['type']} {unit['tag']} because it does not close a loop.")
            continue
        inputs = _stream_inputs(document, unit)
        if unit["type"] == "Mixer" and any(s["id"] in culture_streams for s in inputs) and any(
            s["id"] not in culture_streams for s in inputs
        ):
            _add(findings, "warning", "CULTURE_MIXED_WITH_UNSPECIFIED", unit["tag"], "culture",
                 "Non-culture Mixer inlets contribute zero biomass, nutrients, O₂, DIC and salinity.")
        for output in _stream_outputs(document, unit):
            culture_streams[output["id"]] = output
            queue.append(output)

    if pbr_units:
        known = known_culture_fields(document, feeds, set(consumed))
        for unit in pbr_units:
            inlet = next(iter(_stream_inputs(document, unit)), None)
            if inlet is None:
                continue  # the missing inlet is reported by the generic port check
            if not feeds or inlet["id"] not in culture_streams:
                _add(findings, "blocker", "PBR_REQUIRES_CULTURE_INLET", unit["tag"], "inlets",
                     f"{unit['tag']} needs an inlet that carries culture; connect a culture feed or a culture-carrying stream.")
                continue
            for field, code in _PBR_FIELD_CODES:
                if field not in known.get(inlet["id"], set()):
                    _add(findings, "blocker", code, unit["tag"], field,
                         f"{unit['tag']} needs the inlet {field} specified or known; an explicit zero is valid, "
                         "an unspecified or unknown value is not assumed to be zero.")

    return findings


def _on_material_cycle(document: dict[str, Any], unit_id: str) -> bool:
    from app.modules.process_stack import mixed

    edges = mixed._edges(document, energy=False)
    group = next(group for group in mixed.components(set(mixed._units(document)), edges) if unit_id in group)
    return len(group) > 1 or any(a == b == unit_id for a, b, _ in edges)


def _reachable(graph: dict[str, set[str]], start: str, goal: str) -> bool:
    pending, seen = [start], set()
    while pending:
        item = pending.pop()
        if item == goal:
            return True
        if item not in seen:
            seen.add(item)
            pending.extend(graph.get(item, ()))
    return False


def _density(result: dict[str, Any]) -> float | None:
    reported = result.get("reported") or result
    phases = reported.get("phases", []) if isinstance(reported, dict) else []
    mixture = next((phase for phase in phases if isinstance(phase, dict) and phase.get("name") == "Mixture"), {})
    value = mixture.get("density_kg_m3")
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) and value > 0 else None


def _display_values(state: dict[str, Any], density: float) -> dict[str, Any]:
    values = {}
    for name, value in state["mass_specific"].items():
        if value is None:
            values[name] = {"mass_specific": None, "display": None, "reason": state["reasons"].get(name)}
        else:
            si = value * density if name in FIELD_KINDS and name not in {"salinity", "ph"} else value
            unit = "kg/m3" if name in {"biomass", "nitrogen", "phosphorus", "oxygen"} else "mol/m3" if name == "dic" else "g/kg" if name == "salinity" else "pH"
            values[name] = {"mass_specific": value, "si": si, "display": {"value": si, "unit": unit}}
    return values


def mix_mass_specific(cultured: list[tuple[float, dict[str, Any]]], total_flow: float,
                      input_count: int, unit_tag: str) -> tuple[dict[str, float | None], dict[str, str]]:
    """Mixer weighting shared by the single-owner and mixed-engine paths (spec 167 rules).

    ``cultured`` holds (mass flow, {"mass_specific", "reasons"}) of the culture-carrying inlets only.
    ``total_flow`` is the flow of *all* inlets, so inlets without culture dilute as zero culture;
    a field unknown on any culture inlet is unknown on the outlet. The pH placeholder survives only
    when every inlet carries culture with pH within 0.01.
    """
    mixed: dict[str, float | None] = {}
    reasons: dict[str, str] = {}
    states = [state for _flow, state in cultured]
    for name in CONSERVED:
        if any(name not in state["mass_specific"] or state["mass_specific"][name] is None for state in states):
            mixed[name] = None
            missing = next((state["reasons"].get(name) for state in states
                            if state["mass_specific"].get(name) is None), None)
            reasons[name] = missing or f"unknown on a culture inlet to Mixer {unit_tag}"
        else:
            amount = sum(flow * float(state["mass_specific"][name]) for flow, state in cultured)
            mixed[name] = amount / total_flow
    pHs: list[float | None] = [state["mass_specific"].get("ph") for state in states]
    placeholder = bool(len(cultured) == input_count and pHs and all(value is not None for value in pHs)
                       and max(value for value in pHs if value is not None)
                       - min(value for value in pHs if value is not None) <= 0.01)
    if placeholder:
        assert pHs[0] is not None
        mixed["ph"] = pHs[0]
    else:
        mixed["ph"] = None
    reasons["ph"] = PH_REASON
    return mixed, reasons


def propagate(document: dict[str, Any], streams: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Propagate culture after solve; all concentrations are specific to total stream mass."""
    feeds = [item for item in document["objects"].values() if item["kind"] == "stream"
             and item["type"] != "EnergyStream" and item.get("source") is None
             and item.get("spec", {}).get("culture") is not None]
    if not feeds:
        return {}, []
    results: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []
    states: dict[str, dict[str, Any]] = {}
    failed: set[str] = set()

    def fail(stream: dict[str, Any], code: str, message: str) -> None:
        failed.add(stream["id"])
        results[stream["tag"]] = {"owner": "jarvis", "propagation_version": PROPAGATION_VERSION,
                                   "status": "failed", "values": {}, "finding": code, "message": message,
                                   "fidelity": FIDELITY}
        _add(findings, "blocker", code, stream["tag"], "culture", message)

    for feed in feeds:
        solved_feed = streams.get(feed["tag"], {})
        vapor = solved_feed.get("vapor_fraction")
        if isinstance(vapor, (int, float)) and math.isfinite(vapor) and vapor > 1e-6:
            fail(feed, "CULTURE_PHASE_NOT_LIQUID",
                 f"Culture feed {feed['tag']} is not liquid after solve (vapor fraction {vapor:.6g}).")
            continue
        density = _density(streams.get(feed["tag"], {}))
        if density is None:
            fail(feed, "CULTURE_DENSITY_UNAVAILABLE", f"Culture result for {feed['tag']} needs a finite positive DWSIM Mixture density.")
            continue
        flow = streams.get(feed["tag"], {}).get("mass_flow_kg_s")
        if not isinstance(flow, (int, float)) or not math.isfinite(flow):
            fail(feed, "CULTURE_FLOW_UNAVAILABLE", f"Culture result for {feed['tag']} needs a finite DWSIM mass flow.")
            continue
        culture = feed["spec"]["culture"]
        mass_specific: dict[str, float | None] = {}
        reasons: dict[str, str] = {}
        for name in CONSERVED:
            if name not in culture:
                mass_specific[name] = None
                reasons[name] = f"not specified on feed {feed['tag']}"
            else:
                factor = 1.0 if name == "salinity" else density
                mass_specific[name] = float(culture[name]["si"]) / factor
        mass_specific["ph"] = float(culture["ph"]["si"]) if "ph" in culture else None
        if "ph" not in culture:
            reasons["ph"] = f"not specified on feed {feed['tag']}"
        feed_state = {"mass_specific": mass_specific, "reasons": reasons}
        states[feed["id"]] = feed_state
        results[feed["tag"]] = {
            "owner": "jarvis", "propagation_version": PROPAGATION_VERSION, "status": "completed",
            "density_kg_m3": density, "values": _display_values(feed_state, density), "unit_balances": {},
            "fidelity": FIDELITY,
            "caveats": ["DWSIM mixture density uses seawater-as-water approximation.",
                        "Dissolved O₂ is carried without solubility or degassing changes; heated streams may be supersaturated."],
            "pH_reason": reasons.get("ph"),
        }

    units = {item["id"]: item for item in document["objects"].values() if item["kind"] == "unit"}
    pending: set[str] = set()
    reach_queue = deque(feeds)
    reached_streams = {stream["id"] for stream in feeds}
    while reach_queue:
        stream = reach_queue.popleft()
        endpoint = stream.get("target")
        if endpoint is None or endpoint["unit"] not in units:
            continue
        unit = units[endpoint["unit"]]
        if unit["id"] in pending:
            continue
        pending.add(unit["id"])
        for output in _stream_outputs(document, unit):
            if output["id"] not in reached_streams:
                reached_streams.add(output["id"])
                reach_queue.append(output)
    ordered: list[str] = []
    while pending:
        ready = [uid for uid in pending if all(
            (s.get("source") or {}).get("unit") not in pending
            for s in _stream_inputs(document, units[uid])
        )]
        if not ready:
            break
        ready.sort(key=lambda uid: units[uid]["tag"])
        ordered.extend(ready)
        pending.difference_update(ready)

    for unit_id in ordered:
        unit = units[unit_id]
        inputs = _stream_inputs(document, unit)
        cultured_inputs = [s for s in inputs if s["id"] in states or s["id"] in failed]
        if not cultured_inputs:
            continue
        outputs = _stream_outputs(document, unit)
        if unit["type"] == "HeatExchanger":
            for output in outputs:
                source = next((item for item in inputs if item["target"]["port"] == output["source"]["port"]), None)
                if source is None:
                    continue
                if source["id"] in failed:
                    fail(output, "CULTURE_UPSTREAM_FAILED", f"Culture result failed downstream of {unit['tag']}.")
                    continue
                if source["id"] not in states:
                    continue
                flow_in = streams.get(source["tag"], {}).get("mass_flow_kg_s")
                flow_out = streams.get(output["tag"], {}).get("mass_flow_kg_s")
                if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in (flow_in, flow_out)):
                    fail(output, "CULTURE_FLOW_UNAVAILABLE", f"Heat exchanger side {unit['tag']} needs finite stream mass flows.")
                    continue
                state = states[source["id"]]
                balances = {}
                for name in CONSERVED:
                    value = state["mass_specific"].get(name)
                    if value is None:
                        continue
                    unit_factor = 0.001 if name == "salinity" else 1.0
                    inbound, outbound = float(flow_in) * value * unit_factor, float(flow_out) * value * unit_factor
                    residual = inbound - outbound
                    tolerance = 1e-9 * max(abs(inbound), abs(outbound)) + 1e-12
                    balances[name] = {"in": inbound, "out": outbound, "residual": residual,
                                      "tolerance": tolerance, "unit": "mol/s" if name == "dic" else "kg/s",
                                      "passed": abs(residual) <= tolerance}
                if any(not item["passed"] for item in balances.values()):
                    fail(output, "CULTURE_BALANCE_FAILED", f"Culture balance failed at {unit['tag']}.")
                    continue
                solved = streams.get(output["tag"], {})
                vapor = solved.get("vapor_fraction")
                if isinstance(vapor, (int, float)) and vapor > 1e-6:
                    fail(output, "CULTURE_PHASE_NOT_LIQUID", f"Culture stream {output['tag']} is not liquid downstream of {unit['tag']} (vapor fraction {vapor:.6g}).")
                    continue
                density = _density(solved)
                if density is None:
                    fail(output, "CULTURE_DENSITY_UNAVAILABLE", f"Culture result for {output['tag']} needs a finite positive DWSIM Mixture density.")
                    continue
                results[output["tag"]] = {"owner": "jarvis", "propagation_version": PROPAGATION_VERSION,
                    "status": "completed", "density_kg_m3": density, "values": _display_values(state, density),
                    "unit_balances": balances, "fidelity": FIDELITY,
                    "caveats": ["DWSIM mixture density uses seawater-as-water approximation.",
                                "Dissolved O₂ is carried without solubility or degassing changes; heated streams may be supersaturated."],
                    "pH_reason": state["reasons"].get("ph")}
                states[output["id"]] = state
            continue
        if any(s["id"] in failed for s in cultured_inputs):
            for output in outputs:
                fail(output, "CULTURE_UPSTREAM_FAILED", f"Culture result failed downstream of {unit['tag']}.")
            continue
        if unit["type"] in {"Flash", "DistillationColumn", "PFR", "CSTR", "Recycle"}:
            for output in outputs:
                fail(output, "CULTURE_UNIT_UNSUPPORTED", f"Culture cannot pass through {unit['type']} {unit['tag']}.")
            continue
        source_states = {s["id"]: states[s["id"]] for s in cultured_inputs}
        if unit["type"] == "Mixer":
            flows = [streams.get(s["tag"], {}).get("mass_flow_kg_s") for s in inputs]
            if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in flows):
                for output in outputs:
                    fail(output, "CULTURE_FLOW_UNAVAILABLE", f"Mixer {unit['tag']} needs finite inlet mass flows for culture.")
                continue
            total_flow = sum(float(value) for value in flows)
            if total_flow <= 0:
                for output in outputs:
                    fail(output, "CULTURE_MIXER_FLOW_INVALID", f"Mixer {unit['tag']} has non-positive total inlet mass flow.")
                continue
            mixed, mix_reasons = mix_mass_specific(
                [(float(streams.get(s["tag"], {}).get("mass_flow_kg_s", 0.0)), source_states[s["id"]])
                 for s in cultured_inputs], total_flow, len(inputs), unit["tag"])
            output_state: dict[str, Any] = {"mass_specific": mixed, "reasons": mix_reasons}
        else:
            output_state = source_states[cultured_inputs[0]["id"]]
        unit_balances: dict[str, Any] = {}
        for name in CONSERVED:
            if output_state["mass_specific"].get(name) is None:
                continue
            unit_factor = 0.001 if name == "salinity" else 1.0
            inbound = sum(float(streams.get(s["tag"], {}).get("mass_flow_kg_s") or 0.0)
                          * float(source_states[s["id"]]["mass_specific"][name]) * unit_factor
                          for s in cultured_inputs if source_states[s["id"]]["mass_specific"].get(name) is not None)
            outbound = sum(float(streams.get(s["tag"], {}).get("mass_flow_kg_s") or 0.0)
                           * float(output_state["mass_specific"][name]) * unit_factor for s in outputs)
            unit_label = "mol/s" if name == "dic" else "kg/s"
            residual = inbound - outbound
            floor = 1e-12
            tolerance = 1e-9 * max(abs(inbound), abs(outbound)) + floor
            unit_balances[name] = {"in": inbound, "out": outbound, "residual": residual,
                                   "tolerance": tolerance, "unit": unit_label, "passed": abs(residual) <= tolerance}
        if any(not item["passed"] for item in unit_balances.values()):
            for output in outputs:
                fail(output, "CULTURE_BALANCE_FAILED", f"Culture balance failed at {unit['tag']}.")
            continue
        for output in outputs:
            result = streams.get(output["tag"], {})
            vapor = result.get("vapor_fraction")
            if isinstance(vapor, (int, float)) and vapor > 1e-6:
                fail(output, "CULTURE_PHASE_NOT_LIQUID", f"Culture stream {output['tag']} is not liquid downstream of {unit['tag']} (vapor fraction {vapor:.6g}).")
                continue
            density = _density(result)
            if density is None:
                fail(output, "CULTURE_DENSITY_UNAVAILABLE", f"Culture result for {output['tag']} needs a finite positive DWSIM Mixture density.")
                continue
            results[output["tag"]] = {
                "owner": "jarvis", "propagation_version": PROPAGATION_VERSION,
                "status": "completed", "density_kg_m3": density, "values": _display_values(output_state, density),
                "unit_balances": unit_balances, "fidelity": FIDELITY,
                "caveats": ["DWSIM mixture density uses seawater-as-water approximation.",
                            "Dissolved O₂ is carried without solubility or degassing changes; heated streams may be supersaturated."],
                "pH_reason": output_state["reasons"].get("ph"),
            }
            states[output["id"]] = output_state
    return results, findings
