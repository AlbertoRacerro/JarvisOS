"""Versioned ΔP–Re reduced model fitted from measured pipe runs (spec 216 contracts 7–8).

Each regime segment fits a Darcy friction factor ``f = a Re^b`` by least squares
in log space over measured, residual-converged runs of one tube geometry and
one study template. Its relative uncertainty is the conservative linear sum of

* discretization: ``max(GCI_fine, 1.25 |f_point_mesh - f_extrapolated| / |f_extrapolated|)``
  from the segment's three-mesh study, so points run on a coarser level than the
  study's fine mesh carry their distance from the Richardson value;
* fit: the largest relative residual of the power law at the measured points;
* correlation: the largest relative deviation of the measured points from the
  reference (Hagen–Poiseuille laminar, smooth Colebrook turbulent).

Publication refuses unmeasured, unconverged, mixed-geometry or out-of-tolerance
input and has a content-derived version identity. Evaluation never extrapolates
silently: outside the validated Re, diameter or fluid range it warns or refuses.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.bluecad.cfd_pipe.contract import PipeStudyContract, Regime, contract_digest
from app.modules.bluecad.cfd_pipe.mesh_study import GCI_SAFETY_FACTOR, MeshStudy
from app.modules.bluecad.cfd_pipe.references import darcy_colebrook_smooth, darcy_laminar

REDUCED_MODEL_VERSION: Literal["cfd_pipe_reduced_model_v1"] = "cfd_pipe_reduced_model_v1"
FORM = "f_D = a * Re**b; dP/L = f_D * rho * U**2 / (2 * D); tau_w = f_D * rho * U**2 / 8"
REFERENCE = {"laminar": "hagen_poiseuille", "turbulent_rans": "colebrook_smooth"}
# Contract fields that vary within one study (operating point, mesh level and run budget).
_POINT_FIELDS = ("mesh_level", "solver")
# Relative tolerance for "the same" diameter or fluid property across points and at evaluation.
_SAME = 1e-9

_STRICT = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ReducedModelError(ValueError):
    pass


class PublicationRefused(ReducedModelError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


class StudyPoint(BaseModel):
    """One run's measured result, as recorded in its run report."""

    model_config = _STRICT

    run_id: str = Field(min_length=1)
    contract: PipeStudyContract
    case_manifest_digest: str = Field(min_length=1)
    status: str
    residual_converged: bool
    pressure_gradient_pa_m: float
    tool_versions: dict[str, str]

    @property
    def darcy(self) -> float:
        c = self.contract
        return 2.0 * c.diameter_m * self.pressure_gradient_pa_m / (c.fluid.density_kg_m3 * c.mean_velocity_m_s**2)


class Discretization(BaseModel):
    """Three-mesh study of one segment and the value its points' mesh level gives there."""

    model_config = _STRICT

    regime: Regime
    reynolds: float = Field(gt=0)
    study: MeshStudy
    point_level_value: float
    run_ids: tuple[str, str, str]
    case_manifest_digests: tuple[str, str, str]


class Tolerances(BaseModel):
    """Largest allowed |CFD / reference − 1| per regime, stated before the runs."""

    model_config = _STRICT

    laminar: float = Field(gt=0, lt=1)
    turbulent_rans: float = Field(gt=0, lt=1)


class Uncertainty(BaseModel):
    model_config = _STRICT

    discretization: float
    fit: float
    correlation: float
    combined: float


class Validity(BaseModel):
    model_config = _STRICT

    reynolds_min: float
    reynolds_max: float
    diameter_m: float
    density_kg_m3: float
    dynamic_viscosity_pa_s: float
    regime: Regime
    turbulence_model: str | None
    wall_treatment: str


class PointResult(BaseModel):
    model_config = _STRICT

    run_id: str
    reynolds: float
    darcy_cfd: float
    darcy_reference: float
    error_vs_reference: float


class Segment(BaseModel):
    model_config = _STRICT

    regime: Regime
    a: float
    b: float
    reference: str
    validity: Validity
    uncertainty: Uncertainty
    study_template_digest: str
    points: tuple[PointResult, ...]


class ReducedModel(BaseModel):
    """A published version; ``version_id`` is the digest of every other field."""

    model_config = _STRICT

    model_version: Literal["cfd_pipe_reduced_model_v1"] = REDUCED_MODEL_VERSION
    model_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    form: str = FORM
    geometry_digest: str
    segments: tuple[Segment, ...]
    tolerances: Tolerances
    provenance: dict[str, tuple[str, ...]]
    version_id: str


class Evaluation(BaseModel):
    model_config = _STRICT

    status: Literal["in_range", "out_of_range_warned", "refused"]
    violations: tuple[str, ...]
    regime: Regime | None
    reynolds: float
    darcy: float | None
    pressure_gradient_pa_m: float | None
    wall_shear_pa: float | None
    relative_uncertainty: float | None


