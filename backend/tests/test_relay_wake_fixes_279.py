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
    relay_prompt,
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


def test_point_1_reachability_probe_failure_avoids_needless_sleep_and_keeps_outage(tmp_path: Path) -> None:
    """Finding 2: Reachability failure avoids needless sleep and pre-launch strike, keeping the outage rule."""
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

    slept_seconds: list[float] = []
    sup._sleep = lambda sec: (slept_seconds.append(sec), False)[1]

    # Reachability probe fails
    env.host.reachable = lambda: False

    decision = sup._decide(report, None)
    res = sup._launch(decision[0], decision[1], report) if isinstance(decision, tuple) else decision

    assert res == "waiting"
    # No needless sleep occurred
    assert len(slept_seconds) == 0
    # No strike was recorded before launch
    assert sup.state["counters"].get("transient_claude", 0) == 0
    # Outage rule was kept
    assert sup.state["network_outage"] is True
    assert "claude" not in sup.state["cooldowns"]
    assert len(env.relay.launches) == 0


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


def test_point_4_persisted_woken_wait_routes_through_availability_does_not_launch_cooling_agent(tmp_path: Path) -> None:
    """Finding 1: A persisted woken wait routes through availability / _wait_or_hold on restart and does not launch a cooling agent."""
    jid = "j-20261018T080000Z-100200"
    wait_msg = f"RELAY_STATUS continue\nJOB-WAIT: {jid}"
    events = [claude_result(wait_msg)]
    env = Env(tmp_path, [("hydrogen", SUCCESS)])
    sid = "s-woken-wait-restart"
    write_turn(env.relay.sessions_dir() / sid, events, agent="claude", status="continue", last_message=wait_msg)

    sup = env.supervisor()
    future = T0 + timedelta(hours=2)
    # Claude is cooling down
    cooldowns = {
        "claude": {"reason": "quota", "since": rc.iso(T0), "source": "limits", "until": rc.iso(future)}
    }
    sup.state.update({
        "head_session": sid,
        "agent": "claude",
        "permissions_digest": env.perm["v"],
        "cooldowns": dict(cooldowns),
        "wait": {
            "jobs": [jid],
            "ci": [],
            "started_at": rc.iso(T0),
            "turn": f"{sid}/turn-001",
            "woken": True,
            "wake_reason": "satisfied",
            "wake_agent": "claude",
            "wake_prompt": "woken resume prompt",
        },
    })
    sup.save()
    env.host.reachable = lambda: True

    # 1. When all agents are cooling down, supervisor stops/waits without launching claude
    sup.state["cooldowns"] = {
        a: {"reason": "quota", "since": rc.iso(T0), "source": "limits", "until": rc.iso(future)}
        for a in rc.AGENTS
    }
    sup.signalled = True
    res = sup._loop(None)
    assert res in ("signal", "stopped", "held")
    assert len(env.relay.launches) == 0

    # 2. When another agent is available, supervisor routes through availability instead of cooling claude
    sup.signalled = False
    sup.state["cooldowns"] = {
        "claude": {"reason": "quota", "since": rc.iso(T0), "source": "limits", "until": rc.iso(future)}
    }
    env.relay.script = [("argon", SUCCESS)]
    res2 = sup._loop(None)
    assert res2 != "waiting"
    assert len(env.relay.launches) == 1
    assert env.relay.launches[0]["agent"] != "claude"
    assert env.relay.launches[0]["agent"] in rc.AGENTS





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


def test_point_1_oserror_on_relay_start_records_not_started_without_consuming_strike(tmp_path: Path) -> None:
    """Finding 2: An OSError when executing relay records not_started without consuming a strike."""
    env = Env(tmp_path)
    report = rc.TurnReport(
        outcome="transient",
        detail="network timeout",
        harness_cause="provider_network",
        agent="argon",
        turn_id="s1/turn-001",
    )
    sup = env.supervisor()
    sup.state["head_session"] = "s1"
    sup.state["agent"] = "argon"
    sup.state["permissions_digest"] = env.perm["v"]
    sup.state["counters"]["transient_argon"] = 0

    def fail_start(*args: Any, **kwargs: Any) -> Any:
        raise OSError("exec format error / not found")

    env.relay.start = fail_start  # type: ignore[assignment]

    res = sup._launch("argon", "some prompt", report)
    assert res == "held"
    assert sup.state["hold"] is not None
    assert sup.state["hold"]["reason"] == "relay_invocation_failed"
    assert sup.state["counters"].get("transient_argon", 0) == 0
    assert sup.state["pending"] is None

    # Check journal has settle event with not_started
    entries = [json.loads(line) for line in env.store.journal_path.read_text().splitlines() if line.strip()]
    settle_entries = [e for e in entries if e.get("event") == "settle"]
    assert len(settle_entries) == 1
    assert settle_entries[0]["outcome"] == "not_started"


