"""Deterministic handoff from 108 study points to BLUECAD geometry."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import Field, model_validator

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.jarvis_context_models import FrozenContract, SourceRef
from app.modules.bluecad.spec import canonicalize_geometry_spec
from app.modules.engineering.refs import (
    DomainBound,
    EvaluationResultRef,
    ProcessDesignEnvelopeRef,
    QualificationStatus,
    Quantity,
    StudyRef,
    ValidityEnvelopeRef,
)
from app.modules.engineering.studies import StudyRun

GEOMETRY_INPUTS = ("tube_inner_diameter", "loop_length")


class ProcessDesignEnvelope(FrozenContract):
    """Immutable process quantities and the study evidence that selected them."""

    envelope_ref: ProcessDesignEnvelopeRef
    study_ref: StudyRef
    definition_digest: str
    content_digest: str
    selected_point_index: int = Field(ge=0)
    evaluation_ref: EvaluationResultRef
    validity_ref: ValidityEnvelopeRef | None = None
    quantities: tuple[tuple[str, Quantity], ...] = Field(min_length=1)
    bounds: tuple[DomainBound, ...] = ()
    qualification_status: Literal["unqualified", "candidate", "calibrated", "benchmarked", "qualified"]
    provenance_refs: tuple[SourceRef, ...] = ()
    envelope_digest: str

    @model_validator(mode="after")
    def validate_envelope(self) -> ProcessDesignEnvelope:
        names = [name for name, _ in self.quantities]
        if len(names) != len(set(names)):
            raise ValueError("process design quantities must have unique names")
        if set(names) != set(GEOMETRY_INPUTS):
            raise ValueError(f"process design envelope requires {', '.join(GEOMETRY_INPUTS)}")
        if len({bound.variable for bound in self.bounds}) != len(self.bounds):
            raise ValueError("process design bounds must have unique variables")
        if any(bound.variable not in names for bound in self.bounds):
            raise ValueError("process design bounds must refer to envelope quantities")
        if self.qualification_status != "unqualified" and self.validity_ref is not None:
            order = {"unqualified": 0, "candidate": 1, "calibrated": 2, "benchmarked": 3, "qualified": 4}
            if order[self.qualification_status] > order[self.validity_ref.qualification_status]:
                raise ValueError("envelope qualification cannot exceed selected point validity")
        expected = _envelope_digest(self.model_dump(mode="json", exclude={"envelope_digest"}))
        if self.envelope_digest != expected:
            raise ValueError("process design envelope digest does not match its contents")
        return self


class PhysicalVerificationResult(FrozenContract):
    result_ref: SourceRef
    status: Literal["passed", "failed"]
    measured_quantities: tuple[tuple[str, Quantity], ...] = Field(min_length=1)
    evidence_refs: tuple[SourceRef, ...] = Field(min_length=1)
    tightened_bounds: tuple[DomainBound, ...] = ()

    @model_validator(mode="after")
    def unique_feedback(self) -> PhysicalVerificationResult:
        names = [name for name, _ in self.measured_quantities]
        if len(names) != len(set(names)):
            raise ValueError("physical verification quantities must be unique")
        if len({bound.variable for bound in self.tightened_bounds}) != len(self.tightened_bounds):
            raise ValueError("tightened bounds must have unique variables")
        return self


class StudyReopenRequest(FrozenContract):
    study_ref: StudyRef
    envelope_digest: str
    violated_quantities: tuple[str, ...] = Field(min_length=1)
    evidence_refs: tuple[SourceRef, ...] = Field(min_length=1)
    tightened_bounds: tuple[DomainBound, ...] = Field(min_length=1)
    proposal_only: Literal[True] = True


class HandoffDecision(FrozenContract):
    status: Literal["accepted", "reopen_requested"]
    reopen_request: StudyReopenRequest | None = None

    @model_validator(mode="after")
    def decision_matches_request(self) -> HandoffDecision:
        if (self.status == "accepted") == (self.reopen_request is not None):
            raise ValueError("accepted handoffs have no reopen request; reopen decisions require one")
        return self


def _envelope_digest(payload: dict[str, object]) -> str:
    def json_value(value: object) -> object:
        if isinstance(value, FrozenContract):
            return value.model_dump(mode="json")
        if isinstance(value, tuple):
            return [json_value(item) for item in value]
        if isinstance(value, list):
            return [json_value(item) for item in value]
        if isinstance(value, dict):
            return {key: json_value(item) for key, item in value.items()}
        return value

    return canonical_digest(json_value(payload))


def process_design_envelope(
    run: StudyRun,
    point_index: int,
    *,
    bounds: tuple[DomainBound, ...] = (),
) -> ProcessDesignEnvelope:
    point = next((item for item in run.points if item.index == point_index), None)
    if point is None or point.evaluation is None or point.evaluation.result_ref is None:
        raise ValueError("selected study point must have a completed evaluation")
    if point.status != "succeeded" or not point.feasible:
        raise ValueError("selected study point must be successful and feasible")
    inputs = {item.name: item.value for item in point.inputs}
    if any(name not in inputs for name in GEOMETRY_INPUTS):
        raise ValueError("selected study point is missing tubular geometry inputs")
    # Without explicit tolerances the envelope claims none; a degenerate point bound
    # would make every later tightening proposal impossible.
    selected_bounds = bounds
    validity = point.evaluation.validity
    qualification = run.qualification_status
    if qualification not in {"unqualified", "candidate", "calibrated", "benchmarked", "qualified"}:
        raise ValueError("study run has an unknown qualification status")
    selected_qualification = cast(QualificationStatus, qualification)
    if validity is not None:
        order = {"unqualified": 0, "candidate": 1, "calibrated": 2, "benchmarked": 3, "qualified": 4}
        if order[validity.qualification_status] < order[selected_qualification]:
            selected_qualification = validity.qualification_status
    ref = ProcessDesignEnvelopeRef(
        authority_owner=run.study_ref.authority_owner,
        object_id=f"{run.study_ref.object_id}/point-{point_index}/process-design",
        workspace_id=run.study_ref.workspace_id,
        revision=run.study_ref.revision,
    )
    values = {
        "envelope_ref": ref,
        "study_ref": run.study_ref,
        "definition_digest": run.definition_digest,
        "content_digest": run.content_digest,
        "selected_point_index": point_index,
        "evaluation_ref": point.evaluation.result_ref,
        "validity_ref": validity,
        "quantities": tuple((name, inputs[name]) for name in GEOMETRY_INPUTS),
        "bounds": selected_bounds,
        "qualification_status": selected_qualification,
        "provenance_refs": tuple(point.evaluation.evidence_refs),
    }
    digest = _envelope_digest(values)
    return ProcessDesignEnvelope(
        envelope_ref=ref,
        study_ref=run.study_ref,
        definition_digest=run.definition_digest,
        content_digest=run.content_digest,
        selected_point_index=point_index,
        evaluation_ref=point.evaluation.result_ref,
        validity_ref=validity,
        quantities=tuple((name, inputs[name]) for name in GEOMETRY_INPUTS),
        bounds=selected_bounds,
        qualification_status=selected_qualification,
        provenance_refs=tuple(point.evaluation.evidence_refs),
        envelope_digest=digest,
    )


def envelope_to_geometry_spec(envelope: ProcessDesignEnvelope, *, wall_thickness_mm: float = 2.0) -> dict[str, object]:
    """Map process bore and path length to BLUECAD's millimetre tube primitive.

    Wall thickness is an explicit CAD construction allowance; it does not alter
    either process quantity and is not a process or scientific parameter.
    """
    values = dict(envelope.quantities)
    bore_mm = _convert_length(values["tube_inner_diameter"], "mm")
    length_mm = _convert_length(values["loop_length"], "mm")
    spec = {
        "spec_version": "bluecad_geometry_spec_v0_1",
        "name": f"process-design-{envelope.envelope_digest.removeprefix('sha256:')[:12]}",
        "parts": [{
            "part_id": "process_tube",
            "kind": "tube_run",
            "params": {"outer_d": bore_mm + 2 * wall_thickness_mm,
                       "wall_t": wall_thickness_mm, "length": length_mm},
        }],
        "connections": [],
    }
    return canonicalize_geometry_spec(spec)


def evaluate_physical_verification(
    envelope: ProcessDesignEnvelope,
    result: PhysicalVerificationResult,
) -> HandoffDecision:
    values = dict(envelope.quantities)
    bounds = {bound.variable: bound for bound in envelope.bounds}
    measured = dict(result.measured_quantities)
    if set(measured) != set(values):
        raise ValueError("physical verification must report every envelope quantity")
    violated: list[str] = []
    for name, actual in measured.items():
        if name not in values:
            raise ValueError(f"verification result contains unknown envelope quantity: {name}")
        if actual.unit != values[name].unit:
            raise ValueError(f"verification unit for {name} must match the envelope unit")
        bound = bounds.get(name)
        if bound is not None and (
            bound.lower is not None and actual.value < _convert_length(bound.lower, actual.unit)
            or bound.upper is not None and actual.value > _convert_length(bound.upper, actual.unit)
        ):
            violated.append(name)
    if result.status == "passed" and not violated:
        return HandoffDecision(status="accepted")
    if not violated:
        violated.extend(name for name in measured if name in values)
    proposals = {bound.variable: bound for bound in result.tightened_bounds}
    if not proposals:
        raise ValueError("a failed physical verification must propose tightened study bounds")
    for name, proposed in proposals.items():
        if name not in violated:
            raise ValueError("tightened bounds may only be proposed for violated quantities")
        current = bounds.get(name)
        if current is not None and not _strictly_tighter(proposed, current):
            raise ValueError(f"proposed bounds for {name} must be tighter than current bounds")
    return HandoffDecision(status="reopen_requested", reopen_request=StudyReopenRequest(
        study_ref=envelope.study_ref,
        envelope_digest=envelope.envelope_digest,
        violated_quantities=tuple(sorted(set(violated))),
        evidence_refs=result.evidence_refs,
        tightened_bounds=tuple(proposals[name] for name in sorted(proposals)),
    ))


def _strictly_tighter(proposed: DomainBound, current: DomainBound) -> bool:
    if any(
        proposed_value is not None and current_value is not None and proposed_value.unit != current_value.unit
        for proposed_value, current_value in (
            (proposed.lower, current.lower),
            (proposed.upper, current.upper),
        )
    ):
        return False
    lower_valid = current.lower is None or (
        proposed.lower is not None and proposed.lower.value >= current.lower.value
    )
    upper_valid = current.upper is None or (
        proposed.upper is not None and proposed.upper.value <= current.upper.value
    )
    lower_tighter = proposed.lower is not None and (
        current.lower is None or proposed.lower.value > current.lower.value
    )
    upper_tighter = proposed.upper is not None and (
        current.upper is None or proposed.upper.value < current.upper.value
    )
    return proposed.variable == current.variable and lower_valid and upper_valid and (lower_tighter or upper_tighter)


def _convert_length(quantity: Quantity, target_unit: str) -> float:
    from app.modules.process_stack._common import magnitude

    return magnitude(quantity, target_unit)
