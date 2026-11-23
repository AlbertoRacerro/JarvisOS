"""Tests for Spec 343 — JARVIS-KNOWLEDGE-SQL-READ-1 (Hermes knowledge_read read-only tool)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.agents.hermes import broker_mcp, worker_shim
from app.modules.agents.hermes.knowledge_read_tool import dispatch_knowledge_read_tool
from app.modules.agents.hermes.supervisor import (
    _HERMES_KNOWLEDGE_READ_CAPABILITY,
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

_SESSION = AgentSessionRef(
    jarvis_thread_id="thread-test",
    hermes_session_id="hermes-session-test",
    profile_id="isaac",
    workspace_id="workspace-a",
    generation=1,
    upstream_revision="2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19",
)


def _make_call(
    arguments: dict[str, Any],
    capability_id: str = "jarvis.knowledge_read",
    grant_id: str = "grant-knowledge-read",
    workspace_id: str = "workspace-a",
    thread_id: str = "thread-test",
) -> StructuredToolCall:
    now = datetime.now(UTC)
    session = AgentSessionRef(
        jarvis_thread_id=thread_id,
        hermes_session_id="hermes-session-test",
        profile_id="isaac",
        workspace_id=workspace_id,
        generation=1,
        upstream_revision="2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19",
    )
    return StructuredToolCall(
        call_id=str(uuid4()),
        capability_id=capability_id,
        grant_id=grant_id,
        correlation_id="corr-1",
        session_ref=session,
        arguments=arguments,
        requested_at=now,
        deadline_at=now + timedelta(seconds=60),
    )


def _make_grant(
    grant_id: str = "grant-knowledge-read",
    capability_id: str = "jarvis.knowledge_read",
    workspace_id: str = "workspace-a",
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


@pytest.fixture
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Set up an isolated SQLite database initialized with all schemas and seed test data."""
    data_root = tmp_path / "jarvis_data"
    data_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(data_root))
    initialize_database()

    with open_sqlite_connection() as conn:
        now = "2026-11-22T05:00:00Z"

        # Workspaces
        conn.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?)",
            ("workspace-a", "Workspace Alpha", "ws-alpha", "First workspace", now, now),
        )
        conn.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?)",
            ("workspace-b", "Workspace Beta", "ws-beta", "Second workspace", now, now),
        )

        # Assumptions
        conn.execute(
            "INSERT INTO assumptions (id, workspace_id, statement, scope, confidence, status, source_ref, origin, created_at, updated_at, notes) "
            "VALUES (?, ?, ?, ?, ?, 'accepted', 'ref-1', 'user', ?, ?, ?)",
            ("assump-1", "workspace-a", "Atmospheric pressure is 101.3 kPa", "ambient", "high", now, now, "Standard ambient assumption"),
        )
        conn.execute(
            "INSERT INTO assumptions (id, workspace_id, statement, scope, confidence, status, source_ref, origin, created_at, updated_at, notes) "
            "VALUES (?, ?, ?, ?, ?, 'proposed', 'ref-2', 'user', ?, ?, ?)",
            ("assump-2", "workspace-b", "Other workspace assumption", "ambient", "low", now, now, "Secret token " + "sk-" + "12345678901234567890"),
        )

        # Parameters
        conn.execute(
            "INSERT INTO parameters (id, workspace_id, name, symbol, value, unit, status, confidence, origin, created_at, updated_at, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, 'accepted', 0.95, 'user', ?, ?, ?)",
            ("param-1", "workspace-a", "Biomass Yield", "Y_x", "0.45", "g/g", now, now, "Experimental yield"),
        )

        # Decisions
        conn.execute(
            "INSERT INTO decisions (id, workspace_id, title, decision_text, rationale, status, origin, created_at, updated_at, notes) "
            "VALUES (?, ?, ?, ?, ?, 'accepted', 'user', ?, ?, ?)",
            ("dec-1", "workspace-a", "Select PBR geometry", "Use annular photobioreactor", "Optimal light path", now, now, "Design decision note"),
        )

        # Literature sources & entries
        conn.execute(
            "INSERT INTO literature_sources (id, workspace_id, title, source_kind, state, published_year, citation, publisher, created_at, updated_at) "
            "VALUES (?, ?, ?, 'paper', 'accepted', 2024, 'Author et al., 2024', 'Nature Pub', ?, ?)",
            ("src-1", "workspace-a", "Photobioreactor Kinetics Review", now, now),
        )
        conn.execute(
            "INSERT INTO literature_entries (id, workspace_id, source_id, entry_kind, statement, value_number, unit, status, locator_kind, locator_start, locator_end, created_at, updated_at) "
            "VALUES (?, ?, ?, 'claim', 'Specific growth rate peaks at 30 deg C', NULL, NULL, 'accepted', 'page', 12, 14, ?, ?)",
            ("entry-1", "workspace-a", "src-1", now, now),
        )
        conn.execute(
            "INSERT INTO literature_entries (id, workspace_id, source_id, entry_kind, statement, value_number, unit, status, locator_kind, locator_start, locator_end, created_at, updated_at) "
            "VALUES (?, ?, ?, 'datum', NULL, 0.085, '1/h', 'accepted', 'line', 45, 46, ?, ?)",
            ("entry-2", "workspace-a", "src-1", now, now),
        )

        # Project knowledge revisions & validations
        conn.execute(
            "INSERT INTO project_knowledge_revisions (id, workspace_id, parent_kind, state, change_set_digest, change_set_json, projected_state_digest, origin, created_at, accepted_at) "
            "VALUES (?, ?, 'working', 'reconciled', 'dig-1', ?, 'proj-1', 'apply', ?, ?)",
            ("rev-1", "workspace-a", json.dumps([{"op": "add_parameter", "id": "p-1"}, {"op": "set_requirement", "id": "r-1"}]), now, now),
        )
        # Requirement for validation
        conn.execute(
            "INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) "
            "VALUES (?, ?, 'Biomass purity > 90%', 'active', ?, ?)",
            ("req-1", "workspace-a", now, now),
        )
        conn.execute(
            "INSERT INTO project_knowledge_validation (id, workspace_id, working_revision_id, requirement_id, requirement_updated_at, rule_version, validated_basis_digest, applicability_set_digest, validator_id, validator_version, outcome, reason_code, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'scalar-v1', 'basis-1', 'app-1', 'val-1', '1', 'pass', 'criteria_met', ?)",
            ("val-1", "workspace-a", "rev-1", "req-1", now, now),
        )

        # Calculation / simulation runs
        conn.execute(
            "INSERT INTO model_specs (id, workspace_id, title, engineering_question, created_at, updated_at) "
            "VALUES ('spec-1', 'workspace-a', 'Heat Exchanger', 'Sizing', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO model_versions (id, workspace_id, model_spec_id, version_label, created_at) "
            "VALUES ('calc_model_v1', 'workspace-a', 'spec-1', 'v1', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO simulation_runs (id, workspace_id, model_version_id, run_label, status, input_payload, output_payload, created_at, notes) "
            "VALUES (?, ?, 'calc_model_v1', 'run_heat_exchanger', 'succeeded', ?, ?, ?, 'Calc run test')",
            ("sim-1", "workspace-a", json.dumps({"temp_in": 300, "flow_rate": 10}), json.dumps({"heat_duty": 4500, "temp_out": 320}), now),
        )

        # AI threads, flows, and jobs
        conn.execute(
            "INSERT INTO ai_threads (id, workspace_id, title, created_at, last_activity_at) "
            "VALUES (?, ?, 'Design thread', ?, ?)",
            ("thread-test", "workspace-a", now, now),
        )
        conn.execute(
            "INSERT INTO ai_flows (id, workspace_id, task_kind, state, created_at, updated_at, completed_at) "
            "VALUES (?, ?, 'general', 'complete', ?, ?, ?)",
            ("flow-1", "workspace-a", now, now, now),
        )
        conn.execute(
            "INSERT INTO ai_jobs (id, created_at, status, task_kind, requested_route_class, selected_route_class, provider_id, model_id, route_reason_json, flow_id) "
            "VALUES (?, ?, 'success', 'general', 'local:llamacpp', 'local:llamacpp', 'local_llamacpp', 'gemma-2-9b', '{}', ?)",
            ("job-1", now, "flow-1"),
        )

        # Artifacts
        conn.execute(
            "INSERT INTO artifacts (id, workspace_id, filename, stored_path, artifact_type, mime_type, sha256, status, created_at, notes) "
            "VALUES (?, ?, 'spec.pdf', '/tmp/spec.pdf', 'literature_source', 'application/pdf', 'sha-dummy', 'registered', ?, 'Doc artifact')",
            ("art-1", "workspace-a", now),
        )

        conn.commit()


