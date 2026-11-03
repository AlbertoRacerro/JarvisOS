"""Deterministic synthetic tear maps for the recycle-solver qualification matrix (spec 183).

Every case is a map g on the real tear-state shape (temperature, pressure, mass flow, mass fractions, culture) with
a known fixed point, so the true fixed-point error of whatever a controller returns can be measured exactly.
Nothing here touches DWSIM; `run_matrix` drives the engine-neutral controller in `mixed` directly.
"""

from __future__ import annotations

import copy
import math
import time
from collections.abc import Callable
from typing import Any

from app.modules.process_stack import mixed

TAG = "R1"
Q_VALUES = (0.0, 0.25, 0.5, 0.7, 0.9, 0.99, 0.999, 0.9999)
SEEDS = {"exact": 1.0, "1pct": 1.01, "10pct": 1.10, "100pct": 2.0, "near_zero": 1e-6}
# Variables a case may move, and the scale each fixed point uses.
FIELDS = ("mass_flow_kg_s", "biomass", "nitrogen")


def state(values: dict[str, float]) -> dict[str, dict[str, Any]]:
    culture = {name: 1.0 for name in mixed.CULTURE_FIELDS}
    culture["ph"] = 7.5
    row = {"temperature_K": 298.15, "pressure_Pa": 101325.0, "mass_flow_kg_s": 1.0,
           "mass_fractions": {"Water": 1.0}, "culture": culture}
    for name, value in values.items():
        if name == "mass_flow_kg_s":
            row["mass_flow_kg_s"] = value
        else:
            row["culture"][name] = value
    return {TAG: row}


def read(tear: dict[str, dict[str, Any]], names: tuple[str, ...]) -> list[float]:
    row = tear[TAG]
    return [float(row["mass_flow_kg_s"]) if name == "mass_flow_kg_s" else float(row["culture"][name])
            for name in names]


class Case:
    """A map on a subset of FIELDS with a known fixed point x_star (one per stable branch)."""

    def __init__(self, name: str, names: tuple[str, ...], g: Callable[[list[float]], list[float]],
                 x_star: list[float], seed: list[float], *, family: str, q: float | None = None,
                 branches: list[list[float]] | None = None, note: str = "") -> None:
        self.name, self.names, self.g, self.x_star, self.seed = name, names, g, x_star, seed
        self.family, self.q, self.note = family, q, note
        self.branches = branches or [x_star]

    def evaluate(self, guess: dict[str, dict[str, Any]], _iteration: int) -> dict[str, dict[str, Any]]:
        x = read(guess, self.names)
        y = self.g(x)
        out = copy.deepcopy(guess)
        for name, value in zip(self.names, y, strict=True):
            if name == "mass_flow_kg_s":
                out[TAG]["mass_flow_kg_s"] = value
            else:
                out[TAG]["culture"][name] = value
        return out


def linear_cases() -> list[Case]:
    cases = []
    for q in Q_VALUES:
        for label, factor in SEEDS.items():
            def g(x: list[float], q: float = q) -> list[float]:
                return [1.0 + q * (x[0] - 1.0)]
            cases.append(Case(f"linear q={q} seed={label}", ("mass_flow_kg_s",), g, [1.0], [factor],
                              family="linear_scalar", q=q))
    # Oscillating and oscillating-divergent maps, where damping is what decides the outcome.
    # Seeds stay where g is physical (non-negative flow); a 100 % seed would make g itself return a negative flow.
    for q in (-0.9, -1.5):
        for label, factor in (("10pct", 1.1), ("50pct", 1.5)):
            def g(x: list[float], q: float = q) -> list[float]:
                return [1.0 + q * (x[0] - 1.0)]
            cases.append(Case(f"linear q={q} seed={label}", ("mass_flow_kg_s",), g, [1.0], [factor],
                              family="linear_oscillating", q=q))
    # The same linear map on a 3-vector, contraction q on every component.
    for q in (0.5, 0.9, 0.99, 0.999):
        for label in ("10pct", "near_zero"):
            def g(x: list[float], q: float = q) -> list[float]:
                return [s + q * (v - s) for v, s in zip(x, (1.0, 2.0, 0.5), strict=True)]
            factor = SEEDS[label]
            cases.append(Case(f"vector q={q} seed={label}", FIELDS, g, [1.0, 2.0, 0.5],
                              [1.0 * factor, 2.0 * factor, 0.5 * factor], family="linear_vector", q=q))
    return cases


def noisy_cases() -> list[Case]:
    """Deterministic evaluation noise of a given size in tolerance units, like a DWSIM inner solve."""
    cases = []
    for q in (0.9, 0.999):
        for amplitude in (0.1, 0.5):
            star = (1.0, 2.0, 0.5)
            calls = [0]

            def g(x: list[float], q: float = q, amplitude: float = amplitude, calls: list[int] = calls,
                  star: tuple[float, ...] = star) -> list[float]:
                calls[0] += 1
                k = calls[0]
                return [s + q * (v - s) + amplitude * 1e-5 * s * math.sin(12.9898 * k + 78.233 * i)
                        for i, (v, s) in enumerate(zip(x, star, strict=True))]
            cases.append(Case(f"noisy q={q} noise={amplitude}tol seed=10pct", FIELDS, g, list(star),
                              [1.1 * s for s in star], family="noisy_vector", q=q,
                              note="noise counter is per case; run each case once per method"))
    return cases


