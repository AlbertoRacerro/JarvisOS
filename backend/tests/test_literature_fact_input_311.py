"""Acceptance tests for spec 311 LITERATURE-FACT-TO-INPUT-1.

Criteria 1 through 5:
1. A datum created with quantity returns it; quantity on a claim is refused (422).
2. Accept on a located datum whose value is found at its locator gives accepted with the quantity;
   on a datum whose check fails, or an ingested entry not eligible for admission, the route returns 409
   and the row is unchanged.
3. Accept without a quantity in body or row is refused; re-accepting with the same quantity is idempotent;
   with a different quantity it is refused.
4. An entry id from another source or workspace gives 404.
5. With a pack case declaring that quantity as a fact and an input, after accepting the datum and creating the
   parameter through the page's calls, compute_readiness_board reports "facts" and "bound" green for the case.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.main import create_app
from app.modules.engineering.readiness_board import compute_readiness_board


@pytest.fixture
def client(isolated_data_root: None) -> TestClient:
    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client


def _create_workspace(client: TestClient, slug: str = "test-ws") -> str:
    res = client.post("/workspaces", json={"name": f"Workspace {slug}", "slug": slug})
    assert res.status_code == 201
    return res.json()["id"]


def _create_text_artifact(workspace_id: str, content: str, filename: str = "paper.md") -> str:
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


def _create_source(client: TestClient, workspace_id: str, title: str, artifact_id: str | None = None) -> str:
    payload: dict[str, object] = {
        "title": title,
        "source_kind": "paper",
        "citation": "Test Citation",
    }
    if artifact_id:
        payload["artifact_id"] = artifact_id
    res = client.post(f"/workspaces/{workspace_id}/literature/sources", json=payload)
    assert res.status_code == 201
    return res.json()["id"]


# --- Criterion 1 ---

def test_datum_created_with_quantity_returns_it(client: TestClient) -> None:
    ws_id = _create_workspace(client, "c1-datum")
    src_id = _create_source(client, ws_id, "Study 1")

    res = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries",
        json={
            "entry_kind": "datum",
            "value_text": "density is 1000 kg/m^3",
            "value_number": 1000.0,
            "unit": "kg/m^3",
            "quantity": "  density  ",
        },
    )
    assert res.status_code == 201
    data = res.json()
    assert data["quantity"] == "density"

    # Verify on GET source as well
    src_res = client.get(f"/workspaces/{ws_id}/literature/sources/{src_id}")
    assert src_res.status_code == 200
    entries = src_res.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["quantity"] == "density"


def test_datum_request_key_includes_quantity(client: TestClient) -> None:
    ws_id = _create_workspace(client, "c1-reqkey")
    src_id = _create_source(client, ws_id, "Study 1")

    req_key = "entry-req-key-1"
    payload = {
        "entry_kind": "datum",
        "value_text": "1000",
        "value_number": 1000.0,
        "unit": "kg/m^3",
        "quantity": "density",
        "request_key": req_key,
    }
    res1 = client.post(f"/workspaces/{ws_id}/literature/sources/{src_id}/entries", json=payload)
    assert res1.status_code == 201
    entry_id = res1.json()["id"]

    # Same request key and payload returns existing
    res2 = client.post(f"/workspaces/{ws_id}/literature/sources/{src_id}/entries", json=payload)
    assert res2.status_code == 201
    assert res2.json()["id"] == entry_id

    # Conflict with different quantity
    conflict_payload = dict(payload, quantity="other_quantity")
    res3 = client.post(f"/workspaces/{ws_id}/literature/sources/{src_id}/entries", json=conflict_payload)
    assert res3.status_code == 409


def test_quantity_on_claim_is_refused(client: TestClient) -> None:
    ws_id = _create_workspace(client, "c1-claim")
    src_id = _create_source(client, ws_id, "Study 1")

    res = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries",
        json={
            "entry_kind": "claim",
            "statement": "Reaction occurs at 300 K.",
            "quantity": "temperature",
        },
    )
    assert res.status_code == 422


def test_invalid_quantity_lengths_are_refused(client: TestClient) -> None:
    ws_id = _create_workspace(client, "c1-len")
    src_id = _create_source(client, ws_id, "Study 1")

    # Empty string
    res_empty = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries",
        json={"entry_kind": "datum", "value_number": 1.0, "unit": "m", "quantity": ""},
    )
    assert res_empty.status_code == 422

    # Whitespace only
    res_ws = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries",
        json={"entry_kind": "datum", "value_number": 1.0, "unit": "m", "quantity": "   "},
    )
    assert res_ws.status_code == 422

    # Exceeding 128 characters
    res_long = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries",
        json={"entry_kind": "datum", "value_number": 1.0, "unit": "m", "quantity": "a" * 129},
    )
    assert res_long.status_code == 422


# --- Criterion 2 ---

def test_accept_located_datum_success(client: TestClient) -> None:
    ws_id = _create_workspace(client, "c2-accept")
    art_id = _create_text_artifact(ws_id, "Line 1: preamble\nLine 2: density is 1000 kg/m^3\nLine 3: footer\n")
    src_id = _create_source(client, ws_id, "Paper with File", art_id)

    # Create located datum with line 2 matching 1000
    create_res = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries",
        json={
            "entry_kind": "datum",
            "value_text": "density is 1000 kg/m^3",
            "value_number": 1000.0,
            "unit": "kg/m^3",
            "locator_kind": "line",
            "locator_start": 2,
            "locator_end": 2,
            "quantity": "density",
        },
    )
    assert create_res.status_code == 201
    entry_id = create_res.json()["id"]
    assert create_res.json()["status"] == "raw"
    assert create_res.json()["verdict"] == "usable"

    # Accept the datum
    accept_res = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries/{entry_id}/accept",
        json={"quantity": "density"},
    )
    assert accept_res.status_code == 200
    accepted = accept_res.json()
    assert accepted["status"] == "accepted"
    assert accepted["quantity"] == "density"

    # Verify persisted in database
    with open_sqlite_connection() as conn:
        row = conn.execute("SELECT status, quantity FROM literature_entries WHERE id = ?", (entry_id,)).fetchone()
        assert row["status"] == "accepted"
        assert row["quantity"] == "density"


def test_accept_datum_check_failed_returns_409_and_row_unchanged(client: TestClient) -> None:
    ws_id = _create_workspace(client, "c2-fail")
    art_id = _create_text_artifact(ws_id, "Line 1: preamble\nLine 2: density is 1000 kg/m^3\n")
    src_id = _create_source(client, ws_id, "Paper with File", art_id)

    # Locator points to Line 1 where 1000 is NOT present -> check will fail (not_usable)
    create_res = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries",
        json={
            "entry_kind": "datum",
            "value_text": "density is 1000 kg/m^3",
            "value_number": 1000.0,
            "unit": "kg/m^3",
            "locator_kind": "line",
            "locator_start": 1,
            "locator_end": 1,
            "quantity": "density",
        },
    )
    assert create_res.status_code == 201
    entry_id = create_res.json()["id"]
    assert create_res.json()["verdict"] == "not_usable"

    accept_res = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries/{entry_id}/accept",
        json={"quantity": "density"},
    )
    assert accept_res.status_code == 409
    assert accept_res.json()["detail"]["code"] == "literature_entry_not_usable"

    # Database row is unchanged
    with open_sqlite_connection() as conn:
        row = conn.execute("SELECT status, quantity FROM literature_entries WHERE id = ?", (entry_id,)).fetchone()
        assert row["status"] == "raw"


def test_accept_ingested_entry_not_eligible_returns_409(client: TestClient) -> None:
    ws_id = _create_workspace(client, "c2-ingest")
    art_id = _create_text_artifact(ws_id, "Line 1: density is 1000 kg/m^3\n")
    src_id = _create_source(client, ws_id, "Ingest Paper", art_id)

    # Insert an entry with ingestion_status = 'flagged' (not eligible)
    entry_id = str(uuid.uuid4())
    now = "2026-11-18T00:00:00Z"
    with open_sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO literature_entries (
                id, workspace_id, source_id, entry_kind, value_text, value_number, unit,
                status, locator_kind, locator_start, locator_end, quantity,
                ingestion_status, ingestion_reasons_json, source_sha256, created_at, updated_at
            ) VALUES (?, ?, ?, 'datum', '1000', 1000.0, 'kg/m^3', 'raw', 'line', 1, 1, 'density', 'flagged', '[]', 'wrong-sha', ?, ?)
            """,
            (entry_id, ws_id, src_id, now, now),
        )
        conn.commit()

    accept_res = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries/{entry_id}/accept",
        json={"quantity": "density"},
    )
    assert accept_res.status_code == 409
    assert accept_res.json()["detail"]["code"] == "literature_entry_not_usable"

    # Row status is still raw
    with open_sqlite_connection() as conn:
        row = conn.execute("SELECT status FROM literature_entries WHERE id = ?", (entry_id,)).fetchone()
        assert row["status"] == "raw"


