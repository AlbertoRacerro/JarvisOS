import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.spa_static import SpaStaticFiles, _safe_extensionless_path, derive_reserved_roots
from app.main import _SPA_RESERVED_ROOT_CLIENT_ROUTES, create_app

INDEX_MARKER = "jarvisos-spa-index-marker"


def _build_client(tmp_path: Path) -> TestClient:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(f"<html><body>{INDEX_MARKER}</body></html>", encoding="utf-8")
    (tmp_path / "assets" / "app.js").write_text("console.log('ok')", encoding="utf-8")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/value")
    def api_value() -> dict[str, int]:
        return {"value": 1}

    @app.get("/memory/api-value")
    def memory_api_value() -> dict[str, int]:
        return {"memory": 1}

    @app.get("/coding/api-value")
    def coding_api_value() -> dict[str, int]:
        return {"coding": 1}

    reserved_roots = derive_reserved_roots(app.routes) | frozenset({"settings"})
    app.mount(
        "/",
        SpaStaticFiles(
            directory=tmp_path,
            reserved_roots=reserved_roots,
            reserved_root_client_routes=_SPA_RESERVED_ROOT_CLIENT_ROUTES,
        ),
        name="frontend",
    )
    return TestClient(app, raise_server_exceptions=False)


def test_existing_asset_and_root_are_served_normally(tmp_path: Path) -> None:
    client = _build_client(tmp_path)

    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert asset.text == "console.log('ok')"
    assert "javascript" in asset.headers["content-type"]

    root = client.get("/", headers={"accept": "text/html"})
    assert root.status_code == 200
    assert INDEX_MARKER in root.text
    assert "text/html" in root.headers["content-type"]


def test_html_navigation_routes_fall_back_to_index(tmp_path: Path) -> None:
    client = _build_client(tmp_path)

    for path in ("/home", "/design/model", "/design/flowsheet", "/unknown-client-route"):
        response = client.get(path, headers={"accept": "text/html,application/xhtml+xml"})
        assert response.status_code == 200
        assert INDEX_MARKER in response.text
        assert "text/html" in response.headers["content-type"]

    accepted_values = (
        "text/html;q=0.001",
        "text/html;q=1",
        "text/html;q=1.000",
        'text/html;level="1,2";q=0.5',
        'text/html;level="1;2";q=1',
    )
    for accept in accepted_values:
        response = client.get("/home", headers={"accept": accept})
        assert response.status_code == 200
        assert INDEX_MARKER in response.text

    repeated_accept = client.get(
        "/home",
        headers=[("accept", "application/json"), ("accept", "text/html")],
    )
    assert repeated_accept.status_code == 200
    assert INDEX_MARKER in repeated_accept.text

    head = client.head("/design/model", headers={"accept": "text/html"})
    assert head.status_code == 200
    assert head.content == b""


def test_exact_reserved_root_client_routes_fall_back_without_broadening(tmp_path: Path) -> None:
    client = _build_client(tmp_path)

    for path in ("/memory/models", "/settings/ai", "/coding/repository", "/coding/runtime"):
        response = client.get(path, headers={"accept": "text/html"})
        assert response.status_code == 200
        assert INDEX_MARKER in response.text
        assert "text/html" in response.headers["content-type"]

    head = client.head("/memory/models", headers={"accept": "text/html"})
    assert head.status_code == 200
    assert head.content == b""

    for path in (
        "/memory/unknown",
        "/memory/models/unknown",
        "/coding/unknown",
        "/settings/unknown",
        "/memory//models",
    ):
        response = client.get(path, headers={"accept": "text/html"})
        assert response.status_code == 404
        assert INDEX_MARKER not in response.text

    non_html = client.get("/memory/models", headers={"accept": "application/json"})
    assert non_html.status_code == 404
    assert INDEX_MARKER not in non_html.text

    for method in ("post", "put", "delete"):
        response = getattr(client, method)("/memory/models", headers={"accept": "text/html"})
        assert response.status_code in {404, 405}
        assert INDEX_MARKER not in response.text


def test_invalid_exact_client_route_exception_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(INDEX_MARKER, encoding="utf-8")

    for path in ("memory/models", "/", "/memory//models", "/memory/../models", "/assets/app.js"):
        with pytest.raises(ValueError):
            SpaStaticFiles(
                directory=tmp_path,
                reserved_roots={"memory"},
                reserved_root_client_routes={path},
            )


