"""Spec 210: background Process solves with a live iteration log, safe stop, versioned retry and comparison.

The real draft run path, mixed runtime and tear controller run; only the DWSIM-backed evaluation of one
loop pass is replaced by a controllable fake, so the tests can hold the solve between iterations.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.database import initialize_database
from app.main import app
from app.modules.process_stack import draft, draft_compiler, mixed, mixed_runtime, solve_jobs, tear_solver
from tests import tear_qualification_support as qual
from tests.test_process_mixed_168 import _tear_document


class _Client:
    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class _Loop:
    """A contraction x -> 0.5 x + 0.5 on the tear mass flow; ``gate`` holds chosen iterations."""

    def __init__(self, gain: float = 0.5) -> None:
        self.gain = gain
        self.calls: list[int] = []
        self.gates: dict[int, threading.Event] = {}
        self.entered: dict[int, threading.Event] = {}
        self.busy = False

    def hold(self, iteration: int) -> None:
        self.gates[iteration] = threading.Event()
        self.entered[iteration] = threading.Event()

    def __call__(self, _document: dict, _part: dict, tear: dict, **kwargs: Any) -> dict[str, Any]:
        iteration = kwargs["iteration"]
        full = kwargs.get("full", False)
        if not full:
            self.calls.append(iteration)
            if iteration in self.gates:
                self.entered[iteration].set()
                self.busy = True
                assert self.gates[iteration].wait(20)
                self.busy = False
        produced = copy.deepcopy(tear)
        for state in produced.values():
            state["mass_flow_kg_s"] = self.gain * state["mass_flow_kg_s"] + 0.5
        return {"produced": produced, "elapsed_s_by_phase": {"build": 0.0, "solve": 0.0, "culture": 0.0},
                "max_build_seconds": 0.0, "streams": {"Tear": next(iter(produced.values()))} if full else {},
                "units": {}, "mixed_findings": [], "segments": []}


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch) -> Any:
    initialize_database()
    loop = _Loop()
    monkeypatch.setattr(mixed_runtime, "_evaluate", loop)
    monkeypatch.setattr(mixed_runtime, "_whole_graph_balances",
                        lambda *_args, **_kwargs: {"balances": [], "status": "calculated"})
    monkeypatch.setattr(mixed_runtime, "_culture_results", lambda *_args, **_kwargs: {})

    def prepare(workspace_id: str, draft_id: str, revision: str, action: str) -> draft.PreparedRun:
        directory = draft.draft_dir(workspace_id, draft_id)
        real = draft.load_revision(directory, revision)
        document = {**_tear_document(), **({"solver": real["document"]["solver"]}
                                           if "solver" in real["document"] else {})}
        run = {"run_id": draft_compiler.new_run_id(), "action": action, "draft_id": draft_id,
               "draft_revision": revision, "draft_document_sha256": real["document_sha256"],
               "compiler_version": draft.COMPILER_VERSION, "dwsim_version": "10.2.9",
               "mcp_sha256": "a" * 64, "started_at": draft._now()}
        return draft.PreparedRun(workspace_id, draft_id, directory, {**real, "document": document}, _Client(), run)

    monkeypatch.setattr(draft, "prepare", prepare)
    with TestClient(app) as client:
        workspace = client.post("/workspaces", json={"name": "210 console", "slug": f"solve-210-{time.time_ns()}"})
        workspace.raise_for_status()
        base = f"/workspaces/{workspace.json()['id']}/process/drafts"
        created = client.post(base, json={"name": "Recycle loop"}).json()
        yield client, f"{base}/{created['draft_id']}", created, loop
    for stop in list(solve_jobs._active.values()):
        stop.set()
    for gate in loop.gates.values():
        gate.set()


def _wait(client: TestClient, url: str, done: Any, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(url).json()
        if done(body):
            return body
        time.sleep(0.02)
    raise AssertionError(f"timed out; last {body}")


def _start(client: TestClient, base: str, revision: str, **extra: Any) -> dict[str, Any]:
    response = client.post(f"{base}/solves", json={"revision": revision, **extra})
    assert response.status_code == 202, response.text
    return response.json()


def test_iterations_are_readable_while_the_next_evaluation_is_still_running(env: Any) -> None:
    client, base, state, loop = env
    loop.hold(3)
    started = _start(client, base, state["revision"])
    assert started["state"] == "running" and started["live"] is True and started["iterations_observable"]
    url = f"{base}/solves/{started['run_id']}"
    assert loop.entered[3].wait(20)
    live = client.get(f"{url}?after=0").json()
    # Iterations 1 and 2 are durable and served while iteration 3 is blocked inside its evaluation.
    assert loop.busy and live["live"] is True and live["state"] == "running"
    assert [event["iteration"] for event in live["events"]] == [1, 2]
    assert live["cursor"] == 2
    assert all(event["max_normalized_residual"] > 1 for event in live["events"])
    assert client.get(f"{url}?after=2").json()["events"] == []
    loop.gates[3].set()
    final = _wait(client, url, lambda body: body["state"] == "finished")
    assert final["live"] is False and final["status"] == "completed" and final["reason"] == "converged"
    events = client.get(f"{url}?after=0").json()["events"]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    times = [event["emitted_at"] for event in events]
    assert times == sorted(times)
    run = client.get(f"{base}/runs/{started['run_id']}").json()
    assert times[-1] <= run["finished_at"]
    # The background run is an ordinary run: the existing list and results views read it.
    assert [event["iteration"] for event in events] == [row["iteration"] for row in run["mixed_solve"]["history"]]
    assert started["run_id"] in [row["run_id"] for row in client.get(f"{base}/runs").json()]
    assert client.get(f"{base}/runs/{started['run_id']}/results").status_code == 200


def test_operator_stop_takes_effect_at_the_next_boundary_without_interrupting_an_evaluation(env: Any) -> None:
    client, base, state, loop = env
    loop.hold(2)
    started = _start(client, base, state["revision"])
    url = f"{base}/solves/{started['run_id']}"
    assert loop.entered[2].wait(20)
    stopped = client.post(f"{url}/stop").json()
    assert stopped["stop"] == "requested" and stopped["state"] == "stopping"
    assert loop.busy  # the running evaluation is not interrupted
    loop.gates[2].set()
    final = _wait(client, url, lambda body: body["state"] == "finished")
    assert final["status"] == "unconverged" and final["reason"] == "operator_stop"
    assert loop.calls == [1, 2]
    run = client.get(f"{base}/runs/{started['run_id']}").json()
    assert run["mixed_solve"]["results_label"] == "Not converged — last iterate"
    assert run["outcome"]["state"] == "non_converged" and run["streams"]
    again = client.post(f"{url}/stop").json()
    assert again["stop"] == "noop" and again["state"] == "finished"


def test_one_active_solve_per_draft_and_a_restart_marks_running_solves_interrupted(env: Any) -> None:
    client, base, state, loop = env
    loop.hold(1)
    started = _start(client, base, state["revision"])
    refused = client.post(f"{base}/solves", json={"revision": state["revision"]})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "solve_active"
    # Simulate a backend restart: the record still says running but this process is not running it.
    stop = solve_jobs._active.pop(started["run_id"])
    assert solve_jobs.recover_interrupted() >= 1
    persisted = solve_jobs._read_solve(solve_jobs._run_dir(
        state["workspace_id"], state["draft_id"], started["run_id"]))
    assert persisted["state"] == "interrupted" and not (solve_jobs._run_dir(
        state["workspace_id"], state["draft_id"], started["run_id"]) / "run.json").exists()
    body = client.get(f"{base}/solves/{started['run_id']}").json()
    assert body["state"] == "interrupted" and body["live"] is False and "no result" in body["message"]
    assert client.post(f"{base}/solves/{started['run_id']}/stop").json()["stop"] == "noop"
    stop.set()
    loop.gates[1].set()


def test_retry_links_parent_and_settings_difference_and_comparison_reports_both_runs(env: Any) -> None:
    client, base, state, loop = env
    loop.gain = 0.95  # slow contraction: the default direct substitution does not converge in 3 iterations
    first_patch = client.post(f"{base}/patch", json={"expected_revision": state["revision"], "ops": [
        {"op": "set_solver", "solver": {"max_iterations": 3}}]}).json()
    first = _start(client, base, first_patch["revision"])
    _wait(client, f"{base}/solves/{first['run_id']}", lambda body: body["state"] == "finished")
    second_patch = client.post(f"{base}/patch", json={"expected_revision": first_patch["revision"], "ops": [
        {"op": "set_solver", "solver": {"method": "wegstein", "max_iterations": 40}}]}).json()
    retry = _start(client, base, second_patch["revision"], retry_of=first["run_id"])
    assert retry["retry_of"] == first["run_id"]
    diff = {row["key"]: (row["a"], row["b"]) for row in retry["retry_settings_diff"]}
    assert diff["method"] == ("direct_substitution", "wegstein") and diff["max_iterations"] == (3, 40)
    final = _wait(client, f"{base}/solves/{retry['run_id']}", lambda body: body["state"] == "finished")
    assert final["status"] == "completed"
    run = client.get(f"{base}/runs/{retry['run_id']}").json()
    assert run["retry_of"] == first["run_id"] and run["draft_revision"] == second_patch["revision"]

    comparison = client.get(f"{base}/compare", params={"a": first["run_id"], "b": retry["run_id"]}).json()
    same = client.get(f"{base}/compare", params={"a": first["run_id"], "b": first["run_id"]})
    assert same.status_code == 422 and same.json()["detail"]["code"] == "comparison_invalid"
    assert comparison["a"]["reason"] in {"max_iterations", "iteration_budget_insufficient"}
    assert 1 <= comparison["a"]["iterations"] <= 3
    assert comparison["a"]["results_label"] == "Not converged — last iterate"
    assert comparison["b"]["reason"] == "converged" and comparison["b"]["retry_of"] == first["run_id"]
    assert comparison["same_revision"] is False and comparison["process_content_differs"] is False
    assert comparison["a"]["document_sha256"] != comparison["b"]["document_sha256"]
    assert comparison["process_content_digests"][0] == comparison["process_content_digests"][1]
    assert {row["key"] for row in comparison["settings_diff"]} >= {"method", "max_iterations"}
    assert len(comparison["a"]["residuals"]) == comparison["a"]["iterations"]
    (tear,) = comparison["streams"]
    flow = next(field for field in tear["fields"] if field["field"] == "mass_flow")
    assert flow["unit"] == "kg/h" and flow["delta"] == pytest.approx(flow["b"] - flow["a"], rel=1e-5)
    temperature = next(field for field in tear["fields"] if field["field"] == "temperature")
    assert temperature["unit"] == "degC" and temperature["delta"] == pytest.approx(0.0)

    content = client.post(f"{base}/patch", json={"expected_revision": second_patch["revision"], "ops": [
        {"op": "add_stream", "id": "x", "tag": "Extra", "x": 0, "y": 0}]}).json()
    third = _start(client, base, content["revision"])
    _wait(client, f"{base}/solves/{third['run_id']}", lambda body: body["state"] == "finished")
    differs = client.get(f"{base}/compare", params={"a": retry["run_id"], "b": third["run_id"]}).json()
    assert differs["process_content_differs"] is True
    assert differs["process_content_digests"][0] != differs["process_content_digests"][1]


def test_runs_without_jarvis_iterations_say_so_and_the_synchronous_path_still_works(
        env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, base, state, _loop = env
    monkeypatch.setattr(mixed, "needs_mixed_solve", lambda _document: False)
    monkeypatch.setattr(draft_compiler, "materialize",
                        lambda *_args, **_kwargs: {"status": "completed", "streams": {}, "units": {}})
    monkeypatch.setattr(draft_compiler, "result_fingerprint", lambda *_args, **_kwargs: "f" * 64)
    monkeypatch.setattr(draft.culture_engine, "propagate", lambda *_args: ({}, []))
    started = _start(client, base, state["revision"])
    assert started["iterations_observable"] is False
    final = _wait(client, f"{base}/solves/{started['run_id']}", lambda body: body["state"] == "finished")
    assert final["events"] == []
    response = client.post(f"{base}/revisions/{state['revision']}/run")
    assert response.status_code == 200, response.text
    assert response.json()["run"]["status"] == "completed"


def test_settings_diff_and_event_log_bounds(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert solve_jobs.settings_diff({"a": 1, "t": {"x": 1}}, {"a": 1, "t": {"x": 2}, "m": "w"}) == [
        {"key": "m", "a": None, "b": "w"}, {"key": "t.x", "a": 1, "b": 2}]
    monkeypatch.setattr(solve_jobs, "MAX_EVENTS", 2)
    observer = solve_jobs._Observer(tmp_path, threading.Event())
    for iteration in range(1, 5):
        observer.iteration({"iteration": iteration})
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert [json.loads(line)["iteration"] for line in lines] == [1, 2] and observer.truncated


def test_finished_solve_is_not_overwritten_by_a_stale_running_read(tmp_path: Any) -> None:
    run_id = "a" * 32
    finished = {"run_id": run_id, "state": "finished", "status": "completed"}
    solve_jobs._write(tmp_path / "solve.json", finished)
    stale = {"run_id": run_id, "state": "running"}

    assert solve_jobs._settled(tmp_path, stale) == finished
    assert solve_jobs._read_solve(tmp_path) == finished


def test_iteration_observer_sees_method_events_before_the_next_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    step = tear_solver._step

    def step_with_event(*args: Any) -> dict[str, Any]:
        args[-1]["events"].append("method_step")
        return step(*args)

    monkeypatch.setattr(tear_solver, "_step", step_with_event)
    seen: list[dict[str, Any]] = []

    def evaluate(guess: dict[str, Any], _iteration: int) -> dict[str, Any]:
        if seen:
            assert "method_step" in seen[-1]["events"]
        result = copy.deepcopy(guess)
        result[qual.TAG]["mass_flow_kg_s"] = 0.5 * guess[qual.TAG]["mass_flow_kg_s"] + 0.5
        return result

    result = tear_solver.solve(qual.state({"mass_flow_kg_s": 2.0}), evaluate,
                               settings=tear_solver.Settings(max_iterations=3),
                               on_history=lambda row: seen.append(copy.deepcopy(row)))
    assert len(seen) == len(result["history"])
    assert seen == result["history"]