def test_point_1_adjacent_no_launch_paths_consume_no_strike(tmp_path: Path) -> None:
    """Adjacent no-launch paths (conflict, permissions change, stop during sleep) consume no strike."""
    env = Env(tmp_path)
    report = rc.TurnReport(
        outcome="transient",
        detail="network timeout",
        harness_cause="provider_network",
        agent="argon",
        turn_id="s1/turn-001",
    )
    sup = env.supervisor()
    sup.state["head_session"] = "s1"
    sup.state["agent"] = "argon"
    sup.state["permissions_digest"] = env.perm["v"]
    sup.state["counters"]["transient_argon"] = 0

    # 1. Conflict before launch
    env.conflicts = ["other-coord"]
    res1 = sup._launch("argon", "prompt", report)
    assert res1 == "waiting"
    assert sup.state["counters"].get("transient_argon", 0) == 0

    # 2. Permissions changed before launch
    env.conflicts = []
    sup.state["permissions_digest"] = "different_digest"
    res2 = sup._launch("argon", "prompt", report)
    assert res2 == "held"
    assert sup.state["hold"]["reason"] == "permissions_changed"
    assert sup.state["counters"].get("transient_argon", 0) == 0

    # 3. Interrupted sleep during backoff
    sup.state["permissions_digest"] = env.perm["v"]
    sup.state["hold"] = None
    sup.signalled = True
    res3 = sup._launch("argon", "prompt", report)
    assert res3 in ("signal", "stopped")
    assert sup.state["counters"].get("transient_argon", 0) == 0


def test_point_4_persisted_woken_wait_switching_to_fresh_agent_preserves_wait_status_and_task_context(
    tmp_path: Path,
) -> None:
    """Finding 1: When a persisted woken wait switches to another agent due to cooldown,
    wait status is preserved and the fresh agent path receives the full task context prompt,
    not the short resume prompt lacking mission task context.
    """
    jid = "j-20261018T080000Z-100300"
    wait_msg = f"RELAY_STATUS continue\nJOB-WAIT: {jid}"
    events = [claude_result(wait_msg)]
    env = Env(tmp_path, [("argon", SUCCESS)])
    env.relay.argon_limits = lambda a: {"windows": [{"used_percent": 30.0, "disabled": False}]} if a == "argon" else None
    m = mission_def("repair antenna and report telemetry", mid="m-20261116T040000Z-112233")
    rc.write_json_atomic(rc.missions_dir(env.store.root) / f"{m['id']}.json", m)

    sid = "s-woken-wait-fresh-agent"
    write_turn(env.relay.sessions_dir() / sid, events, agent="claude", status="continue", last_message=wait_msg)

    short_resume_prompt = (
        f"[relay-continuity resume {{nonce}}]\n"
        f"External wait satisfied:\n"
        f"- JOB {jid}: succeeded\n"
        f"Resume work on mission {m['id']} task 1/1."
    )

    sup = env.supervisor()
    sup.mission = m
    future = T0 + timedelta(hours=2)
    sup.state.update({
        "head_session": sid,
        "agent": "claude",
        "permissions_digest": env.perm["v"],
        "cooldowns": {
            "claude": {"reason": "quota", "since": rc.iso(T0), "source": "limits", "until": rc.iso(future)}
        },
        "mission": {
            "id": m["id"],
            "next": 0,
            "total": 1,
            "done": [],
        },
        "resumes": 1,
        "wait": {
            "jobs": [jid],
            "ci": [],
            "started_at": rc.iso(T0),
            "turn": f"{sid}/turn-001",
            "woken": True,
            "wake_reason": "satisfied",
            "wake_agent": "claude",
            "wake_prompt": short_resume_prompt,
        },
    })
    sup.save()

    # Pass 1: Reachability probe fails while switching to argon.
    # Wait status must be preserved, and the updated wake prompt for argon must contain the mission task.
    env.host.reachable = lambda: False
    res1 = sup._loop(None)
    assert res1 == "waiting"

    preserved_wait = sup.state.get("wait")
    assert preserved_wait is not None
    assert preserved_wait.get("woken") is True
    assert preserved_wait.get("wake_agent") == "argon"
    # Prompt was regenerated for argon (fresh context) rather than reusing the short resume prompt
    assert rc.MISSION_HEADER in preserved_wait.get("wake_prompt", "")
    assert "repair antenna and report telemetry" in preserved_wait.get("wake_prompt", "")
    assert "External wait satisfied" in preserved_wait.get("wake_prompt", "")
    # Resumes reset to 0 for fresh context
    assert sup.state.get("resumes") == 0

    # Pass 2: Network recovers; supervisor launches argon with full task context
    env.host.reachable = lambda: True
    res2 = sup._loop(None)
    assert res2 != "waiting"
    assert len(env.relay.launches) == 1

    launch = env.relay.launches[0]
    assert launch["agent"] == "argon"
    assert launch["parent"] is None  # fresh native agent has no parent session

    launched_prompt = launch["task"]
    # Verify the launched prompt contains the mission task context
    assert rc.MISSION_HEADER in launched_prompt
    assert "repair antenna and report telemetry" in launched_prompt
    assert "External wait satisfied" in launched_prompt
    assert not launched_prompt.startswith("[relay-continuity resume ")

    # Wait is cleared only after launch succeeded
    assert sup.state.get("wait") is None


