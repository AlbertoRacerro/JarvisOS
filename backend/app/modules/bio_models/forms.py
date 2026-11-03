"""Version-pinned biological model forms and their declarative presentation cards (spec 169)."""

# I is the normative PAR variable in the accepted equations.
# ruff: noqa: E741

from __future__ import annotations

import math
import re
from typing import Any

FORM_VERSION = "1.0.0"


class FormRefusal(ValueError):
    """An input violates a form's declared domain."""


def _finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not math.isfinite(float(value)):
        raise FormRefusal(f"{name} must be a finite number")
    return float(value)


def _positive(name: str, value: float) -> float:
    value = _finite(name, value)
    if value <= 0:
        raise FormRefusal(f"{name} must be > 0")
    return value


def light_monod(I: float, K_I: float) -> float:
    I, K_I = _finite("I", I), _positive("K_I", K_I)
    if I < 0:
        raise FormRefusal("I must be >= 0")
    return I / (K_I + I)


def light_haldane(I: float, K_I: float, K_i: float) -> float:
    I, K_I, K_i = _finite("I", I), _positive("K_I", K_I), _positive("K_i", K_i)
    if I < 0:
        raise FormRefusal("I must be >= 0")
    return I / (K_I + I + I * I / K_i)


def light_steele(I: float, I_opt: float) -> float:
    I, I_opt = _finite("I", I), _positive("I_opt", I_opt)
    if I < 0:
        raise FormRefusal("I must be >= 0")
    x = I / I_opt
    return x * math.exp(1.0 - x)


def light_eilers_peeters_steady(I: float, I_opt: float, beta: float) -> float:
    I, I_opt, beta = _finite("I", I), _positive("I_opt", I_opt), _finite("beta", beta)
    if I < 0:
        raise FormRefusal("I must be >= 0")
    if beta < 0:
        raise FormRefusal("beta must be >= 0")
    x = I / I_opt
    return (2.0 + beta) * x / (x * x + beta * x + 1.0)


def light_hill(I: float, I_k: float, n: float) -> float:
    """Hill-type response of Molina Grima et al. (1994), S21 Eq. (1); n = 1 is Monod."""
    I, I_k, n = _finite("I", I), _positive("I_k", I_k), _positive("n", n)
    if I < 0:
        raise FormRefusal("I must be >= 0")
    if I == 0:
        return 0.0
    # (I/I_k)^n / (1 + (I/I_k)^n), written to stay finite for large I/I_k.
    return 1.0 / (1.0 + (I_k / I) ** n)


def slab_mean_irradiance(I0: float, k_X: float, X: float, L: float) -> float:
    I0, k_X, X, L = _finite("I0", I0), _finite("k_X", k_X), _finite("X", X), _positive("L", L)
    if I0 < 0:
        raise FormRefusal("I0 must be >= 0")
    if k_X < 0:
        raise FormRefusal("k_X must be >= 0")
    if X < 0:
        raise FormRefusal("X must be >= 0")
    tau = k_X * X * L
    return I0 if tau == 0 else I0 * (-math.expm1(-tau)) / tau


def slab_response_average(response, I0: float, k_X: float, X: float, L: float, **parameters: float) -> float:
    """8-point Gauss–Legendre depth average; this is the 107 parity form."""
    I0, k_X, X, L = _finite("I0", I0), _finite("k_X", k_X), _finite("X", X), _positive("L", L)
    if I0 < 0:
        raise FormRefusal("I0 must be >= 0")
    if k_X < 0:
        raise FormRefusal("k_X must be >= 0")
    if X < 0:
        raise FormRefusal("X must be >= 0")
    # NumPy's 8-point nodes and weights are the exact quadrature used in 107.
    import numpy as np

    nodes, weights = np.polynomial.legendre.leggauss(8)
    depths = 0.5 * (nodes + 1.0)
    return float(np.dot(0.5 * weights, [response(I0 * math.exp(-k_X * X * L * float(z)), **parameters) for z in depths]))


