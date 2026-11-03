"""Optional read-only GitHub token seam for private-repository reads (spec 118, amended 2026-10-30)."""

from __future__ import annotations

import http.client
import json
import logging
from pathlib import Path

import pytest

from app.modules.coding import repository_truth as rt

TOKEN = "github_pat_" + "A1b2C3d4E5" * 4
TOKEN_ENV = "JARVISOS_GITHUB_READ_TOKEN"


class _Response:
    status = 200

    def read(self, _limit: int) -> bytes:
        return b"{}"

    def getheaders(self) -> list[tuple[str, str]]:
        return [("X-Test", "1")]


class _Connection:
    created: list[_Connection] = []

    def __init__(self, host: str, timeout: float | None = None) -> None:
        self.host = host
        self.sock = None
        self.headers: dict[str, str] = {}
        _Connection.created.append(self)

    def request(self, method: str, path: str, headers: dict[str, str] | None = None) -> None:
        self.method, self.path, self.headers = method, path, dict(headers or {})

    def getresponse(self) -> _Response:
        return _Response()

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def fake_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    _Connection.created = []
    monkeypatch.setattr(http.client, "HTTPSConnection", _Connection)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)


def test_default_is_unauthenticated_public_read() -> None:
    rt._github_request("/repos/AlbertoRacerro/JarvisOS")
    (connection,) = _Connection.created
    assert connection.host == rt.API_HOST
    assert "Authorization" not in connection.headers


def test_configured_token_is_sent_only_as_bearer_to_the_fixed_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, f"  {TOKEN}\n")
    rt._github_request("/repos/AlbertoRacerro/JarvisOS/pulls/1")
    (connection,) = _Connection.created
    assert connection.host == "api.github.com"
    assert connection.headers["Authorization"] == f"Bearer {TOKEN}"


def test_token_can_come_from_a_systemd_credential(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / TOKEN_ENV).write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    rt._github_request("/repos/AlbertoRacerro/JarvisOS")
    assert _Connection.created[0].headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("bad", ["short", "has space " + "x" * 30, "tab\there" + "x" * 30, "x" * 300])
def test_malformed_token_is_ignored_and_never_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, bad: str
) -> None:
    monkeypatch.setenv(TOKEN_ENV, bad)
    with caplog.at_level(logging.DEBUG):
        rt._github_request("/repos/AlbertoRacerro/JarvisOS")
    assert "Authorization" not in _Connection.created[0].headers
    assert bad.strip() not in caplog.text


def test_token_never_reaches_results_or_logs(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    sha = "a" * 40
    body = json.dumps({"sha": sha, "commit": {"message": "m", "author": {"date": "2026-10-30T00:00:00Z"}}}).encode()

    class _Ok(_Response):
        def read(self, _limit: int) -> bytes:
            return body

    monkeypatch.setattr(_Connection, "getresponse", lambda self: _Ok())
    service = rt.RepositoryTruthService(["AlbertoRacerro/JarvisOS"])
    with caplog.at_level(logging.DEBUG):
        result = service.repository_ref_truth("AlbertoRacerro/JarvisOS", "master")
    assert _Connection.created[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in caplog.text
    assert TOKEN not in json.dumps(result.payload, default=str)
    assert TOKEN not in repr(result)


def test_non_fixed_host_paths_still_refused_even_with_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    for path in ("https://evil.example/x", "repos/x", "/repos/x\n"):
        with pytest.raises(rt.RepositoryTruthError) as raised:
            rt._github_request(path)
        assert raised.value.code == "unsupported_host"
    assert _Connection.created == []
