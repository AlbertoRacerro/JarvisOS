"""Acceptance tests for converged process runs in case comparison (spec 320)."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.main import create_app
from app.modules.engineering.readiness_board import compute_readiness_board
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import AddStream, AddUnit, SetThermo

NOW = "2026-11-19T00:00:00Z"


@pytest.fixture(autouse=True)
def initialized_storage(isolated_data_root: None) -> None:
    initialize_storage(seed_default=True)


def _client(isolated_data_root: None) -> TestClient:
    initialize_storage(seed_default=True)
    return TestClient(create_app())


def _workspace(slug: str = "zephyr") -> str:
    workspace_id = str(uuid.uuid4())
    with open_sqlite_connection() as connection:
        connection.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'Test workspace', 'active', ?, ?)",
            (workspace_id, f"Workspace {slug}", slug, NOW, NOW),
        )
        connection.commit()
    return workspace_id


def _process_draft(workspace_id: str, name: str = "Linea 1") -> dict:
    state = draft.create_draft(workspace_id, name)
    state = draft.patch(
        workspace_id,
        state["draft_id"],
        state["revision"],
        [
            SetThermo(op="set_thermo", compounds=["Water"], property_package="NRTL"),
            AddUnit(op="add_unit", id="pump", type="Pump", tag="Pump1", x=100, y=100),
            AddStream(op="add_stream", id="feed", tag="Feed", x=0, y=100),
        ],
    )
    return state


def _record_converged_run(
    workspace_id: str,
    state: dict,
    *,
    run_id: str,
    started_at: str = "2026-11-19T01:00:00+00:00",
) -> str:
    directory = draft.draft_dir(workspace_id, state["draft_id"])
    draft.record_run(
        directory,
        {
            "run_id": run_id,
            "draft_id": state["draft_id"],
            "draft_revision": state["revision"],
            "action": "run",
            "status": "completed",
            "started_at": started_at,
            "outcome": {"state": "converged"},
            "streams": {
                "Feed": {
                    "mass_flow_kg_s": 1.0,
                    "mass_fractions": {"Water": 1.0},
                    "display": {"temperature": {"value": 300.0, "unit": "K"}},
                }
            },
            "units": {
                "Pump1": {
                    "reported": {
                        "pressure": {"value": 3.0, "units": "bar"},
                    }
                }
            },
        },
    )
    return run_id


def _case_file(tmp_path: Path, quantity: str) -> Path:
    path = tmp_path / f"case-{quantity.replace('.', '_')}.toml"
    path.write_text(
        f"""
[[cases]]
id = "process_case"
title = "Process Case"
workspace = "zephyr"
source_title = "Study"
[cases.compare]
quantity = "{quantity}"
value = 3600.0
unit = "kg/h"
rel_tol = 0.05
""",
        encoding="utf-8",
    )
    return path


def _case_file_unit(tmp_path: Path, quantity: str, value: float, unit: str) -> Path:
    path = tmp_path / f"case-{quantity.replace('.', '_')}.toml"
    path.write_text(
        f"""
