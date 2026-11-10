"""Tests for Spec 257 scope item 2: precheck degrees-of-freedom report."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.config import get_settings
from app.core.database import open_sqlite_connection
from app.main import app
from app.modules.process_stack import draft


@pytest.fixture
def test_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, str, str]]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    get_settings.cache_clear()
    initialize_storage(seed_default=True)
    with TestClient(app) as client:
        ws_id = "main"
        base = f"/workspaces/{ws_id}/process/drafts"
        created = client.post(base, json={"name": "Precheck draft"})
        created.raise_for_status()
        draft_id = created.json()["draft_id"]
        yield client, ws_id, draft_id
    get_settings.cache_clear()


def _setup_draft_with_missing_inputs(client: TestClient, ws_id: str, draft_id: str) -> dict[str, Any]:
    res = client.get(f"/workspaces/{ws_id}/process/drafts/{draft_id}")
    res.raise_for_status()
    rev = res.json()["revision"]

    ops = [
        {"op": "set_thermo", "compounds": ["Water"], "property_package": "Steam Tables (IAPWS-IF97)"},
        {"op": "add_stream", "id": "feed", "tag": "Feed", "x": 0, "y": 100},
        {"op": "add_unit", "id": "h1", "type": "Heater", "tag": "H1", "x": 100, "y": 100},
        {"op": "add_stream", "id": "prod", "tag": "Prod", "x": 200, "y": 100},
        {"op": "connect", "stream": "feed", "end": "target", "unit": "h1", "port": 0},
        {"op": "connect", "stream": "prod", "end": "source", "unit": "h1", "port": 0},
        {
            "op": "set_stream_spec",
            "stream": "feed",
            "pressure": {"value": 1.0, "unit": "bar"},
            "temperature": {"value": 300.0, "unit": "K"},
            "composition": {"Water": 1.0},
        },
    ]
    patch_res = client.post(
        f"/workspaces/{ws_id}/process/drafts/{draft_id}/patch",
        json={"expected_revision": rev, "ops": ops},
    )
    patch_res.raise_for_status()
    return patch_res.json()


def _insert_result_value(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    quantity: str,
    value_text: str,
    unit: str,
    status: str = "passed",
    conditions: dict[str, Any] | None = None,
    created_at: str = "2026-11-09T12:00:00+00:00",
    output_name: str = "output_1",
    run_id: str | None = None,
    batch_id: str | None = None,
) -> tuple[str, str, str]:
    run_id = run_id or f"run-{uuid4().hex[:8]}"
    batch_id = batch_id or f"batch-{uuid4().hex[:8]}"
    value_id = f"val-{uuid4().hex[:8]}"

    connection.execute(
        "INSERT OR IGNORE INTO simulation_runs (id, workspace_id, status, created_at) VALUES (?, ?, 'succeeded', ?)",
        (run_id, workspace_id, created_at),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO simulation_result_batches (
            id, workspace_id, run_id, source_payload_digest, extractor_version, notification_state, created_at
        ) VALUES (?, ?, ?, 'digest-test', '1.0', 'none', ?)
        """,
        (batch_id, workspace_id, run_id, created_at),
    )
    conds_json = json.dumps(conditions if conditions is not None else {})
    connection.execute(
        """
        INSERT INTO simulation_result_values (
            id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version,
            quantity, value_text, unit, conditions_json, status, created_at
        ) VALUES (?, ?, ?, 'digest-test', 'extractor-test', '1.0', ?, ?, ?, ?, ?, ?)
        """,
        (value_id, batch_id, output_name, quantity, value_text, unit, conds_json, status, created_at),
    )
    connection.commit()
    return run_id, batch_id, value_id


def test_dof_case_a_missing_inputs(test_setup: tuple[TestClient, str, str]) -> None:
    client, ws_id, draft_id = test_setup
    state = _setup_draft_with_missing_inputs(client, ws_id, draft_id)

    res = client.get(f"/workspaces/{ws_id}/process/drafts/{draft_id}/dof")
    assert res.status_code == 200
    report = res.json()

    assert report["workspace_id"] == ws_id
    assert report["draft_id"] == draft_id
    assert report["revision"] == state["revision"]

    entries = report["entries"]
    counts = report["counts"]
    assert counts["set"] + counts["unset"] + counts["available"] == len(entries)
    assert counts["set"] == sum(1 for e in entries if e["status"] == "set")
    assert counts["unset"] == sum(1 for e in entries if e["status"] == "unset")
    assert counts["available"] == sum(1 for e in entries if e["status"] == "available")
    assert counts["available"] == 0

    # set parameters are "set"
    set_entries = [e for e in entries if e["status"] == "set"]
    assert any(e["object"] == "H1" and e["key"] == "pressure_drop" for e in set_entries)
    assert any(e["object"] == "Feed" and e["key"] == "pressure" for e in set_entries)
    assert any(e["object"] == "Feed" and e["key"] == "state" for e in set_entries)
    assert any(e["object"] == "Feed" and e["key"] == "composition" for e in set_entries)

    # unset parameters are "unset"
    unset_entries = [e for e in entries if e["status"] == "unset"]
    assert len(unset_entries) == 2
    assert unset_entries[0]["object"] == "H1" and unset_entries[0]["key"] == "outlet_temperature"
    assert unset_entries[1]["object"] == "Feed" and unset_entries[1]["key"] == "flow"

    # compare against validate_document findings
    directory = draft.draft_dir(ws_id, draft_id)
    head = draft._head(directory)
    document = draft.load_revision(directory, head["revision"])["document"]
    findings = draft.validate_document(document, ws_id)
    missing_findings = [f for f in findings if f["code"] in {"UNIT_PARAM_MISSING", "FEED_SPEC_MISSING"}]

    assert len(missing_findings) == 2
    assert [e["object"] for e in unset_entries] == [f["object"] for f in missing_findings]
    assert unset_entries[0]["key"] == missing_findings[0]["field"]
    assert missing_findings[1]["field"] in unset_entries[1]["alternatives"]


