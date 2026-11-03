from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Timer

import pytest

from app.modules.bluecad.cfd_adapter import (
    EVALUATOR_ID,
    OpenFoamEvaluator,
    case_digest,
    case_lineage,
    case_parameters,
    generate_case,
    parse_probes,
    parse_residuals,
    parse_velocity_probes,
)
from app.modules.bluecad.registry import run_tool
from app.modules.engineering.evaluator_contracts import EvaluationRequest, NamedQuantity
from app.modules.engineering.refs import EvaluationRequestRef, PhysicsCaseRef, Quantity

PARAMS = {"diameter": 0.01, "length": 0.2, "velocity": 0.01, "viscosity": 1e-6}
FIXTURES = Path(__file__).resolve().parents[2] / "scripts/benchmarks/openfoam"


def request(**changes: object) -> EvaluationRequest:
    now = datetime.now(UTC)
    values = {
        "request_ref": EvaluationRequestRef(authority_owner="engineering", object_id="request", workspace_id="test", content_digest=case_digest(PARAMS)),
        "evaluator_id": EVALUATOR_ID,
        "subject_ref": PhysicsCaseRef(authority_owner="engineering", object_id="case", workspace_id="test", content_digest=case_digest(PARAMS)),
        "inputs": tuple(NamedQuantity(name=name, value=Quantity(value=value, unit=unit)) for name, value, unit in (
            ("diameter", 10, "mm"), ("length", 20, "cm"), ("velocity", 1, "cm/s"), ("viscosity", 1, "mm**2/s")
        )),
        "requested_at": now,
        "deadline_at": now + timedelta(minutes=2),
    }
    values.update(changes)
    return EvaluationRequest(**values)


def test_case_identity_units_and_labels(tmp_path: Path) -> None:
    params = case_parameters(request())
    assert params == pytest.approx(PARAMS)
    generate_case(tmp_path, params)
    assert "inlet" in (tmp_path / "system/blockMeshDict").read_text()
    assert "outlet" in (tmp_path / "system/blockMeshDict").read_text()
    assert "frontAndBack" in (tmp_path / "system/blockMeshDict").read_text()
    assert case_digest(params) in (tmp_path / "case.json").read_text()
    assert "dimensions [0 2 -2" in (tmp_path / "0/p").read_text()


def test_mesh_and_result_lineage(tmp_path: Path) -> None:
    import hashlib

    mesh = tmp_path / "constant/polyMesh"
    mesh.mkdir(parents=True)
    (mesh / "points").write_text("actual mesh bytes")
    lineage = case_lineage(request(), tmp_path, PARAMS, 0.12, 0.015)
    assert lineage["case_digest"] == request().subject_ref.content_digest
    assert lineage["mesh_sha256"]["points"] == hashlib.sha256(b"actual mesh bytes").hexdigest()
    assert lineage["results"] == {"kinematic_pressure_drop_m2_s2": 0.12, "centreline_velocity_m_s": 0.015}


def test_stale_case_ref_refused() -> None:
    stale = PhysicsCaseRef(authority_owner="engineering", object_id="case", workspace_id="test", content_digest="sha256:" + "a" * 64)
    outcome = OpenFoamEvaluator().evaluate(request(subject_ref=stale))
    assert (outcome.status, outcome.failure.category) == ("refused", "invalid_input")


def test_disabled_registry_is_truthful() -> None:
    adapter = OpenFoamEvaluator()
    assert adapter.availability().state == "disabled"
    outcome = adapter.evaluate(request())
    assert (outcome.status, outcome.failure.category) == ("refused", "not_available")
    assert not outcome.outputs and not outcome.output_artifacts


def test_missing_binary_is_not_installed(tmp_path: Path) -> None:
    import yaml

    config = yaml.safe_load((Path(__file__).resolve().parents[2] / "configs/bluecad_tools.yaml").read_text())
    for tool in config["tools"]:
        if tool["id"] == "openfoam":
            tool.update(enabled=True, entrypoint=str(tmp_path / "missing"), binary_sha256="0" * 64, provenance_url="https://www.openfoam.com/")
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(config))
    adapter = OpenFoamEvaluator(path)
    assert adapter.availability().state == "not_installed"
    outcome = adapter.evaluate(request())
    assert (outcome.status, outcome.failure.category) == ("refused", "not_available")


def test_cancel_and_deadline() -> None:
    cancellation = Event()
    cancellation.set()
    assert OpenFoamEvaluator(cancellation=cancellation).evaluate(request()).status == "cancelled"
    now = datetime.now(UTC)
    expired = request(requested_at=now-timedelta(minutes=2), deadline_at=now-timedelta(minutes=1))
    assert OpenFoamEvaluator().evaluate(expired).status == "deadline_exceeded"


def test_real_captured_parser_and_hostile_fields(tmp_path: Path) -> None:
    numerical = parse_residuals((FIXTURES / "solver_tail.txt").read_text())
    assert numerical.iterations > 0 and numerical.final_residual is not None
    p0, p1 = parse_probes(FIXTURES / "probe_p.txt")
    assert p0 > p1
    assert parse_velocity_probes(FIXTURES / "probe_U.txt") > 0
    hostile = tmp_path / "hostile"
    hostile.write_text("1 nan 2\n")
    with pytest.raises(ValueError):
        parse_probes(hostile)
    with pytest.raises(ValueError):
        parse_residuals("no residuals")
    with pytest.raises(ValueError):
        parse_residuals("Solving for p, Initial residual = 1e-3, Final residual = 1e-6\nSolving for p, Initial residual = nan, Final residual = 1e-9")
    with pytest.raises(ValueError):
        parse_velocity_probes(hostile)


