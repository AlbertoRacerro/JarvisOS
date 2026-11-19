"""Acceptance tests for stored case comparison and verdict product object (spec 314, CASE-VERDICT-1).

Tests criteria 1, 2, and 3:
1. POST /workspaces/{id}/engineering/case-verdicts computes deviation and verdict ('pass'/'fail')
   deterministically from the workspace's usable bench run output via Pint, rejecting unknown runs (404),
   non-usable runs (422), and unit mismatches (422).
2. GET lists verdicts for that workspace only; PATCH accepts/updates rationale (200) and returns 404
   across workspaces.
3. readiness_board.py surfaces stored_verdict on the compare step when a matching verdict exists.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.main import create_app
from app.modules.engineering.readiness_board import compute_readiness_board

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(isolated_data_root: None) -> TestClient:
    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client


def _seed_workspace(slug: str = "ws-test") -> str:
    ws_id = str(uuid.uuid4())
    now = "2026-11-19T00:00:00Z"
    with open_sqlite_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO workspaces (id, name, slug, description, status, created_at, updated_at)
            VALUES (?, ?, ?, 'Test workspace', 'active', ?, ?)
            """,
            (ws_id, f"Workspace {slug}", slug, now, now),
        )
        conn.commit()
    return ws_id


def _create_bench_run(
    workspace_id: str,
    run_id: str,
    evaluator_id: str,
    verdict: str,
    outputs: list[dict] | None = None,
    completed_at: str = "2026-11-19T00:00:00Z",
) -> None:
    bench_dir = build_paths().workspaces_dir / workspace_id / "engineering" / "bench" / run_id
    bench_dir.mkdir(parents=True, exist_ok=True)
    res_file = bench_dir / "result.json"
    data = {
        "run_id": run_id,
        "evaluator_id": evaluator_id,
        "status": "complete",
        "verdict": verdict,
        "completed_at": completed_at,
    }
    if outputs is not None:
        data["outputs"] = outputs
    res_file.write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------------------------------------------------------
# Criterion 1: POST computes deviation and verdict deterministically; validation rules
# ------------------------------------------------------------------------------------------------

def test_post_unknown_workspace_returns_404(client: TestClient) -> None:
    res = client.post(
        "/workspaces/nonexistent-workspace/engineering/case-verdicts",
        json={
            "case_id": "case_1",
            "run_id": "run_1",
            "quantity": "density",
            "published_value": 1000.0,
            "published_unit": "kg/m^3",
            "rel_tol": 0.05,
        },
    )
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "workspace_not_found"


def test_post_validates_payload_fields(client: TestClient) -> None:
    ws_id = _seed_workspace("ws-valid")

    # Empty case_id
    res = client.post(
        f"/workspaces/{ws_id}/engineering/case-verdicts",
        json={
            "case_id": "   ",
            "run_id": "run_1",
            "quantity": "density",
            "published_value": 1000.0,
            "published_unit": "kg/m^3",
            "rel_tol": 0.05,
        },
    )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "case_id_invalid"

    # Non-finite / zero published_value
    res = client.post(
        f"/workspaces/{ws_id}/engineering/case-verdicts",
        json={
            "case_id": "case_1",
            "run_id": "run_1",
            "quantity": "density",
            "published_value": 0.0,
            "published_unit": "kg/m^3",
            "rel_tol": 0.05,
        },
    )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "published_value_invalid"

    # rel_tol out of range (0, 1.0]
    for bad_tol in [0.0, -0.05, 1.05]:
        res = client.post(
            f"/workspaces/{ws_id}/engineering/case-verdicts",
            json={
                "case_id": "case_1",
                "run_id": "run_1",
                "quantity": "density",
                "published_value": 1000.0,
                "published_unit": "kg/m^3",
                "rel_tol": bad_tol,
            },
        )
        assert res.status_code == 422
        assert res.json()["detail"]["code"] == "rel_tol_invalid"


def test_post_run_not_found_returns_404(client: TestClient) -> None:
    ws_id = _seed_workspace("ws-norun")
    res = client.post(
        f"/workspaces/{ws_id}/engineering/case-verdicts",
        json={
            "case_id": "case_1",
            "run_id": "missing_run",
            "quantity": "density",
            "published_value": 1000.0,
            "published_unit": "kg/m^3",
            "rel_tol": 0.05,
        },
    )
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "run_not_found"