def test_stopped_owner_after_first_pending_save_proves_no_duplicate_launch_after_recovery(
    tmp_path: Path,
) -> None:
    """Finding P1: When an owner stops after the first pending save of a wake launch,
    the pending intent and consumed wait are atomic so recovery does not duplicate the launch.
    """
    jid = "j-20261116T040000Z-100200"
    wait_msg = f"RELAY_STATUS continue\nJOB-WAIT: {jid}"
    events = [claude_result(wait_msg)]
    env = Env(tmp_path, [("argon", SUCCESS), ("argon", SUCCESS)])
    env.relay.argon_limits = lambda a: {"windows": [{"used_percent": 30.0, "disabled": False}]} if a == "argon" else None

    sid = "s-stopped-owner-atomic-wake"
    write_turn(env.relay.sessions_dir() / sid, events, agent="argon", status="continue", last_message=wait_msg)

    wake_prompt = (
        f"[relay-continuity resume {{nonce}}]\n"
        f"External wait satisfied:\n"
        f"- JOB {jid}: succeeded\n"
        f"Resume work."
    )

    sup = env.supervisor()
    sup.state.update({
        "head_session": sid,
        "agent": "argon",
        "permissions_digest": env.perm["v"],
        "wait": {
            "jobs": [jid],
            "ci": [],
            "started_at": rc.iso(T0),
            "turn": f"{sid}/turn-001",
            "woken": True,
            "wake_reason": "satisfied",
            "wake_agent": "argon",
            "wake_prompt": wake_prompt,
        },
    })
    sup.save()

    # Hook relay.start to simulate owner stopping right after the first pending save.
    # At relay.start invocation time, _launch has already executed self.save() for pending.
    orig_start = env.relay.start

    def stopping_start(agent: str, parent: str | None, task: str, turns: int, **kwargs: Any) -> Any:
        # Load persisted state on disk immediately after the first pending save
        persisted = env.store.load()
        # Verify atomic transition: pending intent is saved and wait is consumed
        assert persisted.get("pending") is not None
        assert persisted.get("wait") is None
        # Start relay session and finish turn so session exists on disk for recovery
        proc = orig_start(agent, parent, task, turns, **kwargs)
        env.relay.finish(proc)
        # Abrupt stop: owner terminates before supervisor executes second save
        raise KeyboardInterrupt("simulated stopped owner after first pending save")

    env.relay.start = stopping_start  # type: ignore[assignment]

    try:
        sup._loop(None)
    except KeyboardInterrupt:
        pass

    assert len(env.relay.launches) == 1

    # Verify persisted state representing stopped owner after the first pending save:
    # Pending intent is durable, and wait was consumed atomically
    persisted = env.store.load()
    assert persisted.get("pending") is not None
    assert persisted.get("wait") is None

    # Restart supervisor from the persisted state representing the stopped owner
    env.relay.start = orig_start
    sup2 = env.supervisor()
    sup2.state = env.store.load()

    # Run recovery loop: _resolve_pending settles the turn from the first launch,
    # and _loop must NOT see a woken wait or launch again.
    res = sup2._loop(None)
    assert res != "waiting"

    # Exactly 1 launch occurred in total: no duplicate launch after recovery
    assert len(env.relay.launches) == 1
    assert sup2.state.get("pending") is None
    assert sup2.state.get("wait") is None