def study_template_digest(contract: PipeStudyContract) -> str:
    """Digest fixed study inputs, excluding only the operating point and run budget."""
    payload = contract.model_dump(mode="json")
    for name in _POINT_FIELDS:
        payload.pop(name)
    payload["inlet"].pop("mean_velocity_m_s")
    payload["inlet"].pop("flow_rate_m3_s")
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def reference_darcy(regime: Regime, re: float) -> float:
    return darcy_laminar(re) if regime == "laminar" else darcy_colebrook_smooth(re)


def fit_power_law(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares ``f = a Re^b`` in log space; needs two distinct Reynolds numbers."""
    xs = [math.log(re) for re, _ in points]
    ys = [math.log(f) for _, f in points]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0.0:
        raise ReducedModelError("a segment needs at least two distinct Reynolds numbers")
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    return math.exp(my - b * mx), b


def discretization_uncertainty(evidence: Discretization) -> float:
    study = evidence.study
    if study.convergence not in ("monotonic", "converged") or study.extrapolated is None or study.gci_fine is None:
        raise ReducedModelError(f"{evidence.regime} mesh study is {study.convergence}; no discretization uncertainty")
    if study.extrapolated == 0.0:
        raise ReducedModelError("extrapolated value is zero; relative uncertainty is undefined")
    offset = GCI_SAFETY_FACTOR * abs(evidence.point_level_value - study.extrapolated) / abs(study.extrapolated)
    return max(study.gci_fine, offset)


def publish(model_id: str, points: list[StudyPoint], discretization: list[Discretization],
            tolerances: Tolerances) -> ReducedModel:
    """Fit, guard and version a reduced model; raise ``PublicationRefused`` with every reason."""
    reasons: list[str] = []
    for p in points:
        if p.status != "measured":
            reasons.append(f"run {p.run_id} is {p.status!r}, not measured")
        if not p.residual_converged:
            reasons.append(f"run {p.run_id} did not meet its residual target")
        if not math.isfinite(p.pressure_gradient_pa_m) or p.pressure_gradient_pa_m <= 0:
            reasons.append(f"run {p.run_id} has no positive pressure gradient")
    if not points:
        reasons.append("no study runs")
    if reasons:
        raise PublicationRefused(reasons)
    toolchains = [p.tool_versions for p in points]
    if not toolchains[0] or any(versions != toolchains[0] for versions in toolchains[1:]):
        raise PublicationRefused(["study points must use one recorded toolchain"])
    geometry = {p.contract.domain.geometry_digest for p in points}
    if len(geometry) != 1:
        raise PublicationRefused([f"runs span {len(geometry)} tube geometries; a model belongs to one"])
    evidence = {d.regime: d for d in discretization}
    segments: list[Segment] = []
    for regime in ("laminar", "turbulent_rans"):
        members = [p for p in points if p.contract.regime == regime]
        if not members:
            continue
        templates = {study_template_digest(p.contract) for p in members}
        if len(templates) != 1:
            reasons.append(f"{regime} runs differ in fluid, model or boundary settings")
            continue
        if len({round(p.contract.reynolds, 6) for p in members}) < 2:
            reasons.append(f"{regime} segment needs runs at two or more Reynolds numbers")
            continue
        if regime not in evidence:
            reasons.append(f"{regime} segment has no three-mesh study")
            continue
        try:
            u_disc = discretization_uncertainty(evidence[regime])
        except ReducedModelError as exc:
            reasons.append(str(exc))
            continue
        rows = []
        for p in sorted(members, key=lambda m: m.contract.reynolds):
            re = p.contract.reynolds
            ref = reference_darcy(regime, re)
            rows.append(PointResult(run_id=p.run_id, reynolds=re, darcy_cfd=p.darcy, darcy_reference=ref,
                                    error_vs_reference=p.darcy / ref - 1.0))
        limit = getattr(tolerances, regime)
        for row in rows:
            if abs(row.error_vs_reference) > limit:
                reasons.append(f"run {row.run_id} deviates {row.error_vs_reference:+.2%} from "
                               f"{REFERENCE[regime]}, beyond the stated ±{limit:.0%}")
        a, b = fit_power_law([(row.reynolds, row.darcy_cfd) for row in rows])
        u_fit = max(abs(a * row.reynolds**b / row.darcy_cfd - 1.0) for row in rows)
        u_corr = max(abs(row.error_vs_reference) for row in rows)
        c = members[0].contract
        segments.append(Segment(
            regime=regime, a=a, b=b, reference=REFERENCE[regime],
            validity=Validity(
                reynolds_min=rows[0].reynolds, reynolds_max=rows[-1].reynolds, diameter_m=c.diameter_m,
                density_kg_m3=c.fluid.density_kg_m3, dynamic_viscosity_pa_s=c.fluid.dynamic_viscosity_pa_s,
                regime=regime, turbulence_model=c.turbulence_model, wall_treatment=c.wall_treatment),
            uncertainty=Uncertainty(discretization=u_disc, fit=u_fit, correlation=u_corr,
                                    combined=u_disc + u_fit + u_corr),
            study_template_digest=templates.pop(), points=tuple(rows)))
    if reasons:
        raise PublicationRefused(reasons)
    provenance = {
        "run_ids": tuple(p.run_id for p in points),
        "contract_digests": tuple(contract_digest(p.contract) for p in points),
        "case_manifest_digests": tuple(p.case_manifest_digest for p in points),
        "mesh_study_run_ids": tuple(f"{d.regime}:{run_id}" for d in discretization for run_id in d.run_ids),
        "mesh_study_case_manifest_digests": tuple(digest for d in discretization
                                                  for digest in d.case_manifest_digests),
        "tool_versions": tuple(sorted({f"{k}={v}" for p in points for k, v in p.tool_versions.items()})),
    }
    draft = ReducedModel(model_id=model_id, geometry_digest=geometry.pop(), segments=tuple(segments),
                         tolerances=tolerances, provenance=provenance, version_id="pending")
    return draft.model_copy(update={"version_id": model_version_id(draft)})


def model_version_id(model: ReducedModel) -> str:
    payload = model.model_dump(mode="json", exclude={"version_id"})
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_version(model: ReducedModel) -> bool:
    return model.version_id == model_version_id(model)


def evaluate(model: ReducedModel, *, reynolds: float, diameter_m: float, density_kg_m3: float,
             dynamic_viscosity_pa_s: float, out_of_range: Literal["warn", "refuse"] = "refuse") -> Evaluation:
    """ΔP/L and wall shear at an operating point; outside validity warn or refuse, never silently."""
    if not all(math.isfinite(v) and v > 0 for v in (reynolds, diameter_m, density_kg_m3, dynamic_viscosity_pa_s)):
        raise ReducedModelError("operating point must be positive and finite")
    segment = next((s for s in model.segments
                    if s.validity.reynolds_min <= reynolds <= s.validity.reynolds_max), None)
    violations: list[str] = []
    if segment is None:
        spans = ", ".join(f"{s.regime} {s.validity.reynolds_min:.0f}–{s.validity.reynolds_max:.0f}" for s in model.segments)
        violations.append(f"Re {reynolds:.0f} is outside the validated range ({spans})")
        segment = min(model.segments, key=lambda s: min(abs(math.log(reynolds / s.validity.reynolds_min)),
                                                        abs(math.log(reynolds / s.validity.reynolds_max))))
    v = segment.validity
    for label, value, valid in (("diameter", diameter_m, v.diameter_m), ("density", density_kg_m3, v.density_kg_m3),
                                ("viscosity", dynamic_viscosity_pa_s, v.dynamic_viscosity_pa_s)):
        if not math.isclose(value, valid, rel_tol=_SAME):
            violations.append(f"{label} {value:.6g} differs from the validated {valid:.6g}")
    if violations and out_of_range == "refuse":
        return Evaluation(status="refused", violations=tuple(violations), regime=None, reynolds=reynolds,
                          darcy=None, pressure_gradient_pa_m=None, wall_shear_pa=None, relative_uncertainty=None)
    darcy = segment.a * reynolds**segment.b
    velocity = reynolds * dynamic_viscosity_pa_s / (density_kg_m3 * diameter_m)
    return Evaluation(
        status="out_of_range_warned" if violations else "in_range", violations=tuple(violations),
        regime=segment.regime, reynolds=reynolds, darcy=darcy,
        pressure_gradient_pa_m=darcy * density_kg_m3 * velocity**2 / (2.0 * diameter_m),
        wall_shear_pa=darcy * density_kg_m3 * velocity**2 / 8.0,
        relative_uncertainty=None if violations else segment.uncertainty.combined)


def obsolescence(model: ReducedModel, *, geometry_digest: str, study_templates: dict[str, str]) -> tuple[str, ...]:
    """Reasons the pinned version no longer describes the component; empty when current.

    ``study_templates`` maps a regime to the component's current study template digest.
    """
    reasons = []
    if not verify_version(model):
        reasons.append("pinned reduced model does not match its version identity")
    if geometry_digest != model.geometry_digest:
        reasons.append("tube geometry changed since the reduced model was published; rerun the study")
    for segment in model.segments:
        current = study_templates.get(segment.regime)
        if current is not None and current != segment.study_template_digest:
            reasons.append(f"{segment.regime} study inputs changed since publication; rerun the study")
    return tuple(reasons)
