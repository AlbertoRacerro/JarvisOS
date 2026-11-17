"""Tests for Spec 260: Named local roles over the Hermes runtime."""
from __future__ import annotations

import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.core.database import initialize_database, open_sqlite_connection
from app.core.packs import PackError, hermes_role_pack_defaults, read_pack
from app.modules.agents.hermes.roles import (
    ROLE_IDS,
    SHELDON_INSTRUCTION_BLOCK,
    get_role,
    get_role_instructions,
    list_roles,
    require_local_role_route,
)
from app.modules.agents.hermes.supervisor import HermesSupervisor
from app.modules.agents.hermes.worker_shim import (
    ENABLED_TOOLSETS,
    Worker,
    withhold_tools,
)
from app.modules.ai.agent_contracts import AgentSessionRef
from app.modules.ai.models import AISettingsUpdate
from app.modules.ai.settings import (
    get_ai_settings,
    resolve_role_route,
    update_ai_settings,
)
from app.modules.ai.thread_routes import _hermes_agent_availability, _hermes_roles_availability
from app.modules.ai.thread_service import (
    AIThreadConflictError,
    _ensure_hermes_thread_session,
    create_thread,
    get_thread,
    submit_interaction,
)

# ---------------------------------------------------------------------------
# AC1: Role registry
# ---------------------------------------------------------------------------


def test_ac1_role_registry_lists_exactly_four_roles() -> None:
    roles = list_roles()
    assert len(roles) == 4
    role_ids = {r.role_id for r in roles}
    assert role_ids == {"isaac", "gregor", "q", "alfred"}

    for role in roles:
        assert role.display_name.strip()
        assert role.purpose.strip()
        assert role.presentation_instructions.strip()
        # Each role's toolsets must be a subset of ENABLED_TOOLSETS
        assert set(role.toolsets).issubset(ENABLED_TOOLSETS)

    # Toolsets specific checks
    isaac = get_role("isaac")
    assert set(isaac.toolsets).issubset(ENABLED_TOOLSETS)

    gregor = get_role("gregor")
    assert set(gregor.toolsets).issubset(ENABLED_TOOLSETS)

    q = get_role("q")
    assert set(q.toolsets).issubset(ENABLED_TOOLSETS)

    alfred = get_role("alfred")
    assert set(alfred.toolsets).issubset(ENABLED_TOOLSETS)


def test_ac1_sheldon_mode_devil_advocate_instruction() -> None:
    isaac_normal = get_role_instructions("isaac", sheldon_mode=False)
    assert SHELDON_INSTRUCTION_BLOCK not in isaac_normal

    isaac_sheldon = get_role_instructions("isaac", sheldon_mode=True)
    assert SHELDON_INSTRUCTION_BLOCK in isaac_sheldon
    assert "Devil's advocate mode (Sheldon)" in isaac_sheldon

    # Non-isaac roles do not include Sheldon block even if requested
    gregor_sheldon = get_role_instructions("gregor", sheldon_mode=True)
    assert SHELDON_INSTRUCTION_BLOCK not in gregor_sheldon


def test_ac1_unknown_role_is_rejected_and_default_keeps_broker_tools() -> None:
    from app.modules.ai.thread_models import AIThreadSubmit

    with pytest.raises(ValueError, match="unknown Hermes role"):
        get_role("unknown_role")
    with pytest.raises(ValidationError, match="unknown Hermes role"):
        AIThreadSubmit(request_id="r1", prompt="hello", route_class="hermes:agent", role_id="unknown_role")
    assert get_role(None).role_id == "isaac"
    assert "mcp-jarvis" in get_role(None).toolsets


# ---------------------------------------------------------------------------
# AC2: Per-role local model setting & pack defaults
# ---------------------------------------------------------------------------


