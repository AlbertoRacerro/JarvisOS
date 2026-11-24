"""Tests for Spec 363 — HERMES-NUMBER-PROVENANCE-1.

Offline backend unit tests covering:
1. Deterministic answer checking & number extraction from tool results (bench, literature, patent, turn plan).
2. Trivial structural tokens skipped (list indices, markdown headings, ISO dates).
3. Rounding tolerance (up to 1% relative matching when evidence rounds to displayed precision).
4. AIThreadInteractionRead projection of number_provenance from hermes.number_provenance event.
5. Bounds enforcement and security (max 40 entries, raw <= 48 chars, source_ref <= 120 chars, no secrets or filesystem paths).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from app.modules.agents.hermes.provenance import (
    MAX_ENTRIES,
    MAX_RAW_LEN,
    MAX_SOURCE_REF_LEN,
    NumericEvidence,
    compute_number_provenance,
    derive_source_ref_and_route,
    extract_assistant_numbers,
    extract_tool_numeric_evidence,
    match_number,
)
from app.modules.agents.hermes.supervisor import (
    UPSTREAM_REVISION,
    HermesSupervisor,
)
from app.modules.ai import thread_service
from app.modules.ai.agent_contracts import (
    AgentSessionRef,
    StructuredToolResult,
)

NOW = datetime.now(UTC)
SESSION = AgentSessionRef(
    jarvis_thread_id="thread-363",
    hermes_session_id="hermes-363",
    profile_id="isaac",
    workspace_id="workspace-363",
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
    connection.execute(
        "CREATE TABLE ai_threads (id TEXT PRIMARY KEY, workspace_id TEXT, title TEXT, created_at TEXT, last_activity_at TEXT)"
    )
    connection.execute(
        "INSERT INTO ai_threads VALUES ('thread-363', 'workspace-363', 'Thread 363', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
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


# ===========================================================================
# 1. Evidence extraction from tool results
# ===========================================================================


def test_extract_evidence_bench_tools() -> None:
    """Verify bench run/read results produce bench:<run_id> refs and /runs route."""
    bench_result = {
        "op": "show",
        "run_id": "run-cfd-101",
        "view": {
            "pressure_drop_kpa": 12.35,
            "max_velocity_ms": 4.5,
            "status": "completed",
        },
    }
    evidence = extract_tool_numeric_evidence(
        tool_name="jarvis_bench_read",
        call_id="call-bench-1",
        arguments={"op": "show", "run_id": "run-cfd-101"},
        result=bench_result,
    )
    assert len(evidence) >= 2
    values = {e.value for e in evidence}
    assert 12.35 in values
    assert 4.5 in values
    for e in evidence:
        assert e.source_ref == "bench:run-cfd-101"
        assert e.route_hint == "/runs"
        assert e.tool_name == "jarvis_bench_read"


def test_extract_evidence_literature_tools() -> None:
    """Verify paper search and pdf read produce doi:<doi> refs and /memory/literature route."""
    paper_result = {
        "papers": [
            {
                "doi": "10.1016/j.ces.2024.119852",
                "title": "Absorption of CO2 in packed columns",
                "hetp_m": 0.45,
                "year": 2024,
            }
        ]
    }
    evidence = extract_tool_numeric_evidence(
        tool_name="jarvis_paper_search",
        call_id="call-paper-1",
        arguments={"query": "CO2 absorption"},
        result=paper_result,
    )
    assert any(e.value == 0.45 and e.source_ref == "doi:10.1016/j.ces.2024.119852" for e in evidence)
    assert all(e.route_hint == "/memory/literature" for e in evidence)


def test_metadata_numbers_do_not_verify_claims_and_long_passages_are_read() -> None:
    result = {
        "run_id": "run-789",
        "doi": "10.1016/example.2024",
        "year": 2024,
        "source_id": "source-456",
        "view": {"passage": "Context: " + "x" * 250 + " measured 12.35 kPa.", "pressure_kpa": 3.5},
    }
    evidence = extract_tool_numeric_evidence(
        tool_name="jarvis_bench_read", call_id="call-1", result=result,
    )
    assert {item.value for item in evidence} == {12.35, 3.5}
    assert match_number("789", 789, evidence)[0] == "unverified"
    assert match_number("2024", 2024, evidence)[0] == "unverified"
    assert match_number("12.35", 12.35, evidence)[0] == "verified"


def test_unsafe_call_id_never_becomes_source_ref() -> None:
    ref, _ = derive_source_ref_and_route("jarvis_browser_page_read", "/home/example/private", {})
    assert ref == "tool:unknown"
    ref, _ = derive_source_ref_and_route(
        "jarvis_knowledge_read", "call-1", {"view": "private_key=42"},
    )
    assert ref == "knowledge:call-1"


def test_repeated_numeric_claims_are_counted_per_occurrence() -> None:
    provenance = compute_number_provenance(
        "The first reading is 12.35 kPa. The second reading is 12.35 kPa.",
        [], interaction_id="turn-1", workspace_id="workspace-1",
    )
    assert provenance["unverified_count"] == 2
    assert len(provenance["entries"]) == 2


def test_extract_evidence_turn_plan_findings() -> None:
    """Verify turn plan findings extract values and map custom source refs."""
    plan_result = {
        "steps": [{"id": "s1", "title": "Compute packing height", "status": "done"}],
        "findings": [
            {"key": "column_diameter_m", "value": 1.45, "unit": "m", "source_ref": "bench:run-size-1"},
            {"key": "packing_height_m", "value": 8.2, "unit": "m", "source_ref": "doi:10.1016/sample"},
            {"key": "unrouted_metric", "value": 100, "source_ref": "calc:hexgas"},
        ],
    }
    evidence = extract_tool_numeric_evidence(
        tool_name="jarvis_turn_plan",
        call_id="call-plan-1",
        arguments={"op": "set_plan"},
        result=plan_result,
    )
    val_map = {e.value: e for e in evidence}
    assert 1.45 in val_map
    assert val_map[1.45].source_ref == "bench:run-size-1"
    assert val_map[1.45].route_hint == "/runs"

    assert 8.2 in val_map
    assert val_map[8.2].source_ref == "doi:10.1016/sample"
    assert val_map[8.2].route_hint == "/memory/literature"

    assert 100.0 in val_map
    assert val_map[100.0].source_ref == "calc:hexgas"
    assert val_map[100.0].route_hint is None


def test_extract_evidence_patent_tools() -> None:
    """Verify patent read produces patent:<pub_no> refs."""
    patent_result = {
        "pub_no": "US10123456B2",
        "title": "Carbon capture system",
        "claims_count": 18,
        "operating_temperature_c": 125.0,
    }
    evidence = extract_tool_numeric_evidence(
        tool_name="jarvis_patent_read",
        call_id="call-patent-1",
        arguments={"pub_no": "US10123456B2"},
        result=patent_result,
    )
    assert any(e.value == 125.0 for e in evidence)
    assert all(e.source_ref == "patent:US10123456B2" for e in evidence)
    assert all(e.route_hint == "/memory/literature" for e in evidence)


# ===========================================================================
# 2. Assistant text extraction & skipping trivial structural tokens
# ===========================================================================


def test_extract_assistant_numbers_skips_structural_tokens() -> None:
    """Requirement 1: skip markdown headings, list indices (1., 2.), and ISO dates."""
    text = """
