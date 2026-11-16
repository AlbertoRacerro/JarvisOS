"""Spec 279: Supervisor waits, outages and question checks settle without loops.

Acceptance criteria tests for points 1-4.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from test_relay_continuity import (
    SUCCESS,
    T0,
    Env,
    claude_result,
    mission_def,
    rc,
    write_turn,
)


def test_point_1_three_passes_with_failing_probe_leave_counters_and_cooldowns_unchanged(tmp_path: Path) -> None:
    """Acceptance criterion 2: Three passes whose launch probe fails leave counters and
    cooldowns as they were and switch no agent; a reachable pass after them launches the same agent.
    """
    env = Env(tmp_path, [("claude", SUCCESS)])
    report = rc.TurnReport(
        outcome="transient",
        detail="network timeout",
        harness_cause="provider_network",
        agent="claude",
        turn_id="s1/turn-001",
    )

    sup = env.supervisor()
    sup.state["head_session"] = "s1"
    sup.state["agent"] = "claude"
    sup.state["permissions_digest"] = env.perm["v"]

    # Reachability probe fails for 3 consecutive supervisor passes
    env.host.reachable = lambda: False

    for _ in range(3):
        decision = sup._decide(report, None)
        assert decision == "waiting" or (isinstance(decision, tuple) and sup._launch(decision[0], decision[1], report) == "waiting")

    # Counters and cooldowns must remain as they were (no strikes recorded, no cooldown set)
    assert sup.state["counters"].get("transient_claude", 0) == 0
    assert "claude" not in sup.state["cooldowns"]

    # Fourth pass: network recovers, probe succeeds -> launches the same agent (claude)
    env.host.reachable = lambda: True
    decision = sup._decide(report, None)
    assert isinstance(decision, tuple)
    agent, prompt = decision
    assert agent == "claude"
    res = sup._launch(agent, prompt, report)
    assert res != "waiting"
    assert len(env.relay.launches) == 1
    assert env.relay.launches[0]["agent"] == "claude"
    # Strike is recorded only upon real launch
    assert sup.state["counters"].get("transient_claude", 0) == 1


# Sanitized fixture copied from the live owner's continuity state.
REAL_OWNER_STATE_SHAPE: dict[str, Any] = {
    "acknowledged": None,
    "agent": "claude",
    "chain": ["20261018-215555-4f193a"],
    "cooldowns": {},
    "counters": {
        "consecutive_failures": 0,
        "crash_retries": 0,
        "transient_streak": 0,
    },
    "head_session": "20261018-215555-4f193a",
    "hold": None,
    "last_attempt": None,
    "last_checkpoint_at": None,
    "last_outcome": "success",
    "last_switch_at": None,
    "notify": {
        "dropped": 0,
        "failures": 0,
        "last_digest": None,
        "retry": None,
        "sent": {},
        "window": [],
    },
    "parked_turns": [],
    "pending": None,
    "permissions_digest": "perm0",
    "permissions_pinned": True,
    "phase": "waiting",
    "resumes": 0,
    "schema": "relay-continuity.v1",
    "spend_caps": {},
    "switches": [],
    "updated_at": "2026-11-03T17:52:18Z",
    "wait": {
        "jobs": ["job-real-001"],
        "ci": [],
        "started_at": "2026-10-18T08:00:00Z",
        "last_job_poll": None,
        "last_ci_poll": None,
        "turn": "20261018-215555-4f193a/turn-001",
    },
    "waiting_since": None,
    "watch": {
        "master": None,
        "seen": {},
    },
}


def test_point_2_wake_waiting_reuses_woken_wait_on_next_pass(tmp_path: Path) -> None:
    """Acceptance criterion 3: A wake whose launch returns 'waiting' and a following pass that
    launches produce one launch with the original wake prompt, one change of resumes, and the
    original started_at.
    """
    reply_events = [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "RELAY_STATUS continue\nJOB-WAIT: job-real-001"}],
                "usage": {"input_tokens": 5000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            },
        }
    ]
    env = Env(tmp_path, [("claude", SUCCESS)])
    sid = "20261018-215555-4f193a"
    write_turn(env.relay.sessions_dir() / sid, reply_events, agent="claude", status="continue")
    env.jobs["job-real-001"] = {"status": "succeeded"}

    sup = env.supervisor()
    sup.state = dict(REAL_OWNER_STATE_SHAPE)
    sup.state["permissions_digest"] = env.perm["v"]
    orig_started = REAL_OWNER_STATE_SHAPE["wait"]["started_at"]
    sup.save()

    # Pass 1: reachability probe fails -> _launch returns 'waiting'
    env.host.reachable = lambda: False
    res1 = sup._loop(None)
    assert res1 == "waiting"

    # Woken wait must be preserved, not wiped, with original started_at and marked woken
    woken_wait = sup.state.get("wait")
    assert woken_wait is not None, "wait record must be kept when launch returns 'waiting'"
    assert woken_wait.get("woken") is True
    assert woken_wait.get("started_at") == orig_started
    orig_wake_prompt = woken_wait.get("wake_prompt")
    assert orig_wake_prompt is not None and "job-real-001" in orig_wake_prompt
    resumes_after_wake = sup.state.get("resumes")
    assert resumes_after_wake == 1

    # Pass 2: clock advances 5 minutes, network recovers -> launches successfully
    env.clock.sleep(300)
    env.host.reachable = lambda: True
    res2 = sup._loop(None)
    assert res2 != "waiting"
    assert len(env.relay.launches) == 1
    launch = env.relay.launches[0]
    assert launch["agent"] == "claude"
    # Prompt is the original wake prompt
    assert "job-real-001" in launch["task"]
    # Resumes changed only once across both passes
    assert sup.state.get("resumes") == resumes_after_wake
    # Wait is cleared only after pending was saved
    assert sup.state.get("wait") is None


# Sanitized fixture copied from a live mission's continuity state.
REAL_OWNER_MISSION_STATE_SHAPE: dict[str, Any] = {
    "acknowledged": None,
    "agent": "claude",
    "chain": ["20261113-152631-50e0ed"],
    "cooldowns": {},
    "counters": {
        "consecutive_failures": 0,
        "crash_retries": 0,
        "transient_streak": 0,
    },
    "head_session": "20261113-152631-50e0ed",
    "hold": None,
    "last_attempt": None,
    "last_checkpoint_at": None,
    "last_outcome": "success",
    "last_switch_at": None,
    "mission": {
        "id": "m-20261113T152626Z-cddd54",
        "next": 0,
        "total": 1,
        "done": [],
    },
    "notify": {
        "dropped": 0,
        "failures": 0,
        "last_digest": None,
        "retry": None,
        "sent": {},
        "window": [],
    },
    "parked_turns": [],
    "pending": None,
    "permissions_digest": "perm0",
    "permissions_pinned": True,
    "phase": "running",
    "resumes": 0,
    "schema": "relay-continuity.v1",
    "spend_caps": {},
    "switches": [],
    "updated_at": "2026-11-13T15:26:26Z",
    "wait": None,
    "waiting_since": None,
    "watch": {
        "master": None,
        "seen": {},
    },
}


def test_point_3_unregistered_job_wake_limit_holds_after_third_wake(tmp_path: Path) -> None:
    """Acceptance criterion 4: A coordinator that ends four turns in a row on a wait for an
    unregistered job is held after the third wake; a satisfied wake in between resets the count.
    """
    jid = "j-20261018T080000Z-999999"
    wait_msg = f"RELAY_STATUS continue\nJOB-WAIT: {jid}"
    unreg_reply = [claude_result(wait_msg)]
    script = [("claude", unreg_reply, "continue", wait_msg) for _ in range(6)]
    env = Env(tmp_path, script)
    m = mission_def("benchmark task", mid="m-20261113T152626Z-cddd54")
    rc.write_json_atomic(rc.missions_dir(env.store.root) / f"{m['id']}.json", m)

    # Initial turn 1 already happened in head_session
    sid = "20261113-152631-50e0ed"
    write_turn(env.relay.sessions_dir() / sid, unreg_reply, agent="claude", status="continue", last_message=wait_msg)

    sup = env.supervisor()
    sup.mission = m
    sup.state = json.loads(json.dumps(REAL_OWNER_MISSION_STATE_SHAPE))
    sup.state["permissions_digest"] = env.perm["v"]
    env.store.save(sup.state, T0)
    env.host.reachable = lambda: True

    # Turn 1: ends in wait for unreg-job -> Wake 1 launches Turn 2
    # Turn 2: ends in wait for unreg-job -> Wake 2 launches Turn 3
    # Turn 3: ends in wait for unreg-job -> Wake 3 launches Turn 4
    # Turn 4: ends in wait for unreg-job -> 4th turn ending on wait, after 3rd wake -> held with task_wait_limit!
    res = sup.run(sid, task=m["tasks"][0]["text"])

    assert res == "held"
    assert sup.state["hold"] is not None
    assert sup.state["hold"]["reason"] == "task_wait_limit"
    # Exactly 3 launches for turns 2, 3, 4 occurred (turn 1 was the initial session)
    assert len(env.relay.launches) == 3


def test_point_3_satisfied_wake_resets_unsatisfied_wake_count(tmp_path: Path) -> None:
    """A satisfied wake in between resets the unsatisfied wake count."""
    jid_unreg = "j-20261018T080000Z-999999"
    jid_sat = "j-20261018T080000Z-100100"
    wait_msg = f"RELAY_STATUS continue\nJOB-WAIT: {jid_unreg}"
    unreg_reply = [claude_result(wait_msg)]
    env = Env(tmp_path)
    env.jobs[jid_sat] = {"status": "succeeded"}
    m = mission_def("benchmark task", mid="m-20261113T152626Z-cddd54")
    rc.write_json_atomic(rc.missions_dir(env.store.root) / f"{m['id']}.json", m)

    sid = "20261113-152631-50e0ed"
    write_turn(env.relay.sessions_dir() / sid, unreg_reply, agent="claude", status="continue", last_message=wait_msg)
    turn_dir = env.relay.sessions_dir() / sid / "turns" / "turn-001"
    report = rc.classify_turn(turn_dir)

    sup = env.supervisor()
    sup.mission = m
    sup.state = json.loads(json.dumps(REAL_OWNER_MISSION_STATE_SHAPE))
    sup.state["permissions_digest"] = env.perm["v"]
    env.store.save(sup.state, T0)
    env.host.reachable = lambda: True

    # Wake 1: unsatisfied wake count becomes 1
    w1 = {"jobs": [jid_unreg], "ci": [], "started_at": rc.iso(T0), "turn": "s1/t1"}
    sup._wake_wait("unregistered_job", w1, report, {}, {}, failed_job=jid_unreg)
    assert sup.state["mission"].get("unsatisfied_wakes", {}).get("0") == 1

    # Wake 2: unsatisfied wake count becomes 2
    w2 = {"jobs": [jid_unreg], "ci": [], "started_at": rc.iso(T0), "turn": "s1/t2"}
    sup._wake_wait("unregistered_job", w2, report, {}, {}, failed_job=jid_unreg)
    assert sup.state["mission"].get("unsatisfied_wakes", {}).get("0") == 2

    # Satisfied wake resets count!
    w3 = {"jobs": [jid_sat], "ci": [], "started_at": rc.iso(T0), "turn": "s1/t3"}
    sup._wake_wait("satisfied", w3, report, {jid_sat: "succeeded"}, {})
    assert sup.state["mission"].get("unsatisfied_wakes", {}).get("0", 0) == 0

    # Wake 4 after reset starts count at 1
    w4 = {"jobs": [jid_unreg], "ci": [], "started_at": rc.iso(T0), "turn": "s1/t4"}
    res = sup._wake_wait("unregistered_job", w4, report, {}, {}, failed_job=jid_unreg)
    assert res != "held"
    assert sup.state["mission"].get("unsatisfied_wakes", {}).get("0") == 1


def test_point_4_wake_while_chosen_agent_cooling_down_waits_or_holds_and_launches_nothing(tmp_path: Path) -> None:
    """Acceptance criterion 5: A wake while the chosen agent is cooling down waits or holds
    and launches nothing.
    """
    jid = "j-20261018T080000Z-100100"
    wait_msg = f"RELAY_STATUS continue\nJOB-WAIT: {jid}"
    events = [claude_result(wait_msg)]
    env = Env(tmp_path, [("claude", events)])
    env.jobs[jid] = {"status": "succeeded"}

    sid = "s-wait-cooldown"
    write_turn(env.relay.sessions_dir() / sid, events, agent="claude", status="continue", last_message=wait_msg)
    turn_dir = env.relay.sessions_dir() / sid / "turns" / "turn-001"
    report = rc.classify_turn(turn_dir)

    sup = env.supervisor()
    # All agents on cooldown until T0 + 2 hours
    future = T0 + timedelta(hours=2)
    cooldowns = {a: {"reason": "quota", "since": rc.iso(T0), "source": "limits", "until": rc.iso(future)} for a in rc.AGENTS}
    sup.state.update({
        "head_session": sid,
        "agent": "claude",
        "permissions_digest": env.perm["v"],
        "cooldowns": cooldowns,
        "wait": {
            "jobs": [jid],
            "ci": [],
            "started_at": rc.iso(T0),
            "last_job_poll": None,
            "last_ci_poll": None,
            "turn": f"{sid}/turn-001",
        },
    })
    sup.save()
    env.host.reachable = lambda: True

    # Signal stop so _wait_or_hold returns without hanging the test clock
    sup.signalled = True

    wait = dict(sup.state["wait"])
    res = sup._wake_wait("satisfied", wait, report, {jid: "succeeded"}, {})

    # Must NOT launch claude while cooling down; must return signal/stopped or held
    assert res in ("signal", "stopped", "held")
    assert len(env.relay.launches) == 0





def test_point_3_wake_stopped_while_agents_cool_counts_once(tmp_path: Path) -> None:
    """A wake that launches nothing (every agent cooling, owner stopped) and is repeated on the same wait counts once."""
    jid = "j-20261018T080000Z-999999"
    wait_msg = f"RELAY_STATUS continue\nJOB-WAIT: {jid}"
    events = [claude_result(wait_msg)]
    env = Env(tmp_path)
    m = mission_def("benchmark task", mid="m-20261113T152626Z-cddd54")
    rc.write_json_atomic(rc.missions_dir(env.store.root) / f"{m['id']}.json", m)
    sid = "20261113-152631-50e0ed"
    write_turn(env.relay.sessions_dir() / sid, events, agent="claude", status="continue", last_message=wait_msg)
    report = rc.classify_turn(env.relay.sessions_dir() / sid / "turns" / "turn-001")

    sup = env.supervisor()
    sup.mission = m
    sup.state = json.loads(json.dumps(REAL_OWNER_MISSION_STATE_SHAPE))
    sup.state["permissions_digest"] = env.perm["v"]
    future = T0 + timedelta(hours=2)
    sup.state["cooldowns"] = {a: {"reason": "quota", "since": rc.iso(T0), "source": "limits", "until": rc.iso(future)}
                              for a in rc.AGENTS}
    sup.state["wait"] = {"jobs": [jid], "ci": [], "started_at": rc.iso(T0), "last_job_poll": None,
                         "last_ci_poll": None, "turn": f"{sid}/turn-001"}
    sup.signalled = True
    for _ in range(MAX_REPEATS):
        res = sup._wake_wait("unregistered_job", sup.state["wait"], report, {}, {}, failed_job=jid)
        assert res in ("signal", "stopped")
    assert sup.state["mission"]["unsatisfied_wakes"]["0"] == 1
    assert len(env.relay.launches) == 0


MAX_REPEATS = rc.MAX_UNSATISFIED_WAKES + 1
