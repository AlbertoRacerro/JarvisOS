"""Acceptance tests for readiness board (spec 303, READINESS-BOARD-1).

Tests criteria 1 through 7 offline against the fake pack and isolated data root:
1. Each cell turns green exactly when its condition holds and red otherwise.
2. An accepted datum that fails check_datum keeps facts red and names it.
3. A parameter bound to a raw datum or another source's datum keeps bound red.
4. compare is red with missing_record for cases without matching bench runs.
5. Invalid case entries appear as board errors; valid cases are still computed.
6. No pack or no readiness key: empty board, route 200, command exit 0 with plain message.
7. Route and command change nothing in data root (digests and row counts unchanged).
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
    REASON_WORKSPACE_MISSING,
    STEPS,
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


def _create_backing_artifact(workspace_id: str, content: str, filename: str = "paper.md") -> str:
    paths = build_paths()
    art_path = paths.artifacts_dir / workspace_id / filename
    art_path.parent.mkdir(parents=True, exist_ok=True)
    art_path.write_text(content, encoding="utf-8")
    art_id = str(uuid.uuid4())
    now = "2026-11-18T00:00:00Z"
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with open_sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO artifacts (id, workspace_id, filename, stored_path, artifact_type, mime_type, sha256, source_ref, status, created_at, notes)
            VALUES (?, ?, ?, ?, 'literature_source', 'text/markdown', ?, 'test', 'registered', ?, 'test artifact')
            """,
            (art_id, workspace_id, filename, str(art_path), sha, now),
        )
        conn.commit()
    return art_id


def _create_source(workspace_id: str, title: str, artifact_id: str) -> str:
    source_id = str(uuid.uuid4())
    now = "2026-11-18T00:00:00Z"
    with open_sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO literature_sources (id, workspace_id, title, source_kind, state, artifact_id, created_at, updated_at)
            VALUES (?, ?, ?, 'paper', 'accepted', ?, ?, ?)
            """,
            (source_id, workspace_id, title, artifact_id, now, now),
        )
        conn.commit()
    return source_id


def _create_datum(
    workspace_id: str,
    source_id: str,
    quantity: str,
    value_number: float,
    unit: str,
    locator_start: int,
    locator_end: int,
    status: str = "accepted",
) -> str:
    entry_id = str(uuid.uuid4())
    now = "2026-11-18T00:00:00Z"
    with open_sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO literature_entries (
                id, workspace_id, source_id, entry_kind, quantity, value_number, unit,
                status, locator_kind, locator_start, locator_end, created_at, updated_at
            )
            VALUES (?, ?, ?, 'datum', ?, ?, ?, ?, 'line', ?, ?, ?, ?)
            """,
            (entry_id, workspace_id, source_id, quantity, value_number, unit, status, locator_start, locator_end, now, now),
        )
        conn.commit()
    return entry_id


def _create_parameter(workspace_id: str, name: str, source_ref: str | None = None) -> str:
    param_id = str(uuid.uuid4())
    now = "2026-11-18T00:00:00Z"
    with open_sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO parameters (id, workspace_id, name, source_ref, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (param_id, workspace_id, name, source_ref, now, now),
        )
        conn.commit()
    return param_id


def _create_bench_run(workspace_id: str, run_id: str, evaluator_id: str, verdict: str) -> None:
    bench_dir = build_paths().workspaces_dir / workspace_id / "engineering" / "bench" / run_id
    bench_dir.mkdir(parents=True, exist_ok=True)
    res_file = bench_dir / "result.json"
    data = {
        "run_id": run_id,
        "evaluator_id": evaluator_id,
        "status": "complete",
        "verdict": verdict,
        "completed_at": "2026-11-18T00:00:00Z",
    }
    res_file.write_text(json.dumps(data), encoding="utf-8")


def _create_decision(workspace_id: str, status: str = "accepted", linked_run_id: str | None = "run-1") -> str:
    dec_id = str(uuid.uuid4())
    now = "2026-11-18T00:00:00Z"
    with open_sqlite_connection() as conn:
        if linked_run_id:
            conn.execute(
                """
                INSERT OR REPLACE INTO simulation_runs (id, workspace_id, status, created_at)
                VALUES (?, ?, 'completed', ?)
                """,
                (linked_run_id, workspace_id, now),
            )
        conn.execute(
            """
            INSERT INTO decisions (id, workspace_id, title, decision_text, status, linked_run_id, created_at, updated_at)
            VALUES (?, ?, 'Design decision', 'Adopted run outcome', ?, ?, ?, ?)
            """,
            (dec_id, workspace_id, status, linked_run_id, now, now),
        )
        conn.commit()
    return dec_id


