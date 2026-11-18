"""Acceptance tests for readiness board comparison step (spec 305, READINESS-COMPARE-1).

Tests criteria 1 through 5 and 7 offline against the fake pack and isolated data root:
1. Usable run within tolerance turns compare green; outside tolerance turns red out_of_tolerance.
2. Convertible units (e.g. g/cm3 vs kg/m^3) are converted before deviation calculation.
3. Incompatible units give unit_mismatch; missing quantity gives missing_record; not_usable/unchecked ignored.
4. The newest qualifying run is used when several qualify.
5. Case without compare gives not_declared; zero published value or rel_tol outside (0, 1] gives board error + not_declared.
7. Board route and command change nothing in the data root; command prints deviation.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.main import create_app
from app.modules.engineering.readiness_board import (
    REASON_MISSING_RECORD,
    REASON_NOT_DECLARED,
    REASON_OUT_OF_TOLERANCE,
    REASON_UNIT_MISMATCH,
    compute_readiness_board,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(isolated_data_root: None) -> TestClient:
    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client


def _seed_workspace(slug: str = "zephyr") -> str:
    ws_id = str(uuid.uuid4())
    now = "2026-11-18T00:00:00Z"
    with open_sqlite_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO workspaces (id, name, slug, description, status, created_at, updated_at)
            VALUES (?, ?, ?, 'Zephyr testing workspace', 'active', ?, ?)
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
    completed_at: str = "2026-11-18T00:00:00Z",
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
# Criterion 1: Usable run within tolerance turns green; outside tolerance turns red out_of_tolerance
# ------------------------------------------------------------------------------------------------

def test_criterion_1_tolerance_threshold(client: TestClient) -> None:
    # zephyr pack case: published density = 1000.0 kg/m^3, rel_tol = 0.05 (5%)
    ws_id = _seed_workspace("zephyr")

    # Run within tolerance: 1020 kg/m^3 -> deviation = 0.02 <= 0.05
    _create_bench_run(
        ws_id,
        "run_green",
        "evaluator.density",
        "usable",
        outputs=[{"name": "density", "value": {"value": 1020.0, "unit": "kg/m^3"}}],
        completed_at="2026-11-18T01:00:00Z",
    )

    board = compute_readiness_board()
    case = board.cases[0]
    cmp_step = case.steps["compare"]
    assert cmp_step.status == "green"
    assert cmp_step.reason is None
    assert cmp_step.comparison is not None
    assert cmp_step.comparison.run_id == "run_green"
    assert cmp_step.comparison.computed.value == 1020.0
    assert cmp_step.comparison.computed.unit == "kg/m^3"
    assert cmp_step.comparison.published.value == 1000.0
    assert cmp_step.comparison.published.unit == "kg/m^3"
    assert cmp_step.comparison.computed_in_published_unit == pytest.approx(1020.0)
    assert cmp_step.comparison.deviation == pytest.approx(0.02)
    assert cmp_step.comparison.rel_tol == 0.05

    # API route returns comparison object
    res = client.get("/readiness")
    assert res.status_code == 200
    route_cmp = res.json()["cases"][0]["steps"]["compare"]["comparison"]
    assert route_cmp is not None
    assert route_cmp["run_id"] == "run_green"
    assert route_cmp["deviation"] == pytest.approx(0.02)

    # Newer run outside tolerance: 1060 kg/m^3 -> deviation = 0.06 > 0.05
    _create_bench_run(
        ws_id,
        "run_out_of_tol",
        "evaluator.density",
        "usable",
        outputs=[{"name": "density", "value": {"value": 1060.0, "unit": "kg/m^3"}}],
        completed_at="2026-11-18T02:00:00Z",
    )

    board2 = compute_readiness_board()
    cmp_step2 = board2.cases[0].steps["compare"]
    assert cmp_step2.status == "red"
    assert cmp_step2.reason == REASON_OUT_OF_TOLERANCE
    assert cmp_step2.comparison is not None
    assert cmp_step2.comparison.run_id == "run_out_of_tol"
    assert cmp_step2.comparison.deviation == pytest.approx(0.06)


# ------------------------------------------------------------------------------------------------
# Criterion 2: Run output in a convertible unit (g/cm3 vs kg/m^3)
# ------------------------------------------------------------------------------------------------

def test_criterion_2_convertible_unit(client: TestClient) -> None:
    ws_id = _seed_workspace("zephyr")

    # 1.0 g/cm3 == 1000 kg/m^3 -> deviation is approximately 0
    _create_bench_run(
        ws_id,
        "run_convert",
        "evaluator.density",
        "usable",
        outputs=[{"name": "density", "value": {"value": 1.0, "unit": "g/cm3"}}],
    )

    board = compute_readiness_board()
    cmp_step = board.cases[0].steps["compare"]
    assert cmp_step.status == "green"
    assert cmp_step.reason is None
    assert cmp_step.comparison is not None
    assert cmp_step.comparison.computed.value == 1.0
    assert cmp_step.comparison.computed.unit == "g/cm3"
    assert cmp_step.comparison.published.value == 1000.0
    assert cmp_step.comparison.published.unit == "kg/m^3"
    assert cmp_step.comparison.computed_in_published_unit == pytest.approx(1000.0)
    assert cmp_step.comparison.deviation == pytest.approx(0.0, abs=1e-6)


# ------------------------------------------------------------------------------------------------
# Criterion 3: Incompatible unit, missing quantity, not_usable/unchecked runs ignored
# ------------------------------------------------------------------------------------------------

def test_criterion_3_incompatible_unit_missing_quantity_and_verdicts(client: TestClient, tmp_path: Path) -> None:
    ws_id = _seed_workspace("zephyr")

    # Incompatible unit: meter vs kg/m^3 -> unit_mismatch
    _create_bench_run(
        ws_id,
        "run_incompatible",
        "evaluator.density",
        "usable",
        outputs=[{"name": "density", "value": {"value": 10.0, "unit": "m"}}],
        completed_at="2026-11-18T01:00:00Z",
    )

    board = compute_readiness_board()
    cmp_step = board.cases[0].steps["compare"]
    assert cmp_step.status == "red"
    assert cmp_step.reason == REASON_UNIT_MISMATCH
    assert cmp_step.comparison is not None
    assert cmp_step.comparison.computed.value == 10.0
    assert cmp_step.comparison.computed.unit == "m"
    assert cmp_step.comparison.published.value == 1000.0
    assert cmp_step.comparison.published.unit == "kg/m^3"
    assert cmp_step.comparison.computed_in_published_unit is None
    assert cmp_step.comparison.deviation is None

    # Missing quantity: run has output 'viscosity', not 'density'
    ws_empty = _seed_workspace("zephyr_other")
    _create_bench_run(
        ws_empty,
        "run_visc_only",
        "evaluator.viscosity",
        "usable",
        outputs=[{"name": "viscosity", "value": {"value": 0.001, "unit": "Pa*s"}}],
    )
    cases_toml = tmp_path / "zephyr_other.toml"
    cases_toml.write_text(
        """