def test_point_1_oserror_on_relay_start_with_woken_wait_preserves_retryable_wait(
    tmp_path: Path,
) -> None:
    """Finding P1: An OSError when executing relay with a woken wait settles no launch,
    consumes no strike, and preserves the retryable wait under relay_invocation_failed hold.
    """
    env = Env(tmp_path)
    report = rc.TurnReport(
        outcome="success",
        detail="prev turn",
        agent="argon",
        turn_id="s1/turn-001",
    )
    sup = env.supervisor()
    sup.state["head_session"] = "s1"
    sup.state["agent"] = "argon"
    sup.state["permissions_digest"] = env.perm["v"]
    sup.state["counters"]["transient_argon"] = 0

    jid = "j-20261116T040000Z-300400"
    wait_dict = {
        "jobs": [jid],
        "ci": [],
        "started_at": rc.iso(T0),
        "turn": "s1/turn-001",
        "woken": True,
        "wake_reason": "satisfied",
        "wake_agent": "argon",
        "wake_prompt": "wake prompt",
    }
    sup.state["wait"] = dict(wait_dict)

    def fail_start(*args: Any, **kwargs: Any) -> Any:
        raise OSError("exec format error / not found")

    env.relay.start = fail_start  # type: ignore[assignment]

    res = sup._launch("argon", "wake prompt", report)
    assert res == "held"
    assert sup.state["hold"] is not None
    assert sup.state["hold"]["reason"] == "relay_invocation_failed"
    assert sup.state["counters"].get("transient_argon", 0) == 0
    assert sup.state["pending"] is None

    # Wait is preserved and still retryable
    assert sup.state["wait"] is not None
    assert sup.state["wait"]["woken"] is True
    assert sup.state["wait"]["jobs"] == [jid]
    assert sup.state["wait"]["wake_agent"] == "argon"

    # Settle journal entry recorded not_started with session None
    entries = [json.loads(line) for line in env.store.journal_path.read_text().splitlines() if line.strip()]
    settle_entries = [e for e in entries if e.get("event") == "settle"]
    assert len(settle_entries) == 1
    assert settle_entries[0]["outcome"] == "not_started"
    assert settle_entries[0]["session"] is None


def test_stopped_owner_unatomic_disk_state_settles_and_does_not_duplicate_launch(
    tmp_path: Path,
) -> None:
    """Finding P1: If persisted state on disk has pending set alongside a woken wait
    (e.g. from an earlier un-atomic save), recovery settles the pending turn, consumes
    the woken wait, and does not duplicate the launch.
    """
    jid = "j-20261116T040000Z-500600"
    wait_msg = f"RELAY_STATUS continue\nJOB-WAIT: {jid}"
    events = [claude_result(wait_msg)]
    env = Env(tmp_path, [("claude", SUCCESS)])

    sid0 = "s-prior-session"
    write_turn(env.relay.sessions_dir() / sid0, events, agent="claude", status="continue", last_message=wait_msg)

    nonce = "rc-20261116T040000Z-a1b2c3d4"
    sid1 = "20261116-000001-bbbbbb"
    write_turn(
        env.relay.sessions_dir() / sid1,
        [claude_result("RELAY_STATUS propose_done\nWork finished")],
        agent="claude",
        prompt=f"\n## Task\n[relay-continuity attempt {nonce}]\ndo task",
        status="propose_done",
        last_message="Work finished",
    )

    sup = env.supervisor()
    sup.state.update({
        "head_session": sid0,
        "agent": "claude",
        "permissions_digest": env.perm["v"],
        "pending": {
            "nonce": nonce,
            "agent": "claude",
            "parent": None,
            "started_at": rc.iso(T0),
            "pid": None,
            "pid_start": None,
            "fingerprint": env.fp["v"],
            "attempt": 1,
        },
        "wait": {
            "jobs": [jid],
            "ci": [],
            "started_at": rc.iso(T0),
            "turn": f"{sid0}/turn-001",
            "woken": True,
            "wake_reason": "satisfied",
            "wake_agent": "claude",
            "wake_prompt": "wake prompt",
        },
    })
    sup.save()

    # Recovery: _resolve_pending settles the turn for nonce, and clears the woken wait
    res = sup._loop(None)
    assert res == "idle"

    # Zero new launches were triggered
    assert len(env.relay.launches) == 0
    assert sup.state.get("pending") is None
    assert sup.state.get("wait") is None


