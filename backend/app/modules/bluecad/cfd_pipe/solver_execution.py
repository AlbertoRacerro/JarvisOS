"""Bounded serial solver step after an accepted, registry-checked pipe mesh.

This is a low-level execution result. A study owner must still persist the run,
stream live events, check probes and fields, and qualify it before publication.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.modules.bluecad.cfd_pipe.casegen import MANIFEST_NAME, MAX_PROCESSORS, mesh_plan
from app.modules.bluecad.cfd_pipe.contract import PipeStudyContract, contract_digest
from app.modules.bluecad.cfd_pipe.mesh_execution import MeshExecution
from app.modules.bluecad.cfd_pipe.residuals import (
    IterationResiduals,
    LogParseError,
    ResidualStream,
    converged,
    required_fields,
)
from app.modules.bluecad.registry import ToolRegistryError, resolve_tool, run_tool

PARALLEL_TOOLS = ("openfoam_decomposepar", "openfoam_mpiexec", "openfoam_reconstructpar")
PARALLEL_STEP_TIMEOUT_S = 600
# conda-forge openfoam-2412 links its binaries with RPATH .../lib/sys-mpich, which the package does not ship, so they
# fall back to lib/dummy/libPstream.so and abort under MPI. Ranks preload the shipped MPICH Pstream instead.
_MPI_PSTREAM = Path("lib") / "mpich-3.3" / "libPstream.so"


def mpi_solver_args(solver: dict, processors: int, case: Path) -> list[str]:
    """mpiexec arguments running the pinned simpleFoam entrypoint on ``processors`` ranks of ``case``."""
    pstream = Path(solver["entrypoint"]).resolve().parent.parent / _MPI_PSTREAM
    return ["-genv", "LD_PRELOAD", str(pstream), "-genv", "WM_PROJECT_VERSION", solver["version_pin"],
            "-np", str(processors), solver["entrypoint"], "-parallel", "-case", str(case)]


class SolverExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["finished", "refused"]
    reason: str | None = None
    iterations: list[IterationResiduals] = []
    residual_converged: bool = False
    solver_version: str | None = None


def _case_input_error(contract: PipeStudyContract, case_dir: Path, mesh: MeshExecution, version: str,
                      processors: int = 1) -> str | None:
    if mesh.status != "accepted" or not mesh.check.accepted or mesh.case_manifest is None:
        return "mesh has no accepted check"
    if mesh.check.plan != mesh_plan(contract):
        return "mesh check belongs to a different case"
    manifest_path = case_dir / MANIFEST_NAME
    try:
        recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "case manifest is missing or invalid"
    if recorded != mesh.case_manifest:
        return "case manifest differs from the checked mesh"
    if recorded.get("contract_digest") != contract_digest(contract):
        return "case contract differs from the checked mesh"
    if recorded.get("tool_versions", {}).get("openfoam") != version:
        return "solver version differs from the checked mesh"
    if recorded.get("processors", 1) != processors:
        return "case was prepared for a different number of processors"
    if (case_dir / "simpleFoam.log").exists():
        return "case already has a solver run; retry requires a fresh case"
    for relative, expected in recorded.get("files", {}).items():
        path = (case_dir / relative).resolve()
        if not path.is_relative_to(case_dir) or not path.is_file():
            return f"case input {relative} is missing or outside the case"
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            return f"case input {relative} changed after mesh preparation"
    return None


def _parallel_step(case_dir: Path, tool_id: str, args: list[str], registry_path: Path | None, cancel: Event | None,
                   on_log: Callable[[str, str], None] | None, version: str) -> SolverExecution | None:
    """Run decomposePar or reconstructPar; a refusal when it did not finish cleanly, otherwise None."""
    live = (lambda line: on_log(tool_id, line)) if on_log is not None else None
    result = run_tool(tool_id, args, case_dir, timeout=PARALLEL_STEP_TIMEOUT_S, registry_path=registry_path,
                      cancel=cancel, **({"on_stdout": live} if live is not None else {}))
    (case_dir / f"{tool_id.removeprefix('openfoam_')}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode != 0:
        return SolverExecution(status="refused", reason=f"{tool_id}: {result.code or f'exit_{result.returncode}'}",
                               solver_version=version)
    return None


def execute_serial_solver(
    contract: PipeStudyContract, case_dir: Path, mesh: MeshExecution, *,
    registry_path: Path | None = None, cancel: Event | None = None,
    on_residual: Callable[[IterationResiduals], None] | None = None,
) -> SolverExecution:
    """Run pinned simpleFoam only for the exact case whose mesh was accepted."""
    return execute_solver(contract, case_dir, mesh, 1, registry_path=registry_path, cancel=cancel,
                          on_residual=on_residual)


def execute_solver(
    contract: PipeStudyContract, case_dir: Path, mesh: MeshExecution, processors: int, *,
    registry_path: Path | None = None, cancel: Event | None = None,
    on_residual: Callable[[IterationResiduals], None] | None = None,
    on_log: Callable[[str, str], None] | None = None,
) -> SolverExecution:
    """Run pinned simpleFoam (serial, or decomposePar + mpiexec + reconstructPar) on the exact accepted case."""
    case_dir = Path(case_dir).resolve()
    solver = resolve_tool("openfoam_simplefoam", registry_path)
    version = solver["version_pin"]
    if not 1 <= processors <= MAX_PROCESSORS:
        return SolverExecution(status="refused", reason=f"processors must be 1 to {MAX_PROCESSORS}",
                               solver_version=version)
    reason = _case_input_error(contract, case_dir, mesh, version, processors)
    if reason:
        return SolverExecution(status="refused", reason=reason, solver_version=version)
    if cancel is not None and cancel.is_set():
        return SolverExecution(status="refused", reason="CANCELLED", solver_version=version)
    solver_tool, solver_args = "openfoam_simplefoam", ["-case", str(case_dir)]
    if processors > 1:
        try:
            for tool_id in PARALLEL_TOOLS:
                resolve_tool(tool_id, registry_path)
        except ToolRegistryError as exc:
            return SolverExecution(status="refused", reason=f"parallel tool unavailable: {exc}",
                                   solver_version=version)
        solver_tool, solver_args = "openfoam_mpiexec", mpi_solver_args(solver, processors, case_dir)
        if not Path(solver_args[2]).is_file():
            return SolverExecution(status="refused", reason="MPI Pstream library is missing",
                                   solver_version=version)
        refusal = _parallel_step(case_dir, "openfoam_decomposepar", ["-case", str(case_dir)], registry_path, cancel,
                                 on_log, version)
        if refusal is not None:
            return refusal
    live_stream = ResidualStream()
    live_parse_failed = False

    def forward(chunk: str) -> None:
        nonlocal live_parse_failed
        if live_parse_failed:
            return
        try:
            rows = live_stream.feed(chunk)
        except LogParseError:
            # Keep the process log for the final fail-closed parse and diagnosis.
            live_parse_failed = True
            return
        for row in rows:
            assert on_residual is not None
            on_residual(row)

    result = run_tool(solver_tool, solver_args, case_dir,
                      timeout=contract.solver.wall_time_limit_s, registry_path=registry_path, cancel=cancel,
                      on_stdout=forward if on_residual is not None else None)
    log = result.stdout + result.stderr
    (case_dir / "simpleFoam.log").write_text(log, encoding="utf-8")
    stream = ResidualStream()
    try:
        rows = stream.feed(log)
        rows.extend(stream.close())
    except LogParseError:
        return SolverExecution(status="refused", reason="solver residual log is invalid", solver_version=version)
    if result.returncode != 0:
        return SolverExecution(status="refused", reason=result.code or f"exit_{result.returncode}",
                               iterations=rows, solver_version=version)
    if stream.fatal:
        return SolverExecution(status="refused", reason="solver reported a fatal error",
                               iterations=rows, solver_version=version)
    if not rows or not stream.solver_reported_end:
        return SolverExecution(status="refused", reason="solver log has no completed end",
                               iterations=rows, solver_version=version)
    if processors > 1:
        refusal = _parallel_step(case_dir, "openfoam_reconstructpar", ["-latestTime", "-case", str(case_dir)],
                                 registry_path, cancel, on_log, version)
        if refusal is not None:
            return refusal.model_copy(update={"iterations": rows})
    fields = required_fields(("k", "omega") if contract.turbulence_model == "kOmegaSST" else
                             ("k", "epsilon") if contract.turbulence_model == "kEpsilon" else ())
    passed = converged(rows, fields, contract.solver.residual_target)
    return SolverExecution(status="finished", iterations=rows, residual_converged=passed,
                           solver_version=version)
