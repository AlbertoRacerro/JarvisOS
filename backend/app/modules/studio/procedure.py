"""Studio procedure contract and deterministic compiler (spec 191).

A procedure is data: ordered ``actions`` steps whose compiled form is exactly a spec-172/185/186 ``Schedule``.
Nothing here executes user code; the host models validate every compiled value.
"""

import hashlib
import json
from math import isfinite
from typing import Any, Literal

from pint import DimensionalityError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.modules.process_kernel.units import parse_unit_token, unit_registry
from app.modules.process_stack.dynamic_models import MAX_ACTIONS, MAX_EVENTS, Schedule
from app.modules.studio.models import StudioError, Variable

PROCEDURE_KIND = "studio_procedure_v1"
NOTICE = "Simulation only. Actions change modelled tank inventories. This does not command any equipment. Unqualified."
SCHEDULE_ID_PATTERN = r"^[a-z][a-z0-9_-]{0,39}$"
DEFAULT_SCHEDULE_ID = "procedure"

# Native unit of every numeric field a step may carry (the units of specs 185 and 186).
_STEP_NUMERIC = {"time_s": "s", "every_s": "s", "end_s": "s", "count": "dimensionless"}
_STEP_STRING = {"label"}
_CONDITION_NUMERIC = {"threshold": "kg/m**3", "hysteresis": "kg/m**3", "cooldown_s": "s", "max_fires": "dimensionless"}
_CONDITION_STRING = {"observed", "direction"}
_ACTIONS: dict[str, dict[str, Any]] = {
    "draw": {"tank": None, "volume_m3": "m**3", "loop_fraction": "dimensionless"},
    "separate": {"recovery": "dimensionless", "concentration_factor": "dimensionless", "return_to": None},
    "refill": {"tank": None, "volume_m3": "m**3", "to_volume_m3": "m**3",
               "medium": {"N": "kg/m**3", "O2": "kg/m**3"}},
    "dose": {"tank": None, "nitrogen_kg": "kg", "volume_m3": "m**3"},
    "inoculate": {"tank": None, "volume_m3": "m**3", "culture": {"X": "kg/m**3", "N": "kg/m**3", "O2": "kg/m**3"}},
}
_INTEGER_FIELDS = {"count", "max_fires"}


class ProcedureCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["procedure"]
    name: str = Field(min_length=1, max_length=160)
    engineering_question: str = Field(min_length=1, max_length=2000)
    parameters: list[Variable] = Field(default_factory=list, max_length=32)
    steps: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_EVENTS)


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["series", "fire_time", "balances_closed"]
    channel: str | None = None
    time_s: float | None = None
    phase: Literal["pre", "post", "sample"] = "post"
    fire_index: int | None = Field(default=None, ge=1)
    event_order: int | None = Field(default=None, ge=0)
    value: float | None = None
    tolerance: float = Field(default=1e-9, ge=0)
    relative: bool = False


class ProcedureCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="case", min_length=1, max_length=120)
    parameter_values: dict[str, float] = Field(default_factory=dict)
    draft_id: str = Field(min_length=1, max_length=64)
    scenario_id: str = Field(min_length=1, max_length=128)
    schedule_id: str = Field(default=DEFAULT_SCHEDULE_ID, pattern=SCHEDULE_ID_PATTERN)
    simulate: bool = False
    expect: list[Expectation] = Field(default_factory=list, max_length=64)


class ProcedureTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: list[ProcedureCase] = Field(min_length=1, max_length=16)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def procedure_contract(parameters: list[Variable]) -> dict[str, Any]:
    names = [item.name for item in parameters]
    if len(names) != len(set(names)):
        raise StudioError("studio_procedure_invalid", "Parameter names must be unique.")
    return {"category": "procedure", "parameters": [item.model_dump(exclude_none=True) for item in parameters]}


def procedure_source(steps: list[dict[str, Any]]) -> str:
    return canonical_json({"steps": steps})


def steps_of(source: str) -> list[dict[str, Any]]:
    return json.loads(source)["steps"]


def _is_ref(value: object) -> bool:
    return isinstance(value, dict) and set(value) == {"$param"}


def _bad(path: str, message: str) -> StudioError:
    return StudioError("studio_procedure_invalid", f"{path}: {message}")