def test_ac2_settings_model_validation() -> None:
    # Valid local: routes
    update = AISettingsUpdate(hermes_role_routes={"isaac": "local:general", "q": "local:llamacpp"})
    assert update.hermes_role_routes == {"isaac": "local:general", "q": "local:llamacpp"}

    # Reject non-local: routes (paid providers, auto, etc.)
    for invalid in ("openai:gpt-4o", "anthropic:claude", "auto", "http://localhost:8000"):
        with pytest.raises(ValidationError):
            AISettingsUpdate(hermes_role_routes={"isaac": invalid})
    for invalid in ("local:fake", "local:unbound"):
        with pytest.raises(ValidationError):
            AISettingsUpdate(hermes_role_routes={"isaac": invalid})
    with pytest.raises(ValidationError, match="unknown Hermes role"):
        AISettingsUpdate(hermes_role_routes={"other": "local:general"})


def test_ac2_local_prefix_does_not_authorize_network_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.ai import provider_registry
    from app.modules.ai.provider_registry import FallbackEntry

    registry = provider_registry.load_default_provider_registry()
    unsafe = replace(registry.bindings["local:general"], requires_network=True)
    monkeypatch.setattr(provider_registry, "load_default_provider_registry", lambda: replace(
        registry, bindings={**registry.bindings, "local:general": unsafe}))
    with pytest.raises(ValueError, match="local compute"):
        require_local_role_route("local:general")
    monkeypatch.setattr(provider_registry, "load_default_provider_registry", lambda: replace(
        registry, fallback_chains={**registry.fallback_chains, "local:general": (
            FallbackEntry(provider_id=registry.bindings["local:general"].provider_id,
                          model_id=registry.bindings["local:general"].model_id),
            FallbackEntry(provider_id="scaleway", model_id="deepseek-v4-flash-0731"),
        )}))
    with pytest.raises(ValueError, match="fallback"):
        require_local_role_route("local:general")


def test_ac2_update_ai_settings_rejects_non_local_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data"))
    initialize_database()

    # Valid local update
    result = update_ai_settings(AISettingsUpdate(hermes_role_routes={"isaac": "local:general"}))
    assert result.hermes_role_routes["isaac"] == "local:general"

    # Non-local and unregistered local routes are rejected at the request model (422 at the route)
    for route in ("paid:openai", "local:ollama"):
        with pytest.raises(ValidationError):
            AISettingsUpdate(hermes_role_routes={"isaac": route})
    assert get_ai_settings().hermes_role_routes["isaac"] == "local:general"


def test_ac2_pack_defaults_and_env_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data"))
    initialize_database()
    monkeypatch.setenv("JARVIS_HERMES_INFERENCE_ROUTE", "local:env-model")

    # 1. No pack: defaults to env route for all roles
    monkeypatch.delenv("JARVISOS_PACK_PATH", raising=False)
    for role_id in ROLE_IDS:
        assert resolve_role_route(role_id) == "local:env-model"

    # 2. Fake pack zephyr (no [hermes_roles]): falls back to env route
    zephyr_path = Path(__file__).resolve().parent / "fixtures" / "packs" / "zephyr"
    monkeypatch.setenv("JARVISOS_PACK_PATH", str(zephyr_path))
    assert hermes_role_pack_defaults() == {}
    for role_id in ROLE_IDS:
        assert resolve_role_route(role_id) == "local:env-model"

    # 3. Pack with [hermes_roles] section
    custom_pack = tmp_path / "custom_pack"
    custom_pack.mkdir()
    (custom_pack / "jarvis_pack.toml").write_text(
        'id = "test_pack"\n'
        '[hermes_roles]\n'
        'isaac = "local:pack-isaac"\n'
        'gregor = "local:pack-gregor"\n'
    )
    monkeypatch.setenv("JARVISOS_PACK_PATH", str(custom_pack))
    pack_defaults = hermes_role_pack_defaults()
    assert pack_defaults == {"isaac": "local:pack-isaac", "gregor": "local:pack-gregor"}

    # Resolves pack default for isaac and gregor, env route for q and alfred
    assert resolve_role_route("isaac") == "local:pack-isaac"
    assert resolve_role_route("gregor") == "local:pack-gregor"
    assert resolve_role_route("q") == "local:env-model"
    assert resolve_role_route("alfred") == "local:env-model"

    # 4. User setting in DB overrides pack default
    update_ai_settings(AISettingsUpdate(hermes_role_routes={"isaac": "local:general"}))
    assert resolve_role_route("isaac") == "local:general"
    assert resolve_role_route("gregor") == "local:pack-gregor"  # Still pack default


