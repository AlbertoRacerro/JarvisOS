"""Spec 383: Objective consult triggers and maintainer feedback log.

Acceptance criteria:
1. Pure deterministic triggers:
   - explicit_user_request
   - plan_label
   - numeric_disagreement (with Pint unit conversion)
   - solver_double_failure
   Does not fire on non-matching or light turns.
2. Harness nudge in supervisor:
   - Only on eligible local work turns when consults are available.
   - At most one nudge per trigger code per turn.
   - hermes.consult_trigger event recorded.
   - trigger_codes recorded in consult_attempts.gate_steps_json.
3. Feedback API and persistence:
   - POST /ai/consult/{consult_id}/feedback records accepted/rejected in consult_feedback.
   - Rejects unknown, pending, cross-workspace consults.
   - GET /ai/consult/{consult_id}/feedback reads recorded verdict.
   - render_consult_blocks exposes feedback_verdict.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import packs, vault
from app.core.database import initialize_database, open_sqlite_connection
from app.core.vault import VAULT_PATH_ENV
from app.main import create_app
from app.modules.agents.hermes.turn_plan_tool import dispatch_turn_plan_tool
from app.modules.ai.agent_contracts import AgentSessionRef, StructuredToolCall
from app.modules.consult.service import ConsultRequest, request_consult
from app.modules.consult.triggers import (
    evaluate_consult_triggers,
)
from tests.test_consult_380_flow import BRIEF, DIGEST, FakeTransport


def _call(capability: str, arguments: dict, workspace_id: str = "ws-1") -> StructuredToolCall:
    now = datetime.now(UTC)
    return StructuredToolCall(
        call_id="call-383",
        capability_id=capability,
        grant_id="session-turn",
        correlation_id="corr-383",
        arguments=arguments,
        session_ref=AgentSessionRef(
            jarvis_thread_id="thread-383",
            hermes_session_id="hermes-383",
            profile_id="default",
            workspace_id=workspace_id,
            generation=1,
            upstream_revision="test",
        ),
        requested_at=now,
        deadline_at=now + timedelta(minutes=1),
    )


@pytest.fixture(autouse=True)
def _offline_env(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path_factory.mktemp("empty_vault")
    (empty / "jarvis_vault.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv(VAULT_PATH_ENV, str(empty))
    monkeypatch.setenv("JARVISOS_CONSULT_FAKE_TRANSPORT", "1")
    vault._load.cache_clear()
    initialize_database()


@pytest.fixture()
def consult_env(isolated_data_root: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "brief.md").write_text(BRIEF, encoding="utf-8")
    (pack / "jarvis_pack.toml").write_text(
        'id = "orchard"\n[consult]\nbrief = "brief.md"\nnucleus_heading = "Nucleus"\n', encoding="utf-8"
    )
    v_dir = tmp_path / "vault"
    v_dir.mkdir()
    (v_dir / "jarvis_vault.toml").write_text("", encoding="utf-8")
    (v_dir / "consult.toml").write_text(
        f'consult_mode = "ask"\nbrief_sha256 = "{DIGEST}"\n[[trusted_accounts]]\nid = "sub"\nagent = "claude"\n'
        'model = "fake-consultant"\ntraining_off_verified_on = 2026-11-25\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(packs.PACK_PATH_ENV, str(pack))
    monkeypatch.setenv(VAULT_PATH_ENV, str(v_dir))
    packs._load.cache_clear()
    vault._load.cache_clear()
    initialize_database()
    yield v_dir
    packs._load.cache_clear()
    vault._load.cache_clear()


# ==============================================================================
# Trigger Rule 1: explicit_user_request
# ==============================================================================

def test_explicit_user_request_phrases() -> None:
    # Italian phrase
    res = evaluate_consult_triggers(prompt="Potresti per favore chiedi a opus cosa ne pensa?")
    assert "explicit_user_request" in res.trigger_codes
    assert res.recommended_kind == "ideas"

    # English phrase
    res_en = evaluate_consult_triggers(prompt="Please ask opus how to optimize this condenser.")
    assert "explicit_user_request" in res_en.trigger_codes

    # "chiedi al consulente"
    res_it2 = evaluate_consult_triggers(prompt="Consiglio: chiedi al consulente per questa geometria")
    assert "explicit_user_request" in res_it2.trigger_codes

    # "consult the expert"
    res_en2 = evaluate_consult_triggers(prompt="Let's consult the expert on boundary layer separation.")
    assert "explicit_user_request" in res_en2.trigger_codes

    # Kind specialization from prompt keywords
    res_phys = evaluate_consult_triggers(prompt="chiedi a opus per la fisica del plasma")
    assert res_phys.recommended_kind == "physics"

    res_lit = evaluate_consult_triggers(prompt="ask opus for literature on microalgae yields")
    assert res_lit.recommended_kind == "literature"

    res_crit = evaluate_consult_triggers(prompt="chiedi al consulente una critica su questa stima")
    assert res_crit.recommended_kind == "critique"


def test_explicit_user_request_non_matching() -> None:
    assert not evaluate_consult_triggers(prompt="What is the Reynolds number for water at 20C?").trigger_codes
    assert not evaluate_consult_triggers(prompt="Opus 3 is a large language model.").trigger_codes
    assert not evaluate_consult_triggers(prompt="Il consulente lavora in azienda.").trigger_codes


# ==============================================================================
# Trigger Rule 2: plan_label
# ==============================================================================

def test_plan_label_triggers() -> None:
    res_creative = evaluate_consult_triggers(active_plan={"task_label": "creative"})
    assert "plan_label" in res_creative.trigger_codes
    assert res_creative.recommended_kind == "ideas"

    res_physics = evaluate_consult_triggers(active_plan={"task_label": "physics"})
    assert "plan_label" in res_physics.trigger_codes
    assert res_physics.recommended_kind == "physics"

    res_lit = evaluate_consult_triggers(active_plan={"task_label": "literature"})
    assert "plan_label" in res_lit.trigger_codes
    assert res_lit.recommended_kind == "literature"

    # Non-firing task labels
    assert not evaluate_consult_triggers(active_plan={"task_label": "engineering"}).trigger_codes
    assert not evaluate_consult_triggers(active_plan={"task_label": "other"}).trigger_codes
    assert not evaluate_consult_triggers(active_plan={}).trigger_codes


def test_turn_plan_tool_task_label_lifecycle() -> None:
    plan: dict = {}
    call = _call("jarvis.turn_plan", {
        "op": "set_plan",
        "task_label": "creative",
        "steps": [{"id": "s1", "title": "Explore alternatives", "status": "pending"}],
    })
    res, err = dispatch_turn_plan_tool(call, "ws-1", "", active_plan=plan)
    assert err == ""
    assert res is not None
    assert res["status"] == "succeeded"
    assert res["task_label"] == "creative"
    assert plan["task_label"] == "creative"

    # read_plan returns task_label
    call_read = _call("jarvis.turn_plan", {"op": "read_plan"})
    read_res, _ = dispatch_turn_plan_tool(call_read, "ws-1", "", active_plan=plan)
    assert read_res is not None
    assert read_res["task_label"] == "creative"

    # Invalid task label is rejected
    call_invalid = _call("jarvis.turn_plan", {
        "op": "set_plan",
        "task_label": "unsupported_label",
        "steps": [{"id": "s1", "title": "Step", "status": "pending"}],
    })
    res_inv, err_inv = dispatch_turn_plan_tool(call_invalid, "ws-1", "", active_plan=plan)
    assert res_inv is None
    assert err_inv == "invalid_task_label"


# ==============================================================================
# Trigger Rule 3: numeric_disagreement (with Pint)
# ==============================================================================

def test_numeric_disagreement_same_unit() -> None:
    outputs = [
        {"pressure_drop": "200 Pa"},
        {"pressure_drop": "250 Pa"},
    ]
    # |200 - 250| / 250 = 0.20 (20% > 15%)
    res = evaluate_consult_triggers(turn_tool_outputs=outputs)
    assert "numeric_disagreement" in res.trigger_codes
    assert res.recommended_kind == "critique"


def test_numeric_disagreement_with_unit_conversion() -> None:
    # 1.0 bar = 100,000 Pa; 75 kPa = 75,000 Pa -> 25% relative difference
    outputs = [
        {"name": "operating_pressure", "value": 1.0, "unit": "bar"},
        {"name": "operating_pressure", "value": 75.0, "unit": "kPa"},
    ]
    res = evaluate_consult_triggers(turn_tool_outputs=outputs)
    assert "numeric_disagreement" in res.trigger_codes
    assert res.recommended_kind == "critique"


def test_numeric_disagreement_within_threshold_does_not_fire() -> None:
    # 100 Pa vs 105 Pa -> 4.7% diff <= 15%
    outputs = [
        {"pressure_drop": "100 Pa"},
        {"pressure_drop": "105 Pa"},
    ]
    res = evaluate_consult_triggers(turn_tool_outputs=outputs)
    assert "numeric_disagreement" not in res.trigger_codes


def test_numeric_disagreement_unrelated_quantities_do_not_fire() -> None:
    outputs = [
        {"pressure_drop": "200 Pa"},
        {"shear_stress": "400 Pa"},
    ]
    res = evaluate_consult_triggers(turn_tool_outputs=outputs)
    assert "numeric_disagreement" not in res.trigger_codes


# ==============================================================================
# Trigger Rule 4: solver_double_failure
# ==============================================================================

def test_solver_double_failure_triggers() -> None:
    # Single failure does not fire
    res1 = evaluate_consult_triggers(solver_failures={"jarvis_python": 1})
    assert "solver_double_failure" not in res1.trigger_codes

    # Double failure fires
    res2 = evaluate_consult_triggers(solver_failures={"jarvis_python": 2})
    assert "solver_double_failure" in res2.trigger_codes
    assert res2.recommended_kind == "physics"

    # bench run solver
    res_bench = evaluate_consult_triggers(solver_failures={"jarvis_bench_run": 2})
    assert "solver_double_failure" in res_bench.trigger_codes
    assert res_bench.recommended_kind == "physics"

    # list of failed tools
    res_list = evaluate_consult_triggers(tool_failures=["jarvis_python", "jarvis_python"])
    assert "solver_double_failure" in res_list.trigger_codes


def test_solver_double_failure_non_solver_tools() -> None:
    # Non-solver tools failing twice do not fire solver_double_failure
    res = evaluate_consult_triggers(tool_failures=["jarvis_paper_search", "jarvis_paper_search"])
    assert "solver_double_failure" not in res.trigger_codes


# ==============================================================================
# Light Turn & Off Mode Guards
# ==============================================================================

def test_light_turns_emit_no_trigger() -> None:
    res = evaluate_consult_triggers(
        prompt="chiedi a opus",
        active_plan={"task_label": "physics"},
        turn_tool_outputs=[{"p": "100 Pa"}, {"p": "200 Pa"}],
        solver_failures={"jarvis_python": 2},
        light=True,
    )
    assert not res.trigger_codes
    assert res.recommended_kind is None


# ==============================================================================
# Gate Steps Persistence & Ledger
# ==============================================================================

def test_trigger_codes_persisted_in_consult_attempts(consult_env: Path) -> None:
    with open_sqlite_connection() as conn:
        req = ConsultRequest(
            kind="ideas",
            question="What is the optimal root layout?",
            modules=["M1"],
            turn_id="turn-trigger-test",
            workspace_id="ws-1",
            trigger_codes=["explicit_user_request", "plan_label"],
        )
        outcome = request_consult(conn, req, transport=FakeTransport())
        conn.commit()

        row = conn.execute(
            "SELECT gate_steps_json FROM consult_attempts WHERE id = ?",
            (outcome.consult_id,),
        ).fetchone()
        assert row is not None
        steps = json.loads(row[0])
        tr_step = next((s for s in steps if s.get("step") == "triggers"), None)
        assert tr_step is not None
        assert tr_step["trigger_codes"] == ["explicit_user_request", "plan_label"]


def test_trigger_codes_recovered_from_events_fallback(consult_env: Path) -> None:
    from app.modules.events.service import log_event

    with open_sqlite_connection() as conn:
        log_event(
            conn,
            event_type="hermes.consult_trigger",
            actor="jarvis",
            target_type="ai_thread_interaction",
            target_id="turn-event-fallback",
            workspace_id=None,
            payload={"trigger_code": "plan_label", "recommended_kind": "physics"},
        )
        log_event(
            conn,
            event_type="hermes.consult_trigger",
            actor="jarvis",
            target_type="ai_thread_interaction",
            target_id="turn-event-fallback",
            workspace_id=None,
            payload={"trigger_code": "solver_double_failure", "recommended_kind": "physics"},
        )
        conn.commit()

        req = ConsultRequest(
            kind="physics",
            question="Why does this heat balance diverge?",
            modules=["M1"],
            turn_id="turn-event-fallback",
            workspace_id="ws-1",
            trigger_codes=[],
        )
        outcome = request_consult(conn, req, transport=FakeTransport())
        conn.commit()

        row = conn.execute(
            "SELECT gate_steps_json FROM consult_attempts WHERE id = ?",
            (outcome.consult_id,),
        ).fetchone()
        assert row is not None
        steps = json.loads(row[0])
        tr_step = next((s for s in steps if s.get("step") == "triggers"), None)
        assert tr_step is not None
        assert tr_step["trigger_codes"] == ["plan_label", "solver_double_failure"]


# ==============================================================================
# Feedback Route (POST & GET) and Persistence
# ==============================================================================

def test_feedback_route_acceptance_and_rejections(consult_env: Path) -> None:
    client = TestClient(create_app())

    # Create a sent consult
    with open_sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO consult_attempts (
                id, turn_id, thread_id, workspace_id, kind, modules_json, brief_sha256,
                packet_text, packet_sha256, account_id, model_id, state, mode, gate_steps_json,
                audit, created_at, updated_at
            ) VALUES (
                'c-test-sent-1', 'turn-1', 'thread-1', 'ws-test', 'physics', '["M1"]',
                'sha-brief', 'Question: test', 'sha-packet', 'sub', 'fake-model', 'sent', 'ask',
                '[{"step": "triggers", "trigger_codes": ["solver_double_failure"]}]',
                0, '2026-01-01T10:00:00', '2026-01-01T10:00:05'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO consult_attempts (
                id, turn_id, thread_id, workspace_id, kind, modules_json, brief_sha256,
                packet_text, packet_sha256, account_id, model_id, state, mode, gate_steps_json,
                audit, created_at, updated_at
            ) VALUES (
                'c-test-pending-1', 'turn-1', 'thread-1', 'ws-test', 'physics', '["M1"]',
                'sha-brief', 'Question: test', 'sha-packet', 'sub', 'fake-model', 'awaiting_approval', 'ask',
                '[]', 0, '2026-01-01T10:00:00', '2026-01-01T10:00:05'
            )
            """
        )
        conn.commit()

    # 1. Successful accepted feedback
    res_acc = client.post(
        "/ai/consult/c-test-sent-1/feedback",
        json={"verdict": "accepted", "note": "Very clear physical derivation", "workspace_id": "ws-test"},
    )
    assert res_acc.status_code == 200
    body = res_acc.json()
    assert body["status"] == "ok"
    assert body["verdict"] == "accepted"
    assert body["trigger_codes"] == ["solver_double_failure"]

    # Read back via GET
    res_get = client.get("/ai/consult/c-test-sent-1/feedback")
    assert res_get.status_code == 200
    get_body = res_get.json()
    assert get_body["status"] == "ok"
    assert get_body["verdict"] == "accepted"
    assert get_body["note"] == "Very clear physical derivation"
    assert get_body["trigger_codes"] == ["solver_double_failure"]

    # Verify consult_feedback table directly
    with open_sqlite_connection() as conn:
        fb_row = conn.execute(
            "SELECT verdict, note, trigger_codes_json FROM consult_feedback WHERE attempt_id = 'c-test-sent-1'",
        ).fetchone()
        assert fb_row is not None
        assert fb_row[0] == "accepted"
        assert fb_row[1] == "Very clear physical derivation"
        assert json.loads(fb_row[2]) == ["solver_double_failure"]

    # 2. Reject pending consult
    res_pen = client.post(
        "/ai/consult/c-test-pending-1/feedback",
        json={"verdict": "accepted", "workspace_id": "ws-test"},
    )
    assert res_pen.status_code == 400
    assert "consult_not_sent" in res_pen.text

    # 3. Reject unknown consult
    res_unk = client.post(
        "/ai/consult/c-nonexistent/feedback",
        json={"verdict": "accepted"},
    )
    assert res_unk.status_code == 404
    assert "consult_unknown" in res_unk.text

    # 4. Reject cross-workspace consult
    res_cross = client.post(
        "/ai/consult/c-test-sent-1/feedback",
        json={"verdict": "rejected", "workspace_id": "ws-other"},
    )
    assert res_cross.status_code == 400
    assert "cross_workspace" in res_cross.text