# ---------------------------------------------------------------------------
# Registration and Schema Tests
# ---------------------------------------------------------------------------


def test_knowledge_read_tool_registered_in_allowlists_and_broker() -> None:
    """Acceptance criterion 1: jarvis_knowledge_read is registered in all allowlists and schemas."""
    # 1. broker_mcp._TOOLS contains jarvis_knowledge_read with strict JSON schema
    knowledge_tool = next((t for t in broker_mcp._TOOLS if t["name"] == "jarvis_knowledge_read"), None)
    assert knowledge_tool is not None
    schema = knowledge_tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["grant_id", "view"]
    assert set(schema["properties"]["view"]["enum"]) == {
        "workspaces",
        "memory_records",
        "literature_sources",
        "literature_entries",
        "project_revisions",
        "calc_runs",
        "ai_jobs_recent",
        "table_counts",
    }
    assert schema["properties"]["limit"]["minimum"] == 1
    assert schema["properties"]["limit"]["maximum"] == 100

    # 2. supervisor._TOOL_CAPABILITIES maps jarvis_knowledge_read -> jarvis.knowledge_read
    assert _TOOL_CAPABILITIES.get("jarvis_knowledge_read") == "jarvis.knowledge_read"

    # 3. supervisor.HERMES_TOOL_ALLOWLIST contains mcp__jarvis__jarvis_knowledge_read
    assert "mcp__jarvis__jarvis_knowledge_read" in HERMES_TOOL_ALLOWLIST

    # 4. PRODUCTION_CAPABILITY_REGISTRY has jarvis.knowledge_read registered with action_class="READ"
    desc = next(
        (c for c in PRODUCTION_CAPABILITY_REGISTRY.for_route("ai-threads") if c.capability_id == "jarvis.knowledge_read"),
        None,
    )
    assert desc is not None
    assert desc.action_class == "READ"
    assert desc == _HERMES_KNOWLEDGE_READ_CAPABILITY

    # 5. worker_shim.ADMITTED_TOOLS contains mcp__jarvis__jarvis_knowledge_read
    assert "mcp__jarvis__jarvis_knowledge_read" in worker_shim.ADMITTED_TOOLS

    # 6. context_builder.THREAD_AUTHORITY_INSTRUCTIONS mentions jarvis_knowledge_read
    assert "jarvis_knowledge_read" in context_builder.THREAD_AUTHORITY_INSTRUCTIONS

    # 7. thread_service._turn_tools includes mcp__jarvis__jarvis_knowledge_read
    assert "mcp__jarvis__jarvis_knowledge_read" in thread_service._turn_tools("none")


def test_hermes_thread_grants_knowledge_read() -> None:
    """Acceptance criterion 1 & 5: turn prompt guidance advertises jarvis_knowledge_read with grant_id and views."""
    worker = SimpleNamespace(live_grants={})
    payload = AIThreadSubmit(request_id="req-grant", prompt="Inspect database", route_class="hermes:agent")
    text = thread_service._install_hermes_retrieval_grant(worker, payload, "workspace-a", "thread-test")

    assert text is not None
    assert "knowledge_read_grant_id=" in text
    assert "jarvis_knowledge_read views:" in text
    assert "workspaces, memory_records, literature_sources" in text
    assert "Use jarvis_knowledge_read to inspect" in text

    k_grant = next((g for g in worker.live_grants.values() if g.capability_id == "jarvis.knowledge_read"), None)
    assert k_grant is not None
    assert k_grant.scope.workspace_id == "workspace-a"
    assert k_grant.scope.jarvis_thread_id == "thread-test"


# ---------------------------------------------------------------------------
# Named Views Tests against Isolated Database
# ---------------------------------------------------------------------------


def test_view_workspaces(isolated_db) -> None:
    """View 1: workspaces lists workspaces bounded by limit."""
    call = _make_call({"grant_id": "g-1", "view": "workspaces", "limit": 10})
    result, error = dispatch_knowledge_read_tool(call, "workspace-a", "default_error")

    assert error == "default_error"
    assert result is not None
    assert result["view"] == "workspaces"
    assert result["count"] == 2
    assert result["total"] == 2
    assert result["truncated"] is False
    ws_ids = [w["id"] for w in result["workspaces"]]
    assert "workspace-a" in ws_ids
    assert "workspace-b" in ws_ids
    first = next(w for w in result["workspaces"] if w["id"] == "workspace-a")
    assert first["name"] == "Workspace Alpha"
    assert first["description"] == "First workspace"