# 1. Executive Summary
The experiment was performed on 2026-11-24 with ISO timestamp 2026-11-24T14:30:00Z.
1. The column diameter was sized to 1.45 m.
2. The pressure drop was measured at 12.35 kPa.
3. Operating efficiency reached 94.2%.
According to (Smith, 2024), nominal duty is 3.5 MW.
"""
    numbers = extract_assistant_numbers(text)
    raw_tokens = [item[0] for item in numbers]
    values = [item[1] for item in numbers]

    # Structural tokens that MUST be skipped:
    # 1. Markdown heading number '1'
    # 2. ISO dates '2026', '11', '24'
    # 3. List indices '1.', '2.', '3.'
    # 4. Citation '2024'
    assert "2026-11-24" not in raw_tokens
    assert 2026.0 not in values
    assert 2024.0 not in values

    # Legitimate numbers that MUST be extracted:
    assert 1.45 in values
    assert 12.35 in values
    assert 94.2 in values
    assert 3.5 in values


# ===========================================================================
# 3. Deterministic matching and rounding tolerance
# ===========================================================================


def test_match_number_exact_and_rounding() -> None:
    """Requirement 1: Exact matches preferred, rounded tolerance up to 1% accepted."""
    evidence = [
        NumericEvidence(value=3.14159265, source_ref="calc:pi", tool_name="bench", tool_call_id="c1", route_hint="/runs"),
        NumericEvidence(value=100.0, source_ref="bench:run-1", tool_name="bench", tool_call_id="c2", route_hint="/runs"),
        NumericEvidence(value=0.00543, source_ref="literature:s1", tool_name="lit", tool_call_id="c3"),
    ]

    # Exact match
    status, ev = match_number("100", 100.0, evidence)
    assert status == "verified"
    assert ev is not None and ev.source_ref == "bench:run-1"

    # Display-rounded match: 3.14 rounds 3.14159265 to 2 decimals, within 0.05% relative diff
    status, ev = match_number("3.14", 3.14, evidence)
    assert status == "verified"
    assert ev is not None and ev.source_ref == "calc:pi"

    # Beyond 1% relative tolerance or non-matching precision should be unverified
    # 3.1 rounds 3.14159265 to 3.1, relative diff is abs(3.1 - 3.14159) / 3.14159 = 1.32% > 1%
    status, ev = match_number("3.1", 3.1, evidence)
    assert status == "unverified"
    assert ev is None

    # Completely absent number
    status, ev = match_number("999.0", 999.0, evidence)
    assert status == "unverified"
    assert ev is None


# ===========================================================================
# 4. Full provenance computation and bounds enforcement
# ===========================================================================


def test_compute_number_provenance_payload() -> None:
    """Verify compute_number_provenance returns verified and unverified counts and bounded entries."""
    evidence = [
        NumericEvidence(value=1.45, source_ref="bench:run-col", tool_name="jarvis_bench_read", tool_call_id="c1", route_hint="/runs"),
        NumericEvidence(value=12.35, source_ref="doi:10.1016/sample", tool_name="jarvis_paper_search", tool_call_id="c2", route_hint="/memory/literature"),
    ]
    assistant_text = (
        "We calculated diameter 1.45 m and pressure drop 12.35 kPa.\n"
        "However, temperature 350 K is an unverified assumption."
    )
    prov = compute_number_provenance(assistant_text, evidence, interaction_id="inter-1", workspace_id="ws-1")

    assert prov["interaction_id"] == "inter-1"
    assert prov["workspace_id"] == "ws-1"
    assert prov["verified_count"] == 2
    assert prov["unverified_count"] == 1
    assert len(prov["entries"]) == 3

    entries_by_val = {e["value"]: e for e in prov["entries"]}
    assert entries_by_val[1.45]["status"] == "verified"
    assert entries_by_val[1.45]["source_ref"] == "bench:run-col"
    assert entries_by_val[1.45]["route_hint"] == "/runs"

    assert entries_by_val[12.35]["status"] == "verified"
    assert entries_by_val[12.35]["source_ref"] == "doi:10.1016/sample"
    assert entries_by_val[12.35]["route_hint"] == "/memory/literature"

    assert entries_by_val[350.0]["status"] == "unverified"
    assert "source_ref" not in entries_by_val[350.0]


def test_compute_number_provenance_bounds_and_safety() -> None:
    """Verify provenance entries respect bounds (raw <= 48, source_ref <= 120, max 40 entries, no secrets/paths)."""
    # Create 50 numbers in text
    many_numbers = "\n".join(f"Item value is {1000 + i}." for i in range(50))
    prov = compute_number_provenance(many_numbers, [], interaction_id="inter-bounds", workspace_id="ws-1")

    assert len(prov["entries"]) <= MAX_ENTRIES
    for entry in prov["entries"]:
        assert len(entry["raw"]) <= MAX_RAW_LEN
        if "source_ref" in entry:
            assert len(entry["source_ref"]) <= MAX_SOURCE_REF_LEN
            assert not entry["source_ref"].startswith(("/home/", "/tmp/"))

    # Test that forbidden path/secret keys are rejected in source ref derivation
    ref, route = derive_source_ref_and_route("jarvis_bench_read", "c1", {"run_id": "/home/user/private/run"})
    assert not ref.startswith("/home/")
    assert ref.startswith("bench:")


# ===========================================================================
# 5. SQLite Event Persistence and AIThreadInteractionRead Projection
# ===========================================================================


def test_interaction_number_provenance_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test AIThreadInteractionRead projects number_provenance from hermes.number_provenance event."""
    conn = _connection()

    payload = {
        "interaction_id": "inter-test-1",
        "workspace_id": "workspace-363",
        "verified_count": 1,
        "unverified_count": 1,
        "entries": [
            {"raw": "42.5", "value": 42.5, "status": "verified", "source_ref": "bench:run-12", "tool_name": "bench", "route_hint": "/runs"},
            {"raw": "99.0", "value": 99.0, "status": "unverified"},
        ],
    }
    conn.execute(
        """
        INSERT INTO events (id, workspace_id, event_type, actor, target_type, target_id, payload, created_at)
        VALUES ('evt-1', 'workspace-363', 'hermes.number_provenance', 'jarvis', 'ai_thread_interaction', 'inter-test-1', ?, '2026-01-01T00:00:00+00:00')
        """,
        (json.dumps(payload),),
    )

    prov = thread_service.interaction_number_provenance(conn, "inter-test-1")
    assert prov is not None
    assert prov.verified_count == 1
    assert prov.unverified_count == 1
    assert len(prov.entries) == 2
    assert prov.entries[0].raw == "42.5"
    assert prov.entries[0].status == "verified"
    assert prov.entries[0].source_ref == "bench:run-12"
    assert prov.entries[0].route_hint == "/runs"
    assert prov.entries[1].raw == "99.0"
    assert prov.entries[1].status == "unverified"

    # Non-existent interaction returns None (backward compatibility)
    assert thread_service.interaction_number_provenance(conn, "inter-nonexistent") is None


