"""Offline tests for search guard specificity rules and Italian transparency (spec 364)."""

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
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import search_guard as guard  # noqa: E402

from app.core import packs  # noqa: E402
from app.core.packs import PackError, read_pack  # noqa: E402
from app.modules.ai import routes as ai_routes  # noqa: E402
from app.modules.ai.search_guard_summary import (  # noqa: E402
    REASON_MESSAGES_IT,
    read_search_guard_blocked_summary,
)


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


def request(path: str, tmp_path: Path, headers=None):
    handler, output = make_handler(path, tmp_path / "guard.log", headers)
    handler.do_GET()
    return output, handler.wfile.getvalue()


def status(output):
    return next(value for key, value in output if key == "status")


@pytest.mark.usefixtures("fake_pack")
def test_generic_ranges_pass_search_guard(tmp_path):
    """Generic physics/engineering queries with broad dimension ranges (> 20% width) pass through."""
    queries = [
        "apple 40-60 mm",
        "banana 40–60 mm",
        "cherry 40 to 60 mm",
        "date 10-30 mm",
        "from 40 to 60 mm",
        "between 40 and 60 mm",
        "physics of 40mm - 60mm piping",
    ]
    for q in queries:
        output, body = request("/search?q=" + quote(q) + "&format=json", tmp_path)
        assert status(output) == 200, f"Query {q!r} should pass"
        assert b"sample" in body, f"Query {q!r} should forward to upstream"


@pytest.mark.usefixtures("fake_pack")
def test_exact_combinations_block(tmp_path):
    """Queries matching all terms in an exact combination block; partial combinations pass."""
    # Full combination match: ["alpha", "beta", "gamma"]
    output, body = request("/search?q=" + quote("study of alpha beta and gamma components") + "&format=json", tmp_path)
    assert status(output) == 200
    assert json.loads(body) == {"results": []}

    # Partial combination does not block
    FakeConnection.calls.clear()
    output, body = request("/search?q=" + quote("alpha beta analysis") + "&format=json", tmp_path)
    assert status(output) == 200
    assert b"sample" in body
    assert len(FakeConnection.calls) == 1

    # Check reason in log
    lines = (tmp_path / "guard.log").read_text().splitlines()
    blocked_entry = json.loads(lines[0])
    assert blocked_entry["status"] == "blocked"
    assert blocked_entry["reason_code"] == "exact_combination"
    assert blocked_entry["reason_it"] == REASON_MESSAGES_IT["exact_combination"]


@pytest.mark.usefixtures("fake_pack")
def test_exact_protected_dimensions_block(tmp_path):
    """Exact protected dimensions within tolerance alongside context groups block."""
    # 50.0 mm protected in zephyr pack with 2% tolerance (49.0 - 51.0 mm)
    blocked_queries = [
        "apple 50 mm",
        "malus 49.5 mm",
        "banana 50.8 mm",
        "cherry 50mm",
        "date 50.0 mm",
    ]
    for q in blocked_queries:
        output, body = request("/search?q=" + quote(q) + "&format=json", tmp_path)
        assert status(output) == 200
        assert json.loads(body) == {"results": []}, f"Query {q!r} should be blocked"

    # Dimension outside tolerance: 55 mm passes
    FakeConnection.calls.clear()
    output, body = request("/search?q=" + quote("apple 55 mm") + "&format=json", tmp_path)
    assert status(output) == 200
    assert b"sample" in body

    # Dimension without any context group passes
    FakeConnection.calls.clear()
    output, body = request("/search?q=" + quote("standard 50 mm bolt") + "&format=json", tmp_path)
    assert status(output) == 200
    assert b"sample" in body

    # Narrow range enclosing protected dimension (span 1% <= 20%) blocks
    output, body = request("/search?q=" + quote("apple 50 to 50.5 mm") + "&format=json", tmp_path)
    assert status(output) == 200
    assert json.loads(body) == {"results": []}


@pytest.mark.usefixtures("fake_pack")
def test_fail_closed_invariants_preserved(tmp_path):
    """Direct pack terms, sanitized diffs, and sensitivity floor queries still block fail-closed."""
    # pack term
    output, body = request("/search?q=zephyr+blueprints&format=json", tmp_path)
    assert json.loads(body) == {"results": []}

    # sanitized diff
    output, body = request("/search?q=password%3DMySecretKey123&format=json", tmp_path)
    assert json.loads(body) == {"results": []}

    # check reasons in log
    lines = [json.loads(line) for line in (tmp_path / "guard.log").read_text().splitlines()]
    codes = [entry["reason_code"] for entry in lines]
    assert "pack_term" in codes
    assert "sanitized_diff" in codes