def test_view_memory_records(isolated_db) -> None:
    """View 2: memory_records returns workspace-scoped assumptions, parameters, decisions with filters."""
    # 1. Unfiltered in workspace-a (has 1 assumption, 1 parameter, 1 decision)
    call = _make_call({"grant_id": "g-1", "view": "memory_records"})
    result, error = dispatch_knowledge_read_tool(call, "workspace-a", "default_error")

    assert error == "default_error"
    assert result is not None
    assert result["view"] == "memory_records"
    assert result["workspace_id"] == "workspace-a"
    assert result["count"] == 3
    assert result["total"] == 3
    assert result["truncated"] is False

    types = {r["record_type"] for r in result["memory_records"]}
    assert types == {"assumption", "parameter", "decision"}

    # 2. Filter by record_type='parameter'
    call_param = _make_call({"grant_id": "g-1", "view": "memory_records", "record_type": "parameter"})
    res_param, _ = dispatch_knowledge_read_tool(call_param, "workspace-a", "default_error")
    assert res_param is not None
    assert res_param["count"] == 1
    assert res_param["memory_records"][0]["title"] == "Biomass Yield"
    assert res_param["memory_records"][0]["confidence"] == 0.95
    assert res_param["memory_records"][0]["tags"] == ["Y_x"]

    # 3. Filter by status='accepted'
    call_status = _make_call({"grant_id": "g-1", "view": "memory_records", "status": "accepted"})
    res_status, _ = dispatch_knowledge_read_tool(call_status, "workspace-a", "default_error")
    assert res_status is not None
    assert res_status["count"] == 3

    # 4. Filter by query substring
    call_query = _make_call({"grant_id": "g-1", "view": "memory_records", "query": "photobioreactor"})
    res_query, _ = dispatch_knowledge_read_tool(call_query, "workspace-a", "default_error")
    assert res_query is not None
    assert res_query["count"] == 1
    assert res_query["memory_records"][0]["id"] == "dec-1"


def test_view_literature_sources(isolated_db) -> None:
    """View 3: literature_sources returns workspace sources with entry counts and query filter."""
    call = _make_call({"grant_id": "g-1", "view": "literature_sources"})
    result, error = dispatch_knowledge_read_tool(call, "workspace-a", "default_error")

    assert error == "default_error"
    assert result is not None
    assert result["view"] == "literature_sources"
    assert result["count"] == 1
    src = result["literature_sources"][0]
    assert src["id"] == "src-1"
    assert src["title"] == "Photobioreactor Kinetics Review"
    assert src["year"] == 2024
    assert src["source_type"] == "paper"
    assert src["backing_state"] == "accepted"
    assert src["entry_count"] == 2

    # Query filter
    call_q = _make_call({"grant_id": "g-1", "view": "literature_sources", "query": "Kinetics"})
    res_q, _ = dispatch_knowledge_read_tool(call_q, "workspace-a", "default_error")
    assert res_q is not None and res_q["count"] == 1

    call_nomatch = _make_call({"grant_id": "g-1", "view": "literature_sources", "query": "NonExistent"})
    res_nomatch, _ = dispatch_knowledge_read_tool(call_nomatch, "workspace-a", "default_error")
    assert res_nomatch is not None and res_nomatch["count"] == 0


def test_view_literature_entries(isolated_db) -> None:
    """View 4: literature_entries returns workspace entries with locators and filters."""
    call = _make_call({"grant_id": "g-1", "view": "literature_entries"})
    result, error = dispatch_knowledge_read_tool(call, "workspace-a", "default_error")

    assert error == "default_error"
    assert result is not None
    assert result["view"] == "literature_entries"
    assert result["count"] == 2
    claim = next(e for e in result["literature_entries"] if e["id"] == "entry-1")
    datum = next(e for e in result["literature_entries"] if e["id"] == "entry-2")

    assert claim["entry_type"] == "claim"
    assert "growth rate peaks" in claim["claim_text"]
    assert claim["locator"] == "page:12-14"

    assert datum["entry_type"] == "datum"
    assert datum["value"] == 0.085
    assert datum["unit"] == "1/h"
    assert datum["locator"] == "line:45-46"

    # Filters: source_id, status, query
    call_src = _make_call({"grant_id": "g-1", "view": "literature_entries", "source_id": "src-1"})
    res_src, _ = dispatch_knowledge_read_tool(call_src, "workspace-a", "default_error")
    assert res_src is not None and res_src["count"] == 2

    call_status = _make_call({"grant_id": "g-1", "view": "literature_entries", "status": "accepted"})
    res_status, _ = dispatch_knowledge_read_tool(call_status, "workspace-a", "default_error")
    assert res_status is not None and res_status["count"] == 2

    call_status_reject = _make_call({"grant_id": "g-1", "view": "literature_entries", "status": "rejected"})
    res_status_reject, _ = dispatch_knowledge_read_tool(call_status_reject, "workspace-a", "default_error")
    assert res_status_reject is not None and res_status_reject["count"] == 0

    call_query = _make_call({"grant_id": "g-1", "view": "literature_entries", "query": "growth"})
    res_query, _ = dispatch_knowledge_read_tool(call_query, "workspace-a", "default_error")
    assert res_query is not None and res_query["count"] == 1


def test_view_project_revisions(isolated_db) -> None:
    """View 5: project_revisions returns revisions with revalidation summary."""
    call = _make_call({"grant_id": "g-1", "view": "project_revisions"})
    result, error = dispatch_knowledge_read_tool(call, "workspace-a", "default_error")

    assert error == "default_error"
    assert result is not None
    assert result["view"] == "project_revisions"
    assert result["count"] == 1
    rev = result["project_revisions"][0]
    assert rev["id"] == "rev-1"
    assert rev["status"] == "reconciled"
    assert "operations" in rev["summary"]
    assert rev["revalidation_summary"]["status"] == "pass"
    assert rev["revalidation_summary"]["pass"] == 1
    assert rev["revalidation_summary"]["total"] == 1


def test_view_calc_runs(isolated_db) -> None:
    """View 6: calc_runs returns simulation runs with input/output summaries."""
    call = _make_call({"grant_id": "g-1", "view": "calc_runs"})
    result, error = dispatch_knowledge_read_tool(call, "workspace-a", "default_error")

    assert error == "default_error"
    assert result is not None
    assert result["view"] == "calc_runs"
    assert result["count"] == 1
    run = result["calc_runs"][0]
    assert run["id"] == "sim-1"
    assert run["calculator_id"] == "calc_model_v1"
    assert run["status"] == "succeeded"
    assert "inputs:" in run["inputs_summary"]
    assert "outputs:" in run["outputs_summary"]

    # Calculator filter
    call_filter = _make_call({"grant_id": "g-1", "view": "calc_runs", "calculator_id": "calc_model_v1"})
    res_filter, _ = dispatch_knowledge_read_tool(call_filter, "workspace-a", "default_error")
    assert res_filter is not None and res_filter["count"] == 1

    call_nomatch = _make_call({"grant_id": "g-1", "view": "calc_runs", "calculator_id": "different_calc"})
    res_nomatch, _ = dispatch_knowledge_read_tool(call_nomatch, "workspace-a", "default_error")
    assert res_nomatch is not None and res_nomatch["count"] == 0


