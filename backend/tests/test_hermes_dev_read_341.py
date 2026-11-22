"""Tests for Spec 341 — JARVIS-DEV-READ-TOOLS-1 (Hermes dev_read read-only tool)."""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.modules.agents.hermes import broker_mcp
from app.modules.agents.hermes.dev_read_tool import dispatch_dev_read_tool
from app.modules.agents.hermes.supervisor import (
    _HERMES_DEV_READ_CAPABILITY,
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
from app.modules.coding import provider_quota, relay_monitor

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
    capability_id: str = "jarvis.dev_read",
    grant_id: str = "grant-dev-read",
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
    grant_id: str = "grant-dev-read",
    capability_id: str = "jarvis.dev_read",
    workspace_id: str = "workspace-test",
    thread_id: str = "thread-test",
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
        issued_at=issued,
        expires_at=expires,
    )


# ---------------------------------------------------------------------------
# Registration and Schema Checks
# ---------------------------------------------------------------------------


def test_dev_read_tool_registered_in_allowlists_and_broker() -> None:
    """Acceptance criterion 1: jarvis_dev_read is registered in all allowlists and schemas."""
    # 1. broker_mcp._TOOLS contains jarvis_dev_read with strict JSON schema
    dev_tool = next((t for t in broker_mcp._TOOLS if t["name"] == "jarvis_dev_read"), None)
    assert dev_tool is not None
    schema = dev_tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["grant_id", "op"]
    assert set(schema["properties"]["op"]["enum"]) == {
        "specs", "spec_text", "recent_work", "plan", "supervisor", "directives", "quotas", "runtime_health",
    }
    assert schema["properties"]["limit"]["minimum"] == 1
    assert schema["properties"]["limit"]["maximum"] == 50

    # 2. supervisor._TOOL_CAPABILITIES maps jarvis_dev_read -> jarvis.dev_read
    assert _TOOL_CAPABILITIES.get("jarvis_dev_read") == "jarvis.dev_read"

    # 3. supervisor.HERMES_TOOL_ALLOWLIST contains mcp__jarvis__jarvis_dev_read
    assert "mcp__jarvis__jarvis_dev_read" in HERMES_TOOL_ALLOWLIST

    # 4. PRODUCTION_CAPABILITY_REGISTRY has jarvis.dev_read registered with action_class="READ"
    desc = next(
        (c for c in PRODUCTION_CAPABILITY_REGISTRY.for_route("ai-threads") if c.capability_id == "jarvis.dev_read"),
        None,
    )
    assert desc is not None
    assert desc.action_class == "READ"
    assert desc == _HERMES_DEV_READ_CAPABILITY


# ---------------------------------------------------------------------------
# Op 1: specs
# ---------------------------------------------------------------------------


def test_op_specs_with_filter_and_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    specs_dir = tmp_path / "docs" / "specs"
    specs_dir.mkdir(parents=True)
    status_content = """# Spec Registry

## Registry

| Spec | Status | Implementation PRs | Name | Depends on | Description |
|---|---|---|---|---|---|
| 341 | ready | | JARVIS-DEV-READ-TOOLS-1 | none | Read-only dev tools |
| 340 | in_progress | [#10](/pull/10) | AGENT-LANE-LOCK-1 | 339 | Lane lock |
| 339 | merged | [#9](/pull/9), [#8](/pull/8) | SUPERVISOR-RELOAD-1 | none | Supervisor reload |
"""
    (specs_dir / "STATUS.md").write_text(status_content, encoding="utf-8")
    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: tmp_path)

    # 1. Unfiltered (default limit 20)
    call = _make_call({"op": "specs"})
    res, err = dispatch_dev_read_tool(call, "workspace-test", "err")
    assert res is not None
    assert res["op"] == "specs"
    assert res["total"] == 3
    assert res["count"] == 3
    assert [s["spec_id"] for s in res["specs"]] == ["341", "340", "339"]
    assert res["specs"][0]["pr_numbers"] == []
    assert res["specs"][1]["pr_numbers"] == [10]
    assert res["specs"][2]["pr_numbers"] == [9, 8]

    # 2. Filter by status
    call_status = _make_call({"op": "specs", "status": "merged"})
    res_status, _ = dispatch_dev_read_tool(call_status, "workspace-test", "err")
    assert res_status is not None
    assert res_status["total"] == 1
    assert res_status["count"] == 1
    assert res_status["specs"][0]["spec_id"] == "339"

    # 3. Filter by spec_id
    call_id = _make_call({"op": "specs", "spec_id": "341"})
    res_id, _ = dispatch_dev_read_tool(call_id, "workspace-test", "err")
    assert res_id is not None
    assert res_id["total"] == 1
    assert res_id["specs"][0]["name"] == "JARVIS-DEV-READ-TOOLS-1"

    # 4. Limit bounding
    call_lim = _make_call({"op": "specs", "limit": 2})
    res_lim, _ = dispatch_dev_read_tool(call_lim, "workspace-test", "err")
    assert res_lim is not None
    assert res_lim["total"] == 3
    assert res_lim["count"] == 2
    assert len(res_lim["specs"]) == 2


