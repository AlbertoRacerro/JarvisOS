"""Independent stream-balance check for one Jarvis-authored liquid rate law.

Only measured stream properties and the typed reaction enter this module. In particular,
neither DWSIM's script text nor its per-reaction extent/rate is used as evidence.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

_GAS_CONSTANT = 8.31446261815324  # J/(mol.K)
_CSTR_TOLERANCE = 1e-3
_PFR_TOLERANCE = 2e-3


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    result = float(value)
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _quantity(value: Any, kind: str) -> float:
    """Read a draft quantity as rate kmol/(m3.h), concentration kmol/m3, J/mol or K."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{kind} must be a quantity")
    if "si" in value:
        si = _number(value["si"], kind)
        return si * 3.6 if kind == "rate" else si / 1000 if kind == "concentration" else si
    raw = _number(value.get("value"), kind)
    unit = value.get("unit")
    factors = {
        "rate": {"kmol/(m3.h)": 1.0, "mol/(m3.s)": 3.6, "mol/(L.h)": 1.0},
        "concentration": {"kmol/m3": 1.0, "mol/m3": 0.001, "mmol/L": 0.001},
        "energy": {"J/mol": 1.0, "kJ/mol": 1000.0},
        "temperature": {"K": 1.0, "degC": 1.0},
    }
    if unit not in factors[kind]:
        raise ValueError(f"unsupported {kind} unit")
    return (raw + 273.15) if kind == "temperature" and unit == "degC" else raw * factors[kind][unit]


