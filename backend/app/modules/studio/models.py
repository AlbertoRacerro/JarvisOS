"""Studio authoring contracts. User source stays inert in this module."""

import keyword
import re
from math import isfinite
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.process_kernel.units import parse_unit_token

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class Variable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    unit: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    min: float | None = None
    max: float | None = None

    @field_validator("name")
    @classmethod
    def identifier(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value) or keyword.iskeyword(value) or value in {"inputs", "evaluate", "main"}:
            raise ValueError("Variable name must be a Python identifier.")
        return value

    @field_validator("unit")
    @classmethod
    def pint_unit(cls, value: str) -> str:
        # The unit is written into the scaffold docstring, so it must stay one inert line.
        if not value.isprintable() or any(character in value for character in "\"'\\#"):
            raise ValueError("Unit contains unsupported characters.")
        parse_unit_token(value)
        return value

    @model_validator(mode="after")
    def bounds(self) -> "Variable":
        if any(value is not None and not isfinite(value) for value in (self.min, self.max)):
            raise ValueError("Bounds must be finite.")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("Minimum must not exceed maximum.")
        return self


class StudioCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    engineering_question: str = Field(min_length=1, max_length=2000)
    inputs: list[Variable] = Field(min_length=1, max_length=64)
    outputs: list[Variable] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_variables(self) -> "StudioCreate":
        for variables in (self.inputs, self.outputs):
            names = [item.name for item in variables]
            if len(names) != len(set(names)):
                raise ValueError("Variable names must be unique within inputs and outputs.")
        return self


class StudioUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=160)
    engineering_question: str | None = Field(default=None, min_length=1, max_length=2000)
    inputs: list[Variable] | None = None
    outputs: list[Variable] | None = None
    # Process-unit contract parts (spec 190); validated by StudioUnitCreate in the service.
    inlets: list[dict[str, Any]] | None = None
    outlets: list[dict[str, Any]] | None = None
    parameters: list[dict[str, Any]] | None = None
    symbol: str | None = Field(default=None, max_length=32768)
    steps: list[dict[str, Any]] | None = Field(default=None, max_length=1000)  # procedure steps (spec 191)
    culture_generation: list[str] | None = None
    source: str | None = Field(default=None, max_length=65536)
    annotations: str | None = Field(default=None, max_length=20000)
    assumptions: str | None = Field(default=None, max_length=20000)


class TestCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: dict[str, float]
    expected_outputs: dict[str, float] | None = None
    tolerance: float = Field(default=1e-6, ge=0)

    @model_validator(mode="after")
    def finite_values(self) -> "TestCase":
        values = [*self.inputs.values(), *(self.expected_outputs or {}).values(), self.tolerance]
        if any(isinstance(value, bool) or not isfinite(value) for value in values):
            raise ValueError("Test values must be finite numbers.")
        return self


class TestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: list[TestCase] = Field(min_length=1, max_length=32)


class StudioError(ValueError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None,
                 status: int | None = None) -> None:
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.status = status
        super().__init__(message)


def model_payload(data: StudioCreate) -> dict[str, Any]:
    return {"inputs": [item.model_dump(exclude_none=True) for item in data.inputs],
            "outputs": [item.model_dump(exclude_none=True) for item in data.outputs]}