def test_render_consult_blocks_feedback_verdict(consult_env: Path) -> None:
    from app.modules.consult.render import render_consult_blocks

    with open_sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO consult_attempts (
                id, turn_id, thread_id, workspace_id, kind, modules_json, brief_sha256,
                packet_text, packet_sha256, account_id, model_id, state, mode, gate_steps_json,
                audit, created_at, updated_at
            ) VALUES (
                'c-render-1', 'turn-r1', 'thread-1', 'ws-test', 'physics', '["M1"]',
                'sha-brief', 'Question: test', 'sha-packet', 'sub', 'fake-model', 'sent', 'ask',
                '[]', 0, '2026-01-01T10:00:00', '2026-01-01T10:00:05'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO consult_feedback (id, attempt_id, verdict, note, trigger_codes_json, created_at)
            VALUES ('cf-1', 'c-render-1', 'accepted', 'Solid', '[]', '2026-01-01T10:01:00')
            """
        )
        conn.commit()

        blocks = render_consult_blocks(conn, "turn-r1", "Assistant says: {{consult:c-render-1}}")
        assert len(blocks) == 1
        assert blocks[0].feedback_verdict == "accepted"


def test_supervisor_relay_nudge_injection_and_single_cap(consult_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.agents.hermes.roles as roles_mod
    from app.modules.agents.hermes.supervisor import HermesSupervisor

    with open_sqlite_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO workspaces (id, name, created_at) VALUES ('ws-1', 'Test WS', '2026-01-01')")
        conn.commit()

    monkeypatch.setattr(roles_mod, "require_local_role_route", lambda r: None)
    monkeypatch.setattr("app.modules.agents.hermes.supervisor.run_governed_inference", lambda *a, **k: {"status": "success", "answer": "ok"})

    sup = HermesSupervisor(sys.executable, route_for_task=lambda task: "local:fake")
    sup.session = AgentSessionRef(
        jarvis_thread_id="t-1",
        hermes_session_id="h-1",
        profile_id="default",
        workspace_id="ws-1",
        generation=1,
        upstream_revision="test",
    )
    sup.active_interaction_id = "turn-nudge-1"
    sup.active_turn_prompt = "chiedi a opus per favore"
    sup.turn_light = False

    sent_frames: list = []
    sup._send = lambda frame: sent_frames.append(frame)

    frame = {
        "id": "req-1",
        "task_kind": "general",
        "messages": [{"role": "user", "content": "chiedi a opus per favore"}],
    }

    # First relay step: nudge should be injected
    sup._handle_relay(frame)
    assert len(sup.turn_nudged_triggers) == 1
    assert "explicit_user_request" in sup.turn_nudged_triggers
    assert any("[Harness consult hint:" in m.get("content", "") for m in frame["messages"])

    # Second relay step in same turn: nudge should NOT be injected again
    frame2 = {
        "id": "req-2",
        "task_kind": "general",
        "messages": [{"role": "user", "content": "next step"}],
    }
    sup._handle_relay(frame2)
    assert not any("[Harness consult hint:" in m.get("content", "") for m in frame2["messages"])


def test_supervisor_no_nudge_when_consult_mode_off(consult_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.agents.hermes.supervisor import HermesSupervisor
    from app.modules.consult.config import ConsultConfig

    monkeypatch.setattr(
        "app.modules.consult.config.load_consult_config",
        lambda: ConsultConfig(mode="off", reason="consult_disabled", brief_sha256="", trusted_accounts=[], sage_model_id=None),
    )

    sup = HermesSupervisor(sys.executable, route_for_task=lambda task: "local:fake")
    sup.session = AgentSessionRef(
        jarvis_thread_id="t-1",
        hermes_session_id="h-1",
        profile_id="default",
        workspace_id="ws-1",
        generation=1,
        upstream_revision="test",
    )
    sup.active_interaction_id = "turn-off-1"
    sup.active_turn_prompt = "chiedi a opus per favore"
    sup.turn_light = False
    sup._send = lambda frame: None

    frame = {
        "id": "req-off",
        "task_kind": "general",
        "messages": [{"role": "user", "content": "chiedi a opus per favore"}],
    }
    sup._handle_relay(frame)
    assert len(sup.turn_nudged_triggers) == 0
    assert not any("[Harness consult hint:" in m.get("content", "") for m in frame["messages"])


def test_supervisor_no_nudge_on_light_turn(consult_env: Path) -> None:
    from app.modules.agents.hermes.supervisor import HermesSupervisor

    sup = HermesSupervisor(sys.executable, route_for_task=lambda task: "local:fake")
    sup.session = AgentSessionRef(
        jarvis_thread_id="t-1",
        hermes_session_id="h-1",
        profile_id="default",
        workspace_id="ws-1",
        generation=1,
        upstream_revision="test",
    )
    sup.active_interaction_id = "turn-light-1"
    sup.active_turn_prompt = "chiedi a opus per favore"
    sup.turn_light = True
    sup._send = lambda frame: None

    frame = {
        "id": "req-light",
        "task_kind": "general",
        "messages": [{"role": "user", "content": "chiedi a opus per favore"}],
    }
    sup._handle_relay(frame)
    assert len(sup.turn_nudged_triggers) == 0
    assert not any("[Harness consult hint:" in m.get("content", "") for m in frame["messages"])

