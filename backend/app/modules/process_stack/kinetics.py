"""Closed, deterministic DWSIM script kinetics for spec 180.

A typed reactor rate law is compiled from a fixed template per form and option. Only the form id,
validated finite parameters (converted to DWSIM's kmol, m³, h, J/mol and K) and compound names from
the declared, closed compound list reach the generated IronPython. No caller text becomes syntax.
"""

from __future__ import annotations

import math
from collections.abc import Collection
from typing import Any

from app.modules.process_stack.draft_models import COMPOUNDS

KINETICS_VERSION = "180.1"
GAS_CONSTANT = 8.31446261815324  # J/(mol.K)

# Supported domains in DWSIM units. The bounds keep every template product finite: with
# concentrations ≤ 1e6 kmol/m³, half-saturation and inhibition constants ≥ 1e-12 kmol/m³ and the
# Arrhenius exponent clamped to ±200, r ≤ 1e6 · e²⁰⁰ ≈ 7e92 kmol/(m³·h).
V_MAX_RANGE = (0.0, 1e6)            # kmol/(m3.h), inclusive
CONCENTRATION_RANGE = (1e-12, 1e6)  # kmol/m3, inclusive (constants must be > 0)
ACTIVATION_ENERGY_RANGE = (0.0, 3e5)  # J/mol
REFERENCE_TEMPERATURE_RANGE = (200.0, 1000.0)  # K
EXPONENT_CLAMP = 200.0

FORM_LABELS = {"power_law_arrhenius": "Power law (Arrhenius)", "monod": "Monod", "haldane": "Haldane / Andrews"}

EXPLANATIONS = {
    "power_law_arrhenius": (
        "DWSIM's native power law: r = A·exp(−E/RT)·Π Cᵢ^orderᵢ, with A in kmol/(m³·h) and E in J/mol. "
        "It is DWSIM's own kinetic model, evaluated natively."),
    "monod": (
        "Monod saturation: r = V_max·S/(K_S + S). V_max is the largest consumption rate of the base reactant, "
        "reached when S is far above K_S; K_S is the substrate concentration at which the rate is half of V_max. "
        "S is the base-reactant concentration, clamped at zero. r is the base-reactant consumption rate in "
        "kmol/(m³·h); other participants change in proportion to their coefficient over the base coefficient."),
    "haldane": (
        "Haldane (Andrews) substrate inhibition: r = V_max·S/(K_S + S + S²/K_I). Like Monod at low S, but the "
        "rate falls again at high S; K_I sets how strongly excess substrate inhibits. The rate peaks at "
        "S = √(K_S·K_I). S is clamped at zero and r is the base-reactant consumption rate in kmol/(m³·h)."),
}
INHIBITION_EXPLANATION = (
    "Non-competitive inhibition multiplies the rate by K_i/(K_i + I); competitive inhibition raises the "
    "half-saturation constant to K_S·(1 + I/K_i). I is the concentration of a named reaction participant, "
    "clamped at zero.")
TEMPERATURE_EXPLANATION = (
    "The optional temperature factor multiplies the rate by exp(−(E_a/R)(1/T − 1/T_ref)); it equals 1 at T_ref. "
    "A PFR supports it only in Isothermic mode; a CSTR uses its outlet temperature.")
UNSUPPORTED_CONDITIONS = (
    "vapour or two-phase reactor contents",
    "CSTR Adiabatic; PFR Adiabatic, HeatExchange and NonIsothermalNonAdiabatic for typed rate laws",
    "reverse and equilibrium reactions",
    "free-text rate expressions or scripts",
    "more than one typed reaction on one reactor",
)


class KineticsError(ValueError):
    def __init__(self, problems: list[dict[str, str]]):
        super().__init__("; ".join(item["message"] for item in problems))
        self.problems = problems


def _si(value: Any) -> float | None:
    if isinstance(value, dict) and isinstance(value.get("si"), (int, float)) and not isinstance(value.get("si"), bool):
        return float(value["si"])
    return None


def _problem(code: str, message: str, field: str) -> dict[str, str]:
    return {"code": code, "message": message, "field": field}