def test_missing_assets_and_non_html_requests_remain_404(tmp_path: Path) -> None:
    client = _build_client(tmp_path)

    for path in ("/assets/missing.js", "/assets/missing"):
        missing_asset = client.get(path, headers={"accept": "text/html"})
        assert missing_asset.status_code == 404
        assert INDEX_MARKER not in missing_asset.text

    rejected_accept_values = (
        "application/json",
        "text/html;q",
        "text/html;Q",
        "text/html;q=0",
        "text/html;q=0.000",
        "text/html;q=invalid",
        "text/html;q=2",
        "text/html;q=Infinity",
        "text/html;q=1.0001",
        "text/html;q=-1",
        "text/html;q=.5",
        "text/html;q=0;q=1",
        "text/html;q=1;q=0.5",
        'text/html;level="1,2";q=0',
        'text/html;level="1;2";q=0',
        'text/html;level="1\\\",2";q=0',
        'text/html;level="1,2;q=1',
        'text/html;level="a"junk',
        'text/html;level="a""b";q=1',
        'text/html;level=a"b";q=1',
    )
    for accept in rejected_accept_values:
        response = client.get("/home", headers={"accept": accept})
        assert response.status_code == 404
        assert INDEX_MARKER not in response.text

    repeated_rejected = client.get(
        "/home",
        headers=[("accept", "text/html;q=0"), ("accept", "application/json")],
    )
    assert repeated_rejected.status_code == 404
    assert INDEX_MARKER not in repeated_rejected.text


def test_registered_and_unknown_api_paths_never_receive_index(tmp_path: Path) -> None:
    client = _build_client(tmp_path)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert "application/json" in health.headers["content-type"]

    api_value = client.get("/api/value")
    assert api_value.status_code == 200
    assert api_value.json() == {"value": 1}

    memory_api = client.get("/memory/api-value", headers={"accept": "text/html"})
    assert memory_api.status_code == 200
    assert memory_api.json() == {"memory": 1}
    assert "application/json" in memory_api.headers["content-type"]

    coding_api = client.get("/coding/api-value", headers={"accept": "text/html"})
    assert coding_api.status_code == 200
    assert coding_api.json() == {"coding": 1}
    assert "application/json" in coding_api.headers["content-type"]

    for path in ("/health/missing", "/api/missing", "/memory/missing", "/coding/missing"):
        response = client.get(path, headers={"accept": "text/html"})
        assert response.status_code == 404
        assert INDEX_MARKER not in response.text


def test_non_get_methods_do_not_receive_index(tmp_path: Path) -> None:
    client = _build_client(tmp_path)

    for method in ("post", "put", "delete"):
        response = getattr(client, method)("/home", headers={"accept": "text/html"})
        assert response.status_code in {404, 405}
        assert INDEX_MARKER not in response.text


def test_reserved_root_derivation_is_literal_and_ignores_mounts(tmp_path: Path) -> None:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/api/items/{item_id}")
    def item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    @app.get("/{tenant}/probe")
    def tenant_probe(tenant: str) -> dict[str, str]:
        return {"tenant": tenant}

    before_mount = derive_reserved_roots(app.routes)
    assert before_mount == frozenset({"api"})

    (tmp_path / "index.html").write_text(INDEX_MARKER, encoding="utf-8")
    app.mount("/", SpaStaticFiles(directory=tmp_path, reserved_roots=before_mount), name="frontend")
    assert derive_reserved_roots(app.routes) == before_mount


def test_encoded_traversal_does_not_receive_index(tmp_path: Path) -> None:
    client = _build_client(tmp_path)

    for path in ("/foo/%2e%2e/home", "/foo/%2E%2E/home", "/foo%5c..%5chome"):
        response = client.get(path, headers={"accept": "text/html"})
        assert response.status_code == 404
        assert INDEX_MARKER not in response.text


def test_fallback_index_cannot_escape_static_root_via_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-index.html"
    outside.write_text("external-secret", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    try:
        os.symlink(outside, tmp_path / "index.html")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/", SpaStaticFiles(directory=tmp_path, reserved_roots=frozenset()), name="frontend")
    response = TestClient(app, raise_server_exceptions=False).get(
        "/home", headers={"accept": "text/html"}
    )

    assert response.status_code == 404
    assert "external-secret" not in response.text


def test_traversal_and_malformed_paths_are_not_fallback_candidates() -> None:
    assert not _safe_extensionless_path("../secret")
    assert not _safe_extensionless_path("folder/../../secret")
    assert not _safe_extensionless_path(r"folder\..\secret")
    assert not _safe_extensionless_path("folder/secret\x00")
    assert not _safe_extensionless_path("")
    assert not _safe_extensionless_path("assets/app.js")
    assert _safe_extensionless_path("design/model")


def test_create_app_without_frontend_build_has_no_frontend_mount(monkeypatch, tmp_path: Path) -> None:
    missing_dist = tmp_path / "missing-dist"
    monkeypatch.setattr("app.main._frontend_dist_path", lambda: missing_dist)

    application = create_app()

    assert all(getattr(route, "name", None) != "frontend" for route in application.routes)


def test_create_app_wires_only_the_bounded_exact_client_routes(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(INDEX_MARKER, encoding="utf-8")
    monkeypatch.setattr("app.main._frontend_dist_path", lambda: tmp_path)

    application = create_app()
    frontend_mount = next(route for route in application.routes if getattr(route, "name", None) == "frontend")

    assert frontend_mount.app._reserved_root_client_routes == _SPA_RESERVED_ROOT_CLIENT_ROUTES
