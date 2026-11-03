"""Engine-neutral tear solver for mixed Jarvis/DWSIM recycles (spec 183).

The controller iterates x_{k+1} from x_k and g(x_k) on the flattened tear state. Every method shares one convergence
rule: a solve is `converged` only when the step residual |g(x)-x| is within tolerance AND a validated estimate of the
distance to the fixed point is within tolerance. A small step alone never converges a near-neutral recycle.

The fixed-point error estimate is the size of the estimated Newton step (I - g')^-1 (g(x) - x):
- direct substitution and Wegstein estimate the contraction from secants along the last step and are valid only when
  two consecutive secant estimates agree (a locally linear map);
- Broyden uses its inverse-Jacobian approximation, valid after one accepted secant update.
Accelerated methods additionally need two agreeing vector secants, which also bounds per-variable Wegstein slopes
that miss coupling. These are labelled estimates, never applied when the secant information is missing or
inconsistent. Anderson acceleration was qualified against the same matrix and not adopted: it was never better than
Broyden on these small tear vectors and certified a noise-dominated near-neutral map before the secant cross-check.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

METHODS = ("direct_substitution", "wegstein", "broyden")
DEFAULT_MAX_ITERATIONS = 25
MAX_ITERATIONS_LIMIT = 5000
CULTURE_FIELDS = ("biomass", "nitrogen", "phosphorus", "oxygen", "dic", "salinity")
STATE_FIELDS = ("temperature_K", "pressure_Pa", "mass_flow_kg_s")
DEFAULT_TOLERANCES = {"mass_flow_rel": 1e-5, "temperature_abs": 0.01, "pressure_rel": 1e-6,
                      "mass_fraction_abs": 1e-7, "culture_rel": 1e-5}
TOLERANCE_BOUNDS = {"mass_flow_rel": (1e-10, 1e-2), "temperature_abs": (1e-6, 1.0),
                    "pressure_rel": (1e-12, 1e-2), "mass_fraction_abs": (1e-12, 1e-3),
                    "culture_rel": (1e-10, 1e-2)}
# legacy: 1e-6 x total feed flow (1e-3 before a HeatExchanger) with zero culture, as before 183;
# feed: total feed flow with the culture of the culture-carrying feed (avoids seeding the washout branch).
SEED_MODES = ("legacy", "feed")
# Explicit per-Recycle seed values; culture values use the internal mass-specific basis (kg per kg carrier).
SEED_FIELDS = ("mass_flow_kg_s", "temperature_K", "pressure_Pa", *CULTURE_FIELDS)
# Secant estimates agree when their (1 - q) differ by at most this fraction.
AGREEMENT = 0.5
# Stop early only when the estimated need is clearly beyond the budget: early estimates on nonlinear maps are rough.
BUDGET_MARGIN = 2.0


@dataclass(frozen=True)
class Settings:
    method: str = "direct_substitution"
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    damping: float = 1.0
    tolerances: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TOLERANCES))
    wegstein_bounds: tuple[float, float] = (-5.0, 0.0)
    max_step_ratio: float = 1e5
    stop_when_budget_insufficient: bool = True
    wall_s: float = 90.0
    seed_mode: str = "legacy"
    seeds: dict[str, dict[str, float]] = field(default_factory=dict)

    def tolerance(self, name: str, value: float | None) -> float:
        t = self.tolerances
        if name == "mass_flow_kg_s":
            return t["mass_flow_rel"] * max(abs(value or 0.0), 1e-6)
        if name == "temperature_K":
            return t["temperature_abs"]
        if name == "pressure_Pa":
            return t["pressure_rel"] * max(abs(value or 0.0), 1.0)
        if name.startswith("mass_fraction."):
            return t["mass_fraction_abs"]
        return t["culture_rel"] * abs(value or 0.0) + 1e-12

    def record(self) -> dict[str, Any]:
        row: dict[str, Any] = {"method": self.method, "max_iterations": self.max_iterations,
                               "damping": self.damping, "tolerances": dict(self.tolerances),
                               "stop_when_budget_insufficient": self.stop_when_budget_insufficient,
                               "wall_s": self.wall_s, "seed_mode": self.seed_mode,
                               "seeds": copy.deepcopy(self.seeds)}
        if self.method == "wegstein":
            row["wegstein_bounds"] = list(self.wegstein_bounds)
        if self.method == "broyden":
            row["max_step_ratio"] = self.max_step_ratio
        return row


def parse_settings(raw: dict[str, Any] | None) -> Settings:
    """Validate operator solver settings; absent fields keep the legacy defaults. Raises ValueError."""
    raw = dict(raw or {})
    unknown = set(raw) - {"method", "max_iterations", "damping", "tolerances", "wegstein_bounds",
                          "max_step_ratio", "stop_when_budget_insufficient", "wall_s", "seed_mode", "seeds"}
    if unknown:
        raise ValueError(f"unknown solver settings: {', '.join(sorted(unknown))}")
    method = raw.get("method", "direct_substitution")
    if method not in METHODS:
        raise ValueError(f"solver method must be one of {', '.join(METHODS)}")
    iterations = raw.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    if isinstance(iterations, bool) or not isinstance(iterations, int) or not 1 <= iterations <= MAX_ITERATIONS_LIMIT:
        raise ValueError(f"max_iterations must be an integer in 1..{MAX_ITERATIONS_LIMIT}")
    damping = _finite(raw.get("damping", 1.0), "damping")
    if not 0.05 <= damping <= 1.0:
        raise ValueError("damping must be in 0.05..1")
    tolerances = dict(DEFAULT_TOLERANCES)
    for name, value in (raw.get("tolerances") or {}).items():
        if name not in TOLERANCE_BOUNDS:
            raise ValueError(f"unknown tolerance {name}")
        low, high = TOLERANCE_BOUNDS[name]
        number = _finite(value, f"tolerance {name}")
        if not low <= number <= high:
            raise ValueError(f"tolerance {name} must be in {low:g}..{high:g}")
        tolerances[name] = number
    bounds = tuple(_finite(value, "wegstein_bounds") for value in raw.get("wegstein_bounds", (-5.0, 0.0)))
    if len(bounds) != 2 or not -1000.0 <= bounds[0] <= bounds[1] <= 0.5:
        raise ValueError("wegstein_bounds must be [low, high] with -1000 <= low <= high <= 0.5")
    ratio = _finite(raw.get("max_step_ratio", 1e5), "max_step_ratio")
    if not 10.0 <= ratio <= 1e8:
        raise ValueError("max_step_ratio must be in 10..1e8")
    stop = raw.get("stop_when_budget_insufficient", True)
    if not isinstance(stop, bool):
        raise ValueError("stop_when_budget_insufficient must be a boolean")
    wall = _finite(raw.get("wall_s", 90.0), "wall_s")
    if not 10.0 <= wall <= 600.0:
        raise ValueError("wall_s must be in 10..600")
    seed_mode = raw.get("seed_mode", "legacy")
    if seed_mode not in SEED_MODES:
        raise ValueError(f"seed_mode must be one of {', '.join(SEED_MODES)}")
    seeds: dict[str, dict[str, float]] = {}
    raw_seeds = raw.get("seeds") or {}
    if not isinstance(raw_seeds, dict) or len(raw_seeds) > 16:
        raise ValueError("seeds must map at most 16 Recycle tags to seed values")
    for tag, values in raw_seeds.items():
        if not isinstance(tag, str) or not isinstance(values, dict) or not values:
            raise ValueError("each seed maps a Recycle tag to seed values")
        row: dict[str, float] = {}
        for name, value in values.items():
            if name not in SEED_FIELDS:
                raise ValueError(f"seed field {name} is not supported; use {', '.join(SEED_FIELDS)}")
            number = _finite(value, f"seed {tag}.{name}")
            if number < 0 or (name in ("temperature_K", "pressure_Pa") and number <= 0):
                raise ValueError(f"seed {tag}.{name} must be positive")
            row[name] = number
        seeds[tag] = row
    return Settings(method=method, max_iterations=iterations, damping=damping, tolerances=tolerances,
                    wegstein_bounds=(bounds[0], bounds[1]), max_step_ratio=ratio,
                    stop_when_budget_insufficient=stop, wall_s=wall, seed_mode=seed_mode, seeds=seeds)


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def flatten(state: dict[str, Any]) -> dict[str, Any]:
    row = {name: state.get(name) for name in STATE_FIELDS}
    row.update({"mass_fraction." + name: value for name, value in state.get("mass_fractions", {}).items()})
    row.update({name: state.get("culture", {}).get(name) for name in CULTURE_FIELDS})
    return row


def residual(settings: Settings, guess: dict[str, Any], output: dict[str, Any]) -> tuple[float, str, dict[str, float]]:
    fields: dict[str, float] = {}
    for name in sorted(set(guess) | set(output)):
        a, b = guess.get(name), output.get(name)
        if a is None and b is None:
            normalized = 0.0
        elif a is None or b is None:
            normalized = math.inf
        else:
            normalized = abs(float(b) - float(a)) / settings.tolerance(name, float(a))
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


def _keys(guess: dict[str, dict[str, Any]], output: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """Iterated coordinates: numeric on both sides. Fields missing on one side are copied, never iterated."""
    keys = []
    for tag in sorted(output):
        before, after = flatten(guess[tag]), flatten(output[tag])
        for name in sorted(set(before) | set(after)):
            if before.get(name) is not None and after.get(name) is not None:
                keys.append((tag, name))
    return keys


def _vector(tear: dict[str, dict[str, Any]], keys: list[tuple[str, str]]) -> np.ndarray:
    return np.array([float(flatten(tear[tag])[name]) for tag, name in keys], dtype=float)


def _assign(base: dict[str, dict[str, Any]], output: dict[str, dict[str, Any]], keys: list[tuple[str, str]],
            values: np.ndarray) -> tuple[dict[str, dict[str, Any]], bool]:
    """Write iterated values back; project onto physical bounds. Returns (state, projected)."""
    updated = copy.deepcopy(base)
    projected = False
    for (tag, name), value in zip(keys, values, strict=True):
        value = float(value)
        low, high = _bounds(name)
        if value < low or value > high:
            projected, value = True, min(max(value, low), high)
        if name.startswith("mass_fraction."):
            updated[tag]["mass_fractions"][name.split(".", 1)[1]] = value
        elif name in STATE_FIELDS:
            updated[tag][name] = value
        else:
            updated[tag]["culture"][name] = value
    for tag, output_state in output.items():
        # Non-iterated fields (a None on either side) take the evaluated value, as the legacy controller did.
        before, after = flatten(base[tag]), flatten(output_state)
        for name in CULTURE_FIELDS:
            if before.get(name) is None or after.get(name) is None:
                updated[tag]["culture"][name] = after.get(name)
        for name, value in output_state.get("mass_fractions", {}).items():
            if base[tag]["mass_fractions"].get(name) is None:
                updated[tag]["mass_fractions"][name] = value
        updated[tag]["culture"]["ph"] = output_state.get("culture", {}).get("ph")
        fractions = updated[tag]["mass_fractions"]
        total, target = sum(fractions.values()), sum(output_state.get("mass_fractions", {}).values())
        if fractions and total > 0 and target > 0 and abs(total - target) > 1e-12:
            projected = True
            for name in fractions:
                fractions[name] *= target / total
    return updated, projected


def classify(q: float | None) -> str:
    if q is None:
        return "unknown"
    if q >= 1.0:
        return "non_contractive"
    if q < -0.2:
        return "oscillatory"
    if q < 0.5:
        return "fast"
    if q < 0.9:
        return "moderate"
    if q < 0.99:
        return "slow"
    return "near_neutral"


class _Estimator:
    """Secant contraction along the last step: q = 1 + (d . y) / (d . d), y = F_{k+1} - F_k, d = x_{k+1} - x_k."""

    def __init__(self) -> None:
        self.q: list[float] = []

    def reset(self) -> None:
        self.q.clear()

    def add(self, step: np.ndarray, delta_f: np.ndarray) -> float | None:
        denom = float(step @ step)
        if denom <= 0 or not math.isfinite(denom):
            return None
        q = 1.0 + float(step @ delta_f) / denom
        if not math.isfinite(q):
            return None
        self.q.append(q)
        return q

    def stable(self) -> float | None:
        if len(self.q) < 2:
            return None
        a, b = 1.0 - self.q[-2], 1.0 - self.q[-1]
        if a <= 0 or b <= 0:
            return None
        return self.q[-1] if abs(a - b) <= AGREEMENT * max(a, b) else None


def solve(initial: dict[str, dict[str, Any]], evaluate: Any, *, settings: Settings | None = None,
          before_iteration: Any = None, on_history: Any = None) -> dict[str, Any]:
    """Iterate the tear with the configured method. evaluate(x_k, k) returns g(x_k) keyed by tear tag."""
    settings = settings or Settings()
    method = settings.method
    guess = copy.deepcopy(initial)
    best, best_norm = copy.deepcopy(guess), math.inf
    omega = settings.damping
    growth_events = resets = 0
    history: list[dict[str, Any]] = []
    last: Any = None
    last_input: dict[str, dict[str, Any]] | None = None
    restarted = False
    status, reason = "unconverged", "max_iterations"
    estimator = _Estimator()
    keys: list[tuple[str, str]] | None = None
    scale: np.ndarray | None = None
    prev_u: np.ndarray | None = None
    prev_f: np.ndarray | None = None
    prev_gu: np.ndarray | None = None
    slopes: dict[int, list[float]] = {}
    inverse: np.ndarray | None = None
    diagnostics: dict[str, Any] = {}
    limit = settings.max_iterations
    updates = 0  # accepted secant (Broyden) updates since the last reset
    last_delta: np.ndarray | None = None  # the last accelerated step, for backtracking
    halvings = 0
    for iteration in range(1, limit + 1):
        stop_reason = before_iteration(iteration) if before_iteration is not None else None
        if stop_reason:
            reason = stop_reason
            if last_input is not None:
                guess = copy.deepcopy(best) if restarted else last_input
            break
        last_input = copy.deepcopy(guess)
        last = evaluate(copy.deepcopy(guess), iteration)
        per_tear = {tag: residual(settings, flatten(guess[tag]), flatten(output)) for tag, output in last.items()}
        max_residual, worst_tag = max(((item[0], tag) for tag, item in per_tear.items()), default=(0.0, ""))
        row: dict[str, Any] = {
            "iteration": iteration, "method": method, "omega": omega,
            "max_normalized_residual": max_residual, "worst_tear": worst_tag,
            "worst_field": per_tear[worst_tag][1] if worst_tag else "",
            "residuals": {tag: residual_values(flatten(guess[tag]), flatten(output)) for tag, output in last.items()},
            "normalized_residuals": {tag: item[2] for tag, item in per_tear.items()}, "events": []}
        current_keys = _keys(guess, last)
        u_phys, g_phys = _vector(guess, current_keys), _vector(last, current_keys)
        if not (np.all(np.isfinite(u_phys)) and np.all(np.isfinite(g_phys))):
            row["events"].append("non_finite_evaluation")
            history.append(row)
            if on_history is not None:
                on_history(row)
            reason = "non_finite_evaluation"
            guess = copy.deepcopy(best) if math.isfinite(best_norm) else last_input
            break
        if keys != current_keys:
            # A changed coordinate pattern invalidates every secant: restart the acceleration memory.
            keys = current_keys
            scale = np.array([settings.tolerance(name, max(abs(a), abs(b)))
                              for (_tag, name), a, b in zip(keys, u_phys, g_phys, strict=True)], dtype=float)
            prev_u = prev_f = prev_gu = inverse = None
            slopes = {}
            updates = 0
            estimator.reset()
            if iteration > 1:
                row["events"].append("coordinates_changed")
        assert scale is not None
        u, gu = u_phys / scale, g_phys / scale
        f = gu - u
        q_secant = None
        if prev_u is not None and prev_f is not None and not restarted:
            q_secant = estimator.add(u - prev_u, f - prev_f)
        q_stable = estimator.stable()
        newton, valid = _estimate(method, f, u, prev_u, prev_gu, gu, q_stable, slopes, inverse, updates,
                                  restarted)
        if valid and method != "direct_substitution" and newton is not None and f.size:
            # Required cross-check: the vector secant sees coupling that per-variable slopes miss, and secants that
            # disagree (evaluation noise, a regime change) mean the local map is not known well enough to certify.
            if q_stable is None:
                valid = False
            else:
                secant = f / (1.0 - q_stable)
                newton = np.where(np.abs(secant) > np.abs(newton), secant, newton)
        current_tol = np.array([settings.tolerance(name, value) for (_tag, name), value in zip(keys, u_phys, strict=True)])
        estimate = float(np.max(np.abs(newton) * scale / current_tol)) if valid and newton is not None and f.size else (
            0.0 if valid else None)
        exact = bool(f.size == 0 or not np.any(f))
        row.update({"q_hat": q_secant, "q_hat_stable": q_stable, "classification": classify(q_stable),
                    "estimated_error_normalized": estimate, "estimate_valid": valid})
        history.append(row)
        def emit_history(completed_row: dict[str, Any]) -> None:
            # Method steps can add events below; observers must see the finished row.
            if on_history is not None:
                on_history(completed_row)
        if max_residual <= 1 and (exact or (valid and estimate is not None and estimate <= 1)):
            status, reason = "completed", "converged"
            emit_history(row)
            break
        # Direct substitution keeps the legacy residual measure; accelerated methods measure growth in the fixed
        # solve scale, because tolerances relative to a moving iterate inflate near a zero bound.
        measure = max_residual if method == "direct_substitution" else (float(np.max(np.abs(f))) if f.size else 0.0)
        growth_limit = 1.5 if method == "direct_substitution" else 4.0
        if measure > growth_limit * best_norm and math.isfinite(best_norm):
            growth_events += 1
            limit_events = 4
            if growth_events >= limit_events:
                reason = "damping_exhausted" if method == "direct_substitution" else "acceleration_breakdown"
                guess = last_input
                emit_history(row)
                break
            if method == "direct_substitution":
                omega = max(omega / 2, 0.125)
                row["events"].append("damping_reduced")
            elif last_delta is not None and prev_u is not None and halvings < 3:
                # Backtrack: retry from the last accepted point with half the step. The secant memory is kept;
                # the next evaluation pairs the accepted point with the shorter trial.
                halvings += 1
                last_delta = 0.5 * last_delta
                guess, _projected = _assign(guess, last, keys, (prev_u + last_delta) * scale)
                row["events"].append("step_halved")
                growth_events -= 1  # a halving is a line search, not a failure of the method
                emit_history(row)
                continue
            else:
                resets += 1
                omega = max(omega / 2, 0.125)
                row["events"].append("acceleration_reset")
            last_delta, halvings = None, 0
            guess = copy.deepcopy(best)
            restarted = True
            prev_u = prev_f = prev_gu = inverse = None
            slopes = {}
            updates = 0
            estimator.reset()
            emit_history(row)
            continue
        halvings = 0
        if measure < best_norm:
            best_norm = measure
            best = copy.deepcopy(guess)
        if (settings.stop_when_budget_insufficient and method == "direct_substitution" and q_stable is not None
                and estimate is not None):
            needed = required_iterations(q_stable, max_residual, omega)
            if needed is not None and needed > BUDGET_MARGIN * (limit - iteration):
                diagnostics["required_iterations"] = needed
                reason = "iteration_budget_insufficient"
                emit_history(row)
                break
        if iteration == limit:
            reason = "max_iterations"
            emit_history(row)
            break
        restarted = False
        step = _step(method, f, u, gu, prev_u, prev_gu, omega, slopes, settings, inverse, prev_f, row)
        inverse = step["inverse"]
        updates += int(step.get("updated", False))
        delta = step["delta"]
        if method != "direct_substitution":
            if q_secant is not None and q_secant >= 1.0:
                # Locally expansive map: a secant/Newton step may run to a non-physical root. Take the damped
                # direct step this iteration (the secant memory is kept) and say so.
                delta = omega * f
                row["events"].append("direct_step_locally_expansive")
            delta = _to_boundary(delta, u, keys, scale, row)
        last_delta = delta
        new_u = u + delta
        guess, projected = _assign(guess, last, keys, new_u * scale)
        if projected:
            row["events"].append("projected_to_bounds")
        prev_u, prev_f, prev_gu = u, f, gu
        emit_history(row)
    final_q = next((row.get("q_hat_stable") for row in reversed(history) if row.get("q_hat_stable") is not None), None)
    last_row = history[-1] if history else {}
    diagnostics.update({
        "method": method, "q_hat": final_q, "q_hat_label": "estimate (secant along last steps)",
        "classification": classify(final_q),
        "estimated_error_normalized": last_row.get("estimated_error_normalized"),
        "estimate_valid": bool(last_row.get("estimate_valid")),
        "max_iterations": limit, "iterations": len(history), "growth_events": growth_events,
        "acceleration_resets": resets})
    if final_q is not None and status != "completed":
        needed = required_iterations(final_q, last_row.get("max_normalized_residual") or 0.0, 1.0)
        if needed is not None:
            diagnostics["direct_substitution_iterations_required"] = needed
        diagnostics["recommendation"] = _recommend(final_q, method, needed, limit)
    return {"iterate": guess, "last": last, "last_input": last_input, "status": status, "reason": reason,
            "history": history, "omega": omega, "growth_events": growth_events, "diagnostics": diagnostics}


def required_iterations(q: float, residual_norm: float, omega: float) -> int | None:
    """Direct-substitution iterations until |x - x*| <= tol on a linear map with contraction q (an estimate)."""
    effective = 1.0 - omega * (1.0 - q)
    if not 0.0 < effective < 1.0 or residual_norm <= 0:
        return None
    target = 1.0 - q  # the residual at which |g(x)-x| / (1-q) reaches one tolerance
    if residual_norm <= target:
        return 0
    return math.ceil(math.log(target / residual_norm) / math.log(effective))


def _recommend(q: float, method: str, needed: int | None, limit: int) -> str:
    if q >= 1.0:
        return "Recycle map is not contracting here: check the flowsheet, improve the seed, or use a dynamic formulation."
    if classify(q) in ("near_neutral", "slow") and method == "direct_substitution":
        text = f"Near-neutral recycle (estimated contraction {q:.4f})"
        if needed is not None:
            text += f": direct substitution needs about {needed} iterations, the budget is {limit}"
        return text + ". Use the Broyden or Wegstein method, a better seed, or a dynamic formulation."
    return "Increase max_iterations, improve the seed, or try another method."


def _estimate(method: str, f: np.ndarray, u: np.ndarray, prev_u: np.ndarray | None, prev_gu: np.ndarray | None,
              gu: np.ndarray, q_stable: float | None, slopes: dict[int, list[float]], inverse: np.ndarray | None,
              updates: int, restarted: bool) -> tuple[np.ndarray | None, bool]:
    """Estimated Newton step (I - g')^-1 (g(x) - x) in scaled units, and whether it may be trusted."""
    if f.size == 0:
        return f, True
    if method == "direct_substitution":
        if q_stable is None:
            return None, False
        return f / (1.0 - q_stable), True
    if method == "wegstein":
        if prev_u is None or prev_gu is None or restarted:
            return None, False
        values = np.zeros(f.size)
        for i in range(f.size):
            if f[i] == 0:
                continue
            du = u[i] - prev_u[i]
            history = slopes.get(i, [])
            if abs(du) <= 1e-300 or not history:
                return None, False
            s = (gu[i] - prev_gu[i]) / du
            a, b = 1.0 - history[-1], 1.0 - s
            if a <= 0 or b <= 0 or abs(a - b) > AGREEMENT * max(a, b):
                return None, False
            values[i] = f[i] / b
        return values, True
    # Broyden: the initial -omega*I carries no information about the map; certify only after a secant update.
    if inverse is None or restarted or updates < 1:
        return None, False
    return -(inverse @ f), True


def _bounds(name: str) -> tuple[float, float]:
    low = {"temperature_K": 1.0, "pressure_Pa": 1.0}.get(name, 0.0)
    return low, (1.0 if name.startswith("mass_fraction.") else math.inf)


def _to_boundary(delta: np.ndarray, u: np.ndarray, keys: list[tuple[str, str]], scale: np.ndarray,
                 row: dict[str, Any]) -> np.ndarray:
    """Per-variable fraction-to-boundary: a variable that would cross a bound moves 90 % of the way to it.

    Shortening the whole step instead stalls a solve seeded near zero, where one tiny variable limits all others.
    """
    limited = delta.copy()
    for i, (_tag, name) in enumerate(keys):
        low, high = (value / scale[i] for value in _bounds(name))
        if u[i] + delta[i] < low and u[i] > low:
            limited[i] = 0.9 * (low - u[i])
        elif u[i] + delta[i] > high and u[i] < high:
            limited[i] = 0.9 * (high - u[i])
    if np.any(limited != delta):
        row["events"].append("step_limited_at_bounds")
    return limited


def _bounded(delta: np.ndarray, f: np.ndarray, settings: Settings, row: dict[str, Any]) -> np.ndarray:
    limit = settings.max_step_ratio * max(float(np.max(np.abs(f))), 1e-300)
    size = float(np.max(np.abs(delta)))
    if not math.isfinite(size):
        row["events"].append("non_finite_step_replaced_by_direct")
        return f.copy()
    if size > limit:
        row["events"].append("step_clipped")
        return delta * (limit / size)
    return delta


def _step(method: str, f: np.ndarray, u: np.ndarray, gu: np.ndarray, prev_u: np.ndarray | None,
          prev_gu: np.ndarray | None, omega: float, slopes: dict[int, list[float]], settings: Settings,
          inverse: np.ndarray | None, prev_f: np.ndarray | None, row: dict[str, Any]) -> dict[str, Any]:
    if method == "direct_substitution" or prev_u is None or prev_gu is None or prev_f is None:
        if method == "broyden":
            inverse = -omega * np.eye(f.size)
        return {"delta": omega * f, "inverse": inverse}
    if method == "wegstein":
        low, high = settings.wegstein_bounds
        delta = omega * f.copy()
        for i in range(f.size):
            du = u[i] - prev_u[i]
            if abs(du) <= 1e-300:
                continue
            s = (gu[i] - prev_gu[i]) / du
            slopes.setdefault(i, []).append(s)
            q = s / (s - 1.0) if s != 1.0 else low
            q = min(max(q, low), high)
            delta[i] = (1.0 - q) * f[i]
        return {"delta": _bounded(delta, f, settings, row), "inverse": None}
    du, df = u - prev_u, f - prev_f
    h = inverse if inverse is not None else -omega * np.eye(f.size)
    hy = h @ df
    denom = float(du @ hy)
    updated = False
    if abs(denom) > 1e-12 * float(np.linalg.norm(du)) * float(np.linalg.norm(hy)) and math.isfinite(denom):
        h = h + np.outer(du - hy, du @ h) / denom
        updated = True
    else:
        row["events"].append("broyden_update_skipped")
    if not np.all(np.isfinite(h)):
        row["events"].append("broyden_reset_non_finite")
        h, updated = -omega * np.eye(f.size), False
    return {"delta": _bounded(-(h @ f), f, settings, row), "inverse": h, "updated": updated}
