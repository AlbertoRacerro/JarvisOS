"""145 frozen cross-workstream engineering identity/value envelopes.

Identity only: each ref is a ``SourceRef`` whose ``object_type`` is fixed to
one engineering kind and whose ``workspace_id`` (the engineering project) is
mandatory. 102/106 own evidence, evaluator and domain semantics on top of these
envelopes; nothing here declares a model valid or qualified.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.modules.ai.jarvis_context_models import FrozenContract, SourceRef
from app.modules.process_kernel.errors import ProcessKernelError
from app.modules.process_kernel.units import parse_unit_token

ENGINEERING_REFS_VERSION: Final = "engineering_refs.v1"

MAX_ENVELOPE_ITEMS = 32
MAX_REFS = 32

EngineeringObjectType = Literal[
    "engineering_project",
    "engineering_revision",
    "component_registry",
    "property_basis",
    "material_state",
    "process_model_ir",
    "dynamic_model",
    "environmental_scenario",
    "process_design_envelope",
    "geometry_asset",
    "mesh_artifact",
    "physics_case",
    "evaluation_request",
    "evaluation_result",
    "study",
    "validity_envelope",
]
QualificationStatus = Literal["unqualified", "candidate", "calibrated", "benchmarked", "qualified"]
VariableName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")]


class EngineeringRef(SourceRef):
    """Any engineering object ref; ``location`` is not part of engineering identity."""

    object_type: EngineeringObjectType
    workspace_id: str = Field(min_length=1, max_length=128)
    location: None = None


class EngineeringProjectRef(EngineeringRef):
    object_type: Literal["engineering_project"] = "engineering_project"

    @model_validator(mode="after")
    def project_is_workspace(self) -> EngineeringProjectRef:
        if self.object_id != self.workspace_id:
            raise ValueError("engineering project identity is the workspace id")
        return self


class EngineeringRevisionRef(EngineeringRef):
    object_type: Literal["engineering_revision"] = "engineering_revision"


class ComponentRegistryRef(EngineeringRef):
    object_type: Literal["component_registry"] = "component_registry"


class PropertyBasisRef(EngineeringRef):
    object_type: Literal["property_basis"] = "property_basis"


class MaterialStateRef(EngineeringRef):
    object_type: Literal["material_state"] = "material_state"


class ProcessModelIRRef(EngineeringRef):
    object_type: Literal["process_model_ir"] = "process_model_ir"


class DynamicModelRef(EngineeringRef):
    object_type: Literal["dynamic_model"] = "dynamic_model"


class EnvironmentalScenarioRef(EngineeringRef):
    object_type: Literal["environmental_scenario"] = "environmental_scenario"


class ProcessDesignEnvelopeRef(EngineeringRef):
    object_type: Literal["process_design_envelope"] = "process_design_envelope"


class GeometryAssetRef(EngineeringRef):
    object_type: Literal["geometry_asset"] = "geometry_asset"


class MeshArtifactRef(EngineeringRef):
    object_type: Literal["mesh_artifact"] = "mesh_artifact"


class PhysicsCaseRef(EngineeringRef):
    object_type: Literal["physics_case"] = "physics_case"


class EvaluationRequestRef(EngineeringRef):
    object_type: Literal["evaluation_request"] = "evaluation_request"


class EvaluationResultRef(EngineeringRef):
    object_type: Literal["evaluation_result"] = "evaluation_result"


class StudyRef(EngineeringRef):
    object_type: Literal["study"] = "study"


class Quantity(FrozenContract):
    """A finite magnitude in a unit understood by the single Pint owner.

    Compound units use Pint syntax (``W/(m**2*K)``); compact exponents such as
    ``m2`` are accepted only as whole reviewed tokens (``m2``, ``kg/m3``).
    """

    value: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=64)
    basis_ref: SourceRef | None = None

    @field_validator("value", mode="before")
    @classmethod
    def reject_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("quantity value must be a number, not a boolean")
        return value

    @field_validator("unit")
    @classmethod
    def known_unit(cls, value: str) -> str:
        try:
            parse_unit_token(value)
        except ProcessKernelError as exc:
            raise ValueError(str(exc)) from exc
        return value


class DomainBound(FrozenContract):
    variable: VariableName
    lower: Quantity | None = None
    upper: Quantity | None = None

    @model_validator(mode="after")
    def ordered_bounds(self) -> DomainBound:
        if self.lower is None and self.upper is None:
            raise ValueError("domain bound requires lower or upper")
        if self.lower is not None and self.upper is not None:
            if self.lower.unit != self.upper.unit:
                raise ValueError("domain bound lower and upper must share a unit")
            if self.lower.value > self.upper.value:
                raise ValueError("domain bound lower must not exceed upper")
        return self


class UncertaintyBound(FrozenContract):
    variable: VariableName
    absolute: Quantity | None = None
    relative: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    confidence_level: float | None = Field(default=None, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def require_magnitude(self) -> UncertaintyBound:
        if self.absolute is None and self.relative is None:
            raise ValueError("uncertainty bound requires absolute or relative magnitude")
        if self.absolute is not None and self.absolute.value < 0:
            raise ValueError("absolute uncertainty must be non-negative")
        return self


class ValidityEnvelopeRef(EngineeringRef):
    """Where a model/result may be used, how uncertain it is, and its qualification state.

    There is no default status; anything beyond ``candidate`` must cite evidence.
    Recording a status here is metadata, not a qualification decision.
    """

    object_type: Literal["validity_envelope"] = "validity_envelope"
    domain: tuple[DomainBound, ...] = Field(default=(), max_length=MAX_ENVELOPE_ITEMS)
    uncertainty: tuple[UncertaintyBound, ...] = Field(default=(), max_length=MAX_ENVELOPE_ITEMS)
    qualification_status: QualificationStatus
    evidence_refs: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_REFS)

    @model_validator(mode="after")
    def qualification_requires_evidence(self) -> ValidityEnvelopeRef:
        if self.qualification_status not in {"unqualified", "candidate"} and not self.evidence_refs:
            raise ValueError(f"qualification_status {self.qualification_status!r} requires evidence_refs")
        return self
