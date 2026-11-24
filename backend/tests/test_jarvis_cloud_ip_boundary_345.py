"""Comprehensive unit & integration tests for Spec 345 — JARVIS-CLOUD-IP-BOUNDARY-1.

Fail-closed, server-owned IP egress boundary across Jarvis chat read-only tools:
- jarvis_repo_read
- jarvis_knowledge_read
- jarvis_retrieval_query
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.core import packs
from app.core.database import open_sqlite_connection
from app.modules.agents.hermes.ip_boundary import (
    CLOUD_IP_MODE_NO_CLOUD,
    normalize_cloud_ip_mode,
)
from app.modules.agents.hermes.knowledge_read_tool import dispatch_knowledge_read_tool
from app.modules.agents.hermes.repo_read_tool import dispatch_repo_read_tool
from app.modules.agents.hermes.supervisor import dispatch_tool
from app.modules.ai import thread_service
from app.modules.ai.agent_contracts import (
    AgentSessionRef,
    CapabilityGrantRef,
    CapabilityScope,
    StructuredToolCall,
)
from app.modules.ai.retrieval_query import query_context
from app.modules.ai.thread_models import AIThreadSubmit
from app.modules.coding import relay_monitor

_SESSION = AgentSessionRef(
    jarvis_thread_id="thread-345",
    hermes_session_id="hermes-session-345",
    profile_id="isaac",
    workspace_id="ws-345",
    generation=1,
    upstream_revision="2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19",
)


def _make_call(
    arguments: dict[str, Any],
    capability_id: str,
    grant_id: str = "grant-345",
) -> StructuredToolCall:
    now = datetime.now(UTC)
    return StructuredToolCall(
        call_id=str(uuid4()),
        capability_id=capability_id,
        grant_id=grant_id,
        correlation_id="corr-345",
        session_ref=_SESSION,
        arguments=arguments,
        requested_at=now,
        deadline_at=now + timedelta(seconds=60),
    )


def _make_grant(
    capability_id: str,
    grant_id: str = "grant-345",
    workspace_id: str = "ws-345",
    thread_id: str = "thread-345",
    route_class: str = "local:llamacpp",
    is_local: bool | None = None,
    cloud_ip_mode: str | None = None,
) -> CapabilityGrantRef:
    now = datetime.now(UTC)
    constraints: dict[str, str | int | float | bool] = {"route_class": route_class}
    if is_local is not None:
        constraints["is_local"] = is_local
    if cloud_ip_mode is not None:
        os.environ["JARVIS_CLOUD_IP_MODE"] = cloud_ip_mode
    return CapabilityGrantRef(
        grant_id=grant_id,
        capability_id=capability_id,
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=workspace_id, jarvis_thread_id=thread_id),
        constraints=constraints,
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=10),
    )


@pytest.fixture(autouse=True)
def _reset_boundary_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.database import initialize_database

    initialize_database()
    monkeypatch.delenv("JARVIS_CLOUD_IP_MODE", raising=False)
    monkeypatch.delenv("JARVISOS_CLOUD_IP_MODE", raising=False)
    # Default to fake provider mode for offline test isolation
    mock_settings = SimpleNamespace(
        provider_mode="fake",
        hermes_role_routes={"isaac": "local:llamacpp"},
    )
    monkeypatch.setattr("app.modules.ai.settings.get_ai_settings", lambda: mock_settings)
    yield


def _audit_events() -> list[dict[str, Any]]:
    with open_sqlite_connection(read_only=True) as conn:
        rows = conn.execute(
            "SELECT payload FROM events WHERE event_type = 'hermes.cloud_ip_boundary' ORDER BY rowid"
        ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


@pytest.fixture
def fake_zephyr_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pack_dir = tmp_path / "packs" / "zephyr"
    pack_dir.mkdir(parents=True)
    (pack_dir / "jarvis_pack.toml").write_text(
        'id = "zephyr"\n\n[sensitivity]\nterms = ["zephyr"]\n',
        encoding="utf-8",
    )
    (pack_dir / "clean_doc.txt").write_text(
        "Public knowledge overview of the subsystem.\nLine 2.\n",
        encoding="utf-8",
    )
    (pack_dir / "zephyr_design.txt").write_text(
        "This project specifies zephyr geometry and process parameter correlation.\nLine 2.\n",
        encoding="utf-8",
    )
    (pack_dir / "confidential_doc.txt").write_text(
        "Zephyr project notes: confidential partner-only data under NDA.\nLine 2.\n",
        encoding="utf-8",
    )
    (pack_dir / "tokens_doc.txt").write_text(
        "API access credential: api_key = sk-supersecretkey12345678901234567890\nLine 2.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack_dir))
    return pack_dir


@pytest.fixture
def fake_public_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo_dir = tmp_path / "public_repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / "docs").mkdir()
    (repo_dir / "docs" / "architecture.md").write_text(
        "# JarvisOS Architecture\nPublic open source framework.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(relay_monitor, "relay_repo", lambda: repo_dir)
    return repo_dir


# ===========================================================================
# 1. Local Route Unrestricted Reads
# ===========================================================================


def test_local_route_repo_read_unrestricted(
    fake_zephyr_pack: Path, fake_public_repo: Path
) -> None:
    local_grant = _make_grant(
        "jarvis.repo_read",
        route_class="local:llamacpp",
        is_local=True,
    )
    call = _make_call(
        {"op": "read", "scope": "pack", "path": "zephyr_design.txt"},
        capability_id="jarvis.repo_read",
    )
    res, err = dispatch_repo_read_tool(call, "ws-345", "err", grant=local_grant)
    assert err == "err"
    assert res is not None
    assert "zephyr geometry" in res["content"]
    assert res.get("withheld_count") is None
    assert res.get("sanitized") is None


def test_local_route_knowledge_read_unrestricted() -> None:
    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO workspaces (id, name, slug, status, created_at, updated_at) "
            "VALUES ('ws-345', 'Test WS', 'test-ws', 'active', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO assumptions (id, workspace_id, statement, status, confidence, created_at, updated_at) "
            "VALUES ('a1', 'ws-345', 'confidential partner-only under NDA', 'proposed', 'high', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

    local_grant = _make_grant(
        "jarvis.knowledge_read",
        route_class="local:llamacpp",
        is_local=True,
    )
    call = _make_call(
        {"view": "memory_records", "workspace_id": "ws-345"},
        capability_id="jarvis.knowledge_read",
    )
    res, err = dispatch_knowledge_read_tool(call, "ws-345", "err", grant=local_grant)
    assert err == "err"
    assert res is not None
    assert res["count"] >= 1
    assert any("confidential" in m.get("title", "") for m in res["memory_records"])
    assert res.get("withheld_count") is None


def test_local_route_retrieval_query_unrestricted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bundle = SimpleNamespace(
        schema_version="retrieval-query.v1",
        bundle_id="b-1",
        workspace_id="ws-345",
        items=(),
        evidence_manifest=(),
        token_estimate=50,
        token_budget=1024,
        expansion_level=0,
        index_revision="rev-1",
        created_at="2026-01-01",
        model_dump=lambda mode="json": {"items": []},
    )
    fake_freshness = SimpleNamespace(model_dump=lambda mode="json": {})
    item_ref = SimpleNamespace(
        authority_owner="modeling",
        object_type="assumption",
        object_id="a1",
        model_dump=lambda mode="json": {"authority_owner": "modeling", "object_id": "a1"},
    )
    fake_item = SimpleNamespace(
        source_ref=item_ref,
        content_digest="digest-1",
        token_estimate=10,
    )
    fake_resolution = SimpleNamespace(
        state="current",
        content="confidential partner-only zephyr geometry details",
        current_content_digest="digest-1",
    )

    class FakeStore:
        def freshness(self) -> Any:
            return fake_freshness

        def build_bundle(self, *args: Any, **kwargs: Any) -> Any:
            b = fake_bundle
            b.items = (fake_item,)
            return b

        def resolve_authoritative(self, *args: Any) -> Any:
            return fake_resolution

    local_grant = _make_grant(
        "jarvis.retrieval_query",
        route_class="local:llamacpp",
        is_local=True,
    )
    result = query_context(
        "zephyr",
        workspace_id="ws-345",
        store=FakeStore(),
        grant=local_grant,
    )
    assert len(result["evidence"]) == 1
    assert "confidential" in result["evidence"][0]["excerpt"]
    assert result.get("withheld_count") is None


# ===========================================================================
# 2. Cloud Route: no_cloud (Default Fail-Closed)
# ===========================================================================


def test_cloud_route_no_cloud_public_repo_succeeds(
    fake_zephyr_pack: Path, fake_public_repo: Path
) -> None:
    cloud_grant = _make_grant(
        "jarvis.repo_read",
        route_class="cloud:scaleway",
        is_local=False,
        cloud_ip_mode="no_cloud",
    )
    call = _make_call(
        {"op": "read", "scope": "public", "path": "docs/architecture.md"},
        capability_id="jarvis.repo_read",
    )
    res, err = dispatch_repo_read_tool(call, "ws-345", "err", grant=cloud_grant)
    assert err == "err"
    assert res is not None
    assert "JarvisOS Architecture" in res["content"]
    assert res["confidentiality"] == "public"


def test_cloud_route_no_cloud_pack_repo_refused(fake_zephyr_pack: Path) -> None:
    cloud_grant = _make_grant(
        "jarvis.repo_read",
        route_class="cloud:scaleway",
        is_local=False,
        cloud_ip_mode="no_cloud",
    )
    call = _make_call(
        {"op": "read", "scope": "pack", "path": "clean_doc.txt"},
        capability_id="jarvis.repo_read",
    )
    res, err = dispatch_repo_read_tool(call, "ws-345", "err", grant=cloud_grant)
    assert res is None
    assert err == "egress_pack_blocked"

    # Audit event recorded
    events = _audit_events()
    assert len(events) == 1
    assert events[0]["tool"] == "jarvis_repo_read"
    assert events[0]["op"] == "read"
    assert events[0]["mode"] == "no_cloud"
    assert events[0]["reason_code"] == "egress_pack_blocked"


def test_cloud_route_no_cloud_knowledge_read_withholds_s2_s3_s4(
    fake_zephyr_pack: Path,
) -> None:
    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO workspaces (id, name, slug, status, created_at, updated_at) "
            "VALUES ('ws-345', 'Test WS', 'test-ws', 'active', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO assumptions (id, workspace_id, statement, status, confidence, created_at, updated_at) "
            "VALUES ('a-s0', 'ws-345', 'Standard public engineering assumption', 'active', 'high', '2026-01-01', '2026-01-01'), "
            "       ('a-s2', 'ws-345', 'confidential partner-only under NDA', 'active', 'high', '2026-01-01', '2026-01-01'), "
            "       ('a-s3', 'ws-345', 'zephyr geometry correlation and process parameter', 'active', 'high', '2026-01-01', '2026-01-01'), "
            "       ('a-s4', 'ws-345', 'secret api_key = sk-12345678901234567890', 'active', 'high', '2026-01-01', '2026-01-01'), "
            "       ('a-s4-token', 'ws-345', 'sk-12345678901234567890', 'active', 'high', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

    cloud_grant = _make_grant(
        "jarvis.knowledge_read",
        route_class="cloud:openai",
        is_local=False,
        cloud_ip_mode="no_cloud",
    )
    call = _make_call(
        {"view": "memory_records", "workspace_id": "ws-345"},
        capability_id="jarvis.knowledge_read",
    )
    res, err = dispatch_knowledge_read_tool(call, "ws-345", "err", grant=cloud_grant)
    assert err == "err"
    assert res is not None
    assert res["egress_mode"] == "no_cloud"
    assert res["withheld_count"] == 4
    assert res["count"] == 1
    assert res["memory_records"][0]["id"] == "a-s0"

    # Audit event recorded
    events = _audit_events()
    assert any(e["tool"] == "jarvis_knowledge_read" and e["withheld_count"] == 4 for e in events)


def test_cloud_route_no_cloud_retrieval_query_withholds_s2_s3(
    fake_zephyr_pack: Path,
) -> None:
    fake_freshness = SimpleNamespace(model_dump=lambda mode="json": {})
    fake_bundle = SimpleNamespace(
        schema_version="retrieval-query.v1",
        bundle_id="b-1",
        workspace_id="ws-345",
        items=(),
        evidence_manifest=(),
        token_estimate=50,
        token_budget=1024,
        expansion_level=0,
        index_revision="rev-1",
        created_at="2026-01-01",
        model_dump=lambda mode="json": {"items": []},
    )
    item_s0 = SimpleNamespace(
        source_ref=SimpleNamespace(model_dump=lambda mode="json": {"id": "1"}),
        content_digest="d-s0",
        token_estimate=10,
    )
    item_s3 = SimpleNamespace(
        source_ref=SimpleNamespace(model_dump=lambda mode="json": {"id": "zephyr geometry"}),
        content_digest="d-s3",
        token_estimate=10,
    )

    class FakeStore:
        def freshness(self) -> Any:
            return fake_freshness

        def build_bundle(self, *args: Any, **kwargs: Any) -> Any:
            b = fake_bundle
            b.items = (item_s0, item_s3)
            return b

        def resolve_authoritative(self, ref: Any) -> Any:
            if ref.model_dump().get("id") == "1":
                return SimpleNamespace(state="current", content="Standard open passage.", current_content_digest="d-s0")
            return SimpleNamespace(state="current", content="Standard safe passage.", current_content_digest="d-s3")

    cloud_grant = _make_grant(
        "jarvis.retrieval_query",
        route_class="cloud:openai",
        is_local=False,
        cloud_ip_mode="no_cloud",
    )
    result = query_context("test", workspace_id="ws-345", store=FakeStore(), grant=cloud_grant)
    assert result["egress_mode"] == "no_cloud"
    assert result["withheld_count"] == 1
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["content_digest"] == "d-s0"
    assert result["bundle"] == {"items": []}
    assert "zephyr geometry" not in str(result)


# ===========================================================================
# 3. Cloud Route: sanitized
# ===========================================================================


def test_cloud_route_sanitized_redacts_pack_ip_and_returns_s0(
    fake_zephyr_pack: Path,
) -> None:
    cloud_grant = _make_grant(
        "jarvis.repo_read",
        route_class="cloud:scaleway",
        is_local=False,
        cloud_ip_mode="sanitized",
    )
    call = _make_call(
        {"op": "read", "scope": "pack", "path": "zephyr_design.txt"},
        capability_id="jarvis.repo_read",
    )
    res, err = dispatch_repo_read_tool(call, "ws-345", "err", grant=cloud_grant)
    assert err == "err"
    assert res is not None
    assert res["sanitized"] is True
    assert res["egress_mode"] == "sanitized"
    assert "[REDACTED_IP]" in res["content"]
    assert "zephyr geometry" not in res["content"]


def test_cloud_route_sanitized_withholds_unredacted_s2_fail_closed(
    fake_zephyr_pack: Path,
) -> None:
    cloud_grant = _make_grant(
        "jarvis.repo_read",
        route_class="cloud:scaleway",
        is_local=False,
        cloud_ip_mode="sanitized",
    )
    # confidential_doc has 'confidential partner-only data under NDA'
    call = _make_call(
        {"op": "read", "scope": "pack", "path": "confidential_doc.txt"},
        capability_id="jarvis.repo_read",
    )
    res, err = dispatch_repo_read_tool(call, "ws-345", "err", grant=cloud_grant)
    assert res is None
    assert err == "egress_sanitization_required"

    events = _audit_events()
    assert any(e["reason_code"] == "egress_sanitization_required" for e in events)


def test_cloud_route_sanitized_withholds_secrets_in_repo_read(
    fake_zephyr_pack: Path,
) -> None:
    cloud_grant = _make_grant(
        "jarvis.repo_read",
        route_class="cloud:scaleway",
        is_local=False,
        cloud_ip_mode="sanitized",
    )
    call = _make_call(
        {"op": "read", "scope": "pack", "path": "tokens_doc.txt"},
        capability_id="jarvis.repo_read",
    )
    res, err = dispatch_repo_read_tool(call, "ws-345", "err", grant=cloud_grant)
    assert res is None
    assert err == "egress_sanitization_required"


def test_cloud_route_sanitized_knowledge_read(fake_zephyr_pack: Path) -> None:
    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO workspaces (id, name, slug, status, created_at, updated_at) "
            "VALUES ('ws-345', 'Test WS', 'test-ws', 'active', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO assumptions (id, workspace_id, statement, status, confidence, created_at, updated_at) "
            "VALUES ('a-pack-ip', 'ws-345', 'zephyr geometry and design decision notes', 'active', 'high', '2026-01-01', '2026-01-01'), "
            "       ('a-nda', 'ws-345', 'strictly confidential under NDA', 'active', 'high', '2026-01-01', '2026-01-01')"
        )
        conn.commit()

    cloud_grant = _make_grant(
        "jarvis.knowledge_read",
        route_class="cloud:openai",
        is_local=False,
        cloud_ip_mode="sanitized",
    )
    call = _make_call(
        {"view": "memory_records", "workspace_id": "ws-345"},
        capability_id="jarvis.knowledge_read",
    )
    res, err = dispatch_knowledge_read_tool(call, "ws-345", "err", grant=cloud_grant)
    assert err == "err"
    assert res is not None
    assert res["sanitized"] is True
    assert res["egress_mode"] == "sanitized"
    assert res["withheld_count"] == 1
    assert res["count"] == 1
    assert res["memory_records"][0]["id"] == "a-pack-ip"
    assert "[REDACTED_IP]" in res["memory_records"][0]["title"]


# ===========================================================================
# 4. Removed modes fail closed
# ===========================================================================


@pytest.mark.parametrize("mode", ["full", "unknown"])
def test_removed_or_unknown_mode_blocks_pack_reads(fake_zephyr_pack: Path, mode: str) -> None:
    grant = _make_grant("jarvis.repo_read", route_class="cloud:scaleway", is_local=False,
                        cloud_ip_mode=mode)
    call = _make_call({"op": "read", "scope": "pack", "path": "zephyr_design.txt"},
                      capability_id="jarvis.repo_read")
    result, error = dispatch_repo_read_tool(call, "ws-345", "err", grant=grant)
    assert result is None
    assert error == "egress_pack_blocked"
    assert _audit_events()[-1]["mode"] == "no_cloud"


# ===========================================================================
# 5. Invalid / Missing Mode Fails Closed to no_cloud
# ===========================================================================


@pytest.mark.parametrize("invalid_mode", [None, "", "unknown", "bypass", "unrestricted", "FULL_CAPS", "full", 123])
def test_invalid_mode_normalizes_to_no_cloud(invalid_mode: Any) -> None:
    assert normalize_cloud_ip_mode(invalid_mode) == CLOUD_IP_MODE_NO_CLOUD


def test_invalid_mode_in_grant_fails_closed(fake_zephyr_pack: Path) -> None:
    grant = _make_grant(
        "jarvis.repo_read",
        route_class="cloud:scaleway",
        is_local=False,
        cloud_ip_mode="invalid_override",
    )
    call = _make_call(
        {"op": "read", "scope": "pack", "path": "clean_doc.txt"},
        capability_id="jarvis.repo_read",
    )
    res, err = dispatch_repo_read_tool(call, "ws-345", "err", grant=grant)
    assert res is None
    assert err == "egress_pack_blocked"


def test_grant_constraint_cannot_raise_cloud_mode(fake_zephyr_pack: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_CLOUD_IP_MODE", "no_cloud")
    grant = _make_grant("jarvis.repo_read", route_class="cloud:scaleway", is_local=False)
    grant.constraints["cloud_ip_mode"] = "sanitized"
    call = _make_call({"op": "read", "scope": "pack", "path": "zephyr_design.txt"},
                      capability_id="jarvis.repo_read")
    result, error = dispatch_repo_read_tool(call, "ws-345", "err", grant=grant)
    assert result is None
    assert error == "egress_pack_blocked"
    assert _audit_events()[-1]["mode"] == "no_cloud"


# ===========================================================================
# 6. Caller / Model Supplied Mode Overrides Ignored or Rejected
# ===========================================================================


def test_model_supplied_mode_override_rejected_in_repo_read() -> None:
    grant = _make_grant(
        "jarvis.repo_read",
        route_class="cloud:scaleway",
        is_local=False,
        cloud_ip_mode="no_cloud",
    )
    call = _make_call(
        {"op": "read", "scope": "pack", "path": "clean_doc.txt", "cloud_ip_mode": "full"},
        capability_id="jarvis.repo_read",
    )
    res, err = dispatch_repo_read_tool(call, "ws-345", "err", grant=grant)
    assert res is None
    assert err == "invalid_arguments"


def test_model_supplied_mode_override_rejected_in_knowledge_read() -> None:
    grant = _make_grant(
        "jarvis.knowledge_read",
        route_class="cloud:scaleway",
        is_local=False,
        cloud_ip_mode="no_cloud",
    )
    call = _make_call(
        {"view": "table_counts", "cloud_ip_mode": "full"},
        capability_id="jarvis.knowledge_read",
    )
    res, err = dispatch_knowledge_read_tool(call, "ws-345", "err", grant=grant)
    assert res is None
    assert err == "invalid_arguments"


def test_model_supplied_mode_override_rejected_in_retrieval_query() -> None:
    grant = _make_grant(
        "jarvis.retrieval_query",
        route_class="cloud:scaleway",
        is_local=False,
        cloud_ip_mode="no_cloud",
    )
    call = _make_call(
        {"query": "pump", "cloud_ip_mode": "full"},
        capability_id="jarvis.retrieval_query",
        grant_id=grant.grant_id,
    )
    res = dispatch_tool(call, live_grants={grant.grant_id: grant})
    assert res.status == "refused"
    assert res.error_code == "invalid_arguments"


# ===========================================================================
# 7. System Envelope Reports Cloud IP Mode on External Routes
# ===========================================================================


def test_system_envelope_reports_cloud_ip_mode_on_external_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    for mode in ("no_cloud", "sanitized", "full", "unknown"):
        monkeypatch.setenv("JARVIS_CLOUD_IP_MODE", mode)
        envelope = thread_service._jarvis_system_envelope(
            workspace_id="ws-345", workspace_name="Workspace 345", app_state=None,
            route_availability=[], route_class="cloud:scaleway",
        )
        expected = mode if mode in {"no_cloud", "sanitized"} else "no_cloud"
        assert f"Cloud IP mode: {expected}" in envelope
        boundary = "sanitized" if expected == "sanitized" else "blocked"
        assert f"Route boundary: external (private-pack reads {boundary})" in envelope

    local_envelope = thread_service._jarvis_system_envelope(
        workspace_id="ws-345", workspace_name="Workspace 345", app_state=None,
        route_availability=[], route_class="local:llamacpp",
    )
    assert "Route boundary: local (private-pack reads permitted)" in local_envelope
    assert "Cloud IP mode:" not in local_envelope


# ===========================================================================
# 8. Grant Installation Propagates Server Route and Egress Context
# ===========================================================================


def test_grant_installation_keeps_mode_server_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_settings = SimpleNamespace(
        hermes_role_routes={"isaac": "cloud:scaleway"},
        provider_mode="cloud",
    )
    monkeypatch.setattr("app.modules.ai.settings.get_ai_settings", lambda: mock_settings)
    monkeypatch.setenv("JARVIS_CLOUD_IP_MODE", "sanitized")

    payload = AIThreadSubmit(
        request_id="req-345",
        prompt="hello",
        route_class="hermes:agent",
        role_id="isaac",
    )
    worker = SimpleNamespace(live_grants={}, session=_SESSION)
    grant_text = thread_service._install_hermes_retrieval_grant(
        worker, payload, "ws-345", "thread-345"
    )
    assert grant_text is not None

    # A caller route hint cannot turn a cloud-backed worker into a local read grant.
    payload.route_class = "local:llamacpp"
    second_worker = SimpleNamespace(live_grants={}, session=_SESSION)
    thread_service._install_hermes_retrieval_grant(second_worker, payload, "ws-345", "thread-345")
    assert all(g.constraints.get("is_local") is False for g in second_worker.live_grants.values()
               if g.capability_id in {"jarvis.repo_read", "jarvis.knowledge_read", "jarvis.retrieval_query"})

    repo_grant = next(g for g in worker.live_grants.values() if g.capability_id == "jarvis.repo_read")
    assert repo_grant.constraints.get("is_local") is False
    assert "cloud_ip_mode" not in repo_grant.constraints

    knowledge_grant = next(g for g in worker.live_grants.values() if g.capability_id == "jarvis.knowledge_read")
    assert knowledge_grant.constraints.get("is_local") is False
    assert "cloud_ip_mode" not in knowledge_grant.constraints

    retrieval_grant = next(g for g in worker.live_grants.values() if g.capability_id == "jarvis.retrieval_query")
    assert retrieval_grant.constraints.get("is_local") is False
    assert "cloud_ip_mode" not in retrieval_grant.constraints
