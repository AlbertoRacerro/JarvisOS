"""Spec 278: Native turn timeouts settle without an output hold."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from test_relay_continuity import Env, mission_def, rc

ra = rc.relay_argon

FIXTURE_DIR = Path(__file__).parent / "fixtures/relay_continuity/argon_wall_timeout"


def test_native_runner_sigterm_writes_atomic_result_and_cleans_up(tmp_path: Path) -> None:
    """Criterion 1: Native runner SIGTERM writes a non-success atomic native-result.json and exits;

    no partial output can count as completion.
    """
    repo = tmp_path / "repo"
    session_id = "20261115-210000-112233"
    turn_dir = repo / ".agent-relay/sessions" / session_id / "turns/turn-001"
    turn_dir.mkdir(parents=True)
    (turn_dir / "prompt.md").write_text("# Native coordinator\nTask\n", encoding="utf-8")
    (turn_dir / "state.json").write_text(json.dumps({"agent_key": "argon", "status": "continue"}), encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_agy = bin_dir / "agy"
    fake_agy.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, time\n"
        "if '-p' in sys.argv and '/quota' in sys.argv:\n"
        "    print(json.dumps({'status': 'SUCCESS', 'command': {'data': {'groups': [{'buckets': [\n"
        "        {'id': 'gemini-5h', 'remaining_fraction': 0.8},\n"
        "        {'id': 'gemini-weekly', 'remaining_fraction': 0.8},\n"
        "        {'id': 'gemini-argon-wtu-weekly', 'remaining_fraction': 0.8},\n"
        "    ]}]}}}))\n"
        "    sys.exit(0)\n"
        "# In-flight model execution:\n"
        "print('started in-flight', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)

    script = (
        "import sys, os, signal\n"
        "from pathlib import Path\n"
        "import scripts.relay_argon as ra\n"
        "ra.sandbox = lambda: True\n"
        "ra.prepare_login = lambda: None\n"
        "ra.subscription_settings = lambda: None\n"
        "sys.exit(ra.run(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))\n"
    )

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "JARVISOS_MISSION_PROFILE": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent.parent),
    }

    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(repo), session_id, "argon"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    result_file = turn_dir / "native-result.json"
    for _ in range(50):
        if (turn_dir / "output.jsonl").exists() or (turn_dir / "stderr.log").exists():
            break
        try:
            proc.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            pass

    proc.send_signal(signal.SIGTERM)
    code = proc.wait(timeout=10.0)

    assert code != 0
    assert result_file.exists()
    meta = json.loads(result_file.read_text(encoding="utf-8"))
    assert meta["reason"] == "sigterm"
    assert meta["exit_code"] == -signal.SIGTERM
    assert not (turn_dir / "last-message.txt").exists()


def test_retained_native_timeout_fixture_settlement_and_gating(tmp_path: Path) -> None:
    """Criterion 2: Retained shape of a timed-out native turn with no runner result

    and terminal error stream settles as turn_time_limit with durable timeout flag,
    and gates as output_unparseable without it.
    """
    assert FIXTURE_DIR.is_dir()
    turn_dir = tmp_path / "sessions/20261115-210000-aabbcc/turns/turn-001"
    turn_dir.mkdir(parents=True)
    for f in FIXTURE_DIR.iterdir():
        if f.is_file():
            shutil.copy(f, turn_dir / f.name)

    # 1. Without supervisor durable timeout flag: gates as output_unparseable
    report_no_flag = rc.classify_turn(turn_dir, timed_out=False)
    assert rc.gate(report_no_flag) == "output_unparseable"

    env = Env(tmp_path / "env1")
    sup = env.supervisor()
    decision = sup._decide(report_no_flag, "task")
    assert decision == "held"
    assert sup.state["hold"]["reason"] == "output_unparseable"

    # 2. With supervisor durable timeout flag: settles as turn_time_limit, never output_unparseable
    report_flag = rc.classify_turn(turn_dir, timed_out=True)
    assert report_flag.outcome == "unknown"
    assert report_flag.harness_cause == "turn_time_limit"
    assert rc.gate(report_flag) is None

    env2 = Env(tmp_path / "env2")
    sup2 = env2.supervisor()
    pending = {
        "nonce": "rc-20261115T210000Z-aabb",
        "agent": "argon",
        "started_at": rc.iso(rc.now_utc()),
        "timed_out": True,
        "diagnostics": "turn wall limit (3690s) exceeded",
        "attempt": 1,
    }
    settled_report = sup2._settle(
        pending,
        "20261115-210000-aabbcc",
        diagnostics="turn wall limit (3690s) exceeded",
        timed_out=True,
    )
    assert isinstance(settled_report, rc.TurnReport)
    assert settled_report.outcome == "unknown"
    assert settled_report.harness_cause == "turn_time_limit"

    journal_events = [
        json.loads(line) for line in sup2.store.journal_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    settle_entry = next(e for e in journal_events if e.get("event") == "settle")
    assert settle_entry["outcome"] == "unknown"
    assert settle_entry["harness_cause"] == "turn_time_limit"


def test_timed_out_attempt_cannot_complete_task_and_parks_external_effects(tmp_path: Path) -> None:
    """Criterion 3: Timed-out attempt cannot complete a mission task.

    Its recorded external calls are reconciled or parked before retry,
    and an unproven call is not repeated automatically.
    """
    env = Env(tmp_path)
    sup = env.supervisor()
    mission = mission_def("mission 1", mid="m-20261115T210000Z-112233")
    sup.mission = mission
    sup.state["mission"] = {"id": mission["id"], "next": 0, "total": 1, "done": []}

    turn_dir = env.repo / ".agent-relay/sessions/20261115-210000-aabbcc/turns/turn-001"
    turn_dir.mkdir(parents=True)
    for f in FIXTURE_DIR.iterdir():
        if f.is_file():
            shutil.copy(f, turn_dir / f.name)

    # In-flight external call from fixture is 'git push origin HEAD'
    pending = {
        "nonce": "rc-20261115T210000Z-aabb",
        "agent": "argon",
        "started_at": rc.iso(rc.now_utc()),
        "timed_out": True,
        "diagnostics": "turn wall limit (3690s) exceeded",
        "attempt": 1,
        "task": 0,
    }
    # Probe returns None -> cannot be settled/proven
    env.host.probe = lambda call: None
    env.relay.argon_limits = lambda a: {"windows": [{"used_percent": 30}]} if a in ("argon", "agy_opus") else None

    report = sup._settle(pending, "20261115-210000-aabbcc", timed_out=True)
    assert isinstance(report, rc.TurnReport)
    assert report.outcome == "unknown"
    assert report.harness_cause == "turn_time_limit"

    # Mission task 0 was NOT completed
    assert sup.state["mission"]["next"] == 0
    assert len(sup.state["mission"]["done"]) == 0

    # Decision on this report reconciles and parks the external call
    decision = sup._decide(report, "mission task text")
    assert isinstance(decision, tuple)
    next_agent, next_prompt = decision
    assert next_agent == "argon"
    # The external operation is parked and listed in prompt as UNRESOLVED
    assert "git push: UNRESOLVED (parked)" in next_prompt
    assert report.turn_id in sup.state.get("parked_turns", [])


def test_native_and_non_native_wall_limits(tmp_path: Path) -> None:
    """Criterion 4: The native supervisor wall limit exceeds runner TURN_TIMEOUT + 30 seconds

    plus cleanup margin; non-native turns keep their current limit.
    A restart adopting a native turn uses the same limit.
    """
    limits = rc.Limits()
    assert limits.turn_wall == 3600.0
    assert limits.native_turn_wall > ra.TURN_TIMEOUT + 30.0
    assert limits.native_turn_wall >= ra.TURN_TIMEOUT + 30.0 + 30.0

    env = Env(tmp_path)
    sup = env.supervisor()

    recorded_timeouts: list[float | None] = []

    class FakeProc:
        pid = 99999
        returncode = 0

    def fake_start(agent: str, parent: str | None, text: str, turns: int, **kw: Any) -> Any:
        return FakeProc()

    def fake_finish(proc: Any, timeout: float | None = None) -> rc.RunResult:
        recorded_timeouts.append(timeout)
        sid = "20261115-210000-999999"
        td = env.relay.sessions_dir() / sid / "turns/turn-001"
        td.mkdir(parents=True, exist_ok=True)
        (td / "state.json").write_text(json.dumps({"agent_key": "argon"}))
        (td / "output.jsonl").write_text("")
        (td / "native-result.json").write_text(json.dumps({"schema": "argon-coordinator.v1", "agent": "argon", "exit_code": 0}))
        return rc.RunResult(session_id=sid, exit_code=0, raw_ok=True)

    env.relay.start = fake_start
    env.relay.finish = fake_finish
    sup.state["permissions_digest"] = env.host.permissions_digest()

    # 1. Launch native turn uses native_turn_wall
    sup._launch("argon", "[relay-continuity attempt {nonce}]\ntask", None)
    assert recorded_timeouts[-1] == limits.native_turn_wall

    # 2. Launch non-native turn uses turn_wall and prompt matches outer limit
    launches: list[str] = []

    def claude_finish(proc: Any, timeout: float | None = None) -> rc.RunResult:
        recorded_timeouts.append(timeout)
        sid = "20261115-210000-888888"
        td = env.relay.sessions_dir() / sid / "turns/turn-001"
        td.mkdir(parents=True, exist_ok=True)
        (td / "state.json").write_text(json.dumps({"agent_key": "claude"}))
        (td / "output.jsonl").write_text("")
        return rc.RunResult(session_id=sid, exit_code=0, raw_ok=True)

    def fake_start_claude(agent: str, parent: str | None, text: str, turns: int, **kw: Any) -> Any:
        launches.append(text)
        return FakeProc()

    env.relay.start = fake_start_claude
    env.relay.finish = claude_finish
    sup._launch("claude", "[relay-continuity attempt {nonce}]\ntask", None)
    assert recorded_timeouts[-1] == limits.turn_wall
    expected_sentence = f"Your turn has a hard limit of {int(limits.turn_wall // 60)} minutes."
    assert expected_sentence in launches[0]

    # 3. Adopted native turn in _resolve_pending uses native_turn_wall
    sim_time = [rc.now_utc()]

    def clock_now():
        return sim_time[0]

    env.host.now = clock_now

    dead_pid = 88881
    env.host.pid_alive = lambda pid, start: True
    killed_signals: list[tuple[int, int]] = []
    env.relay.killpg = lambda pgid, sig: killed_signals.append((pgid, sig))
    env.relay._group_is_running = lambda pgid: False

    # Started 3650s ago (exceeds 3600s, but less than native_turn_wall 3690s)
    started = clock_now()
    sup.state["pending"] = {
        "nonce": "rc-20261115T210000Z-adopt1",
        "agent": "argon",
        "started_at": rc.iso(started),
        "pid": dead_pid,
        "pid_start": 1,
    }
    # At 3650s elapsed: native turn is NOT timed out yet
    sim_time[0] = started + rc.timedelta(seconds=3650)
    # Give sleep a function that stops pid alive after one check
    call_count = [0]

    def fake_sleep(secs):
        sim_time[0] += rc.timedelta(seconds=secs)
        call_count[0] += 1
        if call_count[0] >= 1:
            env.host.pid_alive = lambda pid, start: False
        return False

    sup._sleep = fake_sleep
    sup._resolve_pending()
    # At 3650s, native turn was NOT timed out by wall limit
    assert (dead_pid, signal.SIGTERM) not in killed_signals

    # For a non-native adopted turn at 3650s: exceeds turn_wall (3600s), so it IS terminated
    killed_signals.clear()
    env.host.pid_alive = lambda pid, start: True
    sup.state["pending"] = {
        "nonce": "rc-20261115T210000Z-adopt2",
        "agent": "claude",
        "started_at": rc.iso(started),
        "pid": dead_pid,
        "pid_start": 1,
    }
    call_count[0] = 0

    def fake_sleep_term(secs):
        sim_time[0] += rc.timedelta(seconds=secs)
        env.host.pid_alive = lambda pid, start: False
        return False

    sup._sleep = fake_sleep_term
    sup._resolve_pending()
    assert (dead_pid, signal.SIGTERM) in killed_signals


def test_ordinary_malformed_output_still_gates(tmp_path: Path) -> None:
    """Ordinary malformed output without supervisor timeout flag still gates."""
    # 1. Missing metadata without timeout
    turn1 = tmp_path / "turn1"
    turn1.mkdir()
    (turn1 / "state.json").write_text(json.dumps({"agent_key": "argon"}))
    (turn1 / "output.jsonl").write_text(
        '{"event":"init","conversation_id":"abc","init":{"model":"gemini-4-argon-low"}}\n'
    )
    report1 = rc.classify_turn(turn1, timed_out=False)
    assert rc.gate(report1) == "output_unparseable"

    # 2. Corrupt JSON line in output without timeout
    turn2 = tmp_path / "turn2"
    turn2.mkdir()
    (turn2 / "state.json").write_text(json.dumps({"agent_key": "argon"}))
    (turn2 / "native-result.json").write_text(
        json.dumps({"schema": "argon-coordinator.v1", "agent": "argon", "exit_code": 0, "model": "gemini-4-argon-low"})
    )
    (turn2 / "output.jsonl").write_text(
        '{"event":"init","conversation_id":"abc","init":{"model":"gemini-4-argon-low"}}\n'
        "{corrupt line\n"
    )
    report2 = rc.classify_turn(turn2, timed_out=False)
    assert rc.gate(report2) == "output_unparseable"

    # 3. Empty output without timeout
    turn3 = tmp_path / "turn3"
    turn3.mkdir()
    (turn3 / "state.json").write_text(json.dumps({"agent_key": "argon"}))
    (turn3 / "output.jsonl").write_text("")
    report3 = rc.classify_turn(turn3, timed_out=False)
    assert rc.gate(report3) == "output_missing"


def test_sigterm_without_supervisor_timeout_flag_retains_output_gate(tmp_path: Path) -> None:
    """Regression test for Spec 278 contract:

    native-result reason 'sigterm' with malformed/incomplete stream:
    - timed_out=False -> gate holds (does not bypass output gate on non-timeout signals);
    - timed_out=True -> outcome is unknown, harness_cause is turn_time_limit, no gate.
    """
    # 1. Torn / malformed stream with native-result reason="sigterm"
    turn_malformed = tmp_path / "turn_malformed"
    turn_malformed.mkdir()
    (turn_malformed / "state.json").write_text(json.dumps({"agent_key": "argon"}))
    (turn_malformed / "native-result.json").write_text(
        json.dumps({
            "schema": "argon-coordinator.v1",
            "agent": "argon",
            "reason": "sigterm",
            "exit_code": -15,
            "model": "gemini-4-argon-low",
        })
    )
    (turn_malformed / "output.jsonl").write_text(
        '{"event":"init","conversation_id":"c1","init":{"model":"gemini-4-argon-low"}}\n'
        '{"event":"step_update","step_update":{"conversation_id":"c1","step_index":1,"state":"ACTIVE","step_type":"tool","tool_name":"run_command","tool_info":{"parameters":{"CommandLine":"ls"}}}}\n'
        "{torn_incomplete_line\n"
    )

    # timed_out=False: gate holds as output_unparseable; harness_cause is NOT turn_time_limit
    rep_malformed_no_flag = rc.classify_turn(turn_malformed, timed_out=False)
    assert rep_malformed_no_flag.harness_cause != "turn_time_limit"
    assert rc.gate(rep_malformed_no_flag) == "output_unparseable"

    # timed_out=True: unknown turn_time_limit, gate is bypassed
    rep_malformed_flag = rc.classify_turn(turn_malformed, timed_out=True)
    assert rep_malformed_flag.outcome == "unknown"
    assert rep_malformed_flag.harness_cause == "turn_time_limit"
    assert rc.gate(rep_malformed_flag) is None

    # 2. Incomplete / missing evidence stream with native-result reason="sigterm"
    turn_empty = tmp_path / "turn_empty"
    turn_empty.mkdir()
    (turn_empty / "state.json").write_text(json.dumps({"agent_key": "argon"}))
    (turn_empty / "native-result.json").write_text(
        json.dumps({
            "schema": "argon-coordinator.v1",
            "agent": "argon",
            "reason": "sigterm",
            "exit_code": -15,
            "model": "gemini-4-argon-low",
            "launched": True,
        })
    )
    (turn_empty / "output.jsonl").write_text("")

    # timed_out=False: gate holds as output_missing; harness_cause is NOT turn_time_limit
    rep_empty_no_flag = rc.classify_turn(turn_empty, timed_out=False)
    assert rep_empty_no_flag.harness_cause != "turn_time_limit"
    assert rc.gate(rep_empty_no_flag) == "output_missing"

    # timed_out=True: unknown turn_time_limit, gate is bypassed
    rep_empty_flag = rc.classify_turn(turn_empty, timed_out=True)
    assert rep_empty_flag.outcome == "unknown"
    assert rep_empty_flag.harness_cause == "turn_time_limit"
    assert rc.gate(rep_empty_flag) is None


def test_runner_stderr_print_timeout_without_supervisor_flag_does_not_bypass_gate(tmp_path: Path) -> None:
    """Causal regression test for review job P1:

    A runner stderr AGY_ERROR reason print_timeout without supervisor timed_out=True:
    1. With missing metadata or malformed lines, harness_cause is NOT turn_time_limit,
       and gate holds as output_unparseable.
    2. With empty output, harness_cause is NOT turn_time_limit,
       and gate holds as output_missing.
    3. With supervisor durable timed_out=True, both cases settle as unknown turn_time_limit
       and bypass the gate.
    4. With a valid record and timed_out=False, honest runner print_timeout is preserved
       as unknown turn_time_limit without holding at gate, keeping in-flight effects.
    """
    agy_err = ra.AGY_ERROR_PREFIX + json.dumps({
        "status": "DEADLINE_EXCEEDED",
        "reason": "print_timeout",
        "short_error": "print timeout after 30m0s with the turn still in progress",
    }) + "\n"

    # Case 1: Missing native-result.json and unparseable/torn lines
    turn_malformed = tmp_path / "turn_malformed"
    turn_malformed.mkdir()
    (turn_malformed / "state.json").write_text(json.dumps({"agent_key": "argon"}))
    (turn_malformed / "output.jsonl").write_text(
        '{"event":"init","conversation_id":"c1","init":{"model":"gemini-4-argon-low"}}\n'
        "{bad_torn_line\n"
    )
    (turn_malformed / "stderr.log").write_text(agy_err)

    # timed_out=False: runner print_timeout cannot set turn_time_limit or bypass gate
    rep_malformed = rc.classify_turn(turn_malformed, timed_out=False)
    assert rep_malformed.harness_cause != "turn_time_limit"
    assert rep_malformed.bad_lines > 0
    assert rc.gate(rep_malformed) == "output_unparseable"

    # timed_out=True: supervisor flag settles as unknown turn_time_limit and bypasses gate
    rep_malformed_sup = rc.classify_turn(turn_malformed, timed_out=True)
    assert rep_malformed_sup.outcome == "unknown"
    assert rep_malformed_sup.harness_cause == "turn_time_limit"
    assert rc.gate(rep_malformed_sup) is None

    # Case 2: Missing evidence / empty output with missing native-result.json
    turn_empty = tmp_path / "turn_empty"
    turn_empty.mkdir()
    (turn_empty / "state.json").write_text(json.dumps({"agent_key": "argon"}))
    (turn_empty / "output.jsonl").write_text("")
    (turn_empty / "stderr.log").write_text(agy_err)

    # timed_out=False: gate holds as output_missing, no turn_time_limit
    rep_empty = rc.classify_turn(turn_empty, timed_out=False)
    assert rep_empty.harness_cause != "turn_time_limit"
    assert rep_empty.no_evidence is True
    assert rc.gate(rep_empty) == "output_missing"

    # timed_out=True: supervisor flag settles as unknown turn_time_limit and bypasses gate
    rep_empty_sup = rc.classify_turn(turn_empty, timed_out=True)
    assert rep_empty_sup.outcome == "unknown"
    assert rep_empty_sup.harness_cause == "turn_time_limit"
    assert rc.gate(rep_empty_sup) is None

    # Case 3: Valid record with runner print_timeout and timed_out=False preserves classification
    turn_valid = tmp_path / "turn_valid"
    turn_valid.mkdir()
    (turn_valid / "state.json").write_text(json.dumps({"agent_key": "argon"}))
    (turn_valid / "native-result.json").write_text(json.dumps({
        "schema": "argon-coordinator.v1",
        "agent": "argon",
        "exit_code": 3,
        "launched": True,
        "model": "gemini-4-argon-low",
    }))
    (turn_valid / "output.jsonl").write_text(
        '{"event":"init","conversation_id":"c1","init":{"model":"gemini-4-argon-low"}}\n'
        '{"event":"step_update","step_update":{"conversation_id":"c1","step_index":2,"state":"ACTIVE","step_type":"tool","tool_name":"run_command","tool_info":{"parameters":{"CommandLine":"git push origin topic"}}}}\n'
    )
    (turn_valid / "stderr.log").write_text(agy_err)

    rep_valid = rc.classify_turn(turn_valid, timed_out=False)
    assert rep_valid.outcome == "unknown"
    assert rep_valid.harness_cause == "turn_time_limit"
    assert rep_valid.bad_lines == 0
    assert rep_valid.no_evidence is False
    assert rc.gate(rep_valid) is None
    assert [c.label for c in rep_valid.in_flight] == ["git push"]


def test_session_without_turns_timeout_settlement_and_gating(tmp_path: Path) -> None:
    """Session without turns:

    1. Native agent with supervisor durable timed_out=True settles as unknown turn_time_limit,
       sets report.timed_out=True, bypasses the output gate, and preserves external-effect safety.
    2. Native agent with timed_out=False settles as crash without harness_cause or timed_out=True,
       and gates as output_missing (does not bypass without flag).
    3. Non-native agent with timed_out=True does not get native timeout bypass and gates as output_missing.
    4. External-effect safety: an in-flight external operation is parked, not repeated or advanced.
    """
    mission = mission_def("mission 1", mid="m-20261115T220000Z-112233")

    # 1. Native agent with supervisor timeout flag
    env1 = Env(tmp_path / "env1")
    sup1 = env1.supervisor()
    sup1.mission = mission
    sup1.state["mission"] = {"id": mission["id"], "next": 0, "total": 1, "done": []}
    env1.relay.argon_limits = lambda a: {"windows": [{"used_percent": 30}]} if a in ("argon", "agy_opus") else None

    sid1 = "20261115-220000-native-timeout"
    (env1.relay.sessions_dir() / sid1).mkdir(parents=True)
    pending1 = {
        "nonce": "rc-20261115T220000Z-t1",
        "agent": "argon",
        "started_at": rc.iso(rc.now_utc()),
        "timed_out": True,
        "diagnostics": "turn wall limit (3690s) exceeded",
        "attempt": 1,
        "task": 0,
    }
    report1 = sup1._settle(pending1, sid1, diagnostics="turn wall limit (3690s) exceeded", timed_out=True)
    assert isinstance(report1, rc.TurnReport)
    assert report1.outcome == "unknown"
    assert report1.harness_cause == "turn_time_limit"
    assert report1.no_evidence is True
    assert report1.timed_out is True
    assert report1.turn_id == f"{sid1}/none"
    assert report1.in_flight == []
    assert report1.effects == []
    assert rc.gate(report1) is None
    # Task is not completed
    assert sup1.state["mission"]["next"] == 0
    assert len(sup1.state["mission"]["done"]) == 0

    # Decision on report1 does not hold on output_missing
    decision1 = sup1._decide(report1, "task 1")
    assert isinstance(decision1, tuple)
    assert sup1.state.get("hold") is None

    # Journal verification
    journal1 = [
        json.loads(line) for line in sup1.store.journal_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    settle1 = next(e for e in journal1 if e.get("event") == "settle")
    assert settle1["outcome"] == "unknown"
    assert settle1["harness_cause"] == "turn_time_limit"
    assert settle1["effects"] == []
    assert settle1["in_flight"] == []

    # 2. Native agent without supervisor timeout flag: gates as output_missing
    env2 = Env(tmp_path / "env2")
    sup2 = env2.supervisor()
    sup2.mission = mission
    sup2.state["mission"] = {"id": mission["id"], "next": 0, "total": 1, "done": []}

    sid2 = "20261115-220000-native-notimeout"
    (env2.relay.sessions_dir() / sid2).mkdir(parents=True)
    pending2 = {
        "nonce": "rc-20261115T220000Z-t2",
        "agent": "argon",
        "started_at": rc.iso(rc.now_utc()),
        "timed_out": False,
        "attempt": 1,
        "task": 0,
    }
    report2 = sup2._settle(pending2, sid2, timed_out=False)
    assert isinstance(report2, rc.TurnReport)
    assert report2.outcome == "crash"
    assert report2.harness_cause is None
    assert report2.no_evidence is True
    assert report2.timed_out is False
    assert rc.gate(report2) == "output_missing"

    decision2 = sup2._decide(report2, "task 1")
    assert decision2 == "held"
    assert sup2.state["hold"]["reason"] == "output_missing"

    # 3. Non-native agent with timed_out=True does not bypass gate
    env3 = Env(tmp_path / "env3")
    sup3 = env3.supervisor()
    sup3.mission = mission
    sup3.state["mission"] = {"id": mission["id"], "next": 0, "total": 1, "done": []}

    sid3 = "20261115-220000-claude-timeout"
    (env3.relay.sessions_dir() / sid3).mkdir(parents=True)
    pending3 = {
        "nonce": "rc-20261115T220000Z-t3",
        "agent": "claude",
        "started_at": rc.iso(rc.now_utc()),
        "timed_out": True,
        "diagnostics": "turn wall limit (3600s) exceeded",
        "attempt": 1,
        "task": 0,
    }
    report3 = sup3._settle(pending3, sid3, diagnostics="turn wall limit (3600s) exceeded", timed_out=True)
    assert isinstance(report3, rc.TurnReport)
    assert report3.outcome == "crash"
    assert report3.harness_cause is None
    assert report3.no_evidence is True
    assert report3.timed_out is False
    assert rc.gate(report3) == "output_missing"

    decision3 = sup3._decide(report3, "task 1")
    assert decision3 == "held"
    assert sup3.state["hold"]["reason"] == "output_missing"

    # 4. External-effect safety: unproven in-flight external operations are parked
    env4 = Env(tmp_path / "env4")
    sup4 = env4.supervisor()
    sup4.mission = mission
    sup4.state["mission"] = {"id": mission["id"], "next": 0, "total": 1, "done": []}
    env4.relay.argon_limits = lambda a: {"windows": [{"used_percent": 30}]} if a in ("argon", "agy_opus") else None
    env4.host.probe = lambda call: None

    report4 = sup4._settle(pending1, sid1, diagnostics="turn wall limit (3690s) exceeded", timed_out=True)
    assert isinstance(report4, rc.TurnReport)
    # Attach an interrupted in-flight external operation to the report
    push_call = rc.ToolCall(id="call_push_1", tool="run_command", kind="external", label="git push", done=False)
    report4.calls.append(push_call)
    assert push_call in report4.in_flight

    decision4 = sup4._decide(report4, "task 1")
    assert isinstance(decision4, tuple)
    _agent, next_prompt = decision4
    assert "git push: UNRESOLVED (parked)" in next_prompt
    assert report4.turn_id in sup4.state.get("parked_turns", [])
    assert sup4.state["mission"]["next"] == 0
