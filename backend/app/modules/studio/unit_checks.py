"""Host-side checks of a Studio process unit's result (spec 190); one function serves Test and the Process solve."""

from dataclasses import dataclass, field
from math import isfinite
from typing import Any

CULTURE_FIELDS = ("biomass", "nitrogen", "phosphorus", "oxygen", "dic", "salinity")
GENERATION_UNITS = {name: "mol/s" if name == "dic" else "kg/s" for name in CULTURE_FIELDS}
TOLERANCE = 1e-9
_STREAM_KEYS = {"mass_flow_kg_s", "temperature_K", "pressure_Pa", "mass_fractions", "culture"}
HONEST_LIMITS = ("Steady only; no energy balance is checked; outlet temperature and pressure are set by the model; "
                 "unqualified")


class UnitCheckError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class CheckedResult:
    outlets: dict[str, dict[str, Any]]
    culture_generation: dict[str, float] = field(default_factory=dict)


def classification_code(classification: str) -> str:
    return {"timeout": "STUDIO_UNIT_TIMEOUT", "disabled": "STUDIO_RUNTIME_UNAVAILABLE",
            "unavailable": "STUDIO_RUNTIME_UNAVAILABLE"}.get(classification, "STUDIO_UNIT_FAILED")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def host_stream(state: dict[str, Any]) -> dict[str, Any]:
    """The plain stream dict the user's evaluate receives: no engine-internal keys."""
    culture = {name: value for name, value in (state.get("culture") or {}).items()
               if name in CULTURE_FIELDS or name == "ph"}
    return {"mass_flow_kg_s": float(state["mass_flow_kg_s"]), "temperature_K": float(state["temperature_K"]),
            "pressure_Pa": float(state["pressure_Pa"]),
            "mass_fractions": {name: float(value) for name, value in state.get("mass_fractions", {}).items()},
            "culture": culture}


def _state(port: str, stream: Any, compounds: list[str]) -> dict[str, Any]:
    def invalid(message: str) -> UnitCheckError:
        return UnitCheckError("STUDIO_UNIT_STATE_INVALID", f"Outlet {port}: {message}")

    if not isinstance(stream, dict) or not {"mass_flow_kg_s", "temperature_K", "pressure_Pa",
                                           "mass_fractions"} <= set(stream) <= _STREAM_KEYS:
        raise invalid("a stream needs mass_flow_kg_s, temperature_K, pressure_Pa, mass_fractions and optional culture.")
    for key in ("mass_flow_kg_s", "temperature_K", "pressure_Pa"):
        if not _number(stream[key]):
            raise invalid(f"{key} must be a finite number.")
    if stream["mass_flow_kg_s"] < 0:
        raise invalid("mass_flow_kg_s must not be negative.")
    if stream["temperature_K"] <= 0 or stream["pressure_Pa"] <= 0:
        raise invalid("temperature_K and pressure_Pa must be positive.")
    fractions = stream["mass_fractions"]
    if not isinstance(fractions, dict) or not set(fractions) <= set(compounds):
        raise invalid("mass_fractions may only name the flowsheet compounds.")
    if any(not _number(value) or value < 0 for value in fractions.values()):
        raise invalid("mass fractions must be finite and not negative.")
    if abs(sum(fractions.values()) - 1.0) > TOLERANCE:
        raise invalid("mass fractions must sum to 1 within 1e-9.")
    culture = stream.get("culture") or {}
    if not isinstance(culture, dict) or not set(culture) <= {*CULTURE_FIELDS, "ph"}:
        raise invalid("culture may only name the culture fields.")
    for name, value in culture.items():
        if value is not None and (not _number(value) or value < 0 or (name == "ph" and value > 14)):
            raise invalid(f"culture {name} must be a finite non-negative number or null.")
    return {"mass_flow_kg_s": float(stream["mass_flow_kg_s"]), "temperature_K": float(stream["temperature_K"]),
            "pressure_Pa": float(stream["pressure_Pa"]),
            "mass_fractions": {name: float(fractions.get(name, 0.0)) for name in compounds},
            "culture": dict(culture)}


def _culture_amount(stream: dict[str, Any], name: str) -> float:
    value = (stream.get("culture") or {}).get(name)
    factor = 0.001 if name == "salinity" else 1.0
    return 0.0 if value is None else float(stream["mass_flow_kg_s"]) * float(value) * factor