def temperature_isothermal(T: float | None = None) -> float:
    if T is not None:
        _finite("T", T)
    return 1.0


def temperature_ctmi(T: float, T_min: float, T_opt: float, T_max: float) -> float:
    T, T_min, T_opt, T_max = (_finite(name, value) for name, value in (("T", T), ("T_min", T_min), ("T_opt", T_opt), ("T_max", T_max)))
    if min(T, T_min, T_opt, T_max) <= 0:
        raise FormRefusal("T, T_min, T_opt and T_max must be > 0 K")
    if not T_min < T_opt < T_max:
        raise FormRefusal("T_min, T_opt, T_max must satisfy T_min < T_opt < T_max")
    if not T_min < T < T_max:
        return 0.0
    numerator = (T - T_max) * (T - T_min) ** 2
    denominator = (T_opt - T_min) * ((T_opt - T_min) * (T - T_opt) - (T_opt - T_max) * (T_opt + T_min - 2.0 * T))
    return max(0.0, numerator / denominator)


def temperature_arrhenius_ref(T: float, T_ref: float, E_a: float, R: float = 8.31446261815324) -> float:
    T, T_ref, E_a = _positive("T", T), _positive("T_ref", T_ref), _finite("E_a", E_a)
    if E_a < 0:
        raise FormRefusal("E_a must be >= 0")
    return math.exp(-(E_a / R) * (1.0 / T - 1.0 / T_ref))


def nutrient_monod(S: float, K: float) -> float:
    S, K = _finite("S", S), _positive("K", K)
    if S < 0:
        raise FormRefusal("S must be >= 0")
    return S / (K + S)


def nutrient_droop(Q: float, Q_min: float) -> float:
    Q, Q_min = _finite("Q", Q), _positive("Q_min", Q_min)
    if Q < 0:
        raise FormRefusal("Q must be >= 0")
    return max(0.0, 1.0 - Q_min / Q) if Q else 0.0


def combine_multiplicative(factors: list[float]) -> float:
    if not factors:
        raise FormRefusal("at least one nutrient factor is required")
    return math.prod(_factor(value) for value in factors)


def combine_liebig(factors: list[float]) -> float:
    if not factors:
        raise FormRefusal("at least one nutrient factor is required")
    return min(_factor(value) for value in factors)


def _factor(value: float) -> float:
    value = _finite("factor", value)
    if not 0 <= value <= 1:
        raise FormRefusal("factor must be in [0, 1]")
    return value


def loss_first_order(k_d: float) -> float:
    k_d = _finite("k_d", k_d)
    if k_d < 0:
        raise FormRefusal("k_d must be >= 0")
    return k_d


def loss_light_dark(I0: float, I_dark: float, m_L: float, m_D: float) -> float:
    I0, I_dark, m_L, m_D = (_finite(name, value) for name, value in (("I0", I0), ("I_dark", I_dark), ("m_L", m_L), ("m_D", m_D)))
    if I0 < 0 or I_dark < 0:
        raise FormRefusal("I0 and I_dark must be >= 0")
    if m_L < 0 or m_D < 0:
        raise FormRefusal("m_L and m_D must be >= 0")
    return m_L if I0 > I_dark else m_D


