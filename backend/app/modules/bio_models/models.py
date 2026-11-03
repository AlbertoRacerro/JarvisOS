from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BioModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetCreate(BioModelRequest):
    name: str = Field(min_length=1, max_length=200)
    species: str = Field(default="", max_length=200)
    strain: str = Field(default="", max_length=200)


class ValueEdit(BioModelRequest):
    expected_revision: str = Field(min_length=1)
    expected_digest: str = Field(min_length=64, max_length=64)
    value: float = Field(allow_inf_nan=False, strict=True)
    unit: str = Field(min_length=1, max_length=128)
    expected_unit: str | None = Field(default=None, max_length=128)
    basis_ref: dict[str, Any] | None = None
    species: str | None = None
    strain: str | None = None
    conditions: str | None = None
    validity_range: dict[str, Any] | None = None


class ValueAction(BioModelRequest):
    expected_revision: str = Field(min_length=1)
    expected_digest: str = Field(min_length=64, max_length=64)
    locator_confirmed: bool = False
    locator_confirmation: str | None = Field(default=None, max_length=500)


class ReviewAction(ValueAction):
    reviewer: str = Field(min_length=1, max_length=200)
    note: str = Field(min_length=1, max_length=8000)


class CardCreate(BioModelRequest):
    name: str = Field(min_length=1, max_length=200)
    parameter_set_id: str = Field(min_length=1)
    factors: dict[str, Any]
    mu_max: dict[str, Any]
    n_source: str = Field(default="NH3", pattern="^(NH3|HNO3)$")


class EvaluationInput(BioModelRequest):
    operating_point: dict[str, Any]