# --- Criterion 3 ---

def test_accept_without_quantity_refused(client: TestClient) -> None:
    ws_id = _create_workspace(client, "c3-noqty")
    art_id = _create_text_artifact(ws_id, "Line 1: 1000 kg/m^3\n")
    src_id = _create_source(client, ws_id, "Paper", art_id)

    create_res = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries",
        json={
            "entry_kind": "datum",
            "value_number": 1000.0,
            "unit": "kg/m^3",
            "locator_kind": "line",
            "locator_start": 1,
            "locator_end": 1,
        },
    )
    assert create_res.status_code == 201
    entry_id = create_res.json()["id"]

    # Calling accept without quantity in body and no stored quantity in row
    accept_res = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries/{entry_id}/accept",
        json={"quantity": None},
    )
    assert accept_res.status_code == 422


def test_reaccept_idempotent_and_rename_refused(client: TestClient) -> None:
    ws_id = _create_workspace(client, "c3-idemp")
    art_id = _create_text_artifact(ws_id, "Line 1: 1000 kg/m^3\n")
    src_id = _create_source(client, ws_id, "Paper", art_id)

    create_res = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries",
        json={
            "entry_kind": "datum",
            "value_number": 1000.0,
            "unit": "kg/m^3",
            "locator_kind": "line",
            "locator_start": 1,
            "locator_end": 1,
            "quantity": "density",
        },
    )
    entry_id = create_res.json()["id"]

    # First accept
    acc1 = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries/{entry_id}/accept",
        json={"quantity": "density"},
    )
    assert acc1.status_code == 200
    assert acc1.json()["status"] == "accepted"

    # Re-accepting with same quantity is idempotent
    acc2 = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries/{entry_id}/accept",
        json={"quantity": "density"},
    )
    assert acc2.status_code == 200
    assert acc2.json()["status"] == "accepted"

    # Re-accepting with body quantity=None reuses stored quantity and is idempotent
    acc3 = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries/{entry_id}/accept",
        json={"quantity": None},
    )
    assert acc3.status_code == 200
    assert acc3.json()["status"] == "accepted"

    # Re-accepting with different quantity is refused (409)
    acc4 = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries/{entry_id}/accept",
        json={"quantity": "thermal_conductivity"},
    )
    assert acc4.status_code == 409
    assert acc4.json()["detail"]["code"] == "literature_entry_already_accepted"

    # Row still has original quantity
    with open_sqlite_connection() as conn:
        row = conn.execute("SELECT quantity FROM literature_entries WHERE id = ?", (entry_id,)).fetchone()
        assert row["quantity"] == "density"


