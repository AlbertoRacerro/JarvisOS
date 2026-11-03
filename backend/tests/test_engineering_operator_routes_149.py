from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.engineering.evaluator_contracts import (
    EvaluationResult,
    EvaluatorAvailability,
    EvaluatorDescriptor,
    NamedQuantity,
    NumericalDiagnostics,
)
from app.modules.engineering.refs import EvaluationResultRef, Quantity
from app.modules.process_stack.correlations import PIPE_EVALUATOR_ID


class GeometryNamedPipeEvaluator:
    def descriptor(self):
        return EvaluatorDescriptor(evaluator_id=PIPE_EVALUATOR_ID, backend_kind="specialist", backend_name="fake pipe",
                                   backend_version="test-1", fidelity="screening")

    def availability(self):
        return EvaluatorAvailability(evaluator_id=PIPE_EVALUATOR_ID, state="available", checked_at=datetime.now(UTC),
                                     backend_version="test-1")

    def evaluate(self, request):
        now = datetime.now(UTC)
        return EvaluationResult(
            result_ref=EvaluationResultRef(authority_owner="test", object_id=request.request_ref.object_id,
                                           workspace_id=request.request_ref.workspace_id, revision="1"),
            request_ref=request.request_ref, evaluator_id=PIPE_EVALUATOR_ID, backend_version="test-1",
            status="succeeded", fidelity="screening",
            outputs=(NamedQuantity(name="pressure_drop", value=Quantity(value=1.0, unit="Pa")),),
            numerical=NumericalDiagnostics(converged=True), started_at=now, completed_at=now,
        )


def _scope(monkeypatch, tmp_path: Path):
    paths = SimpleNamespace(data_root=tmp_path, workspaces_dir=tmp_path / "workspaces",
                            retrieval_index_file=tmp_path / "retrieval.sqlite3")
    paths.workspaces_dir.mkdir()
    monkeypatch.setattr("app.modules.engineering.operator_service.build_paths", lambda: paths)
    monkeypatch.setattr(
        "app.modules.engineering.operator_service.get_workspace",
        lambda workspace_id: object() if workspace_id == "ws-a" else None,
    )
    return paths.workspaces_dir


def _definition(workspace_id="ws-a", budget=3, study_id="pipe-study"):
    return {
        "study_ref": {"authority_owner": "engineering", "object_type": "study", "object_id": study_id,
                      "workspace_id": workspace_id, "revision": "1"},
        "evaluator_id": PIPE_EVALUATOR_ID,
        "subject_ref": {"authority_owner": "engineering", "object_type": "physics_case", "object_id": "pipe",
                        "workspace_id": workspace_id, "revision": "1"},
        "method": "grid",
        "variables": [
            {"name": "tube_inner_diameter", "domain": {"variable": "tube_inner_diameter",
             "lower": {"value": 0.01, "unit": "m"}, "upper": {"value": 0.02, "unit": "m"}},
             "step": {"value": 0.01, "unit": "m"}},
            {"name": "loop_length", "domain": {"variable": "loop_length",
             "lower": {"value": 0.2, "unit": "m"}, "upper": {"value": 0.2, "unit": "m"}},
             "step": {"value": 0.1, "unit": "m"}},
        ],
        "fixed_inputs": [
            {"name": "density", "value": {"value": 1000, "unit": "kg/m3"}},
            {"name": "dynamic_viscosity", "value": {"value": 0.001, "unit": "Pa*s"}},
            {"name": "velocity", "value": {"value": 0.01, "unit": "m/s"}},
            {"name": "roughness", "value": {"value": 0, "unit": "m"}},
        ],
        "objectives": [{"output": "pressure_drop", "sense": "minimize"}],
        "seed": 1, "budget": budget, "sample_count": 3,
    }


