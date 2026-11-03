"""Build reduced-model study evidence from evaluator-owned run records only (spec 216 contract 7).

A caller names run ids; every number comes from the server's own ``run.json`` measurement and ``contract.json``.
Unknown runs, runs of another component, unfinished, unmeasured or digest-less runs are refused with plain reasons.
"""

from __future__ import annotations

from typing import Any

from app.modules.bluecad.cfd_pipe import case_runs
from app.modules.bluecad.cfd_pipe.contract import Regime, contract_digest
from app.modules.bluecad.cfd_pipe.mesh_study import MeshStudyError, refinement_ratios, three_mesh_study
from app.modules.bluecad.cfd_pipe.reduced_model import (
    Discretization,
    PublicationRefused,
    StudyPoint,
    study_template_digest,
)

STUDY_RUNS = 3


def _load(workspace_id: str, component_id: str, run_id: str) -> tuple[StudyPoint | None, list[str]]:
    """One server run as a StudyPoint, or the reasons it cannot be evidence."""
    try:
        record = case_runs.read_run(workspace_id, component_id, run_id)
        contract = case_runs.read_run_contract(workspace_id, component_id, run_id)
    except case_runs.CaseRunError:
        return None, [f"run {run_id} is not a run of this component (or has no readable contract)"]
    measurement: dict[str, Any] = record.get("measurement") or {}
    reasons = []
    if record.get("state") != "finished":
        reasons.append(f"run {run_id} is {record.get('state')!r}, not finished")
    elif measurement.get("status") != "measured":
        reasons.append(f"run {run_id} is unmeasured: {measurement.get('reason') or 'no measurement recorded'}")
    if not record.get("contract_digest") or not record.get("geometry_digest"):
        reasons.append(f"run {run_id} recorded no contract or geometry digest")
    elif (record["contract_digest"] != contract_digest(contract)
          or record["geometry_digest"] != contract.domain.geometry_digest):
        reasons.append(f"run {run_id} contract does not match its recorded digests")
    elif record.get("regime") != contract.regime:
        reasons.append(f"run {run_id} regime does not match its contract")
    if not measurement.get("case_manifest_digest"):
        reasons.append(f"run {run_id} recorded no case manifest digest")
    gradient = measurement.get("pressure_gradient_pa_m")
    if reasons or not isinstance(gradient, float | int):
        return None, reasons or [f"run {run_id} recorded no pressure gradient"]
    point = StudyPoint(run_id=run_id, contract=contract, case_manifest_digest=measurement["case_manifest_digest"],
                       status="measured", residual_converged=bool(record.get("residual_converged")),
                       pressure_gradient_pa_m=float(gradient), tool_versions=measurement.get("tool_versions") or {})
    return point, []


def _load_all(workspace_id: str, component_id: str, run_ids: list[str]) -> tuple[list[StudyPoint], list[str]]:
    points, reasons = [], []
    for run_id in dict.fromkeys(run_ids):
        point, why = _load(workspace_id, component_id, run_id)
        reasons += why
        if point is not None:
            points.append(point)
    if len(set(run_ids)) != len(run_ids):
        reasons.append("a run id is listed more than once")
    return points, reasons


def study_points(workspace_id: str, component_id: str, run_ids: list[str]) -> list[StudyPoint]:
    points, reasons = _load_all(workspace_id, component_id, run_ids)
    if reasons:
        raise PublicationRefused(reasons)
    return points


