"""Strict, bounded content models for Process dynamic scenarios (spec 172)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_DURATION_S = 120 * 86400
MAX_OUTPUT_POINTS = 20_000
MAX_EVENTS = 1_000
MAX_CONTROLLERS = 16
MAX_PARTICIPATING_UNITS = 12
MAX_PARTICIPATING_PBRS = 8
MAX_DWSIM_SAMPLES = 200
MIN_CADENCE_S = 60
MAX_ACTIONS = 16


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class DrawAction(StrictModel):
    """Remove culture from a tank at its current concentrations (spec 185)."""

    type: Literal["draw"]
    tank: str
    volume_m3: float | None = Field(default=None, gt=0)
    loop_fraction: float | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def one_amount(self) -> DrawAction:
        if (self.volume_m3 is None) == (self.loop_fraction is None):
            raise ValueError("draw requires exactly one of volume_m3 or loop_fraction")
        return self


class SeparateAction(StrictModel):
    """Split the culture drawn earlier in the same event into concentrate and clarified liquid."""

    type: Literal["separate"]
    recovery: float = Field(gt=0, le=1)
    concentration_factor: float = Field(gt=1)
    return_to: str


class RefillAction(StrictModel):
    """Add biomass-free medium to a tank."""

    type: Literal["refill"]
    tank: str
    volume_m3: float | None = Field(default=None, gt=0)
    to_volume_m3: float | None = Field(default=None, gt=0)
    medium: dict[str, float]

    @model_validator(mode="after")
    def one_amount(self) -> RefillAction:
        if (self.volume_m3 is None) == (self.to_volume_m3 is None):
            raise ValueError("refill requires exactly one of volume_m3 or to_volume_m3")
        if set(self.medium) != {"N", "O2"} or any(value < 0 for value in self.medium.values()):
            raise ValueError("refill medium requires nonnegative N and O2 in kg/m3")
        return self


class DoseAction(StrictModel):
    """Add nitrogen mass to a tank, optionally in a biomass- and oxygen-free volume."""

    type: Literal["dose"]
    tank: str
    nitrogen_kg: float = Field(gt=0)
    volume_m3: float = Field(default=0.0, ge=0)


class InoculateAction(StrictModel):
    """Add culture to a tank."""

    type: Literal["inoculate"]
    tank: str
    volume_m3: float = Field(gt=0)
    culture: dict[str, float]

    @model_validator(mode="after")
    def full_culture(self) -> InoculateAction:
        if set(self.culture) != {"X", "N", "O2"} or any(value < 0 for value in self.culture.values()):
            raise ValueError("inoculate culture requires nonnegative X, N and O2 in kg/m3")
        return self


Action = Annotated[DrawAction | SeparateAction | RefillAction | DoseAction | InoculateAction,
                   Field(discriminator="type")]


STATE_CHANNELS = ("X", "N", "O2", "loop_X", "loop_N")


class StateCondition(StrictModel):
    """Root-found trigger of an ``actions`` event (spec 186); the event's ``time_s`` is the earliest time.

    ``observed`` is ``<unit>.X|N|O2`` or ``<unit>.loop_X|loop_N`` (volume-weighted mean of that unit's culture
    loop), and ``threshold`` is in kg/m3.
    """

    observed: str = Field(min_length=3, max_length=80)
    direction: Literal["above", "below"]
    threshold: float
    hysteresis: float = Field(default=0.0, ge=0)
    cooldown_s: float = Field(default=0.0, ge=0)
    max_fires: int = Field(default=1, ge=1, le=MAX_EVENTS)

    @field_validator("observed")
    @classmethod
    def known_channel(cls, value: str) -> str:
        unit, _, channel = value.rpartition(".")
        if not unit or channel not in STATE_CHANNELS:
            raise ValueError(f"observed must be <unit>.<{'|'.join(STATE_CHANNELS)}>")
        return value


class ScheduleEvent(StrictModel):
    type: Literal["inoculation", "feed_change", "setpoint_change", "harvest", "dilution", "actions"]
    time_s: float = Field(ge=0)
    unit: str | None = None
    stream: str | None = None
    target: str | None = None
    value: float | str | dict[str, Any] | None = None
    value_unit: Literal["kg/m3", "m3/s", "1"] | None = None
    fraction: float | None = Field(default=None, ge=0, le=1)
    medium: dict[str, float] | None = None
    observed: str | None = None
    threshold: float | None = None
    threshold_unit: Literal["kg/m3"] | None = None
    direction: Literal["above", "below"] | None = None
    hysteresis: float | None = Field(default=None, ge=0)
    every_s: float | None = Field(default=None, gt=0)
    count: int | None = Field(default=None, ge=1, le=MAX_EVENTS)
    end_s: float | None = Field(default=None, ge=0)
    actions: list[Action] | None = Field(default=None, min_length=1, max_length=MAX_ACTIONS)
    condition: StateCondition | None = None

    @model_validator(mode="after")
    def repeated_bounded(self) -> ScheduleEvent:
        if self.condition is not None:
            if self.type != "actions":
                raise ValueError("only an actions event may carry a state condition")
            if self.every_s is not None or self.count is not None or self.end_s is not None:
                raise ValueError("a state-triggered event repeats through max_fires, not every_s/count/end_s")
            if self.observed is not None or self.threshold is not None:
                raise ValueError("a state condition replaces the 172 observed/threshold fields")
        if (self.type == "actions") != (self.actions is not None):
            raise ValueError("an actions event carries an actions list, and only an actions event does")
        if self.every_s is not None and self.count is None and self.end_s is None:
            raise ValueError("repeated event requires count or end_s")
        if (self.observed is None) != (self.threshold is None):
            raise ValueError("conditional event requires observed and threshold")
        if self.observed is not None and (self.direction is None or self.hysteresis is None):
            raise ValueError("conditional event requires direction and hysteresis")
        if self.observed is not None and self.threshold_unit != "kg/m3":
            raise ValueError("conditional threshold requires kg/m3 unit")
        return self


class Schedule(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    events: list[ScheduleEvent] = Field(default_factory=list, max_length=MAX_EVENTS)


class Controller(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    type: Literal["onoff", "pi", "turbidostat"]
    measurement: str
    unit: str
    actuator: str
    output_unit: Literal["m3/s", "1"]
    cadence_s: float = Field(ge=MIN_CADENCE_S)
    setpoint: float
    lower: float = 0
    upper: float = 1
    hysteresis: float = Field(default=0, ge=0)
    kp: float = 0
    ki: float = 0
    output: float = 0
    direction: Literal["above", "below"] = "above"

    @model_validator(mode="after")
    def output_bounds(self) -> Controller:
        if self.lower > self.upper or not self.lower <= self.output <= self.upper:
            raise ValueError("controller output and bounds are inconsistent")
        if self.actuator.startswith("feed:") and self.output_unit != "m3/s":
            raise ValueError("feed controller output requires m3/s")
        if self.actuator.startswith("splitter:") and (
            self.output_unit != "1" or self.lower < 0 or self.upper > 1
        ):
            raise ValueError("splitter controller output requires a ratio in [0,1]")
        if self.type == "turbidostat" and not self.actuator.startswith("splitter:"):
            raise ValueError("turbidostat output is a harvest ratio and requires a splitter actuator")
        return self


class Scenario(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    units: list[str] = Field(min_length=1, max_length=MAX_PARTICIPATING_UNITS)
    profiles: list[dict[str, str]] = Field(min_length=1, max_length=8)
    forcing_profile_id: str | None = None
    schedule_id: str | None = None
    controllers: list[str] = Field(default_factory=list, max_length=MAX_CONTROLLERS)
    start_utc: str
    end_utc: str
    output_cadence_s: float = Field(ge=MIN_CADENCE_S)
    controller_cadence_s: float = Field(default=300, ge=MIN_CADENCE_S)
    fidelity: Literal["T1"] = "T1"
    solver_method: Literal["BDF", "Adams"] = "BDF"
    rtol: float = Field(default=1e-6, gt=0, lt=1)
    atol: float = Field(default=1e-9, gt=0)
    temperature_source: Literal["sea_temperature", "air_temperature", "unit_mean"]
    par_scale: float = Field(default=1.0, ge=0)
    par_from_ghi_factor: float | None = Field(default=None, gt=0)
    downstream_cadence_s: float | None = Field(default=None, ge=3600)
    downstream_enabled: bool = False
    initial: dict[str, dict[str, float]] = Field(default_factory=dict)
    feed_flows: dict[str, float] = Field(default_factory=dict)
    circulation: dict[str, float] = Field(default_factory=dict)

    @field_validator("start_utc", "end_utc")
    @classmethod
    def utc_aware(cls, value: str) -> str:
        from datetime import datetime

        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamps must include a UTC offset")
        return value

    @model_validator(mode="after")
    def downstream_default(self) -> Scenario:
        if self.downstream_enabled and self.downstream_cadence_s is None:
            self.downstream_cadence_s = 86400.0
        return self