def _phase(stream: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    phases = stream.get("phases")
    if not isinstance(phases, list):
        raise ValueError("stream phases are missing")
    match = next((p for p in phases if isinstance(p, Mapping) and p.get("name") == name), None)
    if match is None:
        raise ValueError(f"{name} phase is missing")
    return match


def _stream(raw: Mapping[str, Any]) -> dict[str, Any]:
    stream = raw.get("reported", raw)
    if not isinstance(stream, Mapping):
        raise ValueError("reported stream is missing")
    vapor = _number(_phase(stream, "Vapor").get("fraction"), "vapor fraction")
    if vapor > 1e-9:
        raise _TwoPhaseError
    mixture = _phase(stream, "Mixture")
    density = _number(mixture.get("density_kg_m3"), "liquid density", positive=True)
    mass = _number(stream.get("mass_flow_kg_s"), "mass flow", positive=True)
    molar = _number(stream.get("molar_flow_mol_s"), "molar flow", positive=True)
    temperature = _number(stream.get("temperature_K"), "temperature", positive=True)
    compounds = mixture.get("compounds")
    if not isinstance(compounds, Mapping) or not compounds:
        raise ValueError("mixture mole fractions are missing")
    fractions = {name: _number(item.get("mole_fraction"), f"{name} mole fraction")
                 for name, item in compounds.items() if isinstance(name, str) and isinstance(item, Mapping)}
    if len(fractions) != len(compounds) or any(value < 0 or value > 1 for value in fractions.values()):
        raise ValueError("mixture mole fractions are invalid")
    if not math.isclose(sum(fractions.values()), 1.0, rel_tol=1e-5, abs_tol=1e-5):
        raise ValueError("mixture mole fractions do not sum to one")
    q = 3600.0 * mass / density
    return {"temperature": temperature, "q": q,
            "flows": {name: 3.6 * molar * fraction for name, fraction in fractions.items()}}


class _TwoPhaseError(Exception):
    pass


def _rate(law: Mapping[str, Any], concentrations: Mapping[str, float], temperature: float) -> float:
    form = law.get("form")
    if form not in {"monod", "haldane"}:
        raise ValueError("unsupported rate-law form")
    substrate = law.get("substrate")
    if not isinstance(substrate, str) or substrate not in concentrations:
        raise ValueError("substrate is missing from stream")
    s = max(0.0, concentrations[substrate])
    v_max = _quantity(law.get("v_max"), "rate")
    k_s = _quantity(law.get("k_s"), "concentration")
    if v_max < 0 or k_s <= 0:
        raise ValueError("rate-law parameter is outside its domain")
    extra = 0.0
    if form == "haldane":
        k_i = _quantity(law.get("k_i"), "concentration")
        if k_i <= 0:
            raise ValueError("Haldane inhibition constant must be positive")
        extra = s * s / k_i
    factor = 1.0
    inhibitions = law.get("inhibitions", [])
    if not isinstance(inhibitions, list) or len(inhibitions) > 3:
        raise ValueError("inhibitions are invalid")
    for term in inhibitions:
        if not isinstance(term, Mapping) or term.get("inhibitor") not in concentrations:
            raise ValueError("inhibitor is missing from stream")
        inhibitor = max(0.0, concentrations[term["inhibitor"]])
        k_i = _quantity(term.get("k_i"), "concentration")
        if k_i <= 0:
            raise ValueError("inhibition constant must be positive")
        if term.get("kind") == "competitive":
            k_s *= 1.0 + inhibitor / k_i
        elif term.get("kind") == "noncompetitive":
            factor *= k_i / (k_i + inhibitor)
        else:
            raise ValueError("unsupported inhibition kind")
    rate = v_max * s / (k_s + s + extra) * factor
    temp_factor = law.get("temperature")
    if temp_factor is not None:
        if not isinstance(temp_factor, Mapping):
            raise ValueError("temperature factor is invalid")
        energy = _quantity(temp_factor.get("activation_energy"), "energy")
        reference = _quantity(temp_factor.get("reference_temperature"), "temperature")
        if energy < 0 or reference <= 0 or temperature <= 0:
            raise ValueError("temperature factor is outside its domain")
        rate *= math.exp(-(energy / _GAS_CONSTANT) * (1.0 / temperature - 1.0 / reference))
    if not math.isfinite(rate) or rate < 0:
        raise ValueError("computed rate is not finite and nonnegative")
    return rate


def _finding(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _failed(code: str, message: str, tolerance: float) -> dict[str, Any]:
    return {"ok": False, "code": code, "residual": None, "tolerance": tolerance, "summary": {},
            "findings": [_finding(code, "blocker", message)], "conversion": None,
            "extent_kmol_h": None, "rate_inlet": None, "rate_outlet": None}


def verify_rate_law_reactor(*, reactor_type: str, reaction: Mapping[str, Any], volume_m3: float | None,
                            inlet: Mapping[str, Any], outlet: Mapping[str, Any]) -> dict[str, Any]:
    """Verify one typed CSTR/PFR reaction from raw DWSIM stream responses.

    Rates and extent use kmol/(m3.h) and kmol/h. A failed verification exposes
    only the residual as a diagnostic, never a current reactor result.
    """
    tolerance = _CSTR_TOLERANCE if reactor_type == "CSTR" else _PFR_TOLERANCE
    try:
        if reactor_type not in {"CSTR", "PFR"}:
            raise ValueError("unsupported reactor type")
        volume = _number(volume_m3, "reactor volume", positive=True)
        if not isinstance(reaction, Mapping) or not isinstance(reaction.get("rate_law"), Mapping):
            raise ValueError("typed rate law is missing")
        stoich = reaction.get("stoichiometry")
        base = reaction.get("base_reactant")
        if not isinstance(stoich, Mapping) or base not in stoich:
            raise ValueError("reaction stoichiometry is missing its base reactant")
        coeff = {name: _number(value, f"{name} coefficient") for name, value in stoich.items()}
        if coeff[base] >= 0 or any(value == 0 for value in coeff.values()):
            raise ValueError("reaction stoichiometry is invalid")
        source, product = _stream(inlet), _stream(outlet)
        if not set(coeff) <= source["flows"].keys() or not set(coeff) <= product["flows"].keys():
            raise ValueError("reaction participant is missing from a stream")
        fi, fo = source["flows"], product["flows"]
        f_base = fi[base]
        if f_base <= 0:
            raise ValueError("base reactant inlet flow must be positive")
        normalized = {name: value / abs(coeff[base]) for name, value in coeff.items()}
        extent = fi[base] - fo[base]
        conversion = extent / f_base
        if conversion < -1e-8 or conversion > 1 + 1e-8:
            raise ValueError("base reactant conversion is outside [0, 1]")
        law = reaction["rate_law"]
        ci = {name: flow / source["q"] for name, flow in fi.items()}
        co = {name: flow / product["q"] for name, flow in fo.items()}
        rate_in = _rate(law, ci, source["temperature"])
        rate_out = _rate(law, co, product["temperature"])
        # For one reaction, every participant's flow change must yield the same
        # base-consumption extent. This also detects an unreported side reaction.
        # A zero-rate reaction is valid. Use a small inlet-flow scale so solver
        # roundoff at zero conversion is not treated as a relative failure.
        flow_scale = max(abs(extent), rate_out * volume, 1e-6 * f_base)
        stoich_residual = max(abs((fo[name] - fi[name]) / normalized[name] - extent)
                              for name in normalized) / flow_scale
        if reactor_type == "CSTR":
            predicted_extent = rate_out * volume
            residual = max(stoich_residual, abs(extent - predicted_extent) / flow_scale)
        else:
            # The measured Q endpoints approximate density change along reaction
            # progress, as in the accepted variable-Q reference. Integrate dX/dV
            # with an RK4 solver independent of DWSIM's axial implementation.
            steps = 2048
            step = volume / steps
            x = 0.0
            def derivative(progress: float) -> float:
                fraction = min(max(progress / conversion, 0.0), 1.0) if conversion > 0 else 0.0
                q = source["q"] + (product["q"] - source["q"]) * fraction
                if q <= 0:
                    raise ValueError("interpolated liquid flow is not positive")
                flows = {name: fi[name] + normalized.get(name, 0.0) * f_base * progress for name in fi}
                if flows[base] < -1e-9:
                    raise ValueError("predicted base flow is negative")
                concentration = {name: max(0.0, value) / q for name, value in flows.items()}
                temperature = source["temperature"] + (product["temperature"] - source["temperature"]) * fraction
                return _rate(law, concentration, temperature) / f_base
            for _ in range(steps):
                k1 = derivative(x)
                k2 = derivative(x + 0.5 * step * k1)
                k3 = derivative(x + 0.5 * step * k2)
                k4 = derivative(x + step * k3)
                x += step * (k1 + 2 * k2 + 2 * k3 + k4) / 6
                if x >= 1:
                    x = 1.0
                    break
            residual = max(stoich_residual, abs(x - conversion) / max(abs(x), abs(conversion), 1e-6))
        findings: list[dict[str, str]] = []
        validity = reaction.get("validity")
        if isinstance(validity, Mapping):
            temperatures = (source["temperature"], product["temperature"]) if reactor_type == "PFR" else (product["temperature"],)
            lo = _quantity(validity["temperature_min"], "temperature") if validity.get("temperature_min") else None
            hi = _quantity(validity["temperature_max"], "temperature") if validity.get("temperature_max") else None
            substrate_max = (_quantity(validity["substrate_max"], "concentration")
                             if validity.get("substrate_max") else None)
            if ((lo is not None and min(temperatures) < lo) or
                    (hi is not None and max(temperatures) > hi) or
                    (substrate_max is not None and ci[base] > substrate_max)):
                findings.append(_finding("KINETICS_OUTSIDE_VALIDITY", "warning",
                                         "Measured temperature or inlet substrate concentration is outside declared validity."))
        ok = residual <= tolerance
        if not ok:
            findings.insert(0, _finding("KINETICS_VERIFICATION_FAILED", "blocker",
                                        "Measured stream conversion or stoichiometry disagrees with the typed rate law."))
        summary = ({"outlet_concentrations_kmol_m3": co,
                    "residence_time_h": volume / product["q"],
                    "outlet_temperature_K": product["temperature"]} if ok else {})
        return {"ok": ok, "code": None if ok else "KINETICS_VERIFICATION_FAILED", "residual": residual,
                "tolerance": tolerance, "summary": summary, "findings": findings,
                "conversion": conversion if ok else None, "extent_kmol_h": extent if ok else None,
                "rate_inlet": rate_in if ok and reactor_type == "PFR" else None,
                "rate_outlet": rate_out if ok else None}
    except _TwoPhaseError:
        return _failed("KINETICS_TWO_PHASE_UNSUPPORTED", "A reactor stream has a vapour fraction above 1e-9.", tolerance)
    except (ValueError, KeyError, TypeError, OverflowError, ZeroDivisionError) as exc:
        return _failed("KINETICS_VERIFICATION_FAILED", f"Kinetics verification input is unusable: {exc}", tolerance)