def test_stopped_owner_after_start_returns_and_before_second_save_counts_strike_on_recovery(
    tmp_path: Path,
) -> None:
    """Finding P2: For a same-agent transient retry, if owner stops after relay.start returns
    but before the second save, recovery (_resolve_pending) settles the launched turn with
    exactly one strike, ensuring crashes in this window cannot evade same_agent_retries.
    """
    env = Env(tmp_path, [("argon", []), ("argon", [])])
    env.limits.same_agent_retries = 2
    sid = "20261018-080000-prev01"
    session_dir = env.relay.sessions_dir() / sid
    turn_dir = session_dir / "turns" / "turn-001"
    turn_dir.mkdir(parents=True)
    (turn_dir / "native-result.json").write_text(json.dumps({
        "schema": "argon-coordinator.v1",
        "agent": "argon",
        "launched": False,
        "reason": "network_error",
    }))
    (turn_dir / "output.jsonl").write_text("")
    (turn_dir / "state.json").write_text(json.dumps({"agent_key": "argon", "status": "propose_done"}))
    (turn_dir / "prompt.md").write_text("task")

    sup = env.supervisor()
    sup.state.update({
        "head_session": sid,
        "agent": "argon",
        "permissions_digest": env.perm["v"],
    })
    sup.save()

    prev_report = rc.classify_turn(turn_dir)
    assert prev_report.outcome == "transient"
    assert prev_report.harness_cause == "provider_network"

    decision = sup._decide(prev_report, None)
    assert isinstance(decision, tuple)
    agent, prompt = decision
    assert agent == "argon"

    orig_start = env.relay.start

    def stopping_start(agent: str, parent: str | None, task: str, turns: int, **kwargs: Any) -> Any:
        # Verify first pending save recorded the transient retry intent before relay.start
        persisted = env.store.load()
        assert persisted.get("pending") is not None
        assert persisted["pending"]["transient_retry"] == "argon"
        assert not persisted["pending"].get("strike_counted")
        # On disk before second save, transient strike has not been added yet
        assert persisted.get("counters", {}).get("transient_argon", 0) == 0

        # Launch the turn and write the turn result so the session is durable for recovery
        ts = env.clock.now().strftime("%Y%m%d-%H%M%S")
        sid_retry = f"{ts}-retry01"
        sdir = env.relay.sessions_dir() / sid_retry
        turn = write_turn(sdir, [], agent=agent, prompt=relay_prompt(task), status="continue")
        (turn / "native-result.json").write_text(json.dumps({
            "schema": "argon-coordinator.v1",
            "agent": "argon",
            "launched": False,
            "reason": "network_error",
        }))
        env.relay.launches.append({"agent": agent, "task": task})

        # Abrupt stop: owner terminates right after start returns, BEFORE supervisor executes second save
        raise KeyboardInterrupt("simulated stop after start returns before second save")

    env.relay.start = stopping_start  # type: ignore[assignment]

    try:
        sup._launch(agent, prompt, prev_report)
    except KeyboardInterrupt:
        pass

    # Durable state on disk immediately after crash: pending intent persisted, strike counter still 0
    persisted = env.store.load()
    assert persisted.get("pending") is not None
    assert persisted["pending"]["transient_retry"] == "argon"
    assert persisted["counters"].get("transient_argon", 0) == 0

    # Recovery: a new supervisor recovers from disk
    env.relay.start = orig_start
    sup2 = env.supervisor()
    sup2.state = env.store.load()
    rep = sup2._resolve_pending()

    # Recovery must settle the real launched retry and count exactly one strike
    assert not isinstance(rep, str)
    assert rep.outcome == "transient"
    assert rep.harness_cause == "provider_network"
    assert sup2.state["counters"]["transient_argon"] == 1
    assert sup2.state.get("pending") is None

    # Disk state reflects the counted strike
    persisted2 = env.store.load()
    assert persisted2["counters"]["transient_argon"] == 1
    assert persisted2.get("pending") is None

    # Subsequent crash in this same window advances the strike and cannot evade same_agent_retries
    decision2 = sup2._decide(rep, None)
    assert isinstance(decision2, tuple)
    agent2, prompt2 = decision2
    assert agent2 == "argon"

    def stopping_start2(agent: str, parent: str | None, task: str, turns: int, **kwargs: Any) -> Any:
        ts = env.clock.now().strftime("%Y%m%d-%H%M%S")
        sid_retry2 = f"{ts}-retry02"
        sdir2 = env.relay.sessions_dir() / sid_retry2
        turn2 = write_turn(sdir2, [], agent=agent, prompt=relay_prompt(task), status="continue")
        (turn2 / "native-result.json").write_text(json.dumps({
            "schema": "argon-coordinator.v1",
            "agent": "argon",
            "launched": False,
            "reason": "network_error",
        }))
        env.relay.launches.append({"agent": agent, "task": task})
        raise KeyboardInterrupt("simulated stop after second retry start returns before second save")

    env.relay.start = stopping_start2  # type: ignore[assignment]
    try:
        sup2._launch(agent2, prompt2, rep)
    except KeyboardInterrupt:
        pass

    # Second recovery
    env.relay.start = orig_start
    sup3 = env.supervisor()
    sup3.state = env.store.load()
    rep2 = sup3._resolve_pending()
    assert not isinstance(rep2, str)
    # Recovery increments to 2 strikes
    assert sup3.state["counters"]["transient_argon"] == 2

    # Now same_agent_retries (limit=2) is exhausted: _decide will cool down argon and switch
    decision3 = sup3._decide(rep2, None)
    assert "argon" in sup3.state["cooldowns"]
    assert isinstance(decision3, tuple)
    assert decision3[0] != "argon"