# ---------------------------------------------------------------------------
# Op 2: spec_text
# ---------------------------------------------------------------------------


def test_op_spec_text_success_truncation_and_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    specs_dir = tmp_path / "docs" / "specs"
    specs_dir.mkdir(parents=True)
    short_content = "# 341 — Title\nBody of spec 341"
    (specs_dir / "341-jarvis-dev-read.md").write_text(short_content, encoding="utf-8")

    long_content = "A" * 20_000
    (specs_dir / "340-long-spec.md").write_text(long_content, encoding="utf-8")

    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: tmp_path)

    # 1. Short spec read
    call = _make_call({"op": "spec_text", "spec_id": "341"})
    res, _ = dispatch_dev_read_tool(call, "workspace-test", "err")
    assert res is not None
    assert res["op"] == "spec_text"
    assert res["spec_id"] == "341"
    assert res["filename"] == "341-jarvis-dev-read.md"
    assert res["truncated"] is False
    assert res["content"] == short_content

    # 2. Long spec truncated to 16,000 chars
    call_long = _make_call({"op": "spec_text", "spec_id": "340"})
    res_long, _ = dispatch_dev_read_tool(call_long, "workspace-test", "err")
    assert res_long is not None
    assert res_long["truncated"] is True
    assert len(res_long["content"]) == 16_000

    # 3. Path traversal refusal
    for bad_id in ("../secret", "../../etc/passwd", "341/foo", "341..", "", "a12345", "tool!bar"):
        call_bad = _make_call({"op": "spec_text", "spec_id": bad_id})
        res_bad, err_bad = dispatch_dev_read_tool(call_bad, "workspace-test", "err")
        assert res_bad is None
        assert err_bad == "invalid_arguments"

    # 4. Non-existent spec id returns empty content without error
    call_missing = _make_call({"op": "spec_text", "spec_id": "999"})
    res_missing, _ = dispatch_dev_read_tool(call_missing, "workspace-test", "err")
    assert res_missing is not None
    assert res_missing["filename"] is None
    assert res_missing["content"] == ""

    # 5. Symlink escaping refusal
    outside = tmp_path / "outside.md"
    outside.write_text("secret outside", encoding="utf-8")
    symlink_file = specs_dir / "100-escaped.md"
    try:
        symlink_file.symlink_to(outside)
        call_esc = _make_call({"op": "spec_text", "spec_id": "100"})
        res_esc, err_esc = dispatch_dev_read_tool(call_esc, "workspace-test", "err")
        assert res_esc is None
        assert err_esc == "invalid_arguments"
    finally:
        if symlink_file.is_symlink() or symlink_file.exists():
            symlink_file.unlink()


# ---------------------------------------------------------------------------
# Op 3: recent_work
# ---------------------------------------------------------------------------