def test_post_non_usable_run_returns_422(client: TestClient) -> None:
    ws_id = _seed_workspace("ws-notusable")
    _create_bench_run(
        ws_id,
        "run_bad",
        "eval.density",
        "not_usable",
        outputs=[{"name": "density", "value": {"value": 1000.0, "unit": "kg/m^3"}}],
    )
    res = client.post(
        f"/workspaces/{ws_id}/engineering/case-verdicts",
        json={
            "case_id": "case_1",
            "run_id": "run_bad",
            "quantity": "density",
            "published_value": 1000.0,
            "published_unit": "kg/m^3",
            "rel_tol": 0.05,
        },
    )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "run_not_usable"


def test_post_run_lacking_quantity_returns_422(client: TestClient) -> None:
    ws_id = _seed_workspace("ws-noqty")
    _create_bench_run(
        ws_id,
        "run_noqty",
        "eval.density",
        "usable",
        outputs=[{"name": "temperature", "value": {"value": 300.0, "unit": "K"}}],
    )
    res = client.post(
        f"/workspaces/{ws_id}/engineering/case-verdicts",
        json={
            "case_id": "case_1",
            "run_id": "run_noqty",
            "quantity": "density",
            "published_value": 1000.0,
            "published_unit": "kg/m^3",
            "rel_tol": 0.05,
        },
    )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "run_lacks_quantity"


def test_post_unit_mismatch_returns_422(client: TestClient) -> None:
    ws_id = _seed_workspace("ws-unitmismatch")
    _create_bench_run(
        ws_id,
        "run_mismatch",
        "eval.density",
        "usable",
        outputs=[{"name": "density", "value": {"value": 1000.0, "unit": "kg/m^3"}}],
    )
    # Target unit seconds is incompatible with density
    res = client.post(
        f"/workspaces/{ws_id}/engineering/case-verdicts",
        json={
            "case_id": "case_1",
            "run_id": "run_mismatch",
            "quantity": "density",
            "published_value": 1000.0,
            "published_unit": "s",
            "rel_tol": 0.05,
        },
    )
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "unit_mismatch"


def test_post_computes_pass_and_fail_verdicts_deterministically(client: TestClient) -> None:
    ws_id = _seed_workspace("ws-deterministic")

    # Run within tolerance: 1020 kg/m^3 vs published 1000 kg/m^3 (rel_tol = 0.05 -> dev = 0.02 <= 0.05 -> pass)
    _create_bench_run(
        ws_id,
        "run_pass",
        "eval.density",
        "usable",
        outputs=[{"name": "density", "value": {"value": 1020.0, "unit": "kg/m^3"}}],
    )
    res_pass = client.post(
        f"/workspaces/{ws_id}/engineering/case-verdicts",
        json={
            "case_id": "zephyr_case",
            "run_id": "run_pass",
            "quantity": "density",
            "published_value": 1000.0,
            "published_unit": "kg/m^3",
            "rel_tol": 0.05,
        },
    )
    assert res_pass.status_code == 201
    data_pass = res_pass.json()
    assert data_pass["verdict"] == "pass"
    assert data_pass["status"] == "recorded"
    assert data_pass["deviation"] == pytest.approx(0.02)
    assert data_pass["computed_value"] == 1020.0
    assert data_pass["computed_unit"] == "kg/m^3"
    assert data_pass["accepted_by"] is None
    assert data_pass["accepted_at"] is None

    # Run outside tolerance: 1.1 g/cm3 -> 1100 kg/m^3 vs published 1000 kg/m^3 (dev = 0.10 > 0.05 -> fail)
    _create_bench_run(
        ws_id,
        "run_fail",
        "eval.density",
        "usable_with_warnings",
        outputs=[{"name": "density", "value": {"value": 1.1, "unit": "g/cm3"}}],
    )
    res_fail = client.post(
        f"/workspaces/{ws_id}/engineering/case-verdicts",
        json={
            "case_id": "zephyr_case",
            "run_id": "run_fail",
            "quantity": "density",
            "published_value": 1000.0,
            "published_unit": "kg/m^3",
            "rel_tol": 0.05,
        },
    )
    assert res_fail.status_code == 201
    data_fail = res_fail.json()
    assert data_fail["verdict"] == "fail"
    assert data_fail["status"] == "recorded"
    assert data_fail["deviation"] == pytest.approx(0.10)
    assert data_fail["computed_value"] == 1.1
    assert data_fail["computed_unit"] == "g/cm3"


# ------------------------------------------------------------------------------------------------
# Criterion 2: Workspace isolation in GET and PATCH; PATCH updates rationale and status
# ------------------------------------------------------------------------------------------------

