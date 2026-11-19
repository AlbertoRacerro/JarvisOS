"""Tests for spec 312 GUIDED-FLOW-1: Guided question flow in Design."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.main import create_app
from app.modules.events.service import utc_now


@pytest.fixture
def client(isolated_data_root: None) -> TestClient:
    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client


def _create_workspace(client: TestClient, slug: str = "test-ws") -> str:
    res = client.post("/workspaces", json={"name": f"Workspace {slug}", "slug": slug})
    assert res.status_code == 201
    return res.json()["id"]


def test_guided_flow_crud_and_status_cycle(client: TestClient) -> None:
    ws_id = _create_workspace(client, "crud-ws")

    # Initial GET - no questions yet
    get_res = client.get(f"/workspaces/{ws_id}/engineering/guided-questions")
    assert get_res.status_code == 200
    data = get_res.json()
    assert len(data["steps_definition"]) == 7
    assert data["questions"] == []
    assert data["active_question_id"] is None
    assert len(data["steps"]) == 7
    # Initial fallback next action should be frame
    assert data["next_action"]["step"] == "frame"

    # POST create question
    post_res = client.post(
        f"/workspaces/{ws_id}/engineering/guided-questions",
        json={
            "title": "Come ottimizzare il rendimento termico?",
            "what_changes": "Aumento del diametro condotto",
        },
    )
    assert post_res.status_code == 201
    q = post_res.json()
    q_id = q["id"]
    assert q["workspace_id"] == ws_id
    assert q["title"] == "Come ottimizzare il rendimento termico?"
    assert q["what_changes"] == "Aumento del diametro condotto"
    assert q["status"] == "open"
    assert len(q["steps"]) == 7
    assert q["next_action"]["step"] == "frame"

    # PATCH update question fields
    patch_res = client.patch(
        f"/workspaces/{ws_id}/engineering/guided-questions/{q_id}",
        json={
            "what_changes": "Aggiornato: variazione portata",
            "explore_skip_reason": "Varianti già verificate in v1",
            "linked_decision_id": "dec-101",
        },
    )
    assert patch_res.status_code == 200
    q_updated = patch_res.json()
    assert q_updated["what_changes"] == "Aggiornato: variazione portata"
    assert q_updated["explore_skip_reason"] == "Varianti già verificate in v1"
    assert q_updated["linked_decision_id"] == "dec-101"

    # Close and reopen question
    close_res = client.patch(
        f"/workspaces/{ws_id}/engineering/guided-questions/{q_id}",
        json={"status": "closed"},
    )
    assert close_res.status_code == 200
    assert close_res.json()["status"] == "closed"

    reopen_res = client.patch(
        f"/workspaces/{ws_id}/engineering/guided-questions/{q_id}",
        json={"status": "open"},
    )
    assert reopen_res.status_code == 200
    assert reopen_res.json()["status"] == "open"

    # Verify in GET list
    list_res = client.get(f"/workspaces/{ws_id}/engineering/guided-questions")
    assert list_res.status_code == 200
    list_data = list_res.json()
    assert len(list_data["questions"]) == 1
    assert list_data["active_question_id"] == q_id


def test_guided_flow_workspace_isolation_and_validation(client: TestClient) -> None:
    ws1 = _create_workspace(client, "ws-iso-1")
    ws2 = _create_workspace(client, "ws-iso-2")

    # 404 on unknown workspace
    assert client.get("/workspaces/unknown-ws/engineering/guided-questions").status_code == 404
    assert (
        client.post(
            "/workspaces/unknown-ws/engineering/guided-questions",
            json={"title": "Test"},
        ).status_code
        == 404
    )
    assert (
        client.patch(
            "/workspaces/unknown-ws/engineering/guided-questions/q1",
            json={"title": "Test"},
        ).status_code
        == 404
    )

    # 422 on invalid POST payload
    assert (
        client.post(
            f"/workspaces/{ws1}/engineering/guided-questions",
            json={"title": ""},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/workspaces/{ws1}/engineering/guided-questions",
            json={"title": "   "},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/workspaces/{ws1}/engineering/guided-questions",
            json={},
        ).status_code
        == 422
    )

    # Create question in ws1
    post_res = client.post(
        f"/workspaces/{ws1}/engineering/guided-questions",
        json={"title": "Question in WS1"},
    )
    assert post_res.status_code == 201
    q1_id = post_res.json()["id"]

    # Cross-workspace PATCH gives 404
    cross_res = client.patch(
        f"/workspaces/{ws2}/engineering/guided-questions/{q1_id}",
        json={"title": "Mutated via WS2"},
    )
    assert cross_res.status_code == 404

    # 422 on invalid PATCH payload
    assert (
        client.patch(
            f"/workspaces/{ws1}/engineering/guided-questions/{q1_id}",
            json={"title": "   "},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/workspaces/{ws1}/engineering/guided-questions/{q1_id}",
            json={"status": "invalid_status"},
        ).status_code
        == 422
    )


def test_guided_flow_seven_steps_state_transitions(client: TestClient) -> None:
    ws_id = _create_workspace(client, "transitions-ws")
    now = utc_now()

    # Create question
    post_res = client.post(
        f"/workspaces/{ws_id}/engineering/guided-questions",
        json={"title": "Transizioni di stato"},
    )
    q_id = post_res.json()["id"]

    def get_steps() -> dict[str, dict]:
        res = client.get(f"/workspaces/{ws_id}/engineering/guided-questions")
        q = next(item for item in res.json()["questions"] if item["id"] == q_id)
        return {s["step"]: s for s in q["steps"]}

    steps = get_steps()
    # Initial states
    assert steps["frame"]["state"] == "in_progress"  # title is present, but no requirements
    assert steps["sources"]["state"] == "not_started"
    assert steps["model"]["state"] == "not_started"
    assert steps["run"]["state"] == "not_started"
    assert steps["explore"]["state"] == "not_started"
    assert steps["compare"]["state"] == "not_started"
    assert steps["decide"]["state"] == "not_started"

    # Step 1: frame -> done when requirements row exists
    with open_sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at)
            VALUES (?, ?, 'Pressione massima <= 10 bar', 'active', ?, ?)
            """,
            (str(uuid4()), ws_id, now, now),
        )
        conn.commit()

    steps = get_steps()
    assert steps["frame"]["state"] == "done"

    # Step 2: sources -> blocked when literature entry flagged
    src_id = str(uuid4())
    entry_id = str(uuid4())
    with open_sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO literature_sources (id, workspace_id, title, source_kind, state, created_at, updated_at)
            VALUES (?, ?, 'Perry Handbook', 'book', 'accepted', ?, ?)
            """,
            (src_id, ws_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO literature_entries (id, workspace_id, source_id, entry_kind, statement, ingestion_status, created_at, updated_at)
            VALUES (?, ?, ?, 'claim', 'Flagged citation', 'flagged', ?, ?)
            """,
            (entry_id, ws_id, src_id, now, now),
        )
        conn.commit()

    steps = get_steps()
    assert steps["sources"]["state"] == "blocked"
    assert "Flagged citation" in steps["sources"]["blocked_items"]

    # sources -> done when flagged entry is cleared
    with open_sqlite_connection() as conn:
        conn.execute(
            "UPDATE literature_entries SET ingestion_status = 'approved' WHERE id = ?",
            (entry_id,),
        )
        conn.commit()

    steps = get_steps()
    assert steps["sources"]["state"] == "done"

    # Step 3: model -> blocked when active parameter has no source_ref
    param_id = str(uuid4())
    with open_sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO parameters (id, workspace_id, name, value, lifecycle_state, source_ref, created_at, updated_at)
            VALUES (?, ?, 'pipe_diameter', '0.05', 'active', NULL, ?, ?)
            """,
            (param_id, ws_id, now, now),
        )
        conn.commit()

    steps = get_steps()
    assert steps["model"]["state"] == "blocked"
    assert "pipe_diameter" in steps["model"]["blocked_items"]

    # model -> done when source_ref bound and component/spec/draft exists
    spec_id = str(uuid4())
    with open_sqlite_connection() as conn:
        conn.execute(
            "UPDATE parameters SET source_ref = 'lit:123' WHERE id = ?",
            (param_id,),
        )
        conn.execute(
            """
            INSERT INTO model_specs (id, workspace_id, title, engineering_question, created_at, updated_at)
            VALUES (?, ?, 'Model A', 'Q?', ?, ?)
            """,
            (spec_id, ws_id, now, now),
        )
        conn.commit()

    steps = get_steps()
    assert steps["model"]["state"] == "done"

    # Step 4: run -> in_progress when simulation is running
    sim_id = str(uuid4())
    with open_sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO simulation_runs (id, workspace_id, model_version_id, status, created_at)
            VALUES (?, ?, NULL, 'running', ?)
            """,
            (sim_id, ws_id, now),
        )
        conn.commit()

    steps = get_steps()
    assert steps["run"]["state"] == "in_progress"

    # run -> blocked when simulation failed and no usable runs exist
    with open_sqlite_connection() as conn:
        conn.execute(
            "UPDATE simulation_runs SET status = 'failed' WHERE id = ?",
            (sim_id,),
        )
        conn.commit()

    steps = get_steps()
    assert steps["run"]["state"] == "blocked"

    # run -> done when simulation completed
    with open_sqlite_connection() as conn:
        conn.execute(
            "UPDATE simulation_runs SET status = 'completed' WHERE id = ?",
            (sim_id,),
        )
        conn.commit()

    steps = get_steps()
    assert steps["run"]["state"] == "done"

    # Step 5: explore -> done via skip reason
    client.patch(
        f"/workspaces/{ws_id}/engineering/guided-questions/{q_id}",
        json={"explore_skip_reason": "Single design case explored"},
    )
    steps = get_steps()
    assert steps["explore"]["state"] == "done"

    # Step 6: compare -> done when simulation_result_resolutions exists
    with open_sqlite_connection() as conn:
        batch_id = str(uuid4())
        val_id = str(uuid4())
        res_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO simulation_result_batches (id, workspace_id, run_id, source_payload_digest, extractor_version, created_at)
            VALUES (?, ?, ?, 'sha:1', 'v1', ?)
            """,
            (batch_id, ws_id, sim_id, now),
        )
        conn.execute(
            """
            INSERT INTO simulation_result_values (id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version, value_text, unit, conditions_json, status, created_at)
            VALUES (?, ?, 'flow_rate', 'sha:1', 'ext', 'v1', '10.5', 'm3/h', '{}', 'passed', ?)
            """,
            (val_id, batch_id, now),
        )
        conn.execute(
            """
            INSERT INTO simulation_result_resolutions (id, value_id, action, reason, actor, original_value_text, original_unit, original_conditions_json, created_at)
            VALUES (?, ?, 'confirm', 'Approved', 'operator', '10.5', 'm3/h', '{}', ?)
            """,
            (res_id, val_id, now),
        )
        conn.commit()

    steps = get_steps()
    assert steps["compare"]["state"] == "done"

    # Step 7: decide -> done when linked_decision_id names an accepted decision (spec 318)
    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT INTO decisions (id, workspace_id, title, decision_text, status, created_at, updated_at) "
            "VALUES ('dec-accepted-1', ?, 'Dec', 'Text', 'accepted', ?, ?)",
            (ws_id, now, now),
        )
        conn.commit()
    client.patch(
        f"/workspaces/{ws_id}/engineering/guided-questions/{q_id}",
        json={"linked_decision_id": "dec-accepted-1"},
    )
    steps = get_steps()
    assert steps["decide"]["state"] == "done"

    # When all 7 steps are done, next_action must be None
    res = client.get(f"/workspaces/{ws_id}/engineering/guided-questions")
    q = next(item for item in res.json()["questions"] if item["id"] == q_id)
    assert q["next_action"] is None


def test_deterministic_next_action_sequence_and_variants(client: TestClient) -> None:
    ws_id = _create_workspace(client, "seq-ws")
    now = utc_now()

    post_res = client.post(
        f"/workspaces/{ws_id}/engineering/guided-questions",
        json={"title": "Sequenza deterministica"},
    )
    q_id = post_res.json()["id"]

    def current_next_action() -> str | None:
        res = client.get(f"/workspaces/{ws_id}/engineering/guided-questions")
        q = next(item for item in res.json()["questions"] if item["id"] == q_id)
        return q["next_action"]["step"] if q["next_action"] else None

    # Step 1: frame
    assert current_next_action() == "frame"
    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) "
            "VALUES (?, ?, 'Req 1', 'active', ?, ?)",
            (str(uuid4()), ws_id, now, now),
        )
        conn.commit()

    # Step 2: sources
    assert current_next_action() == "sources"
    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT INTO literature_sources (id, workspace_id, title, source_kind, state, created_at, updated_at) "
            "VALUES (?, ?, 'Src 1', 'paper', 'accepted', ?, ?)",
            (str(uuid4()), ws_id, now, now),
        )
        conn.commit()

    # Step 3: model
    assert current_next_action() == "model"
    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT INTO model_specs (id, workspace_id, title, engineering_question, created_at, updated_at) "
            "VALUES (?, ?, 'Spec 1', 'Q?', ?, ?)",
            (str(uuid4()), ws_id, now, now),
        )
        conn.commit()

    # Step 4: run - test bench run with usable verdict
    assert current_next_action() == "run"
    bench_dir = build_paths().workspaces_dir / ws_id / "engineering" / "bench" / "bench-run-1"
    bench_dir.mkdir(parents=True, exist_ok=True)
    (bench_dir / "result.json").write_text(
        json.dumps({"run_id": "bench-run-1", "verdict": "usable", "completed_at": now}),
        encoding="utf-8",
    )

    # Step 5: explore - test via >1 design_project_versions
    assert current_next_action() == "explore"
    data_ws_id = str(uuid4())
    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT INTO workspaces (id, name, slug, status, created_at, updated_at) "
            "VALUES (?, 'Data WS', ?, 'active', ?, ?)",
            (data_ws_id, f"data-slug-{data_ws_id[:8]}", now, now),
        )
        conn.execute(
            "INSERT INTO design_project_versions (id, project_id, data_workspace_id, label, active, created_at) "
            "VALUES (?, ?, ?, 'v2', 0, ?)",
            (str(uuid4()), ws_id, data_ws_id, now),
        )
        conn.commit()

    # Step 6: compare - test via simulation_result_resolutions
    assert current_next_action() == "compare"
    batch_id = str(uuid4())
    val_id = str(uuid4())
    res_id = str(uuid4())
    sim_id = str(uuid4())
    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT INTO simulation_runs (id, workspace_id, model_version_id, status, created_at) "
            "VALUES (?, ?, NULL, 'completed', ?)",
            (sim_id, ws_id, now),
        )
        conn.execute(
            "INSERT INTO simulation_result_batches (id, workspace_id, run_id, source_payload_digest, extractor_version, created_at) "
            "VALUES (?, ?, ?, 'sha:1', 'v1', ?)",
            (batch_id, ws_id, sim_id, now),
        )
        conn.execute(
            "INSERT INTO simulation_result_values (id, batch_id, output_name, source_payload_digest, extractor_id, extractor_version, value_text, unit, conditions_json, status, created_at) "
            "VALUES (?, ?, 'output_val', 'sha:1', 'ext', 'v1', '5.0', 'bar', '{}', 'passed', ?)",
            (val_id, batch_id, now),
        )
        conn.execute(
            "INSERT INTO simulation_result_resolutions (id, value_id, action, reason, actor, original_value_text, original_unit, original_conditions_json, created_at) "
            "VALUES (?, ?, 'confirm', 'OK', 'user', '5.0', 'bar', '{}', ?)",
            (res_id, val_id, now),
        )
        conn.commit()

    # Step 7: decide - a draft decision does not count (spec 318), an accepted one does
    assert current_next_action() == "decide"
    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT INTO decisions (id, workspace_id, title, decision_text, status, created_at, updated_at) "
            "VALUES (?, ?, 'Draft', 'Text', 'draft', ?, ?)",
            (str(uuid4()), ws_id, now, now),
        )
        conn.commit()
    assert current_next_action() == "decide"
    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT INTO decisions (id, workspace_id, title, decision_text, status, created_at, updated_at) "
            "VALUES (?, ?, 'Dec 1', 'Text', 'accepted', ?, ?)",
            (str(uuid4()), ws_id, now, now),
        )
        conn.commit()

    # All 7 steps done -> next_action is None
    assert current_next_action() is None