def test_op_recent_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.invalid"], cwd=repo, check=True)

    # Commit 1
    (repo / "file1.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Merge pull request #10 from bot/spec-340\n\nspecs 340"], cwd=repo, check=True)

    # Commit 2
    (repo / "file2.txt").write_text("world", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: add read tools (#11)\n\nspecs 341"], cwd=repo, check=True)

    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)

    call = _make_call({"op": "recent_work", "limit": 10})
    res, _ = dispatch_dev_read_tool(call, "workspace-test", "err")
    assert res is not None
    assert res["op"] == "recent_work"
    assert res["count"] == 2
    assert [item["pr_number"] for item in res["items"]] == [11, 10]


# ---------------------------------------------------------------------------
# Op 4: plan
# ---------------------------------------------------------------------------


def test_op_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    plan_data = {
        "schema": "plan.v1",
        "updated_at": "2026-11-22T10:00:00Z",
        "goal": "Build read-only dev tools",
        "steps": [
            {"title": "Spec 341 implementation", "state": "in_progress", "note": "Hermes dev_read", "specs": ["341"]},
        ],
    }
    (control_dir / "plan.json").write_text(json.dumps(plan_data), encoding="utf-8")
    monkeypatch.setattr(relay_monitor, "control_root", lambda: control_dir)
    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: tmp_path)

    call = _make_call({"op": "plan"})
    res, _ = dispatch_dev_read_tool(call, "workspace-test", "err")
    assert res is not None
    assert res["op"] == "plan"
    assert res["status"] == "available"
    assert res["goal"] == "Build read-only dev tools"
    assert len(res["steps"]) == 1
    assert res["steps"][0]["title"] == "Spec 341 implementation"


# ---------------------------------------------------------------------------
# Op 5: supervisor (scrubbing host paths and secrets)
# ---------------------------------------------------------------------------


def test_op_supervisor_scrubs_secrets_and_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_snapshot = {
        "state": "running",
        "mission": "Deploying at /home/example/src/worktrees/k341impl",
        "home_path": str(Path.home() / "secret_dir"),
        "key": "sk-live-1234567890abcdef1234567890",
        "safe_value": 42,
    }
    monkeypatch.setattr(relay_monitor, "snapshot", lambda: fake_snapshot)

    call = _make_call({"op": "supervisor"})
    res, _ = dispatch_dev_read_tool(call, "workspace-test", "err")
    assert res is not None
    assert res["op"] == "supervisor"
    # Ensure raw home path is replaced
    assert str(Path.home()) not in json.dumps(res)
    # Ensure raw secret is scrubbed
    assert "sk-live-1234567890abcdef1234567890" not in json.dumps(res)
    assert res["safe_value"] == 42


# ---------------------------------------------------------------------------
# Op 6: directives (ledger reading & scrubbing)
# ---------------------------------------------------------------------------


def test_op_directives_newest_first_and_superseded_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    devctx_dir = tmp_path / ".agent-relay" / "devctx"
    devctx_dir.mkdir(parents=True)
    ledger_path = devctx_dir / "ledger.jsonl"

    entries = [
        {
            "id": "dir-1",
            "ts": "2026-11-20T10:00:00Z",
            "kind": "directive",
            "status": "accepted",
            "text": "Old directive with /home/example/path",
            "directive": {"path": "/home/example/private/dir-1.md", "source": "relay"},
        },
        {
            "id": "dir-2",
            "ts": "2026-11-20T12:00:00Z",
            "kind": "directive",
            "status": "accepted",
            "text": "New active directive sk-live-1234567890abcdef1234567890",
            "supersedes": ["dir-1"],
            "directive": {"path": "/home/example/private/dir-2.md", "source": "maintainer"},
        },
        {
            "id": "note-1",
            "ts": "2026-11-21T08:00:00Z",
            "kind": "note",
            "status": "accepted",
            "text": "Note about progress",
        },
    ]
    with ledger_path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: tmp_path)

    call = _make_call({"op": "directives", "limit": 10})
    res, _ = dispatch_dev_read_tool(call, "workspace-test", "err")
    assert res is not None
    assert res["op"] == "directives"
    # dir-1 is superseded by dir-2, so only dir-2 should be in active directives
    assert len(res["directives"]) == 1
    assert res["directives"][0]["id"] == "dir-2"
    assert res["directives"][0]["source"] == "maintainer"

    # Verify notes
    assert len(res["notes"]) == 1
    assert res["notes"][0]["id"] == "note-1"

    # Verify path and secret scrubbing in text and omitted filesystem paths
    res_json = json.dumps(res)
    assert "/home/example" not in res_json
    assert "sk-live-1234567890abcdef1234567890" not in res_json
    assert "/private/dir-2.md" not in res_json


