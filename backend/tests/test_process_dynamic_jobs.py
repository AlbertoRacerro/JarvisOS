from __future__ import annotations

import sys
import threading
import time
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.database import initialize_database
from app.main import app
from app.modules.process_stack import dynamic_jobs


class FakeDynamicError(ValueError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code, self.detail = code, detail or {}


class FakeEngine:
    class Snapshot:
        def __init__(self, payload: dict[str, Any], digest: str) -> None:
            self.payload, self.digest = payload, digest

    class EngineResult:
        def __init__(self, status: str = "succeeded", rows: int = 5, error: Any = None) -> None:
            self.status, self.error = status, error
            self.series = {"t_s": np.arange(rows, dtype=np.float64), "X": np.arange(rows, dtype=np.float64) + 1}
            self.manifest = {"fidelity": "T1"}

    DynamicError = FakeDynamicError

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.result = self.EngineResult()
        self.block = False
        self.fail_prepare = False

    def prepare(self, workspace_id: str, draft_id: str, scenario_id: str) -> Any:
        if self.fail_prepare:
            raise self.DynamicError("PROFILE_GAP", "gap", {"channel": "par"})
        return self.Snapshot({"content_digest": "content-1", "revision": "1:abc", "scenario": {},
                              "units": [], "profiles": []}, "snapshot-1")

    def run(self, snapshot: Any, *, cancelled: Any, progress: Any, sampler: Any = None) -> Any:
        self.entered.set()
        progress(0.4)
        if self.block:
            while not self.release.wait(0.005):
                if cancelled():
                    return self.EngineResult("cancelled", rows=2)
        return self.result

@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    initialize_database()
    engine = FakeEngine()
    monkeypatch.setitem(sys.modules, "app.modules.process_stack.dynamic_engine", engine)
    with TestClient(app) as client:
        workspace = client.post("/workspaces", json={"name": "dynamic", "slug": f"dyn-{time.time_ns()}"})
        workspace.raise_for_status()
        base = f"/workspaces/{workspace.json()['id']}/process/drafts"
        draft = client.post(base, json={"name": "dynamic draft"})
        draft.raise_for_status()
        yield client, engine, workspace.json()["id"], draft.json()["draft_id"], base
    dynamic_jobs._futures.clear()
    dynamic_jobs._cancel_events.clear()


def _start(api: Any, scenario: str = "scenario") -> tuple[Any, str]:
    client, _, wid, did, base = api
    response = client.post(f"{base}/{did}/dynamic/runs", json={"scenario_id": scenario})
    return response, response.json().get("job_id", "")


def _wait(client: TestClient, base: str, did: str, job_id: str, expected: str) -> dict[str, Any]:
    for _ in range(200):
        response = client.get(f"{base}/{did}/dynamic/runs/{job_id}")
        if response.status_code == 200 and response.json()["status"] == expected:
            return response.json()
        time.sleep(0.005)
    raise AssertionError(f"job did not reach {expected}")


def test_start_is_prompt_poll_progress_cancel_and_no_success_artifact(api: Any) -> None:
    client, engine, _, did, base = api
    engine.block = True
    started_at = time.monotonic()
    response, job_id = _start(api)
    assert response.status_code == 202
    assert time.monotonic() - started_at < 0.5
    assert engine.entered.wait(1)
    assert client.post(f"{base}/{did}/dynamic/runs/{job_id}/cancel").status_code == 200
    result = _wait(client, base, did, job_id, "cancelled")
    assert set(result["identity"]) == {
        "workspace_id", "draft_id", "draft_revision", "content_digest", "snapshot_digest",
        "scenario_id", "card_refs", "profile_refs",
    }
    assert len(str(result["identity"])) < 1000
    assert result["progress"] == pytest.approx(0.4)
    assert "artifacts" in result  # failed/cancelled diagnostics retain computed rows
    assert client.get(f"{base}/{did}/dynamic/runs/{job_id}/manifest").json()["artifact_label"] == "diagnostic_cancelled"


def test_late_cancel_and_partial_or_empty_failure_artifacts(api: Any) -> None:
    client, engine, _, did, base = api
    response, job_id = _start(api)
    assert response.status_code == 202
    _wait(client, base, did, job_id, "succeeded")
    late = client.post(f"{base}/{did}/dynamic/runs/{job_id}/cancel").json()
    assert late["status"] == "succeeded" and late["cancel_outcome"] == "too_late"

    engine.result = engine.EngineResult("failed", rows=2, error={"code": "BAD_STATE"})
    _, partial_id = _start(api, "partial")
    partial = _wait(client, base, did, partial_id, "failed")
    assert partial["artifacts"]["rows"] == 2
    manifest = client.get(f"{base}/{did}/dynamic/runs/{partial_id}/manifest").json()
    assert manifest["artifact_label"] == "diagnostic_failed"

    engine.result = engine.EngineResult("failed", rows=0, error={"code": "BAD_STATE"})
    _, empty_id = _start(api, "empty")
    empty = _wait(client, base, did, empty_id, "failed")
    assert "artifacts" not in empty


def test_series_filters_stride_cap_artifact_tamper_and_scope(api: Any) -> None:
    client, engine, wid, did, base = api
    engine.result = engine.EngineResult(rows=10001)
    _, job_id = _start(api)
    _wait(client, base, did, job_id, "succeeded")
    url = f"{base}/{did}/dynamic/runs/{job_id}/series"
    response = client.get(url, params={"channels": "X", "start_s": 100, "end_s": 9000, "max_points": 10000})
    assert response.status_code == 200
    body = response.json()
    assert set(body["channels"]) == {"t_s", "X"}
    assert body["total_points"] == 8901 and body["returned_points"] <= 5000
    assert body["channels"]["t_s"][0] == 100
    assert client.get(f"{base}/wrong-draft/dynamic/runs/{job_id}").status_code == 404
    assert client.get(f"/workspaces/other/process/drafts/{did}/dynamic/runs/{job_id}").status_code == 404
    from app.core.paths import build_paths

    path = build_paths().data_root / "process_dynamic" / wid / job_id / "series.npz"
    path.write_bytes(path.read_bytes() + b"tamper")
    tampered = client.get(url)
    assert tampered.status_code == 409
    assert tampered.json()["detail"]["code"] == "ARTIFACT_DIGEST_MISMATCH"


def test_prepare_error_active_limit_list_order_and_recovery(api: Any) -> None:
    client, engine, wid, did, base = api
    engine.fail_prepare = True
    failed = client.post(f"{base}/{did}/dynamic/runs", json={"scenario_id": "gap"})
    assert failed.status_code == 422
    assert failed.json()["detail"]["code"] == "PROFILE_GAP"
    engine.fail_prepare = False
    engine.block = True
    first, first_id = _start(api, "first")
    second, second_id = _start(api, "second")
    assert first.status_code == second.status_code == 202
    assert client.post(f"{base}/{did}/dynamic/runs", json={"scenario_id": "third"}).status_code == 409
    assert client.get(f"{base}/{did}/dynamic/runs").json()[0]["job_id"] == second_id
    from app.core.paths import build_paths

    root = build_paths().data_root / "process_dynamic" / wid
    queued = root / "interrupted-queued"
    queued.mkdir(parents=True)
    (queued / "job.json").write_text('{"job_id":"interrupted-queued","workspace_id":"' + wid +
                                      '","draft_id":"' + did + '","status":"queued"}')
    running = root / "interrupted-running"
    running.mkdir(parents=True)
    (running / "job.json").write_text('{"job_id":"interrupted-running","workspace_id":"' + wid +
                                       '","draft_id":"' + did + '","status":"running"}')
    engine.release.set()
    _wait(client, base, did, first_id, "succeeded")
    _wait(client, base, did, second_id, "succeeded")
    assert dynamic_jobs.recover_interrupted() == 2
    for orphan_id in ("interrupted-queued", "interrupted-running"):
        recovered = client.get(f"{base}/{did}/dynamic/runs/{orphan_id}")
        assert recovered.status_code == 200
        assert recovered.json()["error"]["code"] == "JOB_INTERRUPTED_BY_RESTART"
    for _ in range(100):
        if not dynamic_jobs._futures and not dynamic_jobs._cancel_events:
            break
        time.sleep(0.005)
    assert not dynamic_jobs._futures and not dynamic_jobs._cancel_events


def test_queued_cancel_never_transitions_to_running(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, wid, did, base = api

    class DeferredExecutor:
        pending: tuple[Any, tuple[Any, ...]] | None = None

        def submit(self, function: Any, *args: Any) -> Any:
            self.pending = (function, args)
            return object()

    executor = DeferredExecutor()
    monkeypatch.setattr(dynamic_jobs, "_executor", executor)
    response, job_id = _start(api)
    assert response.status_code == 202
    cancelled = client.post(f"{base}/{did}/dynamic/runs/{job_id}/cancel").json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["error"]["code"] == "CANCELLED"
    assert executor.pending is not None
    executor.pending[0](*executor.pending[1])
    assert client.get(f"{base}/{did}/dynamic/runs/{job_id}").json()["status"] == "cancelled"


def test_global_active_job_cap_and_bounded_list(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, engine, wid, did, base = api
    from app.core.paths import build_paths

    monkeypatch.setattr(dynamic_jobs, "MAX_ACTIVE_GLOBAL", 1)
    other = build_paths().data_root / "process_dynamic" / "other-workspace" / "active-job"
    other.mkdir(parents=True)
    (other / "job.json").write_text(
        '{"job_id":"active-job","workspace_id":"other-workspace","draft_id":"d",'
        '"status":"running","created_at":"2026-10-06T00:00:00Z"}', encoding="utf-8")
    with pytest.raises(dynamic_jobs.DynamicJobError) as raised:
        dynamic_jobs.start(wid, did, "global-cap")
    assert raised.value.code == "GLOBAL_ACTIVE_JOB_LIMIT"
    other.joinpath("job.json").unlink()
    other.rmdir()
    other.parent.rmdir()
    monkeypatch.setattr(dynamic_jobs, "MAX_ACTIVE_GLOBAL", 8)
    engine.block = False
    for index in range(3):
        response, job_id = _start(api, f"list-{index}")
        assert response.status_code == 202
        _wait(client, base, did, job_id, "succeeded")
    limited = client.get(f"{base}/{did}/dynamic/runs?limit=1")
    assert limited.status_code == 200 and len(limited.json()) == 1
    assert len(dynamic_jobs.list_jobs(wid, did, limit=500)) <= 200
