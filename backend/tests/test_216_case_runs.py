"""Spec 216 contract 4: evaluator-owned background pipe-case runs with live events, safe stop and honest recovery."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.database import initialize_database
from app.main import app
from app.modules.bluecad.cfd_pipe import case_runs
from app.modules.bluecad.cfd_pipe.case_execution import PipeCaseExecution
from app.modules.bluecad.cfd_pipe.mesh_execution import MeshExecution
from app.modules.bluecad.cfd_pipe.residuals import IterationResiduals
from app.modules.bluecad.cfd_pipe.solver_execution import SolverExecution
from tests.test_216_component_reduced_models import _ready
from tests.test_216_component_study_templates import _set
from tests.test_216_process_pin_from_design import water  # noqa: F401  (autouse fixture)
from tests.test_216_reduced_model import turbulent

ROW = IterationResiduals(iteration=1, initial={"p": 0.5, "Ux": 0.1}, final={"p": 0.01, "Ux": 0.001})


def _wait(client: TestClient, url: str, states: set[str]) -> dict[str, Any]:
    deadline = time.time() + 10
    while time.time() < deadline:
        record = client.get(url).json()
        if record["state"] in states:
            return record
        time.sleep(0.02)
    raise AssertionError(f"run never reached {states}: {record}")


def _finished(status: str = "finished") -> PipeCaseExecution:
    mesh = MeshExecution.model_construct(status="accepted", check=None, reason=None, case_manifest=None)
    solver = SolverExecution(status="finished" if status == "finished" else "refused", iterations=[ROW],
                             reason=None if status == "finished" else "diverged at /secret/path/x")
    return PipeCaseExecution.model_construct(status=status, mesh=mesh, solver=solver)


@pytest.fixture
def setup():
    initialize_database()
    with TestClient(app) as client:
        project_id, base, component_id, _ = _ready(client, f"cases-{time.time_ns()}")
        assert _set(client, base, project_id, component_id, turbulent(1e4)).status_code == 200
        yield client, f"{base}/{component_id}/cfd-runs", project_id


def _start(client, url, **body):
    return client.post(url, json={"regime": "turbulent_rans", **body})


def test_run_streams_events_while_running_then_finishes(monkeypatch, setup) -> None:
    client, url, _ = setup
    release = threading.Event()

    def fake(contract, case_dir, *, cancel, on_log, on_residual, **_):
        on_log("stdout", "Time = 1")
        on_residual(ROW)
        assert release.wait(10)
        return _finished()

    monkeypatch.setattr(case_runs, "execute_pipe_case", fake)
    response = _start(client, url)
    assert response.status_code == 202, response.text
    record = response.json()
    assert record["qualifies_publication"] is False and record["retry_of"] is None
    assert RUN_ID_OK(record["run_id"]) and record["template_digest"]
    run_url = f"{url}/{record['run_id']}"
    deadline = time.time() + 10
    while time.time() < deadline:
        live = client.get(f"{run_url}/events").json()
        if len(live["events"]) == 2:
            break
        time.sleep(0.02)
    assert live["live"] is True and [e["kind"] for e in live["events"]] == ["log", "residual"]
    assert live["events"][0]["line"] == "Time = 1" and live["events"][0]["emitted_at"].endswith("Z")
    assert live["events"][1]["max_normalized_residual"] == 0.5
    assert client.get(f"{run_url}/events?cursor=1").json()["events"][0]["kind"] == "residual"
    assert _start(client, url).status_code == 409  # one active run per component
    assert client.get(run_url).json()["state"] == "running"
    release.set()
    done = _wait(client, run_url, {"finished"})
    assert done["live"] is False and done["iterations"] == 1 and done["events"] == 2
    assert done["qualifies_publication"] is False
    assert len(client.get(url).json()) == 1


def test_stop_is_cooperative_and_a_stopped_run_is_never_finished(monkeypatch, setup) -> None:
    client, url, _ = setup
    entered = threading.Event()

    def fake(contract, case_dir, *, cancel, **_):
        entered.set()
        assert cancel.wait(10)
        return _finished()  # even a result returned after the stop request is not reported as finished

    monkeypatch.setattr(case_runs, "execute_pipe_case", fake)
    run_url = f"{url}/{_start(client, url).json()['run_id']}"
    assert entered.wait(10)
    stopped = client.post(f"{run_url}/stop").json()
    assert stopped["state"] == "stopping" and stopped["stop"] == "requested"
    assert _wait(client, run_url, {"stopped"})["state"] == "stopped"
    assert client.post(f"{run_url}/stop").json()["stop"] == "noop"


def test_exception_fails_without_traceback_and_refusal_is_recorded(monkeypatch, setup) -> None:
    client, url, _ = setup

    def boom(*_a, **_k):
        raise RuntimeError("tool exploded at /home/x/case/log")

    monkeypatch.setattr(case_runs, "execute_pipe_case", boom)
    run_url = f"{url}/{_start(client, url).json()['run_id']}"
    failed = _wait(client, run_url, {"failed"})
    assert failed["message"] == "tool exploded at <path>" and "Traceback" not in json.dumps(failed)

    monkeypatch.setattr(case_runs, "execute_pipe_case", lambda *a, **k: _finished("refused"))
    refused = _wait(client, f"{url}/{_start(client, url, retry_of=failed['run_id']).json()['run_id']}", {"refused"})
    assert refused["retry_of"] == failed["run_id"] and refused["solver_reason"] == "diverged at <path>"
    assert refused["qualifies_publication"] is False


def test_route_refusals(monkeypatch, setup) -> None:
    client, url, _ = setup
    monkeypatch.setattr(case_runs, "execute_pipe_case", lambda *a, **k: _finished())
    assert client.post(url, json={"regime": "laminar"}).status_code == 409  # no saved laminar template
    assert client.post(url, json={"regime": "bogus"}).status_code == 422
    assert _start(client, url, retry_of="0" * 32).status_code == 404
    assert _start(client, url, retry_of="../x").status_code == 422
    assert client.get(f"{url}/not-a-run").status_code == 404
    assert client.get(f"{url}/{'a' * 32}/events").status_code == 404
    assert client.post(f"{url}/{'a' * 32}/stop").status_code == 404
    assert client.get(url.replace("/cfd-runs", "x/cfd-runs")).status_code == 404


def test_interrupted_recovery_and_settling_do_not_invent_a_result(setup) -> None:
    client, url, _ = setup
    component_id = url.split("/")[-2]
    for state, run_id in (("running", "1" * 32), ("stopping", "2" * 32), ("finished", "3" * 32)):
        run = case_runs._component_dir(_ws(client, url), component_id) / run_id
        run.mkdir(parents=True)
        case_runs._write(run / "run.json", {"run_id": run_id, "state": state, "queued_at": "2020-01-01T00:00:00Z"})
    # read-time settling: an unowned running record is reported interrupted
    assert client.get(f"{url}/{'1' * 32}").json()["state"] == "interrupted"
    assert case_runs.recover_interrupted() == 1  # the stopping one; the other was already settled
    assert client.get(f"{url}/{'2' * 32}").json()["state"] == "interrupted"
    assert client.get(f"{url}/{'3' * 32}").json()["state"] == "finished"
    assert "no result was recorded" in client.get(f"{url}/{'2' * 32}").json()["message"]
    assert client.get(f"{url}/{'2' * 32}").json()["live"] is False


def test_event_reads_are_cursored_and_bounded(monkeypatch, tmp_path, setup) -> None:
    client, url, _ = setup
    component_id = url.split("/")[-2]
    run_id = "4" * 32
    run = case_runs._component_dir(_ws(client, url), component_id) / run_id
    run.mkdir(parents=True)
    case_runs._write(run / "run.json", {"run_id": run_id, "state": "finished", "queued_at": "x"})
    total = case_runs.MAX_EVENTS_PER_READ + 5
    lines = [json.dumps({"seq": i, "kind": "log", "emitted_at": "t", "stream": "stdout", "line": str(i)}) + "\n"
             for i in range(1, total + 1)]
    (run / "events.jsonl").write_text("".join(lines) + '{"seq": 9999, "ki', encoding="utf-8")  # partial last line
    first = client.get(f"{url}/{run_id}/events").json()
    assert len(first["events"]) == case_runs.MAX_EVENTS_PER_READ and first["cursor"] == case_runs.MAX_EVENTS_PER_READ
    rest = client.get(f"{url}/{run_id}/events?cursor={first['cursor']}").json()
    assert [e["seq"] for e in rest["events"]] == list(range(first["cursor"] + 1, total + 1))
    assert client.get(f"{url}/{run_id}/events?cursor=999999").json()["events"] == []
    # emission is bounded: past MAX_EVENTS further rows are dropped and flagged, never rewritten
    monkeypatch.setattr(case_runs, "MAX_EVENTS", 2)
    emitter = case_runs._Events(tmp_path)
    for i in range(4):
        emitter.log("stdout", f"l{i}" + "x" * 5000)
    assert emitter.count == 2 and emitter.truncated is True
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 2
    assert len(json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])["line"]) <= case_runs.MAX_LINE_CHARS


def RUN_ID_OK(value: str) -> bool:
    return bool(case_runs.RUN_RE.fullmatch(value))


def _ws(client: TestClient, url: str) -> str:
    from app.core.database import open_sqlite_connection

    project_id = url.split("/workspaces/")[1].split("/")[0]
    with open_sqlite_connection() as connection:
        return connection.execute("SELECT data_workspace_id FROM design_project_versions WHERE project_id = ?",
                                  (project_id,)).fetchone()["data_workspace_id"]


def test_non_finite_residuals_are_recorded_as_null(tmp_path) -> None:
    emitter = case_runs._Events(tmp_path)
    emitter.residual(IterationResiduals(iteration=3, initial={"p": float("nan"), "Ux": 0.2}, final={"p": 0.1},
                                        continuity_local=float("inf")))
    emitter.residual(IterationResiduals(iteration=4, initial={"k": 0.1}, final={"k": 0.01}))  # k only: no judged
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert rows[0]["initial"] == {"p": None, "Ux": 0.2} and rows[0]["continuity_local"] is None
    assert [row["iteration"] for row in rows] == [3, 4]


def _probed(contract, text_p: str, text_u: str, accepted_cells: int | None = 1234):
    """A finished execution whose case directory holds the given probe files (written by the caller's fake)."""
    from types import SimpleNamespace

    check = SimpleNamespace(report=SimpleNamespace(cells=accepted_cells) if accepted_cells else None, plan=None)
    mesh = SimpleNamespace(status="accepted", check=check, reason=None,
                           case_manifest={"tool_versions": {"openfoam": "2412"}, "contract_digest": "x"})
    solver = SolverExecution(status="finished", iterations=[ROW], residual_converged=True, solver_version="2412")
    return SimpleNamespace(status="finished", mesh=mesh, solver=solver)


def _measured_run(monkeypatch, setup, pressure_text, velocity_text):
    client, url, _ = setup

    def fake(contract, case_dir, **_):
        probes = case_dir / "postProcessing" / "pipe_probes" / "0"
        probes.mkdir(parents=True)
        (probes / "p").write_text(pressure_text(contract), encoding="utf-8")
        (probes / "U").write_text(velocity_text(contract), encoding="utf-8")
        return _probed(contract, "", "")

    monkeypatch.setattr(case_runs, "execute_pipe_case", fake)
    run_url = f"{url}/{_start(client, url).json()['run_id']}"
    return _wait(client, run_url, {"finished"})


def test_finished_run_records_the_gradient_measured_from_its_own_probe_files(monkeypatch, setup) -> None:
    from app.modules.bluecad.cfd_pipe.casegen import manifest_digest
    from tests.cfd_run_fixtures import probe_files

    record = _measured_run(monkeypatch, setup, lambda c: probe_files(c, 12.5)[0], lambda c: probe_files(c, 12.5)[1])
    measurement = record["measurement"]
    assert measurement["status"] == "measured" and measurement["reason"] is None
    assert measurement["pressure_gradient_pa_m"] == pytest.approx(12.5, rel=1e-9)
    assert measurement["wall_shear_pa"] == pytest.approx(12.5 * 0.046 / 4.0, rel=1e-9)
    assert measurement["wall_shear_method"] == "developed_pipe_force_balance"
    assert measurement["mesh_level"] == 1 and measurement["cells"] == 1234 and measurement["regime"] == "turbulent_rans"
    assert measurement["tool_versions"] == {"openfoam": "2412", "openfoam_simplefoam": "2412"}
    assert measurement["case_manifest_digest"] == manifest_digest({"tool_versions": {"openfoam": "2412"},
                                                                    "contract_digest": "x"})
    assert measurement["comparison"]["colebrook_smooth_pa_m"] > 0
    assert record["qualifies_publication"] is False and record["contract_digest"].startswith("sha256:")


def test_malformed_or_missing_probes_are_unmeasured_with_a_plain_reason(monkeypatch, setup) -> None:
    record = _measured_run(monkeypatch, setup, lambda c: "garbage", lambda c: "garbage")
    assert record["state"] == "finished" and record["measurement"]["status"] == "unmeasured"
    assert "probe" in record["measurement"]["reason"] and "pressure_gradient_pa_m" not in record["measurement"]
    client, url, _ = setup
    monkeypatch.setattr(case_runs, "execute_pipe_case", lambda *a, **k: _probed(None, "", ""))
    missing = _wait(client, f"{url}/{_start(client, url).json()['run_id']}", {"finished"})
    assert missing["measurement"] == {"status": "unmeasured", "reason": "probe results could not be read: "
                                      "the case wrote no pressure and velocity probe files"}


def test_refused_and_failed_runs_carry_no_measurement(monkeypatch, setup) -> None:
    client, url, _ = setup
    monkeypatch.setattr(case_runs, "execute_pipe_case", lambda *a, **k: _finished("refused"))
    refused = _wait(client, f"{url}/{_start(client, url).json()['run_id']}", {"refused"})
    assert refused["measurement"]["status"] == "unmeasured" and "pressure_gradient_pa_m" not in refused["measurement"]


def test_processors_are_a_recorded_execution_resource_not_part_of_the_contract(monkeypatch, setup) -> None:
    client, url, _ = setup
    seen: list[int] = []

    def fake(contract, case_dir, *, processors=1, **_):
        seen.append(processors)
        return _finished()

    monkeypatch.setattr(case_runs, "execute_pipe_case", fake)
    serial = _start(client, url).json()
    _wait(client, f"{url}/{serial['run_id']}", {"finished"})
    parallel = _start(client, url, processors=8).json()
    done = _wait(client, f"{url}/{parallel['run_id']}", {"finished"})
    assert seen == [1, 8] and serial["processors"] == 1 and done["processors"] == 8
    assert parallel["contract_digest"] == serial["contract_digest"]
    assert parallel["template_digest"] == serial["template_digest"]


@pytest.mark.parametrize("value", [0, -1, 65, 2.5, "4", True, None])
def test_processors_out_of_range_or_mistyped_are_rejected(monkeypatch, setup, value) -> None:
    client, url, _ = setup
    monkeypatch.setattr(case_runs, "execute_pipe_case", lambda *a, **k: _finished())
    assert _start(client, url, processors=value).status_code == 422
    assert client.get(url).json() == []