def parameters(law: dict[str, Any]) -> dict[str, float | None]:
    """Rate-law parameters in DWSIM units (None when absent)."""
    def scaled(value: Any, factor: float) -> float | None:
        si = _si(value)
        return None if si is None else si * factor
    out = {"v_max": scaled(law.get("v_max"), 3.6),  # mol/(m3.s) -> kmol/(m3.h)
           "k_s": scaled(law.get("k_s"), 1e-3)}      # mol/m3 -> kmol/m3
    if law.get("form") == "haldane":
        out["k_i"] = scaled(law.get("k_i"), 1e-3)
    for index, term in enumerate(law.get("inhibitions") or []):
        out[f"inhibitions.{index}.k_i"] = scaled(term.get("k_i"), 1e-3)
    if law.get("temperature") is not None:
        out["temperature.activation_energy"] = _si(law["temperature"].get("activation_energy"))
        out["temperature.reference_temperature"] = _si(law["temperature"].get("reference_temperature"))
    return out


_LABELS = {"v_max": "V_max", "k_s": "K_S", "k_i": "K_I", "temperature.activation_energy": "activation energy",
           "temperature.reference_temperature": "reference temperature"}


def problems(reaction: dict[str, Any], declared: Collection[str]) -> list[dict[str, str]]:
    """Every pre-Run kinetics finding for one stored rate-law reaction (empty when compilable)."""
    law = reaction.get("rate_law")
    if not law:
        return []
    found: list[dict[str, str]] = []
    participants = set(reaction.get("stoichiometry") or {})
    undeclared = sorted(participants - set(declared))
    if undeclared:
        found.append(_problem("KINETICS_COMPOUND_UNDECLARED",
                              f"Declare {', '.join(undeclared)} in Thermo or remove it from the reaction.",
                              "stoichiometry"))
    substrate = law.get("substrate")
    if substrate != reaction.get("base_reactant") or substrate not in participants:
        found.append(_problem("KINETICS_SUBSTRATE_INVALID", "The substrate must be the base reactant.", "rate_law.substrate"))
    seen: set[str] = set()
    for index, term in enumerate(law.get("inhibitions") or []):
        inhibitor = term.get("inhibitor")
        if inhibitor not in participants or inhibitor == substrate or inhibitor in seen:
            found.append(_problem("KINETICS_SUBSTRATE_INVALID",
                                  "Each inhibitor must be a distinct reaction participant other than the substrate.",
                                  f"rate_law.inhibitions.{index}.inhibitor"))
        seen.add(str(inhibitor))
    for key, value in parameters(law).items():
        label = _LABELS.get(key, "inhibition K_i" if key.startswith("inhibitions.") else key)
        if value is None:
            found.append(_problem("KINETICS_PARAMETER_MISSING", f"Set {label}.", f"rate_law.{key}"))
            continue
        if key == "v_max":
            low, high, open_low = *V_MAX_RANGE, False
        elif key == "temperature.activation_energy":
            low, high, open_low = *ACTIVATION_ENERGY_RANGE, False
        elif key == "temperature.reference_temperature":
            low, high, open_low = *REFERENCE_TEMPERATURE_RANGE, False
        else:
            low, high, open_low = *CONCENTRATION_RANGE, True
        if not math.isfinite(value) or value < low or value > high:
            bound = "greater than zero" if open_low else f"at least {low:g}"
            found.append(_problem("KINETICS_PARAMETER_DOMAIN",
                                  f"{label} must be {bound} and at most {high:g} (in {_unit(key)}).",
                                  f"rate_law.{key}"))
    provenance = reaction.get("provenance") or {}
    if not provenance.get("kind"):
        found.append(_problem("KINETICS_PROVENANCE_MISSING", "Record where the kinetic parameters come from.", "provenance"))
    elif provenance["kind"] == "literature" and not (provenance.get("citation") or "").strip():
        found.append(_problem("KINETICS_PROVENANCE_MISSING", "A literature source needs a citation.", "provenance.citation"))
    return found


def _unit(key: str) -> str:
    if key == "v_max":
        return "kmol/(m³·h)"
    if key == "temperature.activation_energy":
        return "J/mol"
    if key == "temperature.reference_temperature":
        return "K"
    return "kmol/m³"