# --- Criterion 4 ---

def test_accept_cross_workspace_or_source_gives_404(client: TestClient) -> None:
    ws1 = _create_workspace(client, "c4-ws1")
    ws2 = _create_workspace(client, "c4-ws2")
    art1 = _create_text_artifact(ws1, "Line 1: 1000 kg/m^3\n")
    src1 = _create_source(client, ws1, "Paper 1", art1)
    src2 = _create_source(client, ws1, "Paper 2")

    create_res = client.post(
        f"/workspaces/{ws1}/literature/sources/{src1}/entries",
        json={
            "entry_kind": "datum",
            "value_number": 1000.0,
            "unit": "kg/m^3",
            "locator_kind": "line",
            "locator_start": 1,
            "locator_end": 1,
            "quantity": "density",
        },
    )
    entry_id = create_res.json()["id"]

    # Entry exists in ws1/src1. Calling accept in wrong workspace -> 404
    res_wrong_ws = client.post(
        f"/workspaces/{ws2}/literature/sources/{src1}/entries/{entry_id}/accept",
        json={"quantity": "density"},
    )
    assert res_wrong_ws.status_code == 404

    # Calling accept in right workspace but wrong source -> 404
    res_wrong_src = client.post(
        f"/workspaces/{ws1}/literature/sources/{src2}/entries/{entry_id}/accept",
        json={"quantity": "density"},
    )
    assert res_wrong_src.status_code == 404

    # Nonexistent entry id -> 404
    res_nonexistent = client.post(
        f"/workspaces/{ws1}/literature/sources/{src1}/entries/nonexistent-id/accept",
        json={"quantity": "density"},
    )
    assert res_nonexistent.status_code == 404


