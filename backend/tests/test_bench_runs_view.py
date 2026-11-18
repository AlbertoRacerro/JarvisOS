from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.database import initialize_database
from app.main import create_app
from app.modules.engineering.operator_service import workspace_dir
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace
from fastapi.testclient import TestClient

SECRET_DIR = "/home/someone/private/work"


def _workspace() -> str:
    initialize_database()
    return create_workspace(WorkspaceCreate(name=f"Bench View {uuid4().hex[:6]}", slug=f"bv-{uuid4().hex[:8]}")).id


def _check(check_id: str, status: str, **extra) -> dict:
    return {"id": check_id, "family": "geometry", "status": status, "actual": 1.5, "limit": 2.0, "unit": "mm",
            "hint": "one line", "source": "evaluator", **extra}


def _write(workspace_id: str, run_id: str, data: dict | str, *, mtime: float | None = None) -> Path:
    folder = workspace_dir(workspace_id).parent / "bench" / run_id
    folder.mkdir(parents=True)
    result = folder / "result.json"
    result.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    if mtime is not None:
        os.utime(result, (mtime, mtime))
    return folder


def _result(verdict: str, *, evaluator="bluecad.solid", status="succeeded", completed="2026-11-17T10:00:00Z", **extra) -> dict:
    return {"evaluator_id": evaluator, "status": status, "verdict": verdict, "completed_at": completed,
            "checks": [_check("volume_positive", "pass")],
            "outputs": [{"name": "volume", "value": {"value": 12.0, "unit": "mm3"}}], **extra}


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def _url(workspace_id: str, tail: str) -> str:
    return f"/workspaces/{workspace_id}/engineering/bench/{tail}"


def test_list_newest_first_with_four_verdicts_and_skipped_count(client: TestClient) -> None:
    ws = _workspace()
    _write(ws, "run_a", _result("usable", completed="2026-11-17T08:00:00Z"))
    _write(ws, "run_b", _result("usable_with_warnings", completed="2026-11-17T09:00:00Z"))
    _write(ws, "run_c", _result("unchecked", completed="2026-11-17T10:00:00Z", evaluator="bluecad.mesh"))
    _write(ws, "run_d", _result("not_usable", completed="2026-11-17T11:00:00Z", status="failed"))
    _write(ws, "run_e", _result("surprising", completed="2026-11-17T07:00:00Z"))
    _write(ws, "run_bad", "{not json")
    _write(ws, "run_list", "[]")
    (workspace_dir(ws).parent / "bench" / "run_empty").mkdir()

    body = client.get(_url(ws, "runs")).json()

    assert [r["run_id"] for r in body["runs"]] == ["run_d", "run_c", "run_b", "run_a", "run_e"]
    assert [r["verdict"] for r in body["runs"]] == ["not_usable", "unchecked", "usable_with_warnings", "usable", "unchecked"]
    assert body["runs"][0]["status"] == "failed" and body["runs"][1]["evaluator_id"] == "bluecad.mesh"
    assert body["skipped"] == 3


def test_list_without_bench_root_is_empty_and_creates_nothing(client: TestClient) -> None:
    ws = _workspace()
    assert client.get(_url(ws, "runs")).json() == {"runs": [], "skipped": 0}
    assert not (workspace_dir(ws).parent / "bench").exists()


def test_list_is_capped_at_one_hundred(client: TestClient) -> None:
    ws = _workspace()
    for index in range(105):
        _write(ws, f"run_{index:03d}", _result("usable", completed=f"2026-11-17T10:{index % 60:02d}:00Z"))
    assert len(client.get(_url(ws, "runs")).json()["runs"]) == 100


def test_list_falls_back_to_file_time(client: TestClient) -> None:
    ws = _workspace()
    _write(ws, "run_old", {"evaluator_id": "x", "status": "succeeded", "verdict": "usable"}, mtime=1_700_000_000)
    _write(ws, "run_new", {"evaluator_id": "x", "status": "succeeded", "verdict": "usable"}, mtime=1_800_000_000)
    assert [r["run_id"] for r in client.get(_url(ws, "runs")).json()["runs"]] == ["run_new", "run_old"]