def test_supervisor_tool_evidence_collection_and_cleanup() -> None:
    """Test that HermesSupervisor collects numeric evidence during a turn and cleans up on turn end."""
    supervisor = HermesSupervisor("/mock")
    supervisor.session = SESSION

    assert supervisor.turn_evidence_ledger == []

    # Simulate tool handling of bench tool
    frame = {
        "id": "tool-call-1",
        "session_ref": SESSION.model_dump(mode="json"),
        "arguments": {
            "tool_name": "jarvis_bench_run",
            "evaluator_id": "bluecad.solid",
            "grant_id": "grant-1",
            "run_id": "run-geo-01",
        },
    }

    mock_result = StructuredToolResult(
        call_id="tool-call-1",
        capability_id="jarvis.bench_run",
        status="succeeded",
        result={"run_id": "run-geo-01", "view": {"volume_m3": 0.045, "surface_area_m2": 1.2}},
        completed_at=NOW,
    )

    # Monkeypatch dispatch_tool to return succeeded tool result
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.modules.agents.hermes.supervisor.dispatch_tool", lambda *args, **kwargs: mock_result)
        mp.setattr("app.modules.agents.hermes.supervisor.open_sqlite_connection", lambda: _existing_connection(_connection()))
        mp.setattr(supervisor, "_send", lambda frame: None)

        supervisor.active_interaction_id = "inter-turn-1"
        supervisor._handle_tool(frame)

        assert len(supervisor.turn_evidence_ledger) >= 2
        values = {e.value for e in supervisor.turn_evidence_ledger}
        assert 0.045 in values
        assert 1.2 in values
        assert all(e.source_ref == "bench:run-geo-01" for e in supervisor.turn_evidence_ledger)