def test_get_and_patch_workspace_isolation(client: TestClient) -> None:
    ws_a = _seed_workspace("ws-iso-a")
    ws_b = _seed_workspace("ws-iso-b")

    _create_bench_run(
        ws_a,
        "run_a",
        "eval.density",
        "usable",
        outputs=[{"name": "density", "value": {"value": 1000.0, "unit": "kg/m^3"}}],
    )

    created_res = client.post(
        f"/workspaces/{ws_a}/engineering/case-verdicts",
        json={
            "case_id": "case_a",
            "run_id": "run_a",
            "quantity": "density",
            "published_value": 1000.0,
            "published_unit": "kg/m^3",
            "rel_tol": 0.05,
        },
    )
    assert created_res.status_code == 201
    verdict_id = created_res.json()["id"]

    # GET workspace A returns the verdict
    get_a = client.get(f"/workspaces/{ws_a}/engineering/case-verdicts")
    assert get_a.status_code == 200
    assert len(get_a.json()) == 1
    assert get_a.json()[0]["id"] == verdict_id

    # GET workspace B returns empty list
    get_b = client.get(f"/workspaces/{ws_b}/engineering/case-verdicts")
    assert get_b.status_code == 200
    assert len(get_b.json()) == 0

    # Cross-workspace PATCH (verdict exists in A, patch on B) returns 404
    patch_cross = client.patch(
        f"/workspaces/{ws_b}/engineering/case-verdicts/{verdict_id}",
        json={"status": "accepted", "rationale": "Cross-workspace attempt"},
    )
    assert patch_cross.status_code == 404

    # PATCH on correct workspace A succeeds (200)
    patch_ok = client.patch(
        f"/workspaces/{ws_a}/engineering/case-verdicts/{verdict_id}",
        json={"status": "accepted", "rationale": "Verifica approvata dall'operatore"},
    )
    assert patch_ok.status_code == 200
    patched_data = patch_ok.json()
    assert patched_data["status"] == "accepted"
    assert patched_data["rationale"] == "Verifica approvata dall'operatore"
    assert patched_data["accepted_by"] == "operator"
    assert patched_data["accepted_at"] is not None


# ------------------------------------------------------------------------------------------------
# Criterion 3: readiness_board.py surfaces stored_verdict on compare step
# ------------------------------------------------------------------------------------------------

def test_readiness_board_surfaces_stored_verdict(client: TestClient) -> None:
    # zephyr pack has case "zephyr" published density = 1000 kg/m^3, rel_tol = 0.05
    ws_id = _seed_workspace("zephyr")

    _create_bench_run(
        ws_id,
        "run_zephyr_1",
        "evaluator.density",
        "usable",
        outputs=[{"name": "density", "value": {"value": 1005.0, "unit": "kg/m^3"}}],
        completed_at="2026-11-19T01:00:00Z",
    )

    # Before creating a verdict, readiness board compare step has stored_verdict None
    board_before = compute_readiness_board()
    zephyr_case = next(c for c in board_before.cases if c.workspace == "zephyr")
    assert zephyr_case.steps["compare"].comparison is not None
    assert zephyr_case.steps["compare"].stored_verdict is None

    # Record a case verdict
    post_res = client.post(
        f"/workspaces/{ws_id}/engineering/case-verdicts",
        json={
            "case_id": zephyr_case.id,
            "run_id": "run_zephyr_1",
            "quantity": "density",
            "published_value": 1000.0,
            "published_unit": "kg/m^3",
            "rel_tol": 0.05,
            "rationale": "Registrazione automatica test",
        },
    )
    assert post_res.status_code == 201
    verdict_id = post_res.json()["id"]

    # Now readiness board has stored_verdict attached!
    board_after = compute_readiness_board()
    zephyr_after = next(c for c in board_after.cases if c.workspace == "zephyr")
    stored = zephyr_after.steps["compare"].stored_verdict
    assert stored is not None
    assert stored.id == verdict_id
    assert stored.verdict == "pass"
    assert stored.status == "recorded"
    assert stored.run_id == "run_zephyr_1"
    assert stored.rationale == "Registrazione automatica test"

    # API route /readiness returns stored_verdict in payload
    api_res = client.get("/readiness")
    assert api_res.status_code == 200
    route_zephyr = next(c for c in api_res.json()["cases"] if c["workspace"] == "zephyr")
    assert route_zephyr["steps"]["compare"]["stored_verdict"]["id"] == verdict_id

    # If the verdict is updated to superseded, it is no longer surfaced
    patch_res = client.patch(
        f"/workspaces/{ws_id}/engineering/case-verdicts/{verdict_id}",
        json={"status": "superseded"},
    )
    assert patch_res.status_code == 200

    board_superseded = compute_readiness_board()
    zephyr_sup = next(c for c in board_superseded.cases if c.workspace == "zephyr")
    assert zephyr_sup.steps["compare"].stored_verdict is None
