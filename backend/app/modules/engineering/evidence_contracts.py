"""102 frozen engineering evidence, fidelity, provenance and qualification contracts.

These ride on the 145 envelopes (``engineering.refs``) and never replace the
044/077 evidence authority: typed outcome rows stay in ``evidence_records``
(referenced here through ``evidence_record_ref``) and every egress of their
content still goes through the 077/059 lineage. A qualification record is
metadata about a model/result; recording it never qualifies anything. Status
above ``candidate`` needs cited evidence, and ``qualified`` additionally needs
an explicit operator or deterministic-policy promotion, never model output.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import Field, model_validator

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.jarvis_context_models import FrozenContract, SourceRef, UtcDatetime
from app.modules.bluecad.evidence import EvidenceRecord
from app.modules.engineering.refs import (
    DynamicModelRef,
    EvaluationResultRef,
    MaterialStateRef,
    PhysicsCaseRef,
    ProcessModelIRRef,
    PropertyBasisRef,
    Quantity,
    UncertaintyBound,
    ValidityEnvelopeRef,
    VariableName,
)

ENGINEERING_EVIDENCE_VERSION: Final = "engineering_evidence.v1"
EngineeringEvidenceVersion = Literal["engineering_evidence.v1"]

MAX_ITEMS = 32
EVIDENCE_RECORD_OWNER: Final = "bluecad_evidence"
EVIDENCE_RECORD_TYPE: Final = "evidence_record"

# Ordered from cheapest/least resolved to most resolved. The existing 047
# "M0_static_screening" kernel is ``screening``.
FidelityTier = Literal["screening", "reduced_order", "steady_state_detailed", "dynamic_detailed", "field_resolved"]
FIDELITY_ORDER: Final[tuple[FidelityTier, ...]] = (
    "screening",
    "reduced_order",
    "steady_state_detailed",
    "dynamic_detailed",
    "field_resolved",
)
ProvenanceKind = Literal[
    "literature",
    "measurement",
    "vendor_datasheet",
    "simulation",
    "expert_estimate",
    "derived",
    "synthetic_fixture",
]
QualificationDecider = Literal["operator", "deterministic_policy"]

_CITED_KINDS = frozenset({"literature", "measurement", "vendor_datasheet", "simulation", "derived"})

# Physics cases carry CFD/FEM turbulence/boundary/material modelling assumptions;
# property bases carry correlations.
QualifiableSubjectRef = (
    ProcessModelIRRef | DynamicModelRef | PropertyBasisRef | MaterialStateRef | PhysicsCaseRef | EvaluationResultRef
)


def fidelity_rank(tier: FidelityTier) -> int:
    return FIDELITY_ORDER.index(tier)


def validity_content_digest(validity: ValidityEnvelopeRef) -> str:
    """Digest pinning an envelope's identity to its domain, uncertainty, status and evidence."""
    return canonical_digest(
        validity.model_dump(
            mode="json",
            include={"authority_owner", "object_id", "workspace_id", "domain", "uncertainty",
                     "qualification_status", "evidence_refs"},
        )
    )


class ConditionValue(FrozenContract):
    """One experimental/operating condition under which a source or model holds."""

    variable: VariableName
    value: Quantity


class ProvenanceEntry(FrozenContract):
    """Where a parameter, correlation, model or dataset came from.

    Biology-specific sources name organism/strain; everything cited carries a
    ref or citation. ``synthetic_fixture`` marks test/demo data that can never
    support a qualification above ``candidate``.
    """

    kind: ProvenanceKind
    source_ref: SourceRef | None = None
    citation: str | None = Field(default=None, min_length=1, max_length=512)
    organism: str | None = Field(default=None, min_length=1, max_length=128)
    strain: str | None = Field(default=None, min_length=1, max_length=128)
    conditions: tuple[ConditionValue, ...] = Field(default=(), max_length=MAX_ITEMS)
    note: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def cited_sources_are_traceable(self) -> ProvenanceEntry:
        if self.kind in _CITED_KINDS and self.source_ref is None and self.citation is None:
            raise ValueError(f"{self.kind} provenance requires source_ref or citation")
        if self.strain is not None and self.organism is None:
            raise ValueError("strain requires organism")
        return self


class QualificationBasis(FrozenContract):
    """Evidence behind a qualification status and the exact promotion decision, if any."""

    calibration_refs: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_ITEMS)
    benchmark_refs: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_ITEMS)
    decided_by: QualificationDecider | None = None
    decided_at: UtcDatetime | None = None
    decision_ref: SourceRef | None = None

    @model_validator(mode="after")
    def decision_is_complete(self) -> QualificationBasis:
        if len({self.decided_by is None, self.decided_at is None, self.decision_ref is None}) != 1:
            raise ValueError("decided_by, decided_at and decision_ref must be provided together")
        return self


class ScientificQualificationRecord(FrozenContract):
    """One qualification-ledger entry for a model, property basis or evaluation result.

    Required content (spec 145 P packet): source, organism/strain, conditions,
    units (through ``Quantity``), uncertainty, calibration basis, validity
    domain, benchmark evidence and qualification status.
    """

    schema_version: EngineeringEvidenceVersion = ENGINEERING_EVIDENCE_VERSION
    record_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    subject_ref: QualifiableSubjectRef = Field(discriminator="object_type")
    fidelity: FidelityTier
    validity: ValidityEnvelopeRef
    provenance: tuple[ProvenanceEntry, ...] = Field(min_length=1, max_length=MAX_ITEMS)
    uncertainty: tuple[UncertaintyBound, ...] = Field(default=(), max_length=MAX_ITEMS)
    basis: QualificationBasis = QualificationBasis()
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def status_is_supported(self) -> ScientificQualificationRecord:
        if self.subject_ref.workspace_id != self.validity.workspace_id:
            raise ValueError("subject and validity envelope must belong to the same engineering project")
        if self.validity.content_digest != validity_content_digest(self.validity):
            raise ValueError("validity content_digest must pin the envelope content (validity_content_digest)")
        status = self.validity.qualification_status
        if status in {"unqualified", "candidate"}:
            if self.basis.decided_by is not None:
                raise ValueError(f"{status} records carry no promotion decision")
            return self
        if all(entry.kind == "synthetic_fixture" for entry in self.provenance):
            raise ValueError("synthetic fixtures cannot support a status above candidate")
        if status == "calibrated" and not self.basis.calibration_refs:
            raise ValueError("calibrated status requires calibration_refs")
        if status in {"benchmarked", "qualified"} and not self.basis.benchmark_refs:
            raise ValueError(f"{status} status requires benchmark_refs")
        if status == "qualified" and self.basis.decided_by is None:
            raise ValueError("qualified status requires an explicit operator or deterministic-policy decision")
        return self


def evidence_record_ref(record: EvidenceRecord) -> SourceRef:
    """Exact ref to one 044 ``evidence_records`` row, pinned to its full current content.

    Rows may be relinked to a candidate/attempt after creation, so the digest
    covers every column; a relinked row resolves as stale.
    """
    return SourceRef(
        authority_owner=EVIDENCE_RECORD_OWNER,
        object_type=EVIDENCE_RECORD_TYPE,
        object_id=record.id,
        workspace_id=record.workspace_id,
        content_digest=canonical_digest(record.model_dump(mode="json")),
    )
