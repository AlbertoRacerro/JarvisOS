"""Coupled Tier 1 transient photobioreactor engine (spec 172)."""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

import numpy as np

from app.modules.process_stack import draft, optics_t2, pbr_unit
from app.modules.process_stack.dynamic_models import (
    MAX_CONTROLLERS,
    MAX_DURATION_S,
    MAX_EVENTS,
    MAX_OUTPUT_POINTS,
    MAX_PARTICIPATING_PBRS,
    Controller,
    Scenario,
    Schedule,
)
from app.modules.process_stack.state_triggers import Trigger

EVALUATOR_VERSION = "process_dynamic_t1/2"
MAX_DWSIM_SAMPLES = 200
STATE_STRIDE = 7
FLOW_BALANCE_RTOL = 1e-9  # relative volumetric conservation residual accepted from the flow solve


def _slot(index: int, field: int = 0) -> int:
    return index * STATE_STRIDE + field


def _unit_type(item: dict[str, Any]) -> str:
    return item.get("unit", {}).get("type", "PhotobioreactorT1")


class DynamicError(ValueError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message[:600])
        self.code, self.message, self.detail = code, message[:600], detail or {}


@dataclass(frozen=True)
class Snapshot:
    payload: dict[str, Any]
    digest: str


@dataclass(frozen=True)
class EngineResult:
    status: Literal["succeeded", "failed", "cancelled"]
    error: dict[str, Any] | None
    series: dict[str, np.ndarray]
    manifest: dict[str, Any]


def _growth_for_unit(item: dict[str, Any], *, dark: bool = False) -> Any:
    growth = pbr_unit.build_growth(item["params"] if _unit_type(item) == "PhotobioreactorT1"
                                   else item["growth_template_params"], item["model"], (0, 0, 0), 0)
    if not dark:
        return growth
    params = item["params"]
    return replace(growth, mu_max_h=0.0, peak_par=0.0, photoperiod_h=0.0,
                   temperature_mean=params["temperature"], temperature_amplitude=0.0,
                   kla_h=3600.0 * params.get("oxygen_kla", 0.0),
                   oxygen_saturation=params.get("oxygen_saturation", growth.oxygen_saturation))


def t2_optics_for_unit(item: dict[str, Any]) -> optics_t2.Optics:
    """Map one T2 PBR unit's typed draft layout and resolved tube dimensions onto the optics kernel."""
    raw = item["unit"].get("params") or {}
    names = ("tubes_across", "layers", "pitch_h", "pitch_v", "stagger_offset", "axis_azimuth",
             "centre_height_above_sea", "n_wall", "n_water", "sea_albedo")
    missing = [name for name in names if (raw.get(name) or {}).get("si") is None]
    if missing:
        raise DynamicError("OPTICS_LAYOUT_INVALID", "T2 optics layout is incomplete.",
                           {"unit": item["tag"], "fields": missing})
    values = {name: raw[name]["si"] for name in names}
    for name in ("tubes_across", "layers"):
        if isinstance(values[name], bool) or not isinstance(values[name], (int, float)) or not math.isfinite(values[name]) or int(values[name]) != values[name]:
            raise DynamicError("OPTICS_LAYOUT_INVALID", f"{name} must be a whole number.")
    outer = (raw.get("tube_outer_diameter") or {}).get("si")
    absorption = (raw.get("wall_absorption") or {}).get("si")
    outer_diameter: float | None = None
    wall_absorption_m_inv: float | None = None
    if outer is not None or absorption is not None:
        absent = [name for name, value in (("tube_outer_diameter", outer), ("wall_absorption", absorption)) if value is None]
        if absent:
            raise DynamicError("OPTICS_V2_GEOMETRY_INCOMPLETE",
                               "Optics v2 needs both tube outer diameter and wall absorption coefficient; "
                               "set the missing field or clear both to keep optics v1. No outer diameter is assumed.",
                               {"unit": item["tag"], "fields": absent})
        inner = item["params"]["tube_inner_diameter"]
        if (isinstance(outer, bool) or not isinstance(outer, (int, float)) or not math.isfinite(outer)
                or isinstance(absorption, bool) or not isinstance(absorption, (int, float))
                or not math.isfinite(absorption) or outer <= inner or absorption < 0):
            raise DynamicError("OPTICS_V2_GEOMETRY_INVALID",
                               "Tube outer diameter must exceed the inner diameter and wall absorption must be "
                               "finite and zero or greater.",
                               {"unit": item["tag"], "tube_inner_diameter": inner, "tube_outer_diameter": outer,
                                "wall_absorption": absorption})
        outer_diameter, wall_absorption_m_inv = float(outer), float(absorption)
    optics = optics_t2.Optics(
        diameter=item["params"]["tube_inner_diameter"], tubes_across=int(values["tubes_across"]),
        layers=int(values["layers"]), pitch_h=float(values["pitch_h"]), pitch_v=float(values["pitch_v"]),
        stagger=float(values["stagger_offset"]), axis_azimuth=math.radians(float(values["axis_azimuth"])),
        centre_height=float(values["centre_height_above_sea"]), n_wall=float(values["n_wall"]),
        n_water=float(values["n_water"]), lower_albedo=float(values["sea_albedo"]),
        outer_diameter=outer_diameter, wall_absorption_m_inv=wall_absorption_m_inv,
    )
    if optics.module_tubes > item["params"]["tube_count"]:
        raise DynamicError("OPTICS_LAYOUT_INVALID", "The representative module exceeds the unit tube count.",
                           {"unit": item["tag"], "module_tubes": optics.module_tubes})
    return optics


def _prepare_t2_optics(workspace_id: str, units: list[dict[str, Any]],
                       profile: dict[str, Any], scenario: Scenario) -> None:
    """Freeze profile-interval optics for T2 PBRs before the ODE worker starts."""
    selected = [item for item in units if (item["unit"].get("options") or {}).get("optics_model", "t1") == "t2"]
    if not selected:
        return
    import pvlib

    from app.modules.environment import profiles

    source = profile["profile"]
    channels = source["channels"]
    if not {"dni", "dhi"} <= channels.keys():
        raise DynamicError("OPTICS_CHANNELS_MISSING", "T2 optics requires DNI and DHI profile channels.")
    resolution = source.get("resolution_minutes")
    if not isinstance(resolution, (int, float)) or resolution > optics_t2.OPTICS_RESOLUTION_MAX_MIN or resolution <= 0:
        raise DynamicError("OPTICS_RESOLUTION", "T2 optics requires a profile resolution of at most "
                           f"{optics_t2.OPTICS_RESOLUTION_MAX_MIN} minutes.")
    provenance = source.get("provenance") or {}
    site = provenance.get("site_snapshot") or profiles.get_site(workspace_id)
    if not isinstance(site, dict) or not {"latitude", "longitude", "elevation_m"} <= site.keys():
        raise DynamicError("OPTICS_SITE_MISSING", "T2 optics requires a profile site snapshot or workspace site.")
    factor = (provenance.get("factor_umol_per_j") if profile["par_name"] == "par"
              else scenario.par_from_ghi_factor)
    if factor is None:
        factor = scenario.par_from_ghi_factor
    if not isinstance(factor, (int, float)) or not math.isfinite(factor) or factor <= 0:
        raise DynamicError("OPTICS_PAR_CONVERSION_MISSING", "T2 optics needs a declared GHI-to-PAR conversion factor.")
    scale = float(factor) * scenario.par_scale
    times = np.asarray(profile["times"], dtype=np.float64)
    steps = np.diff(times)
    if len(steps) == 0 or np.any(steps <= 0) or np.any(steps / 60 > optics_t2.OPTICS_RESOLUTION_MAX_MIN + 1e-9):
        raise DynamicError("OPTICS_RESOLUTION", "T2 optics profile intervals exceed the midpoint-sun limit.")
    midpoints = times - np.r_[steps[0], steps] / 2
    dni = np.asarray([float("nan") if v is None else v for v in channels["dni"]], dtype=np.float64) * scale
    dhi = np.asarray([float("nan") if v is None else v for v in channels["dhi"]], dtype=np.float64) * scale
    first = max(0, bisect.bisect_left(profile["times"], _stamp(scenario.start_utc)))
    last = min(len(times), bisect.bisect_left(profile["times"], _stamp(scenario.end_utc)) + 1)
    if (not np.all(np.isfinite(dni[first:last])) or not np.all(np.isfinite(dhi[first:last]))
            or np.any(dni[first:last] < 0) or np.any(dhi[first:last] < 0)):
        raise DynamicError("OPTICS_CHANNELS_MISSING", "Consumed DNI and DHI intervals must be finite and nonnegative.")
    dni = np.where(np.isfinite(dni) & (dni >= 0), dni, 0.0)
    dhi = np.where(np.isfinite(dhi) & (dhi >= 0), dhi, 0.0)
    params = provenance.get("parameters") or {}
    conditions = {
        "pressure_pa": float(params.get("pressure_pa", pvlib.atmosphere.alt2pres(float(site["elevation_m"])))),
        "temperature_c": float(params.get("temperature_c", 12.0)),
        "delta_t": float(params.get("delta_t", 67.0)),
    }
    for item in selected:
        optics = t2_optics_for_unit(item)
        handoff = optics_t2.build_handoff(optics, midpoints, site, conditions, dni, dhi)
        item["optics_t2"] = {
            "layout": asdict(optics),
            "intervals": {name: values.tolist() for name, values in handoff.items()},
            "dni_par": dni.tolist(), "dhi_par": dhi.tolist(),
            "provenance": {"model": "t2", "version": optics.version, "parameter_digest": optics.digest(),
                           "pvlib_version": pvlib.__version__,
                           "site_digest": hashlib.sha256(_canonical(site)).hexdigest()},
        }


def _validate_downstream_boundary_specs(
    boundary_streams: dict[str, dict[str, Any]], carrier_spec: dict[str, Any],
) -> None:
    missing_by_stream: dict[str, list[str]] = {}
    for boundary in boundary_streams.values():
        spec = boundary.get("spec", {})
        missing = [field for field in ("temperature", "pressure", "composition")
                   if not spec.get(field) and not carrier_spec.get(field)]
        if missing:
            missing_by_stream[boundary["tag"]] = missing
    if missing_by_stream:
        raise DynamicError(
            "DOWNSTREAM_BOUNDARY_STATE_UNDECLARED",
            "Downstream boundary streams require declared temperature, pressure and composition.",
            {"streams": missing_by_stream},
        )


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _stamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include an offset")
    return parsed.astimezone(UTC).timestamp()


def _stream_flow(stream: dict[str, Any], declared: dict[str, float]) -> float | None:
    """Resolve a boundary culture feed's m³/s flow from an explicit override or kg/s."""
    tag = stream["tag"]
    if tag in declared:
        value = float(declared[tag])
    else:
        mass = (stream.get("spec", {}).get("mass_flow") or {}).get("si")
        value = float(mass) / 1000.0 if mass is not None else math.nan
    return value if math.isfinite(value) and value >= 0 else None


def _splitter_ratios(unit: dict[str, Any], outlet_count: int) -> list[float]:
    params = unit.get("params", {})
    if "split_ratios" in params:
        values = params["split_ratios"]
        ratios = [float(value.get("si", value)) if isinstance(value, dict) else float(value)
                  for _, value in sorted(values.items())]
    else:
        ratios = [float((params.get(f"split_ratio_{index}") or {}).get("si", 0.5)) for index in (1, 2)]
        if outlet_count == 3:
            ratios.append(1.0 - sum(ratios))
    if (len(ratios) != outlet_count or any(value < 0 or value > 1 for value in ratios)
            or abs(sum(ratios) - 1.0) > 1e-6):
        raise DynamicError("SPLITTER_RATIOS_INVALID", "Splitter ratios must be nonnegative and sum to one.",
                           {"unit": unit.get("tag")})
    return ratios


def _validate_events(
    events: list[dict[str, Any]], units: list[dict[str, Any]], topology: dict[str, Any],
    controller_ids: list[str],
) -> None:
    unit_tags = {item["tag"] for item in units}
    feeds = {stream["tag"] for stream in topology["streams"] if stream.get("source") is None}
    splitters = {unit["tag"] for unit in topology["units"] if unit.get("type") == "Splitter"}
    for event in events:
        observed = event.get("observed")
        if observed:
            parts = observed.split(".")
            if len(parts) != 2 or parts[0] not in unit_tags or parts[1] not in {"X", "N", "O2"}:
                raise DynamicError("UNSUPPORTED_MEASUREMENT", "Conditional event observes an unsupported channel.",
                                   {"observed": observed})
            if event.get("threshold_unit") != "kg/m3" or event.get("direction") not in {"above", "below"}:
                raise DynamicError("EVENT_UNIT_INVALID", "Conditional threshold requires kg/m3 and a direction.")
        kind = event["type"]
        if kind in {"inoculation", "harvest"}:
            if event.get("unit") not in unit_tags:
                raise DynamicError("EVENT_TARGET_INVALID", "Event unit is not participating.",
                                   {"unit": event.get("unit")})
        if kind == "harvest":
            if event.get("fraction") is None:
                raise DynamicError("EVENT_VALUE_INVALID", "Harvest requires a fraction.")
        elif kind == "inoculation":
            if event.get("value_unit") != "kg/m3":
                raise DynamicError("EVENT_UNIT_INVALID", "Inoculation concentration requires kg/m3.")
            value: Any = event.get("value")
            if isinstance(value, dict) and (not value or set(value) - {"X", "N", "O2"}):
                raise DynamicError("EVENT_VALUE_INVALID", "Inoculation has unsupported concentration fields.")
            values = value.values() if isinstance(value, dict) else [value]
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values):
                raise DynamicError("EVENT_VALUE_INVALID", "Inoculation requires finite nonnegative concentrations.")
        elif kind in {"feed_change", "dilution"}:
            target = event.get("target") or ""
            if target.startswith("splitter:"):
                if target.partition(":")[2] not in splitters or kind != "dilution":
                    raise DynamicError("EVENT_TARGET_INVALID", "Event splitter is not participating.")
                if target.partition(":")[2] in topology.get("implied_splitters", []):
                    raise DynamicError("SPLIT_IMPLIED_BY_CIRCULATION",
                                       "This splitter's split follows the specified loop circulation; "
                                       "change the boundary feed instead.", {"splitter": target.partition(":")[2]})
            elif (event.get("stream") or target.removeprefix("feed:")) not in feeds:
                raise DynamicError("EVENT_TARGET_INVALID", "Event feed is not a participating boundary stream.")
            value = event.get("value")
            expected_unit = "1" if target.startswith("splitter:") else "m3/s"
            if not isinstance(value, dict) and event.get("value_unit") != expected_unit:
                raise DynamicError("EVENT_UNIT_INVALID", f"Event value requires {expected_unit}.")
            if isinstance(value, dict):
                if set(value) - {"flow_m3_s", "culture", "culture_unit"} or not value:
                    raise DynamicError("EVENT_VALUE_INVALID", "Feed change has unsupported fields.")
                values = [value["flow_m3_s"]] if "flow_m3_s" in value else []
                culture = value.get("culture", {})
                if not isinstance(culture, dict) or set(culture) - {"X", "N", "O2"}:
                    raise DynamicError("EVENT_VALUE_INVALID", "Feed culture has unsupported fields.")
                if culture and value.get("culture_unit") != "kg/m3":
                    raise DynamicError("EVENT_UNIT_INVALID", "Feed culture requires kg/m3.")
                values.extend(culture.values())
            else:
                values = [value]
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values):
                raise DynamicError("EVENT_VALUE_INVALID", "Feed/dilution values must be finite and nonnegative.")
            if target.startswith("splitter:") and not 0 <= float(value) <= 1:
                raise DynamicError("EVENT_VALUE_INVALID", "Splitter ratio must be in [0,1].")
        elif kind == "setpoint_change":
            if event.get("value_unit") != "kg/m3":
                raise DynamicError("EVENT_UNIT_INVALID", "Controller setpoint requires kg/m3.")
            if event.get("target") not in controller_ids:
                raise DynamicError("EVENT_TARGET_INVALID", "Setpoint controller is not participating.")
            value = event.get("value")
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise DynamicError("EVENT_VALUE_INVALID", "Setpoint must be finite.")


def _validate_actions(events: list[dict[str, Any]], units: list[dict[str, Any]], topology: dict[str, Any]) -> None:
    """Refuse invalid action events in prepare, including an exact volume guard (spec 185).

    Flows stay balanced, so tank volumes change only at events; replaying the expanded time-triggered schedule is
    exact. A state-triggered event (spec 186) has its targets and observable validated here, but its volumes can
    only be guarded at run time (``EVENT_VOLUME_INVALID_RUNTIME``), so it neither is range-checked nor moves the
    replayed volumes.
    """
    tanks = {item["tag"]: item for item in units if _unit_type(item) == "HoldupTank"}
    loops = {tag: loop for loop in _culture_loops(topology, units, topology["flows"]) for tag in loop["member_tags"]}
    volumes = {item["tag"]: float(item["volume_m3"]) for item in units}
    unit_tags = {item["tag"] for item in units}
    for event in events:
        if event["type"] != "actions":
            continue
        if event.get("observed"):
            raise DynamicError("EVENT_VALUE_INVALID", "State-triggered actions use `condition`, not observed/threshold.")
        condition = event.get("condition")
        if condition:
            unit_tag, _, channel = condition["observed"].rpartition(".")
            if unit_tag not in unit_tags or (channel.startswith("loop_") and unit_tag not in loops):
                raise DynamicError("UNSUPPORTED_MEASUREMENT",
                                   "A state condition observes a unit that is not participating or has no culture loop.",
                                   {"observed": condition["observed"]})
            if not all(math.isfinite(float(condition[key])) for key in ("threshold", "hysteresis", "cooldown_s")):
                raise DynamicError("EVENT_VALUE_INVALID", "State condition values must be finite.")
        _replay_event_volumes(event, tanks, loops, dict(volumes) if condition else volumes,
                              "EVENT_VOLUME_INVALID", check_range=not condition)