# --- Criterion 5 ---

def test_readiness_board_facts_and_bound_green(client: TestClient, tmp_path: Path) -> None:
    ws_slug = "ws-readiness-311"
    ws_id = _create_workspace(client, ws_slug)
    source_title = "Benchmark Validation Study"
    art_id = _create_text_artifact(ws_id, "Line 1: preamble\nLine 2: density is 1000 kg/m^3\n")
    src_id = _create_source(client, ws_id, source_title, art_id)

    # Temporary readiness case file
    case_toml = tmp_path / "cases.toml"
    case_toml.write_text(
        f"""
[[cases]]
id = "case-311"
title = "Case 311 Study"
workspace = "{ws_slug}"
source_title = "{source_title}"
facts = ["density"]
inputs = ["material_density"]
""",
        encoding="utf-8",
    )

    # 1. Before creating datum: facts is red, bound is red
    board = compute_readiness_board(case_files=(case_toml,))
    assert len(board.cases) == 1
    case = board.cases[0]
    assert case.steps["paper"].status == "green"
    assert case.steps["facts"].status == "red"
    assert case.steps["bound"].status == "red"

    # 2. Record datum with quantity "density"
    create_res = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries",
        json={
            "entry_kind": "datum",
            "value_text": "density is 1000 kg/m^3",
            "value_number": 1000.0,
            "unit": "kg/m^3",
            "locator_kind": "line",
            "locator_start": 2,
            "locator_end": 2,
            "quantity": "density",
        },
    )
    assert create_res.status_code == 201
    entry_id = create_res.json()["id"]

    # Still raw, so facts is red
    board = compute_readiness_board(case_files=(case_toml,))
    assert board.cases[0].steps["facts"].status == "red"

    # 3. Accept the datum
    accept_res = client.post(
        f"/workspaces/{ws_id}/literature/sources/{src_id}/entries/{entry_id}/accept",
        json={"quantity": "density"},
    )
    assert accept_res.status_code == 200

    # Now facts is green, but bound is still red
    board = compute_readiness_board(case_files=(case_toml,))
    assert board.cases[0].steps["facts"].status == "green"
    assert board.cases[0].steps["bound"].status == "red"

    # 4. Create parameter pointing to the literature entry (the page's call)
    param_res = client.post(
        f"/workspaces/{ws_id}/parameters",
        json={
            "name": "material_density",
            "value": "1000.0",
            "unit": "kg/m^3",
            "value_status": "literature",
            "source_ref": f"literature_entry:{entry_id}",
            "status": "draft",
        },
    )
    assert param_res.status_code == 201

    # 5. compute_readiness_board reports both facts and bound green!
    board = compute_readiness_board(case_files=(case_toml,))
    case_result = board.cases[0]
    assert case_result.steps["facts"].status == "green"
    assert case_result.steps["bound"].status == "green"
