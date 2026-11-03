"""The case operation exposes real progress and never runs a solver on a refused mesh."""

import re
from pathlib import Path

from app.modules.bluecad.cfd_pipe import case_execution, mesh_execution
from app.modules.bluecad.cfd_pipe.mesh_execution import MeshExecution
from app.modules.bluecad.cfd_pipe.solver_execution import SolverExecution
from app.modules.bluecad.registry import ToolRunResult
from tests.test_216_cfd_pipe_case import FIXTURES, laminar


def test_pipe_case_streams_mesh_progress_then_runs_solver(monkeypatch, tmp_path: Path) -> None:
    contract = laminar()
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(mesh_execution, "resolve_tool", lambda *args: {"version_pin": "v2412"})

    def run(tool_id, args, cwd, timeout, registry_path, *, cancel, on_stdout):
        output = (re.sub(r"(?m)^\s*cells:\s+\d+\s*$",
                         f"    cells: {mesh_execution.mesh_plan(contract).cell_count}",
                         (FIXTURES / "checkMesh_ok.log").read_text())
                  if tool_id == "openfoam_checkmesh" else "mesh built\n")
        on_stdout("mesh live\n")
        return ToolRunResult(0, output, "")

    monkeypatch.setattr(mesh_execution, "run_tool", run)

    def solver(*args, **kwargs):
        assert events == [("openfoam_blockmesh", "mesh live\n"),
                          ("openfoam_checkmesh", "mesh live\n")]
        assert kwargs["on_residual"] is not None
        return SolverExecution(status="finished", residual_converged=True, solver_version="v2412")

    monkeypatch.setattr(case_execution, "execute_serial_solver", solver)
    result = case_execution.execute_pipe_case(contract, tmp_path / "case", on_log=lambda tool, line: events.append(
        (tool, line)), on_residual=lambda row: None)
    assert result.status == "finished"
    assert result.mesh.check.accepted
    assert result.solver is not None and result.solver.residual_converged


def test_pipe_case_refused_mesh_never_starts_solver(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mesh_execution, "plan_refusals", lambda contract, plan: ["cell budget exceeded"])
    monkeypatch.setattr(case_execution, "execute_serial_solver", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("solver must not start")))
    result = case_execution.execute_pipe_case(laminar(), tmp_path / "case")
    assert result.status == "refused"
    assert result.solver is None
    assert not (tmp_path / "case").exists()


def test_pipe_case_with_processors_prepares_a_parallel_case_and_runs_the_mpi_solver(monkeypatch, tmp_path: Path) -> None:
    seen: dict = {}

    def mesh(contract, case_dir, **kwargs):
        seen["mesh"] = kwargs["processors"]
        return MeshExecution.model_construct(status="accepted", check=None, reason=None, case_manifest={})

    def solver(contract, case_dir, mesh_result, processors, **kwargs):
        seen["solver"] = (processors, kwargs["on_log"] is not None)
        return SolverExecution(status="finished", solver_version="v2412")

    monkeypatch.setattr(case_execution, "prepare_checked_mesh", mesh)
    monkeypatch.setattr(case_execution, "execute_solver", solver)
    monkeypatch.setattr(case_execution, "execute_serial_solver", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("serial solver must not run")))
    result = case_execution.execute_pipe_case(laminar(), tmp_path / "case", on_log=lambda *a: None, processors=6)
    assert result.status == "finished" and seen == {"mesh": 6, "solver": (6, True)}