def test_registry_timeout_and_cancel(tmp_path: Path) -> None:
    import hashlib

    import yaml

    script = tmp_path / "sleep_tool"
    script.write_text("#!/bin/sh\nexec /bin/sleep 2\n")
    script.chmod(0o755)
    config = yaml.safe_load((Path(__file__).resolve().parents[2] / "configs/bluecad_tools.yaml").read_text())
    for tool in config["tools"]:
        if tool["id"] == "openfoam":
            tool.update(enabled=True, entrypoint=str(script), binary_sha256=hashlib.sha256(script.read_bytes()).hexdigest(), provenance_url="https://www.openfoam.com/")
    registry = tmp_path / "registry.yaml"
    registry.write_text(yaml.safe_dump(config))
    assert run_tool("openfoam", [], tmp_path, 0.01, registry, cancel=Event()).code == "TIMEOUT"
    event = Event()
    timer = Timer(0.02, event.set)
    timer.start()
    try:
        assert run_tool("openfoam", [], tmp_path, 1, registry, cancel=event).code == "CANCELLED"
    finally:
        timer.join()


@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("mode", ["cancel", "timeout", "exit"])
def test_run_tool_kills_descendants_holding_the_pipes(tmp_path: Path, streaming: bool, mode: str) -> None:
    """A grandchild (an MPI rank) must not outlive cancel/timeout nor block the run from ending."""
    import hashlib
    import os
    import time

    import yaml

    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "mpi_like_tool"
    tail = "" if mode == "exit" else "/bin/sleep 60\n"
    script.write_text(f"#!/bin/sh\n/bin/sleep 60 &\necho $! > {pidfile}\necho started\n{tail}")
    script.chmod(0o755)
    config = yaml.safe_load((Path(__file__).resolve().parents[2] / "configs/bluecad_tools.yaml").read_text())
    for tool in config["tools"]:
        if tool["id"] == "openfoam":
            tool.update(enabled=True, entrypoint=str(script), binary_sha256=hashlib.sha256(script.read_bytes()).hexdigest(), provenance_url="https://www.openfoam.com/")
    registry = tmp_path / "registry.yaml"
    registry.write_text(yaml.safe_dump(config))
    event = Event()
    timer = Timer(0.5, event.set)
    if mode == "cancel":
        timer.start()
    kwargs = {"on_stdout": lambda line: None} if streaming else {}
    if not streaming and mode == "exit":
        pytest.skip("the plain capture branch has no cancel handling to exercise")
    started = time.monotonic()
    try:
        result = run_tool("openfoam", [], tmp_path, 1 if mode == "timeout" else 30, registry, cancel=event, **kwargs)
    finally:
        timer.cancel()
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 9)  # self-cleaning if the fix regressed
            except (ProcessLookupError, ValueError):
                pass
    assert time.monotonic() - started < 5
    assert result.code == {"cancel": "CANCELLED", "timeout": "TIMEOUT", "exit": None}[mode]
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            with open(f"/proc/{pid}/stat") as stat:
                if stat.read().split(")")[-1].split()[0] == "Z":
                    break
        except (ProcessLookupError, FileNotFoundError):
            break
        time.sleep(0.05)
    else:
        raise AssertionError("grandchild survived")


@pytest.mark.skipif(not __import__("os").environ.get("JARVIS_OPENFOAM_PREFIX"), reason="real solver opt in")
def test_real_solver_opt_in(tmp_path: Path) -> None:
    import hashlib
    import os
    import zipfile

    import yaml

    from app.core.database import initialize_database
    from app.core.paths import build_paths
    from app.modules.workspaces.models import WorkspaceCreate
    from app.modules.workspaces.service import create_workspace

    prefix = Path(os.environ["JARVIS_OPENFOAM_PREFIX"])
    registry = yaml.safe_load((Path(__file__).resolve().parents[2] / "configs/bluecad_tools.yaml").read_text())
    for tool in registry["tools"]:
        if tool["id"] in {"openfoam", "openfoam_blockmesh"}:
            binary = prefix / "bin" / ("icoFoam" if tool["id"] == "openfoam" else "blockMesh")
            tool.update(enabled=True, entrypoint=str(binary), binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(), provenance_url="https://www.openfoam.com/releases/openfoam-v2412/")
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(yaml.safe_dump(registry))
    assert OpenFoamEvaluator(registry_path).availability().state == "available"
    initialize_database()
    workspace = create_workspace(WorkspaceCreate(name="CFD proof", slug="cfd-proof"))
    base = request()
    live_request = base.model_copy(update={
        "request_ref": base.request_ref.model_copy(update={"workspace_id": workspace.id}),
        "subject_ref": base.subject_ref.model_copy(update={"workspace_id": workspace.id}),
        "deadline_at": datetime.now(UTC) + timedelta(minutes=5),
    })
    outcome = OpenFoamEvaluator(registry_path).evaluate(live_request)
    assert outcome.status == "succeeded", outcome.failure
    assert outcome.validity is None and outcome.qualification_record_ref is None
    assert outcome.numerical.converged is True
    assert {item.name for item in outcome.outputs} == {"kinematic_pressure_drop", "centreline_velocity"}
    archive = build_paths().artifacts_dir / workspace.id / "cfd" / (outcome.output_artifacts[0].content_digest[7:] + ".zip")
    with zipfile.ZipFile(archive) as bundle:
        assert "lineage.json" in bundle.namelist()
        assert "constant/polyMesh/points" in bundle.namelist()
