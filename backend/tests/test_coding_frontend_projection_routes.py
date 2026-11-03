from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.coding import runtime_routes
from app.modules.coding.repository_truth import RepositoryTruthError, RepositoryTruthResult

REPOSITORY = "AlbertoRacerro/JarvisOS"
SHA = "a" * 40


def _result(operation: str, *, payload: dict[str, object] | None = None) -> RepositoryTruthResult:
    return RepositoryTruthResult(
        provider="github",
        repository=REPOSITORY,
        operation=operation,
        requested_ref="master",
        resolved_sha=SHA,
        partial=False,
        payload=payload or {"operation": operation},
        observed_at="2026-09-05T00:00:00+00:00",
    )


class FakeRepositoryTruthService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _record(self, name: str, *args: object, **kwargs: object) -> RepositoryTruthResult:
        self.calls.append((name, args, kwargs))
        payload: dict[str, object] = {"operation": name}
        if name == "safe_github_url":
            payload["url"] = f"https://github.com/{REPOSITORY}/blob/{SHA}/README.md"
        if name == "pull_request_truth":
            payload["head_sha"] = SHA
        if name == "file_preview":
            payload["text"] = "bounded file evidence"
        return _result(name, payload=payload)

    def repository_ref_truth(self, repository: str, ref: str) -> RepositoryTruthResult:
        return self._record("repository_ref_truth", repository, ref)

    def path_list(self, repository: str, ref: str, path: str) -> RepositoryTruthResult:
        return self._record("path_list", repository, ref, path)

    def file_preview(self, repository: str, ref: str, path: str) -> RepositoryTruthResult:
        return self._record("file_preview", repository, ref, path)

    def literal_search(self, repository: str, ref: str, literal: str) -> RepositoryTruthResult:
        return self._record("literal_search", repository, ref, literal)

    def pull_request_truth(self, repository: str, pr_number: int) -> RepositoryTruthResult:
        return self._record("pull_request_truth", repository, pr_number)

    def check_truth(
        self,
        repository: str,
        pr_number: int,
        *,
        expected_head_sha: str,
    ) -> RepositoryTruthResult:
        return self._record(
            "check_truth",
            repository,
            pr_number,
            expected_head_sha=expected_head_sha,
        )

    def review_truth(
        self,
        repository: str,
        pr_number: int,
        *,
        expected_head_sha: str,
    ) -> RepositoryTruthResult:
        return self._record(
            "review_truth",
            repository,
            pr_number,
            expected_head_sha=expected_head_sha,
        )

    def safe_github_url(
        self,
        repository: str,
        *,
        commit_sha: str | None = None,
        pr_number: int | None = None,
        path: str | None = None,
    ) -> RepositoryTruthResult:
        return self._record(
            "safe_github_url",
            repository,
            commit_sha=commit_sha,
            pr_number=pr_number,
            path=path,
        )


def _client(monkeypatch) -> tuple[TestClient, FakeRepositoryTruthService]:
    service = FakeRepositoryTruthService()
    monkeypatch.setattr(runtime_routes, "_repository_service", lambda: service)
    app = FastAPI()
    app.include_router(runtime_routes.router)
    return TestClient(app), service


def test_repository_projection_routes_delegate_exact_inputs(monkeypatch) -> None:
    client, service = _client(monkeypatch)
    with client:
        cases = [
            ("/api/coding/repository/ref", {"repository": REPOSITORY, "ref": "master"}, "repository_ref_truth"),
            ("/api/coding/repository/tree", {"repository": REPOSITORY, "ref": "master", "path": "backend"}, "path_list"),
            ("/api/coding/repository/file", {"repository": REPOSITORY, "ref": "master", "path": "README.md"}, "file_preview"),
            ("/api/coding/repository/search", {"repository": REPOSITORY, "ref": "master", "literal": "JarvisOS"}, "literal_search"),
            ("/api/coding/repository/pull-request", {"repository": REPOSITORY, "pr_number": 552}, "pull_request_truth"),
        ]
        for route, params, operation in cases:
            response = client.get(route, params=params)
            assert response.status_code == 200
            payload = response.json()
            assert payload["repository"] == REPOSITORY
            assert payload["resolved_sha"] == SHA
            assert payload["payload"]["operation"] == operation

        checks = client.get(
            "/api/coding/repository/checks",
            params={"repository": REPOSITORY, "pr_number": 552, "expected_head_sha": SHA},
        )
        reviews = client.get(
            "/api/coding/repository/reviews",
            params={"repository": REPOSITORY, "pr_number": 552, "expected_head_sha": SHA},
        )
        navigation = client.get(
            "/api/coding/repository/url",
            params={"repository": REPOSITORY, "commit_sha": SHA, "path": "README.md"},
        )

    assert checks.status_code == 200
    assert reviews.status_code == 200
    assert navigation.status_code == 200
    assert navigation.json()["payload"]["url"].startswith("https://github.com/")
    assert ("check_truth", (REPOSITORY, 552), {"expected_head_sha": SHA}) in service.calls
    assert ("review_truth", (REPOSITORY, 552), {"expected_head_sha": SHA}) in service.calls
    assert (
        "safe_github_url",
        (REPOSITORY,),
        {"commit_sha": SHA, "pr_number": None, "path": "README.md"},
    ) in service.calls


