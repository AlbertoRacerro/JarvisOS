from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.ai.sensitivity_routes import router as sensitivity_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(sensitivity_router)
    return TestClient(app)


def test_label_route_rejects_malformed_subject_ref_as_422() -> None:
    with _client() as client:
        response = client.post(
            "/ai/sensitivity/labels",
            json={
                "workspace_id": "main",
                "subject_ref": "malformed",
                "level": "S1",
            },
        )

    assert response.status_code == 422


def test_derivative_route_rejects_malformed_source_ref_as_422() -> None:
    with _client() as client:
        response = client.post(
            "/ai/sensitivity/derivatives",
            json={
                "workspace_id": "main",
                "source_refs": ["malformed"],
                "content": "Generic sanitized summary.",
                "effective_level": "S1",
                "transformations": ["Removed project-specific details"],
            },
        )

    assert response.status_code == 422


def test_preview_route_rejects_unsupported_kind_as_422() -> None:
    with _client() as client:
        response = client.post(
            "/ai/sensitivity/context-preview",
            json={
                "workspace_id": "main",
                "selection": {"kinds": ["unsupported"]},
            },
        )

    assert response.status_code == 422
