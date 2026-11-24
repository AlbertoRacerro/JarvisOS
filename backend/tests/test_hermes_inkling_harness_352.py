"""Tests for Spec 352 — HERMES-INKLING-HARNESS-1.

Deterministic agent-loop guardrails and literature paper search tool.
All tests run 100% offline with fake HTTP openers and fake inference stubs.
"""
from __future__ import annotations

import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError
from uuid import uuid4

import pytest

from app.modules.agents.hermes import broker_mcp, worker_shim
from app.modules.agents.hermes.paper_search_tool import (
    CROSSREF_SEARCH_URL,
    EUROPE_PMC_SEARCH_URL,
    MAX_RESPONSE_BYTES,
    _DenyRedirects,
    dispatch_paper_search_tool,
    execute_paper_search,
    search_crossref,
    search_europe_pmc,
)
from app.modules.agents.hermes.supervisor import (
    _TOOL_CAPABILITIES,
    DEAD_TURN_NUDGE,
    ENGINEERING_METHODOLOGY_RULES,
    HERMES_TOOL_ALLOWLIST,
    PRODUCTION_CAPABILITY_REGISTRY,
    UPSTREAM_REVISION,
    HermesSupervisor,
    _assemble_turn_prompt,
    compact_tool_payload,
    infer_envelope,
    is_dead_turn,
    is_research_tool,
    run_governed_inference,
    shrink_tool_history,
)
from app.modules.ai import thread_service
from app.modules.ai.agent_contracts import (
    AgentSessionRef,
    CapabilityGrantRef,
    CapabilityScope,
    InferenceEnvelope,
    StructuredToolCall,
)
from app.modules.ai.execution import AiTaskOutcome