def test_show_returns_checks_not_evaluated_outputs_and_failure_without_paths(client: TestClient) -> None:
    ws = _workspace()
    failed = _result(
        "not_usable", status="failed",
        failure={"code": "SOLVER", "message": f"could not read {SECRET_DIR}/model.step"},
        checks=[_check("a_ok", "pass"), _check("b_warn", "warn"), _check("c_fail", "fail"),
                _check("d_skip", "not_evaluated", actual=None, limit=None, unit=None)],
        output_artifacts=[{"source_id": f"{SECRET_DIR}/out.step"}],
    )
    failed["outputs"] = []
    _write(ws, "run_failed", failed)
    _write(ws, "run_ok", _result("usable"))

    body = client.get(_url(ws, "runs/run_failed")).json()
    assert body["verdict"] == "not_usable" and body["failure"]["code"] == "SOLVER"
    assert [c["id"] for c in body["checks"]] == ["c_fail", "b_warn", "a_ok", "d_skip"]
    assert next(c for c in body["checks"] if c["id"] == "d_skip")["status"] == "not_evaluated"
    assert SECRET_DIR not in json.dumps(body) and "/home" not in json.dumps(body)

    ok = client.get(_url(ws, "runs/run_ok")).json()
    assert ok["outputs"] == [{"name": "volume", "value": {"value": 12.0, "unit": "mm3"}}]
    assert ok["outputs_withheld"] is False


def test_show_withholds_outputs_of_a_not_usable_run(client: TestClient) -> None:
    ws = _workspace()
    _write(ws, "run_rej", _result("not_usable"))
    body = client.get(_url(ws, "runs/run_rej")).json()
    assert body["outputs"] == [] and body["outputs_withheld"] is True


def test_show_returns_named_ports_from_manifest(client: TestClient) -> None:
    ws = _workspace()
    folder = _write(ws, "run_ports", _result("usable", evaluator="bluecad.solid_code"))
    assert client.get(_url(ws, "runs/run_ports")).json()["ports"] == []

    artifacts = folder / "artifacts"
    artifacts.mkdir()
    (artifacts / "manifest.json").write_text(json.dumps({
        "resolved_ports": {
            "body": {
                "outlet": {
                    "interface": "pad",
                    "origin": [10, 0, 0],
                    "direction": [1, 0, 0],
                    "pad_d": 20,
                },
                "inlet": {
                    "interface": "tube",
                    "origin": [0, 0, 0],
                    "direction": [-1, 0, 0],
                    "outer_d": 12,
                    "wall_t": 1.5,
                },
                "malformed": {"interface": "tube", "origin": "not-a-list", "direction": [1, 0, 0]},
            }
        }
    }), encoding="utf-8")

    body = client.get(_url(ws, "runs/run_ports")).json()
    assert body["ports"] == [
        {
            "name": "inlet",
            "interface": "tube",
            "origin": [0, 0, 0],
            "direction": [-1, 0, 0],
            "outer_d": 12.0,
            "wall_t": 1.5,
        },
        {
            "name": "outlet",
            "interface": "pad",
            "origin": [10, 0, 0],
            "direction": [1, 0, 0],
            "pad_d": 20.0,
        },
    ]


def test_show_unknown_verdict_reads_as_unchecked(client: TestClient) -> None:
    ws = _workspace()
    _write(ws, "run_odd", _result("great"))
    assert client.get(_url(ws, "runs/run_odd")).json()["verdict"] == "unchecked"


def test_show_missing_and_unreadable_runs(client: TestClient) -> None:
    ws = _workspace()
    _write(ws, "run_bad", "{nope")
    assert client.get(_url(ws, "runs/run_missing")).status_code == 404
    assert client.get(_url(ws, "runs/run_bad")).status_code == 404
    assert client.get("/workspaces/missing-ws/engineering/bench/runs").status_code == 404