def _replay_event_volumes(
    event: dict[str, Any], tanks: dict[str, dict[str, Any]], loops: dict[str, dict[str, Any]],
    volumes: dict[str, float], code: str, *, check_range: bool = True,
) -> None:
    """Replay one action event on tank volumes (mutating ``volumes``), refusing with ``code`` outside the limits.

    Prepare uses it for time-triggered events; the run uses it just before a state-triggered fire.
    """
    parcel_volume: float | None = None
    parcel_loop: str | None = None
    for position, action in enumerate(event["actions"]):
        detail = {"time_s": event["time_s"], "action": position, "type": action["type"]}
        names = [action["return_to"]] if action["type"] == "separate" else [action["tank"]]
        for name in names:
            if name != "none" and name not in tanks:
                raise DynamicError("EVENT_TARGET_INVALID", "Actions act only on participating HoldupTanks.",
                                   {**detail, "tank": name})
        touched = names[0]
        if action["type"] == "draw":
            if action.get("loop_fraction") is not None:
                if touched not in loops:
                    raise DynamicError("EVENT_TARGET_INVALID", "loop_fraction needs a tank inside a culture loop.",
                                       {**detail, "tank": touched})
                amount = float(action["loop_fraction"]) * sum(volumes[t] for t in loops[touched]["member_tags"])
            else:
                amount = float(action["volume_m3"])
            loop_id = loops[touched]["id"] if touched in loops else None
            if parcel_volume is not None and loop_id != parcel_loop:
                raise DynamicError("EVENT_VALUE_INVALID", "One event's undivided draws must come from one loop.",
                                   detail)
            parcel_volume, parcel_loop = (parcel_volume or 0.0) + amount, loop_id
            volumes[touched] -= amount
        elif action["type"] == "separate":
            if parcel_volume is None:
                raise DynamicError("EVENT_VALUE_INVALID", "separate needs a draw earlier in the same event.", detail)
            if touched != "none":
                volumes[touched] += parcel_volume * (1.0 - action["recovery"] / action["concentration_factor"])
            parcel_volume = parcel_loop = None
        elif action["type"] == "refill":
            if action.get("to_volume_m3") is not None:
                if check_range and action["to_volume_m3"] < volumes[touched]:
                    raise DynamicError(code, "Refill target is below the tank's volume then.",
                                       {**detail, "tank": touched, "volume_m3": volumes[touched]})
                volumes[touched] = float(action["to_volume_m3"])
            else:
                volumes[touched] += float(action["volume_m3"])
        else:  # dose, inoculate
            volumes[touched] += float(action.get("volume_m3", 0.0))
        if touched != "none" and check_range:
            params = tanks[touched]["params"]
            low = params.get("min_volume", 0.0)
            high = params.get("max_volume", tanks[touched].get("initial_volume_m3", tanks[touched]["volume_m3"]))
            if not (volumes[touched] > 0 and low - 1e-12 <= volumes[touched] <= high + 1e-12):
                raise DynamicError(code, "An action would take a tank outside its volume limits.",
                                   {**detail, "tank": touched, "volume_m3": volumes[touched],
                                    "min_volume_m3": low, "max_volume_m3": high})


def _t1_reachable(objects: dict[str, Any], selected: set[str]) -> set[str]:
    """Unit ids connected to the participating PBRs through T1-supported units only (as in _topology)."""
    supported = {"PhotobioreactorT1", "HoldupTank", "Mixer", "Splitter", "Pump", "Recycle", "SpecifiedSeparator"}
    edges = [((s.get("source") or {}).get("unit"), (s.get("target") or {}).get("unit"))
             for s in objects.values() if s.get("kind") == "stream" and s.get("type") != "EnergyStream"]
    reached, frontier = set(selected), list(selected)
    while frontier:
        current = frontier.pop()
        for source, target in edges:
            neighbor = target if source == current else source if target == current else None
            if neighbor and neighbor not in reached and objects.get(neighbor, {}).get("type") in supported:
                reached.add(neighbor)
                frontier.append(neighbor)
    return reached


def _implied_splitters(units: list[dict[str, Any]], streams: list[dict[str, Any]],
                       circulation_tags: set[str]) -> set[str]:
    """Splitters inside a loop whose Pump circulation is specified.

    The circulation fixes the flow through the loop, so such a splitter's outlet split follows
    continuity (net boundary inflow leaves through it); a declared ratio would overdetermine it.
    """
    from app.modules.process_stack import mixed

    if not circulation_tags:
        return set()
    nodes = {unit["id"] for unit in units}
    edges = [(s["source"]["unit"], s["target"]["unit"], s) for s in streams
             if s.get("source") and s.get("target")
             and s["source"]["unit"] in nodes and s["target"]["unit"] in nodes]
    implied: set[str] = set()
    for group in mixed.components(nodes, edges):
        members = [unit for unit in units if unit["id"] in group]
        if any(unit.get("type") == "Pump" and unit.get("tag") in circulation_tags for unit in members):
            implied.update(unit["id"] for unit in members if unit.get("type") == "Splitter")
    return implied


def _solve_flows(topology: dict[str, Any], overrides: dict[str, float] | None = None) -> dict[str, float]:
    """Solve one full-rank volumetric balance with fixed pump circulation constraints."""
    overrides = overrides or {}
    matrix = np.asarray(topology["flow_matrix"], dtype=np.float64)
    values = np.asarray(topology["flow_rhs"], dtype=np.float64).copy()
    for row_index, stream_id in topology.get("feed_rows", {}).items():
        values[int(row_index)] = float(overrides.get(stream_id, topology["flow_rhs"][int(row_index)]))
    for row_index, pump in topology.get("circulation_rows", {}).items():
        values[int(row_index)] = float(overrides.get(pump["tag"], pump["flow_m3_s"]))
    rank = int(np.linalg.matrix_rank(matrix))
    n = matrix.shape[1]
    if rank < n:
        from app.modules.process_stack import mixed

        active = {u["id"] for u in topology["units"]}
        edges = [(s["source"]["unit"], s["target"]["unit"], s) for s in topology["streams"]
                 if s.get("source") and s.get("target")
                 and s["source"]["unit"] in active and s["target"]["unit"] in active]
        cycles = []
        for group in mixed.components({u["id"] for u in topology["units"]}, edges):
            cyclic = len(group) > 1 or any(a == b and a in group for a, b, _ in edges)
            if cyclic:
                cycles.append(sorted(u["tag"] for u in topology["units"] if u["id"] in group))
        specified = set(topology.get("circulation", {})) | set(overrides)
        unspecified = [cycle for cycle in cycles if not any(
            unit.get("tag") in specified for unit in topology["units"] if unit["type"] == "Pump"
        )]
        cycles = unspecified or cycles
        raise DynamicError("FLOW_UNDERDETERMINED", "Dynamic flow balance has an unspecified circulation cycle.",
                           {"cycles": cycles, "units": sorted({tag for cycle in cycles for tag in cycle})})
    # Full column rank is checked above, so the least-squares solution is unique. It is only a
    # best fit, though: an overdetermined system is accepted only if it conserves volume exactly.
    solution, *_ = np.linalg.lstsq(matrix, values, rcond=None)
    residual = np.abs(matrix @ solution - values)
    scale = max(float(np.max(np.abs(values))), float(np.max(np.abs(solution))), 1e-300)
    residual_rel = float(np.max(residual)) / scale
    if residual_rel > FLOW_BALANCE_RTOL:
        worst = matrix[int(np.argmax(residual))]
        raise DynamicError("FLOW_BALANCE_UNSOLVED", "Dynamic volumetric flow balance is inconsistent.", {
            "reason": "conservation_residual", "residual_rel": residual_rel, "tolerance": FLOW_BALANCE_RTOL,
            "streams": sorted(topology["streams"][i]["tag"] for i in np.flatnonzero(worst))})
    if np.any(solution < -FLOW_BALANCE_RTOL * scale):
        raise DynamicError("FLOW_BALANCE_UNSOLVED", "Dynamic volumetric flow balance needs a negative flow.", {
            "reason": "negative_flow",
            "streams": sorted(topology["streams"][i]["tag"] for i in np.flatnonzero(solution < -FLOW_BALANCE_RTOL * scale))})
    return {stream["id"]: max(0.0, float(solution[index]))
            for index, stream in enumerate(topology["streams"])}


def _culture_loops(topology: dict[str, Any], units: list[dict[str, Any]], flows: dict[str, float]) -> list[dict[str, Any]]:
    from app.modules.process_stack import mixed

    nodes = {unit["id"] for unit in topology["units"]}
    edges = [(s["source"]["unit"], s["target"]["unit"], s) for s in topology["streams"]
             if s.get("source") and s.get("target")
             and s["source"]["unit"] in nodes and s["target"]["unit"] in nodes]
    by_id = {unit["id"]: unit for unit in topology["units"]}
    selected = {item["unit"]["id"]: item for item in units}
    loops = []
    for group in mixed.components(nodes, edges):
        cyclic = len(group) > 1 or any(a == b and a in group for a, b, _ in edges)
        pbr_tags = sorted(item["tag"] for item in units
                          if item["unit"]["id"] in group and item["unit"]["type"] == "PhotobioreactorT1")
        if not cyclic or not pbr_tags:
            continue
        members = [selected[key] for key in sorted(group) if key in selected]
        volume = sum(item["volume_m3"] for item in members)
        illuminated = sum(item["volume_m3"] for item in members
                          if item["unit"]["type"] == "PhotobioreactorT1")
        internal = [s for s in topology["streams"]
                    if (s.get("source") or {}).get("unit") in group and (s.get("target") or {}).get("unit") in group]
        inbound = [s for s in topology["streams"] if (s.get("target") or {}).get("unit") in group
                   and (s.get("source") or {}).get("unit") not in group]
        outbound = [s for s in topology["streams"] if (s.get("source") or {}).get("unit") in group
                    and (s.get("target") or {}).get("unit") not in group]
        q_circ = max((flows.get(s["id"], 0.0) for s in internal), default=0.0)
        q_in, q_out = sum(flows.get(s["id"], 0.0) for s in inbound), sum(flows.get(s["id"], 0.0) for s in outbound)
        loops.append({"id": pbr_tags[0], "units": sorted(by_id[key]["tag"] for key in group),
                      "member_tags": [item["tag"] for item in members], "volume_m3": volume,
                      "illuminated_fraction": illuminated / volume if volume else 0.0,
                      "circulation_m3_s": q_circ, "pass_transit_time_s": volume / q_circ if q_circ else None,
                      "net_boundary_inflow_m3_s": q_in, "net_boundary_outflow_m3_s": q_out,
                      "net_dilution_1_s": q_out / volume if volume else 0.0,
                      "culture_residence_time_s": volume / q_out if q_out else "∞, batch"})
    return sorted(loops, key=lambda loop: loop["id"])


