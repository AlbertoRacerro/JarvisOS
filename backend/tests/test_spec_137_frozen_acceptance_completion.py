from __future__ import annotations

import email.message
import importlib.util
import io
import json
import subprocess
import sys
import urllib.request
import urllib.response
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "scripts" / "check_architecture_enforcement.py"
ROUTER_RESPONDER = REPO_ROOT / "backend" / "app" / "modules" / "local_ai" / "ollama_generate.py"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_checker(root: Path) -> subprocess.CompletedProcess[str]:
    config = root / "configs" / "architecture_enforcement.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"exceptions": []}), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--root",
            str(root),
            "--config",
            str(config),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_frozen_ae002_fixture_family_includes_remaining_alias_and_socket_cases(tmp_path: Path) -> None:
    source = tmp_path / "backend" / "app" / "frozen_network_fixtures.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
from urllib.request import build_opener as make_opener
from urllib3 import PoolManager
from socket import socket as Socket
from aiohttp import ClientSession as Session
from http.client import HTTPSConnection as Connection
import app.modules.ai.providers.deepseek


def urllib_dispatch(url):
    opener = make_opener()
    return opener.open(url)


def urllib3_dispatch(url):
    pool = PoolManager()
    return pool.request("GET", url)


def socket_connect_ex_dispatch():
    sock = Socket()
    return sock.connect_ex(("example.com", 443))


def socket_sendmsg_dispatch():
    sock = Socket()
    return sock.sendmsg([b"x"], [], 0, ("example.com", 443))


def aiohttp_dispatch(url):
    session = Session()
    return session.get(url)


def http_client_dispatch():
    connection = Connection("example.com")
    return connection.request("GET", "/")


def provider_dispatch(adapter):
    return adapter.complete("x")
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = _run_checker(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    for symbol in (
        "urllib_dispatch",
        "urllib3_dispatch",
        "socket_connect_ex_dispatch",
        "socket_sendmsg_dispatch",
        "aiohttp_dispatch",
        "http_client_dispatch",
        "provider_dispatch",
    ):
        assert f"AE002 backend/app/frozen_network_fixtures.py::{symbol}" in result.stdout


class _FakeRedirectHTTPHandler(urllib.request.BaseHandler):
    handler_order = 100

    def __init__(self, seen: list[str]) -> None:
        self._seen = seen

    def http_open(self, request: Any) -> Any:
        self._seen.append(request.full_url)
        headers = email.message.Message()
        headers["Location"] = "http://example.invalid/escape"
        response = urllib.response.addinfourl(
            io.BytesIO(b""),
            headers,
            request.full_url,
            code=302,
        )
        response.msg = "Found"
        return response


def _install_fake_redirect_transport(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    seen: list[str],
) -> None:
    real_build_opener = urllib.request.build_opener

    def fake_build_opener(*handlers: Any) -> Any:
        return real_build_opener(*handlers, _FakeRedirectHTTPHandler(seen))

    monkeypatch.setattr(module.urllib.request, "build_opener", fake_build_opener)


def test_router_transport_302_never_follows_external_location(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _load_module("spec137_router_redirect", ROUTER_RESPONDER)
    seen: list[str] = []
    _install_fake_redirect_transport(monkeypatch, router, seen)
    local_endpoint = "http://127.0.0.1:11434/api/generate"

    with pytest.raises(router.LocalResponderTransportError):
        router._stdlib_json_post_client(local_endpoint, {"model": "local-test"}, 1.0)

    assert seen == [local_endpoint]


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://localhost:11434/api/generate",
        "http://192.0.2.1:11434/api/generate",
        "http://user@localhost:11434/api/generate",
        "http://localhost:11434/not-generate",
        "http://localhost:11434/api/generate?escape=1",
    ],
)
def test_router_transport_rejects_invalid_endpoint_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    router = _load_module("spec137_router_reject", ROUTER_RESPONDER)

    def fail_dispatch(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("urllib dispatch must not occur for rejected router endpoints")

    monkeypatch.setattr(router.urllib.request, "Request", fail_dispatch)
    monkeypatch.setattr(router.urllib.request, "build_opener", fail_dispatch)

    with pytest.raises(router.LocalResponderPolicyError):
        router._stdlib_json_post_client(endpoint, {}, 1.0)