def test_ac2_pack_manifest_rejects_non_local_routes(tmp_path: Path) -> None:
    invalid_pack = tmp_path / "invalid_pack"
    invalid_pack.mkdir()
    (invalid_pack / "jarvis_pack.toml").write_text(
        'id = "invalid"\n'
        '[hermes_roles]\n'
        'isaac = "paid:openai"\n'
    )
    with pytest.raises(PackError, match="must start with 'local:'"):
        read_pack(invalid_pack)


# ---------------------------------------------------------------------------
# AC3: Worker shim, toolset filtering, and instructions
# ---------------------------------------------------------------------------


def test_ac3_withhold_tools_never_widens_tools() -> None:
    # 1. Strips withheld tools (skill_manage)
    agent = SimpleNamespace()
    agent.tools = [
        {"function": {"name": "mcp__jarvis__jarvis_context_preview"}},
        {"function": {"name": "skill_manage"}},  # Withheld tool
    ]
    names = withhold_tools(agent)
    assert names == ["mcp__jarvis__jarvis_context_preview"]
    assert "skill_manage" not in names

    # 2. Rejects any tool not in ADMITTED_TOOLS (fail-closed)
    agent_unadmitted = SimpleNamespace()
    agent_unadmitted.tools = [
        {"function": {"name": "unadmitted_tool"}},
    ]
    with pytest.raises(RuntimeError, match="outside the Jarvis admission set"):
        withhold_tools(agent_unadmitted)


def _fake_worker() -> Worker:
    worker = Worker.__new__(Worker)
    worker.token = "test-token"
    worker.base_url = "http://127.0.0.1:1/v1"
    worker.session = None
    worker.session_db = None
    worker.agent = None
    worker.tool_names = []
    worker.turn_lock = threading.Lock()
    worker.history = []
    worker.sequences = {}
    worker.sequence = 0
    worker.pending_lock = threading.Lock()
    worker.pending = {}
    worker.active_turn_tools = None
    worker.send = lambda _frame: None  # type: ignore[method-assign]
    return worker


