"""Incremental simpleFoam log parsing for live residuals (spec 216 contract 4).

``ResidualStream.feed`` accepts arbitrary chunks as the solver writes them and
returns only the iterations completed by that chunk, so a caller can publish
residuals while the solver runs instead of replaying a buffered final log.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

_TIME = re.compile(r"^Time = ([0-9.eE+-]+)\s*$")
_SOLVE = re.compile(r"Solving for (\w+), Initial residual = ([^,\s]+), Final residual = ([^,\s]+), No Iterations (\d+)")
_CONTINUITY = re.compile(r"time step continuity errors : sum local = ([^,\s]+), global = ([^,\s]+), cumulative = ([^,\s]+)")
_EXEC = re.compile(r"^ExecutionTime = ([0-9.eE+-]+) s")
_FATAL = re.compile(r"FOAM FATAL (?:IO )?ERROR|Foam::sigFpe::sigHandler|\bnan\b", re.IGNORECASE)
# Wedge round-off component; never part of the convergence judgement.
IGNORED_FIELDS: frozenset[str] = frozenset()


class LogParseError(ValueError):
    pass


class IterationResiduals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    iteration: int
    initial: dict[str, float]
    final: dict[str, float]
    continuity_local: float | None = None
    continuity_global: float | None = None
    execution_time_s: float | None = None

    @property
    def max_normalized_residual(self) -> float:
        judged = [value for name, value in self.initial.items() if name not in IGNORED_FIELDS]
        return max(judged) if judged else math.inf


class ResidualStream:
    def __init__(self) -> None:
        self._buffer = ""
        self._current: dict[str, object] | None = None
        self.fatal: str | None = None
        self.solver_reported_end = False

    def feed(self, chunk: str) -> list[IterationResiduals]:
        self._buffer += chunk
        *lines, self._buffer = self._buffer.split("\n")
        done: list[IterationResiduals] = []
        for line in lines:
            done.extend(self._line(line.rstrip("\r")))
        return done

    def close(self) -> list[IterationResiduals]:
        """Flush the trailing partial line; an iteration without its ExecutionTime line is
        incomplete (solver stopped or killed mid-iteration) and is dropped, never published."""
        done = self._line(self._buffer) if self._buffer else []
        self._buffer = ""
        self._current = None
        return done

    def _line(self, line: str) -> list[IterationResiduals]:
        if self.fatal is None and _FATAL.search(line):
            self.fatal = line.strip()
        stripped = line.strip()
        if stripped == "End":
            self.solver_reported_end = True
        time = _TIME.match(stripped)
        if time:
            value = float(time.group(1))
            if not math.isfinite(value) or value < 0 or value != int(value):
                raise LogParseError(f"invalid steady iteration label {time.group(1)!r}")
            # A new iteration does not make a prior one complete. Only its
            # ExecutionTime line establishes that the solver finished it.
            self._current = {"iteration": int(value), "initial": {}, "final": {}}
            return []
        if self._current is None:
            return []
        solve = _SOLVE.search(stripped)
        if solve:
            name, initial, final = solve.group(1), _number(solve.group(2)), _number(solve.group(3))
            initial_map = self._current["initial"]
            final_map = self._current["final"]
            assert isinstance(initial_map, dict) and isinstance(final_map, dict)
            # First solve of a field in an iteration carries the outer-loop residual.
            initial_map.setdefault(name, initial)
            final_map[name] = final
            return []
        continuity = _CONTINUITY.search(stripped)
        if continuity:
            self._current["continuity_local"] = _number(continuity.group(1))
            self._current["continuity_global"] = float(continuity.group(2))
            return []
        execution = _EXEC.match(stripped)
        if execution:
            self._current["execution_time_s"] = _number(execution.group(1))
            return [self._finish()]
        return []

    def _finish(self) -> IterationResiduals:
        assert self._current is not None
        row = IterationResiduals.model_validate(self._current)
        self._current = None
        return row


def _number(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise LogParseError(f"invalid residual {text!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise LogParseError(f"non-finite or negative residual {text!r}")
    return value


def parse_log(text: str) -> list[IterationResiduals]:
    stream = ResidualStream()
    rows = stream.feed(text)
    rows.extend(stream.close())
    return rows


def converged(rows: Iterable[IterationResiduals], required_fields: Iterable[str], target: float, *, window: int = 5) -> bool:
    """True when every required field's initial residual is below ``target`` in the last ``window`` iterations."""
    required = tuple(required_fields)
    tail = list(rows)[-window:]
    if len(tail) < window:
        return False
    return all(name in row.initial and row.initial[name] < target for row in tail for name in required)


def required_fields(turbulence_fields: Iterable[str]) -> tuple[str, ...]:
    return ("Ux", "Uy", "Uz", "p", *turbulence_fields)