def stoich_photoautotrophic(a: float, b: float, c: float, d: float, w_ash: float, n_source: str) -> dict[str, Any]:
    """Balance CH_aO_bN_cP_d with CO2, NH3/HNO3 and H3PO4; report kg/kg total dry biomass."""
    a, b, c, d, w_ash = (_finite(name, value) for name, value in (("a", a), ("b", b), ("c", c), ("d", d), ("w_ash", w_ash)))
    if min(a, b, c, d) < 0 or not 0 <= w_ash < 1:
        raise FormRefusal("a, b, c, d must be >= 0 and w_ash in [0, 1)")
    if n_source not in {"NH3", "HNO3"}:
        raise FormRefusal("n_source must be NH3 or HNO3")
    # Signed convention: positive coefficients are reactants; negative values
    # indicate net products. H2O is on the reactant side of the written balance.
    water = (a - (3.0 * c if n_source == "NH3" else c) - 3.0 * d) / 2.0
    oxygen = (2.0 + 4.0 * d + (3.0 * c if n_source == "HNO3" else 0.0) + water - b) / 2.0
    # Atomic mass (g/mol): C 12.011 H 1.008 O 15.999 N 14.007 P 30.974.
    biomass_g = 12.011 + 1.008 * a + 15.999 * b + 14.007 * c + 30.974 * d
    total_g = biomass_g / (1.0 - w_ash)
    coefficients = {"CO2": 1.0, n_source: c, "H3PO4": d, "H2O": water, "O2": -oxygen}
    # A signed coefficient retains balances when a selected source is a net product.
    yields = {"O2_produced_kg_per_kg_total_dry": -coefficients["O2"] * 31.998 / total_g,
              "CO2_consumed_kg_per_kg_total_dry": 44.009 * 1.0 / total_g,
              "N_consumed_kg_per_kg_total_dry": c * 14.007 / total_g,
              "P_consumed_kg_per_kg_total_dry": d * 30.974 / total_g}
    # Independently count atoms from the returned coefficient table. Positive
    # coefficients are reactants; negative coefficients are products.
    formulas = {"CO2": "CO2", "NH3": "NH3", "HNO3": "HNO3", "H3PO4": "H3PO4",
                "H2O": "H2O", "O2": "O2", "biomass": f"CH{a}O{b}N{c}P{d}"}
    atom_counts = {name: {element: float(amount) for element, amount in
                          re.findall(r"([A-Z][a-z]?)([0-9.]+)?", formula)
                          for amount in [amount or "1"]}
                   for name, formula in formulas.items()}
    signed = {**coefficients, "biomass": -1.0}
    elements = ("C", "H", "O", "N", "P")
    balances = {element: sum(signed.get(name, 0.0) * atom_counts[name].get(element, 0.0)
                             for name in signed) for element in elements}
    return {"coefficients_mol_per_C_mol": coefficients, "yields": yields, "elemental_residuals_mol": balances}


def _symbol(symbol: str, meaning: str, unit: str, valid_range: str) -> dict[str, str]:
    keys = {"β": "beta", "I_k": "I_k", "I₀": "I0", "Q_min,j": "Q_min", "a,b,c,d": "formula_coefficients"}
    return {"symbol": symbol, "key": keys.get(symbol, symbol), "meaning": meaning,
            "unit": "dimensionless" if unit == "1" else unit, "valid_range": valid_range}