def check_result(result: Any, *, inlets: dict[str, dict[str, Any]], outlet_names: list[str],
                 compounds: list[str], generation: list[str]) -> CheckedResult:
    """Validate a unit's result against the declared contract; never clips or renormalizes."""
    if not isinstance(result, dict) or "outlets" not in result or not set(result) <= {"outlets", "culture_generation"}:
        raise UnitCheckError("STUDIO_UNIT_STATE_INVALID",
                             'evaluate must return {"outlets": {...}} with optional "culture_generation".')
    returned = result["outlets"]
    if not isinstance(returned, dict) or set(returned) != set(outlet_names):
        raise UnitCheckError("STUDIO_UNIT_OUTLETS_MISMATCH",
                             f"Returned outlets {sorted(returned) if isinstance(returned, dict) else returned!r} "
                             f"differ from the declared {sorted(outlet_names)}.")
    outlets = {name: _state(name, returned[name], compounds) for name in outlet_names}
    rates = result.get("culture_generation") or {}
    if (not isinstance(rates, dict) or not set(rates) <= set(generation)
            or any(not _number(rate) for rate in rates.values())):
        raise UnitCheckError("STUDIO_UNIT_CULTURE_IMBALANCE",
                             f"culture_generation may only name declared fields {sorted(generation)} with finite rates.")
    inbound = list(inlets.values())
    outbound = list(outlets.values())
    total_in = sum(float(item["mass_flow_kg_s"]) for item in inbound)
    total_out = sum(item["mass_flow_kg_s"] for item in outbound)
    allowed = TOLERANCE * total_in
    if abs(total_in - total_out) > allowed:
        raise UnitCheckError("STUDIO_UNIT_MASS_IMBALANCE",
                             f"Total mass in {total_in:.12g} kg/s differs from out {total_out:.12g} kg/s.")
    for compound in compounds:
        amount_in = sum(float(item["mass_flow_kg_s"]) * float(item.get("mass_fractions", {}).get(compound, 0.0))
                        for item in inbound)
        amount_out = sum(item["mass_flow_kg_s"] * item["mass_fractions"][compound] for item in outbound)
        if abs(amount_in - amount_out) > TOLERANCE * max(abs(amount_in), abs(amount_out)):
            raise UnitCheckError("STUDIO_UNIT_COMPONENT_IMBALANCE",
                                 f"Compound {compound} in {amount_in:.12g} kg/s differs from out {amount_out:.12g} kg/s.")
    for name in CULTURE_FIELDS:
        active_in = [item for item in inbound if float(item["mass_flow_kg_s"]) > 0]
        active_out = [item for item in outbound if float(item["mass_flow_kg_s"]) > 0]
        unknown_in = any((item.get("culture") or {}).get(name, 0.0) is None for item in active_in)
        unknown_out = [(item.get("culture") or {}).get(name, 0.0) is None for item in active_out]
        if unknown_in:
            if not all(unknown_out) or rates.get(name, 0.0) != 0.0:
                raise UnitCheckError("STUDIO_UNIT_CULTURE_IMBALANCE",
                                     f"Culture {name} has an unknown inlet, so active outlets must stay unknown "
                                     "and cannot declare generation.")
            continue  # unknown inlet amount propagates; no numeric balance can be claimed
        if any(unknown_out):
            raise UnitCheckError("STUDIO_UNIT_CULTURE_IMBALANCE",
                                 f"Culture {name} has known inlets but an unknown active outlet.")
        amount_in = sum(_culture_amount(item, name) for item in inbound)
        amount_out = sum(_culture_amount(item, name) for item in outbound)
        declared_rate = float(rates.get(name, 0.0))
        if abs(amount_out - amount_in - declared_rate) > (
                TOLERANCE * max(abs(amount_in), abs(amount_out), abs(declared_rate)) + 1e-12):
            raise UnitCheckError("STUDIO_UNIT_CULTURE_IMBALANCE",
                                 f"Culture {name} in {amount_in:.12g} plus declared generation "
                                 f"{declared_rate:.12g} differs from out {amount_out:.12g}.")
    return CheckedResult(outlets=outlets, culture_generation={name: float(rate) for name, rate in rates.items()})