def test_dof_case_b_design_value_passed(test_setup: tuple[TestClient, str, str]) -> None:
    client, ws_id, draft_id = test_setup
    _setup_draft_with_missing_inputs(client, ws_id, draft_id)

    with open_sqlite_connection() as connection:
        run_id_h1, batch_id_h1, val_id_h1 = _insert_result_value(
            connection,
            workspace_id=ws_id,
            quantity="H1.outlet_temperature",
            value_text="150.0",
            unit="degC",
            status="passed",
            conditions={},
            output_name="h1_temp_out",
        )
        run_id_f, batch_id_f, val_id_f = _insert_result_value(
            connection,
            workspace_id=ws_id,
            quantity="Feed.mass_flow",
            value_text="7200.0",
            unit="kg/h",
            status="passed",
            conditions={},
            output_name="feed_flow_out",
        )

    res = client.get(f"/workspaces/{ws_id}/process/drafts/{draft_id}/dof")
    assert res.status_code == 200
    report = res.json()

    entries_by_key = {(e["object"], e["key"]): e for e in report["entries"]}

    h1_entry = entries_by_key[("H1", "outlet_temperature")]
    assert h1_entry["status"] == "available"
    assert h1_entry["design_value"]["value_id"] == val_id_h1
    assert h1_entry["design_value"]["value"] == 150.0
    assert h1_entry["design_value"]["unit"] == "degC"
    assert abs(h1_entry["design_value"]["si"] - 423.15) < 1e-4
    assert h1_entry["design_value"]["validation"] == "passed"
    assert h1_entry["design_value"]["source"] == {
        "run_id": run_id_h1,
        "batch_id": batch_id_h1,
        "output_name": "h1_temp_out",
    }

    feed_entry = entries_by_key[("Feed", "flow")]
    assert feed_entry["status"] == "available"
    assert feed_entry["design_value"]["value_id"] == val_id_f
    assert feed_entry["design_value"]["value"] == 7200.0
    assert feed_entry["design_value"]["unit"] == "kg/h"
    assert abs(feed_entry["design_value"]["si"] - 2.0) < 1e-6
    assert feed_entry["design_value"]["validation"] == "passed"
    assert feed_entry["design_value"]["source"] == {
        "run_id": run_id_f,
        "batch_id": batch_id_f,
        "output_name": "feed_flow_out",
    }

    assert report["counts"]["available"] == 2
    assert report["counts"]["unset"] == 0


def test_dof_case_c_design_value_red_dot(test_setup: tuple[TestClient, str, str]) -> None:
    client, ws_id, draft_id = test_setup
    _setup_draft_with_missing_inputs(client, ws_id, draft_id)

    with open_sqlite_connection() as connection:
        # Status "exception" gives "red_dot"
        _insert_result_value(
            connection,
            workspace_id=ws_id,
            quantity="H1.outlet_temperature",
            value_text="400.0",
            unit="K",
            status="exception",
            conditions={},
        )
        # Status "passed" but carrying _unvalidated_input_value_ids gives "red_dot"
        _insert_result_value(
            connection,
            workspace_id=ws_id,
            quantity="Feed.mass_flow",
            value_text="1.5",
            unit="kg/s",
            status="passed",
            conditions={"_unvalidated_input_value_ids": ["val-upstream-123"]},
        )

    res = client.get(f"/workspaces/{ws_id}/process/drafts/{draft_id}/dof")
    assert res.status_code == 200
    report = res.json()

    entries_by_key = {(e["object"], e["key"]): e for e in report["entries"]}
    h1_entry = entries_by_key[("H1", "outlet_temperature")]
    assert h1_entry["status"] == "available"
    assert h1_entry["design_value"]["validation"] == "red_dot"

    feed_entry = entries_by_key[("Feed", "flow")]
    assert feed_entry["status"] == "available"
    assert feed_entry["design_value"]["validation"] == "red_dot"


