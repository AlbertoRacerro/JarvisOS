"""Tests for Spec 362 — HERMES-SAVED-PLAN-1.

Offline unit tests covering:
1. Broker tool `jarvis_turn_plan`: bounds validation, enums, secrets/pack protection,
   persistence of `hermes.turn_plan` events, exemption from research budget, and
   identical-call blocking bypass for read_plan.
2. Per-step plan re-injection: compact formatting, bounded length (<= 2400 chars),
   and re-injection into messages on subsequent model steps after tool shrinking.
3. Interaction read projection: AIThreadInteractionRead projection of turn_plan and live_steps.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.modules.agents.hermes import broker_mcp, worker_shim
from app.modules.agents.hermes.supervisor import (
    HERMES_TOOL_ALLOWLIST,
    UPSTREAM_REVISION,
    HermesSupervisor,
    _assemble_turn_prompt,
    is_research_tool,
)
from app.modules.agents.hermes.turn_plan_tool import dispatch_turn_plan_tool
from app.modules.agents.hermes.worker_shim import (
    format_turn_plan_summary,
    inject_saved_plan,
    shrink_tool_history,
)
from app.modules.ai import thread_service
from app.modules.ai.agent_contracts import (
    AgentSessionRef,
    CapabilityGrantRef,
    CapabilityScope,
    StructuredToolCall,
)
from app.modules.ai.thread_models import (
    AIThreadInteractionRead,
)

NOW = datetime.now(UTC)
SESSION = AgentSessionRef(
    jarvis_thread_id="thread-362",
    hermes_session_id="hermes-362",
    profile_id="isaac",
    workspace_id="workspace-362",
    generation=1,
    upstream_revision=UPSTREAM_REVISION,
)


def _existing_connection(conn: sqlite3.Connection):
    class Ctx:
        def __enter__(self):
            return conn

        def __exit__(self, *args):
            pass

    return Ctx()


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE ai_threads (id TEXT PRIMARY KEY, workspace_id TEXT, title TEXT, created_at TEXT, last_activity_at TEXT)")
    connection.execute("INSERT INTO ai_threads VALUES ('thread-362', 'workspace-362', 'Thread 362', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
    connection.execute(
        """
        CREATE TABLE ai_thread_interactions (
            id TEXT PRIMARY KEY, thread_id TEXT, request_id TEXT, request_digest TEXT,
            interaction_index INTEGER, user_text TEXT, assistant_text TEXT,
            assistant_text_truncated INTEGER, flow_id TEXT, persistence_state TEXT,
            persistence_error TEXT, flow_state TEXT, terminal_reason TEXT,
            attempt_count INTEGER, terminal_attempt_id TEXT, requested_route_class TEXT,
            execution_class TEXT, model_id TEXT, provider_id TEXT, input_tokens INTEGER,
            output_tokens INTEGER, cost_estimate REAL, usage_source TEXT, latency_ms INTEGER,
            completed_at TEXT, surface_summary TEXT, surface_digest TEXT,
            surface_brief_json TEXT, proposal_ids_json TEXT, image_count INTEGER DEFAULT 0,
            images_unsupported INTEGER DEFAULT 0, technical_details TEXT,
            created_at TEXT, updated_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE events (
            id TEXT PRIMARY KEY, workspace_id TEXT, event_type TEXT,
            actor TEXT, target_type TEXT, target_id TEXT, payload TEXT, created_at TEXT
        )
        """
    )
    return connection


def _make_grant(
    capability_id: str = "jarvis.turn_plan",
    grant_id: str = "grant-362",
    workspace_id: str = "workspace-362",
    thread_id: str = "thread-362",
) -> CapabilityGrantRef:
    return CapabilityGrantRef(
        grant_id=grant_id,
        capability_id=capability_id,
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=workspace_id, jarvis_thread_id=thread_id),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )


# ===========================================================================
# 1. Broker tool `jarvis_turn_plan` validation and persistence
# ===========================================================================


def test_turn_plan_tool_schema_and_allowlist() -> None:
    """Verify jarvis_turn_plan is admitted in broker MCP schemas and allowlists."""
    assert "mcp__jarvis__jarvis_turn_plan" in HERMES_TOOL_ALLOWLIST
    assert "jarvis_turn_plan" in HERMES_TOOL_ALLOWLIST
    assert "turn_plan" in HERMES_TOOL_ALLOWLIST
    tool_names = {t["name"] for t in broker_mcp._TOOLS}
    assert "jarvis_turn_plan" in tool_names
    assert "jarvis_turn_plan" in broker_mcp._NAMED


def test_turn_plan_exempt_from_research_budget() -> None:
    """Requirement 1: jarvis_turn_plan must not be classified as a research tool."""
    assert not is_research_tool("jarvis_turn_plan")
    assert not is_research_tool("mcp__jarvis__jarvis_turn_plan")
    assert not is_research_tool("turn_plan")
    assert not worker_shim.is_research_tool("jarvis_turn_plan")


def test_turn_plan_set_plan_validation_and_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test set_plan bounds validation, enum validation, and event persistence."""
    conn = _connection()
    monkeypatch.setattr("app.modules.agents.hermes.turn_plan_tool.open_sqlite_connection", lambda: _existing_connection(conn))

    grant = _make_grant("jarvis.turn_plan", "grant-plan")
    active_plan: dict[str, Any] = {}

    # Valid set_plan
    call = StructuredToolCall(
        call_id="call-set-1", capability_id="jarvis.turn_plan", grant_id="grant-plan",
        correlation_id="call-set-1", session_ref=SESSION,
        arguments={
            "op": "set_plan",
            "steps": [
                {"id": "step-1", "title": "Check thermodynamics", "status": "pending"},
                {"id": "step-2", "title": "Size condenser", "status": "in_progress", "note": "using 200 K delta T"},
            ],
        },
        requested_at=NOW, deadline_at=NOW + timedelta(seconds=60),
    )
    result, error = dispatch_turn_plan_tool(call, "workspace-362", "denied", grant=grant, interaction_id="inter-1", active_plan=active_plan)
    assert error is None or result is not None
    assert result["status"] == "succeeded"
    assert len(result["steps"]) == 2
    assert result["steps"][0]["title"] == "Check thermodynamics"

    # Verify persisted hermes.turn_plan event
    row = conn.execute(
        "SELECT event_type, target_id, payload FROM events WHERE event_type = 'hermes.turn_plan'"
    ).fetchone()
    assert row is not None
    assert row["target_id"] == "inter-1"
    evt_data = json.loads(row["payload"])
    assert len(evt_data["steps"]) == 2

    # Exceeding 10 steps must be refused
    too_many_steps = [{"id": f"s-{i}", "title": f"Step {i}", "status": "pending"} for i in range(11)]
    call_overflow = StructuredToolCall(
        call_id="call-overflow", capability_id="jarvis.turn_plan", grant_id="grant-plan",
        correlation_id="call-overflow", session_ref=SESSION,
        arguments={"op": "set_plan", "steps": too_many_steps},
        requested_at=NOW, deadline_at=NOW + timedelta(seconds=60),
    )
    res_bad, err_bad = dispatch_turn_plan_tool(call_overflow, "workspace-362", "denied", grant=grant, interaction_id="inter-1", active_plan=active_plan)
    assert res_bad is None
    assert err_bad == "invalid_steps"

    # Invalid status must be refused
    call_bad_status = StructuredToolCall(
        call_id="call-status", capability_id="jarvis.turn_plan", grant_id="grant-plan",
        correlation_id="call-status", session_ref=SESSION,
        arguments={"op": "set_plan", "steps": [{"id": "s-1", "title": "Bad", "status": "invalid_status"}]},
        requested_at=NOW, deadline_at=NOW + timedelta(seconds=60),
    )
    res_status, err_status = dispatch_turn_plan_tool(call_bad_status, "workspace-362", "denied", grant=grant, interaction_id="inter-1", active_plan=active_plan)
    assert res_status is None
    assert err_status == "invalid_step_status"

    # Title > 160 chars must be refused
    call_long_title = StructuredToolCall(
        call_id="call-title", capability_id="jarvis.turn_plan", grant_id="grant-plan",
        correlation_id="call-title", session_ref=SESSION,
        arguments={"op": "set_plan", "steps": [{"id": "s-1", "title": "A" * 161, "status": "pending"}]},
        requested_at=NOW, deadline_at=NOW + timedelta(seconds=60),
    )
    res_title, err_title = dispatch_turn_plan_tool(call_long_title, "workspace-362", "denied", grant=grant, interaction_id="inter-1", active_plan=active_plan)
    assert res_title is None
    assert err_title == "invalid_step_title"


def test_turn_plan_update_step_and_record_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test update_step, record_finding bounds, and read_plan."""
    conn = _connection()
    monkeypatch.setattr("app.modules.agents.hermes.turn_plan_tool.open_sqlite_connection", lambda: _existing_connection(conn))

    grant = _make_grant("jarvis.turn_plan", "grant-plan")
    active_plan: dict[str, Any] = {
        "steps": [{"id": "step-1", "title": "Inspect parameters", "status": "pending"}],
        "findings": [],
    }

    # Update step to in_progress
    call_update = StructuredToolCall(
        call_id="call-u-1", capability_id="jarvis.turn_plan", grant_id="grant-plan",
        correlation_id="call-u-1", session_ref=SESSION,
        arguments={"op": "update_step", "id": "step-1", "status": "in_progress", "note": "inspecting model constants"},
        requested_at=NOW, deadline_at=NOW + timedelta(seconds=60),
    )
    res_u, _ = dispatch_turn_plan_tool(call_update, "workspace-362", "denied", grant=grant, interaction_id="inter-1", active_plan=active_plan)
    assert res_u["steps"][0]["status"] == "in_progress"
    assert res_u["steps"][0]["note"] == "inspecting model constants"

    # Record finding
    call_find = StructuredToolCall(
        call_id="call-f-1", capability_id="jarvis.turn_plan", grant_id="grant-plan",
        correlation_id="call-f-1", session_ref=SESSION,
        arguments={
            "op": "record_finding",
            "key": "heat_capacity_cp",
            "value": 4.184,
            "unit": "J/(g*K)",
            "source_ref": "doi:10.1000/182",
        },
        requested_at=NOW, deadline_at=NOW + timedelta(seconds=60),
    )
    res_f, _ = dispatch_turn_plan_tool(call_find, "workspace-362", "denied", grant=grant, interaction_id="inter-1", active_plan=active_plan)
    assert len(res_f["findings"]) == 1
    assert res_f["findings"][0]["key"] == "heat_capacity_cp"
    assert res_f["findings"][0]["value"] == 4.184

    # Finding record total length > 240 chars must be refused
    call_huge_finding = StructuredToolCall(
        call_id="call-f-huge", capability_id="jarvis.turn_plan", grant_id="grant-plan",
        correlation_id="call-f-huge", session_ref=SESSION,
        arguments={"op": "record_finding", "key": "k", "value": "V" * 250},
        requested_at=NOW, deadline_at=NOW + timedelta(seconds=60),
    )
    res_huge, err_huge = dispatch_turn_plan_tool(call_huge_finding, "workspace-362", "denied", grant=grant, interaction_id="inter-1", active_plan=active_plan)
    assert res_huge is None
    assert err_huge == "finding_too_large"

    # Read plan returns current snapshot
    call_read = StructuredToolCall(
        call_id="call-read-1", capability_id="jarvis.turn_plan", grant_id="grant-plan",
        correlation_id="call-read-1", session_ref=SESSION,
        arguments={"op": "read_plan"},
        requested_at=NOW, deadline_at=NOW + timedelta(seconds=60),
    )
    res_read, _ = dispatch_turn_plan_tool(call_read, "workspace-362", "denied", grant=grant, interaction_id="inter-1", active_plan=active_plan)
    assert len(res_read["steps"]) == 1
    assert len(res_read["findings"]) == 1


def test_turn_plan_sanitizes_pack_path_and_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement 1: never emit secrets or private pack paths."""
    conn = _connection()
    monkeypatch.setattr("app.modules.agents.hermes.turn_plan_tool.open_sqlite_connection", lambda: _existing_connection(conn))
    monkeypatch.setenv("JARVISOS_PACK_PATH", "/home/secret/private-pack")

    grant = _make_grant("jarvis.turn_plan", "grant-plan")
    active_plan: dict[str, Any] = {}

    # Pack path in step note is sanitized
    call = StructuredToolCall(
        call_id="call-p-1", capability_id="jarvis.turn_plan", grant_id="grant-plan",
        correlation_id="call-p-1", session_ref=SESSION,
        arguments={
            "op": "set_plan",
            "steps": [{"id": "s1", "title": "Load data", "status": "pending", "note": "file from /home/secret/private-pack/spec.md"}],
        },
        requested_at=NOW, deadline_at=NOW + timedelta(seconds=60),
    )
    res, _ = dispatch_turn_plan_tool(call, "workspace-362", "denied", grant=grant, interaction_id="inter-1", active_plan=active_plan)
    assert "/home/secret/private-pack" not in res["steps"][0]["note"]
    assert "[pack_root]" in res["steps"][0]["note"]

    # Secret in finding value is refused
    call_sec = StructuredToolCall(
        call_id="call-sec", capability_id="jarvis.turn_plan", grant_id="grant-plan",
        correlation_id="call-sec", session_ref=SESSION,
        arguments={"op": "record_finding", "key": "auth", "value": "api_key=sk-12345678"},
        requested_at=NOW, deadline_at=NOW + timedelta(seconds=60),
    )
    res_sec, err_sec = dispatch_turn_plan_tool(call_sec, "workspace-362", "denied", grant=grant, interaction_id="inter-1", active_plan=active_plan)
    assert res_sec is None
    assert err_sec == "finding_contains_secret"


def test_identical_call_blocking_exempts_read_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance criterion 1 & design constraint: read_plan is not blocked if called multiple times."""
    conn = _connection()
    monkeypatch.setattr("app.modules.agents.hermes.supervisor.open_sqlite_connection", lambda: _existing_connection(conn))
    monkeypatch.setattr("app.modules.agents.hermes.turn_plan_tool.open_sqlite_connection", lambda: _existing_connection(conn))

    supervisor_inst = HermesSupervisor("unused", runner=MagicMock())
    supervisor_inst.session = SESSION
    grant = _make_grant("jarvis.turn_plan", "grant-plan")
    supervisor_inst.live_grants = {"grant-plan": grant}

    sent_frames: list[dict[str, Any]] = []
    supervisor_inst._send = lambda f: sent_frames.append(f)

    frame_read_1 = {
        "id": "call-r-1",
        "session_ref": SESSION.model_dump(mode="json"),
        "arguments": {"tool_name": "jarvis_turn_plan", "grant_id": "grant-plan", "op": "read_plan"},
    }
    frame_read_2 = {
        "id": "call-r-2",
        "session_ref": SESSION.model_dump(mode="json"),
        "arguments": {"tool_name": "jarvis_turn_plan", "grant_id": "grant-plan", "op": "read_plan"},
    }

    supervisor_inst._handle_tool(frame_read_1)
    assert sent_frames[0]["tool_result"]["status"] == "succeeded"
    assert "Tool call blocked" not in str(sent_frames[0]["tool_result"]["result"])

    supervisor_inst._handle_tool(frame_read_2)
    assert sent_frames[1]["tool_result"]["status"] == "succeeded"
    # Second read_plan call MUST NOT be blocked!
    assert "Tool call blocked" not in str(sent_frames[1]["tool_result"]["result"])


# ===========================================================================
# 2. Per-step plan re-injection into messages
# ===========================================================================


def test_format_turn_plan_summary_bounded() -> None:
    """Test format_turn_plan_summary compact rendering and max_len bounding."""
    plan = {
        "steps": [
            {"id": "step-1", "title": "Derive equation", "status": "done", "note": "analytical form"},
            {"id": "step-2", "title": "Calculate rate", "status": "in_progress"},
            {"id": "step-3", "title": "Verify boundary conditions", "status": "pending"},
        ],
        "findings": [
            {"key": "k_rate", "value": 0.042, "unit": "1/s", "source_ref": "exp-run-1"},
            {"key": "T_reactor", "value": 310, "unit": "K"},
        ],
    }
    summary = format_turn_plan_summary(plan, max_len=2400)
    assert "[Active turn plan]" in summary
    assert "Derive equation" in summary
    assert "[done] step-1" in summary
    assert "[in_progress] step-2" in summary
    assert "k_rate: 0.042 1/s (exp-run-1)" in summary
    assert len(summary) <= 2400


def test_inject_saved_plan_into_messages_after_shrink() -> None:
    """Requirement 2: plan re-injection into message list after shrink_tool_history."""
    messages = [
        {"role": "system", "content": "You are Hermes."},
        {"role": "user", "content": "Calculate the cooling load."},
        {"role": "assistant", "content": None, "tool_calls": [{"name": "tool1"}]},
        {"role": "tool", "content": "Old output " + ("X" * 400)},
        {"role": "assistant", "content": None, "tool_calls": [{"name": "tool2"}]},
        {"role": "tool", "content": "Recent output " + ("Y" * 100)},
    ]

    # Shrink older tool messages
    shrunk = shrink_tool_history(messages, keep_recent=1, max_shrunk_len=100)
    assert "[prior tool output shrunk]" in shrunk[3]["content"]

    plan = {
        "steps": [{"id": "s1", "title": "Calculate heat duty", "status": "in_progress"}],
        "findings": [{"key": "duty_kw", "value": 150.5, "unit": "kW", "source_ref": "calc_step_1"}],
    }

    injected = inject_saved_plan(shrunk, plan)
    # Re-injected plan must be present in system message
    assert "[Active turn plan]" in injected[0]["content"]
    assert "Calculate heat duty" in injected[0]["content"]
    assert "duty_kw: 150.5 kW (calc_step_1)" in injected[0]["content"]
    # Shrunk message remains shrunk
    assert "[prior tool output shrunk]" in injected[3]["content"]


def test_engineering_methodology_rules_extended_with_turn_plan_rule() -> None:
    """Requirement 2: ENGINEERING_METHODOLOGY_RULES contains turn plan rule."""
    assembled = _assemble_turn_prompt("Multi-step sizing problem")
    assert "jarvis_turn_plan" in assembled
    assert "intermediate findings" in assembled
    assert "Never invent a DOI" in assembled


# ===========================================================================
# 3. Interaction read projection (turn_plan and live_steps)
# ===========================================================================


def test_interaction_read_projection_with_plan_and_live_steps() -> None:
    """Criterion 3: AIThreadInteractionRead projects turn_plan and live_steps from events."""
    conn = _connection()
    inter_id = "inter-test-1"

    # Insert interaction
    conn.execute(
        """
        INSERT INTO ai_thread_interactions (
            id, thread_id, request_id, request_digest, interaction_index, user_text,
            assistant_text, assistant_text_truncated, flow_id, persistence_state,
            flow_state, attempt_count, created_at, updated_at
        ) VALUES (
            ?, 'thread-362', 'req-1', 'sha256:abc', 1, 'Question',
            'Answer text', 0, 'flow-1', 'captured', 'complete', 1,
            '2026-01-01T10:00:00+00:00', '2026-01-01T10:00:05+00:00'
        )
        """,
        (inter_id,),
    )

    # Insert hermes.turn_plan event
    plan_payload = {
        "interaction_id": inter_id,
        "steps": [
            {"id": "1", "title": "Check flow", "status": "done"},
            {"id": "2", "title": "Calculate pressure drop", "status": "in_progress"},
        ],
        "findings": [
            {"key": "delta_p", "value": 1.25, "unit": "bar", "source_ref": "darcy_calc"},
        ],
    }
    conn.execute(
        "INSERT INTO events VALUES ('evt-plan-1', 'workspace-362', 'hermes.turn_plan', 'jarvis', 'ai_thread_interaction', ?, ?, '2026-01-01T10:00:01+00:00')",
        (inter_id, json.dumps(plan_payload)),
    )

    # Insert tool started & tool result events
    conn.execute(
        "INSERT INTO events VALUES ('evt-t-1', 'workspace-362', 'hermes.tool_started', 'jarvis', 'agent_tool_call', 'call-1', ?, '2026-01-01T10:00:02+00:00')",
        (json.dumps({"call_id": "call-1", "tool_name": "jarvis_retrieval_query", "interaction_id": inter_id}),),
    )
    conn.execute(
        "INSERT INTO events VALUES ('evt-t-2', 'workspace-362', 'hermes.tool_result', 'jarvis', 'agent_tool_call', 'call-1', ?, '2026-01-01T10:00:03+00:00')",
        (json.dumps({"call_id": "call-1", "tool_name": "jarvis_retrieval_query", "status": "succeeded", "interaction_id": inter_id}),),
    )
    conn.execute(
        "INSERT INTO events VALUES ('evt-t-3', 'workspace-362', 'hermes.tool_started', 'jarvis', 'agent_tool_call', 'call-2', ?, '2026-01-01T10:00:04+00:00')",
        (json.dumps({"call_id": "call-2", "tool_name": "jarvis_turn_plan", "interaction_id": inter_id}),),
    )
    conn.commit()

    # Project interaction
    row = conn.execute("SELECT * FROM ai_thread_interactions WHERE id = ?", (inter_id,)).fetchone()
    read_obj: AIThreadInteractionRead = thread_service._interaction_from_row(row, conn)

    assert read_obj.turn_plan is not None
    assert len(read_obj.turn_plan.steps) == 2
    assert read_obj.turn_plan.steps[0].title == "Check flow"
    assert read_obj.turn_plan.steps[0].status == "done"
    assert len(read_obj.turn_plan.findings) == 1
    assert read_obj.turn_plan.findings[0].key == "delta_p"
    assert read_obj.turn_plan.findings[0].value == 1.25

    # Check live_steps
    assert len(read_obj.live_steps) == 2
    assert read_obj.live_steps[0].call_id == "call-1"
    assert read_obj.live_steps[0].status == "succeeded"
    assert "knowledge" in read_obj.live_steps[0].label.lower()
    assert read_obj.live_steps[1].call_id == "call-2"
    assert read_obj.live_steps[1].status == "running"
