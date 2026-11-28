"""Pydantic models for deterministic techno-economic analysis (spec 393)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CapexCategory = Literal[
    "equipment",
    "instrumentation",
    "piping",
    "civil",
    "installation",
    "other",
]

OpexSubcategory = Literal[
    "utilities_energy",
    "raw_materials",
    "consumables",
    "labor",
    "maintenance",
    "waste_treatment",
    "insurance_overhead",
    "other",
]

DepreciationMethod = Literal["straight_line", "crf"]


class EconomicsAssumptions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    currency: str = Field(default="EUR", min_length=1, max_length=16)
    default_lifetime_years: float = Field(default=10.0, gt=0.0, le=200.0)
    depreciation_method: DepreciationMethod = "straight_line"
    discount_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    contingency_pct: float = Field(default=15.0, ge=0.0, le=200.0)
    periodic_maintenance_pct: float = Field(default=3.0, ge=0.0, le=100.0)
    operating_hours_per_year: float = Field(default=8000.0, gt=0.0, le=8784.0)


class CapexItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=200)
    category: CapexCategory = "equipment"
    quantity: float = Field(default=1.0, gt=0.0)
    unit_cost: float | None = Field(default=None, ge=0.0)
    installation_factor: float = Field(default=1.0, ge=0.0)
    lifetime_years: float | None = Field(default=None, gt=0.0, le=200.0)
    component_id: str | None = Field(default=None, max_length=128)
    notes: str = Field(default="", max_length=500)


class OpexItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=200)
    subcategory: OpexSubcategory = "other"
    annual_cost: float | None = Field(default=None, ge=0.0)
    unit_rate: float | None = Field(default=None, ge=0.0)
    annual_usage: float | None = Field(default=None, ge=0.0)
    unit_label: str = Field(default="", max_length=64)
    notes: str = Field(default="", max_length=500)


class EconomicsModelInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    assumptions: EconomicsAssumptions = Field(default_factory=EconomicsAssumptions)
    capex_items: list[CapexItem] = Field(default_factory=list)
    opex_items: list[OpexItem] = Field(default_factory=list)


class CapexItemBreakdown(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    category: str
    quantity: float
    unit_cost: float | None
    installation_factor: float
    lifetime_years: float
    purchased_cost: float | None
    installed_cost: float | None
    annual_depreciation: float | None
    component_id: str | None = None
    missing_cost: bool = False


class OpexItemBreakdown(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    subcategory: str
    annual_cost: float | None
    computed_from_rate: bool = False
    missing_cost: bool = False


class CostHotspot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    item_id: str
    name: str
    category: str
    amount: float
    share_pct: float


class MissingCostItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["missing_capex_unit_cost", "missing_opex_cost", "uncosted_component", "empty_model"]
    item_id: str | None = None
    name: str
    message: str


class EconomicsEvaluation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    currency: str
    depreciation_method: DepreciationMethod
    discount_rate: float
    default_lifetime_years: float
    total_capex_purchased: float
    total_capex_installed: float
    contingency_amount: float
    total_capex_with_contingency: float
    annualized_capex: float
    periodic_maintenance_annual: float
    opex_by_subcategory: dict[str, float] = Field(default_factory=dict)
    total_annual_opex: float
    total_annualized_cost: float
    capex_breakdown: list[CapexItemBreakdown] = Field(default_factory=list)
    opex_breakdown: list[OpexItemBreakdown] = Field(default_factory=list)
    capex_hotspots: list[CostHotspot] = Field(default_factory=list)
    opex_hotspots: list[CostHotspot] = Field(default_factory=list)
    opex_subcategory_hotspots: list[CostHotspot] = Field(default_factory=list)
    annualized_hotspots: list[CostHotspot] = Field(default_factory=list)
    hotspot_summary: str
    is_complete: bool
    missing_items: list[MissingCostItem] = Field(default_factory=list)
    headline: str


class WorkspaceEconomicsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    workspace_id: str
    model: EconomicsModelInput
    evaluation: EconomicsEvaluation
    updated_at: str | None = None