def _check_number(value: object, path: str, declared: set[str]) -> None:
    if _is_ref(value):
        name = value["$param"]  # type: ignore[index]
        if not isinstance(name, str) or name not in declared:
            raise StudioError("studio_procedure_param_undeclared", f"{path}: parameter {name!r} is not declared.")
    elif isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise _bad(path, "expected a finite number or {\"$param\": name}.")


def _check_fields(item: object, spec: dict[str, Any], path: str, declared: set[str]) -> None:
    if not isinstance(item, dict):
        raise _bad(path, "expected an object.")
    for key, value in item.items():
        if key not in spec:
            raise _bad(f"{path}.{key}", "unknown field.")
        kind = spec[key]
        if isinstance(kind, dict):
            _check_fields(value, kind, f"{path}.{key}", declared)
        elif kind is None:
            if not isinstance(value, str) or not value:
                raise _bad(f"{path}.{key}", "expected a tag string.")
        else:
            _check_number(value, f"{path}.{key}", declared)


def validate_steps(steps: object, parameters: list[Variable] | list[dict[str, Any]]) -> None:
    """Structural, value-free validation: shape, closed field sets and declared ``$param`` references."""
    declared = {item.name if isinstance(item, Variable) else item["name"] for item in parameters}
    if not isinstance(steps, list) or len(steps) > MAX_EVENTS:
        raise _bad("steps", f"expected a list of at most {MAX_EVENTS} steps.")
    for index, step in enumerate(steps):
        path = f"steps[{index}]"
        if not isinstance(step, dict):
            raise _bad(path, "expected an object.")
        allowed = {*_STEP_NUMERIC, *_STEP_STRING, "condition", "actions"}
        if set(step) - allowed:
            raise _bad(path, f"unknown fields {sorted(set(step) - allowed)}.")
        label = step.get("label")
        if not isinstance(label, str) or not 1 <= len(label) <= 120:
            raise _bad(f"{path}.label", "a label of 1 to 120 characters is required.")
        if "time_s" not in step:
            raise _bad(f"{path}.time_s", "time_s is required.")
        for key in _STEP_NUMERIC:
            if key in step:
                _check_number(step[key], f"{path}.{key}", declared)
        condition = step.get("condition")
        if condition is not None:
            if any(key in step for key in ("every_s", "count", "end_s")):
                raise _bad(path, "a conditioned step repeats through condition.max_fires, not every_s/count/end_s.")
            spec = {**_CONDITION_NUMERIC, **{key: None for key in _CONDITION_STRING}}
            _check_fields(condition, spec, f"{path}.condition", declared)
            for key in _CONDITION_STRING:
                if key in condition and not isinstance(condition[key], str):
                    raise _bad(f"{path}.condition.{key}", "expected a string.")
        actions = step.get("actions")
        if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
            raise _bad(f"{path}.actions", f"expected 1 to {MAX_ACTIONS} actions.")
        for position, action in enumerate(actions):
            apath = f"{path}.actions[{position}]"
            kind = action.get("type") if isinstance(action, dict) else None
            if kind not in _ACTIONS:
                raise _bad(apath, f"type must be one of {sorted(_ACTIONS)}.")
            _check_fields({key: value for key, value in action.items() if key != "type"}, _ACTIONS[kind], apath, declared)


def _quantity_to(value: float, declared_unit: str, native: str, where: str, name: str) -> float:
    source = parse_unit_token(declared_unit)
    target = parse_unit_token(native)
    if source == target:
        return value
    registry = unit_registry()
    try:
        return float((value * registry.Unit(source)).to(registry.Unit(target)).magnitude)
    except DimensionalityError as exc:
        raise StudioError(
            "studio_procedure_unit_mismatch",
            f"Parameter {name} ({declared_unit}) cannot be used for {where} ({native}).",
        ) from exc


