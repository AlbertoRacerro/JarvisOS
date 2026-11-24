"""Offline checks for the local search guard and its host service policy."""

from __future__ import annotations

import io
import json
import os
import sys
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import host_ops as host  # noqa: E402
import search_guard as guard  # noqa: E402

from app.core import packs  # noqa: E402
from app.core.packs import PackError, read_pack  # noqa: E402


def make_handler(path: str, log_file: Path, headers: dict[str, str] | None = None) -> tuple[guard.SearchGuardHandler, list]:
    handler = guard.SearchGuardHandler.__new__(guard.SearchGuardHandler)
    handler.path = path
    handler.headers = Message()
    for key, value in (headers or {}).items():
        handler.headers[key] = value
    handler.wfile = io.BytesIO()
    handler.server = SimpleNamespace(upstream_host="127.0.0.1", upstream_port=8888, log_file=log_file)
    output: list = []
    handler.send_response = lambda status: output.append(("status", status))
    handler.send_header = lambda key, value: output.append((key.lower(), value))
    handler.end_headers = lambda: None
    return handler, output


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b'{"results":[{"title":"sample"}]}', headers=None):
        self.status = status
        self.body = body
        self.headers = headers or [("Content-Type", "application/json")]

    def read(self, size: int) -> bytes:
        return self.body[:size]

    def getheaders(self):
        return self.headers


class FakeConnection:
    calls: list[tuple[str, str, dict]] = []
    response = FakeResponse()
    failure = False

    def __init__(self, host, port, timeout):
        assert (host, port) == ("127.0.0.1", 8888)
        assert timeout <= 10

    def request(self, method, path, headers):
        self.calls.append((method, path, headers))
        if self.failure:
            raise OSError("private path /hidden")

    def getresponse(self):
        return self.response

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_upstream(monkeypatch):
    FakeConnection.calls = []
    FakeConnection.response = FakeResponse()
    FakeConnection.failure = False
    monkeypatch.setattr(guard.http.client, "HTTPConnection", FakeConnection)


@pytest.fixture
def fake_pack(monkeypatch):
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(ROOT / "backend/tests/fixtures/packs/zephyr"))
    packs._load.cache_clear()
    yield
    packs._load.cache_clear()


def request(path: str, tmp_path: Path, headers=None):
    handler, output = make_handler(path, tmp_path / "guard.log", headers)
    handler.do_GET()
    return output, handler.wfile.getvalue()


def status(output):
    return next(value for key, value in output if key == "status")


def test_pack_schema_and_defaults(tmp_path):
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    manifest = pack_dir / "jarvis_pack.toml"
    manifest.write_text('id="sample"\n[search_guard]\nmin_groups=2\ngroups=[["apple","malus"],["banana"]]\n')
    loaded = read_pack(pack_dir)
    assert loaded.search_guard.min_groups == 2
    assert loaded.search_guard.groups == (("apple", "malus"), ("banana",))
    manifest.write_text('id="sample"\n')
    assert read_pack(pack_dir).search_guard.groups == ()
    assert read_pack(pack_dir).search_guard.min_groups == 4
    for table in ('groups="bad"', 'groups=[[]]', 'groups=[[1]]', 'min_groups=0', 'min_groups=true'):
        manifest.write_text('id="sample"\n[search_guard]\n' + table + '\n')
        with pytest.raises(PackError):
            read_pack(pack_dir)


@pytest.mark.usefixtures("fake_pack")
def test_blocked_categories_and_no_query_echo(tmp_path):
    for query in ("zephyr plans", "apple banana cherry date", "password=ExampleSecret123"):
        output, body = request("/search?q=" + quote(query) + "&format=json", tmp_path)
        assert status(output) == 200
        assert json.loads(body) == {"results": []}
        assert query.encode() not in body
    assert not FakeConnection.calls
    lines = (tmp_path / "guard.log").read_text().splitlines()
    assert len(lines) == 3
    assert all(
        (len(line.split()) == 3 and line.split()[1] == "blocked")
        or (json.loads(line).get("status") == "blocked")
        for line in lines
    )
    assert all(query not in "\n".join(lines) for query in ("zephyr", "apple", "password"))


def test_multi_whitespace_cannot_bypass_multiword_terms_or_groups(tmp_path, monkeypatch):
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (pack_dir / "jarvis_pack.toml").write_text(
        'id="sample"\n'
        '[sensitivity]\n'
        'terms=["project falcon"]\n'
        '[search_guard]\n'
        'min_groups=2\n'
        'groups=[["blue ridge"],["solar array"]]\n'
    )
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack_dir))
    packs._load.cache_clear()
    try:
        for query in ("project   falcon", "blue  ridge  solar   array"):
            output, body = request("/search?q=" + quote(query) + "&format=json", tmp_path)
            assert status(output) == 200
            assert json.loads(body) == {"results": []}
        assert not FakeConnection.calls
    finally:
        packs._load.cache_clear()