def test_view_ai_jobs_recent(isolated_db) -> None:
    """View 7: ai_jobs_recent returns jobs metadata without secrets or prompt bodies."""
    call = _make_call({"grant_id": "g-1", "view": "ai_jobs_recent"})
    result, error = dispatch_knowledge_read_tool(call, "workspace-a", "default_error")

    assert error == "default_error"
    assert result is not None
    assert result["view"] == "ai_jobs_recent"
    assert result["count"] == 1
    job = result["ai_jobs"][0]
    assert job["id"] == "job-1"
    assert job["task_type"] == "general"
    assert job["route_class"] == "local:llamacpp"
    assert job["provider_id"] == "local_llamacpp"
    assert job["model"] == "gemma-2-9b"
    assert job["status"] == "success"

    # Verify no prompt, response, or secret bodies exist in output
    job_str = json.dumps(job)
    assert "prompt" not in job
    assert "response" not in job
    assert "route_reason" not in job_str


def test_view_table_counts(isolated_db) -> None:
    """View 8: table_counts returns exact domain counts for all 10 non-secret tables."""
    call = _make_call({"grant_id": "g-1", "view": "table_counts"})
    result, error = dispatch_knowledge_read_tool(call, "workspace-a", "default_error")

    assert error == "default_error"
    assert result is not None
    assert result["view"] == "table_counts"
    assert result["workspace_id"] == "workspace-a"
    counts = result["counts"]

    expected_keys = {
        "workspaces",
        "memory_records",
        "literature_sources",
        "literature_entries",
        "ai_threads",
        "ai_jobs",
        "calc_runs",
        "artifacts",
        "project_revisions",
        "project_validations",
    }
    assert set(counts.keys()) == expected_keys

    assert counts["workspaces"] == 2
    assert counts["memory_records"] == 3  # 1 assumption + 1 parameter + 1 decision in workspace-a
    assert counts["literature_sources"] == 1
    assert counts["literature_entries"] == 2
    assert counts["ai_threads"] == 1
    assert counts["ai_jobs"] == 1
    assert counts["calc_runs"] == 1
    assert counts["artifacts"] == 1
    assert counts["project_revisions"] == 1
    assert counts["project_validations"] == 1


# ---------------------------------------------------------------------------
# Workspace Scoping & Security Enforcement
# ---------------------------------------------------------------------------


def test_cross_workspace_query_refused(isolated_db) -> None:
    """Acceptance criterion 4: cross-workspace query is refused when workspace_id differs from grant."""
    # Attempting to read workspace-b memory records with a workspace-a grant
    call = _make_call({"grant_id": "g-1", "view": "memory_records", "workspace_id": "workspace-b"})
    result, error = dispatch_knowledge_read_tool(call, "workspace-a", "default_error")

    assert result is None
    assert error == "cross_workspace_denied"

    # workspaces view is the sole exception where listing all workspaces is permitted
    call_ws = _make_call({"grant_id": "g-1", "view": "workspaces", "workspace_id": "workspace-b"})
    res_ws, err_ws = dispatch_knowledge_read_tool(call_ws, "workspace-a", "default_error")
    assert err_ws == "default_error"
    assert res_ws is not None and res_ws["count"] == 1


def test_unknown_view_refused(isolated_db) -> None:
    """Acceptance criterion 3: unknown view returns structured error unknown_view."""
    call = _make_call({"grant_id": "g-1", "view": "secrets_table"})
    result, error = dispatch_knowledge_read_tool(call, "workspace-a", "default_error")

    assert result is None
    assert error == "unknown_view"


def test_invalid_arguments_refused(isolated_db) -> None:
    """Invalid limits or extra unauthorized keys are rejected with invalid_arguments."""
    # Limit out of bounds
    call_limit = _make_call({"grant_id": "g-1", "view": "memory_records", "limit": 200})
    res_limit, err_limit = dispatch_knowledge_read_tool(call_limit, "workspace-a", "default_error")
    assert res_limit is None and err_limit == "invalid_arguments"

    # Extra key
    call_extra = _make_call({"grant_id": "g-1", "view": "memory_records", "inject": "DROP TABLE"})
    res_extra, err_extra = dispatch_knowledge_read_tool(call_extra, "workspace-a", "default_error")
    assert res_extra is None and err_extra == "invalid_arguments"


def test_read_only_pragma_enforced(monkeypatch: pytest.MonkeyPatch, isolated_db) -> None:
    """Acceptance criterion 3: PRAGMA query_only = ON is executed on the dispatch connection."""
    from contextlib import contextmanager

    from app.modules.agents.hermes import knowledge_read_tool

    pragma_observed: list[int] = []
    real_open = knowledge_read_tool.open_sqlite_connection

    @contextmanager
    def _spy_open(*args, **kwargs):
        with real_open(*args, **kwargs) as conn:
            yield conn
            row = conn.execute("PRAGMA query_only").fetchone()
            pragma_observed.append(int(row[0]) if row else 0)

    monkeypatch.setattr(knowledge_read_tool, "open_sqlite_connection", _spy_open)
    call = _make_call({"grant_id": "g-1", "view": "table_counts"})
    result, _ = dispatch_knowledge_read_tool(call, "workspace-a", "default_error")
    assert result is not None
    assert pragma_observed == [1]


def test_read_only_connection_used_and_write_impossible(monkeypatch: pytest.MonkeyPatch, isolated_db) -> None:
    """Finding 1: open_sqlite_connection(read_only=True) is used and write through tool connection is impossible."""
    import sqlite3
    from contextlib import contextmanager

    from app.modules.agents.hermes import knowledge_read_tool

    read_only_flags: list[bool] = []
    write_errors: list[str] = []
    real_open = knowledge_read_tool.open_sqlite_connection

    @contextmanager
    def _spy_open(*args, **kwargs):
        read_only_flags.append(kwargs.get("read_only", False))
        with real_open(*args, **kwargs) as conn:
            try:
                conn.execute("INSERT INTO workspaces (id, name, slug) VALUES ('leak', 'leak', 'leak')")
            except sqlite3.OperationalError as exc:
                write_errors.append(str(exc))
            yield conn

    monkeypatch.setattr(knowledge_read_tool, "open_sqlite_connection", _spy_open)
    call = _make_call({"grant_id": "g-1", "view": "table_counts"})
    result, _ = dispatch_knowledge_read_tool(call, "workspace-a", "default_error")
    assert result is not None
    assert read_only_flags == [True]
    assert len(write_errors) == 1
    assert "readonly" in write_errors[0].lower() or "read-only" in write_errors[0].lower()