_cards = [
    ("light.monod", "light", "f = I/(K_I + I)", [_symbol("I", "PAR irradiance", "µmol m⁻² s⁻¹", "≥ 0"), _symbol("K_I", "half-saturation irradiance", "µmol m⁻² s⁻¹", "> 0")], "light-limitation factor of a bioreactor growth model; not a DWSIM reactor rate law (see 180)."),
    ("light.haldane", "light", "f = I/(K_I + I + I²/K_i)", [_symbol("I", "PAR irradiance", "µmol m⁻² s⁻¹", "≥ 0"), _symbol("K_I", "half-saturation irradiance", "µmol m⁻² s⁻¹", "> 0"), _symbol("K_i", "inhibition irradiance", "µmol m⁻² s⁻¹", "> 0")], "light-limitation factor of a bioreactor growth model; not a DWSIM reactor rate law (see 180)."),
    ("light.steele", "light", "f = (I/I_opt) exp(1 − I/I_opt)", [_symbol("I", "PAR irradiance", "µmol m⁻² s⁻¹", "≥ 0"), _symbol("I_opt", "optimum irradiance", "µmol m⁻² s⁻¹", "> 0")], "light response of a bioreactor growth model."),
    ("light.eilers_peeters_steady", "light", "f = (2 + β)x/(x² + βx + 1), x = I/I_opt", [_symbol("I", "PAR irradiance", "µmol m⁻² s⁻¹", "≥ 0"), _symbol("I_opt", "optimum irradiance", "µmol m⁻² s⁻¹", "> 0"), _symbol("β", "curve shape", "1", "≥ 0")], "steady-state Eilers–Peeters light response, not the dynamic photoinhibition model."),
    ("light.hill", "light", "f = Iⁿ/(I_kⁿ + Iⁿ)", [_symbol("I", "PAR irradiance", "µmol m⁻² s⁻¹", "≥ 0"), _symbol("I_k", "half-saturation irradiance", "µmol m⁻² s⁻¹", "> 0"), _symbol("n", "Hill exponent", "1", "> 0")], "Hill-type light response (Molina Grima et al. 1994; S21 Eq. (1)); with optics.slab_mean_irradiance at L = d_t/cos θ it is the response to the mean irradiance of S21 Eq. (4)."),
    ("optics.slab_mean_irradiance", "optics", "Ī = I₀(1 − e^(−τ))/τ", [_symbol("I₀", "surface irradiance", "µmol m⁻² s⁻¹", "≥ 0"), _symbol("k_X", "specific extinction", "m² kg⁻¹", "≥ 0"), _symbol("X", "biomass concentration", "kg m⁻³", "≥ 0"), _symbol("L", "slab depth", "m", "> 0")], "depth-averaged Beer–Lambert slab irradiance."),
    ("optics.slab_response_average", "optics", "⟨f⟩ = (1/L)∫₀ᴸ f(I₀e^(−k_XXz)) dz", [_symbol("I₀", "surface irradiance", "µmol m⁻² s⁻¹", "≥ 0"), _symbol("k_X", "specific extinction", "m² kg⁻¹", "≥ 0"), _symbol("X", "biomass concentration", "kg m⁻³", "≥ 0"), _symbol("L", "slab depth", "m", "> 0")], "8-point Gauss–Legendre depth-averaged response; parity form for 107."),
    ("optics.cylinder_beam_diffuse_response_average", "optics",
     "⟨f⟩ = (1/πR²)∬ₐ f[(1−f_d)I₀e^(−k_XXs_b) + f_d I₀S₃] dA, R = D/2",
     [_symbol("D", "tube inner diameter", "m", "> 0"),
      _symbol("k_X", "specific extinction", "m² kg⁻¹", "≥ 0"),
      _symbol("X", "biomass concentration", "kg m⁻³", "≥ 0"),
      _symbol("I₀", "surface scalar irradiance", "µmol m⁻² s⁻¹", "≥ 0"),
      _symbol("f_d", "isotropic diffuse fraction", "1", "[0, 1]")],
     "True cylindrical beam-normal-to-axis and 3-D isotropic diffuse response, evaluated inside PhotobioreactorT1 only."),
    ("temperature.isothermal", "temperature", "f = 1", [_symbol("T", "temperature", "K", "> 0")], "isothermal temperature factor."),
    ("temperature.ctmi", "temperature", "f = (T−T_max)(T−T_min)² / ((T_opt−T_min)[(T_opt−T_min)(T−T_opt)−(T_opt−T_max)(T_opt+T_min−2T)])", [_symbol("T", "temperature", "K", "> 0"), _symbol("T_min", "minimum cardinal temperature", "K", "> 0"), _symbol("T_opt", "optimum cardinal temperature", "K", "T_min < T_opt < T_max"), _symbol("T_max", "maximum cardinal temperature", "K", "> T_opt")], "Rosso cardinal model with inflexion as used by Bernard–Rémond."),
    ("temperature.arrhenius_ref", "temperature", "f = exp[−(E_a/R)(1/T − 1/T_ref)]", [_symbol("T", "temperature", "K", "> 0"), _symbol("T_ref", "reference temperature", "K", "> 0"), _symbol("E_a", "activation energy", "J mol⁻¹", "≥ 0")], "Arrhenius factor; μ_max is defined at T_ref."),
    ("nutrient.monod", "nutrient", "f_j = S_j/(K_j + S_j)", [_symbol("S_j", "nutrient concentration", "kg m⁻³", "≥ 0"), _symbol("K_j", "half-saturation concentration", "kg m⁻³", "> 0")], "nutrient-limitation factor of a bioreactor growth model; in a DWSIM PFR or CSTR, Monod is instead a typed reactor rate law (180)."),
    ("nutrient.droop", "nutrient", "f_j = max(0, 1 − Q_min,j/Q_j)", [_symbol("Q_j", "intracellular quota", "kg element kg⁻¹ dry biomass", "≥ 0"), _symbol("Q_min,j", "minimum quota", "kg element kg⁻¹ dry biomass", "> 0")], "quota limitation factor; 170 carries quota as state."),
    ("combine.multiplicative", "combination", "f_S = ∏ⱼ f_j", [_symbol("f_j", "per-nutrient factor", "1", "[0, 1]")], "combines nutrient limitation factors multiplicatively."),
    ("combine.liebig", "combination", "f_S = minⱼ f_j", [_symbol("f_j", "per-nutrient factor", "1", "[0, 1]")], "combines nutrient limitation by Liebig minimum."),
    ("loss.first_order", "loss", "r = k_d", [_symbol("k_d", "specific biomass-loss rate", "h⁻¹", "≥ 0")], "specific biomass-loss rate subtracted from μ; not substrate maintenance demand."),
    ("loss.light_dark", "loss", "r = m_L if I₀ > I_dark else m_D", [_symbol("I₀", "surface irradiance", "µmol m⁻² s⁻¹", "≥ 0"), _symbol("I_dark", "dark threshold", "µmol m⁻² s⁻¹", "≥ 0"), _symbol("m_L", "light loss rate", "h⁻¹", "≥ 0"), _symbol("m_D", "dark loss rate", "h⁻¹", "≥ 0")], "specific biomass-loss rate, not substrate maintenance demand."),
    ("stoich.photoautotrophic", "stoichiometry", "CO₂ + c N-source + d H₃PO₄ + h H₂O → CHₐOᵦN꜀P𝒹 + o O₂", [_symbol("a,b,c,d", "ash-free biomass empirical formula per C-mol", "mol atom C-mol⁻¹", "≥ 0"), _symbol("w_ash", "ash fraction of total dry biomass", "1", "[0, 1)"), _symbol("N-source", "neutral nitrogen source", "NH₃ or HNO₃", "one of NH3, HNO3")], "Photoautotrophic elemental balance; h and o are signed coefficients, so a negative value reverses that species' side."),
]


