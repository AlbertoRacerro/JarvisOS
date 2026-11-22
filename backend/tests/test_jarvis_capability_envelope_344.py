"""Tests for Spec 344: JARVIS-CHAT-CAPABILITY-ENVELOPE-1.

Verifies:
1. Unified capability envelope in thread_service._jarvis_system_envelope:
   - Loaded packs (IDs only, or 'none', no host paths/secrets)
   - Route boundary (local vs external, fail-closed)
   - Granted read-only tools and routing cues
   - Deterministic character bounds (<= 2,400 chars)
2. Prompt guidance in context_builder.THREAD_AUTHORITY_INSTRUCTIONS
3. Larger deterministic context budget (48,000 chars / 32 blocks) and priority drop order
4. Retrieval result bounding (<= 12,000 chars) and explicit truncated flag in query_context
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import packs
from app.modules.ai.context_builder import (
    DEFAULT_CONTEXT_BUDGET_CHARS,
    MAX_CONTEXT_BLOCKS,
    THREAD_AUTHORITY_INSTRUCTIONS,
    build_workspace_context_bundle,
)
from app.modules.ai.retrieval_contracts import IndexDocument
from app.modules.ai.retrieval_index import SQLiteIndexStore
from app.modules.ai.retrieval_query import (
    MAX_RETRIEVAL_RESULT_CHARS,
    query_context,
)
from app.modules.ai.thread_service import (
    MAX_SYSTEM_ENVELOPE_CHARS,
    READ_ONLY_TOOL_ROUTING_CUES,
    _jarvis_system_envelope,
)

# --- 1. Envelope: Loaded packs & Host Path / Secret safety -------------------


def test_system_envelope_loaded_packs_none_when_no_pack_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(packs.PACK_PATH_ENV, raising=False)
    packs._load.cache_clear()

    envelope = _jarvis_system_envelope(
        workspace_id="ws-123",
        workspace_name="Main Workspace",
        app_state=None,
        route_availability=[],
        route_class="local:fake",
    )

    assert "Loaded packs: none" in envelope
    assert len(envelope) <= MAX_SYSTEM_ENVELOPE_CHARS


def test_system_envelope_loaded_packs_shows_id_only_without_host_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    zephyr_dir = Path(__file__).resolve().parent / "fixtures" / "packs" / "zephyr"
    assert zephyr_dir.is_dir()
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(zephyr_dir))
    packs._load.cache_clear()

    envelope = _jarvis_system_envelope(
        workspace_id="ws-zephyr",
        workspace_name="Zephyr Workspace",
        app_state=None,
        route_availability=[],
        route_class="local:general",
    )

    # Pack id must be present
    assert "Loaded packs: zephyr" in envelope
    # Host paths must NEVER appear in the envelope
    assert str(zephyr_dir) not in envelope
    assert "fixtures" not in envelope
    assert "/home" not in envelope
    assert len(envelope) <= MAX_SYSTEM_ENVELOPE_CHARS


# --- 2. Route boundary: local vs external & fail-closed ----------------------


@pytest.mark.parametrize(
    "route_class",
    [
        "local:fake",
        "local:llamacpp",
        "local:general",
        "local:qwen2.5-coder",
        "fake",
    ],
)
def test_system_envelope_route_boundary_local(route_class: str) -> None:
    envelope = _jarvis_system_envelope(
        workspace_id="ws-1",
        workspace_name="Workspace 1",
        app_state=None,
        route_availability=[],
        route_class=route_class,
    )
    assert "Route boundary: local (private-pack reads permitted)" in envelope


@pytest.mark.parametrize(
    "route_class",
    [
        None,
        "",
        "unknown",
        "external:scaleway",
        "cloud:anthropic",
        "external:openai",
    ],
)
def test_system_envelope_route_boundary_external_fail_closed(route_class: str | None) -> None:
    envelope = _jarvis_system_envelope(
        workspace_id="ws-1",
        workspace_name="Workspace 1",
        app_state=None,
        route_availability=[],
        route_class=route_class,
    )
    assert "Route boundary: external (private-pack reads blocked)" in envelope


def test_system_envelope_route_boundary_hermes_agent_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    # hermes:agent with default fake/local settings -> local
    envelope = _jarvis_system_envelope(
        workspace_id="ws-1",
        workspace_name="Workspace 1",
        app_state=None,
        route_availability=[],
        route_class="hermes:agent",
    )
    assert "Route boundary: local (private-pack reads permitted)" in envelope

    # hermes:agent with explicit cloud route for isaac -> external
    mock_settings = SimpleNamespace(
        hermes_role_routes={"isaac": "external:openai"},
        provider_mode="cloud",
    )
    monkeypatch.setattr("app.modules.ai.settings.get_ai_settings", lambda: mock_settings)
    envelope_cloud = _jarvis_system_envelope(
        workspace_id="ws-1",
        workspace_name="Workspace 1",
        app_state=None,
        route_availability=[],
        route_class="hermes:agent",
    )
    assert "Route boundary: external (private-pack reads blocked)" in envelope_cloud


# --- 3. Granted read-only tools and routing cues -----------------------------


def test_system_envelope_default_granted_read_tools_and_cues() -> None:
    # When granted_tools is None, safe default includes all 4 read tools
    envelope = _jarvis_system_envelope(
        workspace_id="ws-1",
        workspace_name="Workspace 1",
        app_state=None,
        route_availability=[],
        route_class="local:general",
    )

    assert "Read-only tools granted:" in envelope
    for tool_name, cue in READ_ONLY_TOOL_ROUTING_CUES.items():
        assert tool_name in envelope
        assert cue in envelope


def test_system_envelope_filtered_granted_tools() -> None:
    # Pass subset with and without mcp prefix
    granted = ["mcp__jarvis__jarvis_dev_read", "jarvis_repo_read"]
    envelope = _jarvis_system_envelope(
        workspace_id="ws-1",
        workspace_name="Workspace 1",
        app_state=None,
        route_availability=[],
        route_class="local:general",
        granted_tools=granted,
    )

    assert "Read-only tools granted:" in envelope
    assert "jarvis_dev_read:" in envelope
    assert "jarvis_repo_read:" in envelope
    assert "jarvis_knowledge_read:" not in envelope
    assert "jarvis_retrieval_query:" not in envelope


def test_system_envelope_empty_granted_tools() -> None:
    envelope = _jarvis_system_envelope(
        workspace_id="ws-1",
        workspace_name="Workspace 1",
        app_state=None,
        route_availability=[],
        route_class="local:general",
        granted_tools=[],
    )
    assert "Read-only tools granted: none" in envelope


def test_system_envelope_hard_character_bounding() -> None:
    # Huge capability and route inputs must be deterministically bounded to <= 2,400 chars
    long_routes = [
        {
            "route_class": f"local:route-{i}-" + ("x" * 50),
            "availability": {
                "configured": True,
                "runtime_reachable": True,
                "model_installed": True,
                "model_loaded": True,
                "qualified": "ok",
                "reason_code": "healthy_and_ready_" + ("y" * 50),
            },
        }
        for i in range(20)
    ]
    envelope = _jarvis_system_envelope(
        workspace_id="ws-long-" + ("w" * 50),
        workspace_name="Name-" + ("n" * 50),
        app_state=None,
        route_availability=long_routes,
        route_class="local:fake",
    )
    assert len(envelope) <= MAX_SYSTEM_ENVELOPE_CHARS
    assert MAX_SYSTEM_ENVELOPE_CHARS == 2_400


# --- 4. THREAD_AUTHORITY_INSTRUCTIONS Prompt Guidance ------------------------


def test_thread_authority_instructions_guidance() -> None:
    instructions = THREAD_AUTHORITY_INSTRUCTIONS

    # Write / execution authority stays human-governed
    assert "write and execution authority stays human-governed" in instructions.lower() or "human-operated" in instructions
    assert "cannot mutate files, databases, or run solvers directly" in instructions

    # Read-only tool inspection authority is explicitly allowed
    assert "CAN inspect system state, code/docs, Second Brain memory, literature, and calculations" in instructions
    for tool in ("jarvis_dev_read", "jarvis_repo_read", "jarvis_knowledge_read", "jarvis_retrieval_query"):
        assert tool in instructions

    # Duty to check tools before claiming unavailable & cite exact refs
    assert "call the appropriate read-only tool" in instructions
    assert "Cite exact paths, line numbers, or record IDs" in instructions
    assert "never invent tool outputs" in instructions


# --- 5. Context Budget & Blocks (48,000 chars / 32 blocks) -------------------


def test_context_budget_constants() -> None:
    assert DEFAULT_CONTEXT_BUDGET_CHARS == 48_000
    assert MAX_CONTEXT_BLOCKS == 32


def test_workspace_builder_preserves_priority_drop_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "ctx344"))
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.core.bootstrap import initialize_storage
    initialize_storage(seed_default=True)

    from app.modules.modeling.models import AssumptionCreate, DecisionCreate, ParameterCreate
    from app.modules.modeling.service import create_assumption, create_decision, create_parameter

    # Create parameter, assumption, and decisions
    create_decision("main", DecisionCreate(title="Decision-Important", decision_text="Keep EU Scaleway first", status="accepted"))
    create_assumption("main", AssumptionCreate(statement="Laminar flow in pipe", status="accepted"))
    create_parameter("main", ParameterCreate(name="pipe_d", value="0.05", unit="m", source_ref="spec-344", status="accepted"))

    # When budget is tight, parameters drop before assumptions before decisions
    tight_bundle = build_workspace_context_bundle("main", budget_chars=250)
    assert tight_bundle.dropped_count > 0
    # Decisions have highest priority (drop priority 4), so they should survive longest
    decision_sources = [s for s in tight_bundle.sources if s.get("type") == "decision"]
    assert len(decision_sources) >= 1


# --- 6. Retrieval Result Bounding (12,000 chars) & Truncated Flag ------------


def test_retrieval_query_constants() -> None:
    assert MAX_RETRIEVAL_RESULT_CHARS == 12_000


def test_query_context_bounds_result_and_sets_truncated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.ai.context_builder import canonical_digest
    from app.modules.ai.jarvis_context_models import SourceRef

    # Create several large documents
    docs = {}
    for i in range(10):
        content = f"Document {i} content: " + ("abcdefghijklmnopqrstuvwxyz0123456789 " * 50)
        ref = SourceRef(
            authority_owner="workspace_documents",
            object_type="doc",
            object_id=f"doc-{i}",
            workspace_id="w-ret",
            revision="1",
            content_digest=canonical_digest(content),
        )
        docs[f"doc-{i}"] = IndexDocument(source_ref=ref, text=content)

    store = SQLiteIndexStore(documents=lambda: docs.values(), resolver=lambda ref: docs.get(ref.object_id))
    store.rebuild()

    # Small token budget -> truncated is True
    res_small = query_context(
        "Document",
        workspace_id="w-ret",
        source_scope=("workspace_documents",),
        limit=8,
        token_budget=50,
        store=store,
    )
    assert res_small["truncated"] is True
    assert len(json.dumps(res_small, ensure_ascii=False)) <= MAX_RETRIEVAL_RESULT_CHARS

    # Normal token budget where all documents fit
    res_normal = query_context(
        "Document 0",
        workspace_id="w-ret",
        source_scope=("workspace_documents",),
        limit=1,
        token_budget=2000,
        store=store,
    )
    assert res_normal["truncated"] is False
    assert len(json.dumps(res_normal, ensure_ascii=False)) <= MAX_RETRIEVAL_RESULT_CHARS