@pytest.mark.parametrize(
    ("input_str", "expected_str"),
    [
        # Absolute host paths redacted
        ("/srv/customer/secrets.txt", "[REDACTED_PATH]"),
        ("/data/x/token.txt", "[REDACTED_PATH]"),
        ("/var/log/audit.log", "[REDACTED_PATH]"),
        ("/etc/shadow", "[REDACTED_PATH]"),
        ("/opt/company/data.bin", "[REDACTED_PATH]"),
        # file:// URLs redacted (case-insensitive)
        ("file:///srv/customer/secrets.txt", "[REDACTED_PATH]"),
        ("file:///data/x/token.txt", "[REDACTED_PATH]"),
        ("FILE:///srv/x/y", "[REDACTED_PATH]"),
        ("File:///srv/customer/secrets.txt", "[REDACTED_PATH]"),
        # /home/<user> keeps mapping to ~
        ("/home/alice", "~"),
        ("/home/bob/project/file.txt", "~/project/file.txt"),
        # Non-mangling cases
        ("https://example.org/a/b", "https://example.org/a/b"),
        ("http://localhost:8000/api/v1", "http://localhost:8000/api/v1"),
        ("Yield is 10 mg/L", "Yield is 10 mg/L"),
        ("Rate is 5 g/m2/d", "Rate is 5 g/m2/d"),
        ("Speed: 60 km/h", "Speed: 60 km/h"),
        ("Calculation 10 / 20", "Calculation 10 / 20"),
        ("Flag /single is not a full path", "Flag /single is not a full path"),
        # key=value and key: value secrets
        ("password=supersecret", "password=[REDACTED_SECRET]"),
        ("password: supersecret", "password: [REDACTED_SECRET]"),
        ("passwd=my_passwd", "passwd=[REDACTED_SECRET]"),
        ("passwd: my_passwd", "passwd: [REDACTED_SECRET]"),
        ("secret=topsecret", "secret=[REDACTED_SECRET]"),
        ("secret: topsecret", "secret: [REDACTED_SECRET]"),
        ("token=tok12345", "token=[REDACTED_SECRET]"),
        ("token: tok12345", "token: [REDACTED_SECRET]"),
        ("api_key=key98765", "api_key=[REDACTED_SECRET]"),
        ("api_key: key98765", "api_key: [REDACTED_SECRET]"),
        ("apikey=key98765", "apikey=[REDACTED_SECRET]"),
        ("apikey: key98765", "apikey: [REDACTED_SECRET]"),
        ("user_password=secret123", "user_password=[REDACTED_SECRET]"),
        ('client_secret: "quoted_secret"', "client_secret: [REDACTED_SECRET]"),
        ("AUTH_TOKEN=BearerXYZ", "AUTH_TOKEN=[REDACTED_SECRET]"),
        ("my_api_key: 'single_quoted'", "my_api_key: [REDACTED_SECRET]"),
        ("db_passwd: secretpass", "db_passwd: [REDACTED_SECRET]"),
        # Non-secret key-value non-mangling
        ("status=accepted", "status=accepted"),
        ("status: accepted", "status: accepted"),
        ("total: 10", "total: 10"),
        ("count: 5", "count: 5"),
    ],
)
def test_scrub_string_cases(input_str: str, expected_str: str) -> None:
    from app.modules.agents.hermes.knowledge_read_tool import _scrub_string

    assert _scrub_string(input_str, "") == expected_str


def test_payload_bounds_oversized_non_list_field() -> None:
    from app.modules.agents.hermes.knowledge_read_tool import _enforce_payload_bounds

    # Oversized non-list field capped to 300 chars, payload bounded
    payload = {
        "view": "memory_records",
        "workspace_id": "workspace-a",
        "count": 1,
        "memory_records": [{"id": "1", "title": "rec"}],
        "oversized_notes": "X" * 20_000,
    }
    bounded = _enforce_payload_bounds(payload, "memory_records")
    assert bounded["truncated"] is True
    assert len(json.dumps(bounded)) <= 16_000
    assert len(bounded["oversized_notes"]) == 300
    assert bounded["count"] == 0
    assert bounded["memory_records"] == []


def test_payload_bounds_oversized_fallback_minimal() -> None:
    from app.modules.agents.hermes.knowledge_read_tool import _enforce_payload_bounds

    # When even capping strings to 300 chars leaves payload > 16000, minimal payload is returned
    payload = {
        "view": "memory_records",
        "workspace_id": "workspace-a",
        "count": 0,
        "memory_records": [],
    }
    for i in range(100):
        payload[f"huge_field_{i}"] = "Y" * 500

    bounded = _enforce_payload_bounds(payload, "memory_records")
    assert bounded["truncated"] is True
    assert len(json.dumps(bounded)) <= 16_000
    assert bounded["view"] == "memory_records"
    assert bounded["count"] == 0
    assert bounded["memory_records"] == []
    assert "huge_field_0" not in bounded


def test_empty_workspace_returns_empty_views(isolated_db) -> None:
    """Finding 4: An empty workspace returns empty lists and zero counts for every view."""
    now = "2026-11-22T05:00:00Z"
    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES ('workspace-empty', 'Empty Workspace', 'ws-empty', 'Workspace with zero data', 'active', ?, ?)",
            (now, now),
        )
        conn.commit()

    # 1. memory_records
    call_mem = _make_call({"grant_id": "g-1", "view": "memory_records"}, workspace_id="workspace-empty")
    res_mem, err_mem = dispatch_knowledge_read_tool(call_mem, "workspace-empty", "err")
    assert err_mem == "err"
    assert res_mem is not None
    assert res_mem["count"] == 0
    assert res_mem["total"] == 0
    assert res_mem["memory_records"] == []

    # 2. literature_sources
    call_ls = _make_call({"grant_id": "g-1", "view": "literature_sources"}, workspace_id="workspace-empty")
    res_ls, err_ls = dispatch_knowledge_read_tool(call_ls, "workspace-empty", "err")
    assert err_ls == "err"
    assert res_ls is not None
    assert res_ls["count"] == 0
    assert res_ls["total"] == 0
    assert res_ls["literature_sources"] == []

    # 3. literature_entries
    call_le = _make_call({"grant_id": "g-1", "view": "literature_entries"}, workspace_id="workspace-empty")
    res_le, err_le = dispatch_knowledge_read_tool(call_le, "workspace-empty", "err")
    assert err_le == "err"
    assert res_le is not None
    assert res_le["count"] == 0
    assert res_le["total"] == 0
    assert res_le["literature_entries"] == []

    # 4. project_revisions
    call_pr = _make_call({"grant_id": "g-1", "view": "project_revisions"}, workspace_id="workspace-empty")
    res_pr, err_pr = dispatch_knowledge_read_tool(call_pr, "workspace-empty", "err")
    assert err_pr == "err"
    assert res_pr is not None
    assert res_pr["count"] == 0
    assert res_pr["total"] == 0
    assert res_pr["project_revisions"] == []

    # 5. calc_runs
    call_cr = _make_call({"grant_id": "g-1", "view": "calc_runs"}, workspace_id="workspace-empty")
    res_cr, err_cr = dispatch_knowledge_read_tool(call_cr, "workspace-empty", "err")
    assert err_cr == "err"
    assert res_cr is not None
    assert res_cr["count"] == 0
    assert res_cr["total"] == 0
    assert res_cr["calc_runs"] == []

    # 6. ai_jobs_recent
    call_aj = _make_call({"grant_id": "g-1", "view": "ai_jobs_recent"}, workspace_id="workspace-empty")
    res_aj, err_aj = dispatch_knowledge_read_tool(call_aj, "workspace-empty", "err")
    assert err_aj == "err"
    assert res_aj is not None
    assert res_aj["count"] == 0
    assert res_aj["total"] == 0
    assert res_aj["ai_jobs"] == []

    # 7. table_counts
    call_tc = _make_call({"grant_id": "g-1", "view": "table_counts"}, workspace_id="workspace-empty")
    res_tc, err_tc = dispatch_knowledge_read_tool(call_tc, "workspace-empty", "err")
    assert err_tc == "err"
    assert res_tc is not None
    counts = res_tc["counts"]
    assert counts["memory_records"] == 0
    assert counts["literature_sources"] == 0
    assert counts["literature_entries"] == 0
    assert counts["ai_threads"] == 0
    assert counts["ai_jobs"] == 0
    assert counts["calc_runs"] == 0
    assert counts["artifacts"] == 0
    assert counts["project_revisions"] == 0
    assert counts["project_validations"] == 0

    # 8. workspaces (scoped to nonexistent)
    call_ws = _make_call(
        {"grant_id": "g-1", "view": "workspaces", "workspace_id": "workspace-nonexistent"},
        workspace_id="workspace-empty",
    )
    res_ws, err_ws = dispatch_knowledge_read_tool(call_ws, "workspace-empty", "err")
    assert err_ws == "err"
    assert res_ws is not None
    assert res_ws["count"] == 0
    assert res_ws["workspaces"] == []