def _discretization(workspace_id: str, component_id: str, regime: Regime, run_ids: list[str],
                    members: list[StudyPoint]) -> Discretization:
    levels = {p.contract.mesh_level for p in members}
    templates = {study_template_digest(p.contract) for p in members}
    if len(levels) != 1:
        raise PublicationRefused([f"{regime} points use mesh levels {sorted(levels)}; they must share one level"])
    if len(templates) != 1:
        raise PublicationRefused([f"{regime} runs differ in fluid, model or boundary settings"])
    if len(run_ids) != STUDY_RUNS or len(set(run_ids)) != STUDY_RUNS:
        raise PublicationRefused([f"{regime} mesh study needs exactly {STUDY_RUNS} distinct runs, got {len(run_ids)}"])
    runs, reasons = _load_all(workspace_id, component_id, run_ids)
    reasons = [f"{regime} mesh study: {r}" for r in reasons]
    if reasons:
        raise PublicationRefused(reasons)
    if any(not run.residual_converged for run in runs):
        reasons.append(f"{regime} mesh study contains a run that did not meet its residual target")
    toolchains = [run.tool_versions for run in [*members, *runs]]
    if not toolchains[0] or any(versions != toolchains[0] for versions in toolchains[1:]):
        reasons.append(f"{regime} study points and mesh runs must use one recorded toolchain")
    if {run.contract.domain.geometry_digest for run in runs} != {p.contract.domain.geometry_digest for p in members}:
        reasons.append(f"{regime} mesh study was not run on the points' tube geometry")
    if any(run.contract.regime != regime for run in runs):
        reasons.append(f"{regime} mesh study contains a run of another regime")
    if len({study_template_digest(run.contract) for run in runs}) != 1:
        reasons.append(f"{regime} mesh study runs differ in fluid, model or boundary settings")
    elif study_template_digest(runs[0].contract) != templates.copy().pop():
        reasons.append(f"{regime} mesh study does not share the points' study template")
    if len({round(run.contract.reynolds, 6) for run in runs}) != 1:
        reasons.append(f"{regime} mesh study runs must share one Reynolds number")
    elif round(runs[0].contract.reynolds, 6) not in {round(p.contract.reynolds, 6) for p in members}:
        reasons.append(f"{regime} mesh study Reynolds number must match a measured study point")
    if len({run.contract.mesh_level for run in runs}) != STUDY_RUNS:
        reasons.append(f"{regime} mesh study needs three distinct mesh levels")
    if levels.copy().pop() not in {run.contract.mesh_level for run in runs}:
        reasons.append(f"{regime} points use mesh level {levels.copy().pop()}, which is not in the mesh study")
    cells = {run.run_id: case_runs.read_run(workspace_id, component_id, run.run_id)["measurement"].get("cells")
             for run in runs}
    if any(not isinstance(count, int) or isinstance(count, bool) or count <= 0 for count in cells.values()):
        reasons.append(f"{regime} mesh study runs recorded no mesh cell count; not guessing refinement ratios")
    if reasons:
        raise PublicationRefused(reasons)
    fine, medium, coarse = sorted(runs, key=lambda run: cells[run.run_id], reverse=True)
    try:
        ratios = refinement_ratios((cells[fine.run_id], cells[medium.run_id], cells[coarse.run_id]))
        study = three_mesh_study((fine.darcy, medium.darcy, coarse.darcy), ratios)
    except MeshStudyError as exc:
        raise PublicationRefused([f"{regime} mesh study: {exc}"]) from exc
    level = levels.copy().pop()
    at_level = next(run for run in runs if run.contract.mesh_level == level)
    return Discretization(regime=regime, reynolds=fine.contract.reynolds, study=study,
                          point_level_value=at_level.darcy,
                          run_ids=(fine.run_id, medium.run_id, coarse.run_id),
                          case_manifest_digests=(fine.case_manifest_digest, medium.case_manifest_digest,
                                                 coarse.case_manifest_digest))


def publication_inputs(workspace_id: str, component_id: str, run_ids: list[str],
                       mesh_study_run_ids: dict[Regime, list[str]]) -> tuple[list[StudyPoint], list[Discretization]]:
    """Server-built points and one three-mesh Discretization per regime that has points."""
    points = study_points(workspace_id, component_id, run_ids)
    reasons: list[str] = []
    order: tuple[Regime, ...] = ("laminar", "turbulent_rans")
    regimes = [r for r in order if any(p.contract.regime == r for p in points)]
    reasons += [f"mesh study given for {r}, which has no study runs" for r in mesh_study_run_ids if r not in regimes]
    evidence: list[Discretization] = []
    for regime in regimes:
        try:
            evidence.append(_discretization(workspace_id, component_id, regime, mesh_study_run_ids.get(regime, []),
                                            [p for p in points if p.contract.regime == regime]))
        except PublicationRefused as error:
            reasons += error.reasons
    if reasons:
        raise PublicationRefused(reasons)
    return points, evidence

