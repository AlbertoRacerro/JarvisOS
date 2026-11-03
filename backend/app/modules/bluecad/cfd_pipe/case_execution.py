"""One bounded pipe case, ready for an evaluator-owned background study run.

This operation exposes live tool output and residuals to its caller. It grants no
study qualification or reduced-model publication authority.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.modules.bluecad.cfd_pipe.contract import PipeStudyContract
from app.modules.bluecad.cfd_pipe.mesh_execution import MeshExecution, prepare_checked_mesh
from app.modules.bluecad.cfd_pipe.residuals import IterationResiduals
from app.modules.bluecad.cfd_pipe.solver_execution import SolverExecution, execute_serial_solver, execute_solver


class PipeCaseExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["finished", "refused"]
    mesh: MeshExecution
    solver: SolverExecution | None = None


def execute_pipe_case(
    contract: PipeStudyContract, case_dir: Path, *, registry_path: Path | None = None,
    cancel: Event | None = None, on_log: Callable[[str, str], None] | None = None,
    on_residual: Callable[[IterationResiduals], None] | None = None, processors: int = 1,
) -> PipeCaseExecution:
    """Run a checked mesh followed by the pinned solver (serial, or MPI over ``processors``) in a fresh case."""
    mesh = prepare_checked_mesh(contract, case_dir, registry_path=registry_path, cancel=cancel, on_log=on_log,
                                 processors=processors)
    if mesh.status != "accepted":
        return PipeCaseExecution(status="refused", mesh=mesh)
    if processors > 1:
        solver = execute_solver(contract, case_dir, mesh, processors, registry_path=registry_path, cancel=cancel,
                                on_residual=on_residual, on_log=on_log)
    else:
        solver = execute_serial_solver(contract, case_dir, mesh, registry_path=registry_path,
                                       cancel=cancel, on_residual=on_residual)
    return PipeCaseExecution(status="finished" if solver.status == "finished" else "refused",
                             mesh=mesh, solver=solver)