@pytest.mark.usefixtures("fake_pack")
def test_jsonl_log_structure_and_leak_guard(tmp_path):
    """Log file contains only JSONL with SHA-256 prefixes, reason codes, and Italian reasons — never raw queries."""
    query = "zephyr proprietary turbine 50 mm"
    request("/search?q=" + quote(query) + "&format=json", tmp_path)

    log_content = (tmp_path / "guard.log").read_text(encoding="utf-8")
    lines = [json.loads(line) for line in log_content.splitlines()]
    assert len(lines) == 1
    entry = lines[0]

    assert entry["status"] == "blocked"
    assert "sha256_prefix" in entry
    assert len(entry["sha256_prefix"]) == 16
    assert "reason_code" in entry
    assert "reason_it" in entry
    assert entry["reason_it"] in REASON_MESSAGES_IT.values()

    # Absolute leak guard: raw query string and sensitive literals must never appear in log
    assert "zephyr" not in log_content
    assert "turbine" not in log_content
    assert "proprietary" not in log_content


def test_log_write_failure_blocks_fail_closed(tmp_path):
    """When the query log cannot be written, search guard blocks with 502 Bad Gateway."""
    # Point log_file to a non-existent path in a read-only directory
    unwritable_log = tmp_path / "readonly_dir" / "guard.log"
    handler, output = make_handler("/search?q=ordinary+query&format=json", unwritable_log)

    # Force write failure by making log_query return False
    original_log = guard.log_query
    try:
        guard.log_query = lambda *a, **k: False
        handler.do_GET()
        assert status(output) == 502
        assert b"query log unavailable" in handler.wfile.getvalue()
        assert not FakeConnection.calls
    finally:
        guard.log_query = original_log


def test_read_search_guard_blocked_summary_endpoint(tmp_path, monkeypatch):
    """The read-only endpoint returns bounded recent blocked queries with plain Italian reasons."""
    log_file = tmp_path / "test_guard.log"

    # Write both legacy and JSONL lines
    legacy_line = "2026-11-24T10:00:00Z blocked 0123456789abcdef\n"
    jsonl_line1 = json.dumps({
        "ts": "2026-11-24T11:00:00Z",
        "status": "blocked",
        "sha256_prefix": "abcdef0123456789",
        "reason_code": "exact_combination",
        "reason_it": REASON_MESSAGES_IT["exact_combination"],
        "rule_index": 0,
    }) + "\n"
    jsonl_ok = json.dumps({
        "ts": "2026-11-24T11:30:00Z",
        "status": "ok",
        "sha256_prefix": "1111222233334444",
        "reason_code": "ok",
        "reason_it": "Consentito",
    }) + "\n"
    jsonl_line2 = json.dumps({
        "ts": "2026-11-24T12:00:00Z",
        "status": "blocked",
        "sha256_prefix": "fedcba9876543210",
        "reason_code": "protected_dimension",
        "reason_it": REASON_MESSAGES_IT["protected_dimension"],
        "rule_index": 1,
    }) + "\n"

    log_file.write_text(legacy_line + jsonl_line1 + jsonl_ok + jsonl_line2, encoding="utf-8")

    summary = read_search_guard_blocked_summary(log_file, limit=50)
    assert summary.total_blocked == 3
    assert len(summary.recent_blocked) == 3

    # Most recent first
    assert summary.recent_blocked[0].sha256_prefix == "fedcba9876543210"
    assert summary.recent_blocked[0].reason_code == "protected_dimension"
    assert summary.recent_blocked[0].reason_it == REASON_MESSAGES_IT["protected_dimension"]

    assert summary.recent_blocked[1].sha256_prefix == "abcdef0123456789"
    assert summary.recent_blocked[1].reason_code == "exact_combination"

    assert summary.recent_blocked[2].sha256_prefix == "0123456789abcdef"
    assert summary.recent_blocked[2].reason_code == "legacy_blocked"
    assert summary.recent_blocked[2].reason_it == REASON_MESSAGES_IT["legacy_blocked"]

    # Verify the registered read-only route and its returned model.
    monkeypatch.setattr(ai_routes, "read_search_guard_blocked_summary", lambda: summary)
    route = next(route for route in ai_routes.router.routes if route.path == "/ai/search-guard/blocked")
    assert route.methods == {"GET"}
    response = route.endpoint()
    assert response.total_blocked == 3
    assert len(response.recent_blocked) == 3


def test_malformed_pack_configs_fail_closed(tmp_path):
    """Malformed [search_guard] configs fail closed with PackError."""
    pack_dir = tmp_path / "bad_pack"
    pack_dir.mkdir()
    manifest = pack_dir / "jarvis_pack.toml"

    malformed_configs = [
        # Non-positive value
        'id="bad"\n[search_guard]\nprotected_dimensions=[{value=-5.0, unit="mm"}]\n',
        'id="bad"\n[search_guard]\nprotected_dimensions=[{value=0, unit="mm"}]\n',
        # Unknown or empty unit
        'id="bad"\n[search_guard]\nprotected_dimensions=[{value=50.0, unit="unknown_unit_xyz"}]\n',
        'id="bad"\n[search_guard]\nprotected_dimensions=[{value=50.0, unit=""}]\n',
        # Invalid combination types
        'id="bad"\n[search_guard]\nexact_combinations=["not_a_list_of_lists"]\n',
        'id="bad"\n[search_guard]\nexact_combinations=[[]]\n',
        'id="bad"\n[search_guard]\nexact_combinations=[[123]]\n',
        # Non-boolean allow_generic_ranges
        'id="bad"\n[search_guard]\nallow_generic_ranges="true"\n',
        # Invalid tolerance
        'id="bad"\n[search_guard]\nprotected_dimensions=[{value=50.0, unit="mm", tolerance_rel=-0.05}]\n',
        'id="bad"\n[search_guard]\nprotected_dimensions=[{value=50.0, unit="mm", tolerance_rel=1.5}]\n',
        'id="bad"\n[search_guard]\nprotected_dimensions=[{value=nan, unit="mm"}]\n',
        'id="bad"\n[search_guard]\nprotected_dimensions=[{value=50.0, unit="mm", tolerance_rel=nan}]\n',
        # Unknown search_guard key
        'id="bad"\n[search_guard]\nunknown_key=123\n',
    ]

    for cfg in malformed_configs:
        manifest.write_text(cfg)
        with pytest.raises(PackError):
            read_pack(pack_dir)