def test_transient_retry_no_launch_race_before_start_and_oserror_count_zero_strikes(
    tmp_path: Path,
) -> None:
    """Finding P2: A race where owner stops before relay.start (no launch), or where relay.start
    raises OSError, counts exactly zero strikes.
    """
    env = Env(tmp_path)

    # 1. No-launch race before relay.start
    nonce_no_launch = "rc-20261018T080100Z-11111111"
    sup = env.supervisor()
    sup.state.update({
        "permissions_digest": env.perm["v"],
        "pending": {
            "nonce": nonce_no_launch,
            "agent": "argon",
            "parent": None,
            "started_at": rc.iso(T0),
            "pid": None,
            "pid_start": None,
            "fingerprint": env.fp["v"],
            "attempt": 1,
            "transient_retry": "argon",
        },
    })
    sup.save()
    res = sup._resolve_pending()
    assert res == "held"
    assert sup.state["hold"]["reason"] == "attempt_unresolved"
    assert sup.state["counters"].get("transient_argon", 0) == 0

    # 2. OSError on relay.start
    report = rc.TurnReport(
        outcome="transient",
        detail="network timeout",
        harness_cause="provider_network",
        agent="argon",
        turn_id="s1/turn-001",
    )
    sup.state.update({
        "hold": None,
        "head_session": "s1",
        "agent": "argon",
        "pending": None,
    })
    sup.save()

    def fail_start(*args: Any, **kwargs: Any) -> Any:
        raise OSError("exec format error")

    env.relay.start = fail_start  # type: ignore[assignment]
    res_launch = sup._launch("argon", "some prompt", report)
    assert res_launch == "held"
    assert sup.state["hold"]["reason"] == "relay_invocation_failed"
    assert sup.state["counters"].get("transient_argon", 0) == 0
    assert sup.state["pending"] is None