# ------------------------------------------------------------------------------------------------
# Criterion 1: Seven cells turn green exactly when conditions hold, red otherwise
# ------------------------------------------------------------------------------------------------

def test_criterion_1_seven_steps_transition_and_missing_names(client: TestClient) -> None:
    # 0. Missing workspace makes every cell red with workspace_missing
    board = compute_readiness_board()
    assert len(board.cases) == 1
    c0 = board.cases[0]
    assert c0.workspace_id is None
    res0 = client.get("/readiness")
    assert res0.status_code == 200
    assert res0.json()["cases"][0]["workspace_id"] is None
    for step_name in STEPS:
        assert c0.steps[step_name].status == "red"
        assert c0.steps[step_name].reason == REASON_WORKSPACE_MISSING
        assert c0.steps[step_name].missing == ["zephyr"]

    # 1. Seed workspace: now cells are red for their specific missing records
    ws_id = _seed_workspace("zephyr")
    board = compute_readiness_board()
    c1 = board.cases[0]
    assert c1.workspace_id == ws_id
    res1 = client.get("/readiness")
    assert res1.status_code == 200
    assert res1.json()["cases"][0]["workspace_id"] == ws_id
    assert c1.steps["paper"].status == "red"
    assert c1.steps["paper"].reason == REASON_MISSING_RECORD
    assert c1.steps["paper"].missing == ["Benchmark Validation Study"]
    assert c1.steps["facts"].status == "red"
    assert c1.steps["facts"].missing == ["density", "thermal_conductivity"]
    assert c1.steps["bound"].status == "red"
    assert c1.steps["bound"].missing == ["material_density", "inlet_conductivity"]
    assert c1.steps["run"].status == "red"
    assert c1.steps["run"].missing == ["bench_run"]
    assert c1.steps["compare"].status == "red"
    assert c1.steps["compare"].reason == REASON_MISSING_RECORD
    assert c1.steps["compare"].missing == ["density"]
    assert c1.steps["decide"].status == "red"
    assert c1.steps["decide"].missing == ["decision"]
    assert c1.steps["answer"].status == "red"
    assert c1.steps["answer"].missing == ["retrieval.eval run"]

    # 2. Add paper with backing text: 'paper' turns green
    backing_content = "Line 1: Header\nLine 2: density is 1000 kg/m^3\nLine 3: conductivity is 0.6 W/(m·K)\n"
    art_id = _create_backing_artifact(ws_id, backing_content)
    source_id = _create_source(ws_id, "Benchmark Validation Study", art_id)
    board = compute_readiness_board()
    assert board.cases[0].steps["paper"].status == "green"
    assert board.cases[0].steps["facts"].status == "red"

    # 3. Add accepted datums at correct locators: 'facts' turns green
    d1_id = _create_datum(ws_id, source_id, "density", 1000.0, "kg/m^3", 2, 2, status="accepted")
    d2_id = _create_datum(ws_id, source_id, "thermal_conductivity", 0.6, "W/(m·K)", 3, 3, status="accepted")
    board = compute_readiness_board()
    assert board.cases[0].steps["facts"].status == "green"
    assert board.cases[0].steps["bound"].status == "red"

    # 4. Bind parameters to accepted datums of the source: 'bound' turns green
    _create_parameter(ws_id, "material_density", f"literature_entry:{d1_id}")
    _create_parameter(ws_id, "inlet_conductivity", f"literature_entry:{d2_id}")
    board = compute_readiness_board()
    assert board.cases[0].steps["bound"].status == "green"
    assert board.cases[0].steps["run"].status == "red"

    # 5. Add a bench run with usable verdict: 'run' turns green
    _create_bench_run(ws_id, "run_001", "evaluator.thermal", "usable")
    board = compute_readiness_board()
    assert board.cases[0].steps["run"].status == "green"
    assert board.cases[0].steps["compare"].status == "red"
    assert board.cases[0].steps["decide"].status == "red"

    # 6. 'compare' stays red with missing_record (density output not present in run_001)
    assert board.cases[0].steps["compare"].status == "red"
    assert board.cases[0].steps["compare"].reason == REASON_MISSING_RECORD
    assert board.cases[0].steps["compare"].missing == ["density"]

    # 7. Add an accepted decision linked to a run: 'decide' turns green
    _create_decision(ws_id, status="accepted", linked_run_id="run_001")
    board = compute_readiness_board()
    assert board.cases[0].steps["decide"].status == "green"
    assert board.cases[0].steps["answer"].status == "red"

    # 8. Add a retrieval.eval bench run with usable verdict: 'answer' turns green
    _create_bench_run(ws_id, "run_retrieval", "retrieval.eval", "usable")
    board = compute_readiness_board()
    assert board.cases[0].steps["answer"].status == "green"

    # Verify summary: 6/7 green, first red is compare
    summary = board.summary
    assert summary.total_cases == 1
    assert summary.total_cells == 7
    assert summary.green_cells == 6
    assert summary.red_cells == 1
    assert summary.first_red == "generic-literature-1 compare"


