"""Shared BLUECAD CAD adapter data models."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ErrorCode = Literal["SPEC_INVALID", "PORT_MISMATCH", "KERNEL_ERROR", "EXPORT_ERROR", "TIMEOUT"]
Verdict = Literal["pass", "fail", "error"]


class BluecadError(RuntimeError):
    """Structured BLUECAD adapter error."""

    def __init__(self, code: ErrorCode, detail: dict[str, Any]) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def as_report_error(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class PortFrame:
    origin: tuple[float, float, float]
    direction: tuple[float, float, float]
    outer_d: float | None = None
    wall_t: float | None = None
    interface: Literal["tube", "pad"] = "tube"
    pad_d: float | None = None

    def transformed(self, rotation_z_rad: float, translation: tuple[float, float, float]) -> PortFrame:
        return PortFrame(
            origin=_transform_point(self.origin, rotation_z_rad, translation),
            direction=_rotate_vector(self.direction, rotation_z_rad),
            outer_d=self.outer_d,
            wall_t=self.wall_t,
            interface=self.interface,
            pad_d=self.pad_d,
        )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "origin": [round(value, 9) for value in self.origin],
            "direction": [round(value, 9) for value in self.direction],
            "interface": self.interface,
        }
        if self.interface == "tube":
            payload["outer_d"] = round(float(self.outer_d), 9)
            payload["wall_t"] = round(float(self.wall_t), 9)
        else:
            payload["pad_d"] = round(float(self.pad_d), 9)
        return payload


@dataclass(frozen=True)
class BuiltPart:
    part_id: str
    kind: str
    volume_mm3: float
    bbox_mm: tuple[tuple[float, float, float], tuple[float, float, float]]
    ports: dict[str, PortFrame]
    shape: Any = None

    def placed(self, rotation_z_rad: float, translation: tuple[float, float, float]) -> BuiltPart:
        shape = self.shape
        if shape is not None:
            try:
                import build123d as bd
            except ImportError as exc:  # pragma: no cover
                raise BluecadError("KERNEL_ERROR", {"message": "build123d is not installed"}) from exc
            shape = bd.Pos(*translation) * bd.Rot(Z=math.degrees(rotation_z_rad)) * shape
        return BuiltPart(
            part_id=self.part_id,
            kind=self.kind,
            volume_mm3=self.volume_mm3,
            bbox_mm=_transform_bbox(self.bbox_mm, rotation_z_rad, translation),
            ports={name: port.transformed(rotation_z_rad, translation) for name, port in self.ports.items()},
            shape=shape,
        )

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "volume_mm3": round(self.volume_mm3, 9),
            "bbox_mm": {
                "min": [round(value, 9) for value in self.bbox_mm[0]],
                "max": [round(value, 9) for value in self.bbox_mm[1]],
            },
            "ports": {name: port.as_dict() for name, port in sorted(self.ports.items())},
        }


@dataclass(frozen=True)
class BuildResult:
    spec_id: str
    out_dir: Path
    manifest_path: Path | None
    report_path: Path | None
    manifest: dict[str, Any] | None
    report: dict[str, Any]
    verdict: Verdict
    errors: list[dict[str, Any]] = field(default_factory=list)


def _rotate_vector(value: tuple[float, float, float], rotation_z_rad: float) -> tuple[float, float, float]:
    cos_t = math.cos(rotation_z_rad)
    sin_t = math.sin(rotation_z_rad)
    x, y, z = value
    return (cos_t * x - sin_t * y, sin_t * x + cos_t * y, z)


def _transform_point(value: tuple[float, float, float], rotation_z_rad: float, translation: tuple[float, float, float]) -> tuple[float, float, float]:
    rx, ry, rz = _rotate_vector(value, rotation_z_rad)
    return (rx + translation[0], ry + translation[1], rz + translation[2])


def _transform_bbox(bbox: tuple[tuple[float, float, float], tuple[float, float, float]], rotation_z_rad: float, translation: tuple[float, float, float]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    mins, maxs = bbox
    corners = [
        (x, y, z)
        for x in (mins[0], maxs[0])
        for y in (mins[1], maxs[1])
        for z in (mins[2], maxs[2])
    ]
    transformed = [_transform_point(corner, rotation_z_rad, translation) for corner in corners]
    return (tuple(min(point[axis] for point in transformed) for axis in range(3)), tuple(max(point[axis] for point in transformed) for axis in range(3)))


# Spec 010 AI loop ledger/API models.

CandidateStatus = Literal["generating", "validating", "valid", "parked", "archived"]
ParkedReason = Literal["attempts_exhausted", "budget_blocked", "policy_blocked", "malformed_repeated", "user_cancelled", "cad_link_failed", "template_build_failed", "agent_action_failed"]
CandidateOrigin = Literal["ai", "parametric_variant", "process_linked", "template", "agent_action"]
ProposalOutcome = Literal["ok", "malformed", "provider_error", "blocked", "not_applicable"]
ValidationVerdict = Literal["pass", "fail"]

_CALCULIX_SAFE_MATERIAL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _reject_nulls_and_non_finite_numbers(value: Any, path: str = "analysis_spec") -> None:
    if value is None:
        raise ValueError(f"{path} cannot be null")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} must be finite")
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_nulls_and_non_finite_numbers(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_nulls_and_non_finite_numbers(child, f"{path}[{index}]")


class _AnalysisMaterial(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    E: float = Field(gt=0)
    nu: float
    rho: float = Field(gt=0)
    yield_strength: float = Field(gt=0)

    @field_validator("name")
    @classmethod
    def _validate_calculix_safe_name(cls, value: str) -> str:
        if not _CALCULIX_SAFE_MATERIAL_NAME.fullmatch(value):
            raise ValueError("material name must be a CalculiX-safe identifier")
        return value


class _AnalysisBoundaryCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    port_label: str = Field(min_length=1)
    kind: Literal["fixed"]


class _AnalysisLoad(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    port_label: str = Field(min_length=1)
    type: Literal["pressure", "force_total"]
    force: list[float] | None = Field(default=None, min_length=3, max_length=3)
    vector_n: list[float] | None = Field(default=None, min_length=3, max_length=3)
    pressure: float | None = None

    @model_validator(mode="after")
    def _validate_required_magnitude(self) -> _AnalysisLoad:
        for field_name in ("force", "vector_n", "pressure"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null when supplied")
        if self.type == "pressure" and self.pressure is None:
            raise ValueError("pressure loads require pressure")
        if self.type == "force_total" and self.force is None and self.vector_n is None:
            raise ValueError("force_total loads require force or vector_n")
        return self


class _AnalysisMeshQuality(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    min_element_quality: float | None = Field(default=None, ge=0)


class _AnalysisMesh(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target_size: float = Field(gt=0)
    element_order: Literal[1, 2] = 1
    refinements: dict[str, float] | None = None
    quality: _AnalysisMeshQuality | None = None

    @field_validator("element_order", mode="before")
    @classmethod
    def _validate_element_order(cls, value: Any) -> int:
        if type(value) is not int or value not in (1, 2):
            raise ValueError("mesh.element_order must be integer 1 or 2")
        return value

    @model_validator(mode="after")
    def _validate_refinements(self) -> _AnalysisMesh:
        if self.refinements is not None and any(value <= 0 for value in self.refinements.values()):
            raise ValueError("mesh refinements must be positive")
        return self


class _AnalysisPassCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    metric: Literal["max_displacement", "max_von_mises"]
    op: Literal["<=", "<", ">=", ">", "=="]
    value: float


class _AnalysisSpecWithoutGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["bluecad_analysis_spec_v0_1"]
    analysis_id: str = Field(min_length=1)
    analysis_type: Literal["static"]
    material: _AnalysisMaterial
    bcs: list[_AnalysisBoundaryCondition]
    loads: list[_AnalysisLoad]
    mesh: _AnalysisMesh
    pass_criteria: list[_AnalysisPassCriterion]
    timeout_s: float | None = Field(default=None, gt=0)


class BluecadLoopConfig(BaseModel):
    max_attempts_per_tier: int = Field(default=3, ge=1, le=10)
    tier_ladder: list[str] = Field(default_factory=lambda: ["external:cheap", "external:reasoning"])
    max_output_tokens: int = Field(default=4000, ge=128, le=32000)
    per_call_timeout_s: float = Field(default=20.0, gt=0, le=120)
    analysis_spec: dict[str, Any] | None = None
    structural_repair: bool = False
    max_structural_repairs: int = Field(default=1, ge=0, le=3)

    @model_validator(mode="after")
    def _validate_analysis_spec_without_geometry(self) -> BluecadLoopConfig:
        if self.analysis_spec is None:
            if self.structural_repair:
                raise ValueError("structural_repair requires analysis_spec")
            return self
        if not isinstance(self.analysis_spec, dict):
            raise ValueError("analysis_spec must be an object")
        _reject_nulls_and_non_finite_numbers(self.analysis_spec)
        if "geometry" in self.analysis_spec:
            raise ValueError("analysis_spec geometry is filled from build artifacts by the loop")
        _AnalysisSpecWithoutGeometry.model_validate(self.analysis_spec)
        if self.structural_repair and not self.analysis_spec.get("pass_criteria"):
            raise ValueError("structural_repair requires non-empty analysis_spec.pass_criteria")
        return self


class BluecadCandidateCreate(BaseModel):
    brief_text: str = Field(min_length=1)
    loop_config: BluecadLoopConfig | None = None


class BluecadAttemptRead(BaseModel):
    id: str
    candidate_id: str
    attempt_no: int
    route_class: str
    proposal_ai_job_id: str | None = None
    proposal_outcome: ProposalOutcome
    build_outcome: str | None = None
    validation_verdict: ValidationVerdict | None = None
    spec_artifact_id: str | None = None
    report_artifact_id: str | None = None
    manifest_artifact_id: str | None = None
    started_at: str
    finished_at: str | None = None
    error_detail_json: str | None = None


class BluecadCandidateRead(BaseModel):
    id: str
    workspace_id: str
    brief_text: str
    brief_digest: str
    status: CandidateStatus
    parked_reason: ParkedReason | None = None
    spec_artifact_id: str | None = None
    glb_artifact_id: str | None = None
    report_artifact_id: str | None = None
    promoted_decision_id: str | None = None
    origin: CandidateOrigin
    parent_candidate_id: str | None = None
    loop_config_json: str
    created_at: str
    updated_at: str
    notes: str | None = None
    attempts: list[BluecadAttemptRead] = Field(default_factory=list)