def test_dof_case_d_non_matching_gives_unset(test_setup: tuple[TestClient, str, str]) -> None:
    client, ws_id, draft_id = test_setup
    _setup_draft_with_missing_inputs(client, ws_id, draft_id)

    with open_sqlite_connection() as connection:
        # Non-empty conditions -> unset
        _insert_result_value(
            connection,
            workspace_id=ws_id,
            quantity="H1.outlet_temperature",
            value_text="350.0",
            unit="K",
            status="passed",
            conditions={"ambient_temperature": 298.15},
        )
        # Unit not in allowed list -> unset
        _insert_result_value(
            connection,
            workspace_id=ws_id,
            quantity="Feed.mass_flow",
            value_text="100.0",
            unit="m/s",
            status="passed",
            conditions={},
        )

    res = client.get(f"/workspaces/{ws_id}/process/drafts/{draft_id}/dof")
    assert res.status_code == 200
    report = res.json()

    entries_by_key = {(e["object"], e["key"]): e for e in report["entries"]}
    assert entries_by_key[("H1", "outlet_temperature")]["status"] == "unset"
    assert entries_by_key[("Feed", "flow")]["status"] == "unset"

    # Different workspace -> unset
    with open_sqlite_connection() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, slug, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("other_workspace", "Other", "other-workspace", "2026-11-09T12:00:00+00:00", "2026-11-09T12:00:00+00:00"),
        )
        _insert_result_value(
            connection,
            workspace_id="other_workspace",
            quantity="H1.outlet_temperature",
            value_text="350.0",
            unit="K",
            status="passed",
            conditions={},
        )

    res = client.get(f"/workspaces/{ws_id}/process/drafts/{draft_id}/dof")
    assert res.status_code == 200
    report = res.json()
    entries_by_key = {(e["object"], e["key"]): e for e in report["entries"]}
    assert entries_by_key[("H1", "outlet_temperature")]["status"] == "unset"

    # Latest non-matching value overrides older matching value -> gives unset, never older value
    with open_sqlite_connection() as connection:
        _insert_result_value(
            connection,
            workspace_id=ws_id,
            quantity="H1.outlet_temperature",
            value_text="350.0",
            unit="K",
            status="passed",
            conditions={},
            created_at="2026-11-09T08:00:00+00:00",
        )
        _insert_result_value(
            connection,
            workspace_id=ws_id,
            quantity="H1.outlet_temperature",
            value_text="360.0",
            unit="K",
            status="passed",
            conditions={"extra": "unmatched"},
            created_at="2026-11-09T09:00:00+00:00",
        )

    res = client.get(f"/workspaces/{ws_id}/process/drafts/{draft_id}/dof")
    assert res.status_code == 200
    report = res.json()
    entries_by_key = {(e["object"], e["key"]): e for e in report["entries"]}
    assert entries_by_key[("H1", "outlet_temperature")]["status"] == "unset"


def test_dof_case_e_read_only(test_setup: tuple[TestClient, str, str]) -> None:
    client, ws_id, draft_id = test_setup
    _setup_draft_with_missing_inputs(client, ws_id, draft_id)

    directory = draft.draft_dir(ws_id, draft_id)
    rev_before = draft._head(directory)["revision"]

    with open_sqlite_connection() as connection:
        v_count_before = connection.execute("SELECT COUNT(*) FROM simulation_result_values").fetchone()[0]
        b_count_before = connection.execute("SELECT COUNT(*) FROM simulation_result_batches").fetchone()[0]
        p_count_before = connection.execute("SELECT COUNT(*) FROM parameters").fetchone()[0]

    # Call endpoint
    res = client.get(f"/workspaces/{ws_id}/process/drafts/{draft_id}/dof")
    assert res.status_code == 200

    rev_after = draft._head(directory)["revision"]
    assert rev_after == rev_before

    with open_sqlite_connection() as connection:
        v_count_after = connection.execute("SELECT COUNT(*) FROM simulation_result_values").fetchone()[0]
        b_count_after = connection.execute("SELECT COUNT(*) FROM simulation_result_batches").fetchone()[0]
        p_count_after = connection.execute("SELECT COUNT(*) FROM parameters").fetchone()[0]

    assert v_count_after == v_count_before
    assert b_count_after == b_count_before
    assert p_count_after == p_count_before


def test_dof_case_f_unknown_draft_or_workspace_error(test_setup: tuple[TestClient, str, str]) -> None:
    client, ws_id, draft_id = test_setup

    unknown_draft = "00000000-0000-0000-0000-000000000000"
    res_dof = client.get(f"/workspaces/{ws_id}/process/drafts/{unknown_draft}/dof")
    res_get = client.get(f"/workspaces/{ws_id}/process/drafts/{unknown_draft}")
    assert res_dof.status_code == res_get.status_code == 404
    assert res_dof.json() == res_get.json()

    # Unknown workspace
    res_dof_ws = client.get(f"/workspaces/nonexistent_ws/process/drafts/{draft_id}/dof")
    res_get_ws = client.get(f"/workspaces/nonexistent_ws/process/drafts/{draft_id}")
    assert res_dof_ws.status_code == res_get_ws.status_code == 404
    assert res_dof_ws.json() == res_get_ws.json()