def test_no_absolute_host_path_in_any_view(isolated_db) -> None:
    """Finding 4: No absolute host path appears in any view output when DB rows contain them."""
    import re

    now = "2026-11-22T05:00:00Z"
    ws = "workspace-paths"
    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES (?, 'Path Test WS', 'ws-paths', 'Config at /etc/service/config.yaml', 'active', ?, ?)",
            (ws, now, now),
        )
        conn.execute(
            "INSERT INTO assumptions (id, workspace_id, statement, scope, confidence, status, source_ref, origin, created_at, updated_at, notes) "
            "VALUES ('assump-path', ?, 'Data in /srv/customer/secrets.txt', 'ambient', 'high', 'accepted', 'ref', 'user', ?, ?, 'Token at /data/x/token.txt')",
            (ws, now, now),
        )
        conn.execute(
            "INSERT INTO literature_sources (id, workspace_id, title, source_kind, state, published_year, citation, publisher, created_at, updated_at) "
            "VALUES ('src-path', ?, 'Paper at /opt/storage/report.pdf', 'paper', 'accepted', 2024, 'Archive: /var/data/archive.pdf', 'Pub', ?, ?)",
            (ws, now, now),
        )
        conn.execute(
            "INSERT INTO literature_entries (id, workspace_id, source_id, entry_kind, statement, value_number, unit, status, locator_kind, locator_start, locator_end, created_at, updated_at) "
            "VALUES ('entry-path', ?, 'src-path', 'claim', 'Evidence from /srv/models/dataset.csv', NULL, NULL, 'accepted', 'section', 1, 10, ?, ?)",
            (ws, now, now),
        )
        conn.execute(
            "INSERT INTO project_knowledge_revisions (id, workspace_id, parent_kind, state, change_set_digest, change_set_json, projected_state_digest, origin, created_at, accepted_at) "
            "VALUES ('rev-path', ?, 'working', 'reconciled', 'dig', ?, 'proj', 'Imported from /data/repo/project.git', ?, ?)",
            (ws, json.dumps([{"file": "/etc/secrets/key.pem"}]), now, now),
        )
        conn.execute(
            "INSERT INTO model_specs (id, workspace_id, title, engineering_question, created_at, updated_at) "
            "VALUES ('spec-path', ?, 'Spec', 'Question', ?, ?)",
            (ws, now, now),
        )
        conn.execute(
            "INSERT INTO model_versions (id, workspace_id, model_spec_id, version_label, created_at) "
            "VALUES ('calc-path', ?, 'spec-path', 'v1', ?)",
            (ws, now),
        )
        conn.execute(
            "INSERT INTO simulation_runs (id, workspace_id, model_version_id, run_label, status, input_payload, output_payload, created_at, notes) "
            "VALUES ('sim-path', ?, 'calc-path', 'run-path', 'succeeded', ?, ?, ?, 'Run log at /var/log/sim.log')",
            (ws, json.dumps({"input_file": "/srv/calc/input.dat"}), json.dumps({"output_file": "/srv/calc/output.dat"}), now),
        )
        conn.execute(
            "INSERT INTO ai_flows (id, workspace_id, task_kind, state, created_at, updated_at, completed_at) "
            "VALUES ('flow-path', ?, 'general', 'complete', ?, ?, ?)",
            (ws, now, now, now),
        )
        conn.execute(
            "INSERT INTO ai_jobs (id, created_at, status, task_kind, requested_route_class, selected_route_class, provider_id, model_id, route_reason_json, flow_id, error_type) "
            "VALUES ('job-path', ?, 'success', 'general', 'local:test', 'local:test', 'local', 'model', '{}', 'flow-path', 'Error in /tmp/crash.log')",
            (now,),
        )
        conn.commit()

    raw_paths = [
        "/srv/customer/secrets.txt",
        "/data/x/token.txt",
        "/opt/storage/report.pdf",
        "/var/data/archive.pdf",
        "/srv/models/dataset.csv",
        "/etc/service/config.yaml",
        "/data/repo/project.git",
        "/etc/secrets/key.pem",
        "/srv/calc/input.dat",
        "/srv/calc/output.dat",
        "/var/log/sim.log",
        "/tmp/crash.log",
    ]

    all_views = [
        "workspaces",
        "memory_records",
        "literature_sources",
        "literature_entries",
        "project_revisions",
        "calc_runs",
        "ai_jobs_recent",
        "table_counts",
    ]

    for view in all_views:
        args = {"grant_id": "g-1", "view": view}
        if view == "workspaces":
            args["workspace_id"] = ws
        call = _make_call(args, workspace_id=ws)
        result, error = dispatch_knowledge_read_tool(call, ws, "default_error")
        assert error == "default_error"
        assert result is not None
        dumped = json.dumps(result)

        # Assert no raw absolute host path appears anywhere in view output
        for p in raw_paths:
            assert p not in dumped, f"Raw host path {p} leaked in view {view} output!"

        # Assert regex finds no host absolute paths
        assert re.search(r"(?<![\w:/~])/(?:[^\s,;\"'<>(){}\[\]/]+/)+[^\s,;\"'<>(){}\[\]/]+", dumped) is None, (
            f"Unredacted absolute path found in view {view}: {dumped}"
        )


def test_secret_scrubbing(isolated_db) -> None:
    """Acceptance criterion 4: secrets (sk-..., passwords, tokens) and paths are scrubbed."""
    call = _make_call({"grant_id": "g-1", "view": "memory_records"}, workspace_id="workspace-b")
    result, error = dispatch_knowledge_read_tool(call, "workspace-b", "default_error")

    assert result is not None
    record = result["memory_records"][0]
    assert ("sk-" + "12345678901234567890") not in record["summary"]
    assert "[REDACTED_SECRET]" in record["summary"]


