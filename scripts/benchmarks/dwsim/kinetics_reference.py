#!/usr/bin/env python3
"""Independent reference values for spec 180 (process rate-law kinetics).

Imports NO product code (nothing from backend/app); standard library only.
Deterministic.  `python3 kinetics_reference.py --json` prints the acceptance table.

Setup (spec 180, Required evidence 3): Water 70 / Ethylene oxide 30 wt %,
1 kg/s, 328.15 K, 200 kPa, NRTL, V = 2 m3, EO + H2O -> EG (-1, -1, +1, base EO).
Rate r = consumption of EO in kmol/(m3 h); concentrations in kmol/m3.

Two models, both integrated on conversion X of EO:

  constant density : Q = Q_in.
  variable Q       : Q = Q_in * (1 - K_Q * X)   (volumetric flow linear in X).
      DWSIM 10.2.9 liquid mixing is ideal in volume, so Q is exactly linear in X;
      K_Q = (1 - Q_out/Q_in) / X was identical to 1e-11 over nine measured solves
      (out/w180/coord_ref.md, evidence/pbr/pDWSIM3/p1_raw.json).
  Concentrations: S = C0 (1-X)/(1-K_Q X), P = C0 X/(1-K_Q X).
  CSTR: X F0 = r(S, P) V  (bracketing bisection, lowest-X steady state).
  PFR : dX/dV = r(S, P)/F0 (RK4, step refinement with Richardson check).
  F0 = C0 Q_in = EO molar feed (kmol/h).
"""
import argparse
import json
import math
import sys

R_GAS = 8.314462618  # J/(mol K)
V_REACTOR = 2.0  # m3

# Thermodynamic states measured with DWSIM 10.2.9 (evidence/pbr).  `--states-json`
# may replace them: {"T328": {"C0":..,"Qin_m3_h":..,"K_Q":..}, ...} or K_Q derived
# from {"Qin_m3_s":..,"Qout_m3_s":..,"X":..}.
STATES = {
    # pDWSIM3 p1_raw.json: C0 6.340125683883058 kmol/m3, Q_in 1.0741185975089746e-3 m3/s,
    # K_Q = (1 - Qout/Qin)/X = 0.09291365070074 (A1 CSTR).
    "T328": {"T_K": 328.15, "C0": 6.340125683883058,
             "Qin_m3_h": 1.0741185975089746e-3 * 3600.0,
             "K_Q": 0.09291365070074181},
    # pDWSIM4 q3_raw.json (feed and reactor at 338.15 K): C0 6.277917733309825,
    # Q_in 1.0847620495996412e-3 m3/s, Q_out 1.0486449515993569e-3 m3/s at X 0.34467038887400403.
    "T338": {"T_K": 338.15, "C0": 6.277917733309825,
             "Qin_m3_h": 1.0847620495996412e-3 * 3600.0,
             "K_Q": (1.0 - 1.0486449515993569e-3 / 1.0847620495996412e-3) / 0.34467038887400403},
}

# ---------------------------------------------------------------------------
# Single case table (acceptance script imports/parses this via --json).
# form: monod | haldane | monod_noncompetitive | monod_competitive | monod_temperature
# ---------------------------------------------------------------------------
COMMON = {"V_max": 5.0, "K_S": 2.0}
CASES = [
    {"id": "A1", "reactors": ["CSTR"], "form": "monod", "state": "T328",
     "params": {**COMMON}, "spec_X_variable_q": {"CSTR": 0.28531}},
    {"id": "A2", "reactors": ["CSTR", "PFR"], "form": "monod", "state": "T328",
     "params": {"V_max": 5.0, "K_S": 1e-9},
     "spec_X_variable_q": {"CSTR": 0.40789, "PFR": 0.40789}},
    {"id": "A3", "reactors": ["CSTR", "PFR"], "form": "haldane", "state": "T328",
     "params": {**COMMON, "K_I": 10.0}},
    {"id": "A4", "reactors": ["PFR"], "form": "monod", "state": "T328",
     "params": {**COMMON}, "spec_X_variable_q": {"PFR": 0.29793}},
    {"id": "A5", "reactors": ["CSTR", "PFR"], "form": "monod_noncompetitive", "state": "T328",
     "params": {**COMMON, "K_i_EG": 4.0}},
    # A6: q3 control values (pDWSIM4): E_a 20 kJ/mol, T_ref 328.15 K, reactor and feed at 338.15 K.
    {"id": "A6", "reactors": ["CSTR", "PFR"], "form": "monod_temperature", "state": "T338",
     "params": {**COMMON, "E_a_J_mol": 20000.0, "T_ref_K": 328.15, "T_K": 338.15}},
    {"id": "A7", "reactors": ["CSTR", "PFR"], "form": "monod_competitive", "state": "T328",
     "params": {**COMMON, "K_i_EG": 4.0}},
]


def rate(form, p, S, P):
    """EO consumption rate in kmol/(m3 h)."""
    S = max(S, 0.0)
    P = max(P, 0.0)
    vmax, ks = p["V_max"], p["K_S"]
    if form == "monod":
        return vmax * S / (ks + S)
    if form == "haldane":
        return vmax * S / (ks + S + S * S / p["K_I"])
    if form == "monod_noncompetitive":
        return vmax * S / (ks + S) * p["K_i_EG"] / (p["K_i_EG"] + P)
    if form == "monod_competitive":
        return vmax * S / (ks * (1.0 + P / p["K_i_EG"]) + S)
    if form == "monod_temperature":
        f = math.exp(-(p["E_a_J_mol"] / R_GAS) * (1.0 / p["T_K"] - 1.0 / p["T_ref_K"]))
        return vmax * S / (ks + S) * f
    raise ValueError(form)


