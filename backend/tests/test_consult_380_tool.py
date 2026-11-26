"""Spec 380 part a: Hermes broker consult tools (jarvis_consult and jarvis_consult_read)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core import packs
from app.core.database import initialize_database
from app.core.vault import VAULT_PATH_ENV
from app.modules.agents.hermes import consult_tool
from app.modules.agents.hermes.supervisor import dispatch_tool
from app.modules.ai.agent_contracts import AgentSessionRef, StructuredToolCall
from tests.test_consult_380_flow import BRIEF, DIGEST, FakeTransport


def _call(capability: str, arguments: dict, workspace_id: str = "ws-1") -> StructuredToolCall:
    now = datetime.now(UTC)
    return StructuredToolCall(
        call_id="call-1",
        capability_id=capability,
        grant_id="session-turn",
        correlation_id="corr-1",
        arguments=arguments,
        session_ref=AgentSessionRef(
            jarvis_thread_id="thread-1",
            hermes_session_id="hermes-1",
            profile_id="default",
            workspace_id=workspace_id,
            generation=1,
            upstream_revision="test",
        ),
        requested_at=now,
        deadline_at=now + timedelta(minutes=1),
    )


@pytest.fixture()
def consult_env(isolated_data_root: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "brief.md").write_text(BRIEF, encoding="utf-8")
    (pack / "jarvis_pack.toml").write_text(
        'id = "orchard"\n[consult]\nbrief = "brief.md"\nnucleus_heading = "Nucleus"\n', encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "jarvis_vault.toml").write_text("", encoding="utf-8")
    (vault / "consult.toml").write_text(
        f'consult_mode = "ask"\nbrief_sha256 = "{DIGEST}"\n[[trusted_accounts]]\nid = "sub"\nagent = "claude"\n'
        'model = "fake-consultant"\ntraining_off_verified_on = 2026-11-25\n', encoding="utf-8")
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack))
    monkeypatch.setenv(VAULT_PATH_ENV, str(vault))
    packs._load.cache_clear()
    initialize_database()
    yield vault
    packs._load.cache_clear()


def test_default_transport_none_refuses_transport_unavailable(consult_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(consult_tool, "TRANSPORT", None)
    result = dispatch_tool(
        _call("jarvis.consult", {"kind": "physics", "question": "Why do roots grow down?", "modules": ["M1"]}),
        live_grants={},
        interaction_id="turn-1",
    )
    assert result.status == "succeeded" and result.result is not None
    assert result.result["status"] == "refused" and result.result["code"] == "transport_unavailable"


def test_ask_mode_and_turn_tool_output_taint_through_dispatch_tool(
    consult_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport()
    monkeypatch.setattr(consult_tool, "TRANSPORT", transport)
    tainted = dispatch_tool(
        _call("jarvis.consult", {"kind": "physics", "question": "Is 240 Pa plausible?", "modules": ["M1"]}),
        live_grants={},
        interaction_id="turn-2",
        turn_tool_outputs=[{"pressure_drop": "236.8 Pa"}],
    )
    assert tainted.status == "succeeded" and tainted.result is not None
    assert tainted.result["status"] == "rewrite_required" and tainted.result["offending_numbers"] == ["240"]

    pending = dispatch_tool(
        _call("jarvis.consult", {"kind": "physics", "question": "Is 100-300 Pa plausible?", "modules": ["M1"]}),
        live_grants={},
        interaction_id="turn-2",
        turn_tool_outputs=[{"pressure_drop": "236.8 Pa"}],
    )
    assert pending.status == "succeeded" and pending.result is not None
    assert pending.result["status"] == "pending"
    assert f"{{{{consult:{pending.result['consult_id']}}}}}" in pending.result["reason"]
    assert transport.sent == []


def test_auto_mode_returns_untrusted_preview_and_read_tool_respects_workspace(
    consult_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (consult_env / "consult.toml").write_text(
        f'consult_mode = "auto"\nbrief_sha256 = "{DIGEST}"\n[[trusted_accounts]]\nid = "sub"\nagent = "claude"\n'
        'model = "fake-consultant"\ntraining_off_verified_on = 2026-11-25\n', encoding="utf-8")
    long_answer = "A" * 6500
    monkeypatch.setattr(consult_tool, "TRANSPORT", FakeTransport(answer=long_answer))
    sent = dispatch_tool(
        _call("jarvis.consult", {"kind": "ideas", "question": "How to improve aeration?", "modules": ["M2"]}),
        live_grants={},
        interaction_id="turn-3",
    )
    assert sent.status == "succeeded" and sent.result is not None
    assert sent.result["status"] == "answered"
    answer = sent.result["answer"]
    assert answer["untrusted_data"] is True and answer["truncated"] is True and len(answer["text"]) == 6000
    cid = sent.result["consult_id"]

    chunk = dispatch_tool(_call("jarvis.consult_read", {"id": cid, "offset": 6000}), live_grants={})
    assert chunk.status == "succeeded" and chunk.result is not None
    assert chunk.result["status"] == "ok" and chunk.result["text"] == "A" * 500 and chunk.result["next_offset"] is None

    other_ws = dispatch_tool(_call("jarvis.consult_read", {"id": cid, "offset": 0}, workspace_id="ws-other"),
                             live_grants={})
    assert other_ws.status == "succeeded" and other_ws.result is not None
    assert other_ws.result["status"] == "not_found"


def test_local_only_and_invalid_arguments(consult_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(consult_tool, "TRANSPORT", FakeTransport())
    local = dispatch_tool(
        _call("jarvis.consult", {"kind": "physics", "question": "Why?", "local_only": True}),
        live_grants={},
        interaction_id="turn-4",
    )
    assert local.status == "succeeded" and local.result is not None
    assert local.result["status"] == "sage_unavailable"

    bad = dispatch_tool(
        _call("jarvis.consult", {"kind": "physics", "question": "Why?", "attachment": "/tmp/x.step"}),
        live_grants={},
        interaction_id="turn-4",
    )
    assert bad.status == "refused" and bad.error_code == "invalid_arguments"