NOW = datetime.now(UTC)
SESSION = AgentSessionRef(
    jarvis_thread_id="thread-352",
    hermes_session_id="hermes-352",
    profile_id="isaac",
    workspace_id="workspace-352",
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
    connection.execute("CREATE TABLE ai_threads (id TEXT PRIMARY KEY, workspace_id TEXT, last_activity_at TEXT)")
    connection.execute("INSERT INTO ai_threads VALUES ('thread-352', 'workspace-352', '2000-01-01T00:00:00+00:00')")
    connection.execute("CREATE TABLE ai_thread_interactions (id TEXT, thread_id TEXT, request_id TEXT, "
                       "request_digest TEXT, interaction_index INTEGER, user_text TEXT, assistant_text TEXT, "
                       "assistant_text_truncated INTEGER, flow_id TEXT, persistence_state TEXT, "
                       "created_at TEXT, updated_at TEXT)")
    connection.execute("CREATE TABLE events (id TEXT PRIMARY KEY, workspace_id TEXT, event_type TEXT, "
                       "actor TEXT, target_type TEXT, target_id TEXT, payload TEXT, created_at TEXT)")
    return connection


def _make_grant(
    capability_id: str = "jarvis.paper_search",
    grant_id: str = "grant-352",
    workspace_id: str = "workspace-352",
    thread_id: str = "thread-352",
) -> CapabilityGrantRef:
    return CapabilityGrantRef(
        grant_id=grant_id,
        capability_id=capability_id,
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=workspace_id, jarvis_thread_id=thread_id),
        constraints={"route_class": "fake", "is_local": True},
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


def _make_call(
    arguments: dict[str, Any],
    capability_id: str = "jarvis.paper_search",
    grant_id: str = "grant-352",
) -> StructuredToolCall:
    return StructuredToolCall(
        call_id=str(uuid4()),
        capability_id=capability_id,
        grant_id=grant_id,
        correlation_id=str(uuid4()),
        session_ref=SESSION,
        arguments=arguments,
        requested_at=NOW,
        deadline_at=NOW + timedelta(seconds=60),
    )


class _FakeHTTPResponse:
    def __init__(self, data: bytes, headers: dict[str, str] | None = None, status: int = 200):
        self._stream = io.BytesIO(data)
        self.headers = headers or {"Content-Length": str(len(data)), "Content-Type": "application/json"}
        self.status = status

    def read(self, amt: int = -1) -> bytes:
        return self._stream.read(amt)

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _FakeOpener:
    def __init__(self, routes: dict[str, _FakeHTTPResponse | Exception]):
        self.routes = routes
        self.calls: list[str] = []

    def open(self, req: Any, timeout: float = 10.0) -> Any:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append(url)
        for pattern, response in self.routes.items():
            if pattern in url:
                if isinstance(response, Exception):
                    raise response
                return response
        raise URLError(f"unexpected fake url: {url}")


# ===========================================================================
# 1. Methodology prompt tests
# ===========================================================================


def test_assemble_turn_prompt_injects_engineering_methodology_rules() -> None:
    """Acceptance criterion 1: prompt includes concise, deterministic methodology rules."""
    prompt = "What is the yield of the reaction?"
    assembled = _assemble_turn_prompt(prompt, [])

    assert "Report numbers only from tool outputs" in assembled
    assert "Compute physics with deterministic tools rather than searching for answers" in assembled
    assert "Express uncertain inputs as an explicit range" in assembled
    assert "Read a source before citing it" in assembled
    assert "Never invent a DOI" in assembled
    assert prompt in assembled


def test_assemble_turn_prompt_idempotence() -> None:
    """Methodology rules are not duplicated if already present in prompt."""
    prompt = f"Previous prompt with rules:\n\n{ENGINEERING_METHODOLOGY_RULES}\n\nUser text."
    assembled = _assemble_turn_prompt(prompt, [])
    assert assembled.count("Engineering methodology rules:") == 1


def test_assemble_turn_prompt_with_context_blocks() -> None:
    """Methodology prompt co-exists cleanly with Jarvis validated context blocks."""
    blocks = [
        {"source": "jarvis:agent-grant", "content": "grant_data_here"},
        {"source": "jarvis:system-envelope", "content": "envelope_data_here"},
    ]
    assembled = _assemble_turn_prompt("Calculate kinetics", blocks)
    assert "Engineering methodology rules:" in assembled
    assert "grant_data_here" in assembled
    assert "envelope_data_here" in assembled
    assert len(assembled) <= 12_000


# ===========================================================================
# 2. Loop guardrails: Identical-call blocking & research budget
# ===========================================================================


def test_identical_tool_call_blocking_within_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance criterion 2: duplicate tool calls are blocked deterministically without re-executing."""
    conn = _connection()
    monkeypatch.setattr("app.modules.agents.hermes.supervisor.open_sqlite_connection", lambda: _existing_connection(conn))
    supervisor_inst = HermesSupervisor("unused", runner=MagicMock())
    supervisor_inst.session = SESSION
    grant = _make_grant("jarvis.decide", "grant-decide")
    supervisor_inst.live_grants = {"grant-decide": grant}

    frame1 = {
        "id": "call-1",
        "session_ref": SESSION.model_dump(mode="json"),
        "arguments": {
            "tool_name": "jarvis_decide",
            "grant_id": "grant-decide",
            "kind": "route_class",
            "request": {"task": "fit"},
        },
    }
    frame2 = {
        "id": "call-2",
        "session_ref": SESSION.model_dump(mode="json"),
        "arguments": {
            "tool_name": "jarvis_decide",
            "grant_id": "grant-decide",
            "kind": "route_class",
            "request": {"task": "fit"},
        },
    }

    sent_frames: list[dict[str, Any]] = []
    supervisor_inst._send = lambda f: sent_frames.append(f)

    supervisor_inst._handle_tool(frame1)
    assert len(sent_frames) == 1
    assert sent_frames[0]["tool_result"]["status"] == "succeeded"

    supervisor_inst._handle_tool(frame2)
    assert len(sent_frames) == 2
    res2 = sent_frames[1]["tool_result"]
    assert "Tool call blocked: identical call was already executed" in str(res2["result"])


def test_research_budget_cap_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance criterion 2: cap search/read research calls per turn (default 8) with notice."""
    conn = _connection()
    monkeypatch.setattr("app.modules.agents.hermes.supervisor.open_sqlite_connection", lambda: _existing_connection(conn))
    supervisor_inst = HermesSupervisor("unused", runner=MagicMock())
    supervisor_inst.session = SESSION
    grant = _make_grant("jarvis.decide", "grant-r")
    supervisor_inst.live_grants = {"grant-r": grant}
    supervisor_inst.research_budget = 3

    sent_frames: list[dict[str, Any]] = []
    supervisor_inst._send = lambda f: sent_frames.append(f)

    for i in range(3):
        frame = {
            "id": f"call-{i}",
            "session_ref": SESSION.model_dump(mode="json"),
            "arguments": {
                "tool_name": "jarvis_retrieval_query",
                "grant_id": "grant-r",
                "query": f"search query {i}",
            },
        }
        supervisor_inst._handle_tool(frame)

    assert supervisor_inst.turn_research_count == 3

    # Call 4 exceeds budget of 3
    frame_exceed = {
        "id": "call-exceed",
        "session_ref": SESSION.model_dump(mode="json"),
        "arguments": {
            "tool_name": "jarvis_retrieval_query",
            "grant_id": "grant-r",
            "query": "search query beyond budget",
        },
    }
    supervisor_inst._handle_tool(frame_exceed)
    res_exceed = sent_frames[-1]["tool_result"]
    assert "Research budget reached for this turn" in str(res_exceed["result"])
    assert "synthesize your final answer" in str(res_exceed["result"]).lower()


def test_is_research_tool_classification() -> None:
    """Verify research tools include retrieval, repo_read, knowledge_read, dev_read, paper_search."""
    assert is_research_tool("paper_search")
    assert is_research_tool("jarvis_paper_search")
    assert is_research_tool("mcp__jarvis__paper_search")
    assert is_research_tool("jarvis_retrieval_query")
    assert is_research_tool("jarvis_repo_read")
    assert is_research_tool("jarvis_knowledge_read")
    assert is_research_tool("jarvis_dev_read")
    assert is_research_tool("session_search")
    assert not is_research_tool("jarvis_process_act")
    assert not is_research_tool("jarvis_bluecad_act")


# ===========================================================================
# 3. Compact outputs, shrinking, dead-turn nudge & forced final answer
# ===========================================================================


def test_compact_tool_payload_bounds_oversized_payload() -> None:
    """Acceptance criterion 3: large tool outputs are compacted within bounds."""
    large_str = "x" * 5000
    compacted = compact_tool_payload(large_str, max_chars=1000)
    assert len(compacted) <= 1000
    assert "[output truncated]" in compacted

    large_dict = {
        "papers": [{"id": i, "data": "paper" * 50} for i in range(10)],
        "description": "d" * 600,
    }
    compacted_dict = compact_tool_payload(large_dict, max_chars=500)
    assert len(compacted_dict["papers"]) <= 4
    assert len(compacted_dict["description"]) <= 450


def test_shrink_tool_history_preserves_kv_cache() -> None:
    """Acceptance criterion 3: older tool results are shrunk while recent ones are kept intact."""
    messages = [
        {"role": "user", "content": "Question"},
        {"role": "assistant", "content": None, "tool_calls": [{"name": "tool1"}]},
        {"role": "tool", "content": "Older tool result " + ("A" * 500)},
        {"role": "assistant", "content": None, "tool_calls": [{"name": "tool2"}]},
        {"role": "tool", "content": "Recent tool result " + ("B" * 500)},
    ]
    shrunk = shrink_tool_history(messages, keep_recent=1, max_shrunk_len=100)

    # Message 2 (first tool result) should be shrunk
    assert len(shrunk[2]["content"]) <= 100
    assert "[prior tool output shrunk]" in shrunk[2]["content"]

    # Message 4 (most recent tool result) should remain full
    assert "Recent tool result " in shrunk[4]["content"]
    assert len(shrunk[4]["content"]) > 500


def test_dead_turn_detection_and_nudge() -> None:
    """Acceptance criterion 3: empty dead turns trigger a bounded nudge."""
    assert is_dead_turn("")
    assert is_dead_turn("   \n\t  ")
    assert is_dead_turn(json.dumps({"answer": "  "}))
    assert not is_dead_turn(json.dumps({"answer": "Here is the final answer."}))
    assert not is_dead_turn(json.dumps({"tool_calls": [{"name": "paper_search", "arguments": {}}]}))

    calls = 0

    def fake_runner(**kwargs: Any) -> AiTaskOutcome:
        nonlocal calls
        calls += 1
        prompt = kwargs.get("user_prompt", "")
        if DEAD_TURN_NUDGE in prompt:
            # Model recovered after nudge
            return AiTaskOutcome(
                flow_id="flow-1", status="success", ledger_id="l-1",
                selected_route_class="fake", decision=MagicMock(),
                response=MagicMock(text=json.dumps({"answer": "Recovered answer."}), finish_reason="stop")
            )
        # Empty dead turn on first attempt
        return AiTaskOutcome(
            flow_id="flow-1", status="success", ledger_id="l-1",
            selected_route_class="fake", decision=MagicMock(),
            response=MagicMock(text="", finish_reason="stop")
        )

    envelope = InferenceEnvelope(
        envelope_id=str(uuid4()), correlation_id="c-1", task_kind="general",
        prompt="Initial prompt", requested_at=NOW, deadline_at=NOW + timedelta(seconds=60),
        agent_session=SESSION,
    )

    result = run_governed_inference(envelope, runner=fake_runner, max_nudges=1)
    assert calls == 2
    assert result["status"] == "success"
    assert "Recovered answer." in result["text"]


def test_forced_final_answer_at_turn_cap() -> None:
    """Acceptance criterion 3: reaching turn cap withholds tools and forces reasoning_phase='final'."""
    supervisor_inst = HermesSupervisor("unused", runner=MagicMock())
    supervisor_inst.session = SESSION
    supervisor_inst.active_role_route = "local:fake"
    supervisor_inst.turn_cap = 2
    supervisor_inst.turn_step_count = 1  # Next step will hit turn_cap

    def mock_runner(**kwargs: Any) -> AiTaskOutcome:
        return AiTaskOutcome(
            flow_id="flow-cap", status="success", ledger_id="l-cap",
            selected_route_class="fake", decision=MagicMock(),
            response=MagicMock(text=json.dumps({"answer": "Final conclusion."}), finish_reason="stop")
        )
    supervisor_inst.runner = mock_runner

    sent_frames: list[dict[str, Any]] = []
    supervisor_inst._send = lambda f: sent_frames.append(f)

    frame = {
        "id": "relay-cap",
        "session_ref": SESSION.model_dump(mode="json"),
        "task_kind": "general",
        "messages": [{"role": "user", "content": "Synthesize"}],
        "tools": [{"type": "function", "function": {"name": "paper_search", "parameters": {}}}],
    }
    supervisor_inst._handle_relay(frame)

    assert supervisor_inst.turn_step_count == 2
    assert frame["metadata"].get("reasoning_phase") == "final"
    assert frame["tools"] == []


# ===========================================================================
# 4. Literature metadata tool (paper_search) tests
# ===========================================================================


_FAKE_EPMC_JSON = json.dumps({
    "resultList": {
        "result": [
            {
                "title": "Kinetic modeling of cyanobacterial photobioreactors",
                "authorString": "Müller K, Zhang L",
                "pubYear": "2023",
                "journalTitle": "Biotechnology and Bioengineering",
                "doi": "10.1002/bit.28001",
                "abstractText": "This paper presents Monod kinetics and light attenuation models...",
            }
        ]
    }
}).encode("utf-8")

_FAKE_CROSSREF_JSON = json.dumps({
    "status": "ok",
    "message": {
        "items": [
            {
                "title": ["Hydrodynamic analysis of annular photobioreactors"],
                "author": [{"family": "Smith", "given": "John"}, {"family": "Doe", "given": "Alice"}],
                "issued": {"date-parts": [[2024, 2]]},
                "container-title": ["Chemical Engineering Science"],
                "DOI": "10.1016/j.ces.2024.119001",
                "abstract": "<jats:p>Annular geometry provides uniform irradiance distributions.</jats:p>",
            }
        ]
    }
}).encode("utf-8")


def test_europe_pmc_parsing() -> None:
    """Acceptance criterion 4: parses Europe PMC JSON into compact structured records."""
    fake_opener = _FakeOpener({EUROPE_PMC_SEARCH_URL: _FakeHTTPResponse(_FAKE_EPMC_JSON)})
    records = search_europe_pmc("photobioreactor", limit=5, opener=fake_opener)

    assert len(records) == 1
    rec = records[0]
    assert rec["title"] == "Kinetic modeling of cyanobacterial photobioreactors"
    assert rec["authors"] == "Müller K, Zhang L"
    assert rec["year"] == 2023
    assert rec["journal"] == "Biotechnology and Bioengineering"
    assert rec["doi"] == "10.1002/bit.28001"
    assert "Monod kinetics" in rec["abstract"]
    assert rec["source"] == "europe_pmc"


def test_crossref_parsing() -> None:
    """Acceptance criterion 4: parses Crossref JSON into compact structured records with cleaned XML tags."""
    fake_opener = _FakeOpener({CROSSREF_SEARCH_URL: _FakeHTTPResponse(_FAKE_CROSSREF_JSON)})
    records = search_crossref("annular photobioreactor", limit=5, opener=fake_opener)

    assert len(records) == 1
    rec = records[0]
    assert rec["title"] == "Hydrodynamic analysis of annular photobioreactors"
    assert "Smith John, Doe Alice" in rec["authors"] or "Smith, Doe" in rec["authors"]
    assert rec["year"] == 2024
    assert rec["journal"] == "Chemical Engineering Science"
    assert rec["doi"] == "10.1016/j.ces.2024.119001"
    assert "<jats:p>" not in rec["abstract"]
    assert "Annular geometry provides" in rec["abstract"]
    assert rec["source"] == "crossref"


def test_execute_paper_search_combined_and_bounded() -> None:
    """Acceptance criterion 4: combined search returns bounded results from both sources."""
    fake_opener = _FakeOpener({
        EUROPE_PMC_SEARCH_URL: _FakeHTTPResponse(_FAKE_EPMC_JSON),
        CROSSREF_SEARCH_URL: _FakeHTTPResponse(_FAKE_CROSSREF_JSON),
    })
    result = execute_paper_search("photobioreactor kinetics", limit=4, source="all", opener=fake_opener)

    assert result["count"] == 2
    sources = {p["source"] for p in result["papers"]}
    assert sources == {"europe_pmc", "crossref"}


def test_paper_search_denies_http_redirects() -> None:
    """Acceptance criterion 4: fails closed on HTTP redirects."""
    handler = _DenyRedirects()
    req = MagicMock()
    assert handler.redirect_request(req, MagicMock(), 302, "msg", {}, "http://example.com") is None


def test_paper_search_refuses_oversized_response() -> None:
    """Acceptance criterion 4: fails closed on oversized responses (>512 KB)."""
    oversized = b"x" * (MAX_RESPONSE_BYTES + 500)
    fake_opener = _FakeOpener({EUROPE_PMC_SEARCH_URL: _FakeHTTPResponse(oversized)})
    records = search_europe_pmc("query", opener=fake_opener)
    assert records == []


def test_paper_search_handles_upstream_http_errors_cleanly() -> None:
    """Acceptance criterion 4: handles network errors cleanly without exceptions."""
    http_err = HTTPError(EUROPE_PMC_SEARCH_URL, 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]
    fake_opener = _FakeOpener({EUROPE_PMC_SEARCH_URL: http_err})
    records = search_europe_pmc("query", opener=fake_opener)
    assert records == []


def test_paper_search_egress_sanitization_blocks_secrets_and_private_pack() -> None:
    """Acceptance criterion 4: never sends private pack content or secrets in queries."""
    secret_query = "photobioreactor with key sk-1234567890abcdef1234567890"
    res = execute_paper_search(secret_query)
    assert res.get("error") == "egress_blocked_private_content"
    assert res["count"] == 0

    call = _make_call({"grant_id": "grant-352", "query": secret_query})
    result, error = dispatch_paper_search_tool(call, "workspace-352", "default_error")
    assert result is None
    assert error == "egress_pack_blocked"


def test_paper_search_registration_in_all_catalogs_and_allowlists() -> None:
    """Acceptance criterion 1 & 4: registered in broker_mcp, supervisor, worker_shim, thread_service."""
    # 1. broker_mcp._TOOLS contains jarvis_paper_search
    tool_entry = next((t for t in broker_mcp._TOOLS if t["name"] == "jarvis_paper_search"), None)
    assert tool_entry is not None
    assert tool_entry["inputSchema"]["required"] == ["grant_id", "query"]

    # 2. supervisor._TOOL_CAPABILITIES maps paper_search -> jarvis.paper_search
    assert _TOOL_CAPABILITIES.get("paper_search") == "jarvis.paper_search"
    assert _TOOL_CAPABILITIES.get("jarvis_paper_search") == "jarvis.paper_search"

    # 3. supervisor.HERMES_TOOL_ALLOWLIST
    assert "mcp__jarvis__paper_search" in HERMES_TOOL_ALLOWLIST
    assert "paper_search" in HERMES_TOOL_ALLOWLIST

    # 4. PRODUCTION_CAPABILITY_REGISTRY
    cap = next((c for c in PRODUCTION_CAPABILITY_REGISTRY.for_route("ai-threads") if c.capability_id == "jarvis.paper_search"), None)
    assert cap is not None
    assert cap.action_class == "READ"

    # 5. worker_shim.ADMITTED_TOOLS
    assert "mcp__jarvis__paper_search" in worker_shim.ADMITTED_TOOLS
    assert "paper_search" in worker_shim.ADMITTED_TOOLS

    # 6. thread_service._turn_tools
    assert "mcp__jarvis__jarvis_paper_search" in thread_service._turn_tools("none")
    assert "mcp__jarvis__paper_search" not in thread_service._turn_tools("none")


def test_compact_tool_payload_strict_recursive_bound() -> None:
    """Requirement 4: deeply nested large dict payload is strictly bounded to <= max_chars serialized bytes."""
    # 1. Moderately nested structure that compacts via list truncation and string shortening
    nested = {
        "items": [
            {"nested_key": f"value_{i}" * 50, "extra": "a" * 500}
            for i in range(10)
        ]
    }
    compacted = compact_tool_payload(nested, max_chars=1000)
    serialized = json.dumps(compacted, separators=(",", ":"))
    assert len(serialized) <= 1000
    assert len(serialized.encode("utf-8")) <= 1000

    # 2. Deeply nested structure with massive payloads that exceeds max_chars even after recursive shortening
    deep: dict[str, Any] = {"level0": {}}
    cursor = deep["level0"]
    for lvl in range(1, 15):
        cursor[f"level{lvl}"] = {
            "title": "large_title_" * 20,
            "data": ["long_entry_" * 30 for _ in range(10)],
            "metadata": {"blob": "z" * 1000},
        }
        cursor = cursor[f"level{lvl}"]

    max_chars = 400
    result = compact_tool_payload(deep, max_chars=max_chars)
    result_bytes = json.dumps(result, separators=(",", ":")).encode("utf-8")
    result_str = json.dumps(result, separators=(",", ":"))

    assert len(result_bytes) <= max_chars
    assert len(result_str) <= max_chars
    assert isinstance(result, dict)
    assert result.get("truncated") is True
    assert "preview" in result


def test_infer_envelope_metadata_whitelisting() -> None:
    """Requirement 2: infer_envelope only whitelists reasoning_phase in {'analysis', 'final', 'routing'}."""
    frame = {
        "id": "frame-meta",
        "session_ref": SESSION.model_dump(mode="json"),
        "messages": [{"role": "user", "content": "Analyze options"}],
        "metadata": {
            "arbitrary_client_key": "untrusted_value",
            "context_digest": "spoofed_digest",
            "reasoning_phase": "analysis",
        },
    }
    envelope = infer_envelope(frame)
    assert envelope.metadata == {"reasoning_phase": "analysis"}

    # Whitelist accepts "final" and "routing"
    for phase in ("final", "routing"):
        frame["metadata"] = {"reasoning_phase": phase}
        assert infer_envelope(frame).metadata == {"reasoning_phase": phase}

    # Rejects unauthorized reasoning_phase values and arbitrary keys
    frame["metadata"] = {"reasoning_phase": "unauthorized_phase", "other_key": 123}
    assert infer_envelope(frame).metadata == {}