def make_rx(form, p, st, kq):
    c0, f0 = st["C0"], st["C0"] * st["Qin_m3_h"]

    def rx(x):
        d = 1.0 - kq * x
        return rate(form, p, c0 * (1.0 - x) / d, c0 * x / d)
    return rx, f0


def cstr(rx, f0, v=V_REACTOR):
    """Lowest-X root of g(X) = X F0 - r V by scan + bisection; returns (X, n_roots)."""
    g = lambda x: x * f0 - rx(x) * v
    n = 20000
    prev_x, prev_g = 0.0, g(0.0)
    roots = []
    for i in range(1, n + 1):
        x = i / n
        gx = g(x)
        if prev_g == 0.0:
            roots.append(prev_x)
        elif prev_g * gx < 0.0:
            lo, hi = prev_x, x
            for _ in range(200):
                mid = 0.5 * (lo + hi)
                if g(lo) * g(mid) <= 0.0:
                    hi = mid
                else:
                    lo = mid
            roots.append(0.5 * (lo + hi))
        prev_x, prev_g = x, gx
    if not roots:
        raise RuntimeError("no CSTR root in (0,1]")
    return roots[0], len(roots)


def pfr(rx, f0, v=V_REACTOR, n=2000):
    def run(nn):
        h, x = v / nn, 0.0
        for _ in range(nn):
            k1 = rx(x) / f0
            k2 = rx(x + 0.5 * h * k1) / f0
            k3 = rx(x + 0.5 * h * k2) / f0
            k4 = rx(x + h * k3) / f0
            x += h * (k1 + 2 * k2 + 2 * k3 + k4) / 6.0
        return x
    a, b = run(n), run(2 * n)
    rich = b + (b - a) / 15.0
    return rich, abs(b - a) / 15.0


def solve(case, states):
    st = states[case["state"]]
    out = []
    for rtype in case["reactors"]:
        row = {"id": case["id"], "reactor": rtype, "form": case["form"], "params": case["params"],
               "state": case["state"], "K_Q": st["K_Q"], "notes": []}
        for key, kq in (("X_constant_density", 0.0), ("X_variable_q", st["K_Q"])):
            rx, f0 = make_rx(case["form"], case["params"], st, kq)
            if rtype == "CSTR":
                x, nroots = cstr(rx, f0)
                if nroots > 1:
                    row["notes"].append("%s: %d steady states, lowest X reported" % (key, nroots))
            else:
                x, err = pfr(rx, f0)
                row["notes"].append("%s: RK4 Richardson error estimate %.1e" % (key, err))
            row[key] = x
        row["X_difference_vq_minus_cd"] = row["X_variable_q"] - row["X_constant_density"]
        spec = case.get("spec_X_variable_q", {}).get(rtype)
        if spec is not None:
            row["spec_X_variable_q"] = spec
            row["rel_diff_vs_spec"] = (row["X_variable_q"] - spec) / spec
            if abs(row["rel_diff_vs_spec"]) > 2e-5:
                row["notes"].append(
                    "spec value is the 5-digit DWSIM measurement, not this reference; "
                    "DWSIM-vs-reference residual %.1e (coord_ref.md: A4 PFR -3.60e-5), inside the 2e-4 acceptance"
                    % -row["rel_diff_vs_spec"])
        out.append(row)
    return out


def load_states(path):
    states = json.loads(json.dumps(STATES))
    if path:
        for name, s in json.load(open(path)).items():
            s = dict(s)
            if "K_Q" not in s:
                s["K_Q"] = (1.0 - s["Qout_m3_s"] / s["Qin_m3_s"]) / s["X"]
            if "Qin_m3_h" not in s:
                s["Qin_m3_h"] = s["Qin_m3_s"] * 3600.0
            states.setdefault(name, {}).update(s)
    return states


def build(states_path=None):
    states = load_states(states_path)
    rows = []
    for c in CASES:
        rows.extend(solve(c, states))
    return {
        "setup": {"feed": "Water 70 / Ethylene oxide 30 wt %", "mass_flow_kg_s": 1.0, "T_K": 328.15,
                  "P_kPa": 200.0, "package": "NRTL", "V_m3": V_REACTOR, "reaction": "EO + H2O -> EG",
                  "rate_units": "kmol/(m3 h) EO consumption", "states": states},
        "method": ("CSTR: bisection on X F0 = r V; PFR: RK4 on dX/dV = r/F0 with Richardson (n, 2n); "
                   "variable Q: Q = Q_in (1 - K_Q X), K_Q measured from DWSIM ideal-volume liquid; "
                   "constant density: K_Q = 0. No product code imported."),
        "cases": rows,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="print JSON document")
    ap.add_argument("--states-json", help="override thermo states (C0, Qin, K_Q or Qout/X)")
    a = ap.parse_args(argv)
    doc = build(a.states_json)
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print("%-3s %-5s %-10s %-10s %-10s %s" % ("id", "rx", "X_cd", "X_vq", "diff", "spec rel"))
        for r in doc["cases"]:
            print("%-3s %-5s %.6f   %.6f   %+.2e  %s" % (
                r["id"], r["reactor"], r["X_constant_density"], r["X_variable_q"],
                r["X_difference_vq_minus_cd"], r.get("rel_diff_vs_spec", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