def _number(value: float) -> str:
    """Deterministic, round-trip exact Python literal for a validated finite float."""
    if not math.isfinite(value):
        raise KineticsError([_problem("KINETICS_PARAMETER_DOMAIN", "Parameter is not finite.", "rate_law")])
    return repr(float(value) + 0.0)  # + 0.0 folds -0.0 to 0.0


def _amount(name: str) -> str:
    # Names come from the closed compound list; reject anything that could alter generated code.
    if name not in COMPOUNDS or not all(char.isalnum() or char in " -" for char in name):
        raise KineticsError([_problem("KINETICS_COMPOUND_UNDECLARED", "Unsupported compound name.", "stoichiometry")])
    return f"max(Amounts['{name}'], 0.0)"


def render(reaction: dict[str, Any]) -> str:
    """The fixed IronPython template for one stored, valid rate-law reaction."""
    found = problems(reaction, COMPOUNDS)
    if found:
        raise KineticsError(found)
    law = reaction["rate_law"]
    values = parameters(law)
    lines = ["# Jarvis rate law " + KINETICS_VERSION + ": " + str(law["form"]), f"S = {_amount(law['substrate'])}",
             f"K = {_number(values['k_s'])}"]  # type: ignore[arg-type]
    factors: list[str] = []
    for index, term in enumerate(law.get("inhibitions") or []):
        k_i = _number(values[f"inhibitions.{index}.k_i"])  # type: ignore[arg-type]
        lines.append(f"I{index} = {_amount(term['inhibitor'])}")
        if term["kind"] == "competitive":
            lines.append(f"K = K * (1.0 + I{index} / {k_i})")
        else:
            factors.append(f"({k_i} / ({k_i} + I{index}))")
    if law["form"] == "monod":
        lines.append("f = S / (K + S)")
    else:
        lines.append(f"f = S / (K + S + S * S / {_number(values['k_i'])})")  # type: ignore[arg-type]
    for factor in factors:
        lines.append(f"f = f * {factor}")
    if law.get("temperature") is not None:
        ea = _number(values["temperature.activation_energy"])  # type: ignore[arg-type]
        tref = _number(values["temperature.reference_temperature"])  # type: ignore[arg-type]
        lines.insert(1, "import math")
        lines.append(f"x = -({ea} / {_number(GAS_CONSTANT)}) * (1.0 / max(T, 1.0) - 1.0 / {tref})")
        lines.append(f"f = f * math.exp(min({_number(EXPONENT_CLAMP)}, max(-{_number(EXPONENT_CLAMP)}, x)))")
    lines.append(f"r = {_number(values['v_max'])} * f")  # type: ignore[arg-type]
    return "\n".join(lines) + "\n"


def script_title(unit_tag: str, reaction_id: str) -> str:
    return f"jarvis-rate-{unit_tag}-{reaction_id}"


def _node(tag: str, *children: dict[str, Any], text: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"tag": tag}
    if children:
        result["children"] = list(children)
    if text is not None:
        result["text"] = text
    return result


def _mi(text: str) -> dict[str, Any]:
    if "_" in text:
        base, subscript = text.split("_", 1)
        return _node("msub", _node("mi", text=base), _node("mi", text=subscript))
    return _node("mi", text=text)


def _mo(text: str) -> dict[str, Any]:
    return _node("mo", text=text)


def _mn(text: str) -> dict[str, Any]:
    return _node("mn", text=text)


def _row(*items: dict[str, Any]) -> dict[str, Any]:
    return _node("mrow", *items)