def test_coding_context_preview_binds_exact_file_evidence(monkeypatch) -> None:
    client, service = _client(monkeypatch)
    payload = {
        "workspace_id": "ws-140",
        "repository": REPOSITORY,
        "base_ref": "master",
        "base_sha": SHA,
        "target_paths": ["README.md"],
    }
    with client:
        response = client.post("/api/coding/actions/context-preview", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "current"
    assert body["base_sha"] == SHA
    assert body["target_paths"] == ["README.md"]
    assert body["context_digest"].startswith("sha256:")
    assert body["added_context_refs"] == [
        {
            "workspace_id": "ws-140",
            "owner": "coding",
            "kind": "repository-file",
            "id": "README.md",
            "version": SHA,
        }
    ]
    assert ("file_preview", (REPOSITORY, SHA, "README.md"), {}) in service.calls


def test_coding_context_preview_refuses_budget_dropped_requested_evidence(monkeypatch) -> None:
    client, _service = _client(monkeypatch)
    monkeypatch.setattr(
        runtime_routes,
        "build_jarvis_context_preview",
        lambda *_args, **_kwargs: SimpleNamespace(
            dispatchable=True,
            context_sources_manifest=[],
            context_digest="sha256:dropped",
        ),
    )
    payload = {
        "workspace_id": "ws-140",
        "repository": REPOSITORY,
        "base_ref": "master",
        "base_sha": SHA,
        "target_paths": ["README.md"],
    }
    with client:
        response = client.post("/api/coding/actions/context-preview", json=payload)

    assert response.status_code == 200
    assert response.json() == {"state": "refused", "reason": "missing_evidence"}


def test_coding_context_preview_refuses_oversized_adapter_evidence(monkeypatch) -> None:
    client, _service = _client(monkeypatch)

    def _oversized(*_args, **_kwargs):
        raise runtime_routes.JarvisContextError("context adapter returned oversized preview evidence")

    monkeypatch.setattr(runtime_routes, "build_jarvis_context_preview", _oversized)
    payload = {
        "workspace_id": "ws-140",
        "repository": REPOSITORY,
        "base_ref": "master",
        "base_sha": SHA,
        "target_paths": ["README.md"],
    }
    with client:
        response = client.post("/api/coding/actions/context-preview", json=payload)

    assert response.status_code == 200
    assert response.json() == {"state": "refused", "reason": "missing_evidence"}


def test_repository_projection_routes_preserve_fail_closed_error_classes(monkeypatch) -> None:
    class FailingService(FakeRepositoryTruthService):
        def repository_ref_truth(self, repository: str, ref: str) -> RepositoryTruthResult:
            raise RepositoryTruthError(
                "authentication_required",
                "credential unavailable",
                metadata={"operation": "repository_ref_truth"},
            )

        def file_preview(self, repository: str, ref: str, path: str) -> RepositoryTruthResult:
            raise RepositoryTruthError(
                "provider_unavailable",
                "provider unavailable",
                partial=True,
                metadata={"operation": "file_preview"},
            )

    service = FailingService()
    monkeypatch.setattr(runtime_routes, "_repository_service", lambda: service)
    app = FastAPI()
    app.include_router(runtime_routes.router)

    with TestClient(app) as client:
        authentication = client.get(
            "/api/coding/repository/ref",
            params={"repository": REPOSITORY, "ref": "master"},
        )
        unavailable = client.get(
            "/api/coding/repository/file",
            params={"repository": REPOSITORY, "ref": "master", "path": "README.md"},
        )

    assert authentication.status_code == 401
    assert authentication.json()["detail"] == {
        "code": "authentication_required",
        "partial": False,
        "metadata": {"operation": "repository_ref_truth"},
    }
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == {
        "code": "provider_unavailable",
        "partial": True,
        "metadata": {"operation": "file_preview"},
    }