def test_summary_does_not_echo_untrusted_log_fields(tmp_path):
    log_file = tmp_path / "guard.log"
    log_file.write_text(json.dumps({
        "ts": "private query",
        "status": "blocked",
        "sha256_prefix": "private query",
        "reason_code": "private query",
        "reason_it": "private query",
    }) + "\n", encoding="utf-8")
    summary = read_search_guard_blocked_summary(log_file)
    assert summary.total_blocked == 1
    assert "private query" not in summary.model_dump_json()
    assert summary.recent_blocked[0].reason_code == "legacy_blocked"


def test_no_pack_mode(tmp_path, monkeypatch):
    """When no pack is configured, search guard allows normal queries and blocks secrets."""
    monkeypatch.delenv(packs.PACK_PATH_ENV, raising=False)
    packs._load.cache_clear()

    # Normal query with ranges passes
    output, _ = request("/search?q=normal+engineering+40-60+mm", tmp_path)
    assert status(output) == 200
    assert len(FakeConnection.calls) == 1

    # Secret still blocks fail-closed
    FakeConnection.calls.clear()
    output, body = request("/search?q=password%3DExampleSecret123&format=json", tmp_path)
    assert json.loads(body) == {"results": []}
    assert not FakeConnection.calls


def test_mixed_packs_enforce_each_packs_policy(tmp_path, monkeypatch):
    """A new rule in one pack must not disable a legacy pack or borrow its context."""
    legacy = tmp_path / "legacy"
    specific = tmp_path / "specific"
    legacy.mkdir()
    specific.mkdir()
    (legacy / "jarvis_pack.toml").write_text(
        'id="legacy"\n[search_guard]\nmin_groups=2\n'
        'groups=[["copper"],["orbit"]]\n', encoding="utf-8"
    )
    (specific / "jarvis_pack.toml").write_text(
        'id="specific"\n[search_guard]\n'
        'groups=[["valve"]]\nexact_combinations=[["iris","delta"]]\n'
        'protected_dimensions=[{value=50.0, unit="mm"}]\n', encoding="utf-8"
    )
    monkeypatch.setenv(packs.PACK_PATH_ENV, os.pathsep.join((str(legacy), str(specific))))
    packs._load.cache_clear()
    try:
        cases = (
            ("copper orbit study", "guard_groups"),
            ("iris delta study", "exact_combination"),
            ("valve 50 mm study", "protected_dimension"),
            ("copper 50 mm study", "ok"),
            ("copper valve study", "ok"),
        )
        for query, reason in cases:
            output, body = request("/search?q=" + quote(query) + "&format=json", tmp_path)
            assert status(output) == 200
            if reason == "ok":
                assert b"sample" in body
            else:
                assert json.loads(body) == {"results": []}
            entry = json.loads((tmp_path / "guard.log").read_text().splitlines()[-1])
            assert entry["reason_code"] == reason
    finally:
        packs._load.cache_clear()


def test_protected_dimension_with_compound_unit(tmp_path, monkeypatch):
    pack_dir = tmp_path / "specific"
    pack_dir.mkdir()
    (pack_dir / "jarvis_pack.toml").write_text(
        'id="specific"\n[search_guard]\ngroups=[["flow"]]\n'
        'protected_dimensions=[{value=50.0, unit="m/s"}]\n', encoding="utf-8"
    )
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack_dir))
    packs._load.cache_clear()
    try:
        output, body = request("/search?q=" + quote("flow 50 m/s") + "&format=json", tmp_path)
        assert status(output) == 200
        assert json.loads(body) == {"results": []}
        assert json.loads((tmp_path / "guard.log").read_text())["reason_code"] == "protected_dimension"
    finally:
        packs._load.cache_clear()


@pytest.mark.usefixtures("fake_pack")
def test_protected_terms_in_other_parameters_block(tmp_path):
    """The whole request is forwarded upstream, so every parameter value is evaluated, not only q."""
    output, body = request(
        "/search?q=" + quote("alpha analysis") + "&format=json&categories=" + quote("beta gamma"), tmp_path
    )
    assert status(output) == 200
    assert json.loads(body) == {"results": []}
    assert json.loads((tmp_path / "guard.log").read_text().splitlines()[0])["reason_code"] == "exact_combination"
