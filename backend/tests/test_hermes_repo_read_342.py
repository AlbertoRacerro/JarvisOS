"""Tests for Spec 342 — JARVIS-REPO-READ-TOOLS-1 (Hermes repo_read read-only tool)."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.core import packs
from app.modules.agents.hermes import broker_mcp
from app.modules.agents.hermes.repo_read_tool import dispatch_repo_read_tool
from app.modules.agents.hermes.supervisor import (
    _HERMES_REPO_READ_CAPABILITY,
    _TOOL_CAPABILITIES,
    HERMES_TOOL_ALLOWLIST,
    PRODUCTION_CAPABILITY_REGISTRY,
    dispatch_tool,
)
from app.modules.ai import context_builder, thread_service
from app.modules.ai.agent_contracts import (
    AgentSessionRef,
    CapabilityGrantRef,
    CapabilityScope,
    StructuredToolCall,
)
from app.modules.ai.thread_models import AIThreadSubmit
from app.modules.coding import relay_monitor

_SESSION = AgentSessionRef(
    jarvis_thread_id="thread-test",
    hermes_session_id="hermes-session-test",
    profile_id="isaac",
    workspace_id="workspace-test",
    generation=1,
    upstream_revision="2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19",
)


def _make_call(
    arguments: dict[str, Any],
    capability_id: str = "jarvis.repo_read",
    grant_id: str = "grant-repo-read",
) -> StructuredToolCall:
    now = datetime.now(UTC)
    return StructuredToolCall(
        call_id=str(uuid4()),
        capability_id=capability_id,
        grant_id=grant_id,
        correlation_id="corr-1",
        session_ref=_SESSION,
        arguments=arguments,
        requested_at=now,
        deadline_at=now + timedelta(seconds=60),
    )


def _make_grant(
    grant_id: str = "grant-repo-read",
    capability_id: str = "jarvis.repo_read",
    workspace_id: str = "workspace-test",
    thread_id: str = "thread-test",
    route_class: str = "local:llamacpp",
    expired: bool = False,
) -> CapabilityGrantRef:
    now = datetime.now(UTC)
    if expired:
        issued = now - timedelta(minutes=10)
        expires = now - timedelta(minutes=1)
    else:
        issued = now - timedelta(minutes=5)
        expires = now + timedelta(minutes=10)
    return CapabilityGrantRef(
        grant_id=grant_id,
        capability_id=capability_id,
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=workspace_id, jarvis_thread_id=thread_id),
        constraints={
            "route_class": route_class,
            "is_local": route_class.startswith("local:") or route_class == "fake",
        },
        issued_at=issued,
        expires_at=expires,
    )


# ---------------------------------------------------------------------------
# Registration and Schema Checks
# ---------------------------------------------------------------------------


def test_repo_read_tool_registered_in_allowlists_and_broker() -> None:
    """Acceptance criterion 1: jarvis_repo_read is registered in all allowlists and schemas."""
    # 1. broker_mcp._TOOLS contains jarvis_repo_read with strict JSON schema
    repo_tool = next((t for t in broker_mcp._TOOLS if t["name"] == "jarvis_repo_read"), None)
    assert repo_tool is not None
    schema = repo_tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["grant_id", "op"]
    assert set(schema["properties"]["op"]["enum"]) == {"list", "search", "read"}
    assert set(schema["properties"]["scope"]["enum"]) == {"public", "pack"}
    assert schema["properties"]["limit"]["minimum"] == 1
    assert schema["properties"]["limit"]["maximum"] == 200

    # 2. supervisor._TOOL_CAPABILITIES maps jarvis_repo_read -> jarvis.repo_read
    assert _TOOL_CAPABILITIES.get("jarvis_repo_read") == "jarvis.repo_read"

    # 3. supervisor.HERMES_TOOL_ALLOWLIST contains mcp__jarvis__jarvis_repo_read
    assert "mcp__jarvis__jarvis_repo_read" in HERMES_TOOL_ALLOWLIST

    # 4. PRODUCTION_CAPABILITY_REGISTRY has jarvis.repo_read registered with action_class="READ"
    desc = next(
        (c for c in PRODUCTION_CAPABILITY_REGISTRY.for_route("ai-threads") if c.capability_id == "jarvis.repo_read"),
        None,
    )
    assert desc is not None
    assert desc.action_class == "READ"
    assert desc == _HERMES_REPO_READ_CAPABILITY


# ---------------------------------------------------------------------------
# Scope Public: list, search, read
# ---------------------------------------------------------------------------


def test_op_list_public_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs").mkdir()
    (repo / "docs" / "index.md").write_text("# Home\nWelcome to JarvisOS", encoding="utf-8")
    (repo / "docs" / "guide.md").write_text("# Guide\nHow to use", encoding="utf-8")
    (repo / "backend").mkdir()
    (repo / "backend" / "main.py").write_text("print('hello')\n", encoding="utf-8")

    # Directories that must be skipped
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "package.json").write_text("{}", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "lib.py").write_text("# venv\n", encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"\x00\x00\x00\x00")

    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)

    # 1. Unfiltered list at root
    call = _make_call({"op": "list"})
    res, _ = dispatch_repo_read_tool(call, "workspace-test", "err")
    assert res is not None
    assert res["op"] == "list"
    assert res["scope"] == "public"
    assert res["pack_id"] is None
    assert res["confidentiality"] == "public"
    assert res["path"] == ""
    assert res["total"] == 3
    assert res["count"] == 3
    assert res["files"] == ["backend/main.py", "docs/guide.md", "docs/index.md"]
    assert res["truncated"] is False

    # 2. List with subpath prefix
    call_sub = _make_call({"op": "list", "path": "docs"})
    res_sub, _ = dispatch_repo_read_tool(call_sub, "workspace-test", "err")
    assert res_sub is not None
    assert res_sub["path"] == "docs"
    assert res_sub["total"] == 2
    assert res_sub["files"] == ["docs/guide.md", "docs/index.md"]

    # 3. List with limit
    call_lim = _make_call({"op": "list", "limit": 2})
    res_lim, _ = dispatch_repo_read_tool(call_lim, "workspace-test", "err")
    assert res_lim is not None
    assert res_lim["total"] == 3
    assert res_lim["count"] == 2
    assert len(res_lim["files"]) == 2
    assert res_lim["truncated"] is True


def test_op_search_public_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app").mkdir()
    (repo / "app" / "calc.py").write_text(
        "# Coefficient calculation\n"
        "ALPHA = 0.42\n"
        "BETA = 1.25\n"
        "# coefficient helper\n"
        "def compute_coefficient(x):\n"
        "    return ALPHA * x\n",
        encoding="utf-8",
    )
    (repo / "docs").mkdir()
    (repo / "docs" / "math.md").write_text(
        "# Math rules\n"
        "See coefficient table below:\n"
        "- ALPHA: 0.42\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)

    # 1. Search across whole repo (case-insensitive literal substring)
    call = _make_call({"op": "search", "query": "coefficient"})
    res, _ = dispatch_repo_read_tool(call, "workspace-test", "err")
    assert res is not None
    assert res["op"] == "search"
    assert res["scope"] == "public"
    assert res["pack_id"] is None
    assert res["confidentiality"] == "public"
    assert res["count"] == 4
    matches = res["matches"]
    assert matches[0]["path"] == "app/calc.py"
    assert matches[0]["line"] == 1
    assert "Coefficient calculation" in matches[0]["snippet"]
    assert matches[1]["path"] == "app/calc.py"
    assert matches[1]["line"] == 4
    assert matches[2]["path"] == "app/calc.py"
    assert matches[2]["line"] == 5
    assert matches[3]["path"] == "docs/math.md"
    assert matches[3]["line"] == 2

    # 2. Search restricted by path prefix
    call_path = _make_call({"op": "search", "query": "coefficient", "path": "docs"})
    res_path, _ = dispatch_repo_read_tool(call_path, "workspace-test", "err")
    assert res_path is not None
    assert res_path["count"] == 1
    assert res_path["matches"][0]["path"] == "docs/math.md"

    # 3. Search with limit
    call_lim = _make_call({"op": "search", "query": "coefficient", "limit": 2})
    res_lim, _ = dispatch_repo_read_tool(call_lim, "workspace-test", "err")
    assert res_lim is not None
    assert res_lim["count"] == 2
    assert len(res_lim["matches"]) == 2


def test_op_read_public_scope_slicing_and_truncation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lines = [f"line {i:03d}" for i in range(1, 201)]
    (repo / "test.txt").write_text("\n".join(lines), encoding="utf-8")

    # Long text for 16,000 char truncation check
    long_lines = ["X" * 100 for _ in range(250)]  # 25,000 chars
    (repo / "long.txt").write_text("\n".join(long_lines), encoding="utf-8")

    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)

    # 1. Read default start_line=1, max_lines=150
    call = _make_call({"op": "read", "path": "test.txt"})
    res, _ = dispatch_repo_read_tool(call, "workspace-test", "err")
    assert res is not None
    assert res["op"] == "read"
    assert res["scope"] == "public"
    assert res["pack_id"] is None
    assert res["confidentiality"] == "public"
    assert res["path"] == "test.txt"
    assert res["start_line"] == 1
    assert res["end_line"] == 150
    assert res["total_lines"] == 200
    assert res["truncated"] is True
    assert res["content"].startswith("line 001")
    assert res["content"].endswith("line 150")

    # 2. Read with specific slice
    call_slice = _make_call({"op": "read", "path": "test.txt", "start_line": 151, "max_lines": 60})
    res_slice, _ = dispatch_repo_read_tool(call_slice, "workspace-test", "err")
    assert res_slice is not None
    assert res_slice["start_line"] == 151
    assert res_slice["end_line"] == 200
    assert res_slice["total_lines"] == 200
    assert res_slice["truncated"] is False
    assert res_slice["content"].startswith("line 151")
    assert res_slice["content"].endswith("line 200")

    # 3. Read beyond total lines
    call_beyond = _make_call({"op": "read", "path": "test.txt", "start_line": 500})
    res_beyond, _ = dispatch_repo_read_tool(call_beyond, "workspace-test", "err")
    assert res_beyond is not None
    assert res_beyond["content"] == ""
    assert res_beyond["truncated"] is False

    # 4. Truncation at 16,000 characters
    call_long = _make_call({"op": "read", "path": "long.txt", "max_lines": 300})
    res_long, _ = dispatch_repo_read_tool(call_long, "workspace-test", "err")
    assert res_long is not None
    assert res_long["truncated"] is True
    assert len(res_long["content"]) == 16_000


# ---------------------------------------------------------------------------
# Scope Pack: loaded pack, missing pack, ambiguous pack, cloud egress refusal
# ---------------------------------------------------------------------------


def test_op_pack_with_fake_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack_dir = tmp_path / "my_pack"
    pack_dir.mkdir()
    (pack_dir / "jarvis_pack.toml").write_text('id = "fake_zephyr"\n', encoding="utf-8")
    (pack_dir / "models").mkdir()
    (pack_dir / "models" / "equations.py").write_text("RULE_K = 3.14\n", encoding="utf-8")
    (pack_dir / "README.md").write_text("# Fake Pack Documentation\nSecret details here.\n", encoding="utf-8")

    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack_dir))
    grant = _make_grant(route_class="local:llamacpp")

    # 1. List pack files
    call_list = _make_call({"op": "list", "scope": "pack"})
    res_list, _ = dispatch_repo_read_tool(call_list, "workspace-test", "err", grant=grant)
    assert res_list is not None
    assert res_list["op"] == "list"
    assert res_list["scope"] == "pack"
    assert res_list["pack_id"] == "fake_zephyr"
    assert res_list["confidentiality"] == "private_pack"
    assert "README.md" in res_list["files"]
    assert "models/equations.py" in res_list["files"]
    assert "jarvis_pack.toml" in res_list["files"]

    # 2. Search pack files
    call_search = _make_call({"op": "search", "scope": "pack", "query": "RULE_K"})
    res_search, _ = dispatch_repo_read_tool(call_search, "workspace-test", "err", grant=grant)
    assert res_search is not None
    assert res_search["count"] == 1
    assert res_search["matches"][0]["path"] == "models/equations.py"
    assert "RULE_K = 3.14" in res_search["matches"][0]["snippet"]
    assert res_search["confidentiality"] == "private_pack"

    # 3. Read pack file
    call_read = _make_call({"op": "read", "scope": "pack", "path": "models/equations.py"})
    res_read, _ = dispatch_repo_read_tool(call_read, "workspace-test", "err", grant=grant)
    assert res_read is not None
    assert res_read["op"] == "read"
    assert res_read["scope"] == "pack"
    assert res_read["pack_id"] == "fake_zephyr"
    assert res_read["confidentiality"] == "private_pack"
    assert res_read["content"] == "RULE_K = 3.14"


def test_op_pack_not_configured_when_no_pack_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(packs.PACK_PATH_ENV, "")
    grant = _make_grant(route_class="local:llamacpp")

    call = _make_call({"op": "list", "scope": "pack"})
    res, _ = dispatch_repo_read_tool(call, "workspace-test", "err", grant=grant)
    assert res is not None
    assert res["scope"] == "pack"
    assert res["pack_id"] is None
    assert res["confidentiality"] == "private_pack"
    assert res["error_code"] == "pack_not_configured"


def test_op_pack_multiple_packs_and_pack_id_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack1 = tmp_path / "pack1"
    pack1.mkdir()
    (pack1 / "jarvis_pack.toml").write_text('id = "alpha"\n', encoding="utf-8")
    (pack1 / "a.txt").write_text("file in alpha\n", encoding="utf-8")

    pack2 = tmp_path / "pack2"
    pack2.mkdir()
    (pack2 / "jarvis_pack.toml").write_text('id = "beta"\n', encoding="utf-8")
    (pack2 / "b.txt").write_text("file in beta\n", encoding="utf-8")

    monkeypatch.setenv(packs.PACK_PATH_ENV, f"{pack1}{os.pathsep}{pack2}")
    grant = _make_grant(route_class="local:llamacpp")

    # 1. Missing pack_id when multiple packs loaded returns ambiguous_pack_id
    call_ambig = _make_call({"op": "list", "scope": "pack"})
    res_ambig, _ = dispatch_repo_read_tool(call_ambig, "workspace-test", "err", grant=grant)
    assert res_ambig is not None
    assert res_ambig["error_code"] == "ambiguous_pack_id"

    # 2. Explicit pack_id="alpha" selects pack1
    call_alpha = _make_call({"op": "list", "scope": "pack", "pack_id": "alpha"})
    res_alpha, _ = dispatch_repo_read_tool(call_alpha, "workspace-test", "err", grant=grant)
    assert res_alpha is not None
    assert res_alpha["pack_id"] == "alpha"
    assert "a.txt" in res_alpha["files"]

    # 3. Explicit pack_id="beta" selects pack2
    call_beta = _make_call({"op": "list", "scope": "pack", "pack_id": "beta"})
    res_beta, _ = dispatch_repo_read_tool(call_beta, "workspace-test", "err", grant=grant)
    assert res_beta is not None
    assert res_beta["pack_id"] == "beta"
    assert "b.txt" in res_beta["files"]

    # 4. Unknown pack_id returns pack_not_found
    call_unknown = _make_call({"op": "list", "scope": "pack", "pack_id": "gamma"})
    res_unknown, _ = dispatch_repo_read_tool(call_unknown, "workspace-test", "err", grant=grant)
    assert res_unknown is not None
    assert res_unknown["error_code"] == "pack_not_found"


def test_op_pack_cloud_egress_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack_dir = tmp_path / "zephyr_pack"
    pack_dir.mkdir()
    (pack_dir / "jarvis_pack.toml").write_text('id = "pack_zephyr"\n', encoding="utf-8")
    (pack_dir / "file.txt").write_text("secret\n", encoding="utf-8")
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack_dir))

    # Cloud routes must fail closed with pack_cloud_egress_blocked
    for cloud_route in ("cloud:openai", "external:provider", "scaleway", "deepseek", "remote:llm"):
        grant = _make_grant(route_class=cloud_route)
        call = _make_call({"op": "list", "scope": "pack"})
        res, err = dispatch_repo_read_tool(call, "workspace-test", "err", grant=grant)
        assert res is None
        assert err == "egress_pack_blocked"

        # Direct argument route_class also refuses
        res_direct, err_direct = dispatch_repo_read_tool(
            call, "workspace-test", "err", route_class=cloud_route
        )
        assert res_direct is None
        assert err_direct == "egress_pack_blocked"

    # Public scope remains allowed on cloud route
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "public.txt").write_text("public content\n", encoding="utf-8")
    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)
    cloud_grant = _make_grant(route_class="cloud:openai")
    call_pub = _make_call({"op": "read", "scope": "public", "path": "public.txt"})
    res_pub, _ = dispatch_repo_read_tool(call_pub, "workspace-test", "err", grant=cloud_grant)
    assert res_pub is not None
    assert res_pub["scope"] == "public"
    assert res_pub["content"] == "public content"


# ---------------------------------------------------------------------------
# Strict Containment: Traversal, Symlinks, Secrets, Binary
# ---------------------------------------------------------------------------


def test_containment_path_traversal_and_absolute_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "good.txt").write_text("good\n", encoding="utf-8")
    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)

    bad_paths = [
        "../outside.txt",
        "../../etc/passwd",
        "/etc/passwd",
        "/home/example/secret.txt",
        "good.txt/../../secret",
        "foo/bar/../../../outside",
        "path\\with\\backslash",
        "path\x00withnul",
    ]
    for bad_path in bad_paths:
        call_read = _make_call({"op": "read", "path": bad_path})
        res_read, err_read = dispatch_repo_read_tool(call_read, "workspace-test", "err")
        assert res_read is None
        assert err_read == "invalid_arguments"

        call_list = _make_call({"op": "list", "path": bad_path})
        res_list, err_list = dispatch_repo_read_tool(call_list, "workspace-test", "err")
        assert res_list is None
        assert err_list == "invalid_arguments"


def test_containment_symlinks_outside_scope_root_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside_file = tmp_path / "outside_secret.txt"
    outside_file.write_text("outside secret", encoding="utf-8")

    symlink_file = repo / "escaped.txt"
    try:
        symlink_file.symlink_to(outside_file)
        monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)

        call = _make_call({"op": "read", "path": "escaped.txt"})
        res, err = dispatch_repo_read_tool(call, "workspace-test", "err")
        assert res is None
        assert err == "invalid_arguments"

        # In op="list", the escaped symlink must not be included
        call_list = _make_call({"op": "list"})
        res_list, _ = dispatch_repo_read_tool(call_list, "workspace-test", "err")
        assert res_list is not None
        assert "escaped.txt" not in res_list["files"]
    finally:
        if symlink_file.is_symlink() or symlink_file.exists():
            symlink_file.unlink()


def test_containment_secret_files_refused_and_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "normal.txt").write_text("normal\n", encoding="utf-8")

    # Various secret-like files
    secret_files = [
        ".env",
        ".env.local",
        "server.pem",
        "cert.key",
        "id_rsa",
        "id_rsa.pub",
        "my_secret_token.txt",
        "credentials.json",
        "database.sqlite3",
        "app.sqlite",
    ]
    for s_name in secret_files:
        (repo / s_name).write_text("secret dummy content", encoding="utf-8")

    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)

    # Reading any secret file is refused
    for s_name in secret_files:
        call = _make_call({"op": "read", "path": s_name})
        res, err = dispatch_repo_read_tool(call, "workspace-test", "err")
        assert res is None
        assert err == "invalid_arguments"

    # Listing must skip all secret files
    call_list = _make_call({"op": "list"})
    res_list, _ = dispatch_repo_read_tool(call_list, "workspace-test", "err")
    assert res_list is not None
    assert res_list["files"] == ["normal.txt"]

    # Searching must skip all secret files
    call_search = _make_call({"op": "search", "query": "dummy"})
    res_search, _ = dispatch_repo_read_tool(call_search, "workspace-test", "err")
    assert res_search is not None
    assert res_search["count"] == 0


def test_containment_binary_files_skipped_and_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "text.txt").write_text("regular text\n", encoding="utf-8")
    (repo / "data.bin").write_bytes(b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00")
    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)

    # Binary file refused on read
    call_read = _make_call({"op": "read", "path": "data.bin"})
    res_read, err_read = dispatch_repo_read_tool(call_read, "workspace-test", "err")
    assert res_read is None
    assert err_read == "invalid_arguments"

    # Binary file skipped on list
    call_list = _make_call({"op": "list"})
    res_list, _ = dispatch_repo_read_tool(call_list, "workspace-test", "err")
    assert res_list is not None
    assert "data.bin" not in res_list["files"]
    assert "text.txt" in res_list["files"]

    # Binary file skipped on search
    call_search = _make_call({"op": "search", "query": "ELF"})
    res_search, _ = dispatch_repo_read_tool(call_search, "workspace-test", "err")
    assert res_search is not None
    assert res_search["count"] == 0


# ---------------------------------------------------------------------------
# Grant Enforcement & Supervisor dispatch_tool
# ---------------------------------------------------------------------------


def test_dispatch_tool_enforces_repo_read_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "file.txt").write_text("hello repo\n", encoding="utf-8")
    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)

    # 1. Valid grant succeeds
    grant = _make_grant(grant_id="grant-1")
    call = _make_call({"op": "read", "path": "file.txt"}, grant_id="grant-1")
    res = dispatch_tool(call, live_grants={"grant-1": grant})
    assert res.status == "succeeded"
    assert res.result is not None
    assert res.result["content"] == "hello repo"

    # 2. Missing grant refused
    res_missing = dispatch_tool(call, live_grants={})
    assert res_missing.status == "refused"
    assert res_missing.error_code == "capability_denied"

    # 3. Wrong capability grant refused
    wrong_grant = _make_grant(grant_id="grant-wrong", capability_id="jarvis.dev_read")
    call_wrong = _make_call({"op": "read", "path": "file.txt"}, grant_id="grant-wrong")
    res_wrong = dispatch_tool(call_wrong, live_grants={"grant-wrong": wrong_grant})
    assert res_wrong.status == "refused"
    assert res_wrong.error_code == "capability_denied"

    # 4. Expired grant refused
    expired_grant = _make_grant(grant_id="grant-expired", expired=True)
    call_expired = _make_call({"op": "read", "path": "file.txt"}, grant_id="grant-expired")
    res_expired = dispatch_tool(call_expired, live_grants={"grant-expired": expired_grant})
    assert res_expired.status == "refused"
    assert res_expired.error_code == "grant_expired"

    # 5. Cloud route with pack scope refused via dispatch_tool
    cloud_grant = _make_grant(grant_id="grant-cloud", route_class="cloud:openai")
    call_cloud = _make_call({"op": "list", "scope": "pack"}, grant_id="grant-cloud")
    res_cloud = dispatch_tool(call_cloud, live_grants={"grant-cloud": cloud_grant})
    assert res_cloud.status == "refused"
    assert res_cloud.error_code == "egress_pack_blocked"


# ---------------------------------------------------------------------------
# Prompt Guidance and Instruction Checks
# ---------------------------------------------------------------------------


def test_prompt_advertisement_and_thread_instructions() -> None:
    # 1. context_builder.THREAD_AUTHORITY_INSTRUCTIONS mentions jarvis_repo_read
    assert "jarvis_repo_read" in context_builder.THREAD_AUTHORITY_INSTRUCTIONS

    # 2. thread_service._install_hermes_retrieval_grant advertises jarvis_repo_read
    payload = AIThreadSubmit(
        request_id="req-1",
        prompt="Where is coefficient ALPHA defined?",
        route_class="hermes:agent",
    )
    worker = SimpleNamespace(live_grants={}, session=_SESSION)
    text = thread_service._install_hermes_retrieval_grant(worker, payload, "workspace-test", "thread-test")
    assert text is not None
    assert "repo_read_grant_id=" in text
    assert "jarvis_repo_read scopes: public, pack; ops: list, search, read" in text
    assert "jarvis.repo_read" in [g.capability_id for g in worker.live_grants.values()]

    # 3. _turn_tools includes mcp__jarvis__jarvis_repo_read
    tools = thread_service._turn_tools("workspace", "Where is the coefficient in the repository?")
    assert "mcp__jarvis__jarvis_repo_read" in tools


def test_containment_internal_symlinks_to_git_and_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "safe.txt").write_text("safe_marker_value\n", encoding="utf-8")
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("git_internal_marker\n", encoding="utf-8")
    (repo / ".env").write_text("ENV_SECRET_MARKER=1\n", encoding="utf-8")

    (repo / "exposed_git_cfg.txt").symlink_to(repo / ".git" / "config")
    (repo / "exposed_git_dir").symlink_to(repo / ".git")
    (repo / "exposed_env.txt").symlink_to(repo / ".env")
    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)

    # 1. Direct read via internal symlink to .git/config or .env is refused
    for bad_link in ("exposed_git_cfg.txt", "exposed_env.txt", "exposed_git_dir/config"):
        res_read, err_read = dispatch_repo_read_tool(_make_call({"op": "read", "path": bad_link}), "workspace-test", "err")
        assert res_read is None
        assert err_read == "invalid_arguments"

    # 2. Direct list of symlinked .git dir is refused
    res_list_git, err_list_git = dispatch_repo_read_tool(
        _make_call({"op": "list", "path": "exposed_git_dir"}), "workspace-test", "err"
    )
    assert res_list_git is None
    assert err_list_git == "invalid_arguments"

    # 3. Root list excludes symlinks resolving into .git or secret files
    res_list, _ = dispatch_repo_read_tool(_make_call({"op": "list"}), "workspace-test", "err")
    assert res_list is not None
    assert res_list["files"] == ["safe.txt"]

    # 4. Root search does not read or leak content from symlinks resolving into .git or .env
    res_search_env, _ = dispatch_repo_read_tool(
        _make_call({"op": "search", "query": "ENV_SECRET_MARKER"}), "workspace-test", "err"
    )
    assert res_search_env is not None
    assert res_search_env["count"] == 0

    res_search_git, _ = dispatch_repo_read_tool(
        _make_call({"op": "search", "query": "git_internal_marker"}), "workspace-test", "err"
    )
    assert res_search_git is not None
    assert res_search_git["count"] == 0


def test_is_local_route_fail_closed_when_route_missing_or_unlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.agents.hermes.repo_read_tool import _is_local_route
    from app.modules.ai import settings as ai_settings

    # Unspecified wrapper route is not blindly treated as local
    assert _is_local_route("hermes:agent", None) is False

    # Unlisted cloud provider_mode fails closed when candidate_route is None
    monkeypatch.setattr(ai_settings, "get_ai_settings", lambda: SimpleNamespace(provider_mode="openrouter"))
    assert _is_local_route(None, None) is False

    # Exception reading settings fails closed
    def _boom() -> Any:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(ai_settings, "get_ai_settings", _boom)
    assert _is_local_route(None, None) is False

    # Allowlisted local/fake provider_mode succeeds when candidate_route is None
    monkeypatch.setattr(ai_settings, "get_ai_settings", lambda: SimpleNamespace(provider_mode="fake"))
    assert _is_local_route(None, None) is True


def test_install_hermes_retrieval_grant_resolves_role_route_for_hermes_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.ai import settings as ai_settings

    pack_dir = tmp_path / "zephyr_pack"
    pack_dir.mkdir()
    (pack_dir / "jarvis_pack.toml").write_text('id = "pack_zephyr"\n', encoding="utf-8")
    (pack_dir / "model.py").write_text("K = 42\n", encoding="utf-8")
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack_dir))

    # 1. Local role route resolved from settings allows pack read on hermes:agent turn
    monkeypatch.setattr(
        ai_settings,
        "get_ai_settings",
        lambda: SimpleNamespace(hermes_role_routes={"isaac": "local:llamacpp"}),
    )
    payload = AIThreadSubmit(request_id="req-local", prompt="Read pack", route_class="hermes:agent")
    worker = SimpleNamespace(live_grants={}, session=_SESSION)
    thread_service._install_hermes_retrieval_grant(worker, payload, "workspace-test", "thread-test")
    repo_grant = next(g for g in worker.live_grants.values() if g.capability_id == "jarvis.repo_read")
    assert repo_grant.constraints.get("route_class") == "local:llamacpp"

    res_ok, _ = dispatch_repo_read_tool(
        _make_call({"op": "read", "scope": "pack", "path": "model.py"}, grant_id=repo_grant.grant_id),
        "workspace-test",
        "err",
        grant=repo_grant,
    )
    assert res_ok is not None
    assert res_ok["content"] == "K = 42"

    # 2. Cloud role route resolved from settings blocks pack read on hermes:agent turn
    monkeypatch.setattr(
        ai_settings,
        "get_ai_settings",
        lambda: SimpleNamespace(hermes_role_routes={"isaac": "cloud:scaleway"}),
    )
    worker_cloud = SimpleNamespace(live_grants={}, session=_SESSION)
    thread_service._install_hermes_retrieval_grant(worker_cloud, payload, "workspace-test", "thread-test")
    cloud_grant = next(g for g in worker_cloud.live_grants.values() if g.capability_id == "jarvis.repo_read")
    assert cloud_grant.constraints.get("route_class") == "cloud:scaleway"

    res_blocked, err_blocked = dispatch_repo_read_tool(
        _make_call({"op": "read", "scope": "pack", "path": "model.py"}, grant_id=cloud_grant.grant_id),
        "workspace-test",
        "err",
        grant=cloud_grant,
    )
    assert res_blocked is None
    assert err_blocked == "egress_pack_blocked"