def _node(tag: str, *children: dict[str, Any], text: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"tag": tag}
    if children:
        result["children"] = list(children)
    if text is not None:
        result["text"] = text
    return result


def _mi(text: str) -> dict[str, Any]: return _node("mi", text=text)
def _mn(text: str) -> dict[str, Any]: return _node("mn", text=text)
def _mo(text: str) -> dict[str, Any]: return _node("mo", text=text)
def _row(*items: dict[str, Any]) -> dict[str, Any]: return _node("mrow", *items)


def _symbol_node(name: str) -> dict[str, Any]:
    if "_" in name:
        base, subscript = name.split("_", 1)
        return _node("msub", _mi(base), _mi(subscript))
    return _mi(name)


def _equation_tree(form_id: str, equation: str) -> dict[str, Any]:
    # The published text names its own left-hand side ("r = k_d", "⟨f⟩ = …");
    # a reaction equation has none.
    published = re.match(r"\s*([^\s=()]+)\s*=\s*", equation)
    # Equation bodies use a typed MathML expression tree. The selected forms
    # below cover all forms with nested fraction, index or exponent structure;
    # simple forms still use explicit identifiers and operators.
    if form_id == "light.monod":
        body = _node("mfrac", _mi("I"), _row(_symbol_node("K_I"), _mo("+"), _mi("I")))
    elif form_id == "light.haldane":
        body = _node("mfrac", _mi("I"), _row(_symbol_node("K_I"), _mo("+"), _mi("I"), _mo("+"),
            _node("mfrac", _node("msup", _mi("I"), _mn("2")), _symbol_node("K_i"))))
    elif form_id == "light.hill":
        body = _node("mfrac", _node("msup", _mi("I"), _mi("n")),
            _row(_node("msup", _symbol_node("I_k"), _mi("n")), _mo("+"), _node("msup", _mi("I"), _mi("n"))))
    elif form_id == "light.steele":
        body = _row(_node("mfrac", _mi("I"), _symbol_node("I_opt")), _mo("·"),
                    _node("msup", _mi("e"), _row(_mn("1"), _mo("−"), _node("mfrac", _mi("I"), _symbol_node("I_opt")))))
    elif form_id == "light.eilers_peeters_steady":
        body = _node("mfrac", _row(_mn("2"), _mo("+"), _mi("β"), _mo("·"), _mi("x")),
            _row(_node("msup", _mi("x"), _mn("2")), _mo("+"), _mi("β"), _mo("·"), _mi("x"), _mo("+"), _mn("1")))
    elif form_id == "temperature.arrhenius_ref":
        body = _node("msup", _mi("e"), _row(_mo("−"), _node("mfrac", _mi("E_a"), _mi("R")), _mo("·"),
            _row(_node("mfrac", _mn("1"), _mi("T")), _mo("−"), _node("mfrac", _mn("1"), _mi("T_ref")))))
    elif form_id == "nutrient.monod":
        body = _node("mfrac", _mi("S_j"), _row(_mi("K_j"), _mo("+"), _mi("S_j")))
    elif form_id == "nutrient.droop":
        body = _row(_mi("max"), _mo("("), _mn("0"), _mo(","), _mn("1"), _mo("−"),
                    _node("mfrac", _mi("Q_min,j"), _mi("Q_j")), _mo(")"))
    elif form_id == "optics.slab_mean_irradiance":
        body = _node("mfrac", _row(_mi("I₀"), _mo("·"), _row(_mn("1"), _mo("−"),
            _node("msup", _mi("e"), _row(_mo("−"), _mi("τ"))))), _mi("τ"))
    elif form_id == "optics.slab_response_average":
        body = _row(_node("mfrac", _mi("1"), _mi("L")), _mo("·"),
            _node("msub", _mo("∫"), _row(_mn("0"), _mo(","), _mi("L"))), _mi("f"), _mo("("),
            _row(_mi("I₀"), _node("msup", _mi("e"), _row(_mo("−"), _symbol_node("k_X"), _mi("X"), _mi("z")))), _mo(")"), _mi("dz"))
    elif form_id == "optics.cylinder_beam_diffuse_response_average":
        body = _row(
            _node("mfrac", _mn("1"), _row(_mi("π"), _node("msup", _mi("R"), _mn("2")))),
            _mo("∬"), _mi("A"), _mi("f"), _mo("("),
            _row(_mo("("), _mn("1"), _mo("−"), _mi("f_d"), _mo(")"), _mi("I₀"),
                 _node("msup", _mi("e"), _row(_mo("−"), _mi("k_X"), _mi("X"), _mi("s_b"))),
                 _mo("+"), _mi("f_d"), _mi("I₀"), _mi("S₃")),
            _mo(")"), _mi("dA"), _mo(","), _mi("R"), _mo("="),
            _node("mfrac", _mi("D"), _mn("2")))
    elif form_id == "temperature.ctmi":
        body = _node("mfrac", _row(_row(_mi("T"), _mo("−"), _symbol_node("T_max")), _node("msup", _row(_mi("T"), _mo("−"), _symbol_node("T_min")), _mn("2"))),
            _row(_symbol_node("T_opt"), _mo("−"), _symbol_node("T_min"), _row(_row(_symbol_node("T_opt"), _mo("−"), _symbol_node("T_min")), _row(_mi("T"), _mo("−"), _symbol_node("T_opt"))),
                 _mo("−"), _row(_row(_symbol_node("T_opt"), _mo("−"), _symbol_node("T_max")), _row(_symbol_node("T_opt"), _mo("+"), _symbol_node("T_min"), _mo("−"), _mn("2"), _mi("T")))))
    else:
        # Tokenize the published expression into identifiers, numbers and
        # operators rather than placing the equation in a single mtext node.
        tokens = re.findall(r"[A-Za-zμĪβ⟨⟩₀ₐᵦ][A-Za-z0-9_₀ₐᵦ,]*|\d+(?:\.\d+)?|[^\s]", equation[len(published.group(0)):] if published else equation)
        items = []
        for token in tokens:
            if re.fullmatch(r"\d+(?:\.\d+)?", token):
                items.append(_mn(token))
            elif re.fullmatch(r"[A-Za-zμĪβ⟨⟩₀ₐᵦ][A-Za-z0-9_₀ₐᵦ,]*", token):
                items.append(_mi(token))
            else:
                items.append(_mo(token))
        body = _row(*items)
    if published is None:
        return _node("math", _row(body))
    return _node("math", _row(_mi(published.group(1)), _mo("="), body))


