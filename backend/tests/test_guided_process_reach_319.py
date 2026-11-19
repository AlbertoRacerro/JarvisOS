"""Offline acceptance tests for spec 319 guided process reach."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.main import create_app
from app.modules.engineering.guided_flow import (
    GuidedStepEvaluation,
    compute_next_action,
    evaluate_step,
)
from app.modules.process_stack import draft
from app.modules.process_stack.draft_models import AddUnit


def _workspace(client: TestClient, slug: str) -> str:
    response = client.post("/workspaces", json={"name": slug, "slug": slug})
    assert response.status_code == 201
    return response.json()["id"]


def _client(isolated_data_root: None) -> TestClient:
    initialize_storage(seed_default=True)
    return TestClient(create_app())


def test_model_done_for_process_draft_with_unit(isolated_data_root: None) -> None:
    with _client(isolated_data_root) as client:
        workspace_id = _workspace(client, "reach-unit")
        state = draft.create_draft(workspace_id, "Linea 1")
        draft.patch(
            workspace_id,
            state["draft_id"],
            state["revision"],
            [AddUnit(op="add_unit", id="pump", type="Pump", tag="Pompa", x=0, y=0)],
        )
        with open_sqlite_connection() as connection:
            assert evaluate_step(connection, workspace_id, "model") == ("done", [])


def test_empty_process_draft_keeps_model_not_started_and_routes_to_process(
    isolated_data_root: None,
) -> None:
    with _client(isolated_data_root) as client:
        workspace_id = _workspace(client, "reach-empty")
        draft.create_draft(workspace_id, "Linea vuota")
        with open_sqlite_connection() as connection:
            state, blocked_items = evaluate_step(connection, workspace_id, "model")
        assert (state, blocked_items) == ("not_started", [])
        action = compute_next_action(
            [
                GuidedStepEvaluation(
                    step="model",
                    order=3,
                    label_it="Modello",
                    route="/design/project",
                    description_it="",
                    state=state,
                    blocked_items=blocked_items,
                )
            ]
        )
        assert action is not None
        assert action.route == "/design/process"
        assert action.action_label_it == "Apri il diagramma di processo"


def test_converged_process_run_makes_run_done(isolated_data_root: None) -> None:
    with _client(isolated_data_root) as client:
        workspace_id = _workspace(client, "reach-converged")
        state = draft.create_draft(workspace_id, "Linea 2")
        draft.record_run(
            draft.draft_dir(workspace_id, state["draft_id"]),
            {
                "run_id": "converged",
                "draft_id": state["draft_id"],
                "draft_revision": state["revision"],
                "action": "run",
                "status": "completed",
                "started_at": "2026-01-01T00:00:00+00:00",
                "outcome": {"state": "converged"},
            },
        )
        with open_sqlite_connection() as connection:
            assert evaluate_step(connection, workspace_id, "run") == ("done", [])


def test_non_converged_latest_process_run_blocks_and_routes_to_process(
    isolated_data_root: None,
) -> None:
    with _client(isolated_data_root) as client:
        workspace_id = _workspace(client, "reach-blocked")
        state = draft.create_draft(workspace_id, "Linea 3")
        draft.record_run(
            draft.draft_dir(workspace_id, state["draft_id"]),
            {
                "run_id": "non-converged",
                "draft_id": state["draft_id"],
                "draft_revision": state["revision"],
                "action": "run",
                "status": "completed",
                "started_at": "2026-01-01T00:00:00+00:00",
                "outcome": {"state": "non_converged"},
            },
        )
        with open_sqlite_connection() as connection:
            state_value, blocked_items = evaluate_step(connection, workspace_id, "run")
        assert state_value == "blocked"
        assert blocked_items == ['Diagramma "Linea 3": non converge']
        action = compute_next_action(
            [
                GuidedStepEvaluation(
                    step="run",
                    order=4,
                    label_it="Calcolo",
                    route="/runs",
                    description_it="",
                    state=state_value,
                    blocked_items=blocked_items,
                )
            ]
        )
        assert action is not None
        assert action.route == "/design/process"


def test_workspace_without_process_drafts_keeps_existing_states(isolated_data_root: None) -> None:
    with _client(isolated_data_root) as client:
        workspace_id = _workspace(client, "reach-none")
        with open_sqlite_connection() as connection:
            assert evaluate_step(connection, workspace_id, "model") == ("not_started", [])
            assert evaluate_step(connection, workspace_id, "run") == ("not_started", [])


def test_guided_questions_response_with_process_drafts_is_path_free(isolated_data_root: None) -> None:
    with _client(isolated_data_root) as client:
        workspace_id = _workspace(client, "reach-paths")
        state = draft.create_draft(workspace_id, "Linea 4")
        draft.record_run(
            draft.draft_dir(workspace_id, state["draft_id"]),
            {
                "run_id": "failed",
                "draft_id": state["draft_id"],
                "draft_revision": state["revision"],
                "action": "run",
                "status": "failed",
                "started_at": "2026-01-01T00:00:00+00:00",
                "outcome": {"state": "failed"},
            },
        )
        response = client.get(f"/workspaces/{workspace_id}/engineering/guided-questions")
        assert response.status_code == 200
        run_step = next(step for step in response.json()["steps"] if step["step"] == "run")
        assert run_step["state"] == "blocked"
        assert run_step["blocked_items"] == ['Diagramma "Linea 4": fallito']
        assert os.environ["JARVISOS_DATA_ROOT"] not in response.text