def _flow_hydraulics(topology: dict[str, Any], units: list[dict[str, Any]], flows: dict[str, float]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    loops = topology.get("culture_loops", [])
    result: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []
    for item in units:
        if _unit_type(item) != "PhotobioreactorT1" or "tube_count" not in item["params"]:
            continue
        unit_id = item["unit"]["id"]
        inlet_streams = [s for s in topology["streams"] if (s.get("target") or {}).get("unit") == unit_id]
        q_in = sum(flows.get(s["id"], 0.0) for s in inlet_streams)
        loop = next((entry for entry in loops if item["tag"] in entry["member_tags"]), None)
        specified_loop = bool(loop and any(
            pump["tag"] in loop["units"] for pump in topology.get("circulation", {}).values()
        ))
        params = dict(item["params"])
        if specified_loop:
            area = math.pi * params["tube_inner_diameter"] ** 2 / 4.0
            velocity = q_in / (params["tube_count"] * area)
            params["liquid_velocity"] = velocity
            declared = item["params"]["liquid_velocity"]
            if abs(velocity - declared) / declared > 0.01:
                findings.append({"code": "PBR_CIRCULATION_VELOCITY_MISMATCH", "severity": "warning",
                                 "unit": item["tag"], "declared_velocity_m_s": declared,
                                 "solved_velocity_m_s": velocity})
        else:
            velocity = params["liquid_velocity"]
        hydro = pbr_unit.hydraulics(params)
        if item["unit"].get("hydraulics_model"):
            findings.append({"code": "PBR_HYDRAULICS_PIN_NOT_APPLIED_DYNAMIC", "severity": "warning",
                             "unit": item["tag"], "hydraulics_fidelity": pbr_unit.CORRELATION_FIDELITY,
                             "message": "Dynamic runs use the literature correlation for loop hydraulics; the pinned "
                                        "CFD reduced model applies to steady Process evaluation only."})
        result[item["tag"]] = {**hydro, "velocity_m_s": velocity, "inlet_flow_m3_s": q_in,
                                "pass_rate_1_s": q_in / item["volume_m3"] if item["volume_m3"] else None,
                                "culture_loop_id": loop["id"] if loop is not None and specified_loop else None}
    return result, findings


def _topology(document: dict[str, Any], units: list[dict[str, Any]], scenario: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded T1 stream network and solve its steady volumetric flow balance."""
    objects = document["objects"]
    selected = {item["unit"]["id"] for item in units}
    streams = [
        value for value in objects.values() if value.get("kind") == "stream" and value.get("type") != "EnergyStream"
    ]
    by_id = {key: value for key, value in objects.items() if value.get("kind") == "unit"}
    active_ids = set(selected)
    frontier = list(selected)
    while frontier:
        current = frontier.pop()
        for stream in streams:
            source = (stream.get("source") or {}).get("unit")
            target = (stream.get("target") or {}).get("unit")
            if current == source:
                neighbor = target
            elif current == target:
                neighbor = source
            else:
                continue
            if neighbor is None or neighbor == current:
                continue
            candidate = by_id.get(neighbor, {})
            if candidate.get("type") not in {
                "PhotobioreactorT1", "HoldupTank", "Mixer", "Splitter", "Pump", "Recycle", "SpecifiedSeparator"
            }:
                if current == target:
                    raise DynamicError(
                        "UNSUPPORTED_TOPOLOGY",
                        "A non-participating unit cannot feed the T1 network.",
                        {"unit": candidate.get("tag")},
                    )
                # A product stream can cross into the optional downstream DWSIM sampler.
                continue
            if neighbor not in active_ids:
                active_ids.add(neighbor)
                frontier.append(neighbor)
    active_units = {key: by_id[key] for key in active_ids}
    relevant: list[dict[str, Any]] = []
    for stream in streams:
        source, target = stream.get("source"), stream.get("target")
        if (source and source.get("unit") in active_units) or (target and target.get("unit") in active_units):
            relevant.append(stream)
    for item in active_units.values():
        if item.get("type") == "Splitter" and item.get("mode") != "split_ratios":
            raise DynamicError(
                "SPLITTER_FLOW_SPEC_UNSUPPORTED",
                "Dynamic T1 supports split_ratios mode only.",
                {"unit": item.get("tag")},
            )
    # Unknown stream Q values obey unit balances; splitter outlet ratios add independent equations,
    # except for splitters whose split is implied by a specified loop circulation.
    implied_splitters = _implied_splitters(list(active_units.values()), relevant,
                                           set(scenario.get("circulation", {})))
    n = len(relevant)
    index = {stream["id"]: i for i, stream in enumerate(relevant)}
    equations: list[np.ndarray] = []
    rhs: list[float] = []
    feed_rows: dict[int, str] = {}
    feeds: dict[str, dict[str, float]] = {}
    for stream in relevant:
        if stream.get("source") is None:
            culture = stream.get("spec", {}).get("culture")
            if culture is None:
                raise DynamicError(
                    "CULTURE_FEED_REQUIRED",
                    "Every dynamic boundary feed needs a culture composition.",
                    {"stream": stream["tag"]},
                )
            flow = _stream_flow(stream, scenario.get("feed_flows", {}))
            if flow is None:
                raise DynamicError(
                    "FEED_FLOW_REQUIRED",
                    "Boundary feed needs a declared volumetric flow or mass flow.",
                    {"stream": stream["tag"]},
                )
            row = np.zeros(n)
            row[index[stream["id"]]] = 1
            equations.append(row)
            rhs.append(flow)
            feed_rows[len(rhs) - 1] = stream["id"]
            feeds[stream["id"]] = {
                name: float((culture.get(key) or {}).get("si", 0.0))
                for name, key in (("X", "biomass"), ("N", "nitrogen"), ("O2", "oxygen"))
            }
        elif stream.get("source", {}).get("unit") not in active_units:
            raise DynamicError("UNSUPPORTED_TOPOLOGY", "A boundary product cannot be reintroduced as an internal feed.")
    for key, unit in active_units.items():
        incoming = [s for s in relevant if (s.get("target") or {}).get("unit") == key]
        outgoing = [s for s in relevant if (s.get("source") or {}).get("unit") == key]
        outgoing.sort(key=lambda stream: (stream.get("source") or {}).get("port", 0))
        if not incoming or not outgoing:
            if key in selected:
                raise DynamicError(
                    "ZERO_THROUGHFLOW", "Participating PBR must have inlet and outlet streams.", {"unit": unit["tag"]}
                )
            continue
        if unit.get("type") in {"Pump", "Recycle"} and (len(incoming) != 1 or len(outgoing) != 1):
            raise DynamicError("UNIT_TOPOLOGY_INVALID", "Pump and Recycle require one inlet and one outlet.",
                               {"unit": unit["tag"]})
        row = np.zeros(n)
        for stream in incoming:
            row[index[stream["id"]]] += 1
        for stream in outgoing:
            row[index[stream["id"]]] -= 1
        equations.append(row)
        rhs.append(0.0)
        if unit.get("type") == "SpecifiedSeparator":
            params = unit.get("params", {})
            recovery = float((params.get("biomass_recovery") or {}).get("si", 90.0)) / 100.0
            factor = float((params.get("concentration_factor") or {}).get("si", 10.0))
            if len(incoming) != 1 or len(outgoing) != 2 or not 0 < recovery <= 1 or factor <= 1:
                raise DynamicError(
                    "SEPARATOR_TOPOLOGY_INVALID",
                    "Dynamic SpecifiedSeparator requires one inlet, two outlets, valid recovery and factor.",
                    {"unit": unit["tag"]},
                )
            for stream, ratio in zip(outgoing, (recovery / factor, 1.0 - recovery / factor), strict=True):
                row = np.zeros(n)
                row[index[stream["id"]]] = 1
                row[index[incoming[0]["id"]]] = -ratio
                equations.append(row)
                rhs.append(0.0)
        elif unit.get("type") == "Splitter" and key not in implied_splitters:
            ratio_values = _splitter_ratios(unit, len(outgoing))
            for stream, ratio in zip(outgoing, ratio_values, strict=True):
                row = np.zeros(n)
                row[index[stream["id"]]] = 1
                for inlet in incoming:
                    row[index[inlet["id"]]] -= ratio
                equations.append(row)
                rhs.append(0.0)
    circulation: dict[str, dict[str, Any]] = {}
    units_by_tag = {unit.get("tag"): unit for unit in active_units.values()}
    for pump_tag, flow in scenario.get("circulation", {}).items():
        pump = units_by_tag.get(pump_tag)
        if pump is None or pump.get("type") != "Pump":
            raise DynamicError("CIRCULATION_PUMP_INVALID", "Circulation must target a participating Pump.",
                               {"pump": pump_tag})
        if not math.isfinite(float(flow)) or float(flow) <= 0:
            raise DynamicError("CIRCULATION_FLOW_INVALID", "Specified circulation must be finite and positive.",
                               {"pump": pump_tag})
        outlets = [s for s in relevant if (s.get("source") or {}).get("unit") == pump["id"]]
        if len(outlets) != 1:
            raise DynamicError("UNIT_TOPOLOGY_INVALID", "Circulation Pump requires one outlet.",
                               {"pump": pump_tag})
        row = np.zeros(n)
        row[index[outlets[0]["id"]]] = 1.0
        equations.append(row)
        rhs.append(float(flow))
        circulation[pump_tag] = {"tag": pump_tag, "flow_m3_s": float(flow), "stream_id": outlets[0]["id"]}
    if not equations:
        raise DynamicError("TOPOLOGY_EMPTY", "No dynamic T1 stream topology is connected.")
    matrix, values = np.vstack(equations), np.asarray(rhs)
    circulation_rows = {row: value for row, value in enumerate(circulation.values(), start=len(rhs) - len(circulation))}
    topology = {"units": list(active_units.values()), "streams": relevant, "feeds": feeds,
                "flow_matrix": matrix.tolist(), "flow_rhs": values.tolist(), "feed_rows": feed_rows,
                "circulation_rows": circulation_rows, "circulation": circulation,
                "implied_splitters": sorted(active_units[key]["tag"] for key in implied_splitters)}
    flow_map = _solve_flows(topology)
    for item in units:
        key = item["unit"]["id"]
        qin = sum(flow_map[s["id"]] for s in relevant if (s.get("target") or {}).get("unit") == key)
        if qin <= 0:
            raise DynamicError("ZERO_THROUGHFLOW", "Participating PBR has zero throughflow.", {"unit": item["tag"]})
    return {**topology, "flows": flow_map}


def prepare(workspace_id: str, draft_id: str, scenario_id: str) -> Snapshot:
    """Resolve immutable draft/card/profile inputs and reject unsupported topology pre-worker."""
    try:
        directory = draft.draft_dir(workspace_id, draft_id)
        head = draft._head(directory)
        revision = draft.load_revision(directory, head["revision"])
        document = revision["document"]
        studio = sorted(item.get("tag", item.get("id")) for item in document["objects"].values()
                        if item.get("kind") == "unit" and item.get("type") == "StudioUnit")
        if studio:
            raise DynamicError("STUDIO_UNIT_STEADY_ONLY",
                               "Studio process units are steady only and cannot take part in a dynamic run.",
                               {"units": studio})
        scenario_raw = document.get("scenarios", {}).get(scenario_id)
        if not scenario_raw:
            raise DynamicError("SCENARIO_NOT_FOUND", "Dynamic scenario was not found.")
        if len(scenario_raw.get("controllers", [])) > MAX_CONTROLLERS:
            raise DynamicError("CONTROLLER_LIMIT", "At most 16 controllers may participate.")
        scenario = Scenario.model_validate(scenario_raw)
        start, end = _stamp(scenario.start_utc), _stamp(scenario.end_utc)
        duration = end - start
        if duration <= 0 or duration > MAX_DURATION_S:
            raise DynamicError("DURATION_LIMIT", "Scenario duration must be positive and at most 120 days.")
        if math.ceil(duration / scenario.output_cadence_s) + 1 > MAX_OUTPUT_POINTS:
            raise DynamicError("OUTPUT_LIMIT", "Scenario exceeds the output point limit.")
        schedule_id = scenario_raw.get("schedule_id")
        if schedule_id and schedule_id not in document.get("schedules", {}):
            raise DynamicError("SCHEDULE_NOT_FOUND", "Dynamic schedule was not found.", {"schedule_id": schedule_id})
        schedule_raw = document.get("schedules", {}).get(schedule_id, {"id": "implicit", "events": []})
        schedule = Schedule.model_validate(schedule_raw)
        expanded_events: list[dict[str, Any]] = []
        for order, event in enumerate(schedule.events):
            event_time = float(event.time_s)
            if event_time > duration or (event.end_s is not None and event.end_s > duration):
                raise DynamicError("EVENT_TIME_INVALID", "Schedule event is outside the scenario interval.")
            count = event.count or (
                int(math.floor((min(event.end_s or duration, duration) - event_time) / event.every_s)) + 1
                if event.every_s
                else 1
            )
            for repeat in range(count):
                item = event.model_dump(mode="json")
                item["time_s"] = event_time + repeat * float(event.every_s or 0)
                if item["time_s"] > duration:
                    break
                item["declared_order"] = order
                item["repeat_k"] = repeat
                expanded_events.append(item)
        if len(expanded_events) > MAX_EVENTS:
            raise DynamicError("EVENT_LIMIT", "Expanded schedule exceeds 1000 events.")
        expanded_events.sort(key=lambda item: (float(item["time_s"]), int(item["declared_order"]),
                                               int(item["repeat_k"])))
        controllers_raw = document.get("controllers", {})
        controller_ids = scenario_raw.get("controllers", [])
        if len(controller_ids) > MAX_CONTROLLERS:
            raise DynamicError("CONTROLLER_LIMIT", "At most 16 controllers may participate.")
        controller_items = []
        known_tags = {item.get("tag"): item for item in document["objects"].values()}
        for controller_id in controller_ids:
            raw = controllers_raw.get(controller_id)
            if raw is None:
                raise DynamicError("CONTROLLER_NOT_FOUND", f"Controller {controller_id!r} was not found.")
            controller = Controller.model_validate(raw)
            measurement_parts = controller.measurement.split(".")
            if (len(measurement_parts) not in {1, 2}
                    or measurement_parts[-1] not in {"X", "N", "O2"}
                    or (len(measurement_parts) == 2 and measurement_parts[0] != controller.unit)):
                raise DynamicError(
                    "UNSUPPORTED_MEASUREMENT",
                    "Only T1 X, N and O2 measurements are supported.",
                    {"measurement": controller.measurement},
                )
            if controller.unit not in scenario.units:
                raise DynamicError(
                    "UNSUPPORTED_MEASUREMENT",
                    "Controller measurement unit is not participating.",
                    {"unit": controller.unit},
                )
            actuator_kind, separator, actuator_tag = controller.actuator.partition(":")
            target = known_tags.get(actuator_tag)
            supported = (
                separator
                and target
                and (
                    (actuator_kind == "feed" and target.get("kind") == "stream" and target.get("source") is None)
                    or (
                        actuator_kind == "splitter"
                        and target.get("type") == "Splitter"
                        and target.get("mode") == "split_ratios"
                    )
                )
            )
            if not supported:
                raise DynamicError(
                    "UNSUPPORTED_ACTUATOR",
                    "Only boundary feed flow and splitter ratio actuators are supported.",
                    {"actuator": controller.actuator},
                )
            controller_items.append(controller.model_dump(mode="json"))
        by_tag = {item["tag"]: item for item in document["objects"].values()}
        units = []
        pbr_models = {tag: pbr_unit._resolve(workspace_id, by_tag[tag].get("model") or {})
                      for tag in scenario.units if by_tag.get(tag, {}).get("type") == "PhotobioreactorT1"}
        first_pbr_model = next(iter(pbr_models.values()), None)
        growth_template_params = pbr_unit._parameters(by_tag[next(iter(pbr_models))]) if pbr_models else {}

        def tank_model(tank: dict[str, Any]) -> dict[str, Any] | None:
            graph: dict[str, set[str]] = {}
            for stream in document["objects"].values():
                if stream.get("kind") != "stream" or not stream.get("source") or not stream.get("target"):
                    continue
                source, target = stream["source"]["unit"], stream["target"]["unit"]
                graph.setdefault(source, set()).add(target)
                graph.setdefault(target, set()).add(source)
            reached, pending = {tank["id"]}, [tank["id"]]
            while pending:
                current = pending.pop()
                for neighbor in graph.get(current, set()) - reached:
                    reached.add(neighbor)
                    pending.append(neighbor)
            candidates = [tag for tag in pbr_models if by_tag[tag]["id"] in reached]
            return pbr_models[candidates[0]] if candidates else first_pbr_model
        for tag in scenario.units:
            unit = by_tag.get(tag)
            if unit is None or unit.get("type") not in {"PhotobioreactorT1", "HoldupTank"}:
                raise DynamicError("UNIT_NOT_FOUND", f"Participating T1 unit {tag!r} was not found.")
            model: dict[str, Any] | None
            if unit["type"] == "PhotobioreactorT1":
                params = pbr_unit._parameters(unit)
                model = pbr_models[tag]
                volume = params["tube_count"] * math.pi * params["tube_inner_diameter"] ** 2 * params["tube_length"] / 4
            else:
                params = {key: float((value or {}).get("si", 0.0)) for key, value in unit.get("params", {}).items()}
                volume = params.get("liquid_volume", 0.0)
                if (volume <= 0 or params.get("min_volume", 0.0) < 0
                        or params.get("max_volume", volume) < volume
                        or params.get("min_volume", 0.0) > volume
                        or params.get("temperature", 0.0) <= 0):
                    raise DynamicError("TANK_PARAMETERS_INVALID", "HoldupTank volume limits and temperature are invalid.",
                                       {"unit": tag})
                if ("oxygen_kla" in params) != ("oxygen_saturation" in params):
                    raise DynamicError("TANK_OXYGEN_PARAMETERS_INVALID", "HoldupTank oxygen_kla and oxygen_saturation must be set together.",
                                       {"unit": tag})
                model = tank_model(unit)
            initial = scenario.initial.get(tag, {})
            if set(initial) != {"X", "N", "O2"}:
                raise DynamicError("INITIAL_STATE_REQUIRED", "Each T1 unit requires explicit X, N and O2 initial state.",
                                   {"unit": tag})
            state = [float(initial.get(k, 0.0)) for k in ("X", "N", "O2")]
            if any(not math.isfinite(v) or v < 0 for v in state):
                raise DynamicError(
                    "INITIAL_STATE_INVALID", f"Initial concentrations for {tag} must be finite and nonnegative."
                )
            if unit["type"] == "HoldupTank" and model is None:
                raise DynamicError("TOPOLOGY_CARD_MISMATCH", "A HoldupTank requires a participating PBR model card.",
                                   {"units": [tag]})
            units.append(
                {
                    "tag": tag,
                    "unit": unit,
                    "params": params,
                    "model": model,
                    "volume_m3": volume,
                    "state": state,
                    "growth_template_params": growth_template_params if unit["type"] == "HoldupTank" else params,
                    "card_qualification": model.get("card", {}).get("qualification_status", "unknown") if model else "shared",
                }
            )
        pbr_count = sum(item["unit"]["type"] == "PhotobioreactorT1" for item in units)
        if pbr_count > MAX_PARTICIPATING_PBRS:
            raise DynamicError("UNIT_LIMIT", "At most 8 T1 PBRs may participate.")
        # Reject unsupported connected equipment rather than silently dropping its physics.
        selected = {u["unit"]["id"] for u in units}
        objects = document["objects"]
        t1_reachable = _t1_reachable(objects, selected)
        algebraic: dict[str, set[str]] = {}
        for stream in objects.values():
            if stream.get("kind") != "stream":
                continue
            source, target = stream.get("source"), stream.get("target")
            if source and target:
                src, dst = objects.get(source.get("unit"), {}), objects.get(target.get("unit"), {})
                supported_algebraic = {"Mixer", "Splitter", "Pump", "Recycle", "SpecifiedSeparator"}
                # Only the T1-reachable algebraic network carries culture state; a loop wholly downstream
                # of a DWSIM-owned unit belongs to the downstream sampler, which solves its own tears.
                if (src.get("type") in supported_algebraic and dst.get("type") in supported_algebraic
                        and src["id"] in t1_reachable and dst["id"] in t1_reachable):
                    algebraic.setdefault(src["id"], set()).add(dst["id"])
                if (target.get("unit") in selected and source.get("unit") not in selected
                        and src.get("type") not in supported_algebraic):
                    raise DynamicError(
                        "UNSUPPORTED_TOPOLOGY",
                        "A non-participating unit cannot feed the T1 network.",
                        {"unit": src.get("tag")},
                    )
            if source and source.get("unit") in selected and target and target.get("unit") not in selected:
                foreign = objects.get(target["unit"], {})
                if foreign.get("type") in {
                    "Mixer", "Splitter", "Pump", "Recycle", "SpecifiedSeparator"
                }:
                    continue
            if source and target and source.get("unit") in selected and target.get("unit") in selected:
                pass  # Native PBR-to-PBR recycle coupling is represented by the ODE below.
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise DynamicError(
                    "ALGEBRAIC_CYCLE", "A Mixer/Splitter cycle has no dynamic PBR state.", {"unit_id": node}
                )
            if node in visited:
                return
            visiting.add(node)
            for target in algebraic.get(node, set()):
                visit(target)
            visiting.remove(node)
            visited.add(node)

        for node in algebraic:
            visit(node)
        topology = _topology(document, units, scenario.model_dump(mode="json"))
        for controller_item in controller_items:
            actuator_kind, _, actuator_tag = controller_item["actuator"].partition(":")
            if actuator_kind == "splitter" and actuator_tag in topology["implied_splitters"]:
                raise DynamicError("SPLIT_IMPLIED_BY_CIRCULATION",
                                   "This splitter's split follows the specified loop circulation; "
                                   "actuate the boundary feed instead.", {"splitter": actuator_tag})
        card_by_unit = {unit["unit"]["id"]: (unit["model"].get("card", {}).get("digest"),
                                               unit["model"].get("set", {}).get("digest"))
                        for unit in units}
        graph: dict[str, set[str]] = {}
        for stream in topology["streams"]:
            source = (stream.get("source") or {}).get("unit")
            target = (stream.get("target") or {}).get("unit")
            if source and target:
                graph.setdefault(source, set()).add(target)
                graph.setdefault(target, set()).add(source)
        checked: set[frozenset[str]] = set()
        for origin in card_by_unit:
            todo, reached = [origin], {origin}
            while todo:
                current = todo.pop()
                for neighbor in graph.get(current, set()) - reached:
                    reached.add(neighbor)
                    todo.append(neighbor)
            for other in reached & card_by_unit.keys():
                pair = frozenset((origin, other))
                if pair in checked:
                    continue
                checked.add(pair)
                if card_by_unit[origin] != card_by_unit[other]:
                    tags = {unit["unit"]["id"]: unit["tag"] for unit in units}
                    raise DynamicError(
                        "TOPOLOGY_CARD_MISMATCH",
                        "Coupled T1 units must use the same resolved model card and parameter set.",
                        {"units": sorted(tags[key] for key in pair)},
                    )
        _validate_events(expanded_events, units, topology, controller_ids)
        _validate_actions(expanded_events, units, topology)
        downstream_data = None
        if scenario.downstream_cadence_s:
            samples = math.floor(duration / scenario.downstream_cadence_s)
            if samples > MAX_DWSIM_SAMPLES:
                raise DynamicError("DWSIM_SAMPLE_LIMIT", "Scenario exceeds 200 downstream samples.")
            from app.modules.process_stack.dynamic_downstream import downstream_document

            downstream_data = downstream_document(Snapshot(
                {"document": document, "units": units, "topology": topology}, ""))
            if downstream_data[0] is None:
                raise DynamicError(
                    "DOWNSTREAM_UNITS_REQUIRED",
                    "Downstream sampling requires at least one reachable DWSIM-owned unit.",
                )
            from app.modules.process_stack import mixed

            # DWSIM cannot order a Recycle-free loop, so every sample would fail; refuse before the worker starts.
            loops = mixed.recycle_free_cycles(downstream_data[0])
            if loops:
                raise DynamicError(
                    "DOWNSTREAM_RECYCLE_REQUIRED",
                    "Downstream loop requires a Recycle block before DWSIM can sample it.",
                    {"loops": loops},
                )
            # The carrier is any boundary feed of the dynamic flow network, including one entering a
            # culture loop through its Mixer rather than directly into a PBR or tank.
            carrier_specs = [stream.get("spec", {}) for stream in topology["streams"]
                             if stream["id"] in topology["feeds"]]
            carrier_spec = carrier_specs[0] if carrier_specs else {}
            _validate_downstream_boundary_specs(downstream_data[1], carrier_spec)
        for obj in document["objects"].values():
            if obj.get("type") == "Splitter" and obj.get("mode") != "split_ratios":
                raise DynamicError(
                    "SPLITTER_FLOW_SPEC_UNSUPPORTED",
                    "Dynamic T1 supports split_ratios mode only.",
                    {"unit": obj.get("tag")},
                )
        from app.modules.environment import profiles

        resolved_profiles: list[dict[str, Any]] = []
        forcing_profile_id = scenario.forcing_profile_id or scenario.profiles[0]["profile_id"]
        if forcing_profile_id not in {ref.get("profile_id") for ref in scenario.profiles}:
            raise DynamicError("PROFILE_NOT_BOUND", "Forcing profile must be one of the bound profiles.")
        if len({ref.get("profile_id") for ref in scenario.profiles}) != len(scenario.profiles):
            raise DynamicError("PROFILE_DUPLICATE", "Scenario profiles must have distinct identities.")
        for ref in scenario.profiles:
            try:
                profile = profiles._read(workspace_id, ref["digest"])
            except profiles.EnvironmentIntegrityError as exc:
                raise DynamicError(
                    "PROFILE_DIGEST_INVALID",
                    "Environment profile digest verification failed.",
                    {"profile_id": ref.get("profile_id")},
                ) from exc
            if ref["profile_id"] != ref["digest"]:
                raise DynamicError("PROFILE_ID_MISMATCH", "Profile id must equal its immutable digest.")
            times = [_stamp(s) for s in profile["timestamps"]]
            channels = profile["channels"]
            if ref["profile_id"] != forcing_profile_id:
                resolved_profiles.append({"ref": ref, "profile": profile, "times": times,
                                          "par_name": None, "temp_name": None})
                continue
            par_name = "par" if "par" in channels else "ghi"
            if par_name == "ghi" and scenario.par_from_ghi_factor is None:
                raise DynamicError("PAR_UNAVAILABLE", "Profile has GHI but no declared PAR conversion factor.")
            temp_name = scenario.temperature_source
            if temp_name != "unit_mean" and temp_name not in channels:
                raise DynamicError("TEMPERATURE_UNAVAILABLE", f"Profile has no {temp_name} channel.")
            if par_name not in channels or times[0] > start or times[-1] < end:
                raise DynamicError("PROFILE_COVERAGE", "Profile does not cover the full scenario time range.")
            if temp_name != "unit_mean" and (times[0] > start or times[-1] < end):
                raise DynamicError("PROFILE_COVERAGE", "Temperature profile lacks required interpolation brackets.")
            par_first = max(0, bisect.bisect_left(times, start))
            par_last = min(len(times), bisect.bisect_left(times, end) + 1)
            for index in range(par_first, par_last):
                if channels[par_name][index] is None:
                    raise DynamicError(
                        "PROFILE_GAP",
                        "Null PAR value blocks a consumed interval.",
                        {"channel": par_name, "time_utc": profile["timestamps"][index]},
                    )
            if temp_name != "unit_mean":
                first = max(0, bisect.bisect_right(times, start) - 1)
                last = min(len(times), bisect.bisect_left(times, end) + 2)
                for index in range(first, last):
                    if channels[temp_name][index] is None:
                        raise DynamicError(
                            "PROFILE_GAP",
                            "Null temperature value blocks an interpolation bracket.",
                            {"channel": temp_name, "time_utc": profile["timestamps"][index]},
                        )
            resolved_profiles.append(
                {"ref": ref, "profile": profile, "times": times, "par_name": par_name, "temp_name": temp_name}
            )
        resolved_profiles.sort(key=lambda item: item["ref"]["profile_id"] != forcing_profile_id)
        _prepare_t2_optics(workspace_id, units, resolved_profiles[0], scenario)
        payload: dict[str, Any] = {
            "workspace_id": workspace_id,
            "draft_id": draft_id,
            "revision": revision["revision"],
            "content_digest": draft.content_digest(document),
            "scenario_id": scenario_id,
            "scenario": scenario.model_dump(mode="json"),
            "units": units,
            "profiles": resolved_profiles,
            "start_epoch": start,
            "end_epoch": end,
            "document": document,
            "schedule": {"id": schedule.id, "events": expanded_events},
            "controllers": controller_items,
            "downstream": ({"document": downstream_data[0], "boundary_streams": downstream_data[1]}
                           if downstream_data is not None else None),
        }
        payload["topology"] = topology
        digest = hashlib.sha256(
            _canonical(
                {k: v for k, v in payload.items() if k not in {"units", "profiles"}}
                | {"unit_tags": [u["tag"] for u in units], "profiles": [p["ref"] for p in resolved_profiles]}
            )
        ).hexdigest()
        payload["snapshot_digest"] = digest
        return Snapshot(payload=payload, digest=digest)
    except DynamicError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None) or "INPUT_INVALID"
        raise DynamicError(str(code), str(exc), getattr(exc, "detail", {})) from exc


def _profile_par(snapshot: Snapshot, elapsed_s: float) -> float:
    item = snapshot.payload["profiles"][0]
    channels = item["profile"]["channels"]
    elapsed_edges = [value - snapshot.payload["start_epoch"] for value in item["times"]]
    par_ix = max(0, bisect.bisect_left(elapsed_edges, elapsed_s))
    raw_par = channels[item["par_name"]][par_ix]
    if raw_par is None:
        raise DynamicError("PROFILE_GAP", "Null PAR value blocks the consumed interval.",
                           {"channel": item["par_name"], "time_utc": datetime.fromtimestamp(item["times"][par_ix], UTC).isoformat()})
    factor = snapshot.payload["scenario"].get("par_from_ghi_factor") if item["par_name"] == "ghi" else 1.0
    return float(raw_par) * float(factor or 1.0) * snapshot.payload["scenario"].get("par_scale", 1.0)


def _optics_index(snapshot: Snapshot, elapsed_s: float) -> int:
    times = snapshot.payload["profiles"][0]["times"]
    return max(0, min(len(times) - 1, bisect.bisect_left(times, snapshot.payload["start_epoch"] + elapsed_s)))


def _profile_temperature(snapshot: Snapshot, elapsed_s: float, unit_index: int = 0) -> float:
    unit = snapshot.payload["units"][unit_index]
    if _unit_type(unit) == "HoldupTank":
        return float(unit["params"]["temperature"])
    item = snapshot.payload["profiles"][0]
    if item["temp_name"] == "unit_mean":
        return float(unit["params"]["temperature_mean"])
    times = item["times"]
    epoch = snapshot.payload["start_epoch"] + elapsed_s
    channels = item["profile"]["channels"]
    j = min(max(bisect.bisect_left(times, epoch), 1), len(times) - 1)
    before, after = channels[item["temp_name"]][j - 1], channels[item["temp_name"]][j]
    if before is None or after is None:
        bad = j - 1 if before is None else j
        raise DynamicError("PROFILE_GAP", "Null temperature value blocks an interpolation bracket.",
                           {"channel": item["temp_name"], "time_utc": datetime.fromtimestamp(times[bad], UTC).isoformat()})
    weight = (epoch - times[j - 1]) / (times[j] - times[j - 1])
    return float(before) + weight * (float(after) - float(before))


def _profile_values(snapshot: Snapshot, epoch: float) -> tuple[float, float]:
    elapsed = epoch - snapshot.payload["start_epoch"]
    return _profile_par(snapshot, elapsed), _profile_temperature(snapshot, elapsed)


def _network_runtime(topology: dict[str, Any], flows: dict[str, float], units: list[dict[str, Any]]) -> dict[str, Any]:
    """Precompute inlet flow weights and algebraic evaluation order for one flow state."""
    streams = topology["streams"]
    by_id = {unit["id"]: unit for unit in topology["units"]}
    quota_by_id = {item["unit"]["id"]: item["growth"].nitrogen_quota for item in units}
    incoming: dict[str, list[tuple[dict[str, Any], float]]] = {}
    for item in units:
        key = item["unit"]["id"]
        inlet = [stream for stream in streams if (stream.get("target") or {}).get("unit") == key]
        total = sum(flows.get(stream["id"], 0.0) for stream in inlet)
        incoming[key] = [(stream, flows.get(stream["id"], 0.0) / total) for stream in inlet]
    algebraic_types = {"Mixer", "Splitter", "Pump", "Recycle", "SpecifiedSeparator"}
    pending = {key for key, unit in by_id.items() if unit.get("type") in algebraic_types}
    algebraic_incoming = {key: [stream for stream in streams if (stream.get("target") or {}).get("unit") == key]
                          for key in pending}
    order = []
    while pending:
        ready = sorted(key for key in pending if not any(
            (stream.get("source") or {}).get("unit") in pending
            for stream in streams if (stream.get("target") or {}).get("unit") == key
        ))
        if not ready:
            raise DynamicError("ALGEBRAIC_CYCLE", "Mixer/Splitter concentration graph contains a cycle.")
        order.extend(ready)
        pending.difference_update(ready)
    feed_quota = {
        stream["id"]: quota_by_id.get((stream.get("target") or {}).get("unit"), 0.0)
        for stream in streams if stream.get("source") is None
    }
    return {"incoming": incoming, "algebraic_order": order, "algebraic_incoming": algebraic_incoming,
            "feed_quota": feed_quota}


def _network_concentrations(topology: dict[str, Any], runtime: dict[str, Any], state: tuple[float, ...],
                            units: list[dict[str, Any]], flows: dict[str, float]) -> dict[str, tuple[float, ...]]:
    concentrations: dict[str, tuple[float, ...]] = {}
    for i, item in enumerate(units):
        x, nitrogen, oxygen = (float(v) for v in state[_slot(i):_slot(i, 3)])
        unit_value = (x, nitrogen, oxygen, item["growth"].nitrogen_quota * x)
        concentrations[item["unit"]["id"]] = unit_value
        for stream in topology["streams"]:
            if (stream.get("source") or {}).get("unit") == item["unit"]["id"]:
                concentrations[f"stream:{stream['id']}"] = unit_value
    for key in runtime["algebraic_order"]:
        inlet = runtime["algebraic_incoming"][key]
        total = sum(flows.get(stream["id"], 0.0) for stream in inlet)
        if total <= 0:
            raise DynamicError("ZERO_THROUGHFLOW", "Algebraic unit has zero throughflow.", {"unit_id": key})
        mixed: np.ndarray = np.zeros(4, dtype=np.float64)
        for stream in inlet:
            source = (stream.get("source") or {}).get("unit")
            value = topology["feeds"].get(stream["id"]) if source is None else concentrations.get(
                f"stream:{stream['id']}"
            )
            if value is None:
                raise DynamicError("TOPOLOGY_UNRESOLVED", "A stream concentration could not be resolved.")
            if source is None:
                quota = runtime["feed_quota"].get(stream["id"], 0.0)
                value = (*value[:3], quota * value[0])
            mixed += flows.get(stream["id"], 0.0) * np.asarray(value)
        inlet_value = mixed / total
        unit = next(item for item in topology["units"] if item["id"] == key)
        kind = unit.get("type")
        outgoing = sorted(
            (stream for stream in topology["streams"] if (stream.get("source") or {}).get("unit") == key),
            key=lambda stream: (stream.get("source") or {}).get("port", 0),
        )
        if kind in {"Pump", "Recycle"} and len(outgoing) != 1:
            raise DynamicError("UNIT_TOPOLOGY_INVALID", "Pump and Recycle require one outlet.",
                               {"unit": unit.get("tag")})
        if kind == "SpecifiedSeparator":
            if len(outgoing) != 2:
                raise DynamicError("SEPARATOR_TOPOLOGY_INVALID", "SpecifiedSeparator requires two outlets.",
                                   {"unit": unit.get("tag")})
            params = unit.get("params", {})
            recovery = float((params.get("biomass_recovery") or {}).get("si", 90.0)) / 100.0
            factor = float((params.get("concentration_factor") or {}).get("si", 10.0))
            concentrate = recovery / factor
            clarified = 1.0 - concentrate
            # The ratio-weighted biomass and quota-N concentrations conserve their flow rates.
            outlet_values: tuple[tuple[float, ...], ...] = (
                (inlet_value[0] * factor, inlet_value[1], inlet_value[2], inlet_value[3] * factor),
                (inlet_value[0] * (1.0 - recovery) / clarified, inlet_value[1], inlet_value[2],
                 inlet_value[3] * (1.0 - recovery) / clarified),
            )
        else:
            outlet_values = (tuple(inlet_value.tolist()),) * len(outgoing)
        for stream, value in zip(outgoing, outlet_values, strict=False):
            concentrations[f"stream:{stream['id']}"] = value
    return concentrations


def _result_series(snapshot: Snapshot, times: list[float], rows: list[np.ndarray], units: list[dict[str, Any]],
                   controllers: list[dict[str, Any]], controller_log: list[dict[str, Any]],
                   flow_log: list[dict[str, Any]] | None = None,
                   topology_override: dict[str, Any] | None = None,
                   volume_rows: list[list[float]] | None = None, phase_rows: list[int] | None = None,
                   order_rows: list[int] | None = None) -> dict[str, np.ndarray]:
    elapsed = np.asarray(times, dtype=np.float64)
    matrix = np.asarray(rows, dtype=np.float64)
    volumes = (np.asarray(volume_rows, dtype=np.float64) if volume_rows is not None
               else np.tile([unit["volume_m3"] for unit in units], (len(times), 1)))
    phases = list(phase_rows) if phase_rows is not None else [0] * len(times)
    result = {"t_s": elapsed, "par": np.asarray([_profile_par(snapshot, float(t)) for t in elapsed])}
    if any(phases):
        # Action events (185): two rows share t_s; -1 is the state before the event, +1 after it.
        result["event_phase"] = np.asarray(phases, dtype=np.float64)
        result["event_order"] = np.asarray(order_rows or [-1] * len(times), dtype=np.float64)
    result["temperature"] = np.asarray([_profile_temperature(snapshot, float(t)) for t in elapsed])
    for i, unit in enumerate(units):
        for j, name in enumerate(("X", "N", "O2")):
            result[f"{unit['tag']}_{name}"] = matrix[:, _slot(i, j)]
        result[f"{unit['tag']}_temperature"] = np.asarray(
            [_profile_temperature(snapshot, float(t), i) for t in elapsed]
        )
        result[f"{unit['tag']}_harvest_X_kg"] = matrix[:, _slot(i, 6)]
        if "optics_t2" in unit:
            optics = unit["optics_t2"]
            optics_indices = np.asarray([_optics_index(snapshot, float(t)) for t in elapsed], dtype=np.int64)
            handoff = {name: np.asarray(values, dtype=np.float64)
                       for name, values in optics["intervals"].items()}
            tag = unit["tag"]
            result[f"{tag}_par_intercepted_umol_s_m"] = handoff["intercepted"][optics_indices]
            result[f"{tag}_par_eq_umol_m2_s"] = handoff["i0_eq"][optics_indices]
            result[f"{tag}_beam_unshaded_fraction"] = np.nan_to_num(
                handoff["beam_unshaded_fraction"][optics_indices], nan=0.0
            )
            growth = _growth_for_unit(unit)
            absorbed = optics_t2.array_model(optics_t2.Optics(**optics["layout"])).evaluate(
                handoff["elevation_rad"][optics_indices], handoff["azimuth_rad"][optics_indices],
                np.asarray(optics["dni_par"], dtype=np.float64)[optics_indices],
                np.asarray(optics["dhi_par"], dtype=np.float64)[optics_indices],
                np.maximum(0.0, matrix[:, _slot(i)] * growth.extinction),
            )["absorbed"]
            result[f"{tag}_par_absorbed_umol_s_m"] = absorbed
        if _unit_type(unit) == "HoldupTank":
            result[f"{unit['tag']}_V_m3"] = volumes[:, i]
    result["harvest_X_kg"] = matrix[:, _slot(len(units), 3)] + sum(
        result[f"{unit['tag']}_harvest_X_kg"] for unit in units
    )
    for controller in controllers:
        result[f"controller_{controller['id']}"] = np.asarray([
            next((entry["output"] for entry in reversed(controller_log)
                  if entry["controller"] == controller["id"] and entry["time_s"] <= t),
                 float(controller["_initial_output"])) for t in times
        ], dtype=np.float64)
    if flow_log:
        topology = topology_override or snapshot.payload["topology"]
        # A pre-event row reads the flows in force before that instant; every other row uses the latest record.
        def record_at(t: float, phase: int) -> dict[str, Any]:
            earlier = [entry for entry in flow_log if entry["time_s"] < t - 1e-7] if phase < 0 else []
            return earlier[-1] if earlier else next(entry for entry in reversed(flow_log) if entry["time_s"] <= t)

        records = [record_at(float(t), phase) for t, phase in zip(times, phases, strict=True)]
        flows_at = [entry["flows"] for entry in records]
        hydro_at = [{**entry.get("hydraulics", {}), "culture_loops": entry.get("culture_loops", {})}
                    for entry in records]
        for stream in topology["streams"]:
            if stream.get("source") is None:
                result[f"feed_{stream['tag']}_Q_m3_s"] = np.asarray(
                    [item[stream["id"]] for item in flows_at], dtype=np.float64
                )
        for unit in topology["units"]:
            if unit.get("type") != "Splitter":
                continue
            outlets = sorted(
                (stream for stream in topology["streams"]
                 if (stream.get("source") or {}).get("unit") == unit["id"]),
                key=lambda stream: (stream.get("source") or {}).get("port", 0),
            )
            for stream in outlets:
                result[f"splitter_{unit['tag']}_{stream['tag']}_ratio"] = np.asarray(
                    [item[stream["id"]] / total if total > 0 else math.nan
                     for item in flows_at
                     for total in [sum(item[out["id"]] for out in outlets)]], dtype=np.float64
                )
        for unit in units:
            if _unit_type(unit) == "PhotobioreactorT1":
                result[f"{unit['tag']}_pass_rate_1_s"] = np.asarray([
                    values.get(unit["tag"], {}).get("pass_rate_1_s", math.nan) for values in hydro_at
                ], dtype=np.float64)
        by_tag = {unit["tag"]: i for i, unit in enumerate(units)}
        for loop in topology.get("culture_loops", []):
            members = [(by_tag[tag], next(unit for unit in units if unit["tag"] == tag))
                       for tag in loop["member_tags"]]
            indices = [index for index, _ in members]
            volume = volumes[:, indices].sum(axis=1)
            biomass = (matrix[:, [_slot(index) for index in indices]] * volumes[:, indices]).sum(axis=1)
            result[f"{loop['id']}_X_mean"] = biomass / volume
            result[f"{loop['id']}_N_mean"] = (matrix[:, [_slot(index, 1) for index in indices]]
                                               * volumes[:, indices]).sum(axis=1) / volume
            result[f"{loop['id']}_volume_m3"] = volume
            result[f"{loop['id']}_biomass_kg"] = biomass
        for unit in units:
            tag = unit["tag"]
            if not any(tag in values for values in hydro_at):
                continue
            for key in ("velocity_m_s", "reynolds_number", "pressure_drop", "pumping_power"):
                result[f"{tag}_{key}"] = np.asarray([values.get(tag, {}).get(key, math.nan)
                                                     for values in hydro_at], dtype=np.float64)
        for loop in topology.get("culture_loops", []):
            tag = loop["id"]
            for key, suffix in (("pass_transit_time_s", "pass_transit_time_s"),
                                ("circulation_m3_s", "circulation_Q_m3_s"),
                                ("net_dilution_1_s", "net_dilution_1_s"),
                                ("culture_residence_time_s", "culture_residence_time_s")):
                if key == "culture_residence_time_s":
                    result[f"{tag}_{suffix}"] = np.asarray([
                        math.inf if values.get("culture_loops", {}).get(tag, {}).get(key) == "∞, batch"
                        else values.get("culture_loops", {}).get(tag, {}).get(key, math.nan)
                        for values in hydro_at
                    ], dtype=np.float64)
                else:
                    result[f"{tag}_{suffix}"] = np.asarray([
                        values.get("culture_loops", {}).get(tag, {}).get(key, math.nan) for values in hydro_at
                    ], dtype=np.float64)
    return result


def run(
    snapshot: Snapshot,
    *,
    cancelled: Callable[[], bool],
    progress: Callable[[float], None],
    sampler: Callable[..., dict] | None = None,
) -> EngineResult:
    """Integrate a pinned scenario. All rates stay hourly in the 170 seam and convert once to SI seconds."""
    started = time.perf_counter()
    scenario = snapshot.payload["scenario"]
    units = [dict(unit) for unit in snapshot.payload["units"]]
    start, end = snapshot.payload["start_epoch"], snapshot.payload["end_epoch"]
    duration = end - start
    cadence = float(scenario["output_cadence_s"])
    outputs: np.ndarray = np.arange(0.0, duration, cadence, dtype=np.float64)
    if not len(outputs) or outputs[-1] != duration:
        outputs = np.append(outputs, duration)
    if outputs[0] != 0:
        outputs = np.insert(outputs, 0, 0.0)
    n = len(units)
    topology = snapshot.payload.get("topology", {"units": [], "streams": [], "flows": {}, "feeds": {}})
    topology = dict(topology, flows=dict(topology["flows"]), feeds={
        key: ([value.get("X", 0.0), value.get("N", 0.0), value.get("O2", 0.0)]
              if isinstance(value, dict) else value)
        for key, value in topology["feeds"].items()
    })
    growths = [_growth_for_unit(item, dark=_unit_type(item) == "HoldupTank") for item in units]
    for item, growth in zip(units, growths, strict=True):
        item["growth"] = growth
        item["initial_volume_m3"] = item["volume_m3"]
    # Per unit: X,N,O2 plus integrated generation, transfer and discrete harvest.
    # Global states: boundary X/N/O2 balance and continuous harvested biomass mass.
    initial: np.ndarray = np.zeros(_slot(n) + 4, dtype=np.float64)
    for i, unit in enumerate(units):
        initial[_slot(i) : _slot(i, 3)] = unit["state"]
    rows: list[np.ndarray] = [initial.copy()]
    times_out = [0.0]
    # Per output row: unit liquid volumes, and the action-event phase (0 sample, -1 pre, +1 post) and order.
    volume_rows: list[list[float]] = [[unit["volume_m3"] for unit in units]]
    phase_rows: list[int] = [0]
    order_rows: list[int] = [-1]
    ledger = _campaign_ledger()

    def append_row(time_s: float, state: np.ndarray, phase: int = 0, order: int = -1) -> None:
        times_out.append(float(time_s))
        rows.append(np.asarray(state, dtype=np.float64).copy())
        volume_rows.append([unit["volume_m3"] for unit in units])
        phase_rows.append(phase)
        order_rows.append(order)

    def overwrite_last(state: np.ndarray) -> None:
        rows[-1] = state.copy()
        volume_rows[-1] = [unit["volume_m3"] for unit in units]

    def apply_at(time_s: float, cursor: int, state: np.ndarray) -> np.ndarray:
        """Apply one scheduled event at an integration boundary, keeping pre/post rows for action events."""
        event = events[cursor][1]
        if event["type"] != "actions":
            state = _apply_dynamic_event(
                state, events[cursor], units, event_log, cursor,
                topology, flows, feeds, setpoints, {item["id"]: item for item in controllers},
            )
            if times_out and abs(times_out[-1] - time_s) < 1e-7:
                overwrite_last(state)
            return state
        return apply_action_row(time_s, events[cursor], cursor, state)

    def apply_action_row(time_s: float, indexed: tuple[int, dict[str, Any]], order: int,
                         state: np.ndarray) -> np.ndarray:
        """One action event at ``time_s``: a pre row, the atomic actions, a post row."""
        if triggers:  # spec 186: state-triggered fires make volumes unpredictable, so every action is guarded
            _runtime_volume_guard(indexed[1], units, topology)
        if times_out and abs(times_out[-1] - time_s) < 1e-7 and phase_rows[-1] == 0:
            phase_rows[-1], order_rows[-1] = -1, order
        else:
            append_row(time_s, state, -1, order)
        state = _apply_actions(state, indexed, units, event_log, order, topology, ledger)
        append_row(time_s, state, 1, order)
        return state

    def fire_trigger(trigger: Trigger, time_s: float, state: np.ndarray, observed: float) -> np.ndarray:
        """Apply a state-triggered event at its root time and link the fire to its condition (spec 186)."""
        event = dict(trigger.event, time_s=time_s)
        state = apply_action_row(time_s, (trigger.order, event), trigger.order, state)
        event_log[-1].update(trigger="state", condition={
            **trigger.event["condition"], "fire_index": trigger.fires, "root_time_s": time_s,
            "observed_value": observed, "earliest_s": float(trigger.event["time_s"])})
        return state
    event_log: list[dict[str, Any]] = []
    controller_log: list[dict[str, Any]] = []
    flow_log: list[dict[str, Any]] = []
    downstream: list[dict[str, Any]] = []
    # Prepared snapshots are already ordered; sorting again keeps run() safe for hand-built snapshots.
    all_events = sorted(
        ((index, dict(event)) for index, event in enumerate(snapshot.payload["schedule"].get("events", []))),
        key=lambda pair: (float(pair[1]["time_s"]), int(pair[1].get("declared_order", pair[0])),
                          int(pair[1].get("repeat_k", 0)), pair[0]),
    )
    # State-triggered actions (spec 186) fire at root times, never from the time cursor.
    events = [pair for pair in all_events if not pair[1].get("condition")]
    state_events = [pair for pair in all_events if pair[1].get("condition")]
    controllers = [dict(value, _integral=0.0, _active=False, _bias=float(value["output"]),
                        _initial_output=float(value["output"]))
                   for value in snapshot.payload.get("controllers", [])]
    controller_cadence = float(scenario.get("controller_cadence_s", 300))
    has_conditional_events = any(event.get("observed") for _, event in events)
    controller_times = (
        set(float(t) for t in np.arange(controller_cadence, duration, controller_cadence))
        if controllers or has_conditional_events else set()
    )
    for item in controllers:
        cadence_i = float(item["cadence_s"])
        controller_times.update(float(t) for t in np.arange(cadence_i, duration, cadence_i))
    flows, feeds = topology["flows"], topology["feeds"]
    topology["culture_loops"] = _culture_loops(topology, units, flows)
    triggers = [Trigger(len(events) + k, event, _make_observer(event["condition"]["observed"], units, topology))
                for k, (_, event) in enumerate(state_events)]
    hydraulic_findings: list[dict[str, Any]] = []
    hydraulic_cache: dict[tuple[tuple[str, float], ...], dict[str, Any]] = {}

    def record_flow(time_s: float) -> None:
        signature = (tuple(sorted((key, float(value)) for key, value in flows.items()))
                     + tuple(("V:" + unit["tag"], float(unit["volume_m3"])) for unit in units))
        if signature not in hydraulic_cache:
            hydro, findings = _flow_hydraulics(topology, units, flows)
            hydraulic_cache[signature] = hydro
            for finding in findings:
                if finding not in hydraulic_findings:
                    hydraulic_findings.append(finding)
        flow_log.append({"time_s": time_s, "flows": dict(flows), "hydraulics": hydraulic_cache[signature],
                         "culture_loops": {loop["id"]: loop for loop in _culture_loops(topology, units, flows)}})

    setpoints = {value["id"]: float(value["setpoint"]) for value in controllers}
    controller_by_id = {item["id"]: item for item in controllers}
    conditional_state: dict[int, bool] = {}
    roundoff_clips: list[dict[str, Any]] = []

    def diagnostic(status: Literal["failed", "cancelled"], error: dict[str, Any]) -> EngineResult:
        series = _result_series(snapshot, times_out, rows, units, controllers, controller_log, flow_log, topology,
                                volume_rows, phase_rows, order_rows)
        manifest = _manifest(snapshot, started, event_log, controller_log, downstream)
        if triggers:
            manifest["state_triggers"] = [trigger.summary() for trigger in triggers]
        _add_diagnostic_evidence(manifest, series, controllers)
        manifest["balances"] = _partial_balances(current, units, growths, event_log)
        manifest["artifact_label"] = f"diagnostic_{status}"
        return EngineResult(status, error, series, manifest)

    boundaries = sorted(
        set(float(v) for v in outputs)
        | {float(t - start) for t in snapshot.payload["profiles"][0]["times"] if start < t < end}
        | {float(event["time_s"]) for _, event in all_events if 0 < event["time_s"] < duration}
        | controller_times
        | (set(float(t) for t in np.arange(float(scenario["downstream_cadence_s"]), duration,
                                           float(scenario["downstream_cadence_s"])))
           if scenario.get("downstream_cadence_s") else set())
    )
    current: np.ndarray = initial
    try:
        event_cursor = 0
        while event_cursor < len(events) and float(events[event_cursor][1]["time_s"]) == 0:
            current = apply_at(0.0, event_cursor, current)
            event_cursor += 1
        record_flow(0.0)
        if phase_rows[-1] == 0:
            overwrite_last(current)
        for left, right in zip(boundaries, boundaries[1:], strict=False):
            if cancelled():
                return diagnostic("cancelled", {"code": "CANCELLED", "message": "Run cancelled.", "detail": {}})

            par_segment = _profile_par(snapshot, (left + right) / 2.0)
            optics_index = _optics_index(snapshot, (left + right) / 2.0) if any(
                "optics_t2" in item for item in units
            ) else 0
            t0, restarts = left, 0
            while True:
                restarts += 1
                if restarts > MAX_SEGMENT_RESTARTS:
                    raise DynamicError("SOLVER_FAILED", "State triggers restarted the integration too often.",
                                       {"segment_s": [left, right]})
                for trigger in triggers:
                    trigger.settle(t0, current)
                live = [trigger for trigger in triggers if trigger.live(t0)]
                stop = min([right, *[t for t in (trigger.stop_after(t0, right) for trigger in triggers)
                                     if t is not None]])
                if stop <= t0 + 1e-9:
                    if stop >= right - 1e-9:
                        break
                    t0 = stop
                    continue
                network = _network_runtime(topology, flows, units)

                def rhs(elapsed: float, state: tuple[float, ...], network_state: dict[str, Any] = network,
                        par_value: float = par_segment, optics_step: int = optics_index) -> list[float]:
                    result = [0.0] * len(state)
                    concentrations = _network_concentrations(topology, network_state, state, units, flows)
                    for i, item in enumerate(units):
                        off = _slot(i)
                        x, nitrogen, oxygen = state[off:off + 3]
                        growth = growths[i]
                        temperature = _profile_temperature(snapshot, elapsed, i)
                        is_tank = _unit_type(item) == "HoldupTank"
                        if "optics_t2" in item:
                            handoff = item["optics_t2"]["intervals"]
                            mu, loss = growth.rates_at(
                                float(handoff["i0_eq"][optics_step]), temperature, x, nitrogen,
                                float(handoff["diffuse_fraction"][optics_step]),
                            )
                        else:
                            mu, loss = growth.rates_at(0.0 if is_tank else par_value,
                                                       item["params"]["temperature"] if is_tank else temperature,
                                                       x, nitrogen)
                        rx = (mu - loss) * x / 3600.0
                        transfer = growth.kla_h * (growth.oxygen_saturation - oxygen) / 3600.0
                        inlet: np.ndarray = np.zeros(3, dtype=np.float64)
                        qin = 0.0
                        for stream, weight in network_state["incoming"][item["unit"]["id"]]:
                            flow = flows.get(stream["id"], 0.0)
                            source = (stream.get("source") or {}).get("unit")
                            value = feeds.get(stream["id"]) if source is None else concentrations.get(
                                f"stream:{stream['id']}"
                            )
                            if value is None:
                                raise DynamicError("TOPOLOGY_UNRESOLVED", "A PBR inlet concentration could not be resolved.")
                            inlet += weight * np.asarray(value[:3])
                            qin += flow
                        volume = item["volume_m3"]
                        dilution = qin / volume
                        net_x = rx + dilution * (inlet[0] - x)
                        net_n = -growth.nitrogen_quota * rx + dilution * (inlet[1] - nitrogen)
                        net_o = growth.oxygen_yield * rx + transfer + dilution * (inlet[2] - oxygen)
                        # Accumulators are inventories in kg (spec 185), so a later volume change cannot rescale them.
                        result[off:off + 7] = [net_x, net_n, net_o, rx * volume, -growth.nitrogen_quota * rx * volume,
                                               transfer * volume, 0.0]
                    # Boundary terms are inventory rates in kg/s. Fourth concentration is q*X,
                    # propagated independently through algebraic mixers/splitters.
                    active_ids = {unit["id"] for unit in topology["units"]}
                    for stream in topology["streams"]:
                        source, target = stream.get("source"), stream.get("target")
                        if source is not None and (target or {}).get("unit") in active_ids:
                            continue
                        value = feeds.get(stream["id"]) if source is None else concentrations.get(
                            f"stream:{stream['id']}"
                        )
                        if value is None:
                            continue
                        if source is None:
                            quota_x = network_state["feed_quota"].get(stream["id"], 0.0) * value[0]
                        else:
                            quota_x = value[3]
                        q = flows.get(stream["id"], 0.0) * (1.0 if source is None else -1.0)
                        result[_slot(n):_slot(n, 3)] += q * np.asarray([value[0], value[1] + quota_x, value[2]])
                        if source is not None:
                            result[_slot(n, 3)] += flows.get(stream["id"], 0.0) * value[0]
                    return result

                grid = [t0, *[float(t) for t in outputs if t0 < t < stop], stop]
                from app.modules.process_stack.dynamics import integrate_ode

                solved = integrate_ode(
                    rhs,
                    current.tolist(),
                    grid,
                    rtol=float(scenario["rtol"]),
                    atol=float(scenario["atol"]),
                    method=scenario["solver_method"],
                    roots=_root_values(live) if live else None,
                    root_directions=[trigger.direction for trigger in live],
                )
                if not solved.success:
                    raise DynamicError("SOLVER_FAILED", solved.message, {"segment_s": [t0, stop]})
                for t, solved_state in zip(solved.times[1:], solved.states[1:], strict=True):
                    state_row = np.asarray(solved_state, dtype=np.float64)
                    _valid_state(state_row, units, growths)
                    _clip_tiny_negatives(state_row, units, roundoff_clips, float(t))
                    if any(abs(float(t) - float(out)) <= 1e-7 for out in outputs):
                        append_row(float(t), state_row)
                current = np.asarray(solved.states[-1], dtype=np.float64)
                if solved.root_time is None:
                    _clip_tiny_negatives(current, units, roundoff_clips, stop)
                    t0 = stop
                    if stop >= right - 1e-9:
                        break
                    continue
                # A root: the exact crossing time. Re-arm roots only change the trigger; a fire applies the actions.
                root_time = float(solved.root_time)
                _clip_tiny_negatives(current, units, roundoff_clips, root_time)
                observed_at_root = {id(live[k]): live[k].observe(current) for k in solved.root_indices}
                fired = False
                for k in solved.root_indices:
                    trigger = live[k]
                    if trigger.crossed(root_time, current):
                        current = fire_trigger(trigger, root_time, current, observed_at_root[id(trigger)])
                        fired = True
                if fired:
                    record_flow(root_time)
                t0 = root_time
                if t0 >= right - 1e-9:
                    break
            while event_cursor < len(events) and abs(float(events[event_cursor][1]["time_s"]) - right) < 1e-7:
                current = apply_at(right, event_cursor, current)
                event_cursor += 1
            if any(abs(right % float(item["cadence_s"])) < 1e-7 for item in controllers) or (
                controller_cadence and abs(right % controller_cadence) < 1e-7
            ):
                _sample_controllers(current, units, controllers, topology, flows, setpoints, right, controller_log)
                _conditional_events(current, units, events, conditional_state, event_log, right,
                                    topology, flows, feeds, setpoints, controller_by_id)
                if times_out and abs(times_out[-1] - right) < 1e-7:
                    overwrite_last(current)
            record_flow(right)
            if (
                sampler
                and scenario.get("downstream_cadence_s")
                and abs(right % scenario["downstream_cadence_s"]) < 1e-7
            ):
                if cancelled():
                    return diagnostic("cancelled", {"code": "CANCELLED", "message": "Run cancelled.", "detail": {}})
                try:
                    result = sampler(
                        {
                            "time_s": right,
                            "units": {u["tag"]: current[_slot(i) : _slot(i, 3)].tolist() for i, u in enumerate(units)},
                            "streams": _boundary_streams(topology, current, units, flows, feeds),
                        }
                    )
                    downstream.append({"time_s": right, **result})
                except Exception as exc:
                    downstream.append({"time_s": right, "status": "downstream_unconverged",
                                       "error_type": type(exc).__name__})
                if downstream[-1].get("status") == "unconverged":
                    downstream[-1]["status"] = "downstream_unconverged"
                if cancelled():
                    return diagnostic("cancelled", {"code": "CANCELLED", "message": "Run cancelled.", "detail": {}})
            progress(min(1.0, right / duration))
        series = _result_series(snapshot, times_out, rows, units, controllers, controller_log, flow_log, topology,
                                volume_rows, phase_rows, order_rows)
        manifest = _manifest(snapshot, started, event_log, controller_log, downstream)
        manifest["culture_loops"] = _culture_loops(topology, units, flows)
        if triggers:
            manifest["state_triggers"] = [trigger.summary() for trigger in triggers]
        manifest["hydraulics"] = flow_log
        manifest["findings"] = hydraulic_findings
        manifest["channels"] = _channel_units(series, controllers)
        manifest["diagnostics"]["roundoff_clips"] = roundoff_clips
        net_boundary = current[_slot(n):_slot(n, 3)]
        initial_inventory: np.ndarray = np.zeros(3, dtype=np.float64)
        final_inventory: np.ndarray = np.zeros(3, dtype=np.float64)
        for i, unit in enumerate(units):
            growth = growths[i]
            initial_inventory += unit["initial_volume_m3"] * np.asarray(
                [unit["state"][0], unit["state"][1] + growth.nitrogen_quota * unit["state"][0], unit["state"][2]])
            final_inventory += unit["volume_m3"] * np.asarray(
                [current[_slot(i)], current[_slot(i, 1)] + growth.nitrogen_quota * current[_slot(i)], current[_slot(i, 2)]])
        generated = np.asarray([
            sum(current[_slot(i, 3)] for i in range(n)),
            0.0,
            sum(growths[i].oxygen_yield * current[_slot(i, 3)] + current[_slot(i, 5)] for i in range(n)),
        ])
        impulses: np.ndarray = np.zeros(3, dtype=np.float64)
        for entry in event_log:
            if entry.get("impulse_inventory"):
                impulses += np.asarray(entry["impulse_inventory"], dtype=np.float64)
        residual = initial_inventory + net_boundary + generated + impulses - final_inventory
        action_run = any(entry.get("type") == "actions" for entry in event_log)
        if action_run:
            # Spec 185: a run with action events must close its aggregate balance before it can succeed.
            scale = np.abs(initial_inventory) + np.abs(net_boundary) + np.abs(generated) + np.abs(impulses)
            tolerance = max(1e-6, 100.0 * float(scenario["rtol"])) * scale + AGGREGATE_BALANCE_FLOOR_KG
            if np.any(np.abs(residual) > tolerance):
                raise DynamicError("BALANCE_NOT_CLOSED", "The run's aggregate inventory balance does not close.", {
                    name: {"residual_kg": float(residual[i]), "tolerance_kg": float(tolerance[i])}
                    for i, name in enumerate(("biomass", "total_nitrogen", "oxygen"))})
        manifest["balances"] = {
            "sign_conventions": {"oxygen_transfer": "positive means oxygen absorption into liquid"},
            "units": {
                unit["tag"]: {
                    "biomass_generation_kg": float(current[_slot(i, 3)]),
                    "nitrogen_total_generation_kg": 0.0,
                    "oxygen_transfer_kg": float(current[_slot(i, 5)]),
                }
                for i, unit in enumerate(units)
            },
            "aggregate": {name: {"residual_abs_kg": float(abs(residual[i]),),
                                  "residual_rel": float(abs(residual[i]) / max(1e-12, abs(initial_inventory[i]) + abs(net_boundary[i]) + abs(generated[i]) + abs(impulses[i])))}
                           for i, name in enumerate(("biomass", "total_nitrogen", "oxygen"))},
            "asserted": action_run,
        }
        if action_run:
            manifest["campaign"] = _campaign_kpis(event_log, units, topology, ledger, duration)
        harvest_kg = float(series["harvest_X_kg"][-1])
        volume_m3 = sum(unit["volume_m3"] for unit in units
                        if _unit_type(unit) == "PhotobioreactorT1")
        if volume_m3 <= 0:
            volume_m3 = sum(unit["volume_m3"] for unit in units)
        area_m2 = (
            sum(math.pi * unit["params"]["tube_inner_diameter"] * unit["params"]["tube_length"]
                * unit["params"]["tube_count"] for unit in units
                if _unit_type(unit) == "PhotobioreactorT1")
            if any(_unit_type(unit) == "PhotobioreactorT1" for unit in units)
            and all({"tube_inner_diameter", "tube_length", "tube_count"} <= unit["params"].keys()
                    for unit in units if _unit_type(unit) == "PhotobioreactorT1") else None
        )
        days = duration / 86400.0
        manifest["productivity"] = {
            "harvest_kg": harvest_kg,
            "volumetric_kg_m3_day": harvest_kg / volume_m3 / days,
            "areal_kg_m2_day": harvest_kg / area_m2 / days if area_m2 else None,
            "areal_unavailable_reason": None if area_m2 else "Illuminated tube geometry is unavailable.",
            "energy_per_kg": None,
            "energy_per_kg_unavailable_reason": "No dynamic pump or aeration energy input is declared.",
        }
        # The harvested-mass ledger is network-wide; attribute it to a loop only when it is the only one.
        manifest["productivity_loop"] = {
            loop["id"]: ({"harvest_kg": harvest_kg, "volumetric_kg_m3_day": harvest_kg / loop["volume_m3"] / days}
                         if len(manifest["culture_loops"]) == 1 else
                         {"harvest_kg": None, "volumetric_kg_m3_day": None,
                          "unavailable_reason": "Harvest is not attributed per loop when several loops participate."})
            for loop in manifest["culture_loops"] if loop["volume_m3"] > 0
        }
        return EngineResult("succeeded", None, series, manifest)
    except DynamicError as exc:
        return diagnostic("failed", {"code": exc.code, "message": exc.message, "detail": exc.detail})
    except Exception as exc:
        error = {"code": "DYNAMIC_FAILED", "message": str(exc)[:600], "detail": {}}
        return diagnostic("failed", error)


def _valid_state(state: tuple[float, ...] | np.ndarray, units: list[dict[str, Any]], growths: list[Any] | None = None) -> None:
    if not all(math.isfinite(float(v)) for v in state):
        raise DynamicError("STATE_NONFINITE", "CVODE produced a nonfinite state.")
    for i, item in enumerate(units):
        for name, value in zip(("X", "N", "O2"), state[_slot(i) : _slot(i, 3)], strict=True):
            tolerance = max(1e-9, 1e-6 * max(1.0, abs(float(value))))
            if value < -tolerance:
                raise DynamicError(
                    "STATE_NEGATIVE",
                    f"{name} concentration became materially negative.",
                    {"unit": item["tag"], "channel": name, "value": value},
                )
        growth = growths[i] if growths is not None else _growth_for_unit(
            item, dark=_unit_type(item) == "HoldupTank")
        if (_unit_type(item) == "PhotobioreactorT1"
                and growth.extinction * max(0.0, float(state[_slot(i)])) * growth.diameter > 1e6):
            raise DynamicError("OPTICS_TAU_MAX", "Optical depth exceeds the model limit.", {"unit": item["tag"]})


def _clip_tiny_negatives(
    state: np.ndarray, units: list[dict[str, Any]], clips: list[dict[str, Any]], time_s: float,
) -> None:
    for i, unit in enumerate(units):
        for offset, channel in enumerate(("X", "N", "O2")):
            index = _slot(i, offset)
            if state[index] < 0:
                clips.append({"time_s": time_s, "unit": unit["tag"], "channel": channel,
                              "value_before": float(state[index]), "value_after": 0.0})
                state[index] = 0.0


def _boundary_streams(
    topology: dict[str, Any], state: np.ndarray, units: list[dict[str, Any]],
    flows: dict[str, float], feeds: dict[str, list[float]],
) -> dict[str, dict[str, Any]]:
    for unit in units:
        if "growth" not in unit:
            unit["growth"] = _growth_for_unit(unit, dark=_unit_type(unit) == "HoldupTank")
    runtime = _network_runtime(topology, flows, units)
    concentrations = _network_concentrations(topology, runtime, tuple(state), units, flows)
    dynamic_ids = {item["id"] for item in topology["units"]}
    output = {}
    for stream in topology["streams"]:
        source, target = stream.get("source"), stream.get("target")
        if (source is None or target is None
                or (source.get("unit") in dynamic_ids and target.get("unit") not in dynamic_ids)):
            concentration = feeds.get(stream["id"]) if source is None else concentrations.get(
                f"stream:{stream['id']}"
            )
            output[stream["tag"]] = {
                "flow_m3_s": float(flows.get(stream["id"], 0.0)),
                "X_kg_m3": concentration[0] if concentration is not None else None,
                "N_kg_m3": concentration[1] if concentration is not None else None,
                "O2_kg_m3": concentration[2] if concentration is not None else None,
            }
    return output


def _apply_event(
    state: np.ndarray,
    indexed: tuple[int, dict[str, Any]],
    units: list[dict[str, Any]],
    log: list[dict[str, Any]],
    order: int,
) -> np.ndarray:
    _index, event = indexed
    tag = event.get("unit")
    found = next((i for i, unit in enumerate(units) if unit["tag"] == tag), None)
    if event["type"] in {"inoculation", "harvest"} and found is None:
        raise DynamicError("EVENT_TARGET_INVALID", f"Event target unit {tag!r} is not participating.")
    before = state.copy()
    if event["type"] == "inoculation":
        assert found is not None  # guarded by the EVENT_TARGET_INVALID check above
        value: Any = event.get("value")
        if isinstance(value, dict):
            for name, channel in (("X", 0), ("N", 1), ("O2", 2)):
                if name in value:
                    state[_slot(found, channel)] = float(value[name])
        else:
            state[_slot(found)] = float(value)
    elif event["type"] == "harvest":
        assert found is not None  # guarded by the EVENT_TARGET_INVALID check above
        fraction = float(event.get("fraction", 0.0))
        if not 0 <= fraction <= 1:
            raise DynamicError("EVENT_VALUE_INVALID", "Harvest fraction must be between 0 and 1.")
        state[_slot(found):_slot(found, 3)] *= 1.0 - fraction
        state[_slot(found, 6)] += before[_slot(found)] * fraction * units[found]["volume_m3"]
    elif event["type"] in {"feed_change", "dilution", "setpoint_change"}:
        # These actions affect flow/controller state; the current snapshot does not infer missing actuators.
        raise DynamicError(
            "EVENT_ACTUATOR_UNSUPPORTED", f"{event['type']} requires an explicitly bound dynamic actuator."
        )
    _valid_state(tuple(state), units)
    log.append(
        {
            "order": order,
            "time_s": event["time_s"],
            "type": event["type"],
            "target": tag,
            "pre_state": before.tolist(),
            "post_state": state.tolist(),
            "fraction": event.get("fraction"),
        }
    )
    return state


# Inventory vectors are (liquid volume m3, biomass kg, total nitrogen kg, oxygen kg); total N is dissolved plus quota.
_INVENTORY = ("volume_m3", "biomass_kg", "total_nitrogen_kg", "oxygen_kg")
EVENT_BALANCE_RTOL = 1e-9
EVENT_BALANCE_FLOOR = 1e-15
AGGREGATE_BALANCE_FLOOR_KG = 1e-12
CAMPAIGN_LAST_K = 3
PERIODIC_RTOL = 0.01
_EVENT_BALANCE_TEST_PERTURBATION = 0.0  # test hook: added to the post-event biomass inventory before the check


MAX_SEGMENT_RESTARTS = 10_000


def _make_observer(observed: str, units: list[dict[str, Any]], topology: dict[str, Any]) -> Callable[..., float]:
    """Value of a state-condition observable from a state vector, with unit volumes read live (spec 186).

    ``<unit>.X|N|O2`` is the unit's own concentration; ``<unit>.loop_X|loop_N`` is the inventory (volume-weighted)
    mean of the unit's culture loop, the same quantity the series reports as ``<loop>_X_mean`` / ``_N_mean``.
    """
    tag, _, channel = observed.rpartition(".")
    index = {unit["tag"]: i for i, unit in enumerate(units)}
    if channel in {"X", "N", "O2"}:
        slot = _slot(index[tag], {"X": 0, "N": 1, "O2": 2}[channel])
        return lambda state: float(state[slot])
    members = [index[member] for member in _tank_loops(topology, units)[tag]["member_tags"]]
    field = {"loop_X": 0, "loop_N": 1}[channel]

    def loop_mean(state: Any) -> float:
        volumes = [units[i]["volume_m3"] for i in members]
        return sum(float(state[_slot(i, field)]) * v for i, v in zip(members, volumes, strict=True)) / sum(volumes)

    return loop_mean


def _root_values(live: list[Trigger]) -> Callable[[float, tuple[float, ...]], list[float]]:
    return lambda _t, y: [trigger.root_value(y) for trigger in live]


def _runtime_volume_guard(event: dict[str, Any], units: list[dict[str, Any]], topology: dict[str, Any]) -> None:
    """Refuse an action that would leave a tank's [min_volume, max_volume] before it is applied (spec 186)."""
    tanks = {item["tag"]: item for item in units if _unit_type(item) == "HoldupTank"}
    _replay_event_volumes(event, tanks, _tank_loops(topology, units),
                          {item["tag"]: float(item["volume_m3"]) for item in units}, "EVENT_VOLUME_INVALID_RUNTIME")


def _campaign_ledger() -> dict[str, list[float]]:
    return {key: [0.0] * 4 for key in ("added", "removed", "harvested", "purged", "returned",
                                       "medium_nitrogen")}


def _tank_loops(topology: dict[str, Any], units: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Culture loop of each participating tank, by tag."""
    loops = topology.get("culture_loops") or []
    return {tag: loop for loop in loops for tag in loop["member_tags"]}


def _loop_mean_x(loop: dict[str, Any], units: list[dict[str, Any]], state: np.ndarray,
                 volumes: dict[str, float]) -> float:
    index = {unit["tag"]: i for i, unit in enumerate(units)}
    volume = sum(volumes[tag] for tag in loop["member_tags"])
    return sum(float(state[_slot(index[tag])]) * volumes[tag] for tag in loop["member_tags"]) / volume


def _apply_actions(
    state: np.ndarray,
    indexed: tuple[int, dict[str, Any]],
    units: list[dict[str, Any]],
    log: list[dict[str, Any]],
    order: int,
    topology: dict[str, Any],
    ledger: dict[str, list[float]],
) -> np.ndarray:
    """Execute one atomic ordered action list on tank inventories (spec 185) and assert its balance.

    Actions act on HoldupTank inventory only, never on a Splitter: flows and circulation are unchanged.
    """
    _index, event = indexed
    state = state.copy()
    index = {unit["tag"]: i for i, unit in enumerate(units)}
    loops = _tank_loops(topology, units)
    pre_state = state.copy()
    pre_volumes = {unit["tag"]: unit["volume_m3"] for unit in units}

    def mix_in(i: int, volume: float, x: float, nitrogen: float, oxygen: float, extra_nitrogen_kg: float = 0.0) -> None:
        v = units[i]["volume_m3"]
        total = v + volume
        old = state[_slot(i):_slot(i, 3)].copy()
        state[_slot(i)] = (old[0] * v + x * volume) / total
        state[_slot(i, 1)] = (old[1] * v + nitrogen * volume + extra_nitrogen_kg) / total
        state[_slot(i, 2)] = (old[2] * v + oxygen * volume) / total
        units[i]["volume_m3"] = total

    deltas = {key: np.zeros(4) for key in ("added", "removed", "harvested", "purged", "returned")}
    medium_nitrogen = 0.0
    parcel: dict[str, Any] | None = None  # culture drawn in this event and not yet separated
    applied: list[dict[str, Any]] = []
    for action in event["actions"]:
        kind = action["type"]
        tank = action.get("tank")
        record: dict[str, Any] = {"type": kind, "tank": tank,
                                  "loop": loops.get(tank, {}).get("id") if tank else None}
        if kind == "draw":
            i = index[tank]
            if action.get("loop_fraction") is not None:
                loop = loops[tank]
                volume = float(action["loop_fraction"]) * sum(units[index[t]]["volume_m3"] for t in loop["member_tags"])
            else:
                volume = float(action["volume_m3"])
            if volume >= units[i]["volume_m3"]:
                raise DynamicError("EVENT_VOLUME_INVALID", "A draw would empty its tank.", {"tank": tank})
            x, nitrogen, oxygen = (float(value) for value in state[_slot(i):_slot(i, 3)])
            quota = units[i]["growth"].nitrogen_quota
            drawn = np.asarray([volume, volume * x, volume * (nitrogen + quota * x), volume * oxygen])
            units[i]["volume_m3"] -= volume
            deltas["removed"] += drawn
            if parcel is None:
                parcel = {"inventory": np.zeros(4), "dissolved_n_kg": 0.0, "quota": quota, "loop": record["loop"]}
            parcel["inventory"] += drawn
            parcel["dissolved_n_kg"] += volume * nitrogen
            record["volume_m3"] = volume
        elif kind == "separate":
            assert parcel is not None  # prepare() requires a preceding draw in the same event
            whole = parcel["inventory"]
            v_draw, m_x = whole[0], whole[1]
            recovery, factor = float(action["recovery"]), float(action["concentration_factor"])
            v_conc = v_draw * recovery / factor
            # Dissolved species leave in both products at the draw concentration.
            dissolved_n_c, oxygen_c = parcel["dissolved_n_kg"] / v_draw, whole[3] / v_draw
            m_x_conc = recovery * m_x
            concentrate = np.asarray([v_conc, m_x_conc,
                                      v_conc * dissolved_n_c + parcel["quota"] * m_x_conc, v_conc * oxygen_c])
            clarified = whole - concentrate
            deltas["harvested"] += concentrate
            target = action["return_to"]
            if target == "none":
                deltas["purged"] += clarified
            else:
                j = index[target]
                v_cl = clarified[0]
                x_cl = clarified[1] / v_cl if v_cl > 0 else 0.0
                quota_j = units[j]["growth"].nitrogen_quota
                n_cl = (clarified[2] - quota_j * clarified[1]) / v_cl if v_cl > 0 else 0.0
                mix_in(j, v_cl, x_cl, n_cl, clarified[3] / v_cl if v_cl > 0 else 0.0)
                deltas["returned"] += clarified
            record.update(concentrate_volume_m3=float(v_conc), concentrate_biomass_kg=float(m_x_conc),
                          clarified_to=target)
            parcel = None
        elif kind == "refill":
            i = index[tank]
            volume = (float(action["volume_m3"]) if action.get("volume_m3") is not None
                      else float(action["to_volume_m3"]) - units[i]["volume_m3"])
            if volume < 0:
                raise DynamicError("EVENT_VOLUME_INVALID", "Refill target is below the tank's current volume.",
                                   {"tank": tank})
            medium = action["medium"]
            if volume > 0:
                mix_in(i, volume, 0.0, float(medium["N"]), float(medium["O2"]))
            added = np.asarray([volume, 0.0, volume * float(medium["N"]), volume * float(medium["O2"])])
            deltas["added"] += added
            medium_nitrogen += added[2]
            record["volume_m3"] = volume
        elif kind == "dose":
            i = index[tank]
            volume, nitrogen_kg = float(action.get("volume_m3", 0.0)), float(action["nitrogen_kg"])
            mix_in(i, volume, 0.0, 0.0, 0.0, extra_nitrogen_kg=nitrogen_kg)
            deltas["added"] += np.asarray([volume, 0.0, nitrogen_kg, 0.0])
            medium_nitrogen += nitrogen_kg
            record.update(volume_m3=volume, nitrogen_kg=nitrogen_kg)
        elif kind == "inoculate":
            i = index[tank]
            volume, culture = float(action["volume_m3"]), action["culture"]
            x, nitrogen, oxygen = (float(culture[key]) for key in ("X", "N", "O2"))
            mix_in(i, volume, x, nitrogen, oxygen)
            quota = units[i]["growth"].nitrogen_quota
            deltas["added"] += np.asarray([volume, volume * x, volume * (nitrogen + quota * x), volume * oxygen])
            record["volume_m3"] = volume
        applied.append(record)
    if parcel is not None:  # without `separate`, the whole draw is harvested product
        deltas["harvested"] += parcel["inventory"]
    post_volumes = {unit["tag"]: unit["volume_m3"] for unit in units}
    pre = sum((_inventory_of(pre_state, i, unit, pre_volumes) for i, unit in enumerate(units)), np.zeros(4))
    post = sum((_inventory_of(state, i, unit, post_volumes) for i, unit in enumerate(units)), np.zeros(4))
    post = post + np.asarray([0.0, _EVENT_BALANCE_TEST_PERTURBATION, 0.0, 0.0])
    closure = post - (pre + deltas["added"] - deltas["harvested"] - deltas["purged"])
    split = deltas["removed"] - (deltas["harvested"] + deltas["purged"] + deltas["returned"])
    scale = np.maximum.reduce([np.abs(pre), np.abs(post), np.abs(deltas["added"]), np.abs(deltas["removed"])])
    tolerance = EVENT_BALANCE_RTOL * scale + EVENT_BALANCE_FLOOR
    if np.any(np.abs(closure) > tolerance) or np.any(np.abs(split) > tolerance):
        raise DynamicError("EVENT_BALANCE_NOT_CLOSED", "An action event does not conserve its inventories.", {
            "time_s": event["time_s"], "order": order,
            "closure": dict(zip(_INVENTORY, closure.tolist(), strict=True)),
            "draw_split": dict(zip(_INVENTORY, split.tolist(), strict=True))})
    _valid_state(tuple(state), units)
    for key in ("added", "removed", "harvested", "purged", "returned"):
        ledger[key] = (np.asarray(ledger[key]) + deltas[key]).tolist()
    ledger["medium_nitrogen"][2] += medium_nitrogen
    # The harvested product joins the network-wide harvested-biomass ledger slot.
    state[_slot(len(units), 3)] += deltas["harvested"][1]
    touched = sorted({record["loop"] for record in applied if record.get("loop")})
    loop_by_id = {loop["id"]: loop for loop in topology.get("culture_loops") or []}
    log.append({
        "order": order, "declared_order": event.get("declared_order", order), "repeat_k": event.get("repeat_k", 0),
        "time_s": event["time_s"], "type": "actions", "semantics": "conservative_inventory_actions",
        "actions": applied, "pre_state": pre_state.tolist(), "post_state": state.tolist(),
        "pre_volumes_m3": pre_volumes, "post_volumes_m3": post_volumes,
        "inventory_deltas": {key: dict(zip(_INVENTORY, value.tolist(), strict=True)) for key, value in deltas.items()},
        "medium_nitrogen_kg": medium_nitrogen,
        "balance": {"closure": dict(zip(_INVENTORY, closure.tolist(), strict=True)), "closed": True},
        "impulse_inventory": (deltas["added"] - deltas["harvested"] - deltas["purged"])[1:].tolist(),
        "loops": {loop_id: {
            "x_mean_pre_kg_m3": _loop_mean_x(loop_by_id[loop_id], units, pre_state, pre_volumes),
            "x_mean_post_kg_m3": _loop_mean_x(loop_by_id[loop_id], units, state, post_volumes),
            "volume_pre_m3": sum(pre_volumes[tag] for tag in loop_by_id[loop_id]["member_tags"]),
            "volume_post_m3": sum(post_volumes[tag] for tag in loop_by_id[loop_id]["member_tags"]),
            "draw": any(record["type"] == "draw" and record["loop"] == loop_id for record in applied),
        } for loop_id in touched},
    })
    return state


def _campaign_kpis(event_log: list[dict[str, Any]], units: list[dict[str, Any]], topology: dict[str, Any],
                   ledger: dict[str, list[float]], duration_s: float) -> dict[str, Any]:
    """Per-loop cycles between draws and a campaign summary (spec 185).

    A cycle ends at a draw on its loop; the draw's harvest and purge close it, and additions made by the same
    event (refill, dose, inoculate) open the next cycle. Cycle 0 runs from the start to the first draw.
    """
    loops = {loop["id"]: loop for loop in topology.get("culture_loops") or []}
    by_tag = {unit["tag"]: unit for unit in units}
    result: dict[str, Any] = {}
    for loop_id, loop in loops.items():
        pbrs = [by_tag[tag] for tag in loop["member_tags"] if _unit_type(by_tag[tag]) == "PhotobioreactorT1"]
        area = sum(math.pi * u["params"]["tube_inner_diameter"] * u["params"]["tube_length"] * u["params"]["tube_count"]
                   for u in pbrs) if pbrs and all({"tube_inner_diameter", "tube_length", "tube_count"}
                                                   <= u["params"].keys() for u in pbrs) else None
        cycles: list[dict[str, Any]] = []
        open_cycle = {"start_s": 0.0, "medium_volume_m3": 0.0, "nitrogen_added_kg": 0.0}
        for entry in event_log:
            if entry.get("type") != "actions" or loop_id not in entry.get("loops", {}):
                continue
            info = entry["loops"][loop_id]
            loop_records = [record for record in entry["actions"] if record.get("loop") == loop_id]
            added_volume = sum(record.get("volume_m3", 0.0) for record in loop_records
                               if record["type"] in {"refill", "dose", "inoculate"})
            if info["draw"]:
                deltas = entry["inventory_deltas"]
                harvested, purged = deltas["harvested"], deltas["purged"]
                length = float(entry["time_s"]) - open_cycle["start_s"]
                days = length / 86400.0
                cycles.append({
                    "index": len(cycles), "startup": not cycles, "start_s": open_cycle["start_s"],
                    "end_s": float(entry["time_s"]), "length_s": length,
                    "harvested_biomass_kg": harvested["biomass_kg"], "harvested_volume_m3": harvested["volume_m3"],
                    "concentrate_biomass_kg_m3": (harvested["biomass_kg"] / harvested["volume_m3"]
                                                  if harvested["volume_m3"] > 0 else None),
                    "purge_volume_m3": purged["volume_m3"], "purge_biomass_kg": purged["biomass_kg"],
                    "medium_volume_m3": open_cycle["medium_volume_m3"],
                    "nitrogen_added_kg": open_cycle["nitrogen_added_kg"],
                    "x_mean_before_draw_kg_m3": info["x_mean_pre_kg_m3"],
                    "x_mean_after_event_kg_m3": info["x_mean_post_kg_m3"],
                    "loop_volume_before_draw_m3": info["volume_pre_m3"],
                    "net_volumetric_productivity_kg_m3_day": (harvested["biomass_kg"] / info["volume_pre_m3"] / days
                                                              if days > 0 else None),
                    "areal_productivity_kg_m2_day": (harvested["biomass_kg"] / area / days
                                                     if area and days > 0 else None),
                })
                open_cycle = {"start_s": float(entry["time_s"]), "medium_volume_m3": added_volume,
                              "nitrogen_added_kg": entry["medium_nitrogen_kg"]}
            else:
                open_cycle["medium_volume_m3"] += added_volume
                open_cycle["nitrogen_added_kg"] += entry["medium_nitrogen_kg"]
        if not cycles:
            continue
        regular = [cycle for cycle in cycles if not cycle["startup"]]
        last = regular[-CAMPAIGN_LAST_K:]
        pre_x = [cycle["x_mean_before_draw_kg_m3"] for cycle in cycles][-3:]
        changes = [abs(b - a) / a for a, b in zip(pre_x, pre_x[1:], strict=False) if a > 0]
        if len(pre_x) < 3:
            periodic = "insufficient cycles"
        else:
            periodic = "periodic campaign reached" if max(changes) < PERIODIC_RTOL else "not yet periodic"
        result[loop_id] = {
            "cycles": cycles,
            "summary": {
                "draws": len(cycles),
                "harvested_biomass_kg": sum(cycle["harvested_biomass_kg"] for cycle in cycles),
                "harvested_volume_m3": sum(cycle["harvested_volume_m3"] for cycle in cycles),
                "purge_volume_m3": sum(cycle["purge_volume_m3"] for cycle in cycles),
                "last_k": len(last),
                "mean_last_k_net_volumetric_productivity_kg_m3_day": (
                    sum(cycle["net_volumetric_productivity_kg_m3_day"] for cycle in last) / len(last) if last else None),
                "mean_last_k_harvested_biomass_kg": (
                    sum(cycle["harvested_biomass_kg"] for cycle in last) / len(last) if last else None),
                "pre_draw_x_relative_changes": changes,
                "periodicity": periodic,
            },
        }
    return {"loops": result, "duration_s": duration_s,
            "ledger": {key: dict(zip(_INVENTORY, value, strict=True)) for key, value in ledger.items()
                       if key != "medium_nitrogen"},
            "medium_nitrogen_added_kg": ledger["medium_nitrogen"][2]}


def _inventory_of(state: np.ndarray, i: int, unit: dict[str, Any], volumes: dict[str, float]) -> np.ndarray:
    v = volumes[unit["tag"]]
    x, nitrogen, oxygen = (float(value) for value in state[_slot(i):_slot(i, 3)])
    return np.asarray([v, v * x, v * (nitrogen + unit["growth"].nitrogen_quota * x), v * oxygen])


def _apply_dynamic_event(
    state: np.ndarray,
    indexed: tuple[int, dict[str, Any]],
    units: list[dict[str, Any]],
    log: list[dict[str, Any]],
    order: int,
    topology: dict[str, Any],
    flows: dict[str, float],
    feeds: dict[str, list[float]],
    setpoints: dict[str, float],
    controllers: dict[str, dict[str, Any]],
) -> np.ndarray:
    index, event = indexed
    if event.get("observed"):
        return state
    if event["type"] in {"inoculation", "harvest"}:
        before = state.copy()
        state = _apply_event(state, indexed, units, [], order)
        unit_index = next(i for i, unit in enumerate(units) if unit["tag"] == event["unit"])
        growth = _growth_for_unit(units[unit_index], dark=_unit_type(units[unit_index]) == "HoldupTank")
        volume = units[unit_index]["volume_m3"]
        delta = state[_slot(unit_index):_slot(unit_index, 3)] - before[_slot(unit_index):_slot(unit_index, 3)]
        impulse = [delta[0] * volume, (delta[1] + growth.nitrogen_quota * delta[0]) * volume, delta[2] * volume]
        entry = {"order": order, "declared_order": event.get("declared_order", order),
                 "repeat_k": event.get("repeat_k", 0), "time_s": event["time_s"],
                 "type": event["type"], "target": event.get("unit"),
                 "semantics": "implicit_blank_refill" if event["type"] == "harvest" else "overwrite",
                 "pre_state": before.tolist(), "post_state": state.tolist(), "impulse_inventory": impulse}
        log.append(entry)
        return state
    kind, target = (event.get("target") or "").split(":", 1) if ":" in (event.get("target") or "") else ("", "")
    flows_before, feeds_before, setpoints_before = dict(flows), {k: list(v) for k, v in feeds.items()}, dict(setpoints)
    if event["type"] in {"feed_change", "dilution"}:
        stream_tag = event.get("stream") or (target if kind == "feed" else None)
        value: Any = event.get("value")
        if kind == "splitter" or (event["type"] == "dilution" and event.get("target", "").startswith("splitter:")):
            splitter_tag = target or event["target"].split(":", 1)[1]
            splitter = next((u for u in topology["units"] if u.get("tag") == splitter_tag and u.get("type") == "Splitter"), None)
            if splitter is None:
                raise DynamicError("EVENT_TARGET_INVALID", "Dilution splitter is not participating.", {"target": splitter_tag})
            outgoing = [s for s in topology["streams"] if (s.get("source") or {}).get("unit") == splitter["id"]]
            total = sum(flows[s["id"]] for s in outgoing)
            ratio = float(value)
            if not 0 <= ratio <= 1 or len(outgoing) < 2:
                raise DynamicError("EVENT_VALUE_INVALID", "Splitter ratio must be in [0,1] and have at least two outlets.")
            flows[outgoing[0]["id"]] = total * ratio
            for outlet in outgoing[1:]:
                flows[outlet["id"]] = total * (1 - ratio) / (len(outgoing) - 1)
            _rebalance_flows(topology, flows)
        else:
            stream = next((item for item in topology["streams"] if item["tag"] == stream_tag), None)
            if stream is None or stream.get("source") is not None:
                raise DynamicError("EVENT_TARGET_INVALID", "Feed event must name a boundary feed stream.", {"stream": stream_tag})
            if isinstance(value, dict):
                flows[stream["id"]] = float(value.get("flow_m3_s", flows[stream["id"]]))
                if "culture" in value:
                    feeds[stream["id"]] = [float(value["culture"].get(k, 0.0)) for k in ("X", "N", "O2")]
            elif value is not None:
                flows[stream["id"]] = float(value)
            if flows[stream["id"]] < 0 or not math.isfinite(flows[stream["id"]]):
                raise DynamicError("EVENT_VALUE_INVALID", "Feed flow must be finite and nonnegative.")
            _rebalance_flows(topology, flows)
    elif event["type"] == "setpoint_change":
        controller_id = event.get("target")
        if controller_id not in controllers:
            raise DynamicError("EVENT_TARGET_INVALID", "Setpoint event must target a participating controller.", {"target": controller_id})
        setpoints[controller_id] = float(event["value"])
    # Before/after values of what the event changed (spec 185 §4).
    changed: dict[str, Any]
    if event["type"] == "setpoint_change":
        controller_id = str(event["target"])
        changed = {"before": {"setpoint": setpoints_before.get(controller_id)},
                   "after": {"setpoint": setpoints.get(controller_id)}}
    else:
        tags = {stream["id"]: stream["tag"] for stream in topology["streams"]}
        moved = sorted(key for key in flows if flows[key] != flows_before.get(key))
        recultured = sorted(key for key in feeds if feeds[key] != feeds_before.get(key))
        changed = {"before": {"flows_m3_s": {tags.get(key, key): flows_before.get(key) for key in moved},
                              "feed_culture": {tags.get(key, key): feeds_before.get(key) for key in recultured}},
                   "after": {"flows_m3_s": {tags.get(key, key): flows[key] for key in moved},
                             "feed_culture": {tags.get(key, key): feeds[key] for key in recultured}}}
    log.append({"order": order, "declared_order": event.get("declared_order", order),
                "repeat_k": event.get("repeat_k", 0), "time_s": event["time_s"],
                "type": event["type"], "target": event.get("target"),
                "value": event.get("value"), **changed})
    return state


def _sample_controllers(
    state: np.ndarray, units: list[dict[str, Any]], controllers: list[dict[str, Any]], topology: dict[str, Any],
    flows: dict[str, float], setpoints: dict[str, float], time_s: float, log: list[dict[str, Any]],
) -> None:
    for controller in controllers:
        if abs(time_s % float(controller["cadence_s"])) >= 1e-7:
            continue
        unit_index = next(i for i, unit in enumerate(units) if unit["tag"] == controller["unit"])
        channel = controller["measurement"].split(".")[-1]
        if channel not in {"X", "N", "O2"}:
            raise DynamicError("UNSUPPORTED_MEASUREMENT", "Only X, N, and O2 can be measured.")
        measurement = float(state[_slot(unit_index, {"X": 0, "N": 1, "O2": 2}[channel])])
        error = setpoints[controller["id"]] - measurement
        if controller["type"] == "onoff":
            if controller["direction"] == "above":
                active = measurement <= setpoints[controller["id"]] + controller["hysteresis"] if controller["_active"] else measurement < setpoints[controller["id"]]
            else:
                active = measurement >= setpoints[controller["id"]] - controller["hysteresis"] if controller["_active"] else measurement > setpoints[controller["id"]]
            output = controller["upper"] if active else controller["lower"]
            controller["_active"] = active
        elif controller["type"] == "pi":
            dt = float(controller["cadence_s"])
            raw = controller["_bias"] + controller["kp"] * error + controller["ki"] * controller["_integral"]
            output = min(controller["upper"], max(controller["lower"], raw))
            if output == raw or (output >= controller["upper"] and error < 0) or (output <= controller["lower"] and error > 0):
                controller["_integral"] += error * dt
        else:
            harvest_fraction = max(0.0, 1.0 - setpoints[controller["id"]] / max(measurement, 1e-12))
            output = min(controller["upper"], max(controller["lower"], harvest_fraction))
        controller["output"] = float(output)
        actuator = controller["actuator"]
        kind, tag = actuator.split(":", 1)
        if kind == "feed":
            stream = next((s for s in topology["streams"] if s["tag"] == tag), None)
            if stream is None or stream.get("source") is not None:
                raise DynamicError("UNSUPPORTED_ACTUATOR", "Feed actuator must target a boundary feed.")
            flows[stream["id"]] = float(output)
            _rebalance_flows(topology, flows)
        elif kind == "splitter":
            splitter = next((u for u in topology["units"] if u.get("tag") == tag and u.get("type") == "Splitter"), None)
            if splitter is None:
                raise DynamicError("UNSUPPORTED_ACTUATOR", "Splitter actuator must target a participating splitter.")
            outlets = [s for s in topology["streams"] if (s.get("source") or {}).get("unit") == splitter["id"]]
            total = sum(flows[s["id"]] for s in outlets)
            flows[outlets[0]["id"]] = total * output
            for stream in outlets[1:]:
                flows[stream["id"]] = total * (1 - output) / (len(outlets) - 1)
            _rebalance_flows(topology, flows)
        log.append({"controller": controller["id"], "time_s": time_s, "measurement": measurement, "output": float(output)})


def _conditional_events(
    state: np.ndarray, units: list[dict[str, Any]], events: list[tuple[int, dict[str, Any]]],
    active: dict[int, bool], log: list[dict[str, Any]], time_s: float, topology: dict[str, Any],
    flows: dict[str, float], feeds: dict[str, list[float]], setpoints: dict[str, float],
    controllers: dict[str, dict[str, Any]],
) -> None:
    for order, event in events:
        if not event.get("observed") or float(event["time_s"]) > time_s:
            continue
        unit_tag, _, channel = event["observed"].partition(".")
        unit_index = next((i for i, unit in enumerate(units) if unit["tag"] == unit_tag), None)
        if unit_index is None or channel not in {"X", "N", "O2"}:
            continue
        value = float(state[_slot(unit_index, {"X": 0, "N": 1, "O2": 2}[channel])])
        threshold, band = float(event["threshold"]), float(event.get("hysteresis") or 0)
        is_active = value >= threshold if event.get("direction") == "above" else value <= threshold
        was_active = active.get(order, False)
        if is_active and not was_active:
            log.append({"order": order, "declared_order": event.get("declared_order", order),
                        "repeat_k": event.get("repeat_k", 0), "time_s": time_s,
                        "type": event["type"], "conditional": True,
                        "observed": event["observed"], "measurement": value, "threshold": threshold,
                        "hysteresis": band, "direction": event["direction"]})
            action = dict(event, time_s=time_s)
            action.pop("observed", None)
            action.pop("threshold", None)
            action.pop("direction", None)
            action.pop("hysteresis", None)
            if action["type"] in {"inoculation", "harvest"}:
                state = _apply_dynamic_event(state, (order, action), units, log, order,
                                             topology, flows, feeds, setpoints, controllers)
            else:
                state = _apply_dynamic_event(state, (order, action), units, log, order,
                                             topology, flows, feeds, setpoints, controllers)
        if event.get("direction") == "above":
            if not was_active and value >= threshold:
                active[order] = True
            elif was_active and value <= threshold - band:
                active[order] = False
        else:
            if not was_active and value <= threshold:
                active[order] = True
            elif was_active and value >= threshold + band:
                active[order] = False


def _rebalance_flows(topology: dict[str, Any], flows: dict[str, float]) -> None:
    """Re-solve network continuity after a boundary feed actuator changes Q."""
    streams, units = topology["streams"], topology["units"]
    if not units:
        return
    index = {stream["id"]: i for i, stream in enumerate(streams)}
    rows: list[np.ndarray] = []
    rhs: list[float] = []
    feed_rows: dict[int, str] = {}
    for stream in streams:
        if stream.get("source") is None:
            row = np.zeros(len(streams))
            row[index[stream["id"]]] = 1.0
            rows.append(row)
            rhs.append(float(flows[stream["id"]]))
            feed_rows[len(rhs) - 1] = stream["id"]
    for unit in units:
        key = unit["id"]
        incoming = [s for s in streams if (s.get("target") or {}).get("unit") == key]
        outgoing = [s for s in streams if (s.get("source") or {}).get("unit") == key]
        outgoing.sort(key=lambda stream: (stream.get("source") or {}).get("port", 0))
        if not incoming or not outgoing:
            continue
        row = np.zeros(len(streams))
        for stream in incoming:
            row[index[stream["id"]]] += 1.0
        for stream in outgoing:
            row[index[stream["id"]]] -= 1.0
        rows.append(row)
        rhs.append(0.0)
        if unit.get("type") == "Splitter" and unit.get("tag") not in topology.get("implied_splitters", []):
            total = sum(flows[s["id"]] for s in outgoing)
            if total <= 0:
                raise DynamicError("ZERO_THROUGHFLOW", "Splitter has zero flow after an actuator change.", {"unit": unit.get("tag")})
            ratios = [flows[s["id"]] / total for s in outgoing]
            for stream, ratio in zip(outgoing, ratios, strict=True):
                split_row = np.zeros(len(streams))
                split_row[index[stream["id"]]] = 1.0
                for inlet in incoming:
                    split_row[index[inlet["id"]]] -= ratio
                rows.append(split_row)
                rhs.append(0.0)
        elif unit.get("type") == "SpecifiedSeparator":
            if len(incoming) != 1 or len(outgoing) != 2:
                raise DynamicError("SEPARATOR_TOPOLOGY_INVALID", "Separator needs one inlet and two outlets.")
            params = unit.get("params", {})
            recovery = float((params.get("biomass_recovery") or {}).get("si", 90.0)) / 100.0
            factor = float((params.get("concentration_factor") or {}).get("si", 10.0))
            for stream, ratio in zip(outgoing, (recovery / factor, 1.0 - recovery / factor), strict=True):
                sep_row = np.zeros(len(streams))
                sep_row[index[stream["id"]]] = 1.0
                sep_row[index[incoming[0]["id"]]] = -ratio
                rows.append(sep_row)
                rhs.append(0.0)
    circulation_rows = {}
    for _pump_tag, pump in topology.get("circulation", {}).items():
        row = np.zeros(len(streams))
        row[index[pump["stream_id"]]] = 1.0
        rows.append(row)
        rhs.append(float(pump["flow_m3_s"]))
        circulation_rows[len(rhs) - 1] = pump
    solve_topology = dict(topology, flow_matrix=np.vstack(rows).tolist(), flow_rhs=rhs,
                          feed_rows=feed_rows, circulation_rows=circulation_rows)
    solved = _solve_flows(solve_topology)
    flows.update(solved)


def _manifest(
    snapshot: Snapshot,
    started: float,
    events: list[dict[str, Any]],
    controllers: list[dict[str, Any]],
    downstream: list[dict[str, Any]],
) -> dict[str, Any]:
    p = snapshot.payload
    result = {
        "fidelity": "T1",
        "evaluator_version": EVALUATOR_VERSION,
        "snapshot_digest": snapshot.digest,
        "draft_id": p["draft_id"],
        "draft_revision": p["revision"],
        "content_digest": p["content_digest"],
        "draft_content_digest": p["content_digest"],
        "scenario_id": p["scenario_id"],
        "profiles": [{
            "profile_id": item["ref"]["profile_id"], "digest": item["ref"]["digest"],
            "resolution_minutes": item["profile"].get("resolution_minutes"),
            "consumed_channels": [name for name in (item.get("par_name"), item.get("temp_name")) if name],
            "temporal_rule": {
                "par": "interval-end held over preceding interval; no interpolation",
                "temperature": "linear between UTC point samples; no extrapolation",
            },
        } for item in p["profiles"]],
        "profile_refs": [item["ref"] for item in p["profiles"]],
        "forcing_profile_id": p["profiles"][0]["ref"]["profile_id"],
        "units": [{
            "tag": u["tag"], "model_card_qualification": u["card_qualification"],
            "card_id": u["model"].get("card", {}).get("id"),
            "card_revision": u["model"].get("card", {}).get("revision"),
            "card_digest": u["model"].get("card", {}).get("digest"),
            "parameter_set_digest": u["model"].get("set", {}).get("digest"),
        } for u in p["units"]],
        "solver": {
            "method": p["scenario"]["solver_method"],
            "rtol": p["scenario"]["rtol"],
            "atol": p["scenario"]["atol"],
            "owner": "scikit-sundae CVODE",
        },
        "rate_time_convention": "170 hourly rates divided by 3600 exactly once for SI-second integration",
        "temporal_rules": {
            "par": "interval-end held over preceding interval; no interpolation",
            "temperature": "linear between UTC point samples; no extrapolation",
        },
        "event_log": events,
        "controller_log": controllers,
        "diagnostics": {"wall_time_s": time.perf_counter() - started},
        "downstream_outcomes": downstream,
    }
    if any("optics_t2" in item for item in p["units"]):
        result["fidelity"] = "T2"
        result["profiles"][0]["consumed_channels"].extend(["dni", "dhi"])
        for source, unit in zip(p["units"], result["units"], strict=True):
            if "optics_t2" in source:
                unit["optics"] = source["optics_t2"]["provenance"]
                unit["fidelity"] = "T2 - unqualified - profile-driven tube/array interception, Fresnel wall, opaque shading, no energy balance"
                unit["optics_caveats"] = [
                    "Representative cross-section module: edge effects are represented only at module scale.",
                    "Opaque upstream tubes give a lower bound; transmitted light is not traced into downstream tubes.",
                    "No wall absorption, internal re-reflection, sea glint, anisotropic sky or energy balance.",
                ]
    return result


def _add_diagnostic_evidence(
    manifest: dict[str, Any], series: dict[str, np.ndarray], controllers: list[dict[str, Any]],
) -> None:
    manifest["channels"] = _channel_units(series, controllers)


def _channel_units(series: dict[str, np.ndarray], controllers: list[dict[str, Any]]) -> dict[str, str]:
    controller_by_id = {item["id"]: item for item in controllers}
    channels: dict[str, str] = {}
    for name in series:
        if name == "t_s":
            unit = "s"
        elif name == "par":
            unit = "umol/(m2*s)"
        elif name.endswith("_par_eq_umol_m2_s"):
            unit = "umol/(m2*s)"
        elif name.endswith(("_par_intercepted_umol_s_m", "_par_absorbed_umol_s_m")):
            unit = "umol/(s*m)"
        elif name.endswith("_beam_unshaded_fraction"):
            unit = "1"
        elif name == "temperature" or name.endswith("_temperature"):
            unit = "K"
        elif name == "harvest_X_kg" or name.endswith("_harvest_X_kg"):
            unit = "kg"
        elif name.endswith("_biomass_kg"):
            unit = "kg"
        elif name.endswith(("_V_m3", "_volume_m3")):
            unit = "m3"
        elif name in {"event_phase", "event_order"}:
            unit = "1"
        elif name.endswith("_Q_m3_s"):
            unit = "m3/s"
        elif name.endswith("_pass_rate_1_s"):
            unit = "1/s"
        elif name.endswith("_pass_transit_time_s") or name.endswith("_culture_residence_time_s"):
            unit = "s"
        elif name.endswith("_circulation_Q_m3_s"):
            unit = "m3/s"
        elif name.endswith("_net_dilution_1_s"):
            unit = "1/s"
        elif name.endswith("_velocity_m_s"):
            unit = "m/s"
        elif name.endswith("_reynolds_number"):
            unit = "1"
        elif name.endswith("_pressure_drop"):
            unit = "Pa"
        elif name.endswith("_pumping_power"):
            unit = "W"
        elif name.endswith(("_X_mean", "_N_mean")):
            unit = "kg/m3"
        elif name.endswith("_ratio"):
            unit = "1"
        elif name.startswith("controller_"):
            controller_id = name.removeprefix("controller_")
            controller = controller_by_id.get(controller_id, {})
            unit = "m3/s" if controller.get("actuator", "").startswith("feed:") else "1"
        elif name.endswith(("_X", "_N", "_O2")):
            unit = "kg/m3"
        else:
            unit = "unknown"
        channels[name] = unit
    return channels


def _partial_balances(
    state: np.ndarray, units: list[dict[str, Any]], growths: list[Any], events: list[dict[str, Any]],
) -> dict[str, Any]:
    n = len(units)
    boundary = state[_slot(n):_slot(n, 3)]
    initial: np.ndarray = np.zeros(3, dtype=np.float64)
    final: np.ndarray = np.zeros(3, dtype=np.float64)
    for index, unit in enumerate(units):
        quota = growths[index].nitrogen_quota
        initial += unit.get("initial_volume_m3", unit["volume_m3"]) * np.asarray([
            unit["state"][0], unit["state"][1] + quota * unit["state"][0], unit["state"][2],
        ])
        final += unit["volume_m3"] * np.asarray([
            state[_slot(index)], state[_slot(index, 1)] + quota * state[_slot(index)], state[_slot(index, 2)],
        ])
    generated = np.asarray([
        sum(state[_slot(index, 3)] for index in range(n)),
        0.0,
        sum(growths[index].oxygen_yield * state[_slot(index, 3)] + state[_slot(index, 5)] for index in range(n)),
    ])
    impulses: np.ndarray = np.zeros(3, dtype=np.float64)
    for event in events:
        if event.get("impulse_inventory"):
            impulses += np.asarray(event["impulse_inventory"], dtype=np.float64)
    residual = initial + boundary + generated + impulses - final
    names = ("biomass", "total_nitrogen", "oxygen")
    return {
        "status": "partial", "boundary_net_kg": boundary.tolist(),
        "generated_kg": generated.tolist(), "impulse_inventory_kg": impulses.tolist(),
        "aggregate": {name: {
            "residual_abs_kg": float(abs(residual[index])),
            "residual_rel": float(abs(residual[index]) / max(
                1e-12, abs(initial[index]) + abs(boundary[index]) + abs(generated[index]) + abs(impulses[index]),
            )),
        } for index, name in enumerate(names)},
    }