FORM_CARDS: tuple[dict[str, Any], ...] = tuple(
    {"id": form_id, "version": FORM_VERSION, "family": family, "equation": _equation_tree(form_id, equation),
     "equation_text": equation, "symbols": symbols,
     "parameters": [item for item in symbols if item["key"] in {
         "K_I", "K_i", "I_k", "n", "I_opt", "beta", "T_min", "T_opt", "T_max", "T_ref", "E_a", "K_j", "Q_min",
         "k_d", "I_dark", "m_L", "m_D", "a,b,c,d", "w_ash"}],
     "inputs": [item for item in symbols if item["key"] not in {
         "K_I", "K_i", "I_k", "n", "I_opt", "beta", "T_min", "T_opt", "T_max", "T_ref", "E_a", "K_j", "Q_min",
         "k_d", "I_dark", "m_L", "m_D", "a,b,c,d", "w_ash"}],
     "required_states": [], "required_inputs": [item["key"] for item in symbols],
     "applies_to": applies_to, "citations": (["QSDsan PM² reference", "107"] if form_id.startswith("optics.") else ["107"] if form_id in {"light.monod", "light.haldane", "temperature.ctmi", "nutrient.monod"}
                   else ["S21 Molina et al. 2001 Eq. (1), DOI 10.1016/S0168-1656(01)00353-4"] if form_id == "light.hill" else [])}
    for form_id, family, equation, symbols, applies_to in _cards
)


def kinetics_explanation() -> str:
    """Deterministic seam for spec 166; derived from the nutrient Monod card metadata."""
    card = next(item for item in FORM_CARDS if item["id"] == "nutrient.monod")
    return (f"The biological Monod card is a {card['applies_to']} For a DWSIM PFR or CSTR, a Monod or Haldane "
            "rate law is a typed reactor reaction: select the reactor and open Kinetics to define it. "
            "Photobioreactor (T1) uses a pinned biological model card. "
            "An explanation alone proposes no change.")


def form_card(form_id: str) -> dict[str, Any]:
    for card in FORM_CARDS:
        if card["id"] == form_id:
            return card
    raise FormRefusal(f"unknown form {form_id}")
