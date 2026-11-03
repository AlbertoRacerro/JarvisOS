from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import WORKSPACE_NOT_FOUND_MESSAGE, workspace_not_found_http_error
from app.modules.modeling import routes as modeling_routes
from app.modules.workspaces import routes as workspaces_routes

EXPECTED_WORKSPACE_NOT_FOUND = {
    "detail": {
        "code": "workspace_not_found",
        "message": "Workspace not found.",
    }
}


def test_workspace_not_found_helper_is_canonical() -> None:
    error = workspace_not_found_http_error()

    assert error.status_code == 404
    assert error.detail == EXPECTED_WORKSPACE_NOT_FOUND["detail"]


def test_workspaces_get_missing_workspace_uses_canonical_contract(monkeypatch) -> None:
    monkeypatch.setattr(workspaces_routes, "get_workspace", lambda workspace_id: None)
    app = FastAPI()
    app.include_router(workspaces_routes.router)

    response = TestClient(app).get("/workspaces/missing")

    assert response.status_code == 404
    assert response.json() == EXPECTED_WORKSPACE_NOT_FOUND


def test_modeling_missing_workspace_uses_canonical_contract(monkeypatch) -> None:
    def _missing_workspace(workspace_id: str):
        raise ValueError(WORKSPACE_NOT_FOUND_MESSAGE)

    monkeypatch.setattr(modeling_routes, "list_model_specs", _missing_workspace)
    app = FastAPI()
    app.include_router(modeling_routes.router)

    response = TestClient(app).get("/workspaces/missing/model-specs")

    assert response.status_code == 404
    assert response.json() == EXPECTED_WORKSPACE_NOT_FOUND


def test_modeling_unrelated_value_error_keeps_plain_detail(monkeypatch) -> None:
    def _other_failure(workspace_id: str):
        raise ValueError("Different modeling failure.")

    monkeypatch.setattr(modeling_routes, "list_model_specs", _other_failure)
    app = FastAPI()
    app.include_router(modeling_routes.router)

    response = TestClient(app).get("/workspaces/example/model-specs")

    assert response.status_code == 404
    assert response.json() == {"detail": "Different modeling failure."}
