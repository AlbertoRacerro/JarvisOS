"""State machine of a root-found culture trigger (spec 186).

The engine owns time and state; a trigger only decides, from the observed value, which root function (if any) is
live and what a root means. With ``s = sign * (observed - threshold)`` (sign +1 for ``above``, -1 for ``below``)
the condition is *satisfied* when ``s >= 0`` and *re-arms* at the band edge ``s <= -band``.

States
------
``pending``    before the earliest time ``time_s``; no root.
``armed``      ``s < 0``; root on ``s`` rising through 0 fires the actions (once the cooldown has elapsed).
``wait_edge``  disarmed; root on ``s + band`` falling through 0 re-arms. No action.
``spent``      ``max_fires`` reached; no root.

Rule for a condition that is already satisfied when it would be (re)armed
-------------------------------------------------------------------------
Triggers fire only on a continuous-time crossing found by the root finder, never on a state that is simply
found satisfied. So when a trigger is evaluated at its earliest time, at a restart after another action, or at
the end of its cooldown and ``s >= 0``, it does **not** fire: it becomes ``wait_edge`` and must first leave the
satisfied side by the full band before it can fire again. After a fire the condition is likewise ``wait_edge``.
This is deterministic and cannot chatter: each fire needs one re-arm root and one crossing root in between.

A crossing that happens inside the cooldown is not a fire: the root is not live during the cooldown, and if the
condition is satisfied when the cooldown ends it is treated as above (``wait_edge``, recorded as suppressed).

The effective band is at least ``GUARD_REL * max(1, |threshold|)``, so the state left by a root can never read as
re-armed (or satisfied) because of solver round-off alone.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

GUARD_REL: Final = 1e-9

State = Any  # a state vector: a tuple from the solver or the engine's numpy array
Observe = Callable[[State], float]


class Trigger:
    def __init__(self, order: int, event: dict[str, Any], observe: Observe) -> None:
        condition = event["condition"]
        self.order, self.event, self.observe = order, event, observe
        self.sign = 1.0 if condition["direction"] == "above" else -1.0
        self.threshold = float(condition["threshold"])
        self.band = max(float(condition["hysteresis"]), GUARD_REL * max(1.0, abs(self.threshold)))
        self.cooldown_s = float(condition["cooldown_s"])
        self.max_fires = int(condition["max_fires"])
        self.earliest_s = float(event["time_s"])
        self.status = "pending"
        self.fires = 0
        self.cooldown_end = 0.0
        self.history: list[dict[str, Any]] = []

    def margin(self, state: State) -> float:
        return self.sign * (self.observe(state) - self.threshold)

    def _note(self, kind: str, time_s: float, state: State) -> None:
        self.history.append({"event": kind, "time_s": float(time_s), "observed_value": float(self.observe(state))})

    def settle(self, time_s: float, state: State) -> None:
        """Classify the trigger at an integration restart; never fires."""
        if self.status == "pending" and time_s >= self.earliest_s:
            self.status = "armed" if self.margin(state) < 0 else "wait_edge"
            self._note("armed_at_start" if self.status == "armed" else "satisfied_at_start", time_s, state)
        elif self.status == "wait_edge" and self.margin(state) <= -self.band:
            self.status = "armed"
            self._note("rearmed", time_s, state)
        elif self.status == "armed" and time_s >= self.cooldown_end and self.margin(state) >= 0:
            self.status = "wait_edge"
            self._note("suppressed", time_s, state)

    @property
    def direction(self) -> int:
        return 1 if self.status == "armed" else -1

    def live(self, time_s: float) -> bool:
        return (self.status == "armed" and time_s >= self.cooldown_end) or self.status == "wait_edge"

    def root_value(self, state: State) -> float:
        return self.margin(state) + (self.band if self.status == "wait_edge" else 0.0)

    def stop_after(self, time_s: float, limit: float) -> float | None:
        """Cooldown end inside (time_s, limit), where the trigger becomes live and must be re-evaluated."""
        if self.status == "armed" and time_s < self.cooldown_end < limit:
            return self.cooldown_end
        return None

    def crossed(self, time_s: float, state: State) -> bool:
        """A root of the live function at ``time_s``. True when it is a fire; False when it only re-arms."""
        if self.status == "wait_edge":
            self.status = "armed"
            self._note("rearmed", time_s, state)
            return False
        self.fires += 1
        self.cooldown_end = time_s + self.cooldown_s
        self.status = "spent" if self.fires >= self.max_fires else "wait_edge"
        return True

    def summary(self) -> dict[str, Any]:
        condition = dict(self.event["condition"])
        return {"order": self.order, "condition": condition, "effective_band": self.band, "fires": self.fires,
                "status": self.status, "history": list(self.history)}
