"""Studio process-unit contracts (spec 190). User source stays inert in this module."""

import keyword
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.studio.models import _IDENTIFIER, StudioError, Variable
from app.modules.studio.symbol import MAX_SYMBOL_BYTES, SymbolError, sanitize_symbol
from app.modules.studio.unit_checks import CULTURE_FIELDS


class Port(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def identifier(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value) or keyword.iskeyword(value):
            raise ValueError("Port name must be a Python identifier.")
        return value

    @field_validator("label")
    @classmethod
    def inert_label(cls, value: str) -> str:
        # The label is written into the scaffold docstring, so it must stay one inert line.
        if not value.isprintable() or any(character in value for character in "\"'\\#"):
            raise ValueError("Label contains unsupported characters.")
        return value


def _unique(items: list[Port] | list[Variable]) -> None:
    names = [item.name for item in items]
    if len(names) != len(set(names)):
        raise ValueError("Names must be unique within ports and parameters.")


def checked_symbol(symbol: str | None) -> str | None:
    if symbol is None or not symbol.strip():
        return None
    try:
        return sanitize_symbol(symbol)
    except SymbolError as exc:
        raise StudioError("studio_symbol_invalid", str(exc)) from exc


class StudioUnitCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["process_unit"]
    name: str = Field(min_length=1, max_length=160)
    engineering_question: str = Field(min_length=1, max_length=2000)
    inlets: list[Port] = Field(min_length=1, max_length=4)
    outlets: list[Port] = Field(min_length=1, max_length=4)
    parameters: list[Variable] = Field(default_factory=list, max_length=32)
    symbol: str | None = Field(default=None, max_length=MAX_SYMBOL_BYTES)
    culture_generation: list[str] = Field(default_factory=list, max_length=len(CULTURE_FIELDS))

    @model_validator(mode="after")
    def valid(self) -> "StudioUnitCreate":
        for items in (self.inlets, self.outlets, self.parameters):
            _unique(items)
        if not set(self.culture_generation) <= set(CULTURE_FIELDS) or len(set(self.culture_generation)) != len(
                self.culture_generation):
            raise ValueError(f"culture_generation must be unique names from {list(CULTURE_FIELDS)}.")
        return self


class UnitStream(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mass_flow_kg_s: float = Field(ge=0)
    temperature_K: float = Field(gt=0)
    pressure_Pa: float = Field(gt=0)
    mass_fractions: dict[str, float]
    culture: dict[str, float | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def finite(self) -> "UnitStream":
        values = [self.mass_flow_kg_s, self.temperature_K, self.pressure_Pa, *self.mass_fractions.values(),
                  *(value for value in self.culture.values() if value is not None)]
        if any(not isfinite(value) for value in values):
            raise ValueError("Stream values must be finite numbers.")
        return self


class ExpectedStream(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mass_flow_kg_s: float | None = None
    temperature_K: float | None = None
    pressure_Pa: float | None = None
    mass_fractions: dict[str, float] | None = None


class UnitTestCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inlets: dict[str, UnitStream]
    params: dict[str, float] = Field(default_factory=dict)
    expected_outlets: dict[str, ExpectedStream] | None = None
    tolerance: float = Field(default=1e-6, ge=0)

    @model_validator(mode="after")
    def finite(self) -> "UnitTestCase":
        if any(isinstance(value, bool) or not isfinite(value) for value in (*self.params.values(), self.tolerance)):
            raise ValueError("Test values must be finite numbers.")
        return self


class UnitTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: list[UnitTestCase] = Field(min_length=1, max_length=32)


def unit_contract(data: StudioUnitCreate) -> dict:
    return {"category": "process_unit",
            "inlets": [item.model_dump() for item in data.inlets],
            "outlets": [item.model_dump() for item in data.outlets],
            "parameters": [item.model_dump(exclude_none=True) for item in data.parameters],
            "symbol": checked_symbol(data.symbol),
            "culture_generation": list(data.culture_generation)}


def unit_scaffold(contract: dict) -> str:
    """Contract-only deterministic source; the user edits the placeholder."""
    lines = ["# Studio process unit. Steady state; parameters use the declared contract units.",
             "def evaluate(inlets: dict, params: dict) -> dict:",
             '    """Return the outlet streams from the inlet streams and parameters.', "",
             "    A stream is a dict: mass_flow_kg_s, temperature_K, pressure_Pa,",
             "    mass_fractions {compound: fraction} and culture {field: kg/kg or None}.",
             '    Return {"outlets": {outlet: stream}}, plus "culture_generation": {field: rate}',
             "    only for the declared generation fields (kg/s, dic in mol/s). The host checks",
             "    mass, compound and culture balances and never clips or renormalizes.", "", "    Inlets:",
             *(f"        {item['name']}: {item['label']}" for item in contract["inlets"]), "    Outlets:",
             *(f"        {item['name']}: {item['label']}" for item in contract["outlets"]), "    Parameters:",
             *(f"        {item['name']} [{item['unit']}]" for item in contract["parameters"]),
             "    Culture generation:",
             f"        {', '.join(contract['culture_generation']) or 'none'}", '    """',
             '    raise NotImplementedError("Complete evaluate before testing")', "",
             "", "def main(inputs: dict) -> dict:",
             '    return evaluate(inputs["inlets"], inputs["params"])', ""]
    return "\n".join(lines)