def test_payload_truncation(monkeypatch: pytest.MonkeyPatch, isolated_db) -> None:
    """Large payloads exceeding 16,000 characters are capped and marked truncated=True."""
    # Insert multiple long items in workspace-a
    with open_sqlite_connection() as conn:
        long_text = "Engineering analysis " * 15  # ~300 chars
        now = "2026-11-22T05:00:00Z"
        for i in range(100):
            conn.execute(
                "INSERT INTO assumptions (id, workspace_id, statement, scope, status, origin, created_at, updated_at, notes) "
                "VALUES (?, 'workspace-a', ?, 'scope', 'accepted', 'user', ?, ?, ?)",
                (f"assump-long-{i}", f"Statement {i} {long_text}", now, now, f"Notes {i} {long_text}"),
            )
        conn.commit()

    call = _make_call({"grant_id": "g-1", "view": "memory_records", "limit": 100})
    result, error = dispatch_knowledge_read_tool(call, "workspace-a", "default_error")

    assert result is not None
    assert result["truncated"] is True
    assert len(json.dumps(result, ensure_ascii=False)) <= 16_000


# ---------------------------------------------------------------------------
# Supervisor Integration Test
# ---------------------------------------------------------------------------


def test_supervisor_dispatch_tool_knowledge_read(isolated_db) -> None:
    """Acceptance criterion 1: supervisor.dispatch_tool dispatches jarvis.knowledge_read with grant."""
    grant = _make_grant(grant_id="grant-123", capability_id="jarvis.knowledge_read", workspace_id="workspace-a")
    call = _make_call({"grant_id": "grant-123", "view": "table_counts"}, grant_id="grant-123")

    tool_result = dispatch_tool(call, live_grants={"grant-123": grant})
    assert tool_result.status == "succeeded"
    assert tool_result.error_code is None
    assert tool_result.capability_id == "jarvis.knowledge_read"
    assert tool_result.result is not None
    assert tool_result.result["view"] == "table_counts"
    assert tool_result.result["counts"]["workspaces"] == 2


def test_supervisor_dispatch_refuses_expired_grant(isolated_db) -> None:
    """Supervisor refuses expired knowledge_read grant."""
    grant = _make_grant(grant_id="grant-expired", capability_id="jarvis.knowledge_read", expired=True)
    call = _make_call({"grant_id": "grant-expired", "view": "table_counts"}, grant_id="grant-expired")

    tool_result = dispatch_tool(call, live_grants={"grant-expired": grant})
    assert tool_result.status == "refused"
    assert tool_result.error_code == "capability_denied"


def test_file_url_path_scrubbed_and_https_preserved() -> None:
    """Requirement 1: _scrub_string redacts file:// URL paths (case-insensitive) without touching http(s) URLs."""
    from app.modules.agents.hermes.knowledge_read_tool import _scrub_string

    scrubbed_file = _scrub_string("file:///srv/customer/secrets.txt", "")
    assert "/srv/customer" not in scrubbed_file
    assert scrubbed_file == "[REDACTED_PATH]"

    scrubbed_upper = _scrub_string("FILE:///srv/x/y", "")
    assert "/srv/x/y" not in scrubbed_upper
    assert scrubbed_upper == "[REDACTED_PATH]"

    scrubbed_mixed = _scrub_string("File:///srv/customer/secrets.txt", "")
    assert "/srv/customer" not in scrubbed_mixed
    assert scrubbed_mixed == "[REDACTED_PATH]"

    scrubbed_https = _scrub_string("https://example.org/a/b", "")
    assert scrubbed_https == "https://example.org/a/b"


def test_payload_bounds_just_under_cap_without_truncated_key() -> None:
    """Requirement 2: payload 15,990 chars without truncated key ends <= 16,000."""
    from app.modules.agents.hermes.knowledge_read_tool import _enforce_payload_bounds

    items = [{"id": f"item-{i:03d}", "data": "x" * 50} for i in range(250)]
    payload = {"view": "workspaces", "count": len(items), "workspaces": items}
    while len(json.dumps(payload)) > 15_990:
        items.pop()
        payload["count"] = len(items)
    diff = 15_990 - len(json.dumps(payload))
    payload["workspaces"][-1]["data"] += "x" * diff

    assert "truncated" not in payload
    assert len(json.dumps(payload)) == 15_990

    bounded = _enforce_payload_bounds(payload, "workspaces")
    assert "truncated" in bounded
    assert bounded["truncated"] is True
    assert len(json.dumps(bounded, ensure_ascii=False)) <= 16_000
    assert len(json.dumps(bounded)) <= 16_000


def test_kv_secret_re_superlinear_bounded() -> None:
    """Requirement 3: 20,000-char non-matching key-like string scrubs in under 0.5 s."""
    import time

    from app.modules.agents.hermes.knowledge_read_tool import _scrub_string

    non_matching = ("candidate_variable_name_without_keyword_" * 500)[:20_000]
    t0 = time.perf_counter()
    scrubbed = _scrub_string(non_matching, "")
    duration = time.perf_counter() - t0

    assert duration < 0.5, f"Scrubbing took {duration:.3f} s, expected < 0.5 s"
    assert scrubbed == non_matching


def test_kv_secret_full_value_redaction_and_long_key_suffix() -> None:
    """Unquoted secret value of any length and key with long suffix after keyword are fully redacted."""
    from app.modules.agents.hermes.knowledge_read_tool import _scrub_string

    # 280-char unquoted secret value: fully redacted with nothing of value remaining
    secret_value = "v" * 280
    scrubbed_val = _scrub_string(f"password={secret_value}", "")
    assert scrubbed_val == "password=[REDACTED_SECRET]"
    assert "v" not in scrubbed_val

    # Key with 100 chars after 'password': still has its value redacted
    long_key = "password" + ("k" * 100)
    scrubbed_key = _scrub_string(f"{long_key}=topsecretvalue", "")
    assert scrubbed_key == f"{long_key}=[REDACTED_SECRET]"
    assert "topsecretvalue" not in scrubbed_key


def test_scrub_value_repeated_password_keyword_finishes_under_half_second() -> None:
    """Requirement 1 & 3: _scrub_value on 20,000 x 'password' finishes in < 0.5 s."""
    import time

    from app.modules.agents.hermes.knowledge_read_tool import _scrub_value

    repeated_keywords = "password" * 20_000
    t0 = time.perf_counter()
    scrubbed = _scrub_value(repeated_keywords, "")
    duration = time.perf_counter() - t0

    assert duration < 0.5, f"_scrub_value took {duration:.3f} s, expected < 0.5 s"
    assert len(scrubbed) <= 300
    assert scrubbed == ("password" * 20_000)[:300]


