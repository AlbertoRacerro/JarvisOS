"""Spec 348: OFFLINE-RESUME-1: Offline host wait without provider strikes and plain-Italian resume notice."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from test_relay_continuity import (
    SUCCESS,
    Env,
    rc,
)


def test_format_offline_window_same_day() -> None:
    # 18:05 UTC = 19:05 Europe/Rome (CET, UTC+1)
    # 21:27 UTC = 22:27 Europe/Rome (CET, UTC+1)
    start = datetime(2026, 11, 22, 18, 5, 0, tzinfo=UTC)
    end = datetime(2026, 11, 22, 21, 27, 0, tzinfo=UTC)
    start_str, end_str = rc.format_offline_window(start, end)
    assert start_str == "19:05"
    assert end_str == "22:27"


def test_format_offline_window_spanning_dates() -> None:
    start = datetime(2026, 11, 22, 22, 30, 0, tzinfo=UTC)  # 23:30 CET Nov 22
    end = datetime(2026, 11, 23, 1, 15, 0, tzinfo=UTC)   # 02:15 CET Nov 23
    start_str, end_str = rc.format_offline_window(start, end)
    assert start_str == "2026-11-22 23:30"
    assert end_str == "2026-11-23 02:15"


def test_format_offline_window_none_start() -> None:
    end = datetime(2026, 11, 22, 21, 27, 0, tzinfo=UTC)
    start_str, end_str = rc.format_offline_window(None, end)
    assert start_str == "ora sconosciuta"
    assert end_str == "22:27"


def test_criterion_1_pre_launch_probe_offline_waits_without_strikes_or_cooldowns(tmp_path: Path) -> None:
    """Criterion 1: When reachable() is False at _pre_launch_probe, the supervisor sets
    network_outage=True and network_outage_since, returns 'waiting', increments zero failure/cooldown
    counters, and never holds across repeated offline probes."""
    env = Env(tmp_path, [("claude", SUCCESS), ("claude", SUCCESS)])
    env.clock.t = datetime(2026, 11, 22, 18, 5, 0, tzinfo=UTC)
    env.host.reachable = lambda: False

    sup = env.supervisor()
    sup.state["permissions_digest"] = env.perm["v"]

    # First failing probe
    res1 = sup._launch("claude", "[relay-continuity attempt {nonce}]\ntask", None)
    assert res1 == "waiting"
    assert sup.state["network_outage"] is True
    assert sup.state["network_outage_since"] == "2026-11-22T18:05:00Z"
    assert sup.state["phase"] == "waiting"
    assert sup.state["cooldowns"] == {}
    assert sup.state["counters"]["consecutive_failures"] == 0
    assert sup.state["counters"].get("transient_claude", 0) == 0
    assert sup.state["hold"] is None
    assert len(env.relay.launches) == 0

    # Advance clock while still offline: repeated probes do not overwrite network_outage_since
    # and do not increment counters or trip a hold
    env.clock.t = datetime(2026, 11, 22, 20, 0, 0, tzinfo=UTC)
    for _ in range(10):
        res = sup._launch("claude", "[relay-continuity attempt {nonce}]\ntask", None)
        assert res == "waiting"

    assert sup.state["network_outage"] is True
    assert sup.state["network_outage_since"] == "2026-11-22T18:05:00Z"
    assert sup.state["cooldowns"] == {}
    assert sup.state["counters"]["consecutive_failures"] == 0
    assert sup.state["counters"].get("transient_claude", 0) == 0
    assert sup.state["hold"] is None
    # Exactly one ledger finding recorded
    findings = [t for k, t in env.ledger if k == "finding" and "probe failed" in t]
    assert len(findings) == 1


def test_criterion_2_mid_turn_disconnect_in_decide_waits_without_strikes_or_holds(tmp_path: Path) -> None:
    """Criterion 2: When a turn returns transient or unknown while reachable() is False,
    zero provider cooldowns or failure strikes are recorded and no hold is tripped; the supervisor
    enters network_outage=True and waits."""
    env = Env(tmp_path, [("claude", SUCCESS), ("claude", SUCCESS)])
    env.clock.t = datetime(2026, 11, 22, 18, 5, 0, tzinfo=UTC)
    env.host.reachable = lambda: False

    sup = env.supervisor()
    sup.state["head_session"] = "s1"
    sup.state["agent"] = "claude"
    sup.state["permissions_digest"] = env.perm["v"]
    # Suppose consecutive failures was already close to limit (e.g. 7)
    sup.state["counters"]["consecutive_failures"] = 7

    # Case A: Turn report is transient while offline
    transient_report = rc.TurnReport(
        outcome="transient",
        detail="connection reset by peer",
        harness_cause="stream_interrupted",
        agent="claude",
        turn_id="s1/turn-001",
    )

    decision = sup._decide(transient_report, None)
    assert decision == "waiting"
    assert sup.state["network_outage"] is True
    assert sup.state["network_outage_since"] == "2026-11-22T18:05:00Z"
    assert sup.state["phase"] == "waiting"
    assert "claude" not in sup.state["cooldowns"]
    assert sup.state["counters"].get("transient_claude", 0) == 0
    assert sup.state["counters"]["consecutive_failures"] == 7
    assert sup.state["hold"] is None

    # Case B: Turn report is unknown while offline - must not trip circuit breaker
    unknown_report = rc.TurnReport(
        outcome="unknown",
        detail="network died mid-turn",
        agent="claude",
        turn_id="s1/turn-002",
    )
    decision2 = sup._decide(unknown_report, None)
    assert decision2 == "waiting"
    assert sup.state["network_outage"] is True
    assert "claude" not in sup.state["cooldowns"]
    assert sup.state["counters"]["consecutive_failures"] == 7
    assert sup.state["hold"] is None


def test_criterion_3_reconnect_resumes_with_italian_notice_and_no_duplicate(tmp_path: Path) -> None:
    """Criterion 3: When reachable() later returns True, the supervisor clears network_outage,
    posts exactly one plain-Italian Slack message containing the offline start and end times
    ('Ero offline da ... a ..., riprendo.'), and resumes launching without manual intervention."""
    env = Env(tmp_path, [("claude", SUCCESS), ("claude", SUCCESS)])
    # Outage starts at 18:05 UTC (19:05 CET)
    env.clock.t = datetime(2026, 11, 22, 18, 5, 0, tzinfo=UTC)
    env.host.reachable = lambda: False

    sup = env.supervisor()
    sup.state["head_session"] = "s1"
    sup.state["permissions_digest"] = env.perm["v"]

    # Initial offline probe
    res = sup._launch("claude", "[relay-continuity attempt {nonce}]\ntask", None)
    assert res == "waiting"
    assert sup.state["network_outage"] is True
    assert env.sent == []

    # Host reconnects at 21:27 UTC (22:27 CET)
    env.clock.t = datetime(2026, 11, 22, 21, 27, 0, tzinfo=UTC)
    env.host.reachable = lambda: True

    res2 = sup._launch("claude", "[relay-continuity attempt {nonce}]\ntask", None)
    assert res2 != "waiting"
    assert sup.state["network_outage"] is False
    assert sup.state["network_outage_since"] is None
    assert len(env.relay.launches) == 1

    # Exactly one plain-Italian resume notice sent
    resume_messages = [msg for msg in env.sent if "Ero offline da" in msg]
    assert len(resume_messages) == 1
    assert resume_messages[0] == "Ero offline da 19:05 a 22:27, riprendo."
    # Strict secret and ID hygiene check
    assert "PR" not in resume_messages[0]
    assert "spec" not in resume_messages[0]
    assert "rc-" not in resume_messages[0]

    # Subsequent launch while online does NOT send a duplicate resume notice
    env.clock.t = datetime(2026, 11, 22, 21, 35, 0, tzinfo=UTC)
    res3 = sup._launch("claude", "[relay-continuity attempt {nonce}]\ntask", None)
    assert res3 != "waiting"
    resume_messages_after = [msg for msg in env.sent if "Ero offline da" in msg]
    assert len(resume_messages_after) == 1


def test_criterion_3_reconnect_spanning_dates_formats_full_datetime(tmp_path: Path) -> None:
    """When the outage spans midnight local time, start and end include YYYY-MM-DD."""
    env = Env(tmp_path, [("claude", SUCCESS), ("claude", SUCCESS)])
    env.clock.t = datetime(2026, 11, 22, 22, 30, 0, tzinfo=UTC)  # 23:30 CET Nov 22
    env.host.reachable = lambda: False

    sup = env.supervisor()
    sup.state["permissions_digest"] = env.perm["v"]
    res = sup._launch("claude", "[relay-continuity attempt {nonce}]\ntask", None)
    assert res == "waiting"

    # Reconnects next morning at 08:15 CET Nov 23 (07:15 UTC)
    env.clock.t = datetime(2026, 11, 23, 7, 15, 0, tzinfo=UTC)
    env.host.reachable = lambda: True

    res2 = sup._launch("claude", "[relay-continuity attempt {nonce}]\ntask", None)
    assert res2 != "waiting"
    resume_messages = [msg for msg in env.sent if "Ero offline da" in msg]
    assert len(resume_messages) == 1
    assert resume_messages[0] == "Ero offline da 2026-11-22 23:30 a 2026-11-23 08:15, riprendo."


def test_criterion_4_online_transient_quota_auth_semantics_preserved(tmp_path: Path) -> None:
    """Criterion 4: When reachable() is True, existing transient / quota / auth cooldown
    and hold behavior is unchanged."""
    env = Env(tmp_path, [("claude", SUCCESS), ("claude", SUCCESS)])
    env.host.reachable = lambda: True

    sup = env.supervisor()
    sup.state["head_session"] = "s1"
    sup.state["agent"] = "claude"

    # 1. Transient failure when online increments transient strikes and backs off
    transient_report = rc.TurnReport(
        outcome="transient",
        detail="provider 503",
        agent="claude",
        turn_id="s1/turn-001",
    )
    sup._decide(transient_report, None)
    assert sup.state["counters"]["transient_claude"] == 1
    assert "claude" in sup.state["cooldowns"]
    assert sup.state["cooldowns"]["claude"]["reason"] == "transient"

    # 2. Auth failure when online sets auth cooldown and records question
    auth_report = rc.TurnReport(
        outcome="auth",
        detail="api_key_invalid",
        agent="codex",
        turn_id="s1/turn-002",
    )
    sup._decide(auth_report, None)
    assert "codex" in sup.state["cooldowns"]
    assert sup.state["cooldowns"]["codex"]["source"] == "auth"
    questions = [t for k, t in env.ledger if k == "question"]
    assert len(questions) == 1

    # 3. Quota failure when online sets quota cooldown
    quota_report = rc.TurnReport(
        outcome="quota",
        detail="rate limit reached",
        agent="argon",
        turn_id="s1/turn-003",
    )
    sup._decide(quota_report, None)
    assert "argon" in sup.state["cooldowns"]


def test_state_load_defaults_and_validation(tmp_path: Path) -> None:
    """network_outage_since is an added state key defaulting to None on older states,
    and invalid types are rejected by validate_state."""
    state = rc.default_state()
    assert state["network_outage"] is False
    assert state["network_outage_since"] is None
    assert "network_outage_since" in rc.ADDED_STATE_KEYS

    # Older state missing network_outage_since loads with default None
    older_state = rc.default_state()
    del older_state["network_outage_since"]
    store = rc.Store(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    store.state_path.write_text(json.dumps(older_state), encoding="utf-8")
    loaded = store.load()
    assert loaded["network_outage_since"] is None

    # Corrupt network_outage_since fails validation
    corrupt_state = rc.default_state()
    corrupt_state["network_outage_since"] = "not-a-timestamp"
    assert rc.validate_state(corrupt_state) == "bad network_outage_since"

    # Corrupt network_outage bool fails validation
    corrupt_outage = rc.default_state()
    corrupt_outage["network_outage"] = "true"
    assert rc.validate_state(corrupt_outage) == "bad network_outage"
