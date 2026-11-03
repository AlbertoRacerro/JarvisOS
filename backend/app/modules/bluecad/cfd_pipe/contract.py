"""Typed smooth-tube CFD study contract (spec 216 contract 3).

Validation refuses physically or numerically inconsistent studies before any
case file is written: inlet specification, regime/model compatibility, wall
treatment, and the development length needed for a developed measuring section.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.bluecad.cfd_pipe.domain import FluidDomain

CONTRACT_VERSION: Literal["cfd_pipe_study_v1"] = "cfd_pipe_study_v1"
LAMINAR_RE_MAX = 2000.0
TURBULENT_RE_MIN = 4000.0
# Development-length margins applied to the textbook entrance-length estimates.
LAMINAR_ENTRY_COEFF = 0.06
TURBULENT_ENTRY_COEFF = 4.4
DEVELOPMENT_MARGIN = 1.5
MAX_MESH_LEVEL = 4
# Fractions of the tube length at the two developed-section pressure stations.
UPSTREAM_STATION_FRACTION = 0.65
DOWNSTREAM_STATION_FRACTION = 0.85

_STRICT = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

Regime = Literal["laminar", "turbulent_rans"]
TurbulenceModel = Literal["kOmegaSST", "kEpsilon"]
WallTreatment = Literal["resolved", "low_re_resolved", "wall_function"]


class Fluid(BaseModel):
    model_config = _STRICT

    name: str = Field(default="water_20C", pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    density_kg_m3: float = Field(default=998.2, gt=0, le=5000)
    dynamic_viscosity_pa_s: float = Field(default=1.002e-3, gt=0, le=10)

    @property
    def kinematic_viscosity_m2_s(self) -> float:
        return self.dynamic_viscosity_pa_s / self.density_kg_m3


class Inlet(BaseModel):
    """Exactly one of mean velocity or volumetric flow rate."""

    model_config = _STRICT

    mean_velocity_m_s: float | None = Field(default=None, gt=0, le=50)
    flow_rate_m3_s: float | None = Field(default=None, gt=0)
    turbulence_intensity: float = Field(default=0.05, gt=0, le=0.3)
    mixing_length_over_d: float = Field(default=0.07, gt=0, le=0.5)

    @model_validator(mode="after")
    def _exactly_one(self) -> Inlet:
        if (self.mean_velocity_m_s is None) == (self.flow_rate_m3_s is None):
            raise ValueError("specify exactly one of mean_velocity_m_s or flow_rate_m3_s")
        return self


class SolverControls(BaseModel):
    """Iteration budget, convergence target and the solver's wall-clock bound.

    The runner stops the solver at ``wall_time_limit_s`` and reports the run as
    failed; a timed-out run is never measured or published.
    """

    model_config = _STRICT

    max_iterations: int = Field(default=20000, ge=10, le=200000)
    residual_target: float = Field(default=1e-6, gt=0, lt=1e-2)
    wall_time_limit_s: int = Field(default=1800, ge=60, le=172800)


class PipeStudyContract(BaseModel):
    """Steady incompressible flow through the bore of one smooth straight tube."""

    model_config = _STRICT

    contract_version: Literal["cfd_pipe_study_v1"] = CONTRACT_VERSION
    domain: FluidDomain
    fluid: Fluid = Fluid()
    inlet: Inlet
    outlet_pressure_pa: float = 0.0
    regime: Regime
    turbulence_model: TurbulenceModel | None = None
    wall_treatment: WallTreatment
    mesh_level: int = Field(default=1, ge=-2, le=MAX_MESH_LEVEL)
    solver: SolverControls = SolverControls()

    @property
    def diameter_m(self) -> float:
        return self.domain.inner_diameter_m

    @property
    def area_m2(self) -> float:
        return math.pi * self.diameter_m**2 / 4.0

    @property
    def mean_velocity_m_s(self) -> float:
        if self.inlet.mean_velocity_m_s is not None:
            return self.inlet.mean_velocity_m_s
        assert self.inlet.flow_rate_m3_s is not None
        return self.inlet.flow_rate_m3_s / self.area_m2

    @property
    def flow_rate_m3_s(self) -> float:
        return self.mean_velocity_m_s * self.area_m2

    @property
    def reynolds(self) -> float:
        return self.mean_velocity_m_s * self.diameter_m / self.fluid.kinematic_viscosity_m2_s

    @property
    def required_length_m(self) -> float:
        """Entrance length times margin: laminar 0.06 Re D, turbulent 4.4 Re^(1/6) D."""
        if self.regime == "laminar":
            entry = LAMINAR_ENTRY_COEFF * self.reynolds * self.diameter_m
        else:
            entry = TURBULENT_ENTRY_COEFF * self.reynolds ** (1.0 / 6.0) * self.diameter_m
        return DEVELOPMENT_MARGIN * entry

    @model_validator(mode="after")
    def _consistent(self) -> PipeStudyContract:
        if not math.isfinite(self.outlet_pressure_pa):
            raise ValueError("outlet_pressure_pa must be finite")
        re = self.reynolds
        if self.regime == "laminar":
            if self.turbulence_model is not None:
                raise ValueError("laminar regime takes no turbulence model")
            if self.wall_treatment != "resolved":
                raise ValueError("laminar regime requires wall_treatment 'resolved'")
            if re > LAMINAR_RE_MAX:
                raise ValueError(f"laminar regime requested at Re={re:.0f} > {LAMINAR_RE_MAX:.0f}")
        else:
            if self.turbulence_model is None:
                raise ValueError("turbulent_rans regime requires a turbulence model")
            if self.wall_treatment == "resolved":
                raise ValueError("turbulent_rans requires 'low_re_resolved' or 'wall_function'")
            if self.turbulence_model == "kEpsilon" and self.wall_treatment != "wall_function":
                raise ValueError("standard kEpsilon is valid only with wall functions")
            if re < TURBULENT_RE_MIN:
                raise ValueError(f"turbulent_rans requested at Re={re:.0f} < {TURBULENT_RE_MIN:.0f}")
        if UPSTREAM_STATION_FRACTION * self.domain.length_m < self.required_length_m:
            raise ValueError(
                f"tube length {self.domain.length_m:.4g} m puts the upstream pressure station at "
                f"{UPSTREAM_STATION_FRACTION:.0%} of the length, before the "
                f"{self.required_length_m:.4g} m needed for developed flow"
            )
        return self


def contract_digest(contract: PipeStudyContract) -> str:
    payload = json.dumps(contract.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