# ------------------------------------------------------------------------------------------------
# Criterion 2: Datum that is accepted but fails check_datum keeps facts red
# ------------------------------------------------------------------------------------------------

def test_criterion_2_failing_check_datum_keeps_facts_red(client: TestClient) -> None:
    ws_id = _seed_workspace("zephyr")
    backing_content = "Line 1: Header\nLine 2: density is 500 kg/m^3\n"
    art_id = _create_backing_artifact(ws_id, backing_content)
    source_id = _create_source(ws_id, "Benchmark Validation Study", art_id)

    # Datum value is 1000.0, but located line has 500 -> check_datum returns not_usable
    _create_datum(ws_id, source_id, "density", 1000.0, "kg/m^3", 2, 2, status="accepted")
    _create_datum(ws_id, source_id, "thermal_conductivity", 0.6, "W/(m·K)", 2, 2, status="accepted")

    board = compute_readiness_board()
    c = board.cases[0]
    assert c.steps["facts"].status == "red"
    assert "density" in c.steps["facts"].missing


# ------------------------------------------------------------------------------------------------
# Criterion 3: Parameter bound to raw datum or another source keeps bound red
# ------------------------------------------------------------------------------------------------

def test_criterion_3_raw_datum_or_other_source_keeps_bound_red(client: TestClient) -> None:
    ws_id = _seed_workspace("zephyr")
    backing_content = "Line 1: Header\nLine 2: 1000.0\nLine 3: 0.6\n"
    art_id = _create_backing_artifact(ws_id, backing_content)
    source_id = _create_source(ws_id, "Benchmark Validation Study", art_id)

    # Another source
    other_art_id = _create_backing_artifact(ws_id, backing_content, filename="other.md")
    other_source_id = _create_source(ws_id, "Other Paper", other_art_id)

    d_raw = _create_datum(ws_id, source_id, "density", 1000.0, "kg/m^3", 2, 2, status="raw")
    d_other = _create_datum(ws_id, other_source_id, "thermal_conductivity", 0.6, "W/(m·K)", 3, 3, status="accepted")

    # 1. Parameter bound to raw datum
    _create_parameter(ws_id, "material_density", f"literature_entry:{d_raw}")
    # 2. Parameter bound to another source's accepted datum
    _create_parameter(ws_id, "inlet_conductivity", f"literature_entry:{d_other}")

    board = compute_readiness_board()
    c = board.cases[0]
    assert c.steps["bound"].status == "red"
    assert "material_density" in c.steps["bound"].missing
    assert "inlet_conductivity" in c.steps["bound"].missing


# ------------------------------------------------------------------------------------------------
# Criterion 4: compare is red with missing_record when no bench run has the quantity
# ------------------------------------------------------------------------------------------------

def test_criterion_4_compare_is_red_without_matching_run(client: TestClient) -> None:
    _seed_workspace("zephyr")
    board = compute_readiness_board()
    for case in board.cases:
        cmp_step = case.steps["compare"]
        assert cmp_step.status == "red"
        assert cmp_step.reason == REASON_MISSING_RECORD
        assert cmp_step.missing == ["density"]