[[cases]]
id = "zephyr-other-case"
title = "Zephyr Other Case"
workspace = "zephyr_other"
source_title = "Study"
[cases.compare]
quantity = "density"
value = 1000.0
unit = "kg/m^3"
rel_tol = 0.05
""",
        encoding="utf-8",
    )
    b_empty = compute_readiness_board(case_files=(cases_toml,))
    assert b_empty.cases[0].steps["compare"].status == "red"
    assert b_empty.cases[0].steps["compare"].reason == REASON_MISSING_RECORD
    assert b_empty.cases[0].steps["compare"].missing == ["density"]

    # verdict not_usable and unchecked: should be ignored even if output has density
    _create_bench_run(
        ws_id,
        "run_not_usable",
        "evaluator.density",
        "not_usable",
        outputs=[{"name": "density", "value": {"value": 1000.0, "unit": "kg/m^3"}}],
        completed_at="2026-11-18T03:00:00Z",
    )
    _create_bench_run(
        ws_id,
        "run_unchecked",
        "evaluator.density",
        "unchecked",
        outputs=[{"name": "density", "value": {"value": 1000.0, "unit": "kg/m^3"}}],
        completed_at="2026-11-18T04:00:00Z",
    )
    # Still picks run_incompatible (newest usable)
    board3 = compute_readiness_board()
    assert board3.cases[0].steps["compare"].comparison.run_id == "run_incompatible"

    # Usable_with_warnings qualifies
    _create_bench_run(
        ws_id,
        "run_usable_with_warnings",
        "evaluator.density",
        "usable_with_warnings",
        outputs=[{"name": "density", "value": {"value": 1000.0, "unit": "kg/m^3"}}],
        completed_at="2026-11-18T05:00:00Z",
    )
    board4 = compute_readiness_board()
    assert board4.cases[0].steps["compare"].status == "green"
    assert board4.cases[0].steps["compare"].comparison.run_id == "run_usable_with_warnings"


# ------------------------------------------------------------------------------------------------
# Criterion 4: Newest qualifying run chosen
# ------------------------------------------------------------------------------------------------

def test_criterion_4_newest_qualifying_run(client: TestClient) -> None:
    ws_id = _seed_workspace("zephyr")

    _create_bench_run(
        ws_id,
        "run_old",
        "evaluator.density",
        "usable",
        outputs=[{"name": "density", "value": {"value": 1000.0, "unit": "kg/m^3"}}],
        completed_at="2026-11-18T01:00:00Z",
    )
    _create_bench_run(
        ws_id,
        "run_mid",
        "evaluator.density",
        "usable",
        outputs=[{"name": "density", "value": {"value": 1200.0, "unit": "kg/m^3"}}],
        completed_at="2026-11-18T02:00:00Z",
    )
    _create_bench_run(
        ws_id,
        "run_newest_unusable",
        "evaluator.density",
        "not_usable",
        outputs=[{"name": "density", "value": {"value": 1000.0, "unit": "kg/m^3"}}],
        completed_at="2026-11-18T03:00:00Z",
    )

    board = compute_readiness_board()
    # Newest qualifying is run_mid (completed_at 02:00:00Z, usable)
    cmp_step = board.cases[0].steps["compare"]
    assert cmp_step.status == "red"
    assert cmp_step.reason == REASON_OUT_OF_TOLERANCE
    assert cmp_step.comparison.run_id == "run_mid"

    # Add a newer usable run
    _create_bench_run(
        ws_id,
        "run_newest_usable",
        "evaluator.density",
        "usable",
        outputs=[{"name": "density", "value": {"value": 1005.0, "unit": "kg/m^3"}}],
        completed_at="2026-11-18T04:00:00Z",
    )
    board2 = compute_readiness_board()
    cmp_step2 = board2.cases[0].steps["compare"]
    assert cmp_step2.status == "green"
    assert cmp_step2.comparison.run_id == "run_newest_usable"


# ------------------------------------------------------------------------------------------------
# Criterion 5: Case without compare gives not_declared; invalid compare values give board error
# ------------------------------------------------------------------------------------------------

def test_criterion_5_case_validation_and_not_declared(client: TestClient, tmp_path: Path) -> None:
    cases_toml = tmp_path / "custom_cases.toml"
    cases_toml.write_text(
        """
