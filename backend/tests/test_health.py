from fastapi.testclient import TestClient

from app.main import create_app


def test_backend_app_imports() -> None:
    app = create_app()
    assert app.title == "JarvisOS"


def test_health_returns_success() -> None:
    client = TestClient(create_app())
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app_name"] == "JarvisOS"
    assert body["data_root"]


def test_system_info_returns_success() -> None:
    client = TestClient(create_app())
    response = client.get("/system/info")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"]["engine"] == "sqlite"
    assert body["database"]["configured"] is True
    assert body["ai"]["gateway_configured"] is True
    assert body["ai"]["provider_calls_enabled"] is False
    assert "data_root_exists" in body
