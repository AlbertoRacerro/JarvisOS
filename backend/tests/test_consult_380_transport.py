"""Spec 380 part b tests: Relay consult transport, guarded MCP, egress rule, rendering, and thread injection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from app.core import packs, vault
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.execution import check_egress_level_rule
from app.modules.ai.thread_service import consult_untrusted_blocks
from app.modules.consult.config import TrustedAccount
from app.modules.consult.mcp_server import TOOL_DEFINITIONS, ConsultMcpServer
from app.modules.consult.render import render_consult_blocks
from app.modules.consult.service import (
    ConsultRequest,
    approve_consult,
    request_consult,
)
from app.modules.consult.transport import (
    DEFAULT_CONSULT_TRANSPORT,
    build_consult_manifest,
)
from app.modules.workspaces.service import create_workspace, default_workspace_payload
from tests.test_consult_380_flow import BRIEF, DIGEST

ACCOUNT = TrustedAccount(
    id="sub",
    agent="claude",
    model="fake-consultant",
    training_off_verified_on=date(2026, 11, 25),
)


@pytest.fixture()
def transport_env(isolated_data_root: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "brief.md").write_text(BRIEF, encoding="utf-8")
    (pack / "jarvis_pack.toml").write_text(
        'id = "orchard"\n[sensitivity]\nterms = ["secretfruit"]\n[consult]\nbrief = "brief.md"\nnucleus_heading = "Nucleus"\n',
        encoding="utf-8",
    )
    v = tmp_path / "vault"
    v.mkdir()
    (v / "jarvis_vault.toml").write_text("", encoding="utf-8")
    (v / "consult.toml").write_text(
        f'consult_mode = "ask"\nbrief_sha256 = "{DIGEST}"\n[[trusted_accounts]]\nid = "sub"\nagent = "claude"\n'
        'model = "fake-consultant"\ntraining_off_verified_on = 2026-11-25\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack))
    monkeypatch.setenv(vault.VAULT_PATH_ENV, str(v))
    monkeypatch.setenv("JARVISOS_CONSULT_FAKE_TRANSPORT", "1")
    packs._load.cache_clear()
    initialize_database()
    with open_sqlite_connection() as conn:
        yield conn
    packs._load.cache_clear()


def test_ai_job_created_on_consult_zone_a_and_linked(transport_env: sqlite3.Connection) -> None:
    """Every sent consult runs through run_ai_task, creating ONE ai_jobs row and storing ai_job_id."""
    pending = request_consult(
        transport_env,
        ConsultRequest(kind="ideas", question="How do roots find water?", modules=["M2"]),
        transport=DEFAULT_CONSULT_TRANSPORT,
    )
    transport_env.commit()

    row = transport_env.execute(
        "SELECT packet_sha256 FROM consult_attempts WHERE id = ?",
        (pending.consult_id,),
    ).fetchone()
    assert row is not None
    packet_sha = row[0]

    outcome = approve_consult(
        transport_env,
        pending.consult_id,
        packet_sha,
        transport=DEFAULT_CONSULT_TRANSPORT,
    )
    assert outcome.status == "answered"
    transport_env.commit()

    attempt = transport_env.execute(
        "SELECT state, ai_job_id, packet_sha256 FROM consult_attempts WHERE id = ?",
        (pending.consult_id,),
    ).fetchone()
    assert attempt is not None
    assert attempt["state"] == "sent"
    ai_job_id = attempt["ai_job_id"]
    assert ai_job_id is not None
    assert bool(ai_job_id)
    assert len(ai_job_id) > 10

    # Inspect the ai_jobs ledger row
    job = transport_env.execute(
        "SELECT id, selected_route_class, task_kind, status, cost_estimate, route_reason_json FROM ai_jobs WHERE id = ?",
        (ai_job_id,),
    ).fetchone()
    assert job is not None
    assert job["selected_route_class"] == "consult:zone_a"
    assert job["task_kind"] == "consult"
    assert job["status"] == "success"
    assert float(job["cost_estimate"]) == 0.0
    reason_meta = json.loads(job["route_reason_json"])
    assert reason_meta.get("accounting_basis") == "subscription"


def test_build_consult_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Relay consult manifest: empty workspace, no repo, no common_egress, no built-in web tools, deny_roots."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    monkeypatch.setenv(vault.VAULT_PATH_ENV, str(vault_dir))

    gateway_config = {
        "enabled": True,
        "common_egress": ["common.example.com", "shared.example.com"],
        "agents": {
            "claude": {
                "egress": ["api.anthropic.com", "auth.anthropic.com"],
                "command": "claude",
            }
        },
    }

    workspace = tmp_path / "consult-workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    home = tmp_path / "consult-home"
    home.mkdir()
    context_dir = tmp_path / "context"
    runtime_root = tmp_path / "runtime"
    mcp_sock = Path("/run/jarvis/consult-mcp.sock")

    manifest = build_consult_manifest(
        gateway_config,
        "claude",
        workspace,
        home,
        context_dir,
        runtime_root,
        mcp_socket_path=mcp_sock,
    )

    # 1. Egress allow: ONLY the agent's egress (NO common_egress)
    assert set(manifest["egress_allow"]) == {"api.anthropic.com", "auth.anthropic.com"}
    assert "common.example.com" not in manifest["egress_allow"]

    # 2. deny_roots contains vault
    assert manifest["deny_roots"] == [str(vault_dir)]

    # 3. Built-in web tools disabled and restricted flags
    args = manifest["agents"]["claude"]["sandbox_args"]
    assert "--restricted" in args
    assert "--strict-mcp-config" in args
    assert "--tools" in args and "" in args
    assert any("WebSearch" in a for a in args)

    # 4. MCP socket path configured
    assert manifest.get("mcp_socket") == str(mcp_sock)


def test_egress_level_rule(transport_env: sqlite3.Connection) -> None:
    """Egress level rule: S4 blocked everywhere, S2/S3 admitted only on consult:zone_a with matching approved attempt."""
    s4_text = "Here is the key: ANTHROPIC_API_KEY=sk-ant-api03-1234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890"
    s2_text = "This is confidential partner-only research documentation."
    s0_text = "How does soil composition affect apple tree root depth?"

    # S4 is blocked everywhere
    allowed, reason = check_egress_level_rule(s4_text, "consult:zone_a", connection=transport_env)
    assert not allowed
    assert reason == "secret_detected"

    allowed, reason = check_egress_level_rule(s4_text, "scaleway:default", connection=transport_env)
    assert not allowed
    assert reason == "secret_detected"

    # S0/S1 on non-consult route is allowed, but consult:zone_a ALWAYS requires an approved attempt
    allowed, reason = check_egress_level_rule(s0_text, "scaleway:default", connection=transport_env)
    assert allowed
    assert reason is None

    allowed, reason = check_egress_level_rule(s0_text, "consult:zone_a", connection=transport_env)
    assert not allowed
    assert reason == "egress_unapproved_attempt"

    # S2/S3 on non-consult route is refused
    allowed, reason = check_egress_level_rule(s2_text, "scaleway:default", connection=transport_env)
    assert not allowed
    assert reason == "egress_level_violation"

    # S2/S3 on consult:zone_a without matching approved attempt is refused
    allowed, reason = check_egress_level_rule(s2_text, "consult:zone_a", connection=transport_env)
    assert not allowed
    assert reason == "egress_unapproved_attempt"

    # Insert approved consult attempts with matching digests (both S0 and S2)
    s0_sha = hashlib.sha256(s0_text.encode("utf-8")).hexdigest()
    s2_sha = hashlib.sha256(s2_text.encode("utf-8")).hexdigest()
    transport_env.execute(
        """
        INSERT INTO consult_attempts (
            id, turn_id, kind, modules_json, brief_sha256, packet_text, packet_sha256,
            account_id, model_id, state, mode, gate_steps_json, audit, created_at, updated_at
        ) VALUES (
            'attempt-s0-test', 'turn-1', 'ideas', '[]', 'brief-sha', ?, ?,
            'sub', 'claude-opus-5-5', 'approved', 'ask', '[]', 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        )
        """,
        (s0_text, s0_sha),
    )
    transport_env.execute(
        """
        INSERT INTO consult_attempts (
            id, turn_id, kind, modules_json, brief_sha256, packet_text, packet_sha256,
            account_id, model_id, state, mode, gate_steps_json, audit, created_at, updated_at
        ) VALUES (
            'attempt-s2-test', 'turn-1', 'ideas', '[]', 'brief-sha', ?, ?,
            'sub', 'claude-opus-5-5', 'approved', 'ask', '[]', 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        )
        """,
        (s2_text, s2_sha),
    )
    transport_env.commit()

    # S0/S1 and S2/S3 on consult:zone_a WITH matching approved attempts are allowed
    allowed, reason = check_egress_level_rule(s0_text, "consult:zone_a", connection=transport_env)
    assert allowed
    assert reason is None

    allowed, reason = check_egress_level_rule(s2_text, "consult:zone_a", connection=transport_env)
    assert allowed
    assert reason is None


def test_consult_zone_a_blocks_direct_gateway_and_context_blocks(transport_env: sqlite3.Connection) -> None:
    """Direct gateway dispatch, unapproved S0/S1, and non-empty context_blocks on consult:zone_a are refused."""
    from app.modules.ai.execution import run_ai_task
    from app.modules.ai.execution_types import ProviderBinding
    from app.modules.ai.gateway import AIGateway
    from app.modules.ai.models import AITaskRunRequest
    from app.modules.consult.transport import RelayConsultAdapter

    # 1. Direct AIGateway.run_task on consult:zone_a is blocked before building context
    resp = AIGateway().run_task(
        AITaskRunRequest(
            prompt="Generic question",
            route_class="consult:zone_a",
            include_project_context=True,
        )
    )
    assert resp.status == "validation_error"
    assert resp.blocked_reason == "consult_route_direct_dispatch_forbidden"
    assert resp.response_text is None

    # 2. run_ai_task on consult:zone_a with context_blocks is blocked even with an approved attempt
    packet = "Approved S0 packet text"
    packet_sha = hashlib.sha256(packet.encode("utf-8")).hexdigest()
    transport_env.execute(
        """
        INSERT INTO consult_attempts (
            id, turn_id, kind, modules_json, brief_sha256, packet_text, packet_sha256,
            account_id, model_id, state, mode, gate_steps_json, audit, created_at, updated_at
        ) VALUES (
            'attempt-ctx-test', 'turn-1', 'ideas', '[]', 'brief-sha', ?, ?,
            'sub', 'claude-opus-5-5', 'approved', 'ask', '[]', 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        )
        """,
        (packet, packet_sha),
    )
    transport_env.commit()
    called = False

    def _boom(_manifest: dict, _packet: str, _timeout: float) -> str:
        nonlocal called
        called = True
        return "should not run"

    binding = ProviderBinding(
        route_class="consult:zone_a",
        provider_id="relay_consult",
        model_id="fake-consultant",
        requires_network=False,
        max_output_tokens=1024,
        execution_class="external_provider",
    )
    outcome = run_ai_task(
        user_prompt=packet,
        task_kind="consult",
        route_class="consult:zone_a",
        context_blocks=[{"source": "workspace", "content": "secret project data"}],
        bindings={"consult:zone_a": binding},
        adapters={"relay_consult": RelayConsultAdapter(runner=_boom)},
    )
    assert outcome.status == "validation_error"
    assert outcome.error_type == "egress_context_blocks_forbidden"
    assert not called

    # 3. Unapproved S0/S1 on consult:zone_a via run_ai_task is blocked
    unapproved = run_ai_task(
        user_prompt="Unapproved benign S0 question",
        task_kind="consult",
        route_class="consult:zone_a",
        bindings={"consult:zone_a": binding},
        adapters={"relay_consult": RelayConsultAdapter(runner=_boom)},
    )
    assert unapproved.status == "validation_error"
    assert unapproved.error_type == "egress_unapproved_attempt"
    assert not called

    # 4. A consult binding marked requires_network still goes through the approval check
    from dataclasses import replace

    network_binding = replace(binding, requires_network=True)
    via_network = run_ai_task(
        user_prompt="Unapproved benign S0 question",
        task_kind="consult",
        route_class="consult:zone_a",
        bindings={"consult:zone_a": network_binding},
        adapters={"relay_consult": RelayConsultAdapter(runner=_boom)},
    )
    assert via_network.status == "validation_error"
    assert via_network.error_type == "egress_unapproved_attempt"
    assert not called


class _FakeHTTPResponse:
    def __init__(self, data: bytes):
        self._data = data
        self.headers = {"Content-Length": str(len(data))}

    def read(self, *args: object) -> bytes:
        return self._data

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class _FakeOpener:
    def __init__(self, payload: bytes):
        self._payload = payload

    def open(self, req: object, timeout: float = 10.0) -> _FakeHTTPResponse:
        return _FakeHTTPResponse(self._payload)


def test_guarded_mcp_server(tmp_path: Path) -> None:
    """Guarded MCP exposes ONLY web_search, paper_search, read_page, and enforces sensitivity and search guard."""
    tool_names = {t["name"] for t in TOOL_DEFINITIONS}
    assert tool_names == {"web_search", "paper_search", "read_page"}

    fake_epmc = json.dumps({
        "resultList": {
            "result": [
                {
                    "title": "Root growth study",
                    "authorString": "Müller K",
                    "pubYear": "2023",
                    "journalTitle": "Botany",
                    "doi": "10.1002/bit.28001",
                    "abstractText": "Root growth mechanisms...",
                }
            ]
        }
    }).encode("utf-8")

    server = ConsultMcpServer(
        str(tmp_path / "mcp.sock"),
        paper_opener=_FakeOpener(fake_epmc),
        browser_runner=lambda cmd, timeout: (0, "Public page content about botany.", ""),
        dns_resolver=lambda host: ["93.184.216.34"],
    )

    # 1. Blocks S4 / secret query
    res = server.handle_request({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web_search",
            "arguments": {
                "query": "ANTHROPIC_API_KEY=sk-ant-api03-1234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890"
            },
        },
    })
    assert res is not None
    assert "search_guard_blocked" in res["result"]["content"][0]["text"]

    # 2. Blocks S2/S3 confidential query
    res = server.handle_request({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "web_search",
            "arguments": {"query": "confidential private project details"},
        },
    })
    assert res is not None
    assert "search_guard_blocked" in res["result"]["content"][0]["text"]

    # 3. Paper search with safe query succeeds
    res_paper = server.handle_request({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "paper_search",
            "arguments": {"query": "apple tree roots"},
        },
    })
    assert res_paper is not None
    assert "Root growth study" in res_paper["result"]["content"][0]["text"]

    # 4. Read page with safe url succeeds
    res_page = server.handle_request({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "read_page",
            "arguments": {"url": "https://example.com/botany"},
        },
    })
    assert res_page is not None
    assert "Public page content about botany." in res_page["result"]["content"][0]["text"]

    # 5. Unsupported tool is refused
    res_unsupported = server.handle_request({
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {
            "name": "execute_command",
            "arguments": {"cmd": "ls"},
        },
    })
    assert res_unsupported is not None
    assert "error" in res_unsupported
    assert "not exposed" in res_unsupported["error"]["message"]


def test_render_consult_blocks_and_byte_identity(transport_env: sqlite3.Connection) -> None:
    """Test marker parsing, missing-marker append, and byte-identity verification."""
    answer_text = "Water travels via xylem gradient."
    answer_sha = hashlib.sha256(answer_text.encode("utf-8")).hexdigest()
    packet_text = "Question:\nHow does water move?"
    packet_sha = hashlib.sha256(packet_text.encode("utf-8")).hexdigest()

    # Insert two attempts for turn-render-1: c1 (will have marker) and c2 (marker omitted)
    transport_env.execute(
        """
        INSERT INTO consult_attempts (
            id, turn_id, kind, modules_json, brief_sha256, packet_text, packet_sha256,
            account_id, model_id, state, mode, gate_steps_json, audit, created_at, updated_at
        ) VALUES (
            'c1', 'turn-render-1', 'physics', '["M1"]', 'brief-sha', ?, ?,
            'sub', 'claude-opus-5-5', 'sent', 'ask', '[]', 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        )
        """,
        (packet_text, packet_sha),
    )
    transport_env.execute(
        """
        INSERT INTO consult_answers (
            id, attempt_id, answer_text, answer_sha256, model_id, created_at
        ) VALUES (
            'ans-1', 'c1', ?, ?, 'claude-opus-5-5', '2026-01-01T00:00:05Z'
        )
        """,
        (answer_text, answer_sha),
    )

    # c2: pending attempt whose marker is omitted from assistant text
    transport_env.execute(
        """
        INSERT INTO consult_attempts (
            id, turn_id, kind, modules_json, brief_sha256, packet_text, packet_sha256,
            account_id, model_id, state, mode, gate_steps_json, audit, created_at, updated_at
        ) VALUES (
            'c2', 'turn-render-1', 'ideas', '["M2"]', 'brief-sha', 'Question:\nMore ideas?', 'c2-sha',
            'sub', 'claude-opus-5-5', 'awaiting_approval', 'ask', '[]', 0, '2026-01-01T00:00:01Z', '2026-01-01T00:00:01Z'
        )
        """,
    )
    transport_env.commit()

    # Render with marker only for c1
    assistant_text = "Here is the expert insight:\n\n{{consult:c1}}\n\nFollow up question pending."
    blocks = render_consult_blocks(transport_env, "turn-render-1", assistant_text)

    # Both blocks should be returned (c1 parsed from marker, c2 appended because missing)
    assert len(blocks) == 2
    b1 = next(b for b in blocks if b.consult_id == "c1")
    assert b1.state == "sent"
    assert b1.answer_text == answer_text
    assert b1.answer_sha256 == answer_sha
    assert b1.byte_identical is True  # sha matches!

    b2 = next(b for b in blocks if b.consult_id == "c2")
    assert b2.state == "awaiting_approval"
    assert b2.byte_identical is False


def test_next_turn_untrusted_injection(transport_env: sqlite3.Connection) -> None:
    """Newly answered ask-mode consult from previous turn is injected as untrusted-data summary bounded to <= 6000 chars."""
    ws = create_workspace(default_workspace_payload())
    ws_id = ws.id
    thread_id = "thread-inject-1"
    turn_1_id = "turn-inject-1"

    # Seed thread and turn 1
    transport_env.execute(
        """
        INSERT OR IGNORE INTO ai_flows (
            id, workspace_id, task_kind, state, created_at, updated_at
        ) VALUES (
            'flow-1', ?, 'thread_turn', 'complete', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        )
        """,
        (ws_id,),
    )
    transport_env.execute("INSERT OR IGNORE INTO ai_threads (id, workspace_id, title, created_at, last_activity_at) VALUES (?, ?, 'Test Thread', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", (thread_id, ws_id))
    transport_env.execute(
        """
        INSERT OR IGNORE INTO ai_thread_interactions (
            id, thread_id, request_id, request_digest, interaction_index, user_text,
            assistant_text, flow_id, persistence_state, created_at, updated_at
        ) VALUES (
            ?, ?, 'req-1', 'sha-1', 1, 'Initial question', 'Initial answer', 'flow-1', 'captured',
            '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        )
        """,
        (turn_1_id, thread_id),
    )

    # Insert an answered ask-mode consult attempt on turn 1 with long answer
    long_answer = "Important physical finding. " * 400  # > 6000 characters
    long_sha = hashlib.sha256(long_answer.encode("utf-8")).hexdigest()
    transport_env.execute(
        """
        INSERT INTO consult_attempts (
            id, turn_id, thread_id, workspace_id, kind, modules_json, brief_sha256,
            packet_text, packet_sha256, account_id, model_id, state, mode, gate_steps_json,
            audit, created_at, updated_at
        ) VALUES (
            'consult-inj-1', ?, ?, ?, 'physics', '[]', 'brief-sha', 'Question: Roots?',
            'pkt-sha', 'sub', 'claude-opus-5-5', 'sent', 'ask', '[]', 0,
            '2026-01-01T00:00:01Z', '2026-01-01T00:00:05Z'
        )
        """,
        (turn_1_id, thread_id, ws_id),
    )
    transport_env.execute(
        """
        INSERT INTO consult_answers (
            id, attempt_id, answer_text, answer_sha256, model_id, created_at
        ) VALUES (
            'ans-inj-1', 'consult-inj-1', ?, ?, 'claude-opus-5-5', '2026-01-01T00:00:05Z'
        )
        """,
        (long_answer, long_sha),
    )
    transport_env.commit()

    # Call consult_untrusted_blocks for turn 2 in that thread
    injected_blocks = consult_untrusted_blocks(thread_id, transport_env)
    assert len(injected_blocks) == 1
    assert injected_blocks[0]["source"] == "jarvis:untrusted-consult-answer"
    content = injected_blocks[0]["content"]
    assert "consult-inj-1" in content
    assert "Untrusted data: Expert consult answer" in content
    assert len(content) <= 6500  # Bounded to <= 6000 preview + header template


def test_build_consult_manifest_uses_sandbox_mcp_config_path(tmp_path: Path) -> None:
    """--mcp-config must point to the in-sandbox bind path, never the host path."""
    gateway_config = {
        "enabled": True,
        "agents": {"claude": {"egress": ["api.anthropic.com"], "command": "claude"}},
    }
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    context_dir = tmp_path / "ctx"
    runtime_root = tmp_path / "run"
    host_cfg = tmp_path / "host-only-mcp-config.json"
    host_cfg.write_text('{"mcpServers":{}}', encoding="utf-8")

    manifest = build_consult_manifest(
        gateway_config,
        "claude",
        workspace,
        home,
        context_dir,
        runtime_root,
        mcp_config_path=host_cfg,
    )
    args = manifest["agents"]["claude"]["sandbox_args"]
    idx = args.index("--mcp-config")
    assert args[idx + 1] == "/run/jarvis/mcp-config.json"
    assert str(host_cfg) not in args


def test_execute_web_search_refuses_non_loopback_proxy() -> None:
    """execute_web_search refuses non-127.0.0.1 search_guard_url fail-closed."""
    from app.modules.consult.mcp_server import execute_web_search

    res = execute_web_search("apple orchard soil", search_guard_url="https://example.com/search")
    assert res["error"] == "search_proxy_invalid_url"
    assert res["count"] == 0


def test_run_ai_task_local_route_allows_s2_while_external_blocks(transport_env: sqlite3.Connection) -> None:
    """Local routes (zone B) may process S2/S3 content; non-consult external routes still refuse it."""
    from app.modules.ai.execution import run_ai_task
    from app.modules.ai.execution_types import ProviderBinding

    s2_prompt = "This is confidential partner-only research documentation."
    local_outcome = run_ai_task(user_prompt=s2_prompt, task_kind="chat", route_class="local:fast")
    assert local_outcome.error_type != "egress_level_violation"
    assert local_outcome.decision.blocked_reason != "egress_level_violation"

    ext_binding = ProviderBinding(
        route_class="external:test",
        provider_id="scaleway",
        model_id="test-model",
        requires_network=False,
        max_output_tokens=1024,
        execution_class="external_provider",
    )
    ext_outcome = run_ai_task(
        user_prompt=s2_prompt,
        task_kind="chat",
        route_class="external:test",
        bindings={"external:test": ext_binding},
    )
    assert ext_outcome.decision.blocked is True
    assert ext_outcome.decision.blocked_reason == "egress_level_violation"

    # Also verify non-network external_provider blocks S2 in context_blocks
    ctx_outcome = run_ai_task(
        user_prompt="Benign prompt",
        task_kind="chat",
        route_class="external:test",
        context_blocks=[{"source": "note", "content": s2_prompt}],
        bindings={"external:test": ext_binding},
    )
    assert ctx_outcome.decision.blocked is True
    assert ctx_outcome.decision.blocked_reason == "egress_level_violation"


def test_mcp_server_malformed_params_and_encoded_url(tmp_path: Path) -> None:
    """Malformed numeric params return JSON-RPC -32602; percent-encoded S2 URLs are blocked."""
    server = ConsultMcpServer(
        str(tmp_path / "mcp.sock"),
        browser_runner=lambda cmd, timeout: (0, "ok", ""),
        dns_resolver=lambda host: ["93.184.216.34"],
    )
    bad_count = server.handle_request({
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {"name": "web_search", "arguments": {"query": "botany", "count": "not-an-int"}},
    })
    assert bad_count is not None
    assert bad_count["error"]["code"] == -32602

    encoded_url = server.handle_request({
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {"name": "read_page", "arguments": {"url": "https://example.com/?q=confidential%20partner%20research"}},
    })
    assert encoded_url is not None
    assert encoded_url["result"]["isError"] is True
    assert "search_guard_blocked" in encoded_url["result"]["content"][0]["text"]


