"""A solver cannot run on a different or altered checked case."""

from pathlib import Path

from app.modules.bluecad.cfd_pipe import solver_execution
from app.modules.bluecad.cfd_pipe.casegen import mesh_plan, write_case
from app.modules.bluecad.cfd_pipe.mesh_check import CheckMeshReport, evaluate_mesh
from app.modules.bluecad.cfd_pipe.mesh_execution import MeshExecution
from app.modules.bluecad.registry import ToolRegistryError, ToolRunResult
from tests.test_216_cfd_pipe_case import FIXTURES, laminar


def checked_case(tmp_path: Path, processors: int = 1):
    contract = laminar()
    case_dir = tmp_path / "case"
    manifest = write_case(contract, case_dir, {"openfoam": "v2412"}, processors)
    plan = mesh_plan(contract)
    check = evaluate_mesh(contract, plan, CheckMeshReport(
        cells=plan.cell_count, max_non_orthogonality_deg=1, max_skewness=0.1,
        max_aspect_ratio=10, failed_checks=0,
    ))
    assert check.accepted
    return contract, case_dir, MeshExecution(status="accepted", check=check, case_manifest=manifest)


def test_solver_refuses_altered_input_or_version_without_execution(monkeypatch, tmp_path: Path) -> None:
    contract, case_dir, mesh = checked_case(tmp_path)
    monkeypatch.setattr(solver_execution, "resolve_tool", lambda *args: {"version_pin": "v2506"})
    monkeypatch.setattr(solver_execution, "run_tool", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ran")))
    result = solver_execution.execute_serial_solver(contract, case_dir, mesh)
    assert result.status == "refused"
    assert "version" in result.reason

    monkeypatch.setattr(solver_execution, "resolve_tool", lambda *args: {"version_pin": "v2412"})
    (case_dir / "system/fvSolution").write_text("modified", encoding="utf-8")
    result = solver_execution.execute_serial_solver(contract, case_dir, mesh)
    assert result.status == "refused"
    assert "changed" in result.reason


def test_solver_retains_residual_evidence_without_claiming_qualification(monkeypatch, tmp_path: Path) -> None:
    contract, case_dir, mesh = checked_case(tmp_path)
    monkeypatch.setattr(solver_execution, "resolve_tool", lambda *args: {"version_pin": "v2412"})
    log = (FIXTURES / "simpleFoam_kOmegaSST.log").read_text(encoding="utf-8")
    monkeypatch.setattr(solver_execution, "run_tool", lambda *args, **kwargs: ToolRunResult(0, log, ""))
    result = solver_execution.execute_serial_solver(contract, case_dir, mesh)
    assert result.status == "finished"
    assert [row.iteration for row in result.iterations] == [1, 2, 3, 40]
    assert not result.residual_converged
    assert (case_dir / "simpleFoam.log").read_text(encoding="utf-8") == log


def test_solver_refuses_interrupted_log(monkeypatch, tmp_path: Path) -> None:
    contract, case_dir, mesh = checked_case(tmp_path)
    monkeypatch.setattr(solver_execution, "resolve_tool", lambda *args: {"version_pin": "v2412"})
    log = (FIXTURES / "simpleFoam_kOmegaSST.log").read_text(encoding="utf-8").removesuffix("End\n")
    monkeypatch.setattr(solver_execution, "run_tool", lambda *args, **kwargs: ToolRunResult(0, log, ""))
    result = solver_execution.execute_serial_solver(contract, case_dir, mesh)
    assert result.status == "refused"
    assert result.reason == "solver log has no completed end"
    assert len(result.iterations) == 4


def test_solver_forwards_completed_residuals_during_execution(monkeypatch, tmp_path: Path) -> None:
    contract, case_dir, mesh = checked_case(tmp_path)
    monkeypatch.setattr(solver_execution, "resolve_tool", lambda *args: {"version_pin": "v2412"})
    log = (FIXTURES / "simpleFoam_kOmegaSST.log").read_text(encoding="utf-8")
    observed: list[int] = []

    def fake_run(*args, **kwargs):
        for line in log.splitlines(keepends=True):
            kwargs["on_stdout"](line)
        assert observed == [1, 2, 3, 40]
        return ToolRunResult(0, log, "")

    monkeypatch.setattr(solver_execution, "run_tool", fake_run)
    result = solver_execution.execute_serial_solver(contract, case_dir, mesh,
                                                    on_residual=lambda row: observed.append(row.iteration))
    assert result.status == "finished"
    assert observed == [row.iteration for row in result.iterations]


def _mpi_registry(monkeypatch, tmp_path: Path, *, pstream: bool = True, parallel: bool = True) -> Path:
    root = tmp_path / "prefix"
    (root / "bin").mkdir(parents=True, exist_ok=True)
    if pstream:
        (root / "lib" / "mpich-3.3").mkdir(parents=True, exist_ok=True)
        (root / "lib" / "mpich-3.3" / "libPstream.so").write_bytes(b"x")

    def resolve(tool_id, *_):
        if tool_id in solver_execution.PARALLEL_TOOLS and not parallel:
            raise ToolRegistryError("TOOL_DISABLED", "tool is disabled")
        return {"version_pin": "v2412", "entrypoint": str(root / "bin" / "simpleFoam")}

    monkeypatch.setattr(solver_execution, "resolve_tool", resolve)
    return root


def test_mpi_solver_runs_decompose_mpiexec_reconstruct_in_order(monkeypatch, tmp_path: Path) -> None:
    contract, case_dir, mesh = checked_case(tmp_path, 4)
    root = _mpi_registry(monkeypatch, tmp_path)
    log = (FIXTURES / "simpleFoam_kOmegaSST.log").read_text(encoding="utf-8")
    calls: list[tuple[str, list[str], float]] = []
    observed: list[int] = []

    def fake_run(tool_id, args, cwd, timeout, registry_path=None, *, cancel=None, on_stdout=None):
        calls.append((tool_id, args, timeout))
        if tool_id == "openfoam_mpiexec":
            for line in log.splitlines(keepends=True):
                on_stdout(line)
            return ToolRunResult(0, log, "")
        return ToolRunResult(0, f"{tool_id} done\n", "")

    monkeypatch.setattr(solver_execution, "run_tool", fake_run)
    result = solver_execution.execute_solver(contract, case_dir, mesh, 4,
                                             on_residual=lambda row: observed.append(row.iteration))
    case = str(case_dir.resolve())
    entry = str(root / "bin" / "simpleFoam")
    assert result.status == "finished" and observed == [1, 2, 3, 40]
    assert [call[0] for call in calls] == ["openfoam_decomposepar", "openfoam_mpiexec", "openfoam_reconstructpar"]
    assert calls[0][1] == ["-case", case]
    assert calls[1][1] == ["-genv", "LD_PRELOAD", str(root / "lib" / "mpich-3.3" / "libPstream.so"),
                           "-genv", "WM_PROJECT_VERSION", "v2412", "-np", "4", entry, "-parallel", "-case", case]
    assert calls[1][2] == contract.solver.wall_time_limit_s
    assert calls[2][1] == ["-latestTime", "-case", case]
    assert (case_dir / "simpleFoam.log").read_text(encoding="utf-8") == log
    assert (case_dir / "decomposepar.log").is_file() and (case_dir / "reconstructpar.log").is_file()


def test_mpi_solver_refuses_failed_steps_and_missing_prerequisites(monkeypatch, tmp_path: Path) -> None:
    contract, case_dir, mesh = checked_case(tmp_path, 2)
    _mpi_registry(monkeypatch, tmp_path)
    log = (FIXTURES / "simpleFoam_kOmegaSST.log").read_text(encoding="utf-8")
    seen: list[str] = []

    def failing(step):
        def run(tool_id, *args, **kwargs):
            seen.append(tool_id)
            if tool_id == step:
                return ToolRunResult(1, "", "boom", code="PROCESS_FAILED")
            return ToolRunResult(0, log if tool_id == "openfoam_mpiexec" else "ok", "")
        return run

    monkeypatch.setattr(solver_execution, "run_tool", failing("openfoam_decomposepar"))
    result = solver_execution.execute_solver(contract, case_dir, mesh, 2)
    assert result.status == "refused" and result.reason == "openfoam_decomposepar: PROCESS_FAILED"
    assert seen == ["openfoam_decomposepar"]  # the solver never starts on a failed decomposition

    seen.clear()
    monkeypatch.setattr(solver_execution, "run_tool", failing("openfoam_reconstructpar"))
    result = solver_execution.execute_solver(contract, case_dir, mesh, 2)
    assert result.status == "refused" and result.reason == "openfoam_reconstructpar: PROCESS_FAILED"
    assert len(result.iterations) == 4

    (case_dir / "simpleFoam.log").unlink()
    monkeypatch.setattr(solver_execution, "run_tool", failing("openfoam_mpiexec"))
    result = solver_execution.execute_solver(contract, case_dir, mesh, 2)
    assert result.status == "refused" and result.reason == "PROCESS_FAILED"
    assert seen[-2:] == ["openfoam_decomposepar", "openfoam_mpiexec"]  # no reconstruction after a failed solver


def test_mpi_solver_refuses_without_pstream_parallel_tools_or_matching_case(monkeypatch, tmp_path: Path) -> None:
    contract, case_dir, mesh = checked_case(tmp_path, 2)
    monkeypatch.setattr(solver_execution, "run_tool", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    _mpi_registry(monkeypatch, tmp_path, pstream=False)
    result = solver_execution.execute_solver(contract, case_dir, mesh, 2)
    assert result.status == "refused" and "Pstream" in result.reason

    _mpi_registry(monkeypatch, tmp_path, parallel=False)
    result = solver_execution.execute_solver(contract, case_dir, mesh, 2)
    assert result.status == "refused" and "unavailable" in result.reason

    _mpi_registry(monkeypatch, tmp_path)
    result = solver_execution.execute_solver(contract, case_dir, mesh, 3)  # case was prepared for 2 ranks
    assert result.status == "refused" and "processors" in result.reason
    assert solver_execution.execute_solver(contract, case_dir, mesh, 65).status == "refused"
    serial_contract, serial_dir, serial_mesh = checked_case(tmp_path / "serial")
    assert "processors" in solver_execution.execute_solver(serial_contract, serial_dir, serial_mesh, 2).reason


def test_mpi_solver_stops_on_cancel_during_decompose(monkeypatch, tmp_path: Path) -> None:
    from threading import Event

    contract, case_dir, mesh = checked_case(tmp_path, 2)
    _mpi_registry(monkeypatch, tmp_path)
    cancel = Event()
    seen: list[Event | None] = []

    def run(tool_id, args, cwd, timeout, registry_path=None, *, cancel=None, on_stdout=None):
        seen.append(cancel)
        return ToolRunResult(-15, "", "", code="CANCELLED")

    monkeypatch.setattr(solver_execution, "run_tool", run)
    result = solver_execution.execute_solver(contract, case_dir, mesh, 2, cancel=cancel)
    assert result.status == "refused" and result.reason.endswith("CANCELLED") and seen == [cancel]