# ---------------------------------------------------------------------------
# Op 7: quotas (normalized and free pools without secrets)
# ---------------------------------------------------------------------------


def test_op_quotas_and_free_pools_scrubbed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    pools_data = {
        "pools": [
            {
                "id": "pool-zen",
                "label": "Zen Free Pool",
                "tier": 2,
                "status": "admitted",
                "index_score": 45,
                "api_key": "secret-zen-key-123",
                "key_file": "/home/example/.secrets/zen.key",
            },
        ],
    }
    (control_dir / "temp-pools.json").write_text(json.dumps(pools_data), encoding="utf-8")

    fake_readings = {
        "relay": {"agents": []},
        "agy": None,
        "codex": None,
        "copilot": None,
        "errors": {},
    }
    monkeypatch.setattr(provider_quota, "read_all", lambda refresh=False: fake_readings)
    monkeypatch.setattr(relay_monitor, "control_root", lambda: control_dir)

    call = _make_call({"op": "quotas"})
    res, _ = dispatch_dev_read_tool(call, "workspace-test", "err")
    assert res is not None
    assert res["op"] == "quotas"
    assert "quotas" in res
    assert len(res["free_pools"]) == 1
    pool = res["free_pools"][0]
    assert pool["id"] == "pool-zen"
    assert pool["label"] == "Zen Free Pool"
    assert pool["tier"] == 2
    assert pool["status"] == "admitted"
    assert pool["index_score"] == 45
    # Strict secret omission
    assert "api_key" not in pool
    assert "key_file" not in pool
    assert "secret-zen-key-123" not in json.dumps(res)


# ---------------------------------------------------------------------------
# Op 8: runtime_health
# ---------------------------------------------------------------------------


def test_op_runtime_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.invalid"], cwd=repo, check=True)
    (repo / "f.txt").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo)

    call = _make_call({"op": "runtime_health"})
    res, _ = dispatch_dev_read_tool(call, "workspace-test", "err")
    assert res is not None
    assert res["op"] == "runtime_health"
    assert res["hermes_supervisor"]["status"] == "running"
    assert res["hermes_supervisor"]["generation"] == 1
    assert res["repository"]["status"] == "ok"
    assert res["repository"]["branch"] == "main"
    assert len(res["repository"]["head_sha"]) == 40


# ---------------------------------------------------------------------------
# Argument Validation & Refusals
# ---------------------------------------------------------------------------


def test_argument_validation_and_refusals() -> None:
    # 1. Missing op
    call_no_op = _make_call({})
    res, err = dispatch_dev_read_tool(call_no_op, "workspace-test", "err")
    assert res is None and err == "invalid_arguments"

    # 2. Unknown op
    call_unknown = _make_call({"op": "nonexistent"})
    res, err = dispatch_dev_read_tool(call_unknown, "workspace-test", "err")
    assert res is None and err == "invalid_arguments"

    # 3. Unexpected argument keys
    call_extra = _make_call({"op": "plan", "extra_key": "bad"})
    res, err = dispatch_dev_read_tool(call_extra, "workspace-test", "err")
    assert res is None and err == "invalid_arguments"

    # 4. Out-of-range limit for specs (1..50)
    call_limit_0 = _make_call({"op": "specs", "limit": 0})
    res, err = dispatch_dev_read_tool(call_limit_0, "workspace-test", "err")
    assert res is None and err == "invalid_arguments"

    call_limit_51 = _make_call({"op": "specs", "limit": 51})
    res, err = dispatch_dev_read_tool(call_limit_51, "workspace-test", "err")
    assert res is None and err == "invalid_arguments"

    # 5. Out-of-range limit for recent_work (1..30)
    call_rw_limit = _make_call({"op": "recent_work", "limit": 35})
    res, err = dispatch_dev_read_tool(call_rw_limit, "workspace-test", "err")
    assert res is None and err == "invalid_arguments"

    # 6. Non-int limit (bool is rejected)
    call_bool = _make_call({"op": "specs", "limit": True})
    res, err = dispatch_dev_read_tool(call_bool, "workspace-test", "err")
    assert res is None and err == "invalid_arguments"