class _Resolver:
    def __init__(self, parameters: list[dict[str, Any]], values: dict[str, Any]) -> None:
        by_name = {item["name"]: item for item in parameters}
        unknown = sorted(set(values) - set(by_name))
        if unknown:
            raise StudioError("studio_procedure_param_unknown", f"Values given for undeclared parameters {unknown}.")
        missing = sorted(set(by_name) - set(values))
        if missing:
            raise StudioError("studio_procedure_param_unbound", f"Parameters {missing} have no value.")
        self.by_name = by_name
        self.values: dict[str, float] = {}
        for name, item in by_name.items():
            value = values[name]
            if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
                raise StudioError("studio_procedure_param_invalid", f"Parameter {name} must be a finite number.")
            if item.get("min") is not None and value < item["min"] or item.get("max") is not None and value > item["max"]:
                raise StudioError("studio_procedure_param_out_of_bounds",
                                  f"Parameter {name}={value} {item['unit']} is outside its declared bounds.")
            self.values[name] = float(value)

    def number(self, value: object, native: str, field: str) -> object:
        if not _is_ref(value):
            return value
        name = value["$param"]  # type: ignore[index]
        converted = _quantity_to(self.values[name], self.by_name[name]["unit"], native, field, name)
        if field in _INTEGER_FIELDS:
            if converted != int(converted):
                raise StudioError("studio_procedure_not_integer", f"Parameter {name} must be a whole number for {field}.")
            return int(converted)
        return converted

    def fields(self, item: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in item.items():
            kind = spec[key]
            if isinstance(kind, dict):
                out[key] = self.fields(value, kind)
            elif kind is None:
                out[key] = value
            else:
                out[key] = self.number(value, kind, key)
        return out


def _check_tags(events: list[dict[str, Any]], labels: list[str], document: dict[str, Any]) -> None:
    objects = [item for item in document.get("objects", {}).values() if item.get("kind") == "unit"]
    tanks = {item.get("tag") for item in objects if item.get("type") == "HoldupTank"}
    units = {item.get("tag") for item in objects}
    for event, label in zip(events, labels, strict=True):
        for position, action in enumerate(event["actions"]):
            tag = action["return_to"] if action["type"] == "separate" else action["tank"]
            if tag != "none" and tag not in tanks:
                raise StudioError("EVENT_TARGET_INVALID", f"Step {label!r}: {tag!r} is not a HoldupTank of the draft.",
                                  {"step": label, "action": position, "tank": tag})
        condition = event.get("condition")
        if condition and condition["observed"].rpartition(".")[0] not in units:
            raise StudioError("UNSUPPORTED_MEASUREMENT", f"Step {label!r}: {condition['observed']!r} is not a unit of the draft.",
                              {"step": label, "observed": condition["observed"]})


def compile_procedure(procedure: dict[str, Any], parameter_values: dict[str, Any], draft: dict[str, Any] | None = None,
                      schedule_id: str = DEFAULT_SCHEDULE_ID) -> Schedule:
    """Substitute typed parameters and validate the result as a ``Schedule``. Deterministic; never clips a value.

    ``procedure`` is ``{"parameters": [...], "steps": [...]}``; ``draft`` is the Process draft document used to resolve tags.
    """
    parameters = procedure["parameters"]
    steps = procedure["steps"]
    validate_steps(steps, parameters)
    resolver = _Resolver(parameters, parameter_values)
    events: list[dict[str, Any]] = []
    for step in steps:
        event: dict[str, Any] = {"type": "actions"}
        for key, native in _STEP_NUMERIC.items():
            if key in step:
                event[key] = resolver.number(step[key], native, key)
        if "condition" in step:
            event["condition"] = resolver.fields(step["condition"], {**_CONDITION_NUMERIC, "observed": None, "direction": None})
        event["actions"] = [{"type": action["type"], **resolver.fields(
            {key: value for key, value in action.items() if key != "type"}, _ACTIONS[action["type"]])}
            for action in step["actions"]]
        events.append(event)
    try:
        schedule = Schedule.model_validate({"id": schedule_id, "events": events})
    except ValidationError as exc:
        raise StudioError("studio_procedure_schedule_invalid", "The compiled procedure is not a valid Schedule.",
                          {"errors": json.loads(exc.json(include_url=False, include_context=False))}) from exc
    if draft is not None:
        _check_tags(events, [step["label"] for step in steps], draft)
    return schedule


def schedule_value(schedule: Schedule) -> dict[str, Any]:
    """The authored fields of a compiled schedule (the value written to the draft)."""
    return schedule.model_dump(mode="json", exclude_unset=True)


def compiled_sha256(value: dict[str, Any]) -> str:
    return sha256_text(canonical_json(value))