# ------------------------------------------------------------------------------------------------
# Criterion 5: Invalid case entries appear as board errors; valid cases still computed
# ------------------------------------------------------------------------------------------------

def test_criterion_5_invalid_case_entries_appear_as_board_errors(tmp_path: Path) -> None:
    invalid_cases_toml = tmp_path / "mixed_cases.toml"
    invalid_cases_toml.write_text(
        """
[[cases]]
id = "valid-1"
title = "Valid Case"
workspace = "zephyr"
source_title = "Study A"
facts = ["mass"]
inputs = ["p_mass"]

[cases.compare]
quantity = "mass"
value = 1.0
unit = "kg"
rel_tol = 0.01

[[cases]]
id = "invalid-no-title"
workspace = "zephyr"
source_title = "Study B"
facts = ["mass"]
inputs = ["p_mass"]

[[cases]]
id = "invalid-bad-compare"
title = "Case with Bad Compare"
workspace = "zephyr"
source_title = "Study C"
facts = ["mass"]
inputs = ["p_mass"]

[cases.compare]
quantity = "mass"
value = "not-a-number"
unit = "kg"
rel_tol = 0.01
""",
        encoding="utf-8",
    )

    board = compute_readiness_board(case_files=(invalid_cases_toml,))
    assert len(board.cases) == 1
    assert board.cases[0].id == "valid-1"
    assert len(board.errors) >= 2
    error_cids = {err.case_id for err in board.errors if err.case_id}
    assert "invalid-no-title" in error_cids or any("invalid-no-title" in err.message for err in board.errors)
    assert "invalid-bad-compare" in error_cids or any("invalid-bad-compare" in err.message for err in board.errors)


# ------------------------------------------------------------------------------------------------
# Criterion 6: No pack or no readiness key: empty board, route 200, command exit 0 plain message
# ------------------------------------------------------------------------------------------------

def test_criterion_6_empty_board_route_and_command(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    # Deliberately remove pack path to simulate zero packs
    monkeypatch.delenv("JARVISOS_PACK_PATH", raising=False)

    board = compute_readiness_board()
    assert board.cases == []
    assert board.errors == []
    assert board.summary.total_cases == 0
    assert board.summary.green_cells == 0
    assert board.summary.first_red is None

    # Route returns HTTP 200 with empty cases
    res = client.get("/readiness")
    assert res.status_code == 200
    body = res.json()
    assert body["cases"] == []
    assert body["summary"]["total_cases"] == 0

    # Command exits 0 with plain message
    script = REPO_ROOT / "scripts" / "readiness_board.py"
    env = dict(os.environ)
    env.pop("JARVISOS_PACK_PATH", None)
    proc = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "No readiness cases configured." in proc.stdout


# ------------------------------------------------------------------------------------------------
# Criterion 7: Route and command change nothing in data root (digests and counts unchanged)
# ------------------------------------------------------------------------------------------------

def _digest_data_root(data_root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    if not data_root.exists():
        return digests
    for p in sorted(data_root.rglob("*")):
        # SQLite's WAL and shared-memory sidecars are engine state left by a read-only reader, not data.
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
            cnt = conn.execute(f"SELECT COUNT(*) FROM {tname}").fetchone()[0]
            counts[tname] = cnt
    return counts


def test_criterion_7_data_root_immutability(client: TestClient) -> None:
    ws_id = _seed_workspace("zephyr")
    art_id = _create_backing_artifact(ws_id, "Line 1: 1000\nLine 2: 0.6\n")
    _create_source(ws_id, "Benchmark Validation Study", art_id)

    data_root = build_paths().data_root
    digests_before = _digest_data_root(data_root)
    counts_before = _table_row_counts()

    # Call the GET /readiness route
    res = client.get("/readiness")
    assert res.status_code == 200

    # Call the script
    script = REPO_ROOT / "scripts" / "readiness_board.py"
    proc = subprocess.run([sys.executable, str(script)], env=os.environ, capture_output=True, text=True, check=False)
    assert proc.returncode == 0

    digests_after = _digest_data_root(data_root)
    counts_after = _table_row_counts()

    assert digests_after == digests_before, "Data root files changed during readiness read"
    assert counts_after == counts_before, "Database rows changed during readiness read"