def equation_tree(form: str, inhibitions: list[dict[str, Any]] | None = None,
                  temperature: bool = False) -> dict[str, Any]:
    """Typed MathML tree (the 169 allowlisted renderer's format) for a form and its options."""
    if form == "power_law_arrhenius":
        arrhenius = _row(_mi("A"), _mo("·"), _node("msup", _mi("e"), _row(_mo("−"), _node("mfrac", _mi("E"), _row(_mi("R"), _mi("T"))))),
                    _mo("·"), _mo("∏"), _node("msup", _mi("C_i"), _mi("n_i")))
        return _node("math", _row(_mi("r"), _mo("="), arrhenius))
    inhibitions = inhibitions or []
    k_s: dict[str, Any] = _mi("K_S")
    for index, term in enumerate(inhibitions):
        if term.get("kind") == "competitive":
            k_s = _row(k_s, _row(_mo("("), _mn("1"), _mo("+"), _node("mfrac", _mi(f"I_{index + 1}"), _mi(f"K_i{index + 1}")), _mo(")")))
    denominator = [k_s, _mo("+"), _mi("S")]
    if form == "haldane":
        denominator += [_mo("+"), _node("mfrac", _node("msup", _mi("S"), _mn("2")), _mi("K_I"))]
    body: list[dict[str, Any]] = [_node("mfrac", _row(_mi("V_max"), _mo("·"), _mi("S")), _row(*denominator))]
    for index, term in enumerate(inhibitions):
        if term.get("kind") != "competitive":
            body += [_mo("·"), _node("mfrac", _mi(f"K_i{index + 1}"), _row(_mi(f"K_i{index + 1}"), _mo("+"), _mi(f"I_{index + 1}")))]
    if temperature:
        body += [_mo("·"), _node("msup", _mi("e"), _row(_mo("−"), _node("mfrac", _mi("E_a"), _mi("R")), _mo("·"),
                 _row(_mo("("), _node("mfrac", _mn("1"), _mi("T")), _mo("−"), _node("mfrac", _mn("1"), _mi("T_ref")), _mo(")"))))]
    return _node("math", _row(_mi("r"), _mo("="), *body))


def form_table() -> dict[str, Any]:
    """Registry projection of the closed forms; the frontend draws its fields from it."""
    concentration = {"kind": "molar_concentration", "domain": "> 0", "minimum": CONCENTRATION_RANGE[0],
                     "maximum": CONCENTRATION_RANGE[1], "unit": "kmol/m3"}
    rate = {"kind": "reaction_rate", "domain": "≥ 0", "minimum": V_MAX_RANGE[0], "maximum": V_MAX_RANGE[1],
            "unit": "kmol/[m3.h]"}
    return {
        "version": KINETICS_VERSION,
        "forms": [
            {"id": "power_law_arrhenius", "label": FORM_LABELS["power_law_arrhenius"],
             "explanation": EXPLANATIONS["power_law_arrhenius"], "parameters": [],
             "equation": equation_tree("power_law_arrhenius")},
            {"id": "monod", "label": FORM_LABELS["monod"], "explanation": EXPLANATIONS["monod"],
             "parameters": [{"key": "v_max", "symbol": "V_max", "label": "Maximum rate", **rate},
                            {"key": "k_s", "symbol": "K_S", "label": "Half-saturation constant", **concentration}],
             "equation": equation_tree("monod")},
            {"id": "haldane", "label": FORM_LABELS["haldane"], "explanation": EXPLANATIONS["haldane"],
             "parameters": [{"key": "v_max", "symbol": "V_max", "label": "Maximum rate", **rate},
                            {"key": "k_s", "symbol": "K_S", "label": "Half-saturation constant", **concentration},
                            {"key": "k_i", "symbol": "K_I", "label": "Substrate inhibition constant", **concentration}],
             "equation": equation_tree("haldane")},
        ],
        "inhibition": {"max_terms": 3, "kinds": ["noncompetitive", "competitive"],
                       "k_i": {"symbol": "K_i", "label": "Inhibition constant", **concentration},
                       "explanation": INHIBITION_EXPLANATION},
        "temperature": {"activation_energy": {"kind": "molar_energy", "symbol": "E_a", "domain": "≥ 0",
                                              "minimum": ACTIVATION_ENERGY_RANGE[0],
                                              "maximum": ACTIVATION_ENERGY_RANGE[1], "unit": "J/mol"},
                        "reference_temperature": {"kind": "temperature", "symbol": "T_ref", "domain": "> 0",
                                                  "minimum": REFERENCE_TEMPERATURE_RANGE[0],
                                                  "maximum": REFERENCE_TEMPERATURE_RANGE[1], "unit": "K"},
                        "explanation": TEMPERATURE_EXPLANATION},
        "provenance_kinds": ["literature", "measurement", "operator_estimate", "synthetic"],
        "unsupported": list(UNSUPPORTED_CONDITIONS),
    }