@pytest.mark.parametrize("run_id", ["..", "a%2Fb", "%2Fetc", "..%2F..", "a b", "x" * 200, "%2E%2E"])
def test_unsafe_run_ids_are_refused(client: TestClient, run_id: str) -> None:
    ws = _workspace()
    _write(ws, "run_ok", _result("usable"))
    assert client.get(_url(ws, f"runs/{run_id}")).status_code in {404, 422}
    assert client.get(_url(ws, "compare"), params={"run_a": run_id, "run_b": "run_ok"}).status_code in {404, 422}
    assert client.get(_url(ws, "compare"), params={"run_a": "run_ok", "run_b": run_id}).status_code in {404, 422}


def test_run_id_with_traversal_text_is_refused_by_pattern(client: TestClient) -> None:
    ws = _workspace()
    response = client.get(_url(ws, "compare"), params={"run_a": "../x", "run_b": "/etc"})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "run_id_invalid"
    # a bare "." is folded into the list URL by HTTP clients, so it is only reachable as a compare parameter
    assert client.get(_url(ws, "compare"), params={"run_a": ".", "run_b": ".."}).status_code == 422


def test_compare_returns_compare_document_without_paths(client: TestClient) -> None:
    ws = _workspace()
    _write(ws, "run_a", _result("usable", checks=[_check("c1", "pass")]))
    second = _result("surprising", checks=[_check("c1", "fail"), _check("c2", "not_evaluated", actual=None)])
    second["outputs"] = [{"name": "volume", "value": {"value": 15.0, "unit": "mm3"}}]
    _write(ws, "run_b", second)

    body = client.get(_url(ws, "compare"), params={"run_a": "run_a", "run_b": "run_b"}).json()

    assert body["run_id_a"] == "run_a" and body["verdict_a"] == "usable" and body["verdict_b"] == "unchecked"
    assert body["outputs"]["volume"]["mark"] == "match" and body["outputs"]["volume"]["absolute_difference"] == 3.0
    assert body["checks"]["c1"]["status_a"] == "pass" and body["checks"]["c1"]["status_b"] == "fail"
    assert body["checks"]["c2"]["status_a"] is None
    assert "result_path_a" not in body and "/" not in json.dumps({k: v for k, v in body.items() if k.startswith("result")})


def test_compare_refuses_a_run_of_another_workspace(client: TestClient) -> None:
    ws, other = _workspace(), _workspace()
    _write(ws, "run_a", _result("usable"))
    _write(other, "run_foreign", _result("usable"))
    response = client.get(_url(ws, "compare"), params={"run_a": "run_a", "run_b": "run_foreign"})
    assert response.status_code == 404 and response.json()["detail"]["code"] == "run_not_found"


def test_symlink_out_of_the_bench_root_is_refused(client: TestClient, tmp_path: Path) -> None:
    ws = _workspace()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.json").write_text(json.dumps(_result("usable")), encoding="utf-8")
    bench = workspace_dir(ws).parent / "bench"
    bench.mkdir(parents=True)
    (bench / "run_link").symlink_to(outside, target_is_directory=True)
    assert client.get(_url(ws, "runs/run_link")).status_code == 404
    assert client.get(_url(ws, "runs")).json() == {"runs": [], "skipped": 1}


def test_routes_write_nothing(client: TestClient) -> None:
    ws = _workspace()
    folder = _write(ws, "run_a", _result("usable"))
    before = sorted(p.name for p in folder.parent.rglob("*"))
    client.get(_url(ws, "runs"))
    client.get(_url(ws, "runs/run_a"))
    client.get(_url(ws, "compare"), params={"run_a": "run_a", "run_b": "run_a"})
    assert sorted(p.name for p in folder.parent.rglob("*")) == before