@pytest.mark.usefixtures("fake_pack")
def test_synonyms_count_once_and_clean_query_forwards(tmp_path):
    output, body = request("/search?q=apple+malus+banana+cherry&format=json", tmp_path)
    assert status(output) == 200
    assert b"sample" in body
    assert len(FakeConnection.calls) == 1
    assert FakeConnection.calls[0][2] == {"Connection": "close", "Accept-Encoding": "identity"}


@pytest.mark.usefixtures("fake_pack")
def test_html_and_duplicate_query_fail_closed(tmp_path):
    output, body = request("/search?q=zephyr", tmp_path)
    assert status(output) == 200
    assert b'<div id="results"></div>' in body
    output, body = request("/search?q=clean&q=zephyr&format=json", tmp_path)
    assert status(output) == 200
    assert json.loads(body) == {"results": []}
    assert not FakeConnection.calls


def test_pack_load_error_is_generic_and_blocks(tmp_path, monkeypatch):
    private_path = tmp_path / "private_source"
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(private_path))
    packs._load.cache_clear()
    output, body = request("/healthz", tmp_path)
    assert status(output) == 503
    assert b"not_ready" in body
    assert str(private_path).encode() not in body
    output, body = request("/search?q=clean&format=json", tmp_path)
    assert status(output) == 200
    assert json.loads(body) == {"results": []}
    assert not FakeConnection.calls


def test_empty_configured_pack_path_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv(packs.PACK_PATH_ENV, os.pathsep)
    packs._load.cache_clear()
    output, body = request("/search?q=clean&format=json", tmp_path)
    assert status(output) == 200
    assert json.loads(body) == {"results": []}
    assert not FakeConnection.calls


def test_no_pack_still_blocks_secret(tmp_path, monkeypatch):
    monkeypatch.delenv(packs.PACK_PATH_ENV, raising=False)
    packs._load.cache_clear()
    output, _ = request("/search?q=ordinary+plants", tmp_path)
    assert status(output) == 200
    assert len(FakeConnection.calls) == 1
    output, body = request("/search?q=password%3DExampleSecret123&format=json", tmp_path)
    assert json.loads(body) == {"results": []}
    assert len(FakeConnection.calls) == 1


def test_paths_methods_bounds_and_upstream_failures(tmp_path):
    for path in ("/", "/stats", "/config?secret=foo", "/healthz?secret=foo"):
        output, _ = request(path, tmp_path)
        assert status(output) in {404, 502}
    handler, output = make_handler("/search?q=ok", tmp_path / "log")
    handler.do_POST()
    assert status(output) == 404
    output, _ = request("/search?q=" + "a" * guard.MAX_REQUEST_TARGET, tmp_path)
    assert status(output) == 502
    FakeConnection.response = FakeResponse(302, b"", [("Location", "/private")])
    output, body = request("/config", tmp_path)
    assert status(output) == 502
    assert b"private" not in body
    FakeConnection.response = FakeResponse(200, b"x" * (guard.MAX_UPSTREAM_BYTES + 1))
    output, _ = request("/config", tmp_path)
    assert status(output) == 502
    FakeConnection.failure = True
    output, body = request("/config", tmp_path)
    assert status(output) == 502
    assert b"hidden" not in body
    handler, output = make_handler("/search?q=private", tmp_path / "log")
    handler.send_error(400, "Bad request syntax: /search?q=private")
    assert status(output) == 400
    assert b"private" not in handler.wfile.getvalue()


def test_loopback_and_host_policy(tmp_path):
    for address in ("0.0.0.0", "localhost", "192.0.2.1"):
        with pytest.raises(ValueError, match="must be loopback"):
            guard.create_server(bind_address=address)
        with pytest.raises(ValueError, match="must be loopback"):
            guard.create_server(upstream_host=address)
    searx = host.UNIT_TEMPLATES["jarvisos-searxng"]
    proxy = host.UNIT_TEMPLATES["jarvisos-search-guard"]
    for fragment in ("WorkingDirectory=%h/tools/searxng", "SEARXNG_SETTINGS_PATH=", "searx.webapp", "MemoryMax=", "CPUQuota="):
        assert fragment in searx
    for fragment in ("search_guard.py", "EnvironmentFile=", "PYTHONPATH=", "NoNewPrivileges=yes", "After=jarvisos-searxng.service"):
        assert fragment in proxy
    ops = host.DEFAULT_POLICY["ops"]
    for name in ("jarvisos-searxng", "jarvisos-search-guard"):
        assert name in ops["service.install"]["templates"]
        for action in ("service.status", "service.control"):
            assert name + ".service" in ops[action]["units"]
    assert ops["http.smoke"]["targets"]["search-guard"]["expect"] == 200
    paths = host.Paths(tmp_path)
    paths.policy.parent.mkdir(parents=True, exist_ok=True)
    paths.policy.write_text(json.dumps(host.DEFAULT_POLICY))
    ctx = host.Context(paths, host.DEFAULT_POLICY,
                       {"id": "1" * 32, "op": "service.install", "args": {"template": "unknown"}},
                       lambda *_a, **_k: None, lambda *_a: 0, lambda: None, lambda: False, [])
    with pytest.raises(host.Refused):
        host.op_service_install(ctx)