def test_operator_routes_scope_budget_and_write_once_persistence(monkeypatch, tmp_path):
    workspace_root = _scope(monkeypatch, tmp_path)
    app = create_app()
    app.state.engineering_evaluator_registry = {PIPE_EVALUATOR_ID: GeometryNamedPipeEvaluator()}
    with TestClient(app) as client:
        unknown = client.get("/workspaces/missing/engineering/evaluators")
        assert unknown.status_code == 404
        mismatch = client.post("/workspaces/ws-a/engineering/studies", json=_definition("other"))
        assert mismatch.status_code == 404
        over_budget = client.post("/workspaces/ws-a/engineering/studies", json=_definition(budget=17))
        assert over_budget.status_code == 422
        traversal = client.post("/workspaces/ws-a/engineering/studies", json=_definition(study_id=".."))
        assert traversal.status_code == 422
        response = client.post("/workspaces/ws-a/engineering/studies", json=_definition())
        assert response.status_code == 200, response.text
        run = response.json()
        assert run["qualification_status"] == "unqualified"
        assert len(run["points"]) == 2
        file_digest = run["content_digest"].replace(":", "-")
        run_path = workspace_root / "ws-a" / "engineering" / "studies" / "pipe-study" / "runs" / f"{file_digest}.json"
        assert run_path.is_file()
        listed = client.get(f"/workspaces/ws-a/engineering/studies/pipe-study/runs/{run['content_digest']}")
        assert listed.status_code == 200
        assert listed.json() == run
        assert run_path.resolve().is_relative_to(workspace_root.resolve())
        envelope = client.post(
            f"/workspaces/ws-a/engineering/studies/pipe-study/runs/{run['content_digest']}/envelope",
            params={"point_index": run["best_point_index"]},
        )
        assert envelope.status_code == 200, envelope.text
        escalated = client.post(
            f"/workspaces/ws-a/engineering/studies/pipe-study/runs/{run['content_digest']}/escalations",
            json={"policy": {"operator_requested_points": [run["best_point_index"]]}, "evaluator_ids": []},
        )
        assert escalated.status_code == 200, escalated.text
        assert escalated.json()["points"][0]["status"] == "escalation_unavailable"
        assert escalated.json()["base_run_digest"] == run["content_digest"]

        infeasible_definition = _definition(study_id="infeasible-study")
        infeasible_definition["constraints"] = [{"output": "pressure_drop", "operator": "le",
                                                   "bound": {"value": 0, "unit": "Pa"}}]
        infeasible = client.post("/workspaces/ws-a/engineering/studies", json=infeasible_definition)
        assert infeasible.status_code == 200, infeasible.text
        refusal = client.post(
            f"/workspaces/ws-a/engineering/studies/infeasible-study/runs/{infeasible.json()['content_digest']}/envelope",
            params={"point_index": 0},
        )
        assert refusal.status_code == 422
        assert refusal.json()["detail"]["code"] == "envelope_point_invalid"
        capabilities = client.get("/workspaces/ws-a/engineering/capabilities")
        assert capabilities.status_code == 200
        states = {item["capability_id"]: item["state"] for item in capabilities.json()}
        assert states["retrieval"] == "not_configured"
        assert states["dwsim"] == "not_configured"


def test_operator_routes_reject_unknown_escalators_and_use_portable_names(monkeypatch, tmp_path):
    workspace_root = _scope(monkeypatch, tmp_path)
    app = create_app()
    app.state.engineering_evaluator_registry = {PIPE_EVALUATOR_ID: GeometryNamedPipeEvaluator()}
    with TestClient(app) as client:
        run = client.post("/workspaces/ws-a/engineering/studies", json=_definition()).json()
        study_root = workspace_root / "ws-a" / "engineering" / "studies" / "pipe-study"
        assert all(":" not in path.name for path in study_root.rglob("*"))
        base = f"/workspaces/ws-a/engineering/studies/pipe-study/runs/{run['content_digest']}/escalations"
        unknown = client.post(base, json={"policy": {"operator_requested_points": [0]}, "evaluator_ids": ["ghost"]})
        assert unknown.status_code == 404
        assert unknown.json()["detail"] == {"code": "evaluator_not_found", "evaluator_ids": ["ghost"]}
        malformed = client.post(base, json={"policy": {"operator_requested_points": "all"}, "evaluator_ids": []})
        assert malformed.status_code == 422
        assert client.post(base, json={"operator_requested_points": [0]}).status_code == 422