def test_memory_record_with_1000_char_scope_tag_capped(isolated_db) -> None:
    """Requirement 1 & 3: A memory record with a 1,000-char scope/tag is returned capped at 300 chars."""
    now = "2026-11-22T05:00:00Z"
    long_scope = "tagscope_" + ("x" * 1000)
    long_symbol = "paramtag_" + ("y" * 1000)

    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT INTO assumptions (id, workspace_id, statement, scope, confidence, status, origin, created_at, updated_at) "
            "VALUES ('assump-long-scope', 'workspace-a', 'Long scope tag test', ?, 'high', 'accepted', 'user', ?, ?)",
            (long_scope, now, now),
        )
        conn.execute(
            "INSERT INTO parameters (id, workspace_id, name, symbol, value, unit, status, confidence, origin, created_at, updated_at) "
            "VALUES ('param-long-sym', 'workspace-a', 'Long symbol tag test', ?, '1.0', 'm', 'accepted', 0.9, 'user', ?, ?)",
            (long_symbol, now, now),
        )
        conn.commit()

    call_scope = _make_call({"grant_id": "g-1", "view": "memory_records", "query": "Long scope tag test"})
    res_scope, _ = dispatch_knowledge_read_tool(call_scope, "workspace-a", "default_error")
    assert res_scope is not None
    assert res_scope["count"] == 1
    rec_scope = res_scope["memory_records"][0]
    assert len(rec_scope["tags"]) == 1
    assert len(rec_scope["tags"][0]) == 300
    assert rec_scope["tags"][0] == long_scope[:300]

    call_param = _make_call({"grant_id": "g-1", "view": "memory_records", "query": "Long symbol tag test"})
    res_param, _ = dispatch_knowledge_read_tool(call_param, "workspace-a", "default_error")
    assert res_param is not None
    assert res_param["count"] == 1
    rec_param = res_param["memory_records"][0]
    assert len(rec_param["tags"]) == 1
    assert len(rec_param["tags"][0]) == 300
    assert rec_param["tags"][0] == long_symbol[:300]


def test_scrub_value_redacts_tail_secret_fragment_at_cap() -> None:
    """Secret fragments cut at the 300-char cap are replaced with [REDACTED_SECRET]; normal strings unchanged."""
    from app.modules.agents.hermes.knowledge_read_tool import _scrub_value

    # 1. password="..." straddling char 300
    straddle_pw = ("A" * 285) + 'password="sensitive_secret_data"' + (" B" * 40)
    scrubbed_pw = _scrub_value(straddle_pw, "")
    assert "sensitive" not in scrubbed_pw
    assert "[REDACTED_SECRET]" in scrubbed_pw
    assert scrubbed_pw.endswith("[REDACTED_SECRET]")

    # 2. sk- token straddling char 300
    straddle_sk = ("C" * 290) + "sk-abcdef1234567890abcdef" + (" D" * 40)
    scrubbed_sk = _scrub_value(straddle_sk, "")
    assert "abcdef" not in scrubbed_sk
    assert "[REDACTED_SECRET]" in scrubbed_sk
    assert scrubbed_sk.endswith("[REDACTED_SECRET]")

    # 3. Bearer and ghp_ fragments straddling char 300
    straddle_bearer = ("E" * 292) + "Bearer auth_token_secret" + (" F" * 40)
    scrubbed_bearer = _scrub_value(straddle_bearer, "")
    assert "auth_token" not in scrubbed_bearer
    assert "[REDACTED_SECRET]" in scrubbed_bearer

    straddle_ghp = ("G" * 292) + "ghp_tokendata12345" + (" H" * 40)
    scrubbed_ghp = _scrub_value(straddle_ghp, "")
    assert "tokendata" not in scrubbed_ghp
    assert "[REDACTED_SECRET]" in scrubbed_ghp

    # 4. Normal 300-char string without secrets is unchanged
    normal_300 = ("Standard engineering operating parameter without secrets. " * 8)[:300]
    assert len(normal_300) == 300
    assert _scrub_value(normal_300, "") == normal_300


def test_view_memory_records_redacts_secret_fragment_at_field_cap(isolated_db) -> None:
    """Real view test: memory_records with 400-char notes/statement straddling char 300 redacts secrets."""
    now = "2026-11-22T05:00:00Z"
    long_notes_pw = ("A" * 285) + 'password="sensitive_database_password_xyz"' + (" B" * 40)
    long_notes_sk = ("C" * 290) + "sk-1234567890abcdef1234567890" + (" D" * 40)
    normal_long_notes = ("Normal operating temperature condition for reactor. " * 10)[:400]

    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT INTO assumptions (id, workspace_id, statement, scope, confidence, status, origin, created_at, updated_at, notes) "
            "VALUES ('assump-frag-pw', 'workspace-a', 'Straddle password test', 'ambient', 'high', 'accepted', 'user', ?, ?, ?)",
            (now, now, long_notes_pw),
        )
        conn.execute(
            "INSERT INTO assumptions (id, workspace_id, statement, scope, confidence, status, origin, created_at, updated_at, notes) "
            "VALUES ('assump-frag-sk', 'workspace-a', 'Straddle sk test', 'ambient', 'high', 'accepted', 'user', ?, ?, ?)",
            (now, now, long_notes_sk),
        )
        conn.execute(
            "INSERT INTO assumptions (id, workspace_id, statement, scope, confidence, status, origin, created_at, updated_at, notes) "
            "VALUES ('assump-normal-300', 'workspace-a', 'Normal long notes test', 'ambient', 'high', 'accepted', 'user', ?, ?, ?)",
            (now, now, normal_long_notes),
        )
        conn.commit()

    # Query password straddle
    call_pw = _make_call({"grant_id": "g-1", "view": "memory_records", "query": "Straddle password test"})
    res_pw, err_pw = dispatch_knowledge_read_tool(call_pw, "workspace-a", "default_error")
    assert err_pw == "default_error"
    assert res_pw is not None and res_pw["count"] == 1
    rec_pw = res_pw["memory_records"][0]
    assert "sensitive" not in rec_pw["summary"]
    assert "[REDACTED_SECRET]" in rec_pw["summary"]

    # Query sk straddle
    call_sk = _make_call({"grant_id": "g-1", "view": "memory_records", "query": "Straddle sk test"})
    res_sk, err_sk = dispatch_knowledge_read_tool(call_sk, "workspace-a", "default_error")
    assert err_sk == "default_error"
    assert res_sk is not None and res_sk["count"] == 1
    rec_sk = res_sk["memory_records"][0]
    assert "123456" not in rec_sk["summary"]
    assert "[REDACTED_SECRET]" in rec_sk["summary"]

    # Query normal 300-char string without secrets is capped to 300 chars without mangling
    call_normal = _make_call({"grant_id": "g-1", "view": "memory_records", "query": "Normal long notes test"})
    res_normal, err_normal = dispatch_knowledge_read_tool(call_normal, "workspace-a", "default_error")
    assert err_normal == "default_error"
    assert res_normal is not None and res_normal["count"] == 1
    rec_normal = res_normal["memory_records"][0]
    assert rec_normal["summary"] == normal_long_notes[:300]