def nonlinear_cases() -> list[Case]:
    cases = []

    # Mildly nonlinear contraction: a saturating recycle return, g'(x*) ~ 0.6.
    def saturating(x: list[float]) -> list[float]:
        return [0.4 + 1.2 * x[0] / (1.0 + 0.2 * x[0])]
    star = _fixed_point_scalar(saturating, 1.0)
    for label, factor in SEEDS.items():
        cases.append(Case(f"saturating seed={label}", ("mass_flow_kg_s",), saturating, [star], [star * factor],
                          family="nonlinear_contractive", q=_slope(saturating, star)))

    # Badly conditioned, non-normal coupled map: eigenvalues 0.95 and 0.3, strong off-diagonal coupling
    # (transient growth before contraction), components of very different magnitude.
    x_star = [1.0, 2.0, 0.05]
    a = [[0.95, 40.0, 0.0], [0.0, 0.3, 0.0], [0.0, 0.0, 0.95]]

    def coupled(x: list[float]) -> list[float]:
        d = [v - s for v, s in zip(x, x_star, strict=True)]
        return [s + sum(a[i][j] * d[j] for j in range(3)) for i, s in enumerate(x_star)]
    for label in ("1pct", "10pct", "100pct", "near_zero"):
        f = SEEDS[label]
        cases.append(Case(f"coupled-nonnormal seed={label}", FIELDS, coupled, x_star, [s * f for s in x_star],
                          family="ill_conditioned_vector", q=0.95))

    # Regime switch, washout <-> productive biology: below a critical biomass the culture washes out to
    # a low branch, above it the productive branch is the attractor. Both branches are stable fixed points.
    def regime(x: list[float]) -> list[float]:
        b = x[0]
        return [0.01 + 0.5 * b] if b < 0.05 else [0.11 + 0.9 * b]
    for label, value in (("productive_seed", 1.0), ("10pct", 1.21), ("near_threshold", 0.06),
                         ("below_threshold", 0.04), ("near_zero", 1e-6)):
        cases.append(Case(f"regime-switch seed={label}", ("biomass",), regime, [1.1], [value],
                          family="regime_switch", q=0.9, branches=[[1.1], [0.02]],
                          note="productive branch 1.1, washout branch 0.02"))

    # Flat plateau: g(x) = x - c (x - x*)^3, so |g(x)-x| is tiny while x is still far from x*.
    def plateau(x: list[float]) -> list[float]:
        return [x[0] - 0.01 * (x[0] - 1.0) ** 3]
    for label in ("10pct", "100pct"):
        cases.append(Case(f"plateau seed={label}", ("mass_flow_kg_s",), plateau, [1.0], [SEEDS[label]],
                          family="small_step_far", q=1.0, note="sublinear: g'(x*) = 1"))
    return cases


def all_cases() -> list[Case]:
    return linear_cases() + nonlinear_cases() + noisy_cases()


def _fixed_point_scalar(g: Callable[[list[float]], list[float]], x: float) -> float:
    for _ in range(10_000):
        x = g([x])[0]
    return x


def _slope(g: Callable[[list[float]], list[float]], x: float, h: float = 1e-6) -> float:
    return (g([x + h])[0] - g([x - h])[0]) / (2 * h)


def true_error(case: Case, tear: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Distance of the returned iterate from the nearest true fixed point, in tolerance units and relative."""
    x = read(tear, case.names)
    best: dict[str, Any] | None = None
    for branch in case.branches:
        normalized = max(abs(v - s) / mixed.tolerance(name, s) for v, s, name in zip(x, branch, case.names, strict=True))
        relative = max(abs(v - s) / max(abs(s), 1e-12) for v, s in zip(x, branch, strict=True))
        if best is None or normalized < best["normalized"]:
            best = {"normalized": normalized, "relative": relative, "branch": branch}
    assert best is not None
    best["intended_branch"] = best["branch"] == case.x_star
    return best


def run_case(case: Case, solve: Callable[..., dict[str, Any]], **settings: Any) -> dict[str, Any]:
    initial = state(dict(zip(case.names, case.seed, strict=True)))
    started = time.perf_counter()
    result = solve(initial, case.evaluate, **settings)
    wall = time.perf_counter() - started
    err = true_error(case, result["iterate"])
    history = result.get("history", [])
    converged = result["status"] == "completed"
    reported = history[-1]["max_normalized_residual"] if history else math.nan
    return {"case": case.name, "family": case.family, "q": case.q, "status": result["status"],
            "reason": result["reason"], "iterations": len(history), "reported_residual": reported,
            "true_error_tol_units": err["normalized"], "true_error_rel": err["relative"],
            "branch": err["branch"], "intended_branch": err["intended_branch"],
            # Claimed converged, but more than ten tolerances from every true fixed point.
            "false_convergence": converged and err["normalized"] > 10,
            # Declared non-converged although the returned iterate already met the tolerance.
            "false_nonconvergence": (not converged) and err["normalized"] <= 1,
            "min_omega": min((row.get("omega", 1.0) for row in history), default=1.0),
            "wall_s": wall, "result_extra": {k: result[k] for k in ("diagnostics",) if k in result}}


def run_matrix(solve: Callable[..., dict[str, Any]], **settings: Any) -> list[dict[str, Any]]:
    return [run_case(case, solve, **settings) for case in all_cases()]
