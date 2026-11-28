"""Offline tests for Spec 391 — DATED-RESEARCH-REUSE-1.

Covers:
- Pure helper _build_dated_research_summary
- jarvis_knowledge_read(view="literature_sources") with timestamps, validated flag,
  state filter (raw/review/accepted and rejection of invalid states), and dated_research_summary
- jarvis_knowledge_read(view="literature_entries") with joined source fields and dated_research_summary
- broker_mcp schema declarations and tool description
- supervisor ENGINEERING_METHODOLOGY_RULES and _assemble_turn_prompt
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.agents.hermes import broker_mcp
from app.modules.agents.hermes.knowledge_read_tool import (
    _build_dated_research_summary,
    dispatch_knowledge_read_tool,
)
from app.modules.agents.hermes.supervisor import (
    ENGINEERING_METHODOLOGY_RULES,
    _assemble_turn_prompt,
    dispatch_tool,
)
from app.modules.ai.agent_contracts import (
    AgentSessionRef,
    CapabilityGrantRef,
    CapabilityScope,
    StructuredToolCall,
)


def _make_grant(
    grant_id: str = "grant-knowledge-read",
    capability_id: str = "jarvis.knowledge_read",
    workspace_id: str = "workspace-test",
    thread_id: str = "thread-test",
) -> CapabilityGrantRef:
    now = datetime.now(UTC)
    return CapabilityGrantRef(
        grant_id=grant_id,
        capability_id=capability_id,
        issuer="jarvis_policy",
        scope=CapabilityScope(workspace_id=workspace_id, jarvis_thread_id=thread_id),
        issued_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(minutes=10),
    )


def _make_call(
    arguments: dict[str, Any],
    capability_id: str = "jarvis.knowledge_read",
    grant_id: str = "grant-knowledge-read",
    workspace_id: str = "workspace-test",
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


@pytest.fixture
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Set up an isolated SQLite database initialized with all schemas and test literature data."""
    data_root = tmp_path / "jarvis_data"
    data_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(data_root))
    initialize_database()

    with open_sqlite_connection() as conn:
        now = "2026-11-20T10:00:00Z"

        # Workspace
        conn.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?)",
            ("workspace-test", "Test Workspace", "ws-test", "Workspace for dated research reuse", now, now),
        )

        # Sources with mixed states
        conn.execute(
            "INSERT INTO literature_sources (id, workspace_id, title, source_kind, state, published_year, citation, publisher, created_at, updated_at) "
            "VALUES (?, ?, ?, 'paper', 'accepted', 2023, 'Alpha Author, 2023', 'Journal of Kinetics', ?, ?)",
            ("src-accepted-1", "workspace-test", "Kinetic Models of Algae", "2023-01-10T08:00:00Z", "2024-03-15T12:00:00Z"),
        )
        conn.execute(
            "INSERT INTO literature_sources (id, workspace_id, title, source_kind, state, published_year, citation, publisher, created_at, updated_at) "
            "VALUES (?, ?, ?, 'report', 'review', 2025, 'Beta Author, 2025', 'Tech Report Series', ?, ?)",
            ("src-review", "workspace-test", "Bioreactor Scale-up Report", "2025-01-05T09:00:00Z", "2025-06-01T15:00:00Z"),
        )
        conn.execute(
            "INSERT INTO literature_sources (id, workspace_id, title, source_kind, state, published_year, citation, publisher, created_at, updated_at) "
            "VALUES (?, ?, ?, 'paper', 'raw', 2022, 'Gamma Author, 2022', 'Science Press', ?, ?)",
            ("src-raw", "workspace-test", "Unreviewed Raw Notes", "2022-02-01T10:00:00Z", "2022-03-01T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO literature_sources (id, workspace_id, title, source_kind, state, published_year, citation, publisher, created_at, updated_at) "
            "VALUES (?, ?, ?, 'paper', 'accepted', 2021, 'Delta Author, 2021', 'Academic Press', ?, ?)",
            ("src-accepted-2", "workspace-test", "Earlier Baseline Kinetics", "2021-05-01T12:00:00Z", "2024-01-10T10:00:00Z"),
        )

        # Entries linked to sources
        conn.execute(
            "INSERT INTO literature_entries (id, workspace_id, source_id, entry_kind, statement, value_number, unit, status, locator_kind, locator_start, locator_end, created_at, updated_at) "
            "VALUES (?, ?, ?, 'claim', 'Specific growth rate peaks at 30 deg C', NULL, NULL, 'accepted', 'page', 12, 14, ?, ?)",
            ("entry-accepted-from-accepted", "workspace-test", "src-accepted-1", "2024-03-15T12:00:00Z", "2024-05-10T14:00:00Z"),
        )
        conn.execute(
            "INSERT INTO literature_entries (id, workspace_id, source_id, entry_kind, statement, value_number, unit, status, locator_kind, locator_start, locator_end, created_at, updated_at) "
            "VALUES (?, ?, ?, 'datum', NULL, 0.085, '1/h', 'raw', 'line', 45, 46, ?, ?)",
            ("entry-raw-from-accepted", "workspace-test", "src-accepted-1", "2024-03-15T12:00:00Z", "2024-03-15T12:00:00Z"),
        )
        conn.execute(
            "INSERT INTO literature_entries (id, workspace_id, source_id, entry_kind, statement, value_number, unit, status, locator_kind, locator_start, locator_end, created_at, updated_at) "
            "VALUES (?, ?, ?, 'claim', 'Draft finding on aeration', NULL, NULL, 'review', 'section', 1, 2, ?, ?)",
            ("entry-review-from-review", "workspace-test", "src-review", "2025-01-05T09:00:00Z", "2025-06-01T15:00:00Z"),
        )
        conn.execute(
            "INSERT INTO literature_entries (id, workspace_id, source_id, entry_kind, statement, value_number, unit, status, locator_kind, locator_start, locator_end, created_at, updated_at) "
            "VALUES (?, ?, ?, 'datum', NULL, 120.0, 'mg/L', 'accepted', 'line', 10, 12, ?, ?)",
            ("entry-accepted-from-raw", "workspace-test", "src-raw", "2022-02-01T10:00:00Z", "2025-03-01T08:00:00Z"),
        )

        # Workspace with only raw/review sources
        conn.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?)",
            ("workspace-unvalidated", "Unvalidated Workspace", "ws-unval", "Workspace with no accepted literature", now, now),
        )
        conn.execute(
            "INSERT INTO literature_sources (id, workspace_id, title, source_kind, state, published_year, citation, publisher, created_at, updated_at) "
            "VALUES (?, ?, ?, 'paper', 'raw', 2024, 'Raw Author, 2024', 'Draft Archive', ?, ?)",
            ("src-only-raw", "workspace-unvalidated", "Completely Raw Notes", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO literature_entries (id, workspace_id, source_id, entry_kind, statement, value_number, unit, status, locator_kind, locator_start, locator_end, created_at, updated_at) "
            "VALUES (?, ?, ?, 'claim', 'Unverified observation', NULL, NULL, 'raw', 'page', 1, 1, ?, ?)",
            ("entry-only-raw", "workspace-unvalidated", "src-only-raw", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z"),
        )

        conn.commit()


def test_build_dated_research_summary_pure_helper() -> None:
    """Verify deterministic pure helper behavior across combinations of counts, dates, and years."""
    # 1. Zero validated count
    zero_summary = _build_dated_research_summary([], [], 0)
    assert zero_summary["validated_count"] == 0
    assert zero_summary["latest_validated_date"] is None
    assert zero_summary["latest_published_year"] is None
    assert "No validated stored literature sources matched" in zero_summary["reuse_guidance"]
    assert "fresh search or a labelled engineering estimate is permitted" in zero_summary["reuse_guidance"]

    # Zero count with non-empty lists still gives zero count and fallback guidance
    zero_with_lists = _build_dated_research_summary(["2024-05-10T12:00:00Z"], [2024], 0)
    assert zero_with_lists["validated_count"] == 0
    assert zero_with_lists["latest_validated_date"] == "2024-05-10"
    assert zero_with_lists["latest_published_year"] == 2024
    assert "No validated stored literature sources matched" in zero_with_lists["reuse_guidance"]

    # 2. Validated count > 0 with valid ISO timestamps and years
    dates = ["2023-01-15T10:00:00Z", "2024-05-20T12:00:00+00:00", "2022-11-01", None, "invalid-date"]
    years = [2021, 2024, None]
    summary = _build_dated_research_summary(dates, years, 2)
    assert summary["validated_count"] == 2
    assert summary["latest_validated_date"] == "2024-05-20"
    assert summary["latest_published_year"] == 2024
    guidance = summary["reuse_guidance"]
    assert "2024-05-20" in guidance
    assert "Reuse validated stored literature/documents first" in guidance
    assert "offer at the end to search for anything newer than 2024-05-20" in guidance
    assert "search afresh only when stored coverage is incomplete or the user asks" in guidance

    # 3. Validated count > 0 with no valid dates, only years
    year_only_summary = _build_dated_research_summary([None, ""], [2022, 2023], 1)
    assert year_only_summary["validated_count"] == 1
    assert year_only_summary["latest_validated_date"] is None
    assert year_only_summary["latest_published_year"] == 2023
    assert "2023" in year_only_summary["reuse_guidance"]
    assert "offer at the end to search for anything newer than 2023" in year_only_summary["reuse_guidance"]

    # 4. Validated count > 0 with neither dates nor years
    no_dates_summary = _build_dated_research_summary([], [], 3)
    assert no_dates_summary["validated_count"] == 3
    assert no_dates_summary["latest_validated_date"] is None
    assert no_dates_summary["latest_published_year"] is None
    assert "Reuse validated stored literature/documents first" in no_dates_summary["reuse_guidance"]


def test_broker_mcp_knowledge_read_tool_schema_and_description() -> None:
    """Verify broker_mcp exposes optional state enum on literature_sources and notes dated_research_summary."""
    tool = broker_mcp._KNOWLEDGE_READ_TOOL
    schema = tool["inputSchema"]
    assert isinstance(schema, dict)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert "state" in properties
    state_prop = properties["state"]
    assert isinstance(state_prop, dict)
    assert state_prop["type"] == "string"
    assert state_prop["enum"] == ["raw", "review", "accepted"]

    desc = tool["description"]
    assert isinstance(desc, str)
    assert "dated_research_summary" in desc
    assert "Prefer" in desc
    assert len(desc) <= 200


def test_engineering_methodology_rules_dated_research_reuse() -> None:
    """Verify ENGINEERING_METHODOLOGY_RULES and _assemble_turn_prompt include the dated-research reuse rule."""
    expected_rule = (
        "- Reuse dated research: check validated stored literature/documents first, cite their date, "
        "offer at the end to search for anything newer than that date, and search afresh only when stored coverage is incomplete or the user asks."
    )
    assert expected_rule in ENGINEERING_METHODOLOGY_RULES

    assembled = _assemble_turn_prompt("What is the maximum growth rate?")
    assert expected_rule in assembled
    assert "Reuse dated research" in assembled


def test_literature_sources_dated_research_reuse(isolated_db) -> None:
    """Verify literature_sources exposes created_at, updated_at, validated, state filter, and dated_research_summary."""
    # 1. Unfiltered call: returns all 4 sources
    call = _make_call({"grant_id": "g-1", "view": "literature_sources"})
    res, err = dispatch_knowledge_read_tool(call, "workspace-test", "default_error")
    assert err == "default_error"
    assert res is not None
    assert res["view"] == "literature_sources"
    assert res["count"] == 4

    sources = res["literature_sources"]
    src_map = {s["id"]: s for s in sources}

    # Verify all expected keys and new fields
    s1 = src_map["src-accepted-1"]
    assert s1["validated"] is True
    assert s1["backing_state"] == "accepted"
    assert s1["year"] == 2023
    assert s1["created_at"] == "2023-01-10T08:00:00Z"
    assert s1["updated_at"] == "2024-03-15T12:00:00Z"
    assert s1["title"] == "Kinetic Models of Algae"
    assert s1["authors"] == "Alpha Author, 2023"
    assert s1["entry_count"] == 2

    s_rev = src_map["src-review"]
    assert s_rev["validated"] is False
    assert s_rev["backing_state"] == "review"
    assert s_rev["created_at"] == "2025-01-05T09:00:00Z"
    assert s_rev["updated_at"] == "2025-06-01T15:00:00Z"

    s_raw = src_map["src-raw"]
    assert s_raw["validated"] is False
    assert s_raw["backing_state"] == "raw"

    s2 = src_map["src-accepted-2"]
    assert s2["validated"] is True
    assert s2["backing_state"] == "accepted"
    assert s2["year"] == 2021
    assert s2["updated_at"] == "2024-01-10T10:00:00Z"

    # Verify top-level dated_research_summary computed over the accepted sources
    summary = res["dated_research_summary"]
    assert summary["validated_count"] == 2
    assert summary["latest_validated_date"] == "2024-03-15"
    assert summary["latest_published_year"] == 2023
    assert "2024-03-15" in summary["reuse_guidance"]

    # 2. Filter by state="accepted"
    call_accepted = _make_call({"grant_id": "g-1", "view": "literature_sources", "state": "accepted"})
    res_acc, err_acc = dispatch_knowledge_read_tool(call_accepted, "workspace-test", "default_error")
    assert err_acc == "default_error"
    assert res_acc is not None
    assert res_acc["count"] == 2
    assert all(s["validated"] is True for s in res_acc["literature_sources"])
    assert res_acc["dated_research_summary"]["validated_count"] == 2
    assert res_acc["dated_research_summary"]["latest_validated_date"] == "2024-03-15"

    # 3. Filter by state="raw"
    call_raw = _make_call({"grant_id": "g-1", "view": "literature_sources", "state": "raw"})
    res_raw, err_raw = dispatch_knowledge_read_tool(call_raw, "workspace-test", "default_error")
    assert err_raw == "default_error"
    assert res_raw is not None
    assert res_raw["count"] == 1
    assert res_raw["literature_sources"][0]["id"] == "src-raw"
    assert res_raw["dated_research_summary"]["validated_count"] == 0
    assert res_raw["dated_research_summary"]["latest_validated_date"] is None
    assert "No validated stored literature sources matched" in res_raw["dated_research_summary"]["reuse_guidance"]

    # 4. Reject invalid state cleanly with invalid_arguments
    call_invalid_state = _make_call({"grant_id": "g-1", "view": "literature_sources", "state": "unknown_state"})
    res_inv, err_inv = dispatch_knowledge_read_tool(call_invalid_state, "workspace-test", "default_error")
    assert res_inv is None
    assert err_inv == "invalid_arguments"

    # Reject state on non-sources view (e.g. literature_entries)
    call_invalid_view = _make_call({"grant_id": "g-1", "view": "literature_entries", "state": "accepted"})
    res_inv_v, err_inv_v = dispatch_knowledge_read_tool(call_invalid_view, "workspace-test", "default_error")
    assert res_inv_v is None
    assert err_inv_v == "invalid_arguments"


def test_literature_sources_zero_accepted(isolated_db) -> None:
    """Verify literature_sources in a workspace with zero accepted sources returns fallback summary."""
    call = _make_call({"grant_id": "g-1", "view": "literature_sources"}, workspace_id="workspace-unvalidated")
    res, err = dispatch_knowledge_read_tool(call, "workspace-unvalidated", "default_error")
    assert err == "default_error"
    assert res is not None
    assert res["count"] == 1
    src = res["literature_sources"][0]
    assert src["validated"] is False
    assert src["backing_state"] == "raw"

    summary = res["dated_research_summary"]
    assert summary["validated_count"] == 0
    assert summary["latest_validated_date"] is None
    assert summary["latest_published_year"] is None
    assert "No validated stored literature sources matched" in summary["reuse_guidance"]


def test_literature_entries_join_and_dated_summary(isolated_db) -> None:
    """Verify literature_entries joins literature_sources to return source fields and computes dated_research_summary."""
    call = _make_call({"grant_id": "g-1", "view": "literature_entries"})
    res, err = dispatch_knowledge_read_tool(call, "workspace-test", "default_error")
    assert err == "default_error"
    assert res is not None
    assert res["view"] == "literature_entries"
    assert res["count"] == 4

    entries = res["literature_entries"]
    e_map = {e["id"]: e for e in entries}

    # Entry 1: accepted entry linked to accepted source
    e1 = e_map["entry-accepted-from-accepted"]
    assert e1["status"] == "accepted"
    assert e1["source_title"] == "Kinetic Models of Algae"
    assert e1["source_state"] == "accepted"
    assert e1["source_updated_at"] == "2024-03-15T12:00:00Z"
    assert e1["source_published_year"] == 2023
    assert e1["updated_at"] == "2024-05-10T14:00:00Z"
    assert e1["claim_text"] == "Specific growth rate peaks at 30 deg C"
    assert e1["locator"] == "page:12-14"

    # Entry 2: raw entry linked to accepted source
    e2 = e_map["entry-raw-from-accepted"]
    assert e2["status"] == "raw"
    assert e2["source_title"] == "Kinetic Models of Algae"
    assert e2["source_state"] == "accepted"
    assert e2["source_updated_at"] == "2024-03-15T12:00:00Z"
    assert e2["source_published_year"] == 2023
    assert e2["value"] == 0.085
    assert e2["unit"] == "1/h"

    # Entry 3: review entry linked to review source (neither accepted)
    e3 = e_map["entry-review-from-review"]
    assert e3["status"] == "review"
    assert e3["source_state"] == "review"
    assert e3["source_title"] == "Bioreactor Scale-up Report"

    # Entry 4: accepted entry linked to raw source
    e4 = e_map["entry-accepted-from-raw"]
    assert e4["status"] == "accepted"
    assert e4["source_state"] == "raw"
    assert e4["source_title"] == "Unreviewed Raw Notes"
    assert e4["source_published_year"] == 2022
    assert e4["updated_at"] == "2025-03-01T08:00:00Z"

    # Top-level dated_research_summary:
    # Validated entries are e1 (status=accepted), e2 (source_state=accepted), e4 (status=accepted). Total: 3.
    # Latest validated date among them is 2025-03-01 (from e4's updated_at).
    # Latest published year among them is 2023 (from e1 and e2; e4 has 2022).
    summary = res["dated_research_summary"]
    assert summary["validated_count"] == 3
    assert summary["latest_validated_date"] == "2025-03-01"
    assert summary["latest_published_year"] == 2023
    assert "2025-03-01" in summary["reuse_guidance"]

    # Filter by status="accepted": returns e1 and e4
    call_accepted = _make_call({"grant_id": "g-1", "view": "literature_entries", "status": "accepted"})
    res_acc, _ = dispatch_knowledge_read_tool(call_accepted, "workspace-test", "default_error")
    assert res_acc is not None
    assert res_acc["count"] == 2
    assert res_acc["dated_research_summary"]["validated_count"] == 2
    assert res_acc["dated_research_summary"]["latest_validated_date"] == "2025-03-01"


def test_literature_entries_zero_validated(isolated_db) -> None:
    """Verify literature_entries with no accepted entries or sources returns fallback summary."""
    call = _make_call({"grant_id": "g-1", "view": "literature_entries"}, workspace_id="workspace-unvalidated")
    res, err = dispatch_knowledge_read_tool(call, "workspace-unvalidated", "default_error")
    assert err == "default_error"
    assert res is not None
    assert res["count"] == 1
    e = res["literature_entries"][0]
    assert e["status"] == "raw"
    assert e["source_state"] == "raw"

    summary = res["dated_research_summary"]
    assert summary["validated_count"] == 0
    assert summary["latest_validated_date"] is None
    assert summary["latest_published_year"] is None
    assert "No validated stored literature sources matched" in summary["reuse_guidance"]


def test_supervisor_dispatch_tool_with_dated_research(isolated_db) -> None:
    """Verify Hermes supervisor dispatch_tool end-to-end integration for knowledge_read literature views."""
    grant = _make_grant(grant_id="grant-1", workspace_id="workspace-test")

    # 1. Dispatch literature_sources via supervisor
    call_src = _make_call(
        {"grant_id": "grant-1", "view": "literature_sources", "state": "accepted"},
        grant_id="grant-1",
        workspace_id="workspace-test",
    )
    result_src = dispatch_tool(call_src, live_grants={"grant-1": grant})
    assert result_src.status == "succeeded"
    assert result_src.error_code is None
    res_dict = result_src.result
    assert isinstance(res_dict, dict)
    assert res_dict["view"] == "literature_sources"
    assert res_dict["count"] == 2
    dated_sum = res_dict["dated_research_summary"]
    assert isinstance(dated_sum, dict)
    assert dated_sum["validated_count"] == 2
    assert dated_sum["latest_validated_date"] == "2024-03-15"

    # 2. Dispatch literature_entries via supervisor
    call_entry = _make_call(
        {"grant_id": "grant-1", "view": "literature_entries"},
        grant_id="grant-1",
        workspace_id="workspace-test",
    )
    result_entry = dispatch_tool(call_entry, live_grants={"grant-1": grant})
    assert result_entry.status == "succeeded"
    assert result_entry.error_code is None
    entry_dict = result_entry.result
    assert isinstance(entry_dict, dict)
    assert entry_dict["view"] == "literature_entries"
    assert entry_dict["count"] == 4
    dated_sum_e = entry_dict["dated_research_summary"]
    assert isinstance(dated_sum_e, dict)
    assert dated_sum_e["validated_count"] == 3


def test_literature_sources_query_and_state_combined(isolated_db) -> None:
    """Verify combining query and state filters on literature_sources."""
    call = _make_call({
        "grant_id": "g-1",
        "view": "literature_sources",
        "query": "Kinetics",
        "state": "accepted",
    })
    res, err = dispatch_knowledge_read_tool(call, "workspace-test", "default_error")
    assert err == "default_error"
    assert res is not None
    assert res["count"] == 2  # Both accepted sources match "Kinetics"
    assert all(s["validated"] is True for s in res["literature_sources"])

    # Query matching non-accepted source with state="accepted" should return 0
    call_nomatch = _make_call({
        "grant_id": "g-1",
        "view": "literature_sources",
        "query": "Bioreactor",
        "state": "accepted",
    })
    res_nomatch, _ = dispatch_knowledge_read_tool(call_nomatch, "workspace-test", "default_error")
    assert res_nomatch is not None
    assert res_nomatch["count"] == 0
    assert res_nomatch["dated_research_summary"]["validated_count"] == 0