# ---------------------------------------------------------------------------
# Full Dispatch Tool & Grant Security
# ---------------------------------------------------------------------------


def test_dispatch_tool_grant_enforcement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    specs_dir = tmp_path / "docs" / "specs"
    specs_dir.mkdir(parents=True)
    (specs_dir / "STATUS.md").write_text(
        "# Spec Registry\n\n## Registry\n\n| Spec | Status | Implementation PRs | Name | Depends on | Description |\n|---|---|---|---|---|---|\n| 341 | ready | | READ | none | desc |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: tmp_path)

    grant = _make_grant("grant-dev-1")
    call = _make_call({"op": "specs"}, grant_id="grant-dev-1")

    # 1. Valid grant succeeds
    res_ok = dispatch_tool(call, live_grants={"grant-dev-1": grant})
    assert res_ok.status == "succeeded"
    assert res_ok.result is not None
    assert res_ok.result["op"] == "specs"

    # 2. Missing grant fails
    res_no_grant = dispatch_tool(call, live_grants={})
    assert res_no_grant.status == "refused"
    assert res_no_grant.error_code == "capability_denied"

    # 3. Expired grant fails
    expired_grant = _make_grant("grant-dev-1", expired=True)
    res_expired = dispatch_tool(call, live_grants={"grant-dev-1": expired_grant})
    assert res_expired.status == "refused"

    # 4. Workspace mismatch fails
    other_ws = _make_grant("grant-dev-1", workspace_id="workspace-other")
    res_other = dispatch_tool(call, live_grants={"grant-dev-1": other_ws})
    assert res_other.status == "refused"

    # 5. Capability mismatch fails
    other_cap = _make_grant("grant-dev-1", capability_id="jarvis.retrieval_query")
    res_cap = dispatch_tool(call, live_grants={"grant-dev-1": other_cap})
    assert res_cap.status == "refused"


# ---------------------------------------------------------------------------
# Turn Grant & Prompt Guidance
# ---------------------------------------------------------------------------


def test_turn_grant_issuance_and_prompt_guidance() -> None:
    worker = SimpleNamespace(live_grants={})
    payload = AIThreadSubmit(
        request_id="req-1",
        prompt="Tell me about recent PRs",
        route_class="hermes:agent",
    )
    text = thread_service._install_hermes_retrieval_grant(worker, payload, "workspace-test", "thread-test")
    assert text is not None

    # Verify jarvis.dev_read grant is in worker.live_grants
    dev_grant = next((g for g in worker.live_grants.values() if g.capability_id == "jarvis.dev_read"), None)
    assert dev_grant is not None
    assert dev_grant.scope.workspace_id == "workspace-test"
    assert dev_grant.scope.jarvis_thread_id == "thread-test"

    # Verify guidance mentions grant_id, ops, and instructions to check live state
    assert f"dev_read_grant_id={dev_grant.grant_id}" in text
    assert "specs, spec_text, recent_work, plan, supervisor, directives, quotas, runtime_health" in text
    assert "Use jarvis_dev_read to inspect live development" in text

    # Verify THREAD_AUTHORITY_INSTRUCTIONS instructs using read-only tools before stating no access
    assert "jarvis_dev_read" in context_builder.THREAD_AUTHORITY_INSTRUCTIONS
    assert "jarvis_retrieval_query" in context_builder.THREAD_AUTHORITY_INSTRUCTIONS