[[cases]]
id = "no-compare-case"
title = "Case without Compare"
workspace = "zephyr"
source_title = "Study A"

[[cases]]
id = "zero-val-case"
title = "Case with Zero Value"
workspace = "zephyr"
source_title = "Study B"
[cases.compare]
quantity = "density"
value = 0.0
unit = "kg/m^3"
rel_tol = 0.05

[[cases]]
id = "bad-rel-tol-case"
title = "Case with Bad Rel Tol"
workspace = "zephyr"
source_title = "Study C"
[cases.compare]
quantity = "density"
value = 1000.0
unit = "kg/m^3"
rel_tol = 1.5

[[cases]]
id = "zero-rel-tol-case"
title = "Case with Zero Rel Tol"
workspace = "zephyr"
source_title = "Study D"
[cases.compare]
quantity = "density"
value = 1000.0
unit = "kg/m^3"
rel_tol = 0.0
""",
        encoding="utf-8",
    )

    _seed_workspace("zephyr")
    board = compute_readiness_board(case_files=(cases_toml,))

    # All 4 cases are preserved in board.cases
    assert len(board.cases) == 4
    case_map = {c.id: c for c in board.cases}

    # Case without compare -> not_declared, no error for it
    c_none = case_map["no-compare-case"]
    assert c_none.steps["compare"].status == "red"
    assert c_none.steps["compare"].reason == REASON_NOT_DECLARED
    assert c_none.steps["compare"].missing == []

    # Case with zero val -> not_declared + board error
    c_zero = case_map["zero-val-case"]
    assert c_zero.steps["compare"].status == "red"
    assert c_zero.steps["compare"].reason == REASON_NOT_DECLARED

    # Case with rel_tol 1.5 -> not_declared + board error
    c_tol15 = case_map["bad-rel-tol-case"]
    assert c_tol15.steps["compare"].status == "red"
    assert c_tol15.steps["compare"].reason == REASON_NOT_DECLARED

    # Case with rel_tol 0.0 -> not_declared + board error
    c_tol0 = case_map["zero-rel-tol-case"]
    assert c_tol0.steps["compare"].status == "red"
    assert c_tol0.steps["compare"].reason == REASON_NOT_DECLARED

    # Verify board errors
    error_cids = {err.case_id for err in board.errors if err.case_id}
    assert "zero-val-case" in error_cids
    assert "bad-rel-tol-case" in error_cids
    assert "zero-rel-tol-case" in error_cids


# ------------------------------------------------------------------------------------------------
# Criterion 7: Board route and command change nothing in the data root; command outputs deviation
# ------------------------------------------------------------------------------------------------

def _digest_data_root(data_root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    if not data_root.exists():
        return digests
    for p in sorted(data_root.rglob("*")):
        if p.is_file() and not p.name.endswith(("-wal", "-shm")):
            rel = str(p.relative_to(data_root))
            digests[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return digests


def _table_row_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    with open_sqlite_connection() as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for t in tables:
            tname = t["name"]
            counts[tname] = conn.execute(f"SELECT COUNT(*) FROM {tname}").fetchone()[0]
    return counts


def test_criterion_7_data_root_immutability_and_command_deviation(client: TestClient) -> None:
    ws_id = _seed_workspace("zephyr")
    _create_bench_run(
        ws_id,
        "run_good",
        "evaluator.density",
        "usable",
        outputs=[{"name": "density", "value": {"value": 1020.0, "unit": "kg/m^3"}}],
    )

    data_root = build_paths().data_root
    digests_before = _digest_data_root(data_root)
    counts_before = _table_row_counts()

    # Call GET /readiness
    res = client.get("/readiness")
    assert res.status_code == 200

    # Call scripts/readiness_board.py
    script = REPO_ROOT / "scripts" / "readiness_board.py"
    proc = subprocess.run([sys.executable, str(script)], env=os.environ, capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    # Must show deviation percentage in the compare cell
    assert "2.0%" in proc.stdout

    digests_after = _digest_data_root(data_root)
    counts_after = _table_row_counts()

    assert digests_after == digests_before, "Data root files changed during readiness read"
    assert counts_after == counts_before, "Database rows changed during readiness read"
