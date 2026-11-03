"""Single time/state owner for ODE models: SUNDIALS CVODE via scikit-sundae (103 role: dynamics).

A dynamic model supplies only ``rhs(t, y) -> dy/dt``; CVODE alone advances time
and accepts state. Callers that need discrete actions (harvest, cleaning)
integrate segment by segment and apply the action between calls, so no second
integrator ever owns the same state. Convergence is solver health, not validity.

Optional root functions (spec 186) let CVODE locate state-dependent events to solver accuracy. Every root is
terminal: integration stops at the exact root time, the caller applies its discrete action, and integrates again.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from app.modules.engineering.evaluator_contracts import NumericalDiagnostics

MAX_STATES: Final = 256
MAX_OUTPUT_TIMES: Final = 100_000

Rhs = Callable[[float, tuple[float, ...]], Sequence[float]]
Roots = Callable[[float, tuple[float, ...]], Sequence[float]]


class OdeInputError(ValueError):
    """The problem definition is invalid before any integration is attempted."""


@dataclass(frozen=True, slots=True)
class OdeSolution:
    times: tuple[float, ...]
    states: tuple[tuple[float, ...], ...]
    success: bool
    message: str
    diagnostics: NumericalDiagnostics
    # Set when a terminal root stopped the integration: ``times``/``states`` then end at the root.
    root_time: float | None = None
    root_state: tuple[float, ...] | None = None
    root_indices: tuple[int, ...] = ()


def _finite_vector(values: Sequence[float], label: str) -> tuple[float, ...]:
    vector = tuple(float(value) for value in values)
    if not vector or not all(math.isfinite(value) for value in vector):
        raise OdeInputError(f"{label} must be a non-empty finite vector")
    return vector


def integrate_ode(
    rhs: Rhs,
    y0: Sequence[float],
    times: Sequence[float],
    *,
    rtol: float = 1e-6,
    atol: float = 1e-9,
    method: Literal["BDF", "Adams"] = "BDF",
    max_step: float = 0.0,
    roots: Roots | None = None,
    root_directions: Sequence[int] = (),
) -> OdeSolution:
    """Integrate ``dy/dt = rhs(t, y)`` and report states at exactly ``times``.

    Invalid problems raise ``OdeInputError``; solver failures return
    ``success=False`` with the native CVODE message and no states.

    ``roots(t, y)`` returns one value per root function; ``root_directions`` gives each one's accepted slope
    (+1 rising through zero, -1 falling, 0 either). The first root stops the integration: the result then carries
    the requested times up to the root followed by the root time itself, and ``root_time``, ``root_state`` and
    ``root_indices`` (every function that crossed at that instant). Without a root the call behaves as before.
    """
    import numpy as np
    from sksundae.cvode import CVODE

    state0 = _finite_vector(y0, "y0")
    grid = _finite_vector(times, "times")
    if len(state0) > MAX_STATES or not 2 <= len(grid) <= MAX_OUTPUT_TIMES:
        raise OdeInputError(f"at most {MAX_STATES} states and 2..{MAX_OUTPUT_TIMES} output times")
    if any(later <= earlier for earlier, later in zip(grid, grid[1:], strict=False)):
        raise OdeInputError("times must be strictly increasing")
    if not (0.0 < rtol < 1.0 and 0.0 < atol and max_step >= 0.0):
        raise OdeInputError("tolerances must be positive (rtol < 1) and max_step non-negative")
    size = len(state0)
    root_count = len(root_directions)
    if (roots is None) != (root_count == 0) or any(d not in (-1, 0, 1) for d in root_directions):
        raise OdeInputError("roots require one direction (-1, 0 or +1) per root function")

    def native_roots(t: float, y: Any, g: Any) -> None:
        values = roots(float(t), tuple(float(value) for value in y)) if roots else ()
        if len(values) != root_count:
            raise OdeInputError(f"roots returned {len(values)} values for {root_count} functions")
        g[:] = values

    if root_count:
        native_roots.terminal = [True] * root_count  # type: ignore[attr-defined]
        native_roots.direction = [int(d) for d in root_directions]  # type: ignore[attr-defined]

    def native_rhs(t: float, y: Any, yp: Any) -> None:
        derivative = rhs(float(t), tuple(float(value) for value in y))
        if len(derivative) != size:
            raise OdeInputError(f"rhs returned {len(derivative)} derivatives for {size} states")
        yp[:] = derivative

    started = time.perf_counter()
    # scikit-sundae interprets a two-element tspan as an internal-step request. On
    # the installed CVODE build, its normal tstop return is marked unsuccessful in
    # that mode. Request one interior output instead, then expose only caller times.
    requested_grid = grid
    solver_grid = grid if len(grid) != 2 else (grid[0], (grid[0] + grid[1]) / 2.0, grid[1])
    solver = CVODE(native_rhs, method=method, rtol=rtol, atol=atol, max_step=max_step,
                   **({"eventsfn": native_roots, "num_events": root_count} if root_count else {}))
    result = solver.solve(np.asarray(solver_grid, dtype=float), np.asarray(state0, dtype=float))
    elapsed = time.perf_counter() - started
    solver_states = tuple(tuple(float(value) for value in row) for row in result.y)
    rooted = root_count > 0 and result.t_events is not None and len(result.t_events) > 0
    if rooted:
        # CVODE appends the terminal root to the grid points it passed; keep only the caller's own times before it.
        root_time = float(result.t_events[0])
        passed = len(solver_states) - 1
        kept = [j for j in range(passed) if solver_grid[j] in requested_grid]
        times_kept = tuple(float(solver_grid[j]) for j in kept) + (root_time,)
        states_kept = tuple(solver_states[j] for j in kept) + (solver_states[-1],)
        root_indices = tuple(int(i) for i in np.flatnonzero(np.asarray(result.i_events[0])))
        success = bool(result.success) and all(math.isfinite(v) for row in states_kept for v in row)
        return OdeSolution(
            times=times_kept if success else (), states=states_kept if success else (), success=success,
            message=str(result.message),
            diagnostics=NumericalDiagnostics(converged=success, iterations=int(result.nfev), wall_time_s=elapsed),
            root_time=root_time if success else None, root_state=solver_states[-1] if success else None,
            root_indices=root_indices if success else (),
        )
    success = bool(result.success) and len(solver_states) == len(solver_grid) and all(
        math.isfinite(value) for row in solver_states for value in row
    )
    selected = (solver_states[0], solver_states[-1]) if len(requested_grid) == 2 else solver_states
    selected_times = requested_grid
    return OdeSolution(
        times=tuple(selected_times) if success else (),
        states=selected if success else (),
        success=success,
        message=str(result.message),
        # CVODE reports RHS evaluations, the closest native work counter to "iterations".
        diagnostics=NumericalDiagnostics(converged=success, iterations=int(result.nfev), wall_time_s=elapsed),
    )
