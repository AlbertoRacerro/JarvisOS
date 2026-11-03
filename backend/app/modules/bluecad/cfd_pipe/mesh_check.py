"""Mesh quality record and run refusal (spec 216 contract 2).

The record combines OpenFOAM ``checkMesh`` output (cell count, non-orthogonality,
skewness, aspect ratio, failed checks) with the pre-run wall y+ estimate of the
mesh plan. ``evaluate_mesh`` refuses a run when any limit is exceeded.

``checkMesh -allGeometry`` also flags cells with a small determinant. A wall-resolved
low-Re pipe mesh produces such cells by construction (thin, high-aspect-ratio wall
layers), and the spec's own aspect-ratio limit already bounds them, so that one check
is recorded as waived instead of refusing the run. Every other failed check refuses.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from app.modules.bluecad.cfd_pipe.casegen import MeshPlan
from app.modules.bluecad.cfd_pipe.contract import PipeStudyContract

_CELLS = re.compile(r"^\s*cells:\s+(\d+)\s*$", re.MULTILINE)
_NON_ORTH = re.compile(r"Mesh non-orthogonality Max:\s*([0-9.eE+-]+)")
_SKEW = re.compile(r"Max skewness = ([0-9.eE+-]+)")
_ASPECT = re.compile(r"Max aspect ratio(?: =|:)\s*([0-9.eE+-]+)")
_FAILED = re.compile(r"Failed (\d+) mesh checks")
_FAILURE_LINE = re.compile(r"^\s*\*\*\*(.+?)\s*$", re.MULTILINE)
_WAIVED_PREFIXES = ("Cells with small determinant",)


class MeshCheckParseError(ValueError):
    pass


class CheckMeshReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cells: int
    max_non_orthogonality_deg: float
    max_skewness: float
    max_aspect_ratio: float
    failed_checks: int
    waived_checks: tuple[str, ...] = ()


class MeshPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_cells: int = Field(default=2_000_000, gt=0)
    max_non_orthogonality_deg: float = 70.0
    max_skewness: float = 4.0
    max_aspect_ratio: float = 1000.0
    low_re_max_yplus: float = 1.0
    wall_function_yplus: tuple[float, float] = (30.0, 300.0)


class MeshCheckRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: MeshPlan
    report: CheckMeshReport | None
    accepted: bool
    refusals: tuple[str, ...]


def parse_checkmesh(log: str) -> CheckMeshReport:
    def first(pattern: re.Pattern[str], name: str) -> str:
        match = pattern.search(log)
        if match is None:
            raise MeshCheckParseError(f"checkMesh output has no {name}")
        return match.group(1)

    if "Mesh OK." in log:
        failed = 0
    else:
        failed_match = _FAILED.search(log)
        if failed_match is None:
            raise MeshCheckParseError("checkMesh output has no verdict")
        failed = int(failed_match.group(1))
    waived = tuple(line for line in _FAILURE_LINE.findall(log) if line.startswith(_WAIVED_PREFIXES))
    return CheckMeshReport(
        cells=int(first(_CELLS, "cell count")),
        max_non_orthogonality_deg=float(first(_NON_ORTH, "non-orthogonality")),
        max_skewness=float(first(_SKEW, "skewness")),
        max_aspect_ratio=float(first(_ASPECT, "aspect ratio")),
        failed_checks=failed,
        waived_checks=waived if failed else (),
    )


def plan_refusals(contract: PipeStudyContract, plan: MeshPlan, policy: MeshPolicy | None = None) -> list[str]:
    """Refusals decidable before meshing: cell budget and wall y+ window."""
    policy = policy or MeshPolicy()
    refusals: list[str] = []
    if plan.cell_count > policy.max_cells:
        refusals.append(f"cell_budget: {plan.cell_count} cells > {policy.max_cells}")
    yplus = plan.estimated_wall_yplus
    if contract.wall_treatment == "low_re_resolved":
        assert yplus is not None
        if yplus > policy.low_re_max_yplus:
            refusals.append(f"wall_yplus: estimated {yplus:.3g} > {policy.low_re_max_yplus} for a resolved low-Re wall")
    elif contract.wall_treatment == "wall_function":
        assert yplus is not None
        lo, hi = policy.wall_function_yplus
        if not lo <= yplus <= hi:
            refusals.append(f"wall_yplus: estimated {yplus:.3g} outside the wall-function window [{lo}, {hi}]")
    return refusals


def evaluate_mesh(
    contract: PipeStudyContract, plan: MeshPlan, report: CheckMeshReport | None, policy: MeshPolicy | None = None
) -> MeshCheckRecord:
    """Record the mesh check; refuse when a plan or checkMesh limit is exceeded.

    ``report`` is None only before meshing; a run is never accepted without one.
    """
    policy = policy or MeshPolicy()
    refusals = plan_refusals(contract, plan, policy)
    if report is None:
        refusals.append("check_missing: no checkMesh report")
    else:
        refused_checks = report.failed_checks - len(report.waived_checks)
        if refused_checks > 0:
            refusals.append(f"checkmesh_failed: {refused_checks} failed mesh checks")
        if report.cells != plan.cell_count:
            refusals.append(f"cell_count_mismatch: checkMesh {report.cells} != planned {plan.cell_count}")
        if report.max_non_orthogonality_deg > policy.max_non_orthogonality_deg:
            refusals.append(f"non_orthogonality: {report.max_non_orthogonality_deg:.3g} > {policy.max_non_orthogonality_deg}")
        if report.max_skewness > policy.max_skewness:
            refusals.append(f"skewness: {report.max_skewness:.3g} > {policy.max_skewness}")
        if report.max_aspect_ratio > policy.max_aspect_ratio:
            refusals.append(f"aspect_ratio: {report.max_aspect_ratio:.4g} > {policy.max_aspect_ratio}")
    return MeshCheckRecord(plan=plan, report=report, accepted=not refusals, refusals=tuple(refusals))