[[cases]]
id = "process_case"
title = "Process Case"
workspace = "zephyr"
source_title = "Study"
[cases.compare]
quantity = "{quantity}"
value = {value}
unit = "{unit}"
rel_tol = 0.05
""",
        encoding="utf-8",
    )
    return path


def _create_bench_run(workspace_id: str, run_id: str, quantity: str, value: float, unit: str) -> None:
    directory = build_paths().workspaces_dir / workspace_id / "engineering" / "bench" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "complete",
                "verdict": "usable",
                "completed_at": NOW,
                "outputs": [{"name": quantity, "value": {"value": value, "unit": unit}}],
            }
        ),
        encoding="utf-8",
    )


def test_criterion_1_converged_process_run_makes_run_green(isolated_data_root: None, tmp_path: Path) -> None:
    workspace_id = _workspace()
    state = _process_draft(workspace_id)
    _record_converged_run(workspace_id, state, run_id="a" * 32)
    board = compute_readiness_board(case_files=(_case_file(tmp_path, "water_input"),))
    assert board.cases[0].steps["run"].status == "green"


def test_criterion_2_available_kpi_compare_has_process_reference_and_label(isolated_data_root: None, tmp_path: Path) -> None:
    workspace_id = _workspace()
    state = _process_draft(workspace_id, "Linea KPI")
    run_id = _record_converged_run(workspace_id, state, run_id="b" * 32)
    board = compute_readiness_board(case_files=(_case_file(tmp_path, "water_input"),))
    comparison = board.cases[0].steps["compare"].comparison
    assert comparison is not None
    assert comparison.run_id == f"process:{state['draft_id']}:{run_id}"
    assert comparison.run_label == 'Diagramma "Linea KPI"'
    assert comparison.computed.value == 3600.0
    assert comparison.computed.unit == "kg/h"
    assert board.cases[0].steps["compare"].status == "green"


def test_criterion_3_unit_and_stream_quantities_resolve_unknown_missing(isolated_data_root: None, tmp_path: Path) -> None:
    workspace_id = _workspace()
    state = _process_draft(workspace_id)
    _record_converged_run(workspace_id, state, run_id="c" * 32)

    unit_board = compute_readiness_board(case_files=(_case_file_unit(tmp_path, "Pump1.pressure", 3.0, "bar"),))
    unit_cmp = unit_board.cases[0].steps["compare"].comparison
    assert unit_cmp is not None
    assert unit_cmp.computed.value == 3.0
    assert unit_cmp.computed.unit == "bar"

    stream_board = compute_readiness_board(case_files=(_case_file_unit(tmp_path, "Feed.temperature", 300.0, "K"),))
    stream_cmp = stream_board.cases[0].steps["compare"].comparison
    assert stream_cmp is not None
    assert stream_cmp.computed.value == 300.0
    assert stream_cmp.computed.unit == "K"

    missing_board = compute_readiness_board(case_files=(_case_file(tmp_path, "Unknown.quantity"),))
    missing = missing_board.cases[0].steps["compare"]
    assert missing.status == "red"
    assert missing.reason == "missing_record"


def test_criterion_4_non_converged_run_never_makes_run_or_compare_green(isolated_data_root: None, tmp_path: Path) -> None:
    workspace_id = _workspace()
    state = _process_draft(workspace_id)
    directory = draft.draft_dir(workspace_id, state["draft_id"])
    draft.record_run(
        directory,
        {
            "run_id": "d" * 32,
            "draft_id": state["draft_id"],
            "draft_revision": state["revision"],
            "action": "run",
            "status": "completed",
            "started_at": NOW,
            "outcome": {"state": "non_converged"},
            "streams": {},
            "units": {},
        },
    )
    board = compute_readiness_board(case_files=(_case_file(tmp_path, "water_input"),))
    assert board.cases[0].steps["run"].status == "red"
    assert board.cases[0].steps["compare"].status == "red"


def test_criterion_5_bench_run_keeps_priority(isolated_data_root: None, tmp_path: Path) -> None:
    workspace_id = _workspace()
    state = _process_draft(workspace_id)
    _record_converged_run(workspace_id, state, run_id="e" * 32)
    _create_bench_run(workspace_id, "bench_priority", "water_input", 3600.0, "kg/h")
    board = compute_readiness_board(case_files=(_case_file(tmp_path, "water_input"),))
    comparison = board.cases[0].steps["compare"].comparison
    assert comparison is not None
    assert comparison.run_id == "bench_priority"


def test_criterion_6_process_verdict_stored_and_decision_acceptance(
    isolated_data_root: None, tmp_path: Path
) -> None:
    with _client(isolated_data_root) as client:
        workspace_id = _workspace()
        state = _process_draft(workspace_id)
        run_id = _record_converged_run(workspace_id, state, run_id="f" * 32)
        reference = f"process:{state['draft_id']}:{run_id}"
        created = client.post(
            f"/workspaces/{workspace_id}/engineering/case-verdicts",
            json={
                "case_id": "process_case",
                "run_id": reference,
                "quantity": "water_input",
                "published_value": 3600.0,
                "published_unit": "kg/h",
                "rel_tol": 0.05,
            },
        )
        assert created.status_code == 201
        verdict_id = created.json()["id"]
        patched = client.patch(
            f"/workspaces/{workspace_id}/engineering/case-verdicts/{verdict_id}",
            json={"status": "accepted"},
        )
        assert patched.status_code == 200
        decision = client.post(
            f"/workspaces/{workspace_id}/decisions",
            json={
                "title": "Accept process result",
                "decision_text": "The process KPI matches.",
                "status": "accepted",
                "case_verdict_id": verdict_id,
            },
        )
        assert decision.status_code == 201
        board = compute_readiness_board(case_files=(_case_file(tmp_path, "water_input"),))
        compare = board.cases[0].steps["compare"]
        assert compare.comparison is not None
        assert compare.stored_verdict is not None
        assert compare.stored_verdict.run_id == reference
        assert board.cases[0].steps["decide"].status == "green"


def test_criterion_7_process_reference_error_codes(isolated_data_root: None) -> None:
    with _client(isolated_data_root) as client:
        workspace_id = _workspace()
        payload = {
            "case_id": "process_case",
            "quantity": "water_input",
            "published_value": 3600.0,
            "published_unit": "kg/h",
            "rel_tol": 0.05,
        }
        malformed = client.post(
            f"/workspaces/{workspace_id}/engineering/case-verdicts",
            json={**payload, "run_id": "process:not-valid:not-valid"},
        )
        assert malformed.status_code == 404
        assert malformed.json()["detail"]["code"] == "run_not_found"

        state = _process_draft(workspace_id)
        directory = draft.draft_dir(workspace_id, state["draft_id"])
        draft.record_run(
            directory,
            {
                "run_id": "9" * 32,
                "draft_id": state["draft_id"],
                "draft_revision": state["revision"],
                "action": "run",
                "status": "completed",
                "started_at": NOW,
                "outcome": {"state": "non_converged"},
                "streams": {},
                "units": {},
            },
        )
        nonconverged = client.post(
            f"/workspaces/{workspace_id}/engineering/case-verdicts",
            json={**payload, "run_id": f"process:{state['draft_id']}:{'9' * 32}"},
        )
        assert nonconverged.status_code == 422
        assert nonconverged.json()["detail"]["code"] == "run_not_usable"

        _record_converged_run(workspace_id, state, run_id="8" * 32)
        missing = client.post(
            f"/workspaces/{workspace_id}/engineering/case-verdicts",
            json={**payload, "run_id": f"process:{state['draft_id']}:{'8' * 32}", "quantity": "unknown"},
        )
        assert missing.status_code == 422
        assert missing.json()["detail"]["code"] == "run_lacks_quantity"

        mismatch = client.post(
            f"/workspaces/{workspace_id}/engineering/case-verdicts",
            json={
                **payload,
                "run_id": f"process:{state['draft_id']}:{'8' * 32}",
                "quantity": "Pump1.pressure",
                "published_unit": "s",
            },
        )
        assert mismatch.status_code == 422
        assert mismatch.json()["detail"]["code"] == "unit_mismatch"


def test_criterion_8_process_responses_do_not_leak_data_root(
    isolated_data_root: None, tmp_path: Path
) -> None:
    workspace_id = _workspace()
    state = _process_draft(workspace_id)
    _record_converged_run(workspace_id, state, run_id="7" * 32)
    board = compute_readiness_board(case_files=(_case_file(tmp_path, "water_input"),))
    text = json.dumps(board.model_dump())
    assert os.environ["JARVISOS_DATA_ROOT"] not in text