def test_ac3_worker_builds_agent_with_role_instructions_and_toolsets(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _fake_worker()

    # Mock Hermes dependencies needed for _build_agent
    mock_agent_instance = MagicMock()
    mock_agent_instance.tools = [{"function": {"name": "mcp__jarvis__jarvis_context_preview"}}]
    mock_agent_cls = MagicMock(return_value=mock_agent_instance)

    mock_run_agent = ModuleType("run_agent")
    mock_run_agent.AIAgent = mock_agent_cls  # type: ignore[attr-defined]

    mock_registry = ModuleType("hermes_state_registry")
    mock_registry.acquire = MagicMock(return_value="fake_db")  # type: ignore[attr-defined]

    mock_discovery = ModuleType("tools.mcp_tool_discovery")
    mock_discovery.register_mcp_servers = MagicMock(  # type: ignore[attr-defined]
        return_value=["mcp__jarvis__jarvis_context_preview", "mcp__jarvis__jarvis_retrieval_query"]
    )

    monkeypatch.setitem(sys.modules, "run_agent", mock_run_agent)
    monkeypatch.setitem(sys.modules, "hermes_state_registry", mock_registry)
    monkeypatch.setitem(sys.modules, "tools.mcp_tool_discovery", mock_discovery)

    # 1. Isaac role
    worker.session = {"profile_id": "isaac", "hermes_session_id": "h-1"}
    agent = worker._build_agent()
    assert agent is mock_agent_instance
    isaac = get_role("isaac")
    call_kwargs = mock_agent_cls.call_args[1]
    assert call_kwargs["enabled_toolsets"] == list(isaac.toolsets)
    assert call_kwargs["ephemeral_system_prompt"] == isaac.presentation_instructions

    # 2. Rebinding with a different role closes previous agent
    mock_agent_instance.close = MagicMock()
    worker.agent = mock_agent_instance
    worker.handle_frame({
        "type": "bind",
        "id": "bind-2",
        "session_ref": {
            "profile_id": "gregor",
            "hermes_session_id": "h-2",
            "jarvis_thread_id": "t-1",
            "workspace_id": "ws-1",
            "generation": 2,
        },
    })
    mock_agent_instance.close.assert_called_once()
    assert worker.agent is not None  # Rebuilt for gregor


def test_ac3_sheldon_mode_applied_in_run_turn() -> None:
    worker = _fake_worker()
    fake_agent = SimpleNamespace(
        ephemeral_system_prompt="",
        tools=[{"function": {"name": "mcp__jarvis__jarvis_context_preview"}}],
        run_conversation=MagicMock(return_value={"messages": [], "turn_exit_reason": "text_response",
                                                 "final_response": "response", "completed": True}),
    )
    worker.agent = fake_agent
    worker.session = {"profile_id": "isaac", "hermes_session_id": "h-1", "generation": 1}

    sent_frames: list[dict[str, Any]] = []
    worker.send = sent_frames.append  # type: ignore[assignment]
    worker.event = lambda _kind, _id: None  # type: ignore[method-assign]

    # Run turn with sheldon_mode=True
    worker.run_turn("req-1", "test prompt", sheldon_mode=True)
    assert SHELDON_INSTRUCTION_BLOCK in fake_agent.ephemeral_system_prompt

    # Run turn with sheldon_mode=False
    worker.run_turn("req-2", "test prompt", sheldon_mode=False)
    assert SHELDON_INSTRUCTION_BLOCK not in fake_agent.ephemeral_system_prompt


# ---------------------------------------------------------------------------
# AC3: Session binding & thread service
# ---------------------------------------------------------------------------


def test_ac3_session_binding_to_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data"))
    initialize_database()

    with open_sqlite_connection() as connection:
        connection.execute(
            "INSERT INTO workspaces (id, name, slug, description, created_at, updated_at) "
            "VALUES ('ws1', 'Workspace', 'ws1', '', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        connection.commit()

    # Create thread in DB
    from app.modules.ai.thread_models import AIThreadCreate
    from app.modules.ai.thread_service import create_thread
    thread = create_thread(AIThreadCreate(workspace_id="ws1", title="Title"))

    supervisor = HermesSupervisor("unused")
    monkeypatch.setattr(supervisor, "start", lambda: None)
    monkeypatch.setattr(supervisor, "_alive", lambda: True)
    sent_frames: list[dict[str, Any]] = []
    monkeypatch.setattr(supervisor, "_send", sent_frames.append)
    monkeypatch.setattr(supervisor, "_await", lambda _id, **_kwargs: {"type": "ack"})

    # Session for Isaac
    _ensure_hermes_thread_session(supervisor, thread_id=thread.id, workspace_id="ws1", role_id="isaac")
    assert supervisor.session is not None
    assert supervisor.session.profile_id == "isaac"
    assert supervisor.session.generation == 1
    session_1_id = supervisor.session.hermes_session_id

    # Calling with same role keeps same session
    _ensure_hermes_thread_session(supervisor, thread_id=thread.id, workspace_id="ws1", role_id="isaac")
    assert supervisor.session.hermes_session_id == session_1_id
    assert supervisor.session.generation == 1

    # Calling with different role (gregor) starts a NEW session (new hermes_session_id, generation 2)
    _ensure_hermes_thread_session(supervisor, thread_id=thread.id, workspace_id="ws1", role_id="gregor")
    assert supervisor.session.profile_id == "gregor"
    assert supervisor.session.generation == 2
    assert supervisor.session.hermes_session_id != session_1_id


def test_ac3_supervisor_resolves_role_route() -> None:
    resolved_routes: list[str] = []

    def fake_route_for_role(role_id: str) -> str:
        route = f"local:model-for-{role_id}"
        resolved_routes.append(route)
        return route

    supervisor = HermesSupervisor("unused", route_for_role=fake_route_for_role)
    supervisor.session = AgentSessionRef(
        jarvis_thread_id="t1",
        hermes_session_id="h1",
        profile_id="alfred",
        workspace_id="ws1",
        generation=1,
        upstream_revision="rev1",
    )

    sent: list[dict[str, Any]] = []
    supervisor._send = sent.append  # type: ignore[assignment]

    frame = {
        "id": "relay-1",
        "session_ref": supervisor.session.model_dump(mode="json"),
        "messages": [{"role": "user", "content": "hello"}],
    }
    # Handling relay invokes route_for_role with "alfred"
    supervisor._handle_relay(frame)

    assert "local:model-for-alfred" in resolved_routes


def test_role_and_sheldon_are_bound_to_replay_and_successful_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.ai.thread_models import AIThreadCreate, AIThreadSubmit
    from app.modules.ai.token_flow_service import create_flow_in_transaction
    from app.modules.events.service import log_event

    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data"))
    initialize_database()
    with open_sqlite_connection() as connection:
        connection.execute(
            "INSERT INTO workspaces (id, name, slug, description, created_at, updated_at) "
            "VALUES ('ws1', 'Workspace', 'ws1', '', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        connection.commit()
    thread = create_thread(AIThreadCreate(workspace_id="ws1"))
    base = dict(request_id="r1", prompt="Hello", route_class="hermes:agent", role_id="isaac")
    first = submit_interaction(workspace_id="ws1", thread_id=thread.id,
                               payload=AIThreadSubmit(**base, sheldon_mode=True), route_availability=[])
    assert first.interaction.route_class == "hermes:agent"
    for changed in ({**base, "role_id": "gregor", "sheldon_mode": True},
                    {**base, "sheldon_mode": False}):
        with pytest.raises(AIThreadConflictError, match="different submit semantics"):
            submit_interaction(workspace_id="ws1", thread_id=thread.id,
                               payload=AIThreadSubmit(**changed), route_availability=[])
    with open_sqlite_connection() as connection:
        relay_flow_id = create_flow_in_transaction(connection, task_kind="general",
                                                    requested_route_class="local:general", workspace_id="ws1")
        connection.execute("UPDATE ai_thread_interactions SET flow_id = ?, persistence_state = 'captured', "
                           "assistant_text = 'Reply' WHERE id = ?", (relay_flow_id, first.interaction.id))
        log_event(connection, event_type="hermes.interaction_flows", actor="jarvis",
                  target_type="ai_thread_interaction", target_id=first.interaction.id,
                  workspace_id="ws1", payload={"role_id": "isaac", "sheldon_mode": True,
                                               "final_flow_id": relay_flow_id})
        connection.commit()
    interaction = get_thread(workspace_id="ws1", thread_id=thread.id).interactions[0]
    assert interaction.assistant_text == "Reply"
    assert interaction.route_class == "hermes:agent"
    assert interaction.execution_class == "agent"
    assert interaction.role_id == "isaac"
    assert interaction.sheldon_mode is True


# ---------------------------------------------------------------------------
# Options availability
# ---------------------------------------------------------------------------


def test_hermes_roles_availability_reporting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data"))
    initialize_database()

    options = _hermes_roles_availability(None)
    assert len(options) == 4
    for opt in options:
        assert opt.role_id in ROLE_IDS
        assert opt.display_name
        assert opt.purpose
        assert opt.route.startswith("local:")


def test_agent_availability_uses_selected_role_routes_not_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.ai.thread_models import AIConversationRouteAvailability

    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.touch()
    monkeypatch.setenv("JARVIS_HERMES_VENV", str(tmp_path))
    monkeypatch.setenv("JARVIS_HERMES_INFERENCE_ROUTE", "local:unbound")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    monkeypatch.setattr("app.modules.ai.thread_routes._hermes_revision_qualified", lambda *_args: True)
    monkeypatch.setattr("app.modules.ai.settings.resolve_role_route", lambda role: "local:general" if role == "gregor" else "local:unbound")
    monkeypatch.setattr("app.modules.ai.thread_routes._ollama_route_availability", lambda *_args:
                        AIConversationRouteAvailability(configured=True, runtime_reachable=True,
                            model_installed=True, model_loaded=False, qualified="unknown", message="ready"))

    roles = _hermes_roles_availability(None)
    assert {role.role_id for role in roles if role.available} == {"gregor"}
    assert _hermes_agent_availability(None, roles).runtime_reachable is True