def test_transient_retry_ordinary_path_counts_strike_once(tmp_path: Path) -> None:
    """Finding P2: In the ordinary path without crashes, a launched retry increments the strike
    exactly once across both the second save and _settle (no double counting).
    """
    env = Env(tmp_path, [("argon", [])])
    orig_finish = env.relay.finish

    def finish_transient(proc: Any, **kw: Any) -> Any:
        res = orig_finish(proc, **kw)
        turn = rc.last_turn_dir(env.relay.sessions_dir() / res.session_id)
        (turn / "native-result.json").write_text(json.dumps({
            "schema": "argon-coordinator.v1",
            "agent": "argon",
            "launched": False,
            "reason": "network_error",
        }))
        return res

    env.relay.finish = finish_transient  # type: ignore[assignment]

    sid = "20261018-080000-prev01"
    session_dir = env.relay.sessions_dir() / sid
    turn_dir = session_dir / "turns" / "turn-001"
    turn_dir.mkdir(parents=True)
    (turn_dir / "native-result.json").write_text(json.dumps({
        "schema": "argon-coordinator.v1",
        "agent": "argon",
        "launched": False,
        "reason": "network_error",
    }))
    (turn_dir / "output.jsonl").write_text("")
    (turn_dir / "state.json").write_text(json.dumps({"agent_key": "argon", "status": "propose_done"}))
    (turn_dir / "prompt.md").write_text("task")

    sup = env.supervisor()
    sup.state.update({
        "head_session": sid,
        "agent": "argon",
        "permissions_digest": env.perm["v"],
    })
    sup.save()

    prev_report = rc.classify_turn(turn_dir)
    decision = sup._decide(prev_report, None)
    assert isinstance(decision, tuple)
    agent, prompt = decision

    launched_rep = sup._launch(agent, prompt, prev_report)
    assert not isinstance(launched_rep, str)

    # Strike was counted exactly once (not twice)
    assert sup.state["counters"]["transient_argon"] == 1
    persisted = env.store.load()
    assert persisted["counters"]["transient_argon"] == 1


def test_transient_retry_old_pending_states_recovery(tmp_path: Path) -> None:
    """Finding P2: Recovery safely handles old pending states without transient_retry field:
    non-retry states count 0 strikes, while legacy retry states that stopped before second save
    count exactly 1 strike.
    """
    env = Env(tmp_path)

    # 1. Old pending state representing a non-retry attempt
    nonce_regular = "rc-20261018T080200Z-22222222"
    sid_regular = "20261018-080200-reg001"
    sdir = env.relay.sessions_dir() / sid_regular
    write_turn(sdir, [], agent="claude", prompt=relay_prompt(f"[relay-continuity attempt {nonce_regular}]"), status="propose_done")
    sup = env.supervisor()
    sup.state.update({
        "permissions_digest": env.perm["v"],
        "head_session": "prior-head",
        "pending": {
            "nonce": nonce_regular,
            "agent": "claude",
            "parent": None,
            "started_at": rc.iso(T0),
            "pid": None,
            "pid_start": None,
            "fingerprint": env.fp["v"],
            "attempt": 1,
        },
    })
    sup.save()
    res = sup._resolve_pending()
    assert not isinstance(res, str)
    assert sup.state["counters"].get("transient_claude", 0) == 0

    # 2. Old pending state representing a same-agent transient retry stopped before second save (pid=None)
    sid_prev_transient = "20261018-080000-trans01"
    sdir_prev = env.relay.sessions_dir() / sid_prev_transient
    t_prev = write_turn(sdir_prev, [], agent="argon", prompt="task", status="continue")
    (t_prev / "native-result.json").write_text(json.dumps({
        "schema": "argon-coordinator.v1",
        "agent": "argon",
        "launched": False,
        "reason": "network_error",
    }))
    nonce_old_retry = "rc-20261018T080300Z-33333333"
    sid_retry = "20261018-080300-ret001"
    sdir_retry = env.relay.sessions_dir() / sid_retry
    write_turn(sdir_retry, [], agent="argon", prompt=relay_prompt(f"[relay-continuity attempt {nonce_old_retry}]"), status="continue")
    sup.state.update({
        "head_session": sid_prev_transient,
        "agent": "argon",
        "counters": {"consecutive_failures": 0, "crash_retries": 0, "transient_streak": 0},
        "pending": {
            "nonce": nonce_old_retry,
            "agent": "argon",
            "parent": None,
            "started_at": rc.iso(T0),
            "pid": None,
            "pid_start": None,
            "fingerprint": env.fp["v"],
            "attempt": 1,
        },
    })
    sup.save()
    res2 = sup._resolve_pending()
    assert not isinstance(res2, str)
    assert sup.state["counters"]["transient_argon"] == 1


MAX_REPEATS = rc.MAX_UNSATISFIED_WAKES + 1
