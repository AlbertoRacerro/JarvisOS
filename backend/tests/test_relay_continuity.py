"""Spec 195: Relay provider continuity supervisor and Slack outbound monitor (offline, injected Relay)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import runpy
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "relay_continuity.py"
SPEC = importlib.util.spec_from_file_location("relay_continuity", SCRIPT)
assert SPEC and SPEC.loader
rc = importlib.util.module_from_spec(SPEC)
sys.modules["relay_continuity"] = rc
SPEC.loader.exec_module(rc)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "relay_continuity"
T0 = datetime(2026, 10, 18, 8, 0, tzinfo=UTC)
HEAD = "20261018-080000-aaaaaa"
SECRET_URL = "https://hooks.slack.com/services/T000/B000/abcdefghijklmnopqrstuvwx"


# ---------------------------------------------------------------------------
# Harness


class Clock:
    def __init__(self, start: datetime = T0):
        self.t = start
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += timedelta(seconds=seconds)


def write_turn(session_dir: Path, events: list[dict[str, Any]], *, agent: str = "claude", status: str = "propose_done",
               prompt: str = "task", number: int = 1, last_message: str | None = None,
               context_used_tokens: int | None = None) -> Path:
    turn = session_dir / "turns" / f"turn-{number:03d}"
    turn.mkdir(parents=True, exist_ok=True)
    (turn / "output.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    state_data: dict[str, Any] = {"agent_key": agent, "status": status}
    if context_used_tokens is not None:
        state_data["context_used_tokens"] = context_used_tokens
    (turn / "state.json").write_text(json.dumps(state_data))
    (turn / "prompt.md").write_text(prompt)
    if last_message is not None:
        (turn / "last-message.txt").write_text(last_message, encoding="utf-8")
    pin_to_t0(turn)
    return turn


def relay_prompt(task: str) -> str:
    """Relay's real prompt layout (v0.7.7): rules, then the task, then continuation context."""
    return (f"You are participating in a Relay-managed single-agent session.\n\n## Rules\n- Focus on the task.\n\n"
            f"## Task\n\n{task}\n\n\n## Continuation Context\n\n- This run continues prior relay session: x\n")


def copy_fixture(name: str, session_dir: Path) -> Path:
    turn = session_dir / "turns" / "turn-001"
    shutil.copytree(FIXTURES / name, turn)
    (turn / "prompt.md").write_text("fixture")
    pin_to_t0(turn)
    return turn


def pin_to_t0(turn: Path) -> None:
    """Date the turn output at the fake clock: the supervisor reads a printed reset relative to its mtime, so a
    wall-clock mtime makes reset parsing depend on the day the suite runs."""
    os.utime(turn / "output.jsonl", (T0.timestamp(), T0.timestamp()))


def claude_result(text: str = "done", *, error: bool = False, status: int | None = None) -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": error, "result": text, "api_error_status": status,
            "terminal_reason": "api_error" if error else "completed"}


def tool_use(tid: str, name: str, **inp: Any) -> dict[str, Any]:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def tool_result(tid: str) -> dict[str, Any]:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": False}]}}


SUCCESS = [claude_result("ok")]
CODEX_SUCCESS = [{"type": "thread.started"}, {"type": "turn.completed"}]
QUOTA = [claude_result("You've hit your session limit · resets 11:50pm (Europe/Rome)", error=True, status=429)]
CODEX_QUOTA = [{"type": "thread.started"}, {"type": "error", "message": "You've hit your usage limit. try again at 11:38 PM."},
               {"type": "turn.failed", "error": {"message": "You've hit your usage limit. try again at 11:38 PM."}}]
EAI = [claude_result("API Error: Can't reach the API server — check your internet or DNS (EAI_AGAIN)", error=True)]


class FakeProc:
    def __init__(self, pid: int | None):
        self.pid = pid


class FakeRelay(rc.RelayAdapter):
    """Relay runner double: each launch consumes one scripted turn and writes a real session directory."""

    def __init__(self, repo: Path, script: list[tuple[str, list[dict[str, Any]]] | None], readings: Any = None):
        super().__init__(repo)
        self.script = list(script)
        self.launches: list[dict[str, Any]] = []
        self.readings = readings
        self.clock: Clock | None = None
        (repo / ".agent-relay" / "sessions").mkdir(parents=True, exist_ok=True)
        self._conv_store = repo / ".agent-relay" / "conversations"
        self._conv_store.mkdir(parents=True, exist_ok=True)

    def conversation_store(self) -> Path:
        return self._conv_store

    def limits(self) -> dict[str, Any] | None:
        return self.readings() if callable(self.readings) else self.readings

    def start(self, agent: str, parent: str, task: str, turns: int,
              resume: str | None = None, **kwargs: Any) -> FakeProc:
        if not self.script:
            raise AssertionError("unexpected Relay invocation")
        self.launches.append({"agent": agent, "parent": parent, "task": task, "turns": turns,
                              "resume": resume, "at": self.clock.now() if self.clock else None, **kwargs})
        step = self.script.pop(0)
        self._current = (step, agent, task)
        return FakeProc(None)

    def finish(self, proc: FakeProc, timeout: float | None = None) -> Any:
        step, agent, task = self._current
        if step is None:
            return rc.RunResult(None, 1, False)
        _, events, *reported = step
        if callable(events):  # a reply that depends on the task (a diagnostic answers its own nonce)
            events = events(task)
        sid = f"20261018-{len(self.launches):06d}-{'b' * 6}"
        status = reported[0] if reported else "propose_done"
        last_msg = reported[1] if len(reported) > 1 else None
        tokens = reported[2] if len(reported) > 2 else None
        turn = write_turn(self.sessions_dir() / sid, events, agent=agent, prompt=relay_prompt(task),
                          status=status, last_message=last_msg, context_used_tokens=tokens)
        if agent in rc.NATIVE_AGENTS and not (turn / "native-result.json").exists():
            (turn / "native-result.json").write_text(
                json.dumps({"schema": "argon-coordinator.v1", "launched": True, "exit_code": 0, "quota": [], "reason": None,
                            "model": rc.relay_argon.MODEL})
            )
        return rc.RunResult(sid, 0, True)


class Env:
    def __init__(self, tmp: Path, script: list[Any] | None = None, *, readings: Any = None,
                 limits: Any = None, sender: Any = None):
        self.tmp = tmp
        self.repo = tmp / "repo"
        self.relay = FakeRelay(self.repo, script or [], readings)
        self.clock = Clock()
        self.relay.clock = self.clock
        self.fp = {"v": "fp0"}
        self.perm = {"v": "perm0"}
        self.conflicts: list[str] = []
        self.ledger: list[tuple[str, str]] = []
        self.logs: list[str] = []
        self.alive: dict[int, int] = {}
        self.sent: list[str] = []
        self.store = rc.Store(tmp / "control")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.ci: dict[tuple[int, str], dict[str, Any]] = {}
        self.host = rc.Host(
            relay=self.relay, now=self.clock.now, sleep=self.clock.sleep, fingerprint=lambda: self.fp["v"],
            conflicts=lambda own: list(self.conflicts), pid_alive=self._alive,
            ledger=lambda kind, text: self.ledger.append((kind, text)), permissions_digest=lambda: self.perm["v"],
            log=self.logs.append,
            job_status=lambda jid: self.jobs.get(jid),
            pr_ci_status=lambda pr, sha: self.ci.get((pr, sha)),
            reachable=lambda: True)
        self.limits = limits or rc.Limits()
        self.sender = sender if sender is not None else self._send

    def _alive(self, pid: int, start: int | None) -> bool:
        left = self.alive.get(pid, 0)
        if left > 0:
            self.alive[pid] = left - 1
            if left == 1:
                # The adopted Relay run finishes while we wait.
                self.relay.finish_pending()
            return True
        return False

    def _send(self, text: str) -> str | None:
        self.sent.append(text)
        return None

    def head(self, events: list[dict[str, Any]] | None = None, *, fixture: str | None = None, agent: str = "claude",
             sid: str = HEAD) -> Path:
        session = self.relay.sessions_dir() / sid
        return copy_fixture(fixture, session) if fixture else write_turn(session, events or SUCCESS, agent=agent)

    def supervisor(self, turns: int = 1) -> Any:
        return rc.Supervisor(self.store, self.host, self.limits, turns=turns, sender=self.sender)

    def run(self, sid: str = HEAD, task: str | None = None) -> tuple[str, Any]:
        sup = self.supervisor()
        return sup.run(sid, task=task), sup

    def events(self) -> list[str]:
        return [text.splitlines()[0] for text in self.sent]


def reading(agent: str, used: float, resets_at: datetime, as_of: datetime) -> dict[str, Any]:
    return {"agent_key": agent, "status": "ok", "as_of": rc.iso(as_of),
            "windows": [{"id": "session", "used_percent": used, "resets_at": rc.iso(resets_at)}]}


def room(env: Env, *agents: str, exhausted: dict[str, datetime] | None = None) -> Any:
    """Fresh limits readings at the env clock: listed agents have room, `exhausted` agents are capped until a time."""
    def readings() -> dict[str, Any]:
        now = env.clock.now()
        out = {a: reading(a, 5, now + timedelta(hours=5), now) for a in agents}
        for agent, until in (exhausted or {}).items():
            out[agent] = reading(agent, 100, until, now) if now < until else reading(agent, 5, now + timedelta(hours=5), now)
        return out
    return readings


# ---------------------------------------------------------------------------
# Classification from real recorded turns


@pytest.mark.parametrize(("fixture", "outcome", "agent", "relay_status"), [
    ("codex_usage_limit", "quota", "codex", "completed"),
    ("claude_eai_again", "transient", "claude", "completed"),
    ("claude_session_limit", "quota", "claude", "completed"),
    ("claude_session_limit_bg_killed", "quota", "claude", "completed"),
    ("claude_spend_limit", "quota", "claude", "completed"),
    ("claude_crash_no_terminal", "crash", "claude", None),
    ("claude_success", "success", "claude", "completed"),
    ("codex_success_misreported", "success", "codex", "continue"),
    ("claude_invalid_model", "unknown", "claude", "continue"),
])
def test_real_turns_classify_from_raw_evidence(fixture: str, outcome: str, agent: str, relay_status: str | None) -> None:
    report = rc.classify_turn(FIXTURES / fixture, ref=T0)
    assert (report.outcome, report.agent, report.relay_status) == (outcome, agent, relay_status)
    assert report.bad_lines == 0
    assert rc.gate(report) is None


def test_relay_completed_status_never_means_success() -> None:
    for name in ("codex_usage_limit", "claude_eai_again", "claude_session_limit", "claude_spend_limit",
                 "claude_session_limit_bg_killed"):
        state = json.loads((FIXTURES / name / "state.json").read_text())
        assert state["status"] == "completed"
        assert rc.classify_turn(FIXTURES / name, ref=T0).outcome != "success"
    misreported = json.loads((FIXTURES / "codex_success_misreported" / "state.json").read_text())
    assert "couldn't reach" in misreported["summary"]
    assert rc.classify_turn(FIXTURES / "codex_success_misreported", ref=T0).outcome == "success"


def test_missing_and_empty_output_are_crashes(tmp_path: Path) -> None:
    turn = tmp_path / "turn-001"
    turn.mkdir()
    assert rc.classify_turn(turn).outcome == "crash"
    (turn / "output.jsonl").write_text("")
    assert rc.classify_turn(turn).detail == "output empty"


def test_torn_output_is_unparseable(tmp_path: Path) -> None:
    turn = write_turn(tmp_path / "s", [tool_use("t1", "Read", file_path="/x")])
    with open(turn / "output.jsonl", "a") as handle:
        handle.write('{"type": "result", "is_err')
    report = rc.classify_turn(turn)
    assert report.outcome == "crash" and report.bad_lines == 1
    assert rc.gate(report) == "output_unparseable"


@pytest.mark.parametrize(("text", "status", "expected"), [
    ("You've hit your monthly spend limit · your session limit resets 12:40pm (Europe/Rome)", 429, "spend_limit"),
    ("You've hit your weekly limit · resets Oct 20, 9am (Europe/Rome)", 429, "quota"),
    ("Invalid API key · Please run /login", 401, "auth"),
    ("OAuth token has expired", None, "auth"),
    ("API Error: 529 Overloaded", 529, "transient"),
    ("stream disconnected before completion", None, "transient"),
    ("There's an issue with the selected model", 404, "unknown"),
])
def test_text_classification(text: str, status: int | None, expected: str) -> None:
    assert rc.classify_text(text, status) == expected


def test_reset_times_are_parsed_as_next_occurrence() -> None:
    ref = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)  # 22:00 in Rome
    assert rc.parse_reset("resets 11:50pm (Europe/Rome)", ref) == datetime(2026, 10, 18, 21, 50, tzinfo=UTC)
    assert rc.parse_reset("resets 4:20pm (Europe/Rome)", ref) == ref  # 5h40m behind: stale, due now
    assert rc.parse_reset("resets 1am (Europe/Rome)", ref) == datetime(2026, 10, 18, 23, 0, tzinfo=UTC)
    assert rc.parse_reset("resets Oct 20, 9am (Europe/Rome)", ref) == datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
    rome = ZoneInfo("Europe/Rome")
    assert rc.parse_reset("try again at 11:38 PM.", ref, rome) == datetime(2026, 10, 18, 21, 38, tzinfo=UTC)
    assert rc.parse_reset("no time here", ref) is None
    # A time-only reset slightly behind the turn's end is stale: due now, not tomorrow.
    late = datetime(2026, 10, 18, 10, 45, tzinfo=UTC)  # 12:45 in Rome
    assert rc.parse_reset("resets 12:40pm (Europe/Rome)", late) == late
    assert rc.parse_reset("resets 4:20am (Europe/Rome)", late) == datetime(2026, 10, 19, 2, 20, tzinfo=UTC)


def test_spend_cap_with_timed_reset_is_a_session_quota(tmp_path: Path) -> None:
    report = rc.classify_turn(FIXTURES / "claude_spend_limit", ref=datetime(2026, 9, 24, 9, 0, tzinfo=UTC))
    assert report.outcome == "quota" and report.reset_at == "2026-09-24T10:40:00Z"
    turn = write_turn(tmp_path / "s", [claude_result("You've hit your monthly spend limit", error=True, status=429)])
    assert rc.classify_turn(turn).outcome == "spend_limit"


@pytest.mark.parametrize(("command", "kind"), [
    ("git status && git log -1", "read_only"),
    ("cd /x && git fetch -q && gh pr checks 5", "read_only"),
    ("git push origin HEAD", "external"),
    ("(cd x && git push)", "external"),
    ("echo $(git push)", "external"),
    ("x=`gh pr merge 3`", "external"),
    ("gh pr merge 828 --merge --match-head-commit abc", "external"),
    ("gh api repos/a/b/pulls -X PATCH -f state=closed", "external"),
    ("gh api repos/a/b/pulls", "read_only"),
    ("curl -s https://example.com", "read_only"),
    ("curl -X POST -d x https://example.com", "external"),
    ("git commit -qm \"$(cat <<'EOF'\nmsg; git push\nEOF\n)\"", "local_write"),
    ("cat > f.py <<'EOF'\ngit push\nrm -rf /\nEOF", "local_write"),
    ("echo 'a; git push' | grep x", "read_only"),
    ("sed -i s/a/b/ f", "local_write"),
    ("sleep 30 &", "background"),
    ("nohup python3 run.py > log 2>&1 &", "unknown"),
    ("python3 scripts/devctx.py recover", "read_only"),
    ("python3 scripts/devctx.py note finding x", "local_write"),
    ("find . -name x -exec rm {} \\;", "unknown"),
    ("for f in a b; do cat $f; done", "read_only"),
    ("ls > out.txt", "local_write"),
    ("ls >/dev/null 2>&1", "read_only"),
])
def test_command_effect_classes(command: str, kind: str) -> None:
    assert rc.classify_command(command)[0] == kind


def test_subagent_calls_settle_with_their_parent(tmp_path: Path) -> None:
    child = tool_use("c1", "Bash", command="git push")
    child["parent_tool_use_id"] = "p1"
    turn = write_turn(tmp_path / "s", [tool_use("p1", "Agent"), child, tool_result("p1"), claude_result("ok")])
    report = rc.classify_turn(turn)
    assert report.in_flight == []
    assert {c.label for c in report.effects} == {"Agent", "git push"}


# ---------------------------------------------------------------------------
# Continuation policy


def test_choose_skips_current_coordinator_with_low_headroom(tmp_path: Path) -> None:
    env = Env(tmp_path, readings={"claude": reading("claude", 86, T0 + timedelta(days=5), T0),
                                  "codex": reading("codex", 10, T0 + timedelta(days=5), T0)})
    assert env.supervisor()._choose("claude") == "codex"


def test_choose_keeps_first_available_when_everyone_is_low(tmp_path: Path) -> None:
    env = Env(tmp_path, readings={"claude": reading("claude", 86, T0 + timedelta(days=5), T0),
                                  "codex": reading("codex", 90, T0 + timedelta(days=5), T0)})
    assert env.supervisor()._choose("claude") == "claude"


@pytest.mark.parametrize("kind", ["missing", "stale", "unusable"])
def test_choose_does_not_treat_unproven_headroom_as_low(tmp_path: Path, kind: str) -> None:
    readings = {"codex": reading("codex", 10, T0 + timedelta(days=5), T0)}
    if kind == "stale":
        readings["claude"] = reading("claude", 86, T0 + timedelta(days=5), T0 - timedelta(hours=2))
    elif kind == "unusable":
        readings["claude"] = reading("claude", 86, T0 - timedelta(hours=1), T0)
    env = Env(tmp_path, readings=readings)
    assert env.supervisor()._choose("claude") == "claude"


def test_choose_skips_native_coordinator_with_low_headroom(tmp_path: Path) -> None:
    env = Env(tmp_path, readings={"claude": reading("claude", 10, T0 + timedelta(days=5), T0)})
    env.relay.argon_limits = lambda agent: {"windows": [{"used_percent": 86}]} if agent == "argon" else None
    assert env.supervisor()._choose("argon") == "claude"


def test_claude_quota_switches_to_codex_and_goes_idle(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.relay.readings = room(env, "codex")
    env.head(fixture="claude_session_limit")
    result, sup = env.run()
    assert result == "idle"
    assert [launch["agent"] for launch in env.relay.launches] == ["codex"]
    assert env.relay.launches[0]["parent"] is None  # fresh Relay and native Codex session on takeover
    task = env.relay.launches[0]["task"]
    assert "[relay-continuity attempt rc-" in task and "devctx.py recover" in task
    assert "grants no new authority" in task
    cooldown = sup.state["cooldowns"]["claude"]
    assert cooldown["reason"] == "quota" and cooldown["source"] in ("message", "estimate")
    events = env.events()
    assert any("ha cambiato assistente" in e for e in events) and any("ha completato un passaggio" in e for e in events)
    assert sup.state["head_session"] != HEAD and sup.state["chain"][0] == HEAD


def test_provider_reported_reset_is_preferred_and_validated(tmp_path: Path) -> None:
    reset = T0 + timedelta(hours=2)
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)],
              readings={"claude": reading("claude", 100, reset, T0), "codex": reading("codex", 10, reset, T0)})
    env.head(QUOTA)
    _, sup = env.run()
    assert sup.state["cooldowns"]["claude"] == {"until": rc.iso(reset), "source": "limits", "reason": "quota",
                                                "since": rc.iso(T0), "turn": f"{HEAD}/turn-001"}


@pytest.mark.parametrize("stale", ["old_as_of", "past_reset"])
def test_stale_limit_readings_are_discarded(tmp_path: Path, stale: str) -> None:
    as_of = T0 - timedelta(hours=3) if stale == "old_as_of" else T0
    reset = T0 + timedelta(hours=2) if stale == "old_as_of" else T0 - timedelta(hours=5)
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.relay.readings = lambda: {"claude": reading("claude", 100, reset, as_of),
                                  "codex": reading("codex", 5, T0 + timedelta(hours=5), env.clock.now())}
    env.head(QUOTA)
    _, sup = env.run()
    cooldown = sup.state["cooldowns"]["claude"]
    assert cooldown["source"] in ("message", "estimate")
    assert rc.parse_ts(cooldown["until"]) > T0


def test_codex_quota_returns_to_preferred_claude(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)])
    env.relay.readings = room(env, "claude")
    env.head(fixture="codex_usage_limit")
    result, _ = env.run()
    assert result == "idle" and env.relay.launches[0]["agent"] == "claude"


@pytest.mark.parametrize("readings", ["none", "failed", "stale", "implausible"])
def test_claude_quota_fails_over_to_codex_without_any_usable_reading(tmp_path: Path, readings: str) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    entry = {"none": None,
             "failed": {"codex": {"agent_key": "codex", "status": "error"}},
             "stale": {"codex": reading("codex", 5, T0 + timedelta(hours=5), T0 - timedelta(hours=2))},
             "implausible": {"codex": reading("codex", 100, T0 + timedelta(days=30), T0)}}[readings]
    env.relay.readings = entry
    env.head(QUOTA)
    result, sup = env.run()
    # Usage metadata is optional: with no usable reading Codex is simply tried, at once, and its run decides.
    assert result == "idle"
    assert [launch["agent"] for launch in env.relay.launches] == ["codex"]
    assert env.relay.launches[0]["at"] == T0 and "codex" not in sup.state["cooldowns"]


def test_unavailable_fallback_waits_for_earliest_validated_reset(tmp_path: Path) -> None:
    codex_reset = T0 + timedelta(hours=1)

    def readings() -> dict[str, Any]:
        now = env.clock.now()
        used = 100 if now < codex_reset else 5
        return {"codex": reading("codex", used, codex_reset if now < codex_reset else now + timedelta(hours=5), now)}

    env = Env(tmp_path, [("codex", CODEX_SUCCESS)], readings=readings)
    env.head(QUOTA)  # Claude resets later (message time), Codex resets in one hour
    result, sup = env.run()
    assert result == "idle"
    assert [launch["agent"] for launch in env.relay.launches] == ["codex"]
    assert env.clock.now() >= codex_reset
    assert any("Gli assistenti non sono disponibili" in e for e in env.events())


def test_spend_limits_on_both_agents_hold(tmp_path: Path) -> None:
    spend = [claude_result("You've hit your monthly spend limit", error=True, status=429)]
    codex_spend = [{"type": "thread.started"},
                   {"type": "turn.failed", "error": {"message": "purchase more credits to continue"}}]
    env = Env(tmp_path, [("codex", codex_spend)])
    env.relay.readings = room(env, "codex")
    env.head(spend)
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["reason"] == "no_provider_available"
    assert [launch["agent"] for launch in env.relay.launches] == ["codex"]
    assert sup.state["cooldowns"]["claude"]["until"] is None and sup.state["cooldowns"]["codex"]["until"] is None


CODEX_EAI = [{"type": "thread.started"}, {"type": "turn.failed", "error": {"message": "stream error: EAI_AGAIN"}}]
CRASH = [tool_use("r", "Read", file_path="/x"), tool_result("r")]


def test_transient_failure_backs_off_that_provider_and_the_other_continues(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.head(fixture="claude_eai_again")
    result, sup = env.run()
    assert result == "idle" and [launch["agent"] for launch in env.relay.launches] == ["codex"]
    assert env.relay.launches[0]["at"] == T0  # no wait: only Claude is backing off
    assert sup.state["cooldowns"]["claude"]["reason"] == "transient"
    assert rc.parse_ts(sup.state["cooldowns"]["claude"]["until"]) == T0 + timedelta(seconds=60)


def test_an_outage_on_both_providers_waits_with_bounded_backoff_then_resumes(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_EAI), ("claude", EAI), ("codex", CODEX_SUCCESS)])
    env.head(EAI)
    result, sup = env.run()
    assert result == "idle" and sup.state["hold"] is None
    agents, at = [launch["agent"] for launch in env.relay.launches], [launch["at"] for launch in env.relay.launches]
    assert agents[0] == "codex" and set(agents) == {"claude", "codex"}
    assert at[0] == T0 and at[1] == T0 + timedelta(seconds=60)  # the first backoffs end together
    assert at[2] - at[1] <= timedelta(seconds=120)
    assert max(env.clock.slept) <= 5.0  # interruptible, never a busy loop


def test_crashes_retry_the_same_provider_until_the_failure_circuit_opens(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", CRASH)] * 2, limits=rc.Limits(max_consecutive_failures=2))
    env.relay.readings = room(env, "claude", "codex")
    env.head(CRASH)
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["reason"] == "circuit_failures"
    assert [launch["agent"] for launch in env.relay.launches] == ["claude"] * 2  # exactly the bound


def test_provider_failures_break_the_crash_streak(tmp_path: Path) -> None:
    env = Env(tmp_path, [("a", CRASH), ("a", QUOTA), ("a", CRASH), ("a", EAI), ("a", CRASH), ("a", SUCCESS)],
              limits=rc.Limits(max_consecutive_failures=2))
    env.head(CRASH)
    result, sup = env.run()
    assert result == "idle" and sup.state["hold"] is None and len(env.relay.launches) == 6


def test_serve_decides_a_legacy_mission_hold_again_once(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    root = tmp_path / "control"
    mission = mission_def("cycle", checks={0: {"kind": "coordinator_cycle"}})
    _write_mission(root, mission)
    env.head([tool_use("x", "Bash", command="python3 scripts/capture.py"), *QUOTA])
    store = rc.mission_store(root, mission["id"])
    state = rc.default_state()
    state.update({"phase": "held", "head_session": HEAD, "chain": [HEAD], "agent": "claude",
                  "permissions_digest": "perm0", "permissions_pinned": True,
                  "hold": {"reason": "live_work", "at": rc.iso(T0), "attempt": None, "turn": f"{HEAD}/turn-001"},
                  "mission": {"id": mission["id"], "next": 0, "total": 1, "done": []}})
    store.save(state, T0)
    factory = lambda s, m: rc.Supervisor(s, env.host, env.limits)  # noqa: E731
    assert rc.serve_step(root, factory, conflicts=lambda: [], now=env.clock.now) == f"ran:{mission['id']}:idle"
    assert [launch["agent"] for launch in env.relay.launches] == ["codex"]


def test_a_hold_that_stands_when_decided_again_is_not_decided_on_every_poll(tmp_path: Path) -> None:
    env = Env(tmp_path)
    root = tmp_path / "control"
    mission = mission_def("cycle")
    _write_mission(root, mission)
    write_turn(env.relay.sessions_dir() / HEAD, [])
    store = rc.mission_store(root, mission["id"])
    state = rc.default_state()
    state.update({"phase": "held", "head_session": HEAD, "chain": [HEAD], "agent": "claude",
                  "permissions_digest": "perm0", "permissions_pinned": True,
                  "hold": {"reason": "effects_uncertain", "at": rc.iso(T0), "attempt": None,
                           "turn": f"{HEAD}/turn-001"},
                  "mission": {"id": mission["id"], "next": 0, "total": 1, "done": []}})
    store.save(state, T0)
    factory = lambda s, m: rc.Supervisor(s, env.host, env.limits)  # noqa: E731
    assert rc.serve_step(root, factory, conflicts=lambda: [], now=env.clock.now) == f"ran:{mission['id']}:held"
    held = store.load()["hold"]
    assert held["reason"] == "output_missing"
    assert rc.serve_step(root, factory, conflicts=lambda: [], now=env.clock.now) == f"held:{mission['id']}"
    assert env.relay.launches == []


@pytest.mark.parametrize(("events", "agents"), [
    ([claude_result("There's an issue with the selected model", error=True, status=404)], ["claude"]),
    ([claude_result("Invalid API key · Please run /login", error=True, status=401)], ["codex"]),
])
def test_auth_failure_disables_only_that_provider_and_unknown_failure_retries(
        tmp_path: Path, events: list[Any], agents: list[str]) -> None:
    env = Env(tmp_path, [("any", SUCCESS)])
    env.head(events)
    result, sup = env.run()
    assert result == "idle" and sup.state["hold"] is None
    assert [launch["agent"] for launch in env.relay.launches] == agents
    if agents == ["codex"]:
        cooldown = sup.state["cooldowns"]["claude"]
        assert cooldown["reason"] == "auth" and cooldown["until"] is None and cooldown["source"] == "auth"
        assert any("Un assistente non è disponibile" in e for e in env.events())


def test_codex_exhausted_returns_to_recovered_claude(tmp_path: Path) -> None:
    claude_reset, codex_reset = T0 + timedelta(minutes=5), T0 + timedelta(hours=1)

    def readings() -> dict[str, Any]:
        now = env.clock.now()
        out = {"claude": reading("claude", 100 if now < claude_reset else 5,
                                 claude_reset if now < claude_reset else now + timedelta(hours=5), now)}
        out["codex"] = reading("codex", 100 if env.relay.launches else 5, codex_reset, now)
        return out

    env = Env(tmp_path, [("codex", CODEX_QUOTA), ("claude", SUCCESS)], readings=readings)
    env.head(QUOTA)
    result, sup = env.run()
    assert result == "idle" and sup.state["hold"] is None
    assert [launch["agent"] for launch in env.relay.launches] == ["codex", "claude"]
    assert env.relay.launches[1]["at"] >= claude_reset


NO_RESET_QUOTA = [claude_result("You've hit your session limit", error=True, status=429)]
CODEX_NO_RESET_QUOTA = [{"type": "thread.started"},
                        {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}]


def test_both_exhausted_without_resets_alternate_with_capped_backoff_and_never_hold(tmp_path: Path) -> None:
    # Neither provider prints a reset and no reading exists: each retry waits a doubling estimate, capped.
    env = Env(tmp_path, [("any", NO_RESET_QUOTA)] * 5 + [("any", SUCCESS)],
              limits=rc.Limits(recheck_cap=timedelta(hours=1)))
    env.head(NO_RESET_QUOTA)
    result, sup = env.run()
    assert result == "idle" and sup.state["hold"] is None
    agents = [launch["agent"] for launch in env.relay.launches]
    assert agents[0] == "codex" and len(agents) == 6 and {"claude", "codex"} == set(agents)
    at = [T0] + [launch["at"] for launch in env.relay.launches]
    assert all(b - a <= timedelta(hours=1) for a, b in zip(at, at[1:], strict=False))
    assert min(env.clock.slept) > 0 and max(env.clock.slept) <= 5.0
    assert sup.state["counters"]["consecutive_failures"] == 0  # quota never counts towards the circuit


def test_fallback_codex_returns_to_an_available_preferred_coordinator(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)])
    env.relay.readings = room(env, "claude", "codex")
    state = rc.default_state()
    state.update({"head_session": HEAD, "chain": [HEAD], "permissions_digest": "perm0", "permissions_pinned": True,
                  "agent": "codex"})
    env.store.save(state, T0)
    env.head(CODEX_SUCCESS, agent="codex")
    result, _ = env.run(task="next step")
    # The new allocation reserves Codex for delegated work when a preferred coordinator can run.
    assert result == "idle" and [launch["agent"] for launch in env.relay.launches] == ["claude"]
    assert env.relay.launches[0]["parent"] == HEAD  # Relay retains the authoritative handoff chain
    assert env.relay.launches[0]["task"].endswith("next step")


def test_misreported_success_head_needs_nothing(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.head(fixture="codex_success_misreported")
    assert env.run()[0] == "idle"
    assert env.relay.launches == []


# ---------------------------------------------------------------------------
# Side-effect gate: recovery never silently duplicates a non-idempotent operation


IN_FLIGHT = {
    "git push": tool_use("x", "Bash", command="cd /r && git push origin HEAD"),
    "gh pr merge": tool_use("x", "Bash", command="gh pr merge 1 --merge"),
    "gh pr comment": tool_use("x", "Bash", command="gh pr comment 7 --body done"),
    "mcp tool": tool_use("x", "mcp__slack__post_message", channel="c"),
    "Write": tool_use("x", "Write", file_path="/r/a.py"),
    "subagent": tool_use("x", "Agent", subagent_type="general-purpose"),
    "unknown script": tool_use("x", "Bash", command="./deploy.sh"),
    "server": tool_use("x", "Bash", command="nohup .venv/bin/python -m uvicorn app.main:app &"),
}
EXTERNAL = ("git push", "gh pr merge", "gh pr comment", "mcp tool")


@pytest.mark.parametrize("name", EXTERNAL)
@pytest.mark.parametrize(("terminal", "successor"), [("quota", "codex"), ("transient", "codex"), ("crash", "claude")])
def test_interrupted_external_operation_without_a_settling_probe_parks_and_continues(
        tmp_path: Path, name: str, terminal: str, successor: str) -> None:
    tail = {"quota": QUOTA, "transient": EAI, "crash": []}[terminal]
    env = Env(tmp_path, [("any", SUCCESS)])
    env.head([tool_use("r", "Read", file_path="/x"), tool_result("r"), IN_FLIGHT[name], *tail])
    result, sup = env.run()
    assert result == "idle" and sup.state["hold"] is None
    assert [launch["agent"] for launch in env.relay.launches] == [successor]
    task = env.relay.launches[0]["task"]
    assert rc.PARKED_TEXT in task
    assert any("ha messo da parte un'operazione non verificabile" in e for e in env.events())
    assert any(kind == "finding" and "parked interrupted external operation(s)" in text for kind, text in env.ledger)
    record = [r for r in journal(env) if r.get("event") == "reconcile"][-1]
    assert record["result"] == "effects_uncertain" and record["verdicts"][0]["verdict"] == "unresolved"


@pytest.mark.parametrize("name", sorted(set(IN_FLIGHT) - set(EXTERNAL)))
@pytest.mark.parametrize(("terminal", "successor"), [("quota", "codex"), ("transient", "codex"), ("crash", "claude")])
def test_interrupted_local_operation_continues_and_the_successor_verifies_it(
        tmp_path: Path, name: str, terminal: str, successor: str) -> None:
    tail = {"quota": QUOTA, "transient": EAI, "crash": []}[terminal]
    env = Env(tmp_path, [("any", SUCCESS)])
    env.head([tool_use("r", "Read", file_path="/x"), tool_result("r"), IN_FLIGHT[name], *tail])
    result, sup = env.run()
    assert result == "idle" and sup.state["hold"] is None
    assert [launch["agent"] for launch in env.relay.launches] == [successor]
    task = env.relay.launches[0]["task"]
    assert ": INTERRUPTED" in task and "Inspect its effects" in task and "RELAY_STATUS blocked" in task
    assert rc.PROCESSES_UNKNOWN in task  # the head turn predates this supervisor: survivors are not ruled out


def test_probe_settled_external_operations_continue_with_their_verdicts(tmp_path: Path) -> None:
    verdicts = {"git push": ("completed", "remote branch b is at the pushed commit abc"),
                "gh pr merge": ("not_started", "PR #1 is still open: the merge did not take effect")}
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.host.probe = lambda call: verdicts["git push" if "push" in call.label else "gh pr merge"]
    env.head([IN_FLIGHT["git push"], tool_use("y", "Bash", command="gh pr merge 1 --merge"), *QUOTA])
    result, sup = env.run()
    assert result == "idle" and [launch["agent"] for launch in env.relay.launches] == ["codex"]
    task = env.relay.launches[0]["task"]
    assert "git push: COMPLETED: it took effect; do not repeat it" in task
    assert "gh pr merge: NOT STARTED: it did not take effect" in task
    record = [r for r in journal(env) if r.get("event") == "reconcile"][-1]
    assert record["result"] == "settled" and {v["verdict"] for v in record["verdicts"]} == {"completed", "not_started"}
    assert any(kind == "decision" and "settled interrupted external" in text for kind, text in env.ledger)


def test_an_unsettled_external_operation_parks_even_beside_settled_ones(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.host.probe = lambda call: ("completed", "pushed") if "push" in call.label else None
    env.head([IN_FLIGHT["git push"], tool_use("y", "Bash", command="gh pr comment 7 --body done"), *QUOTA])
    result, sup = env.run()
    assert result == "idle" and sup.state["hold"] is None
    assert [launch["agent"] for launch in env.relay.launches] == ["codex"]
    task = env.relay.launches[0]["task"]
    assert "git push: COMPLETED: it took effect; do not repeat it" in task
    assert rc.PARKED_TEXT in task
    assert any("ha messo da parte un'operazione non verificabile" in e for e in env.events())
    assert any(kind == "finding" and "parked interrupted external operation(s)" in text for kind, text in env.ledger)


def test_a_failing_probe_settles_nothing(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])

    def broken(call: Any) -> Any:
        raise RuntimeError("gh unavailable")
    env.host.probe = broken
    env.head([IN_FLIGHT["gh pr merge"], *QUOTA])
    result, sup = env.run()
    assert result == "idle" and sup.state["hold"] is None
    assert [launch["agent"] for launch in env.relay.launches] == ["codex"]
    task = env.relay.launches[0]["task"]
    assert rc.PARKED_TEXT in task
    assert any("ha messo da parte un'operazione non verificabile" in e for e in env.events())


def test_processes_a_turn_leaves_running_are_stopped_and_named_to_the_successor(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", [IN_FLIGHT["server"], tool_result("x"), *QUOTA]), ("codex", CODEX_SUCCESS)])
    reaps: list[int] = []

    def reap() -> list[str]:
        reaps.append(len(env.relay.launches))
        return ["python3", "node"] if len(reaps) == 1 else []
    env.host.reap = reap
    env.head(CRASH)
    result, sup = env.run()
    assert result == "idle"
    assert reaps == [1, 2]  # after every finished turn, before anything is decided
    task = env.relay.launches[1]["task"]
    assert "stopped by the supervisor: node, python3" in task and "python &: INTERRUPTED" in task
    assert rc.PROCESSES_STOPPED in task
    settle = [r for r in journal(env) if r.get("event") == "settle"]
    assert settle[0]["reaped"] == ["node", "python3"] and settle[1]["reaped"] == []


def test_crash_after_the_repository_changed_is_retried_not_held(tmp_path: Path) -> None:
    # Local changes are the successor's to inspect; only an interrupted external operation could not be undone.
    env = Env(tmp_path, [("claude", CRASH), ("claude", SUCCESS)])
    env.head(CRASH)
    env.host.fingerprint = lambda: "fp-after" if env.relay.launches else "fp0"
    result, sup = env.run()
    assert result == "idle" and len(env.relay.launches) == 2


def test_in_flight_read_only_call_does_not_block(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.head([tool_use("x", "Bash", command="git status"), *QUOTA])
    assert env.run()[0] == "idle"


def test_completed_effects_are_reported_to_the_successor_not_replayed(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.relay.readings = room(env, "codex")
    env.head([tool_use("p", "Bash", command="git push origin HEAD"), tool_result("p"),
              tool_use("m", "Bash", command="gh pr merge 5 --merge"), tool_result("m"), *QUOTA])
    assert env.run()[0] == "idle"
    task = env.relay.launches[0]["task"]
    assert "[external] git push" in task and "[external] gh pr merge" in task
    assert "do not repeat them blindly" in task


def test_crash_without_output_holds_when_repository_changed(tmp_path: Path) -> None:
    env = Env(tmp_path, [None])  # Relay dies without creating a session
    env.head(EAI)

    def changed_fingerprint() -> str:
        return "fp-after" if env.relay.launches else "fp0"
    env.host.fingerprint = changed_fingerprint
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["reason"] == "attempt_unresolved"
    assert len(env.relay.launches) == 1


def test_attempt_without_session_holds_even_when_repository_unchanged(tmp_path: Path) -> None:
    # A remote effect (a merge, a PR comment) leaves the local fingerprint unchanged: no proof of no effect.
    env = Env(tmp_path, [None])
    env.head(EAI)
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["reason"] == "attempt_unresolved"
    assert len(env.relay.launches) == 1
    assert env.run()[0] == "held" and len(env.relay.launches) == 1


# ---------------------------------------------------------------------------
# Restart recovery and process safety


def _pending(env: Env, nonce: str, pid: int | None) -> None:
    state = rc.default_state()
    state.update({"phase": "running", "head_session": HEAD, "chain": [HEAD], "agent": "codex",
                  "permissions_digest": "perm0",
                  "pending": {"nonce": nonce, "agent": "codex", "parent": HEAD, "started_at": rc.iso(T0),
                              "pid": pid, "pid_start": 42, "fingerprint": "fp0", "attempt": 1}})
    env.store.save(state, T0)


def test_restart_during_failover_adopts_running_attempt_without_relaunch(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.head(QUOTA)
    nonce = "rc-20261018T080000Z-deadbeef"
    _pending(env, nonce, pid=4242)
    env.alive[4242] = 2

    def finish_pending() -> None:
        write_turn(env.relay.sessions_dir() / "20261018-080100-cccccc", CODEX_SUCCESS, agent="codex",
                   prompt=relay_prompt(f"[relay-continuity attempt {nonce}]"))
    env.relay.finish_pending = finish_pending  # type: ignore[attr-defined]
    result, sup = env.run()
    assert result == "idle"
    assert env.relay.launches == []
    assert sup.state["head_session"] == "20261018-080100-cccccc" and sup.state["pending"] is None


def test_restart_with_dead_attempt_and_changed_repository_holds(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.head(QUOTA)
    _pending(env, "rc-x", pid=4243)
    env.fp["v"] = "fp-changed"
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["reason"] == "attempt_unresolved"
    assert env.relay.launches == []


def test_restart_with_dead_attempt_found_by_nonce_settles(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)])
    env.head(QUOTA)
    nonce = "rc-20261018T080000Z-cafef00d"
    _pending(env, nonce, pid=None)
    write_turn(env.relay.sessions_dir() / "20261018-080030-dddddd",
               [tool_use("x", "Bash", command="git push"), *CODEX_QUOTA], agent="codex",
               prompt=relay_prompt(f"[relay-continuity attempt {nonce}]"))
    result, sup = env.run()
    assert result == "idle" and sup.state["hold"] is None
    assert [launch["agent"] for launch in env.relay.launches] == ["claude"]
    task = env.relay.launches[0]["task"]
    assert rc.PARKED_TEXT in task
    assert any("ha messo da parte un'operazione non verificabile" in e for e in env.events())
    assert any(kind == "finding" and "parked interrupted external operation(s)" in text for kind, text in env.ledger)
    assert env.relay.launches[0]["parent"] == "20261018-080030-dddddd"


def test_concurrent_supervisors_are_refused(tmp_path: Path) -> None:
    first, second = rc.Lock(tmp_path / "supervisor.lock"), rc.Lock(tmp_path / "supervisor.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_cli_run_refuses_when_lock_is_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(tmp_path))
    holder = rc.Lock(tmp_path / "supervisor.lock")
    assert holder.acquire()
    try:
        assert rc.main(["run", "--session", HEAD, "--no-notify"]) == 3
    finally:
        holder.release()


def test_live_conflicts_detects_real_relay_repl_and_legacy_autopilot(tmp_path: Path) -> None:
    repo, elsewhere = tmp_path / "repo", tmp_path / "other"
    repo.mkdir()
    elsewhere.mkdir()
    procs = [subprocess.Popen(["bash", "-c", f"exec -a {name} sleep 30"], cwd=cwd)
             for name, cwd in (("relay", repo), ("relay", elsewhere),
                               ("/home/u/jarvis-control/autopilot/watchdog.sh", elsewhere))]
    try:
        for _ in range(50):
            if all(Path(f"/proc/{p.pid}/cmdline").read_bytes().startswith((b"relay", b"/home")) for p in procs):
                break
            time.sleep(0.05)
        assert rc.live_conflicts(repo, {os.getpid()}) == ["legacy_autopilot", "repl_active"]
        assert rc.live_conflicts(elsewhere, {os.getpid(), procs[1].pid}) == ["legacy_autopilot"]
    finally:
        for proc in procs:
            proc.kill()
            proc.wait()


RELAY_SAFE = Path(__file__).resolve().parents[2] / "scripts" / "relay_safe.py"


def _lease_holder(repo: Path, *argv: str, **env: str) -> subprocess.Popen[bytes]:
    """A process that takes the coordinator lease exactly as scripts/relay_safe.py does, then idles."""
    code = ("import os, runpy, sys, time; from pathlib import Path\n"
            f"lease = runpy.run_path({str(RELAY_SAFE)!r})['hold_coordinator_lease'](sys.argv, Path.cwd(), dict(os.environ))\n"
            "Path('ready').write_text(str(lease is not None)); time.sleep(30)\n")
    proc = subprocess.Popen([sys.executable, "-c", code, *argv], cwd=repo,
                            env={**os.environ, "JARVISOS_MISSION_PROFILE": "", **env})
    deadline = time.monotonic() + 10
    while not (repo / "ready").exists() and time.monotonic() < deadline:
        assert proc.poll() is None
        time.sleep(0.01)
    return proc


@pytest.mark.parametrize(("argv", "env", "held"), [
    ((), {}, True),                                     # a REPL
    (("run", "--agent", "claude"), {}, True),           # a headless coordinator run
    (("daemon",), {}, False),
    (("hook", "pre-tool"), {}, False),
    (("run",), {"JARVISOS_MISSION_PROFILE": "1"}, False),  # the mission owner's own turn
])
def test_coordinator_lease_is_seen_across_pid_namespaces(
    tmp_path: Path, argv: tuple[str, ...], env: dict[str, str], held: bool,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    assert rc.coordinator_lease_held(repo) is False  # no lease file yet
    proc = _lease_holder(repo, *argv, **env)
    try:
        assert (repo / "ready").read_text() == str(held)
        # Excluding the holder's pid stands in for another sandbox instance, whose process table is invisible.
        assert rc.live_conflicts(repo, {os.getpid(), proc.pid}) == (["repl_active"] if held else [])
    finally:
        proc.kill()
        proc.wait(timeout=5)
    assert rc.coordinator_lease_held(repo) is False  # the kernel released it with the process


def test_coordinator_lease_found_from_a_subdirectory_and_never_outside_a_repository(tmp_path: Path) -> None:
    safe = runpy.run_path(str(RELAY_SAFE))
    assert safe["hold_coordinator_lease"](["relay"], tmp_path, {}) is None
    (tmp_path / "repo/.git").mkdir(parents=True)
    (tmp_path / "repo/a/b").mkdir(parents=True)
    lease = safe["hold_coordinator_lease"](["relay"], tmp_path / "repo/a/b", {})
    try:
        assert lease is not None and (tmp_path / "repo/.agent-relay/locks/coordinator.lease").exists()
    finally:
        lease.close()


@pytest.mark.parametrize("entry", ["relay", "agent-relay", "relay_safe.py", "unrelated.py"])
@pytest.mark.parametrize("mode", ["interactive", "daemon", "hook"])
def test_python_relay_launchers_are_detected_without_claiming_unrelated_python(
    tmp_path: Path, entry: str, mode: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    script = tmp_path / entry
    ready = script.with_suffix(".ready")
    script.write_text(
        "from pathlib import Path\nimport time\n"
        "Path(__file__).with_suffix('.ready').touch()\ntime.sleep(30)\n"
    )
    args = ["--no-tmux", "--no-onboarding"] if mode == "interactive" else [mode]
    proc = subprocess.Popen([sys.executable, str(script), *args], cwd=repo)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(0.01)
        assert ready.exists()
        recognized = entry != "unrelated.py"
        assert rc.live_conflicts(repo, {os.getpid()}) == (
            ["repl_active"] if recognized and mode == "interactive" else []
        )
        assert rc.live_conflicts(repo, {os.getpid(), proc.pid}) == []
    finally:
        proc.kill()
        proc.wait(timeout=5)


@pytest.mark.parametrize("interpreter", [None, "python", "python3", "python3.12"])
def test_relay_safe_direct_or_python_entry_keeps_command_positions(interpreter: str | None) -> None:
    command = ["/opt/tools/relay_safe.py", "--no-tmux", "daemon"]
    argv = [f"/opt/venv/bin/{interpreter}", *command] if interpreter else command
    assert rc._relay_argv(argv) == command


def test_live_repl_on_the_repository_defers_launch_without_a_hold(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.conflicts = ["repl_active"]
    env.head(QUOTA)
    result, sup = env.run()
    assert result == "waiting" and sup.state["hold"] is None and sup.state["phase"] == "waiting"
    assert env.relay.launches == []
    env.conflicts = []  # the REPL exited: the same decision is taken again, once
    result, sup = env.run()
    assert result == "idle" and [launch["agent"] for launch in env.relay.launches] == ["codex"]
    assert sup.state["counters"]["consecutive_failures"] == 0


def test_permission_change_holds(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.head(SUCCESS)
    assert env.run()[0] == "idle"
    env.perm["v"] = "perm-widened"
    env.head(QUOTA, sid="20261018-090000-eeeeee")
    result, sup = env.run("20261018-090000-eeeeee")
    assert result == "held" and sup.state["hold"]["reason"] == "permissions_changed"
    assert env.relay.launches == []


def test_permission_digest_ignores_model_but_not_authority(tmp_path: Path) -> None:
    path = tmp_path / "permissions.toml"
    base = '[claude.allow]\npatterns = []\n\n[claude.backend]\nmode = "default"\nmodel = "opus"\n'
    path.write_text(base)
    digest = rc.permissions_digest(path)
    path.write_text(base.replace('"opus"', '"haiku"'))
    assert rc.permissions_digest(path) == digest
    path.write_text(base.replace('"default"', '"bypassPermissions"'))
    assert rc.permissions_digest(path) != digest
    path.write_text(base.replace("patterns = []", 'patterns = ["Bash(*)"]'))
    assert rc.permissions_digest(path) != digest
    path.write_text("not = [valid")
    assert rc.permissions_digest(path) == "unreadable"


def test_interrupted_state_write_keeps_previous_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = rc.Store(tmp_path)
    good = rc.default_state()
    good["head_session"] = HEAD
    store.save(good, T0)

    def crash(*_args: Any) -> None:
        raise OSError("power loss")
    monkeypatch.setattr(rc.os, "replace", crash)
    broken = rc.default_state()
    broken["head_session"] = "20261018-999999-ffffff"
    with pytest.raises(OSError):
        store.save(broken, T0)
    monkeypatch.undo()
    assert store.load()["head_session"] == HEAD


@pytest.mark.parametrize("content", ["{not json", json.dumps({"schema": "other"}),
                                     json.dumps({"schema": rc.SCHEMA, "phase": "bogus"})])
def test_corrupt_state_holds_without_acting(tmp_path: Path, content: str) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.head(QUOTA)
    env.store.root.mkdir(parents=True, exist_ok=True)
    env.store.state_path.write_text(content)
    result, _ = env.run()
    assert result == "held" and env.relay.launches == []
    assert env.store.state_path.read_text() == content
    assert any("JarvisOS ha messo in pausa il lavoro e attende una decisione in Jarvis." in text for text in env.sent)


def test_stop_request_and_resume(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.head(QUOTA)
    env.store.root.mkdir(parents=True, exist_ok=True)
    env.store.stop_path.write_text("now")
    result, sup = env.run()
    assert result == "stopped" and env.relay.launches == []
    assert sup.state["phase"] == "stopped"
    env.store.stop_path.unlink()
    result, _ = env.run()
    assert result == "idle" and len(env.relay.launches) == 1


def test_release_clears_hold_and_records_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path)
    write_turn(env.relay.sessions_dir() / HEAD, [])
    assert env.run()[0] == "held"
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(env.store.root))
    monkeypatch.setattr(rc, "make_host", lambda repo: env.host)
    monkeypatch.setattr(rc, "repo_root", lambda: env.repo)
    assert rc.main(["release", "--note", "verified push landed on origin; safe to continue"]) == 0
    state = env.store.load()
    assert state["hold"] is None and state["phase"] == "idle"
    assert "release" in env.store.journal_path.read_text()
    assert any(kind == "decision" for kind, _ in env.ledger)
    assert rc.main(["release", "--note", "token ghp_abcdefghijklmnopqrstuvwxyz0123"]) == 2


def test_released_turn_is_continued_not_regated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)])
    write_turn(env.relay.sessions_dir() / HEAD, [])  # empty output holds with output_missing
    assert env.run()[0] == "held"
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(env.store.root))
    monkeypatch.setattr(rc, "make_host", lambda repo: env.host)
    monkeypatch.setattr(rc, "repo_root", lambda: env.repo)
    assert rc.main(["release", "--note", "model fixed in permissions; continue"]) == 0
    result, sup = env.run(task="Retry the step")
    assert result == "idle"
    assert [launch["agent"] for launch in env.relay.launches] == ["claude"]
    assert env.relay.launches[0]["task"].endswith("Retry the step")
    assert sup.state["acknowledged"] is None
    # The acknowledgement covers only that turn: a new failure on the chain is gated again.
    env.relay.script = [("claude", [])]
    assert env.run(task="again")[0] == "held"


# ---------------------------------------------------------------------------
# Live work: detached jobs that outlive the call that launched them


def test_task_notification_never_closes_command_level_detachment(tmp_path: Path) -> None:
    """A notification proves the launching shell ended, not the job it detached."""
    events = [tool_use("x", "Bash", command="npm run dev >/tmp/dev.log 2>&1 &", run_in_background=True),
              tool_result("x"),
              {"type": "system", "subtype": "task_started", "task_id": "t1", "tool_use_id": "x"},
              {"type": "system", "subtype": "task_notification", "task_id": "t1", "tool_use_id": "x",
               "status": "completed"}, *QUOTA]
    turn = write_turn(tmp_path / "s", events)
    assert [c.label for c in rc.classify_turn(turn).in_flight] == ["npm &"]
    plain = [tool_use("y", "Bash", command="python -m pytest -q", run_in_background=True), tool_result("y"),
             {"type": "system", "subtype": "task_started", "task_id": "t2", "tool_use_id": "y"},
             {"type": "system", "subtype": "task_notification", "task_id": "t2", "tool_use_id": "y",
              "status": "completed"}, *QUOTA]
    assert rc.classify_turn(write_turn(tmp_path / "s2", plain)).in_flight == []


# ---------------------------------------------------------------------------
# Slack outbound (Phase 2)


def test_render_accepts_only_allowlisted_validated_fields() -> None:
    assert rc.render("turn_failed", {"session": HEAD, "agent": "claude", "outcome": "quota", "action": "switch"})
    assert rc.render("turn_failed", {"session": HEAD, "agent": "claude", "outcome": "quota", "action": "switch",
                                     "detail": "raw transcript"}) is None
    assert rc.render("hold", {"session": "rm -rf /", "reason": "effects_uncertain", "attempt": "1"}) is None
    assert rc.render("master_advanced", {"sha": "ghp_abcdefghijklmnopqrstuvwxyz0123"}) is None
    assert rc.render("hold", {"session": HEAD, "reason": "free text reason", "attempt": "1"}) is None
    assert rc.render("not_an_event", {}) is None


@pytest.mark.parametrize("failure", ["raises", "error", "timeout"])
def test_webhook_failure_never_interrupts_supervision(tmp_path: Path, failure: str) -> None:
    def sender(_text: str) -> str | None:
        if failure == "raises":
            raise RuntimeError(SECRET_URL)
        return "http_500" if failure == "error" else "TimeoutError"
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)], sender=sender)
    env.head(QUOTA)
    result, sup = env.run()
    assert result == "idle" and len(env.relay.launches) == 1
    assert sup.state["notify"]["failures"] >= 1
    assert SECRET_URL not in env.store.state_path.read_text()
    assert all(SECRET_URL not in line for line in env.logs)


def test_notifications_are_deduplicated_and_rate_limited() -> None:
    state = rc.default_state()
    sent: list[str] = []
    notifier = rc.Notifier(state, sender=lambda t: sent.append(t), log=lambda m: None, max_per_hour=3)
    for _ in range(3):
        notifier.send("turn_completed", T0, session=HEAD, agent="claude")
    assert len(sent) == 1
    for i in range(5):
        notifier.send("master_advanced", T0 + timedelta(minutes=i), sha=f"{i:07d}abc")
    assert len(sent) == 3 and state["notify"]["dropped"] >= 3
    notifier.send("master_advanced", T0 + timedelta(hours=2), sha="fffffff")
    assert len(sent) == 4


def test_failed_delivery_is_retried_at_most_once() -> None:
    state = rc.default_state()
    calls: list[str] = []
    notifier = rc.Notifier(state, sender=lambda t: calls.append(t) or "http_500", log=lambda m: None)
    notifier.send("turn_completed", T0, session=HEAD, agent="claude")
    assert state["notify"]["retry"] is not None
    notifier.retry_pending(T0 + timedelta(minutes=5))
    notifier.retry_pending(T0 + timedelta(minutes=10))
    assert len(calls) == 2 and state["notify"]["retry"] is None


def test_post_webhook_never_exposes_the_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = tmp_path / "hook"
    hook.write_text(SECRET_URL + "\n")

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise urllib.error.URLError(f"failed to reach {SECRET_URL}")
    monkeypatch.setattr(rc._WEBHOOK_OPENER, "open", boom)
    error = rc.post_webhook("hello", webhook_file=hook)
    assert error == "URLError" and SECRET_URL not in error

    def http_error(*_args: Any, **_kwargs: Any) -> Any:
        raise urllib.error.HTTPError(SECRET_URL, 404, "no_service", {}, None)  # type: ignore[arg-type]
    monkeypatch.setattr(rc._WEBHOOK_OPENER, "open", http_error)
    assert rc.post_webhook("hello", webhook_file=hook) == "http_404"
    hook.write_text("https://evil.example/collect")
    assert rc.post_webhook("hello", webhook_file=hook) == "webhook_invalid"
    assert rc.post_webhook("hello", webhook_file=tmp_path / "missing") == "webhook_missing"


@pytest.mark.parametrize(("url", "valid"), [
    (SECRET_URL, True), ("http://hooks.slack.com/services/T/B/x", False),
    ("https://hooks.slack.com.evil.example/services/T/B/x", False), ("https://hooks.slack.com:8443/services/T/B/x", False),
    ("https://u:p@hooks.slack.com/services/T/B/x", False), ("https://hooks.slack.com/@evil.example/services/x", False),
    ("https://hooks.slack.com/services/T/B/x?redirect=1", False), ("https://hooks.slack.com:bad/services/x", False),
])
def test_webhook_url_is_strictly_validated(url: str, valid: bool) -> None:
    assert rc._valid_webhook_url(url) is valid


def test_webhook_transport_rejects_redirects_and_ignores_proxies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handlers = {type(h) for h in rc._WEBHOOK_OPENER.handlers}
    assert rc._RejectRedirects in handlers and urllib.request.HTTPRedirectHandler not in handlers
    # The explicit empty ProxyHandler replaces the default environment-proxy handler and installs no proxy route.
    assert not any(isinstance(h, urllib.request.ProxyHandler) for h in rc._WEBHOOK_OPENER.handlers)
    request = urllib.request.Request(SECRET_URL, data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        rc._RejectRedirects().redirect_request(request, None, 302, "Found", {}, "https://evil.example/x")
    assert excinfo.value.code == 302

    def redirected(*_args: Any, **_kwargs: Any) -> Any:
        raise urllib.error.HTTPError(SECRET_URL, 302, "redirect rejected", {}, None)  # type: ignore[arg-type]
    hook = tmp_path / "hook"
    hook.write_text(SECRET_URL)
    monkeypatch.setattr(rc._WEBHOOK_OPENER, "open", redirected)
    assert rc.post_webhook("hello", webhook_file=hook) == "http_302"


def test_world_readable_webhook_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = tmp_path / "hook"
    hook.write_text(SECRET_URL)
    os.chmod(hook, 0o644)
    monkeypatch.setattr(rc, "WEBHOOK_FILE", hook)
    assert rc.webhook_sender() is None
    os.chmod(hook, 0o600)
    assert rc.webhook_sender() is not None


def test_every_supervisor_message_is_template_only(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.head([tool_use("p", "Bash", command="git push https://user:pw@host/repo"), tool_result("p"), *QUOTA])
    env.run()
    assert env.sent
    for text in env.sent:
        assert "git push" not in text and "pw@" not in text and "/home" not in text
        assert text in {title for title, _ in rc.EVENTS.values()}


def test_supervisor_templates_do_not_expose_machine_fields() -> None:
    for event, (_, fields) in rc.EVENTS.items():
        values = {key: {
            "session": HEAD, "mission": "m-20261104T120000Z-aabbcc", "sha": "abcdef0123456789",
            "task": "1/3", "pr": "123", "agent": "claude", "from_agent": "codex",
            "outcome": "quota", "action": "switch", "reason": "effects_uncertain", "phase": "idle",
            "reset_at": "none", "next_check_at": "none", "reset_source": "limits",
            "attempt": "1", "owner": "control-plane", "count": "2",
        }[key] for key in fields}
        if event == "roadmap_paused":
            values["reason"] = "no_progress"
        rendered = rc.render(event, values)
        assert rendered is not None, event
        assert HEAD not in rendered and "abcdef0" not in rendered and "m-2026" not in rendered
        assert "/" not in rendered and "`" not in rendered


def test_watch_notifies_repl_failures_once_and_master_merges(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sessions = repo / ".agent-relay" / "sessions"
    write_turn(sessions / HEAD, QUOTA)
    write_turn(sessions / "20261018-070000-ababab", SUCCESS)
    write_turn(sessions / "20261018-075000-cdcdcd", EAI)
    state = rc.default_state()
    sent: list[str] = []
    notifier = rc.Notifier(state, sender=lambda t: sent.append(t), log=lambda m: None)
    now = datetime.now(UTC)
    master = iter([("a" * 40, None), ("b" * 40, "830")])
    rc.watch_once(state, notifier, repo, now, supervised={"20261018-075000-cdcdcd"}, master=lambda: next(master))
    rc.watch_once(state, notifier, repo, now, supervised=set(), master=lambda: next(master))
    failed = [t for t in sent if "Un tentativo interattivo non è riuscito" in t]
    assert len(failed) == 1 and HEAD not in failed[0]
    merged = [t for t in sent if "Una modifica è stata integrata" in t]
    assert len(merged) == 1 and "830" not in merged[0]


def test_watch_alerts_when_claude_repl_effective_mode_is_dont_ask(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sessions = repo / ".agent-relay" / "sessions"
    write_turn(sessions / HEAD, [
        {"type": "system", "subtype": "init", "permissionMode": "dontAsk"}, *SUCCESS,
    ])
    write_turn(sessions / "20261018-070000-ababab", [
        {"type": "system", "subtype": "init", "permissionMode": "default"}, *SUCCESS,
    ])
    state = rc.default_state()
    sent: list[str] = []
    notifier = rc.Notifier(state, sender=lambda text: sent.append(text), log=lambda message: None)
    now = datetime.now(UTC)
    for _ in range(2):
        rc.watch_once(state, notifier, repo, now, supervised=set(), master=lambda: (None, None))
    alerts = [text for text in sent if "potrebbe non poter eseguire comandi" in text]
    assert len(alerts) == 1 and HEAD not in alerts[0]


# ---------------------------------------------------------------------------
# Regressions from the independent review of the first implementation head


def test_restart_with_dead_unfound_attempt_holds_even_when_repository_unchanged(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.head(QUOTA)
    _pending(env, "rc-20261018T080000Z-0badf00d", pid=4244)
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["reason"] == "attempt_unresolved"
    assert env.relay.launches == []


def test_nonce_lookup_ignores_sessions_that_merely_quote_it(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)])
    env.head(QUOTA)
    nonce = "rc-20261018T080000Z-0ddba11e"
    _pending(env, nonce, pid=None)
    # The real attempt pushed and was cut off; a later session's prompt quotes the nonce and failed harmlessly.
    write_turn(env.relay.sessions_dir() / "20261018-080030-ffffff",
               [tool_use("x", "Bash", command="git push"), *CODEX_QUOTA], agent="codex",
               prompt=relay_prompt(f"[relay-continuity attempt {nonce}]\ntask"))
    write_turn(env.relay.sessions_dir() / "20261018-080500-aaaaab", EAI,
               prompt=relay_prompt(f"[relay-continuity attempt rc-20261018T080500Z-12345678]\nAudit of {nonce}:\n"
                                   f"[relay-continuity attempt {nonce}]"))
    assert env.relay.find_by_nonce(nonce, T0) == "20261018-080030-ffffff"
    result, sup = env.run()
    assert result == "idle" and sup.state["hold"] is None
    assert [launch["agent"] for launch in env.relay.launches] == ["claude"]
    task = env.relay.launches[0]["task"]
    assert rc.PARKED_TEXT in task
    assert any("ha messo da parte un'operazione non verificabile" in e for e in env.events())
    assert any(kind == "finding" and "parked interrupted external operation(s)" in text for kind, text in env.ledger)
    assert env.relay.launches[0]["parent"] == "20261018-080030-ffffff"


def test_nonce_lookup_refuses_ambiguous_matches(tmp_path: Path) -> None:
    env = Env(tmp_path)
    nonce = "rc-20261018T080000Z-abad1dea"
    for sid in ("20261018-080030-aaaaaa", "20261018-080040-bbbbbb"):
        write_turn(env.relay.sessions_dir() / sid, SUCCESS, prompt=relay_prompt(f"[relay-continuity attempt {nonce}]"))
    assert env.relay.find_by_nonce(nonce, T0) is None


@pytest.mark.parametrize("command", [
    "env git push origin HEAD", "env -i PATH=/bin git push", "awk 'BEGIN {system(\"git push\")}'",
    "awk '{print > \"out\"}' f", "curl -XPOST https://example.invalid", "curl -sSd@body https://example.invalid",
    "curl --request=DELETE https://example.invalid", "gh api repos/o/r/issues -ftitle=hello",
    "gh api -XPATCH repos/o/r", "gh api graphql -f query=q", "cat <<EOF > notes\n$(git push)\nEOF",
    "cat <<<x\ngh pr merge 1", "sed 's/a/b/e' f", "sed -n '/x/w out' f", "find . -fprint list",
])
def test_execution_capable_arguments_are_not_read_only(command: str) -> None:
    assert rc.classify_command(command)[0] != "read_only"


@pytest.mark.parametrize("command", [
    "env", "awk '{print $1}' f", "curl -sSL -o /dev/null https://example.invalid", "gh api repos/o/r",
    "cat <<'EOF'\n$(git push)\nEOF", "sed 's/x/error/' f", "sed -n 1,5p f",
])
def test_plain_reads_stay_read_only(command: str) -> None:
    assert rc.classify_command(command)[0] == "read_only"


def test_background_acknowledgement_is_not_completion(tmp_path: Path) -> None:
    bg_agent = tool_use("a", "Agent", subagent_type="general-purpose", run_in_background=True)
    child = tool_use("c", "Bash", command="git push")
    child["parent_tool_use_id"] = "a"
    # Real Claude stream-json shapes: tasks are keyed by task_id; only backgrounded ones stay open.
    started = {"type": "system", "subtype": "task_started", "task_id": "t-a", "tool_use_id": "a",
               "is_backgrounded": True, "task_type": "local_agent"}
    turn = write_turn(tmp_path / "s", [bg_agent, tool_result("a"), started, child, *QUOTA])
    report = rc.classify_turn(turn)
    assert {c.label for c in report.in_flight} == {"Agent &", "git push"}
    assert any(rc.external(c) for c in report.in_flight)
    done = write_turn(tmp_path / "t", [bg_agent, tool_result("a"), started,
                                       {"type": "system", "subtype": "task_notification", "task_id": "t-a",
                                        "tool_use_id": "a", "status": "completed"}, *QUOTA])
    assert rc.classify_turn(done).in_flight == []
    updated = write_turn(tmp_path / "v", [bg_agent, tool_result("a"), started,
                                          {"type": "system", "subtype": "task_updated", "task_id": "t-a",
                                           "patch": {"status": "killed"}}, *QUOTA])
    assert rc.classify_turn(updated).in_flight == []
    # A foreground task reports task_started too; its tool result completes it.
    fg = write_turn(tmp_path / "w", [tool_use("f", "Bash", command="./build.sh"),
                                     {"type": "system", "subtype": "task_started", "task_id": "t-f", "tool_use_id": "f",
                                      "is_backgrounded": False}, tool_result("f"), *QUOTA])
    assert rc.classify_turn(fg).in_flight == []
    # ... unless it was moved to the background while running.
    moved = write_turn(tmp_path / "x", [tool_use("f", "Bash", command="./build.sh"),
                                        {"type": "system", "subtype": "task_started", "task_id": "t-f", "tool_use_id": "f",
                                         "is_backgrounded": False},
                                        {"type": "system", "subtype": "task_updated", "task_id": "t-f",
                                         "patch": {"is_backgrounded": True}}, tool_result("f"), *QUOTA])
    assert [c.label for c in rc.classify_turn(moved).in_flight] == ["build.sh"]
    # A background shell job that was never reported finished is still running.
    shell = write_turn(tmp_path / "u", [tool_use("b", "Bash", command="./deploy.sh", run_in_background=True),
                                        tool_result("b"), *QUOTA])
    assert [c.label for c in rc.classify_turn(shell).in_flight] == ["deploy.sh &"]


def test_creating_permissions_after_an_absent_pin_holds(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.perm["v"] = None
    env.head(SUCCESS)
    assert env.run()[0] == "idle"
    env.perm["v"] = "perm-created"
    env.head(QUOTA, sid="20261018-090000-eeeeee")
    result, sup = env.run("20261018-090000-eeeeee")
    assert result == "held" and sup.state["hold"]["reason"] == "permissions_changed"
    assert env.relay.launches == []


def test_released_permission_hold_repins_the_reviewed_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.relay.readings = room(env, "codex")
    env.head(SUCCESS)
    assert env.run()[0] == "idle"
    env.perm["v"] = "perm-widened"
    env.head(QUOTA, sid="20261018-090000-eeeeee")
    assert env.run("20261018-090000-eeeeee")[0] == "held"
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(env.store.root))
    monkeypatch.setattr(rc, "make_host", lambda repo: env.host)
    monkeypatch.setattr(rc, "repo_root", lambda: env.repo)
    assert rc.main(["release", "--note", "reviewed the new permissions"]) == 0
    assert env.run("20261018-090000-eeeeee")[0] == "idle"
    assert env.store.load()["permissions_digest"] == "perm-widened"


@pytest.mark.parametrize("events", [
    # A subagent's successful result after the coordinator's own failure decides nothing.
    [*QUOTA, {**claude_result("child ok"), "parent_tool_use_id": "p1"}],
    # Missing or null is_error is not success.
    [{"type": "result", "subtype": "success", "result": "x", "terminal_reason": "completed"}],
    [{"type": "result", "subtype": "success", "is_error": None, "result": "x", "terminal_reason": "completed"}],
    # An empty result without a completed reason is not success.
    [{"type": "result", "subtype": "success", "is_error": False, "result": "", "terminal_reason": None}],
])
def test_ambiguous_terminal_evidence_is_never_success(tmp_path: Path, events: list[Any]) -> None:
    assert rc.classify_turn(write_turn(tmp_path / "s", events)).outcome != "success"


def test_auth_diagnostics_without_terminal_disable_that_provider(tmp_path: Path) -> None:
    turn = write_turn(tmp_path / "s", [{"type": "system", "subtype": "init"}])
    assert rc.classify_turn(turn).outcome == "crash"
    assert rc.classify_turn(turn, diagnostics="Error: Invalid API key · Please run /login").outcome == "auth"
    (turn / "stderr.log").write_text("OAuth token has expired")
    assert rc.classify_turn(turn).outcome == "auth"

    class AuthRelay(FakeRelay):
        def finish(self, proc: FakeProc) -> Any:
            result = super().finish(proc)
            return rc.RunResult(result.session_id, 1, True, "401 Unauthorized: please run /login")
    env = Env(tmp_path / "e")
    env.relay = AuthRelay(env.repo, [("claude", [{"type": "system", "subtype": "init"}]), ("codex", CODEX_SUCCESS)])
    env.relay.clock = env.clock
    env.host.relay = env.relay
    env.head(CRASH)
    result, sup = env.run()
    # Never a retryable crash: Claude's login is broken, so Claude backs off and Codex continues.
    assert result == "idle" and [launch["agent"] for launch in env.relay.launches] == ["claude", "codex"]
    assert sup.state["cooldowns"]["claude"]["reason"] == "auth"


@pytest.mark.parametrize("drop", ["pending", "counters", "permissions_pinned"])
def test_structurally_incomplete_state_is_corrupt_not_repaired(tmp_path: Path, drop: str) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.head(QUOTA)
    state = rc.default_state()
    state.update({"phase": "running", "head_session": HEAD, "chain": [HEAD]})
    del state[drop]
    env.store.root.mkdir(parents=True, exist_ok=True)
    env.store.state_path.write_text(json.dumps(state))
    result, _ = env.run()
    assert result == "held" and env.relay.launches == []
    assert any("JarvisOS ha messo in pausa il lavoro e attende una decisione in Jarvis." in text for text in env.sent)


def test_journal_launch_missing_from_state_holds(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.relay.readings = room(env, "codex")
    env.head(QUOTA)
    env.store.journal({"event": "launch", "nonce": "rc-20261018T075900Z-feedface", "agent": "codex"})
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["reason"] == "attempt_unresolved"
    assert env.relay.launches == []
    assert rc.unsettled_nonces(env.store.journal_path) == ["rc-20261018T075900Z-feedface"]


def test_release_with_drop_pending_settles_unaccounted_journal_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.relay.readings = room(env, "codex")
    env.head(QUOTA)
    env.store.journal({"event": "launch", "nonce": "rc-20261018T075900Z-feedface", "agent": "codex"})
    assert env.run()[0] == "held"
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(env.store.root))
    monkeypatch.setattr(rc, "make_host", lambda repo: env.host)
    monkeypatch.setattr(rc, "repo_root", lambda: env.repo)
    assert rc.main(["release", "--drop-pending", "--note", "checked: that attempt never ran"]) == 0
    assert rc.unsettled_nonces(env.store.journal_path) == []
    assert env.run()[0] == "idle" and [launch["agent"] for launch in env.relay.launches] == ["codex"]


def test_retried_delivery_counts_against_the_hourly_limit() -> None:
    state = rc.default_state()
    calls: list[str] = []
    outcomes = iter(["http_500", None, None])
    notifier = rc.Notifier(state, sender=lambda t: calls.append(t) or next(outcomes), log=lambda m: None, max_per_hour=1)
    notifier.send("turn_completed", T0, session=HEAD, agent="claude")  # fails, queued for one retry
    notifier.send("master_advanced", T0 + timedelta(minutes=1), sha="abcdef1")  # fills the hour
    notifier.retry_pending(T0 + timedelta(minutes=2))
    assert len(calls) == 2 and state["notify"]["dropped"] >= 1


def test_fixtures_carry_no_coordinator_narrative() -> None:
    placeholder = "<coordinator narrative removed from fixture>"
    for path in FIXTURES.glob("*/output.jsonl"):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            if event.get("type") == "result" and event.get("is_error") is False:
                assert event.get("result") in (placeholder, ""), path
    for path in FIXTURES.glob("*/state.json"):
        summary = json.loads(path.read_text()).get("summary") or ""
        assert summary == placeholder or rc.classify_text(summary, None) != "unknown" or len(summary) < 40, path


# ---------------------------------------------------------------------------
# Regressions from the independent Gemini audit of the historical corpus


def test_turn_that_kept_the_previous_output_has_no_evidence_and_holds(tmp_path: Path) -> None:
    # Relay's "No response captured" turn keeps a byte-identical copy of the previous turn's output, while
    # the agent may have worked (and merged) for hours. It must never inherit the previous outcome.
    env = Env(tmp_path)
    session = env.relay.sessions_dir() / HEAD
    write_turn(session, QUOTA, number=1)
    second = write_turn(session, QUOTA, number=2)
    report = rc.classify_turn(second)
    assert (report.outcome, report.no_evidence, report.calls) == ("crash", True, [])
    assert rc.gate(report) == "output_missing"
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["reason"] == "output_missing"
    assert env.relay.launches == []


@pytest.mark.parametrize("content", [None, ""])
def test_missing_or_empty_output_holds(tmp_path: Path, content: str | None) -> None:
    env = Env(tmp_path)
    turn = write_turn(env.relay.sessions_dir() / HEAD, [])
    if content is None:
        (turn / "output.jsonl").unlink()
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["reason"] == "output_missing"
    assert env.relay.launches == []


def test_spend_cap_that_survives_its_session_reset_becomes_a_manual_cap(tmp_path: Path) -> None:
    capped = [claude_result("You've hit your monthly spend limit · raise it at claude.ai/settings/usage · "
                            "your session limit resets 11:50pm (Europe/Rome)", error=True, status=429)]
    # Codex is spend-capped, so the chain waits for Claude's printed reset and retries Claude, which
    # hits the same spend cap again (as in the 2026-09-24 corpus): that is a real cap, held for the maintainer.
    env = Env(tmp_path, [("claude", capped)])
    codex_spend_capped(env)
    env.head(capped)
    result, sup = env.run()
    assert [launch["agent"] for launch in env.relay.launches] == ["claude"]
    assert result == "held" and sup.state["hold"]["reason"] == "no_provider_available"
    assert sup.state["cooldowns"]["claude"]["until"] is None
    assert sup.state["cooldowns"]["claude"]["reason"] == "spend_limit"


def test_success_clears_the_spend_cap_memory(tmp_path: Path) -> None:
    capped = [claude_result("You've hit your monthly spend limit · your session limit resets 11:50pm (Europe/Rome)",
                            error=True, status=429)]
    env = Env(tmp_path, [("claude", SUCCESS)])
    codex_spend_capped(env)
    env.head(capped)
    result, sup = env.run()
    assert result == "idle" and sup.state["spend_caps"] == {}


def test_real_corpus_no_response_turn_shape() -> None:
    # The fixture pair is synthetic, but the rule mirrors corpus turns d31beb/turn-013 and bb14c2/turn-012.
    assert rc.same_output_as_previous_turn(FIXTURES / "claude_success") is False


def test_empty_reasonless_result_never_overrides_a_real_terminal(tmp_path: Path) -> None:
    artifact = {"type": "result", "subtype": "success", "is_error": False, "result": "", "terminal_reason": None}
    assert rc.classify_turn(write_turn(tmp_path / "a", [*EAI, artifact])).outcome == "transient"
    assert rc.classify_turn(write_turn(tmp_path / "b", [*QUOTA, artifact])).outcome == "quota"
    # Alone, it is still not success.
    assert rc.classify_turn(write_turn(tmp_path / "c", [artifact])).outcome == "unknown"



# ---------------------------------------------------------------------------
# Regressions from the second independent review


def test_unresolved_hold_is_durable_before_the_attempt_is_journalled_settled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, [None])
    env.head(EAI)
    real = env.store.journal

    def dies_on_settle(record: dict[str, Any]) -> None:
        if record.get("event") == "settle":
            raise SystemExit("killed between the hold and its settle record")
        real(record)
    monkeypatch.setattr(env.store, "journal", dies_on_settle)
    with pytest.raises(SystemExit):
        env.run()
    state = env.store.load()
    assert state["phase"] == "held" and state["hold"]["reason"] == "attempt_unresolved" and state["pending"] is None
    monkeypatch.setattr(env.store, "journal", real)
    assert env.run()[0] == "held" and len(env.relay.launches) == 1


def test_marker_quoted_later_in_another_task_is_not_the_attempt(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.head(QUOTA)
    nonce = "rc-20261018T080000Z-5eedf00d"
    _pending(env, nonce, pid=None)
    # The original session is gone; a maintainer audit prompt quotes the marker on a later line.
    write_turn(env.relay.sessions_dir() / "20261018-080500-aaaaab", SUCCESS,
               prompt=relay_prompt(f"Audit the earlier attempt.\n[relay-continuity attempt {nonce}]"))
    assert env.relay.find_by_nonce(nonce, T0) is None
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["reason"] == "attempt_unresolved"


@pytest.mark.parametrize("command", [
    "sed -e's/.*/git push/e' input", "sed --expression='s/.*/git push/e' input", "sed -ne 's/x/y/w out' f",
    "sed -f prog.sed f", "awk -f deploy.awk", "gawk -i inplace '{print}' f", "awk --file=deploy.awk",
])
def test_program_text_in_options_or_files_is_not_read_only(command: str) -> None:
    assert rc.classify_command(command)[0] != "read_only"


def test_killed_background_subagent_leaves_its_unanswered_calls_in_flight(tmp_path: Path) -> None:
    parent = tool_use("a", "Agent", subagent_type="general-purpose", run_in_background=True)
    child = tool_use("c", "Bash", command="git push")
    child["parent_tool_use_id"] = "a"
    turn = write_turn(tmp_path / "s", [
        parent, tool_result("a"),
        {"type": "system", "subtype": "task_started", "task_id": "t", "tool_use_id": "a", "is_backgrounded": True},
        child, {"type": "system", "subtype": "task_notification", "task_id": "t", "tool_use_id": "a", "status": "killed"},
        *QUOTA])
    report = rc.classify_turn(turn)
    assert [c.label for c in report.in_flight] == ["git push"]
    assert any(rc.external(c) for c in report.in_flight)


@pytest.mark.parametrize("windows", [None, [], [{}], [{"id": "s", "used_percent": "100"}], [{"id": "s", "used_percent": True}]])
def test_unusable_usage_windows_never_block_a_try(tmp_path: Path, windows: Any) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.relay.readings = lambda: {"codex": {"agent_key": "codex", "status": "ok", "as_of": rc.iso(env.clock.now()),
                                            "windows": windows}}
    env.head(QUOTA)
    result, _ = env.run()
    assert result == "idle" and [launch["agent"] for launch in env.relay.launches] == ["codex"]


def test_codex_transient_hands_over_to_claude_with_room(tmp_path: Path) -> None:
    # Codex hits a transient and a fresh reading shows Codex capped: Claude, which has room, takes over at once.
    codex_reset = T0 + timedelta(minutes=12)
    env = Env(tmp_path, [("claude", SUCCESS)])
    env.relay.readings = room(env, "claude", exhausted={"codex": codex_reset})
    state = rc.default_state()
    state.update({"head_session": HEAD, "chain": [HEAD], "agent": "codex", "permissions_digest": "perm0",
                  "permissions_pinned": True, "last_switch_at": rc.iso(T0 - timedelta(hours=2))})
    env.store.save(state, T0)
    env.head(CODEX_EAI, agent="codex")
    assert env.run()[0] == "idle"
    assert [launch["agent"] for launch in env.relay.launches] == ["claude"]
    assert env.relay.launches[0]["at"] == T0 < codex_reset


def codex_spend_capped(env: Env) -> None:
    """Codex already carries a manual spend cap on this chain, so only Claude can be retried."""
    state = rc.default_state()
    state.update({"head_session": HEAD, "chain": [HEAD], "permissions_digest": "perm0", "permissions_pinned": True,
                  "cooldowns": {"codex": {"until": None, "source": "manual", "reason": "spend_limit", "since": rc.iso(T0)}}})
    env.store.save(state, T0)


SPEND_WITH_RESET = [claude_result("You've hit your monthly spend limit · your session limit resets 11:50pm (Europe/Rome)",
                                  error=True, status=429)]


def test_restart_does_not_escalate_the_same_spend_cap_turn(tmp_path: Path) -> None:
    env = Env(tmp_path)  # any Relay invocation fails the test
    codex_spend_capped(env)
    env.head(SPEND_WITH_RESET)

    def stop_while_waiting(seconds: float) -> None:
        env.store.stop_path.write_text("now")  # the maintainer stops the supervisor during the wait
        env.clock.sleep(seconds)
    env.host.sleep = stop_while_waiting
    for _ in range(2):  # run, stop while waiting, restart on the same head turn, stop again
        assert env.run()[0] == "stopped"
        env.store.stop_path.unlink()
        cooldown = env.store.load()["cooldowns"]["claude"]
        assert cooldown["reason"] == "quota" and cooldown["until"] is not None
    assert env.relay.launches == []


def test_release_clearing_a_spend_cap_retries_the_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spend = [claude_result("You've hit your monthly spend limit", error=True, status=429)]
    env = Env(tmp_path, [("claude", SUCCESS)])
    codex_spend_capped(env)
    env.head(spend)
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["reason"] == "no_provider_available"
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(env.store.root))
    monkeypatch.setattr(rc, "make_host", lambda repo: env.host)
    monkeypatch.setattr(rc, "repo_root", lambda: env.repo)
    assert rc.main(["release", "--clear-cooldown", "claude", "--note", "spend cap raised"]) == 0
    result, sup = env.run()
    assert result == "idle" and [launch["agent"] for launch in env.relay.launches] == ["claude"]
    assert "claude" not in sup.state["cooldowns"]


def test_releasing_a_permissions_hold_does_not_waive_the_side_effect_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.relay.readings = room(env, "codex")
    env.head(SUCCESS)
    assert env.run()[0] == "idle"
    env.perm["v"] = "perm-widened"
    sid = "20261018-090000-eeeeee"
    env.head([tool_use("x", "Bash", command="git push origin HEAD"), *QUOTA], sid=sid)
    result, sup = env.run(sid)
    assert result == "held" and sup.state["hold"]["reason"] == "permissions_changed"
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(env.store.root))
    monkeypatch.setattr(rc, "make_host", lambda repo: env.host)
    monkeypatch.setattr(rc, "repo_root", lambda: env.repo)
    assert rc.main(["release", "--note", "reviewed the permissions change only"]) == 0
    result, sup = env.run(sid)
    assert result == "idle" and sup.state["hold"] is None
    assert [launch["agent"] for launch in env.relay.launches] == ["codex"]
    task = env.relay.launches[0]["task"]
    assert rc.PARKED_TEXT in task
    assert any(kind == "finding" and "parked interrupted external operation(s)" in text for kind, text in env.ledger)
    assert any("ha messo da parte un'operazione non verificabile" in e for e in env.events())


def test_spend_cap_reported_after_its_printed_reset_is_a_spend_limit(tmp_path: Path) -> None:
    # Corpus 20260924-083444-1d5541/turn-004: still capped at 14:27 Rome with "resets 12:40pm".
    late = datetime(2026, 9, 24, 12, 27, 7, tzinfo=UTC)
    turn = write_turn(tmp_path / "s", [claude_result(
        "You've hit your monthly spend limit · raise it at claude.ai/settings/usage · your session limit resets "
        "12:40pm (Europe/Rome)", error=True, status=429)])
    assert rc.classify_turn(turn, ref=late).outcome == "spend_limit"
    assert rc.classify_turn(turn, ref=datetime(2026, 9, 24, 9, 28, tzinfo=UTC)).outcome == "quota"


# ---------------------------------------------------------------------------
# Regressions from the third independent review


@pytest.mark.parametrize(("command", "read_only"), [
    ("sed '1s/.*/git push/e' input", False), ("sed '/x/,/y/s/a/b/w out' f", False), ("sed '$e date' f", False),
    ("sed '2!s/a/b/e' f", False), ("awk 'BEGIN {cmd=\"git push\"; print \"\" | cmd}'", False),
    ("sed 's|a|b|g;s/c/d/' f", True), ("sed '2!d' f", True), ("sed 'y/abc/xyz/' f", True), ("sed '1a hello' f", True),
])
def test_sed_addresses_and_awk_pipes(command: str, read_only: bool) -> None:
    assert (rc.classify_command(command)[0] == "read_only") is read_only


def test_release_acknowledges_only_the_held_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path)  # any Relay invocation fails the test
    session = env.relay.sessions_dir() / HEAD
    write_turn(session, [], number=1)
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["turn"] == f"{HEAD}/turn-001"
    assert sup.state["hold"]["reason"] == "output_missing"
    # Meanwhile a manual REPL turn on the same session leaves an unanswered empty turn.
    write_turn(session, [], number=2)
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(env.store.root))
    monkeypatch.setattr(rc, "make_host", lambda repo: env.host)
    monkeypatch.setattr(rc, "repo_root", lambda: env.repo)
    assert rc.main(["release", "--note", "turn 1 cleared"]) == 0
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["reason"] == "output_missing"
    assert sup.state["hold"]["turn"] == f"{HEAD}/turn-002" and env.relay.launches == []


def test_waiting_keeps_the_completed_effects_record(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)])
    env.head([tool_use("c", "Bash", command="gh pr comment 5 --body done"), tool_result("c"), *QUOTA])
    assert env.run()[0] == "idle"
    task = env.relay.launches[0]["task"]
    assert "[external] gh pr comment" in task and "do not repeat them blindly" in task


def test_returned_session_without_turn_files_holds(tmp_path: Path) -> None:
    class NoTurns(FakeRelay):
        def finish(self, proc: FakeProc) -> Any:
            sid = "20261018-090909-ffffff"
            (self.sessions_dir() / sid).mkdir(parents=True)
            return rc.RunResult(sid, 0, True)
    env = Env(tmp_path)
    env.relay = NoTurns(env.repo, [("claude", SUCCESS)])
    env.relay.clock = env.clock
    env.host.relay = env.relay
    env.head(EAI)
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["reason"] == "output_missing"
    assert len(env.relay.launches) == 1


def test_manual_spend_cap_survives_restart_on_the_same_turn(tmp_path: Path) -> None:
    env = Env(tmp_path)  # any Relay invocation fails the test
    env.head(SPEND_WITH_RESET)
    state = rc.default_state()
    state.update({"head_session": HEAD, "chain": [HEAD], "agent": "claude", "permissions_digest": "perm0",
                  "permissions_pinned": True,
                  "cooldowns": {a: {"until": None, "source": "manual", "reason": "spend_limit", "since": rc.iso(T0)}
                                for a in ("claude", "codex")},
                  "spend_caps": {"claude": {"turn": f"{HEAD}/turn-001", "at": rc.iso(T0)}}})
    env.store.save(state, T0)
    result, sup = env.run()
    assert result == "held" and sup.state["cooldowns"]["claude"]["until"] is None
    assert env.relay.launches == []


def test_released_gate_still_applies_the_provider_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.head([tool_use("p", "Bash", command="git push origin HEAD"),
              claude_result("Invalid API key · Please run /login", error=True, status=401)])
    state = rc.default_state()
    state.update({"phase": "held", "head_session": HEAD, "chain": [HEAD],
                  "hold": {"reason": "effects_uncertain", "at": rc.iso(T0), "attempt": None,
                           "turn": f"{HEAD}/turn-001"}})
    env.store.save(state, T0)
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(env.store.root))
    monkeypatch.setattr(rc, "make_host", lambda repo: env.host)
    monkeypatch.setattr(rc, "repo_root", lambda: env.repo)
    assert rc.main(["release", "--note", "the push landed"]) == 0
    result, sup = env.run()
    # The release waives the reviewed gate only: Claude's login failure still takes Claude out.
    assert result == "idle" and [launch["agent"] for launch in env.relay.launches] == ["codex"]
    assert sup.state["cooldowns"]["claude"]["reason"] == "auth"


# ---------------------------------------------------------------------------
# Regressions from the fourth independent review


def test_sed_transliteration_does_not_hide_a_following_command() -> None:
    assert rc.classify_command("sed 'y/a/b/;e git push' f")[0] != "read_only"
    assert rc.classify_command("sed 'y/a\\/b/xy/;p' f")[0] == "read_only"


def test_detached_job_in_a_compound_command_stays_in_flight(tmp_path: Path) -> None:
    command = "gh pr comment 5 --body started; nohup git push origin HEAD >/dev/null 2>&1 &"
    assert rc.classify_command(command) == ("external", "git push &")
    turn = write_turn(tmp_path / "s", [tool_use("x", "Bash", command=command), tool_result("x"), *QUOTA])
    report = rc.classify_turn(turn)
    assert [c.label for c in report.in_flight] == ["git push &"]
    assert any(rc.external(c) for c in report.in_flight)



# ---------------------------------------------------------------------------
# Regressions from the fifth independent review


@pytest.mark.parametrize(("command", "expected"), [
    ("bash -lc 'git push origin HEAD' >/tmp/push.log 2>&1 &", ("external", "git push &")),
    ("bash -lc 'echo hi' > out.txt", ("local_write", "bash > file")),
    ("bash -c 'ls; git push'", ("external", "git push")),
    ("bash -lc 'git status'", ("read_only", "git status")),
    ("bash -lc 'npm run dev >/tmp/dev.log 2>&1 &' ; python scripts/smoke_test.py", ("unknown", "npm &")),
])
def test_shell_wrappers_keep_the_outer_commands_effects(command: str, expected: tuple[str, str]) -> None:
    assert rc.classify_command(command) == expected


def test_inner_detachment_survives_a_worse_outer_command(tmp_path: Path) -> None:
    command = "bash -lc 'npm run dev >/tmp/dev.log 2>&1 &' ; python scripts/smoke_test.py"
    turn = write_turn(tmp_path / "s", [tool_use("x", "Bash", command=command), tool_result("x"), *QUOTA])
    assert [(c.label, c.detached) for c in rc.classify_turn(turn).in_flight] == [("npm &", True)]


@pytest.mark.parametrize("command", ["tmux new-session -d -s dev 'npm run dev'", "tmux send-keys -t dev 'make' Enter",
                                     "screen -dmS dev npm run dev", "tmux -L dev new-session -d -s web 'npm run dev'",
                                     "screen -d -m -S web npm run dev", "docker compose up -d", "docker run -d nginx",
                                     "docker start web", "podman-compose up --detach", "systemd-run --user npm run dev",
                                     "vite preview --background", "docker compose up --wait", "pm2 start server.js",
                                     "gunicorn -D app:app", "redis-server --daemonize yes",
                                     "docker compose -f compose.dev.yml restart backend",
                                     "docker run -dt -v \"$PWD:/work\" -w /work python:3.12 python -m pytest -q",
                                     "docker run -itd img", "docker compose -p dev -f c.yml up --detach"])
def test_terminal_multiplexer_launches_stay_in_flight(tmp_path: Path, command: str) -> None:
    turn = write_turn(tmp_path / "s", [tool_use("x", "Bash", command=command), tool_result("x"), *QUOTA])
    assert [c.detached for c in rc.classify_turn(turn).in_flight] == [True]


@pytest.mark.parametrize("command", ["tmux -L dev ls", "docker compose up", "docker run --rm img pytest",
                                     "docker compose logs -f", "docker compose -f compose.dev.yml build",
                                     "docker run --rm -it -v \"$PWD:/w\" img pytest", "docker ps -a"])
def test_attached_multiplexer_and_container_commands_are_not_detached(command: str) -> None:
    assert not rc.classify_command(command)[1].endswith(" &")


@pytest.mark.parametrize(("command", "label"), [("git -C ../wt checkout --detach origin/master", "git checkout"),
                                              ("git switch --detach HEAD~1", "git switch"),
                                              ("git worktree add --detach ../wt origin/master", "git worktree")])
def test_git_detached_head_is_not_a_detached_process(tmp_path: Path, command: str, label: str) -> None:
    """`--detach` on checkout/switch/worktree detaches HEAD; the command has ended when its result arrives."""
    assert rc.classify_command(command) == ("local_write", label)
    full = f"{command} && python3 scripts/devctx.py note proof done"
    turn = write_turn(tmp_path / "s", [tool_use("x", "Bash", command=full), tool_result("x"), *QUOTA])
    assert rc.classify_turn(turn).in_flight == []


@pytest.mark.parametrize(("command", "label"), [("git daemon --detach --base-path=/srv", "git daemon &"),
                                              ("git checkout --detach origin/master &", "git checkout &")])
def test_git_process_detachment_stays_in_flight(tmp_path: Path, command: str, label: str) -> None:
    turn = write_turn(tmp_path / "s", [tool_use("x", "Bash", command=command), tool_result("x"), *QUOTA])
    assert [(c.label, c.detached) for c in rc.classify_turn(turn).in_flight] == [(label, True)]


def test_detached_wrapped_push_stays_in_flight(tmp_path: Path) -> None:
    command = "bash -lc 'git push origin HEAD' >/tmp/push.log 2>&1 &"
    turn = write_turn(tmp_path / "s", [tool_use("x", "Bash", command=command), tool_result("x"), *QUOTA])
    assert [c.label for c in rc.classify_turn(turn).in_flight] == ["git push &"]


@pytest.mark.parametrize(("command", "kind"), [
    ('curl -fsSL "$SCHEMA_URL" -o frontend/openapi.json', "local_write"), ("curl -O https://example.invalid/f", "local_write"),
    ("curl --output=x https://example.invalid", "local_write"), ("wget https://example.invalid/f", "local_write"),
    ("curl -sS https://example.invalid", "read_only"), ("curl -sSo /dev/null https://example.invalid", "read_only"),
    ("wget -qO- https://example.invalid", "read_only"), ("wget --spider https://example.invalid", "read_only"),
    ("curl -XPOST -o out https://example.invalid", "external"),
])
def test_downloads_to_files_are_local_writes(command: str, kind: str) -> None:
    assert rc.classify_command(command)[0] == kind



@pytest.mark.parametrize(("command", "kind"), [
    ("> build.log", "local_write"), ("sort -o out.txt in.txt", "local_write"), ("uniq in.txt out.txt", "local_write"),
    ("sort in.txt", "read_only"), ("uniq -c in.txt", "read_only"),
    ("sort -uo names.txt names.txt", "local_write"), ("sort --output names.txt in.txt", "local_write"),
    ("sort -u in.txt", "read_only"), ("git diff --output=/tmp/review.patch HEAD~1 HEAD", "local_write"),
    ("git log -p --output /tmp/log.txt", "local_write"), ("git diff HEAD~1 HEAD", "read_only"),
    ("sed --in-place=.bak 's/old/new/g' config.txt", "local_write"), ("sed -Ei 's/old/new/g' config.txt", "local_write"),
    ("sed -E 's/old/new/' config.txt", "read_only"), ("xxd -r fixture.hex fixture.bin", "local_write"),
    ("xxd -l 16 file.bin", "read_only"), ("xxd -r -p in.hex", "read_only"),
])
def test_other_file_writing_forms(command: str, kind: str) -> None:
    assert rc.classify_command(command)[0] == kind


@pytest.mark.parametrize(("command", "kind"), [
    ('case "$suite" in backend) python -m pytest -q backend/tests ;; esac', "unknown"),
    ('case "$x" in a) echo hi ;; esac', "read_only"),
    ("git diff 1>/tmp/review.patch", "local_write"), ("git status 2>/tmp/status.err", "local_write"),
    ("gh pr checks 831 &>/tmp/checks.log", "local_write"), ("git status 2>>/tmp/err", "local_write"),
    ("git status 2>/dev/null", "read_only"), ("ls 2>&1", "read_only"), ("git log >&2", "read_only"),
    ("sed ':join;N;$!b join;w /tmp/joined.txt' input.txt", "unknown"), (r"sed ':a;N;$!ba;s/\n/ /g' f", "read_only"),
    ("git branch --set-upstream-to=origin/master", "local_write"), ("git branch --unset-upstream", "local_write"),
    ("git branch newfeature", "local_write"), ("git branch -a", "read_only"), ("git branch -vv", "read_only"),
    ("git branch --merged master", "read_only"), ("git reflog expire --expire=now --all", "local_write"),
    ("git reflog", "read_only"), ("git remote -v set-url origin https://example.invalid/r.git", "local_write"),
    ("git remote -v", "read_only"), ("git remote get-url origin", "read_only"),
    ("curl --config /tmp/backend-smoke.curl", "unknown"), ("curl -sK cfg", "unknown"),
    ("curl -s http://127.0.0.1:8000/health", "read_only"),
    ("wget -qO- -o /tmp/health.log http://127.0.0.1:8000/health", "local_write"), ("wget -qO- http://x", "read_only"),
    (r"tmux has-session -t dev \; send-keys -t dev 'python -m pytest -q' Enter", "unknown"),
    ("tmux source-file /tmp/dev.tmux", "unknown"), ("tmux new-s -d -s web 'npm run dev'", "unknown"),
    ("tmux ls", "read_only"), ("tmux list-windows -t dev", "read_only"),
    ("screen -S dev -X screen python -m pytest -q", "unknown"),
])
def test_review_10_write_and_launch_forms(command: str, kind: str) -> None:
    assert rc.classify_command(command)[0] == kind


# ---------------------------------------------------------------------------
# Spec 197: autonomous incident reconciliation of uncertain operations


def journal(env: Env) -> list[dict[str, Any]]:
    return [json.loads(line) for line in env.store.journal_path.read_text().splitlines()]


def fake_runner(responses: dict[str, tuple[int, str]]) -> Any:
    calls: list[list[str]] = []

    def run(args: list[str], cwd: Path | None = None, timeout: float = 20.0) -> tuple[int | None, str]:
        calls.append(args)
        for key, value in responses.items():
            if key in " ".join(args):
                return value
        return 1, "unexpected"
    run.calls = calls  # type: ignore[attr-defined]
    return run


def bash(command: str) -> Any:
    return rc._claude_call({"id": "x", "name": "Bash", "input": {"command": command}})


@pytest.mark.parametrize(("command", "responses", "expected"), [
    ("cd /r && git push origin feat", {"show-ref": (0, ""), "rev-parse --verify": (0, "a" * 40 + "\n"),
                                       "ls-remote": (0, f"{'a' * 40}\trefs/heads/feat\n")}, "completed"),
    ("cd /wt && git push -u origin HEAD:feat 2>&1", {"abbrev-ref HEAD": (0, "feat"), "rev-parse --verify": (0, "a" * 40),
                                                     "ls-remote": (0, "")}, "not_started"),
    ("git push origin feat", {"show-ref": (0, ""), "rev-parse --verify": (0, "a" * 40),
                              "ls-remote": (0, f"{'b' * 40}\trefs/heads/feat"), "merge-base": (0, "")}, "not_started"),
    ("git push origin feat", {"show-ref": (0, ""), "rev-parse --verify": (0, "a" * 40),
                              "ls-remote": (0, f"{'b' * 40}\trefs/heads/feat"), "merge-base": (1, "")}, None),
    ("git push --force origin feat", {"show-ref": (0, ""), "rev-parse --verify": (0, "a" * 40), "ls-remote": (0, "")}, None),
    ("git push origin feat", {"show-ref": (0, ""), "rev-parse --verify": (0, "a" * 40),
                              "ls-remote": (128, "fatal: Authentication failed")}, "insufficient_permissions"),
    # Review 1: the agent's working directory persists across calls, so HEAD/upstream are unknown without a cd.
    ("git push", {"abbrev-ref": (0, "origin/master"), "rev-parse --verify": (0, "a" * 40),
                  "ls-remote": (0, f"{'a' * 40}\trefs/heads/master")}, None),
    ("git push origin HEAD", {"abbrev-ref HEAD": (0, "master"), "rev-parse --verify": (0, "a" * 40),
                              "ls-remote": (0, f"{'a' * 40}\trefs/heads/master")}, None),
    ("cd wt && git push origin feat", {}, None),
    # Review 2: a tag (or any non-branch) push is never probed as a branch.
    ("git push origin v1", {"show-ref": (1, ""), "rev-parse --verify": (0, "a" * 40), "ls-remote": (0, "")}, None),
    ("git push origin refs/tags/v1", {}, None),
    ("git push --tags origin", {}, None),
    ("git push origin feat && gh pr create --fill", {}, None),  # two effectful steps: only a diagnosis can tell
    ("git push origin $BRANCH", {}, None),
    ("gh pr merge 12 --squash", {"pr view 12": (0, '{"number": 12, "state": "MERGED"}')}, "completed"),
    ("gh pr merge 12 --squash", {"pr view 12": (0, '{"number": 12, "state": "OPEN"}')}, "not_started"),
    ("gh pr merge 12 --auto --squash", {}, None),
    ("gh pr merge -m 42", {"pr view 42": (0, '{"number": 42, "state": "MERGED"}')}, "completed"),  # -m is --merge
    ("gh pr merge -r --delete-branch", {"pr view": (0, '{"number": 7, "state": "MERGED"}')}, None),  # target unknown
    ("gh pr create --head feat --fill", {"pr list": (0, '[{"number": 2, "state": "MERGED"}]')}, None),
    ("gh pr create --fill", {}, None),
    ("gh pr merge 12", {"pr view 12": (1, "HTTP 401: Bad credentials (gh auth login)")}, "insufficient_permissions"),
    ("gh pr create --head feat --fill", {"pr list": (0, '[{"number": 3, "state": "OPEN"}]')}, "completed"),
    ("gh pr create --head feat --fill", {"pr list": (0, "[]")}, "not_started"),
    ("gh pr comment 3 --body hi", {}, None),
    ("./deploy.sh", {}, None),
])
def test_deterministic_probes(command: str, responses: dict[str, tuple[int, str]], expected: str | None) -> None:
    run = fake_runner(responses)
    probed = rc.probe_call(bash(command), Path("/r"), run)
    assert (probed[0] if probed else None) == expected
    for args in run.calls:  # probes only read
        assert (args[3] if args[:2] == ["git", "-C"] else " ".join(args[1:3])) in (
            "rev-parse", "ls-remote", "merge-base", "show-ref", "pr view", "pr list")


def test_file_probes(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    edit = rc._claude_call({"id": "e", "name": "Edit", "input": {"file_path": str(target), "old_string": "x = 1",
                                                                  "new_string": "x = 2"}})
    write = rc._claude_call({"id": "w", "name": "Write", "input": {"file_path": str(target), "content": "x = 2\n"}})
    assert rc.probe_call(write, tmp_path)[0] == "not_started"
    target.write_text("x = 1\n")
    assert rc.probe_call(edit, tmp_path)[0] == "not_started"
    assert rc.probe_call(write, tmp_path) is None
    target.write_text("x = 2\n")
    assert rc.probe_call(edit, tmp_path)[0] == "completed" and rc.probe_call(write, tmp_path)[0] == "completed"
    assert "content" not in json.dumps(rc.TurnReport("crash", "claude", "", calls=[write]).to_json())


# ---------------------------------------------------------------------------
# Spec 197 review wave 2: durable provider wait and pre-execution read-only enforcement


# ---------------------------------------------------------------------------
# Spec 201: durable missions and the persistent owner

DIRECTIVE = "L20261020085928-0431"


def mission_def(*texts: str, checks: dict[int, dict[str, Any] | None] | None = None, session: str | None = None,
                mid: str = "m-20261018T075500Z-abc123", submitted: datetime = T0 - timedelta(minutes=5)) -> dict[str, Any]:
    # The fake host accepts checks by default. Explicit None models a legacy unchecked mission.
    tasks = [{"text": t, **({"check": check} if check is not None else {})}
             for i, t in enumerate(texts)
             for check in [(checks or {}).get(i, {"kind": "pr_merged", "pr": 1})]]
    return {"schema": rc.MISSION_SCHEMA, "id": mid, "authority": DIRECTIVE, "tasks": tasks,
            "submitted_at": rc.iso(submitted), "session": session, "repo": "/repo"}


def start_mission(env: Env, mission: dict[str, Any]) -> None:
    state = rc.default_state()
    state["mission"] = {"id": mission["id"], "next": 0, "total": len(mission["tasks"]), "done": []}
    env.store.save(state, T0)


def run_mission(env: Env, mission: dict[str, Any], session: str | None = None) -> tuple[str, Any]:
    sup = env.supervisor()
    sup.mission = mission
    return sup.run(session), sup


def journal_events(store: Any) -> list[str]:
    return [json.loads(line)["event"] for line in store.journal_path.read_text().splitlines()]


def test_mission_validation_rejects_unsafe_definitions() -> None:
    assert rc.validate_mission(mission_def("a", checks={0: {"kind": "pr_merged", "pr": 846}})) is None
    assert rc.validate_mission(mission_def("a", checks={0: {"kind": "path_exists", "path": "docs/x.md"}})) is None
    bad = [
        {**mission_def("a"), "authority": "maintainer said so"},
        {**mission_def("a"), "id": "m-1"},
        {**mission_def("a"), "tasks": []},
        mission_def(*["t"] * (rc.MAX_TASKS + 1)),
        mission_def("x" * (rc.MAX_TASK_CHARS + 1)),
        mission_def("use token ghp_" + "a" * 36),
        mission_def("a", checks={0: {"kind": "shell", "cmd": "true"}}),
        mission_def("a", checks={0: {"kind": "path_exists", "path": "/etc/passwd"}}),
        mission_def("a", checks={0: {"kind": "path_exists", "path": "../outside"}}),
        mission_def("a", checks={0: {"kind": "pr_merged", "pr": True}}),
        {**mission_def("a"), "tasks": [{"text": "a", "agent": "codex"}]},
    ]
    assert [n for n, m in enumerate(bad) if rc.validate_mission(m) is None] == []


def test_mission_advances_each_task_only_on_success_and_completes(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS), ("claude", SUCCESS), ("claude", SUCCESS)])
    mission = mission_def("first task", "second task", "third task")
    start_mission(env, mission)
    result, sup = run_mission(env, mission)
    assert result == "idle"
    launches = env.relay.launches
    assert [launch["parent"] for launch in launches] == [None, "20261018-000001-bbbbbb", "20261018-000002-bbbbbb"]
    for n, (launch, text) in enumerate(zip(launches, ("first task", "second task", "third task"), strict=True), 1):
        assert f"task {n}/3" in launch["task"] and launch["task"].endswith(f"{rc.MISSION_HEADER}\n{text}")
        assert DIRECTIVE in launch["task"] and "devctx.py recover" in launch["task"]
    rec = sup.state["mission"]
    assert rec["next"] == 3 and rec["completed_at"] and [d["task"] for d in rec["done"]] == [0, 1, 2]
    assert journal_events(env.store).count("task_completed") == 3 and "mission_completed" in journal_events(env.store)
    assert sup.state["phase"] == "idle"
    assert sum("ha completato una parte del lavoro" in e for e in env.events()) == 3
    assert any("ha completato il lavoro richiesto" in e for e in env.events())
    assert not any("awaiting maintainer" in e for e in env.events())


def test_mission_quota_fails_over_to_codex_with_task_context_and_continues(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS), ("claude", QUOTA), ("codex", CODEX_SUCCESS), ("codex", CODEX_SUCCESS)])
    env.relay.readings = room(env, "codex")
    mission = mission_def("first task", "second task", "third task")
    start_mission(env, mission)
    result, sup = run_mission(env, mission)
    assert result == "idle"
    assert [launch["agent"] for launch in env.relay.launches] == ["claude", "claude", "codex", "codex"]
    takeover = env.relay.launches[2]["task"]
    assert "grants no new authority" in takeover  # the spec 195 recovery prompt, not a fresh task
    assert takeover.endswith(f"task 2/3, which has not been recorded complete:\n{rc.MISSION_HEADER}\nsecond task")
    done = sup.state["mission"]["done"]
    assert [(d["task"], d["agent"]) for d in done] == [(0, "claude"), (1, "codex"), (2, "codex")]
    assert sup.state["cooldowns"]["claude"]["reason"] == "quota"
    assert any("ha cambiato assistente" in e for e in env.events())


def test_mission_fails_over_on_a_stale_codex_reading_without_probing(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", QUOTA), ("codex", CODEX_SUCCESS)])
    stale = reading("codex", 4, T0 - timedelta(hours=3), T0 - timedelta(hours=6))  # an idle host's old rollout
    env.relay.readings = lambda: {**room(env, "claude")(), "codex": stale}
    mission = mission_def("one task")
    start_mission(env, mission)
    result, sup = run_mission(env, mission)
    assert result == "idle" and not hasattr(env.host, "refresh_codex_quota")
    assert [launch["agent"] for launch in env.relay.launches] == ["claude", "codex"]
    assert env.relay.launches[1]["at"] == T0  # tried at once: no probe turn, no recheck wait
    assert sup.state["mission"]["done"][0]["agent"] == "codex"


def test_mission_fallback_returns_to_recovered_claude_without_retrying_codex(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)])
    env.relay.readings = None  # `agent-relay limits` returned nothing usable
    mission = mission_def("one task")
    state = rc.default_state()
    state.update({"mission": {"id": mission["id"], "next": 0, "total": 1, "done": []}, "agent": "codex",
                  "last_switch_at": rc.iso(T0 - timedelta(minutes=1)), "switches": [rc.iso(T0 - timedelta(minutes=1))],
                  "cooldowns": {"claude": {"until": rc.iso(T0 - timedelta(seconds=1)), "source": "limits",
                                           "reason": "quota", "since": rc.iso(T0 - timedelta(hours=5))}}})
    env.store.save(state, T0)
    result, sup = run_mission(env, mission)
    # Claude's expired cooldown ends by itself (no `release --clear-cooldown`), and the dwell never pins the
    # chain to fallback Codex: Claude starts at once without an unnecessary Codex turn.
    assert result == "idle"
    assert [launch["agent"] for launch in env.relay.launches] == ["claude"]
    assert env.relay.launches[0]["at"] == T0
    assert "claude" not in sup.state["cooldowns"]
    assert sup.state["mission"]["done"][0]["agent"] == "claude"


def test_restart_preserves_the_pending_mission_and_recovers_providers_by_time(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", NO_RESET_QUOTA), ("codex", CODEX_NO_RESET_QUOTA), ("claude", SUCCESS)])

    def stop_during_wait(seconds: float) -> None:  # the owner is stopped while both providers are out
        env.store.stop_path.write_text("x")
        env.clock.sleep(seconds)
    env.host.sleep = stop_during_wait
    mission = mission_def("one task")
    start_mission(env, mission)
    assert run_mission(env, mission)[0] == "stopped"
    saved = env.store.load()
    assert saved["phase"] == "waiting" and saved["mission"]["next"] == 0 and saved["pending"] is None
    assert {a: c["until"] for a, c in saved["cooldowns"].items()} == {"claude": rc.iso(T0 + timedelta(minutes=30)),
                                                                     "codex": rc.iso(T0 + timedelta(minutes=30))}
    env.store.stop_path.unlink()
    env.host.sleep = env.clock.sleep
    env.clock.t = T0 + timedelta(minutes=10)  # restarted before the resets: the same turn is decided again
    result, sup = run_mission(env, mission, saved["head_session"])  # as `serve` resumes it
    assert result == "idle"
    # Re-deciding Codex's quota turn kept its recorded cooldown (no extension, no extra backoff strike): the
    # providers recovered by time alone, at the original reset, and finished the preserved task.
    assert [launch["agent"] for launch in env.relay.launches] == ["claude", "codex", "claude"]
    assert env.relay.launches[2]["at"] == T0 + timedelta(minutes=30)
    assert [d["task"] for d in sup.state["mission"]["done"]] == [0] and sup.state["mission"]["next"] == 1


def test_mission_restart_adopts_running_attempt_and_continues_remaining_tasks(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)])
    mission = mission_def("first task", "second task")
    env.head(SUCCESS)  # task 1's session
    nonce = "rc-20261018T080000Z-0badf00d"
    state = rc.default_state()
    state.update({"phase": "running", "head_session": HEAD, "chain": [HEAD], "agent": "codex", "permissions_digest": "perm0",
                  "permissions_pinned": True,
                  "mission": {"id": mission["id"], "next": 1, "total": 2,
                              "done": [{"task": 0, "session": HEAD, "agent": "claude", "at": rc.iso(T0),
                                        "outcome": "verified"}]},
                  "pending": {"nonce": nonce, "agent": "codex", "parent": HEAD, "started_at": rc.iso(T0), "pid": 4242,
                              "pid_start": 42, "fingerprint": "fp0", "attempt": 1, "task": 1}})
    env.store.save(state, T0)
    env.store.journal({"event": "launch", "nonce": nonce})
    env.alive[4242] = 2

    def finish_pending() -> None:
        write_turn(env.relay.sessions_dir() / "20261018-080100-cccccc", CODEX_SUCCESS, agent="codex",
                   prompt=relay_prompt(f"[relay-continuity attempt {nonce}]"))
    env.relay.finish_pending = finish_pending  # type: ignore[attr-defined]
    result, sup = run_mission(env, mission, HEAD)
    assert result == "idle"
    assert env.relay.launches == []  # the adopted attempt was never relaunched, and it was the last task
    assert sup.state["mission"]["next"] == 2 and sup.state["mission"]["done"][-1]["session"] == "20261018-080100-cccccc"
    assert sup.state["mission"]["completed_at"]


def test_mission_crash_while_settling_success_adopts_instead_of_relaunching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = Env(tmp_path)
    mission = mission_def("one task")
    start_mission(env, mission)
    nonce = "rc-20261018T080000Z-0badf00d"
    pending = {"nonce": nonce, "agent": "claude", "parent": None, "started_at": rc.iso(T0),
               "pid": None, "pid_start": None, "fingerprint": "fp0", "attempt": 1, "task": 0}
    state = env.store.load()
    state["pending"] = pending
    env.store.save(state, T0)
    session = "20261018-080100-cccccc"
    write_turn(env.relay.sessions_dir() / session, SUCCESS, agent="claude",
               prompt=relay_prompt(f"[relay-continuity attempt {nonce}]"))
    sup = env.supervisor()
    sup.mission = mission
    sup.state = env.store.load()

    def crash_before_progress(*_: Any) -> None:
        raise RuntimeError("simulated process death")

    monkeypatch.setattr(sup, "_advance", crash_before_progress)
    with pytest.raises(RuntimeError, match="simulated process death"):
        sup._settle(pending, session)
    assert env.store.load()["pending"]["nonce"] == nonce

    result, restarted = run_mission(env, mission)
    assert result == "idle"
    assert env.relay.launches == []
    assert restarted.state["mission"]["next"] == 1
    assert restarted.state["mission"]["completed_at"]


def test_mission_restart_with_unsettled_journal_launch_holds(tmp_path: Path) -> None:
    env = Env(tmp_path)
    mission = mission_def("first task")
    start_mission(env, mission)
    env.store.journal({"event": "launch", "nonce": "rc-20261018T075900Z-feedface"})  # launched; state lost it
    result, sup = run_mission(env, mission)
    assert result == "held" and sup.state["hold"]["reason"] == "attempt_unresolved"
    assert env.relay.launches == [] and sup.state["mission"]["next"] == 0


def test_mission_uncertain_external_effect_parks_and_continues(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS),
                         ("claude", [tool_use("p", "Bash", command="git push origin x"), *QUOTA]),
                         ("codex", CODEX_SUCCESS),
                         ("claude", SUCCESS)])
    env.relay.readings = room(env, "codex")
    mission = mission_def("first task", "second task", "third task")
    start_mission(env, mission)
    result, sup = run_mission(env, mission)
    assert result == "idle" and sup.state["hold"] is None
    assert len(env.relay.launches) == 4
    assert env.relay.launches[2]["agent"] == "codex"
    assert rc.PARKED_TEXT in env.relay.launches[2]["task"]
    assert any("ha messo da parte un'operazione non verificabile" in e for e in env.events())
    assert any(kind == "finding" and "parked interrupted external operation(s)" in text for kind, text in env.ledger)
    assert "mission_completed" in journal_events(env.store)


def test_mission_task_check_must_pass_before_advancing(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS), ("claude", SUCCESS)])
    seen: list[dict[str, Any]] = []
    env.host.task_check = lambda check: seen.append(check) or (None if len(seen) > 2 else "PR #846 is OPEN")
    mission = mission_def("merge 846", checks={0: {"kind": "pr_merged", "pr": 846}})
    start_mission(env, mission)
    result, sup = run_mission(env, mission)
    # An unconfirmed claim of completion does not advance the task: it is a checkpoint, resumed until confirmed.
    assert result == "idle" and sup.state["mission"]["completed_at"]
    assert seen == [{"kind": "pr_merged", "pr": 846}] * 3 and len(env.relay.launches) == 2
    events = journal_events(env.store)
    assert events.index("task_check_failed") < events.index("task_checkpointed") < events.index("task_completed")


def test_unchecked_mission_cannot_complete_on_successful_provider_turn(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)])
    mission = mission_def("implement, test and merge an engineering deliverable", checks={0: None})
    start_mission(env, mission)
    result, sup = run_mission(env, mission)
    assert result == "held"
    assert sup.state["hold"]["reason"] == "task_check_failed"
    assert sup.state["mission"]["next"] == 0
    assert "mission_completed" not in journal_events(env.store)
    assert len(env.relay.launches) == 1


def test_release_advancing_the_last_task_completes_without_relaunching(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Incident 2026-10-30: the released turn had failed, so the owner relaunched it for task index `total`.
    env = Env(tmp_path, [("claude", SUCCESS)])
    root = tmp_path / "control"
    mission = mission_def("cycle")
    _write_mission(root, mission)
    env.head([IN_FLIGHT["git push"], *QUOTA])
    store = rc.mission_store(root, mission["id"])
    state = rc.default_state()
    state.update({"phase": "held", "head_session": HEAD, "chain": [HEAD], "agent": "claude",
                  "permissions_digest": "perm0", "permissions_pinned": True,
                  "hold": {"reason": "effects_uncertain", "at": rc.iso(T0), "attempt": None, "turn": f"{HEAD}/turn-001"},
                  "mission": {"id": mission["id"], "next": 0, "total": 1, "done": []}})
    store.save(state, T0)
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(root))
    monkeypatch.setattr(rc, "make_host", lambda repo: env.host)
    monkeypatch.setattr(rc, "repo_root", lambda: env.repo)
    assert rc.main(["release", "--mission", mission["id"], "--advance", "--note", "merged and verified by hand"]) == 0
    factory = lambda s, m: rc.Supervisor(s, env.host, env.limits)  # noqa: E731
    assert rc.serve_step(root, factory, conflicts=lambda: [], now=env.clock.now) == f"ran:{mission['id']}:idle"
    rec = store.load()["mission"]
    assert rec["completed_at"] and rec["next"] == 1 and [rc.task_outcome(d) for d in rec["done"]] == ["maintainer_attested"]
    assert env.relay.launches == [] and "mission_completed" in journal_events(store)
    assert rc.mission_status(store)[0] == "complete"


def test_mission_checkpoint_stays_pending_until_check_passes(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS, "continue"), ("claude", SUCCESS, "propose_done")])
    checks: list[dict[str, Any]] = []
    env.host.task_check = lambda check: checks.append(check) or None
    mission = mission_def("merge the deliverable", checks={0: {"kind": "pr_merged", "pr": 846}})
    start_mission(env, mission)
    result, sup = run_mission(env, mission)
    assert result == "idle" and sup.state["mission"]["next"] == 1
    assert len(env.relay.launches) == 2 and len(checks) == 1
    assert journal_events(env.store).count("task_checkpointed") == 1
    assert journal_events(env.store).count("task_completed") == 1


def test_mission_repeated_checkpoints_have_a_finite_bound(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS, "continue")] * rc.MAX_TASK_CHECKPOINTS)
    mission = mission_def("unfinished", checks={0: {"kind": "pr_merged", "pr": 1}})
    start_mission(env, mission)
    result, sup = run_mission(env, mission)
    assert result == "held" and sup.state["hold"]["reason"] == "task_checkpoint_limit"
    assert sup.state["mission"]["next"] == 0
    assert len(env.relay.launches) == rc.MAX_TASK_CHECKPOINTS


def test_check_task_is_read_only_and_bounded_to_the_repository(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("x")
    assert rc.check_task(tmp_path, {"kind": "path_exists", "path": "docs/x.md"}) is None
    assert rc.check_task(tmp_path, {"kind": "path_exists", "path": "docs/y.md"})
    calls: list[list[str]] = []

    def gh(args: list[str], **_: Any) -> tuple[int, str]:
        calls.append(args)
        return 0, "MERGED\n"
    assert rc.check_task(tmp_path, {"kind": "pr_merged", "pr": 846}, run=gh) is None
    assert calls == [["gh", "pr", "view", "846", "--json", "state", "-q", ".state"]]
    assert rc.check_task(tmp_path, {"kind": "pr_merged", "pr": 846}, run=lambda *a, **k: (0, "OPEN")) == "PR #846 is OPEN"
    assert rc.check_task(tmp_path, {"kind": "pr_merged", "pr": 846}, run=lambda *a, **k: (1, ""))


def _write_mission(root: Path, mission: dict[str, Any]) -> None:
    rc.write_json_atomic(rc.missions_dir(root) / f"{mission['id']}.json", mission)


def test_serve_step_runs_missions_one_at_a_time_in_submission_order(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS), ("claude", SUCCESS)])
    root = tmp_path / "control"
    first = mission_def("first mission", mid="m-20261018T070000Z-aaaaaa", submitted=T0 - timedelta(hours=1))
    second = mission_def("second mission", mid="m-20261018T060000Z-bbbbbb", submitted=T0 - timedelta(minutes=30))
    _write_mission(root, second)
    _write_mission(root, first)  # file names sort the other way: order is by submission time
    factory = lambda store, mission: rc.Supervisor(store, env.host, env.limits, sender=env.sender)  # noqa: E731
    assert rc.serve_step(root, factory, conflicts=lambda: [], now=env.clock.now) == f"ran:{first['id']}:idle"
    assert "first mission" in env.relay.launches[0]["task"]
    assert rc.serve_step(root, factory, conflicts=lambda: [], now=env.clock.now) == f"ran:{second['id']}:idle"
    assert "second mission" in env.relay.launches[1]["task"]
    assert rc.serve_step(root, factory, conflicts=lambda: [], now=env.clock.now) == "empty"
    assert journal_events(rc.mission_store(root, first["id"]))[0] == "mission_started"


def test_serve_step_waits_for_a_live_repl_and_a_held_mission_blocks_the_queue(tmp_path: Path) -> None:
    env = Env(tmp_path)
    root = tmp_path / "control"
    held = mission_def("held one", mid="m-20261018T070000Z-aaaaaa", submitted=T0 - timedelta(hours=1))
    later = mission_def("later one", mid="m-20261018T070100Z-bbbbbb", submitted=T0 - timedelta(minutes=30))
    _write_mission(root, held)
    _write_mission(root, later)
    factory = lambda store, mission: rc.Supervisor(store, env.host, env.limits)  # noqa: E731
    assert rc.serve_step(root, factory, conflicts=lambda: ["repl_active"]) == f"waiting:{held['id']}:repl_active"
    assert env.relay.launches == []
    store = rc.mission_store(root, held["id"])
    state = rc.default_state()
    state.update({"phase": "held", "hold": {"reason": "permissions_changed", "at": rc.iso(T0), "attempt": 1, "turn": "x"},
                  "mission": {"id": held["id"], "next": 0, "total": 1, "done": []}})
    store.save(state, T0)
    assert rc.serve_step(root, factory, conflicts=lambda: []) == f"held:{held['id']}"
    assert env.relay.launches == []
    busy = rc.Lock(root / "supervisor.lock")
    assert busy.acquire()
    try:
        assert rc.serve_step(root, factory, conflicts=lambda: []) == "busy"
    finally:
        busy.release()


def test_serve_step_holds_on_a_corrupt_earlier_mission(tmp_path: Path) -> None:
    root = tmp_path / "control"
    first = mission_def("first", mid="m-20261018T070000Z-aaaaaa", submitted=T0 - timedelta(hours=1))
    later = mission_def("later", mid="m-20261018T070100Z-bbbbbb", submitted=T0 - timedelta(minutes=30))
    _write_mission(root, first)
    _write_mission(root, later)
    first_path = rc.missions_dir(root) / f"{first['id']}.json"
    first_path.write_text("{")
    assert rc.serve_step(root, lambda *_: pytest.fail("should not execute"), conflicts=lambda: []) == f"corrupt:{first['id']}"
    first_path.write_text(json.dumps(first))
    state = rc.default_state()
    state["mission"] = {"id": first["id"], "next": 0, "total": 2, "done": []}
    rc.mission_store(root, first["id"]).save(state, T0)
    assert rc.serve_step(root, lambda *_: pytest.fail("should not execute"), conflicts=lambda: []) == f"corrupt:{first['id']}"


def test_mission_store_keeps_the_existing_chain_hold_untouched(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)])
    root = tmp_path / "control"
    chain = rc.Store(root)
    held = rc.default_state()
    held.update({"phase": "held", "head_session": HEAD, "chain": [HEAD],
                 "hold": {"reason": "effects_uncertain", "at": rc.iso(T0), "attempt": 6, "turn": f"{HEAD}/turn-006"}})
    chain.save(held, T0)
    before = chain.state_path.read_bytes()
    _write_mission(root, mission_def("independent work"))
    factory = lambda store, mission: rc.Supervisor(store, env.host, env.limits)  # noqa: E731
    assert rc.serve_step(root, factory, conflicts=lambda: []).endswith(":idle")
    assert chain.state_path.read_bytes() == before and not chain.journal_path.exists()


def test_cli_submit_requires_a_recorded_directive_and_is_durable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "control"
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(root))
    monkeypatch.setattr(rc, "repo_root", lambda: tmp_path)
    ledger = [{"id": DIRECTIVE, "kind": "directive"}, {"id": "L20261020121757-26d5", "kind": "claim"}]
    monkeypatch.setattr(rc.devctx, "read_ledger", lambda env: ledger)
    notes: list[Any] = []
    monkeypatch.setattr(rc.devctx, "append_entry", lambda env, entry: notes.append(entry))
    spec = tmp_path / "m.json"
    spec.write_text(json.dumps({"authority": "L20261020121757-26d5", "tasks": [{"text": "a"}]}))
    assert rc.main(["submit", "--mission-file", str(spec)]) == 2  # a claim is not a maintainer directive
    spec.write_text(json.dumps({"authority": DIRECTIVE, "tasks": [{"text": "a"}], "agent": "codex"}))
    assert rc.main(["submit", "--mission-file", str(spec)]) == 2
    spec.write_text(json.dumps({"authority": DIRECTIVE, "tasks": [{"text": "a"}, {"text": "b"}]}))
    assert rc.main(["submit", "--mission-file", str(spec)]) == 0
    [(mission, mid, problem)] = rc.load_missions(root)
    assert problem is None and mission["tasks"][1]["text"] == "b" and mission["authority"] == DIRECTIVE
    assert rc.mission_status(rc.mission_store(root, mid))[0] == "queued" and len(notes) == 1


def test_cli_serve_refuses_outside_the_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(tmp_path))
    monkeypatch.setattr(rc, "in_sandbox", lambda: False)
    assert rc.main(["serve", "--once", "--no-notify"]) == 2
    assert not (tmp_path / "serve.lock").exists()


def test_cli_serve_keeps_an_operator_stop_across_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(tmp_path))
    monkeypatch.setattr(rc, "in_sandbox", lambda: True)
    (tmp_path / "STOP").write_text("operator stop")
    assert rc.main(["serve", "--once", "--no-notify"]) == 0
    assert (tmp_path / "STOP").read_text() == "operator stop"
    assert not (tmp_path / "serve.lock").exists()


def test_cli_serve_pins_the_sandbox_provider_logins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    login = home / ".local/share/jarvisos-sandbox/state/claude"
    login.mkdir(parents=True)
    (login / ".credentials.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))  # what a Claude shell's launcher leaves behind
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(tmp_path / "control"))
    monkeypatch.setattr(rc, "in_sandbox", lambda: True)
    monkeypatch.setattr(rc.shutil, "which", lambda _name: None)  # stop right after the environment is set
    assert rc.main(["serve", "--once", "--no-notify"]) == 2
    assert os.environ["CODEX_HOME"] == str(home / ".local/share/jarvisos-sandbox/state/codex")
    assert os.environ["CLAUDE_CONFIG_DIR"] == str(login)
    assert (home / ".claude/.credentials.json").resolve() == (login / ".credentials.json").resolve()


def test_sandbox_provider_env_pins_the_github_login_only_when_installed(tmp_path: Path) -> None:
    state = tmp_path / ".local/share/jarvisos-sandbox/state"
    assert "GH_CONFIG_DIR" not in rc.sandbox_provider_env(tmp_path)
    assert "GIT_CONFIG_GLOBAL" not in rc.sandbox_provider_env(tmp_path)
    (state / "gh").mkdir(parents=True)
    (state / "gh/hosts.yml").write_text("github.com: {}\n")
    (state / "gitconfig").write_text("[credential]\n")
    env = rc.sandbox_provider_env(tmp_path)
    assert env["GH_CONFIG_DIR"] == str(state / "gh") and env["GIT_CONFIG_GLOBAL"] == str(state / "gitconfig")


def test_relay_claude_credentials_link_never_replaces_an_existing_file(tmp_path: Path) -> None:
    login, home = tmp_path / "login", tmp_path / "home"
    rc.link_relay_claude_credentials(login, home)
    assert not (home / ".claude").exists()  # no sandbox login: nothing to point at
    login.mkdir()
    (login / ".credentials.json").write_text("sandbox")
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/.credentials.json").write_text("own")
    rc.link_relay_claude_credentials(login, home)
    assert (home / ".claude/.credentials.json").read_text() == "own"


def test_relay_start_opens_a_new_session_with_closed_stdin_and_profile_env(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(rc.subprocess, "Popen", lambda args, **kw: seen.update(args=args, **kw))
    rc.RelayAdapter(tmp_path, env={"AGENT_RELAY_CLAUDE_PERMISSION_MODE": "bypassPermissions"}).start("claude", None, "t", 1)
    assert "--continue" not in seen["args"] and seen["stdin"] is subprocess.DEVNULL
    assert seen["env"]["AGENT_RELAY_CLAUDE_PERMISSION_MODE"] == "bypassPermissions" and "PATH" in seen["env"]
    rc.RelayAdapter(tmp_path).start("codex", HEAD, "t", 1)
    assert seen["args"][4:6] == ["--continue", HEAD] and seen["env"] is None


def test_mission_claude_entrypoint_replaces_relays_default_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runpy

    entrypoint = Path(rc.__file__).resolve().parent / "relay_mission_bin" / "claude"
    real = tmp_path / "claude-real"
    real.write_text("real")
    main = runpy.run_path(str(entrypoint))["main"]
    monkeypatch.setattr("socket.gethostname", lambda: "jarvis-sandbox")
    monkeypatch.setenv("JARVISOS_MISSION_PROFILE", "1")
    monkeypatch.setenv("RELAY_CLAUDE_REAL_BIN", str(real))
    monkeypatch.setattr("sys.argv", [str(entrypoint), "--permission-mode", "default", "--settings", "rules.json",
                                    "-p", "task"])
    invoked: list[Any] = []

    def execv(path: str, argv: list[str]) -> None:
        invoked.append((path, argv))
        raise RuntimeError("captured exec")

    monkeypatch.setattr("os.execv", execv)
    with pytest.raises(RuntimeError, match="captured exec"):
        main()
    assert invoked == [(str(real), [str(real), "--settings", "rules.json", "-p", "task",
                                    "--permission-mode", "bypassPermissions"])]
    monkeypatch.setattr("socket.gethostname", lambda: "ordinary-host")
    assert main() == 2


def test_release_mission_hold_and_advance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)])
    root = tmp_path / "control"
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(root))
    monkeypatch.setattr(rc, "make_host", lambda repo: env.host)
    mission = mission_def("merge it", "then this", checks={0: None})
    _write_mission(root, mission)  # task 1 has no completion check: its claimed finish holds
    factory = lambda store, m: rc.Supervisor(store, env.host, env.limits)  # noqa: E731
    assert rc.serve_step(root, factory, conflicts=lambda: [], now=env.clock.now) == f"ran:{mission['id']}:held"
    assert rc.main(["release", "--mission", mission["id"], "--advance", "--note", "merged by hand"]) == 0
    env.relay.script = [("claude", SUCCESS)]
    assert rc.serve_step(root, factory, conflicts=lambda: [], now=env.clock.now) == f"ran:{mission['id']}:idle"
    assert "then this" in env.relay.launches[-1]["task"]
    state = rc.mission_store(root, mission["id"]).load()
    assert [d.get("by") for d in state["mission"]["done"]] == ["maintainer", None] and state["mission"]["completed_at"]
    assert rc.Store(root).state_path.exists() is False  # the `run` chain store was never touched


def test_cancel_refuses_an_unsettled_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "control"
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(root))
    mission = mission_def("x")
    _write_mission(root, mission)
    store = rc.mission_store(root, mission["id"])
    state = rc.default_state()
    state.update({"mission": {"id": mission["id"], "next": 0, "total": 1, "done": []},
                  "pending": {"nonce": "rc-1", "agent": "claude", "parent": None, "started_at": rc.iso(T0), "pid": None,
                              "pid_start": None, "fingerprint": "f", "attempt": 1, "task": 0}})
    store.save(state, T0)
    assert rc.main(["cancel", mission["id"], "--note", "n"]) == 3
    state["pending"] = None
    store.save(state, T0)
    assert rc.main(["cancel", mission["id"], "--note", "n"]) == 0
    assert rc.mission_status(store)[0] == "cancelled"


# ---------------------------------------------------------------------------
# Spec 203: roadmap continuation cycles


class Roadmap:
    """A control root whose missions are submitted for real and completed or cancelled on demand."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *refs: str | None) -> None:
        self.root, self.repo = tmp_path / "control", tmp_path / "repo"
        self.repo.mkdir()
        monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(self.root))
        monkeypatch.setattr(rc, "repo_root", lambda: self.repo)
        host_paths = rc.host_ops.Paths
        monkeypatch.setattr(rc.host_ops, "Paths", lambda _home: host_paths(tmp_path))
        monkeypatch.setattr(rc.devctx, "read_ledger", lambda env: [{"id": DIRECTIVE, "kind": "directive"}])
        monkeypatch.setattr(rc.devctx, "append_entry", lambda env, entry: None)
        monkeypatch.setattr(rc, "roadmap_ci_fingerprint", lambda repo: "ci0")
        self.prs: list[int] = []
        monkeypatch.setattr(rc, "roadmap_pr_ids", lambda repo: self.prs.copy())
        self.refs = list(refs)
        self.minute = 0

    def fingerprint(self) -> str | None:
        return self.refs.pop(0) if self.refs else "same"

    def step(self) -> str | None:
        self.minute += 1
        return rc.roadmap_step(self.root, self.repo, fingerprint=self.fingerprint,
                               submit=self.submit, now=lambda: T0 + timedelta(minutes=self.minute))

    def submit(self, authority: str, tasks: list[dict[str, Any]], mission_id: str) -> Any:
        return rc.submit_mission(self.root, self.repo, authority, tasks, mission_id=mission_id,
                                 now=T0 + timedelta(minutes=self.minute))

    def finish(self, mission_id: str, *, relay_status: str = "propose_done", cancelled: bool = False) -> None:
        session = f"20261018-0800{self.minute:02d}-cccccc"
        write_turn(self.repo / ".agent-relay" / "sessions" / session, SUCCESS, status=relay_status)
        state = rc.default_state()
        state["head_session"] = session
        state["mission"] = {"id": mission_id, "next": 1, "total": 1, "done": [],
                            **({"cancelled_at": rc.iso(T0)} if cancelled else {"completed_at": rc.iso(T0)})}
        rc.mission_store(self.root, mission_id).save(state, T0)


def test_roadmap_is_off_until_started_and_needs_a_recorded_directive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch)
    assert road.step() is None and not rc.missions_dir(road.root).exists()
    assert rc.main(["roadmap", "start", "--authority", "L20261020121757-26d5"]) == 2  # not a recorded directive
    assert rc.main(["roadmap", "stop", "--note", "x"]) == 2  # nothing to stop
    assert rc.main(["roadmap", "start", "--authority", DIRECTIVE]) == 0
    outcome = road.step()
    assert outcome and outcome.startswith("roadmap:submitted:")
    [(mission, _, problem)] = rc.load_missions(road.root)
    assert problem is None and mission["authority"] == DIRECTIVE and len(mission["tasks"]) == 1
    assert "cycle 1" in mission["tasks"][0]["text"] and "docs/PROCESS.md" in mission["tasks"][0]["text"]


def test_roadmap_keeps_one_cycle_queued_and_never_runs_beside_a_queued_mission(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch, "r0", "r1", "r2")
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    first = road.step().split(":")[2]
    assert road.step() == f"roadmap:waiting:{first}"  # its cycle has not finished: no second one
    road.finish(first)
    second = road.step().split(":")[2]
    assert second != first and rc.load_roadmap(road.root)["cycle"] == 2
    road.finish(second)
    assert road.step().startswith("roadmap:submitted:")
    events = [json.loads(line)["event"] for line in (rc.missions_dir(road.root) / "journal.jsonl").read_text().splitlines()]
    assert events.count("roadmap_cycle_submitted") == 3 and events[0] == "roadmap_started"


def test_roadmap_pauses_when_the_coordinator_reports_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch, "r0", "r1")
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    first = road.step().split(":")[2]
    road.finish(first, relay_status="blocked")
    assert road.step() == "roadmap:paused:coordinator_blocked"
    assert road.step() == "roadmap:paused:coordinator_blocked" and len(rc.load_missions(road.root)) == 1
    assert rc.main(["roadmap", "start", "--authority", DIRECTIVE]) == 0  # the maintainer resumes it
    assert road.step().startswith("roadmap:submitted:")  # the blocked cycle is not re-judged


def test_roadmap_pauses_after_one_cycle_without_commit_pr_merge_or_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch, "r0", "r1", "r1", "r1", "r1")
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    outcomes = []
    while (outcome := road.step()).startswith("roadmap:submitted:"):
        outcomes.append(outcome)
        road.finish(outcome.split(":")[2])
    # Baseline r0; cycle 1 moved the heads to r1 (progress); cycle 2 did no durable work.
    assert outcome == "roadmap:paused:no_progress" and len(outcomes) == 1 + rc.ROADMAP_NO_PROGRESS


def test_roadmap_no_progress_resumes_only_on_a_new_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch, "r0", "r0", "r0", "r1")
    ci = {"value": "ci0"}
    monkeypatch.setattr(rc, "roadmap_ci_fingerprint", lambda repo: ci["value"])
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    first = road.step().split(":")[2]
    road.finish(first)
    assert road.step() == "roadmap:paused:no_progress"
    assert road.step() == "roadmap:paused:no_progress"
    ci["value"] = "ci1"
    assert road.step().startswith("roadmap:submitted:")


def test_roadmap_pr_creation_counts_as_progress_and_wakes_pause(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch, "r0", "r0", "r0", "r0", "r0")
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    first = road.step().split(":")[2]
    road.prs.append(933)
    road.finish(first)
    second = road.step().split(":")[2]
    assert rc.load_roadmap(road.root)["no_progress"] == 0
    road.finish(second)
    assert road.step() == "roadmap:paused:no_progress"
    road.prs.append(934)
    assert road.step().startswith("roadmap:submitted:")


def test_roadmap_registered_job_counts_as_progress_and_job_result_wakes_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch, "r0", "r0", "r0", "r0", "r0")
    jobs: dict[str, str] = {}
    monkeypatch.setattr(rc, "roadmap_job_ids", lambda root: sorted(jobs))
    monkeypatch.setattr(rc, "roadmap_job_states", lambda root: jobs.copy())
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    first = road.step().split(":")[2]
    jobs["j-20261025T094500Z-123456"] = "running"
    road.finish(first)
    second = road.step().split(":")[2]
    assert rc.load_roadmap(road.root)["no_progress"] == 0
    road.finish(second)
    assert road.step() == "roadmap:paused:no_progress"
    jobs["j-20261025T094500Z-123456"] = "succeeded"
    assert road.step().startswith("roadmap:submitted:")


def test_roadmap_no_progress_waits_for_known_provider_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch, "r0", "r0", "r0", "r0", "r0")
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    first = road.step().split(":")[2]
    road.finish(first)
    store = rc.mission_store(road.root, first)
    state = store.load()
    state["cooldowns"] = {"claude": {"until": rc.iso(T0 + timedelta(minutes=4))}}
    store.save(state, T0)
    assert road.step() == "roadmap:paused:no_progress"
    assert road.step() == "roadmap:paused:no_progress"
    assert road.step().startswith("roadmap:submitted:")


def test_roadmap_waits_while_remote_heads_are_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch, None, "r0", "r1", None, "r2")
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    assert road.step() == "roadmap:waiting:remote_unreadable" and not rc.missions_dir(road.root).joinpath("x").exists()
    first = road.step().split(":")[2]
    road.finish(first)
    second = road.step().split(":")[2]
    road.finish(second)
    assert road.step() == "roadmap:waiting:remote_unreadable"
    assert road.step().startswith("roadmap:submitted:") and rc.load_roadmap(road.root)["no_progress"] == 0


def test_roadmap_pauses_on_a_cancelled_cycle_and_on_maintainer_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch, "r0", "r1")
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    first = road.step().split(":")[2]
    road.finish(first, cancelled=True)
    assert road.step() == "roadmap:paused:cycle_cancelled"
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    assert road.step().startswith("roadmap:submitted:")
    assert rc.main(["roadmap", "stop"]) == 2  # a note is required
    assert rc.main(["roadmap", "stop", "--note", "reviewing direction"]) == 0
    assert road.step() == "roadmap:paused:maintainer_stop"
    rc.roadmap_path(road.root).write_text("{")
    assert road.step() == "roadmap:corrupt" and rc.main(["roadmap", "start", "--authority", DIRECTIVE]) == 2


def test_roadmap_pause_notification_uses_the_fixed_template() -> None:
    assert rc.render("roadmap_paused", {"owner": "control-plane", "reason": "no_progress"})
    assert rc.render("roadmap_paused", {"owner": "control-plane", "reason": "anything else"}) is None


def test_roadmap_stop_is_never_overwritten_by_a_concurrent_owner_step(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch, "r0", "r1")
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    road.finish(road.step().split(":")[2])
    monkeypatch.setattr(rc, "ROADMAP_LOCK_WAIT", 0.0)
    stops: list[int] = []

    def slow_remote() -> str:  # the maintainer stops while the owner is reading the remote heads
        stops.append(rc.main(["roadmap", "stop", "--note", "stop now"]))
        return "r1"
    road.fingerprint = slow_remote  # type: ignore[method-assign]
    assert road.step().startswith("roadmap:submitted:") and stops == [3]  # refused while the step held the lock
    assert rc.main(["roadmap", "stop", "--note", "stop now"]) == 0
    assert road.step() == "roadmap:paused:maintainer_stop"
    lock = rc.roadmap_lock(road.root)
    assert lock is not None
    try:
        assert road.step() == "roadmap:waiting:busy"  # and a held lock never lets the owner write around a stop
    finally:
        lock.release()


def test_roadmap_cycle_identity_survives_a_crash_around_submission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch, "r0")
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    real = road.submit

    def crash_before_queueing(authority: str, tasks: list[dict[str, Any]], mission_id: str) -> Any:
        raise OSError("killed before the mission file was written")
    road.submit = crash_before_queueing  # type: ignore[method-assign]
    with pytest.raises(OSError):
        road.step()
    recorded = rc.load_roadmap(road.root)["last_mission"]
    assert not rc.load_missions(road.root)

    def crash_after_queueing(authority: str, tasks: list[dict[str, Any]], mission_id: str) -> Any:
        real(authority, tasks, mission_id)
        raise OSError("killed during the ledger note")
    road.submit = crash_after_queueing  # type: ignore[method-assign]
    with pytest.raises(OSError):
        road.step()  # the recorded cycle is queued under its own id, not a new one
    [(mission, mid, _)] = rc.load_missions(road.root)
    assert mid == recorded and "cycle 1" in mission["tasks"][0]["text"]
    road.submit = real  # type: ignore[method-assign]
    road.finish(mid, relay_status="blocked")
    assert road.step() == "roadmap:paused:coordinator_blocked"  # the crashed-around cycle is still judged
    assert len(rc.load_missions(road.root)) == 1


def test_atomic_write_survives_a_stale_temp_file_from_a_killed_write(tmp_path: Path) -> None:
    target = tmp_path / "missions" / "m-20261018T080000Z-aaaaaa.json"
    target.parent.mkdir()
    (target.parent / f".{target.name}.{os.getpid()}.tmp").write_text("{")  # what a killed write left behind
    rc.write_json_atomic(target, {"ok": True})
    assert json.loads(target.read_text()) == {"ok": True}


def test_resumed_roadmap_reads_the_remote_before_submitting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch, "r0", "r1", None, "r2")
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    road.finish(road.step().split(":")[2], relay_status="blocked")
    assert road.step() == "roadmap:paused:coordinator_blocked"  # blocked is judged before any remote read
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    assert road.refs == ["r1", None, "r2"]
    road.refs.pop(0)  # consume r1 so the next read fails
    assert road.step() == "roadmap:waiting:remote_unreadable"  # a cached baseline is not a fresh reading
    assert len(rc.load_missions(road.root)) == 1
    assert road.step().startswith("roadmap:submitted:")


# ---------------------------------------------------------------------------
# Spec 204: evidence-backed completion, legacy missions and checkpoints


def legacy_done(task: int, session: str) -> dict[str, Any]:
    """A completion recorded by the pre-204 owner on a successful provider return alone."""
    return {"task": task, "session": session, "agent": "codex", "at": rc.iso(T0)}


def test_204_legacy_counted_tasks_are_reopened_not_completed(tmp_path: Path) -> None:
    # The m-20261021T125949Z-2e0739 shape: 3/3 counted on provider returns, no completed_at, owner stopped.
    env = Env(tmp_path, [("claude", SUCCESS, "blocked")])
    root = tmp_path / "control"
    mission = mission_def("phase A", "phase B", "phase C", checks={0: None, 1: None, 2: None},
                          mid="m-20261018T070000Z-aaaaaa")
    _write_mission(root, mission)
    env.head(SUCCESS)
    store = rc.mission_store(root, mission["id"])
    state = rc.default_state()
    state.update({"phase": "stopped", "head_session": HEAD, "chain": [HEAD], "permissions_digest": "perm0",
                  "permissions_pinned": True, "last_attempt": {"nonce": "rc-x", "agent": "codex", "attempt": 3,
                                                               "fingerprint": "fp0", "task": 2},
                  "mission": {"id": mission["id"], "next": 3, "total": 3,
                              "done": [legacy_done(0, HEAD), legacy_done(1, HEAD), legacy_done(2, HEAD)]}})
    store.save(state, T0)
    assert rc.mission_status(store)[0] == "active"  # never read as complete
    factory = lambda s, m: rc.Supervisor(s, env.host, env.limits, sender=env.sender)  # noqa: E731
    assert rc.serve_step(root, factory, conflicts=lambda: [], now=env.clock.now) == f"ran:{mission['id']}:held"
    after = store.load()
    rec = after["mission"]
    assert rec["next"] == 0 and rec["done"] == [] and [d["task"] for d in rec["reopened"]] == [0, 1, 2]
    assert "completed_at" not in rec and "mission_completed" not in journal_events(store)
    assert "mission_reopened" in journal_events(store) and "task_blocked" in journal_events(store)
    assert "phase A" in env.relay.launches[0]["task"]  # resumed at the first unverified task, on the same chain
    assert env.relay.launches[0]["parent"] == HEAD
    assert "no independent completion check" in env.relay.launches[0]["task"]


def test_204_completed_legacy_mission_is_not_reinterpreted_as_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                      capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "control"
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(root))
    mission = mission_def("old cycle", checks={0: None})
    _write_mission(root, mission)
    state = rc.default_state()
    state["mission"] = {"id": mission["id"], "next": 1, "total": 1, "done": [legacy_done(0, HEAD)],
                        "completed_at": rc.iso(T0)}
    rc.mission_store(root, mission["id"]).save(state, T0)
    assert rc.reopen_unverified(state) == []  # a finished record stays as it is (the audit judges it)
    assert rc.main(["missions"]) == 0
    [row] = json.loads(capsys.readouterr().out)["missions"]
    assert row["status"] == "complete" and row["verified"] is False and row["outcomes"] == ["legacy_unverified"]


def test_204_lying_provider_then_merged_evidence_completes_without_relaunch(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, [("claude", SUCCESS), ("claude", SUCCESS)])
    root = tmp_path / "control"
    mission = mission_def("merge it", "then this", checks={0: {"kind": "pr_merged", "pr": 7}})
    _write_mission(root, mission)
    calls: list[Any] = []
    # The provider claims the merge before it happened; the PR merges right after (e.g. once checks pass).
    env.host.task_check = lambda check: calls.append(check) or ("PR #7 is OPEN" if len(calls) == 1 else None)
    factory = lambda store, m: rc.Supervisor(store, env.host, env.limits)  # noqa: E731
    assert rc.serve_step(root, factory, conflicts=lambda: [], now=env.clock.now) == f"ran:{mission['id']}:idle"
    store = rc.mission_store(root, mission["id"])
    assert "task_check_failed" in journal_events(store)
    assert len(env.relay.launches) == 2 and "then this" in env.relay.launches[1]["task"]  # task 1 never relaunched
    done = store.load()["mission"]["done"]
    assert [(d["task"], d["outcome"]) for d in done] == [(0, "verified"), (1, "verified")]


def test_204_relay_status_routes_but_never_completes(tmp_path: Path) -> None:
    env = Env(tmp_path / "blocked", [("claude", SUCCESS, "blocked")])
    checks: list[Any] = []
    env.host.task_check = lambda check, seen=checks: seen.append(check)
    mission = mission_def("do it")
    start_mission(env, mission)
    result, sup = run_mission(env, mission)
    assert result == "held" and sup.state["hold"]["reason"] == "task_blocked"
    assert checks == [] and sup.state["mission"]["next"] == 0
    # An unknown report is a checkpoint: the task is resumed, and completes only on a checked claim.
    env = Env(tmp_path / "unknown", [("claude", SUCCESS, "needs_review"), ("claude", SUCCESS)])
    checks.clear()
    env.host.task_check = lambda check, seen=checks: seen.append(check)
    start_mission(env, mission)
    result, sup = run_mission(env, mission)
    assert result == "idle" and len(checks) == 1 and len(env.relay.launches) == 2
    assert "task_report_unverified" in journal_events(env.store)


def test_204_unreadable_remote_evidence_fails_closed(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)] * rc.MAX_TASK_CHECKPOINTS)

    def offline(check: dict[str, Any]) -> str | None:
        raise TimeoutError("gh timed out")
    env.host.task_check = offline
    mission = mission_def("merge it")
    start_mission(env, mission)
    result, sup = run_mission(env, mission)
    # Never completed on an unverifiable check; resumed a bounded number of times, then held.
    assert result == "held" and sup.state["hold"]["reason"] == "task_checkpoint_limit"
    assert sup.state["mission"]["next"] == 0
    assert "unverifiable" in [json.loads(x) for x in env.store.journal_path.read_text().splitlines()
                              if '"task_check_failed"' in x][0]["problem"]


def test_204_checkpoint_bound_survives_restarts(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS, "continue")])
    mission = mission_def("long task")
    start_mission(env, mission)
    state = env.store.load()
    state["mission"]["checkpoints"] = {"0": rc.MAX_TASK_CHECKPOINTS - 1}  # earlier owner runs checkpointed it
    env.store.save(state, T0)
    result, sup = run_mission(env, mission)
    assert result == "held" and sup.state["hold"]["reason"] == "task_checkpoint_limit"
    assert sup.state["mission"]["next"] == 0 and len(env.relay.launches) == 1


def test_204_contract_attaches_only_to_pending_unchecked_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)])
    root = tmp_path / "control"
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(root))
    monkeypatch.setattr(rc, "make_host", lambda repo: env.host)
    mission = mission_def("legacy", "checked", checks={0: None, 1: {"kind": "pr_merged", "pr": 2}})
    _write_mission(root, mission)
    spec = '{"kind": "spec_merged", "spec": "204"}'
    base = ["contract", "--mission", mission["id"], "--note", "spec 204 row is the deliverable"]
    assert rc.main([*base, "--task", "2", "--check", spec]) == 2  # immutable definition contract
    assert rc.main([*base, "--task", "1", "--check", '{"kind": "shell"}']) == 2
    assert rc.main([*base, "--task", "1", "--check", '{"kind": "coordinator_cycle"}']) == 2  # not a roadmap cycle
    assert rc.main([*base, "--task", "1", "--check", spec]) == 0
    store = rc.mission_store(root, mission["id"])
    assert store.load()["mission"]["contracts"] == {"0": {"kind": "spec_merged", "spec": "204"}}
    assert "contract_attached" in journal_events(store)
    seen: list[Any] = []
    env.host.task_check = lambda check: seen.append(check)
    factory = lambda s, m: rc.Supervisor(s, env.host, env.limits)  # noqa: E731
    env.relay.script = [("claude", SUCCESS), ("claude", SUCCESS)]
    assert rc.serve_step(root, factory, conflicts=lambda: [], now=env.clock.now) == f"ran:{mission['id']}:idle"
    assert seen == [{"kind": "spec_merged", "spec": "204"}, {"kind": "pr_merged", "pr": 2}]
    assert "spec 204 is `merged`" in env.relay.launches[0]["task"]
    assert rc.main([*base, "--task", "1", "--check", spec]) == 2  # complete now


def test_204_check_task_spec_merged_reads_the_remote_registry(tmp_path: Path) -> None:
    registry = ("| Spec | State | PRs |\n|---|---|---|\n"
                "| 203 | merged | [#854](https://x/854) | ROADMAP |\n"
                "| 204 | merged | [#865](https://x/865), [#870](https://x/870) | MISSION |\n"
                "| 205 | in_review | [#871](https://x/871) | X |\n| 206 | merged | — | Y |\n")
    states = {"865": "MERGED", "870": "MERGED", "854": "MERGED"}

    def remote(args: list[str], **_: Any) -> tuple[int, str]:
        if args[:2] == ["gh", "api"]:
            assert "ref=master" in args[-1]
            return 0, registry
        return 0, states[args[3]] + "\n"
    check = lambda spec, run=remote: rc.check_task(tmp_path, {"kind": "spec_merged", "spec": spec}, run=run)  # noqa: E731
    assert check("204") is None
    states["870"] = "OPEN"
    assert check("204") == "PR #870 is OPEN"
    assert check("205") == "spec 205 is in_review in STATUS.md"
    assert check("206") == "spec 206 links no PR in STATUS.md"
    assert check("299") == "spec 299 has 0 STATUS.md rows"
    assert check("204", lambda *a, **k: (1, "")) == "STATUS.md on GitHub master is unreadable"
    assert rc.check_task(tmp_path, {"kind": "coordinator_cycle"}, run=lambda *a, **k: (0, "abc\trefs/heads/master\n")) is None
    assert rc.check_task(tmp_path, {"kind": "coordinator_cycle"}, run=lambda *a, **k: (128, ""))
    assert rc.validate_check({"kind": "spec_merged", "spec": "100g"}) is None
    assert rc.validate_check({"kind": "spec_merged", "spec": "../x"})
    assert rc.validate_check({"kind": "coordinator_cycle", "pr": 1})


def test_204_roadmap_cycle_settles_on_blocked_as_unverified_cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch, "r0", "r1")
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    cycle = road.step().split(":")[2]
    [(mission, _, _)] = rc.load_missions(road.root)
    assert mission["tasks"][0]["check"] == {"kind": "coordinator_cycle"}
    env = Env(tmp_path / "exec", [("claude", SUCCESS, "blocked")])
    env.relay.repo = road.repo
    env.relay.sessions_dir().mkdir(parents=True, exist_ok=True)
    seen: list[Any] = []
    env.host.task_check = lambda check: seen.append(check)
    factory = lambda s, m: rc.Supervisor(s, env.host, env.limits)  # noqa: E731
    assert rc.serve_step(road.root, factory, conflicts=lambda: [], now=env.clock.now) == f"ran:{cycle}:idle"
    done = rc.mission_store(road.root, cycle).load()["mission"]["done"]
    assert seen == [{"kind": "coordinator_cycle"}] and [(d["outcome"], d["status"]) for d in done] == [("cycle_settled", "blocked")]
    assert road.step() == "roadmap:paused:coordinator_blocked"


def test_204_submit_refuses_coordinator_cycle_contracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch)
    spec = tmp_path / "m.json"
    spec.write_text(json.dumps({"authority": DIRECTIVE, "tasks": [{"text": "x", "check": {"kind": "coordinator_cycle"}}]}))
    assert rc.main(["submit", "--mission-file", str(spec)]) == 2
    spec.write_text(json.dumps({"authority": DIRECTIVE, "tasks": [{"text": "x", "check": {"kind": "spec_merged", "spec": "204"}}]}))
    assert rc.main(["submit", "--mission-file", str(spec)]) == 0
    assert len(rc.load_missions(road.root)) == 1


def test_204_legacy_completion_is_reopened_after_a_pending_final_task_settles(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS, "blocked")])
    root = tmp_path / "control"
    mission = mission_def("first", "second", checks={0: None}, mid="m-20261018T070000Z-aaaaaa")
    _write_mission(root, mission)
    nonce = "rc-20261018T080000Z-0badf00d"
    store = rc.mission_store(root, mission["id"])
    state = rc.default_state()
    state.update({"phase": "stopped", "head_session": HEAD, "chain": [HEAD], "permissions_digest": "perm0",
                  "permissions_pinned": True,
                  "mission": {"id": mission["id"], "next": 1, "total": 2, "done": [legacy_done(0, HEAD)]},
                  "pending": {"nonce": nonce, "agent": "claude", "parent": HEAD, "started_at": rc.iso(T0), "pid": None,
                              "pid_start": None, "fingerprint": "fp0", "attempt": 1, "task": 1}})
    store.save(state, T0)
    store.journal({"event": "launch", "nonce": nonce})
    write_turn(env.relay.sessions_dir() / "20261018-080100-cccccc", SUCCESS,
               prompt=relay_prompt(f"[relay-continuity attempt {nonce}]"))
    factory = lambda s, m: rc.Supervisor(s, env.host, env.limits)  # noqa: E731
    assert rc.serve_step(root, factory, conflicts=lambda: [], now=env.clock.now) == f"ran:{mission['id']}:held"
    rec = store.load()["mission"]
    assert "completed_at" not in rec and rec["next"] == 0 and "mission_completed" not in journal_events(store)
    assert "mission_reopened" in journal_events(store) and "first" in env.relay.launches[0]["task"]


@pytest.mark.parametrize("check,valid", [
    ({"kind": "host_receipt", "op": "service.status"}, True),
    ({"kind": "host_receipt", "op": "service.status", "sha": "a" * 40}, True),
    ({"kind": "host_receipt", "op": "shell"}, False),
    ({"kind": "host_receipt"}, False),
    ({"kind": "host_receipt", "op": "service.status", "sha": None}, False),
    ({"kind": "host_receipt", "op": "service.status", "sha": "bad"}, False),
    ({"kind": "host_receipt", "op": "service.status", "extra": 1}, False),
])
def test_host_receipt_contract_validation(check, valid):
    assert (rc.validate_check(check) is None) == valid


def test_host_receipt_evaluation(tmp_path, monkeypatch):
    calls = []

    def find(paths, op, sha):
        calls.append((paths.home, op, sha))
        return None

    monkeypatch.setattr(rc.host_ops, "find_ok_receipt", find)
    check = {"kind": "host_receipt", "op": "service.status", "sha": "a" * 40}
    assert rc.check_task(tmp_path, check)
    assert calls == [(Path.home(), "service.status", "a" * 40)]
    monkeypatch.setattr(rc.host_ops, "find_ok_receipt", lambda *a: {"outcome": "ok"})
    assert rc.check_task(tmp_path, check) is None


def test_roadmap_hostops_waits_for_all_verified_receipts(tmp_path, monkeypatch):
    road = Roadmap(tmp_path, monkeypatch, "r0", "r1")
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    first = road.step().split(":")[2]
    road.finish(first, relay_status="blocked")
    state = rc.mission_store(road.root, first).load()
    turn = rc.last_turn_dir(road.repo / ".agent-relay/sessions" / state["head_session"])
    ids = ["a" * 32, "b" * 32]
    (turn / "last-message.txt").write_text("\n".join(f"HOSTOPS-WAIT: {i}" for i in ids) + "\nRELAY_STATUS blocked")
    assert road.step() == "roadmap:paused:coordinator_blocked"
    assert rc.load_roadmap(road.root)["hostops_wait"] == ids
    available = {}
    monkeypatch.setattr(rc.host_ops, "load_receipt", lambda paths, rid: (available.get(rid), ""))
    assert road.step() == "roadmap:paused:coordinator_blocked"
    available[ids[0]] = {"outcome": "refused"}
    assert road.step() == "roadmap:paused:coordinator_blocked"
    available[ids[1]] = {"outcome": "failed"}
    assert road.step().startswith("roadmap:submitted:")
    assert not rc.load_roadmap(road.root).get("hostops_wait")
    assert rc.load_roadmap(road.root)["cycle"] == 2


# ---------------------------------------------------------------------------
# Process ownership, migration of the earlier controller's state, and the 2026-10-22 incident


REAPER_PROBE = r'''
import os, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
import relay_continuity as rc
assert rc.become_subreaper()
# A turn in its own session that leaves detached work behind in every common way, then exits.
subprocess.run(["sh", "-c", "setsid sleep 301 & nohup sleep 302 >/dev/null 2>&1 & (setsid sh -c 'sleep 303' &) ; "
                "python3 -c 'import os,time\nif os.fork()==0:\n os.setsid()\n if os.fork()==0:\n  time.sleep(304)\n "
                "os._exit(0)\nos._exit(0)'; sleep 0.3"], start_new_session=True, check=True)
time.sleep(0.5)
left = rc.descendants(os.getpid())
print(len(left), ",".join(rc.reap_descendants(grace=2.0)), len(rc.descendants(os.getpid())))
'''


@pytest.mark.skipif(not Path("/proc/self/task").exists(), reason="Linux process table")
def test_subreaper_owns_and_stops_everything_a_turn_detached(tmp_path: Path) -> None:
    out = subprocess.run([sys.executable, "-c", REAPER_PROBE, str(SCRIPT.parent)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    found, names, after = out.stdout.split()
    assert int(found) >= 4 and after == "0"  # setsid, nohup, setsid sh and a double fork were all owned
    assert {"sleep", "python3"} <= set(names.split(","))


def test_reaper_escalates_to_sigkill_and_never_reports_arguments(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    (proc / "100" / "task" / "100").mkdir(parents=True)
    (proc / "100" / "task" / "100" / "children").write_text("200")
    (proc / "200" / "task" / "200").mkdir(parents=True)
    (proc / "200" / "task" / "200" / "children").write_text("")
    (proc / "200" / "stat").write_text("200 (stubborn) S 100")
    (proc / "200" / "comm").write_text("stubborn --token=x\n")
    sent: list[int] = []
    clock = {"t": 0.0}

    def kill(pid: int, sig: int) -> None:
        sent.append(sig)
        if sig == signal.SIGKILL:
            (proc / "200" / "stat").write_text("200 (stubborn) Z 100")

    def sleep(seconds: float) -> None:
        clock["t"] += seconds
    names = rc.reap_descendants(1.0, pid=100, proc_root=proc, kill=kill, sleep=sleep, clock=lambda: clock["t"])
    assert sent[0] == signal.SIGTERM and sent[-1] == signal.SIGKILL and len(sent) <= 12
    assert names == ["stubborn_--token_x"]  # the name is sanitized; no argv is ever read
    assert rc.descendants(100, proc) == set()


def test_cli_run_refuses_without_process_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(tmp_path))
    monkeypatch.setattr(rc, "become_subreaper", lambda: False)
    monkeypatch.setattr(rc, "live_conflicts", lambda repo, own: [])
    assert rc.main(["run", "--session", HEAD, "--no-notify"]) == 2


def _held_state(env: Env, reason: str, turn: str = f"{HEAD}/turn-001", **extra: Any) -> None:
    state = rc.default_state()
    state.update({"phase": "held", "head_session": HEAD, "chain": [HEAD], "agent": "claude",
                  "permissions_digest": "perm0", "permissions_pinned": True,
                  "hold": {"reason": reason, "at": rc.iso(T0), "attempt": None, "turn": turn}, **extra})
    env.store.save(state, T0)


@pytest.mark.parametrize("reason", ["live_work", "reconcile_no_sandbox", "reconcile_failed", "auth_failure",
                                    "effects_uncertain", "circuit_attempts"])
def test_holds_the_current_policy_would_not_raise_are_decided_again(tmp_path: Path, reason: str) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.head([tool_use("x", "Bash", command="python3 scripts/capture.py"), *QUOTA])
    _held_state(env, reason)
    result, sup = env.run()
    assert result == "idle" and sup.state["hold"] is None
    assert [launch["agent"] for launch in env.relay.launches] == ["codex"]
    assert "hold_redecided" in journal_events(env.store)


def test_redecided_hold_on_a_moved_chain_or_another_session_is_kept(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.head(QUOTA)
    _held_state(env, "live_work", turn=f"{HEAD}/turn-000")  # the chain moved past the held turn
    result, sup = env.run()
    assert result == "held" and sup.state["hold"]["reason"] == "live_work" and sup.state["hold"]["resumable"] is False
    assert env.run()[0] == "held" and env.relay.launches == []
    env2 = Env(tmp_path / "other")
    env2.head(QUOTA)
    _held_state(env2, "live_work")
    assert env2.run(sid="20261018-090000-cccccc")[0] == "held" and env2.relay.launches == []


@pytest.mark.parametrize("reason", ["permissions_changed", "output_missing", "attempt_unresolved", "task_blocked",
                                    "no_provider_available"])
def test_holds_that_still_need_review_are_never_decided_again(tmp_path: Path, reason: str) -> None:
    env = Env(tmp_path)
    env.head(QUOTA)
    _held_state(env, reason)
    assert env.run()[0] == "held" and env.relay.launches == []


def test_a_pending_diagnosis_of_the_earlier_controller_is_abandoned_not_a_lost_attempt(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.head([tool_use("x", "Bash", command="./deploy.sh"), *QUOTA])
    diag = {"nonce": "rc-20261018T075900Z-0d0d0d0d", "agent": "codex", "parent": HEAD, "started_at": rc.iso(T0),
            "pid": None, "pid_start": None, "fingerprint": "fp0", "attempt": 1, "ops": ["x"]}
    state = rc.default_state()
    state.update({"phase": "waiting", "head_session": HEAD, "chain": [HEAD], "agent": "claude",
                  "permissions_digest": "perm0", "permissions_pinned": True,
                  "reconcile": {"turn": f"{HEAD}/turn-001", "since": rc.iso(T0), "attempts": 1, "verdicts": {},
                                "diag": diag}})
    env.store.save(state, T0)
    env.store.journal({"event": "launch", "purpose": "diagnostic", "nonce": diag["nonce"]})
    result, sup = env.run()
    assert result == "idle" and "reconcile" not in sup.state
    abandoned = [r for r in journal(env) if r.get("outcome") == "abandoned"]
    assert abandoned and abandoned[0]["nonce"] == diag["nonce"]


def test_incident_20261022_three_interrupted_local_commands_continue_on_recovered_claude(tmp_path: Path) -> None:
    # The preserved m-20261022T181241Z-3c9f97 shape: Codex ran out of quota while uvicorn, `npm run preview` and
    # a capture script were running; the earlier controller waited for Codex to diagnose them although Claude's
    # cooldown had ended. Now the processes are gone and Claude, available, continues the task at once.
    now = T0
    env = Env(tmp_path, [("claude", SUCCESS)])
    started = [{"type": "item.started", "item": {"id": f"i{k}", "type": "command_execution", "command": cmd}}
               for k, cmd in enumerate(["/bin/bash -lc '.venv/bin/python -m uvicorn app.main:app --port 8199'",
                                        "/bin/bash -lc 'npm run preview -- --port 4199'",
                                        "/bin/bash -lc 'python3 /tmp/capture_audit.py'"])]
    env.head([{"type": "thread.started"}, *started,
              {"type": "error", "message": "You've hit your usage limit. try again at 10:34 PM."},
              {"type": "turn.failed", "error": {"message": "You've hit your usage limit. try again at 10:34 PM."}}],
             agent="codex")
    mission = mission_def("Continue developing JarvisOS according to the roadmap",
                          checks={0: {"kind": "coordinator_cycle"}})
    state = rc.default_state()
    state.update({
        "phase": "waiting", "head_session": HEAD, "chain": [HEAD], "agent": "codex", "last_outcome": "quota",
        "permissions_digest": "perm0", "permissions_pinned": True,
        "counters": {"consecutive_failures": 1, "crash_retries": 0, "transient_streak": 0},
        "cooldowns": {"claude": {"reason": "quota", "since": rc.iso(now - timedelta(hours=2)), "source": "limits",
                                 "until": rc.iso(now - timedelta(minutes=10))},
                      "codex": {"reason": "quota", "since": rc.iso(now), "source": "limits",
                                "until": rc.iso(now + timedelta(hours=2))}},
        "last_attempt": {"agent": "codex", "attempt": 2, "fingerprint": "fp0", "nonce": "rc-x", "task": 0},
        "mission": {"id": mission["id"], "next": 0, "total": 1, "done": [], "checkpoints": {"0": 1}},
        "reconcile": {"attempts": 0, "diag": None, "since": rc.iso(now), "turn": f"{HEAD}/turn-001", "verdicts": {},
                      "waiting_since": rc.iso(now), "wake_at": rc.iso(now + timedelta(hours=2))}})
    env.store.save(state, now)
    result, sup = run_mission(env, mission, HEAD)
    assert result == "idle" and sup.state["hold"] is None
    assert [(launch["agent"], launch["at"]) for launch in env.relay.launches] == [("claude", now)]
    task = env.relay.launches[0]["task"]
    assert task.count(": INTERRUPTED") == 3 and rc.MISSION_HEADER in task
    # That turn ran under the stopped owner, not this one: the successor is told to check for survivors.
    assert rc.PROCESSES_UNKNOWN in task and rc.PROCESSES_STOPPED not in task
    assert sup.state["mission"]["completed_at"] and "reconcile" not in sup.state


def test_a_successful_turn_with_an_interrupted_background_push_parks_and_continues(tmp_path: Path) -> None:
    push = tool_use("p", "Bash", command="nohup git push origin HEAD >/dev/null 2>&1 &")
    env = Env(tmp_path)
    mission = mission_def("first", "second")
    start_mission(env, mission)
    env.relay.script = [("claude", [push, tool_result("p"), *SUCCESS]), ("claude", SUCCESS)]
    result, sup = run_mission(env, mission)
    assert result == "idle" and sup.state["hold"] is None
    assert len(env.relay.launches) == 2
    assert rc.PARKED_TEXT in env.relay.launches[1]["task"]
    assert sum(1 for kind, text in env.ledger if kind == "finding" and "parked interrupted" in text) == 1
    assert any("ha messo da parte un'operazione non verificabile" in e for e in env.events())


def test_an_interrupted_background_push_on_the_last_task_parks_instead_of_holding(tmp_path: Path) -> None:
    push = tool_use("p", "Bash", command="nohup git push origin HEAD >/dev/null 2>&1 &")
    env = Env(tmp_path, [("claude", [push, tool_result("p"), *SUCCESS])])
    mission = mission_def("only task")
    start_mission(env, mission)
    result, sup = run_mission(env, mission)
    assert result == "idle" and sup.state["hold"] is None
    assert sum(1 for kind, text in env.ledger if kind == "finding" and "parked interrupted" in text) == 1
    assert any("ha messo da parte un'operazione non verificabile" in e for e in env.events())


def test_the_next_task_is_told_which_processes_were_stopped(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS), ("claude", SUCCESS)])
    env.host.reap = lambda: ["node"] if len(env.relay.launches) == 1 else []
    mission = mission_def("first", "second")
    start_mission(env, mission)
    assert run_mission(env, mission)[0] == "idle"
    assert "stopped processes the previous turn left running (node)" in env.relay.launches[1]["task"]


def test_relay_run_ends_when_relay_exits_even_if_a_detached_process_keeps_its_output(tmp_path: Path) -> None:
    fake = tmp_path / "agent-relay"
    fake.write_text(f"#!/bin/sh\n(sleep 30 & echo $! > {tmp_path}/pid)\n"
                    "echo '{\"session_id\": \"20261018-080000-abcdef\"}'\necho note >&2\n")
    fake.chmod(0o755)
    adapter = rc.RelayAdapter(tmp_path, binary=str(fake))
    started = time.monotonic()
    proc = adapter.start("claude", None, "t", 1)
    result = adapter.finish(proc)
    assert time.monotonic() - started < 10  # not held until the detached `sleep 30` closes the inherited output
    assert result.session_id == "20261018-080000-abcdef" and "note" in result.diagnostics
    os.kill(int((tmp_path / "pid").read_text()), signal.SIGKILL)  # this test's own leftover


def test_reaper_reports_a_process_that_survives(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    for pid, children in ((100, "200"), (200, "")):
        (proc / str(pid) / "task" / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "task" / str(pid) / "children").write_text(children)
    (proc / "200" / "stat").write_text("200 (stuck) D 100")
    (proc / "200" / "comm").write_text("stuck\n")
    clock = {"t": 0.0}

    def sleep(seconds: float) -> None:
        clock["t"] += seconds
    with pytest.raises(RuntimeError, match="survived"):
        rc.reap_descendants(1.0, pid=100, proc_root=proc, kill=lambda pid, sig: None, sleep=sleep,
                            clock=lambda: clock["t"])


def test_nothing_launches_while_a_process_of_the_turn_survives(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", [IN_FLIGHT["server"], tool_result("x"), *QUOTA]), ("codex", CODEX_SUCCESS)])
    outcomes = iter([RuntimeError("1 process(es) survived SIGKILL")] * 3 + [["python3"]] + [[]] * 5)

    def reap() -> list[str]:
        item = next(outcomes)
        if isinstance(item, Exception):
            raise item
        return item
    env.host.reap = reap
    env.head(CRASH)
    result, sup = env.run()
    assert result == "idle" and [launch["agent"] for launch in env.relay.launches] == ["claude", "codex"]
    # Codex started only after the cleanup succeeded, three 30 s retries later.
    assert env.relay.launches[1]["at"] - env.relay.launches[0]["at"] >= timedelta(seconds=90)
    assert "stopped by the supervisor: python3" in env.relay.launches[1]["task"]


def test_a_stop_while_a_process_survives_keeps_the_attempt_pending(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", [IN_FLIGHT["server"], tool_result("x"), *QUOTA])])

    def reap() -> list[str]:
        env.store.stop_path.write_text("x")
        raise RuntimeError("1 process(es) survived SIGKILL")
    env.host.reap = reap
    env.head(CRASH)
    result, sup = env.run()
    assert result == "stopped" and sup.state["pending"] is not None  # adopted on restart, never relaunched
    assert len(env.relay.launches) == 1


# ---------------------------------------------------------------------------
# Spec 218: durable registered jobs, JOB-WAIT, and Codex transport exhaustion as transient

jobs = rc.relay_jobs

CODEX_ROUTING = ([{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}]
                 + [{"type": "error", "message": f"Reconnecting... {i}/5 (workspace routing discovery failed)"}
                    for i in range(1, 6)]
                 + [{"type": "error", "message": "workspace routing discovery failed"},
                    {"type": "turn.failed", "error": {"message": "workspace routing discovery failed"}}])


def test_codex_workspace_routing_failure_is_transient_not_unknown(tmp_path: Path) -> None:
    # The 2026-10-24 05:42-06:03Z outage: eight such turns were `unknown` and opened circuit_failures.
    turn = write_turn(tmp_path / "s", CODEX_ROUTING, agent="codex", status="")
    assert rc.classify_turn(turn).outcome == "transient"
    assert rc.classify_text("Codex couldn’t reach the service. Check your connection", None) == "transient"
    assert rc.classify_text("Reconnecting... 3/5 (stream closed)", None) == "transient"


def test_reaper_spares_registered_job_trees(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    tree = {100: [200, 300], 200: [210], 210: [], 300: [310], 310: []}
    for pid, children in tree.items():
        (proc / str(pid) / "task" / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "task" / str(pid) / "children").write_text(" ".join(map(str, children)))
        (proc / str(pid) / "stat").write_text(f"{pid} (p{pid}) S 1")
        (proc / str(pid) / "comm").write_text(f"p{pid}\n")
    killed: list[int] = []

    def kill(pid: int, sig: int) -> None:
        killed.append(pid)
        (proc / str(pid) / "stat").write_text(f"{pid} (p{pid}) Z 1")
    names = rc.reap_descendants(1.0, pid=100, proc_root=proc, kill=kill, sleep=lambda s: None,
                                clock=lambda: 0.0, keep=lambda: {300, 999})
    assert sorted(set(killed)) == [200, 210] and names == ["p200", "p210"]
    assert rc.descendants(100, proc) == {300, 310}  # the job wrapper and its solver keep running


def job_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "control"
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(root))
    monkeypatch.setattr(jobs, "allowed_roots", lambda: (tmp_path,))
    monkeypatch.setattr(jobs, "ledger", lambda *a: None)
    return root


def test_job_start_refuses_unbounded_or_unattributed_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job_env(tmp_path, monkeypatch)
    ok = {"label": "run", "authority": "spec 216", "cwd": tmp_path, "wall_hours": 1.0, "spawn": lambda c: None,
          "wait": 0.0}
    for bad in ({"label": "bad;label$"}, {"authority": " "}, {"wall_hours": 0.0}, {"wall_hours": 25.0},
                {"cwd": Path("/")}, {"inputs": [tmp_path / "missing"]}):
        with pytest.raises(ValueError):
            jobs.start(["true"], **{**ok, **bad})
    with pytest.raises(ValueError):
        jobs.start([], **ok)
    (tmp_path / "mesh.msh").write_text("mesh")
    job = jobs.start(["true"], **{**ok, "inputs": [Path("mesh.msh")], "spec": "216"})
    assert job["status"] == "starting" and job["inputs"][str(tmp_path / "mesh.msh")].startswith("sha256:")
    assert job["deadline"] > job["created_at"] and job["authority"] == "spec 216"


def test_job_limit_counts_only_live_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = job_env(tmp_path, monkeypatch)
    for i in range(jobs.MAX_RUNNING):
        jobs.write_json_atomic(root / "jobs" / f"j-20261025T0000{i:02d}Z-aaaaaa.json",
                               {"schema": jobs.SCHEMA, "id": f"j-20261025T0000{i:02d}Z-aaaaaa", "status": "running",
                                "pid": os.getpid(), "pid_start": jobs.pid_start(os.getpid())})
    # This test process is not a job wrapper, so every record above reads as lost and none blocks a new job.
    assert {jobs.effective_status(j) for j in jobs.all_jobs()} == {"lost"}
    assert jobs.start(["true"], label="x", authority="spec 216", cwd=tmp_path, wall_hours=1, spawn=lambda c: None,
                      wait=0)["status"] == "starting"
    monkeypatch.setattr(jobs, "is_wrapper", lambda job, proc_root=Path("/proc"): True)
    with pytest.raises(ValueError, match="already running"):
        jobs.start(["true"], label="x", authority="spec 216", cwd=tmp_path, wall_hours=1, spawn=lambda c: None, wait=0)


def wait_status(job_id: str, want: str, timeout: float = 20.0) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = jobs.load(job_id)
        if job and jobs.effective_status(job) == want:
            return job
        time.sleep(0.1)
    raise AssertionError(f"{job_id} never reached {want}: {jobs.load(job_id)}")


@pytest.mark.skipif(not Path("/proc/self/task").exists(), reason="Linux process table")
def test_job_lifecycle_success_timeout_cancel_and_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job_env(tmp_path, monkeypatch)
    common = {"authority": "spec 218 test", "cwd": tmp_path, "spec": "218",
              "retry_reason": "independent lifecycle regression case"}
    done = jobs.start(["sh", "-c", "echo solved; exit 3"], label="exit", wall_hours=1, **common)
    finished = wait_status(done["id"], "failed")
    assert finished["exit_code"] == 3 and "solved" in Path(finished["log"]).read_text()
    slow = jobs.start(["sleep", "300"], label="timeout", wall_hours=1.5 / 3600, **common)
    assert slow["status"] == "running" and slow["pid"] in jobs.live_roots()
    wait_status(slow["id"], "timeout")
    held = jobs.start(["sleep", "300"], label="cancel", wall_hours=1, **common)
    assert jobs.cancel(held["id"]) == "cancelling"
    assert wait_status(held["id"], "cancelled")["exit_code"] == -signal.SIGTERM
    assert jobs.cancel(held["id"]) == "cancelled"
    gone = jobs.start(["sleep", "300"], label="lost", wall_hours=1, **common)
    assert gone["pid"] in jobs.live_roots()
    end = time.monotonic() + 10
    while not (solver := rc.descendants(gone["pid"])) and time.monotonic() < end:
        time.sleep(0.05)
    os.kill(gone["pid"], signal.SIGKILL)  # what an owner restart does to the job's namespace
    wait_status(gone["id"], "orphaned")
    assert gone["pid"] not in jobs.live_roots()
    assert solver & jobs.live_roots()  # preserve verified live command until reconciliation
    for pid in solver:
        os.kill(pid, signal.SIGKILL)
    wait_status(gone["id"], "lost")


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "lost", "timeout"])
def test_roadmap_job_wait_resumes_when_every_job_finished(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        terminal: str) -> None:
    road = Roadmap(tmp_path, monkeypatch, "r0", "r1")
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    first = road.step().split(":")[2]
    road.finish(first, relay_status="blocked")
    state = rc.mission_store(road.root, first).load()
    turn = rc.last_turn_dir(road.repo / ".agent-relay/sessions" / state["head_session"])
    ids = ["j-20261025T080000Z-aaaaaa", "j-20261025T080001Z-bbbbbb"]
    (turn / "last-message.txt").write_text("\n".join(f"JOB-WAIT: {i}" for i in ids) + "\nRELAY_STATUS blocked")
    assert road.step() == "roadmap:paused:coordinator_blocked"
    assert rc.load_roadmap(road.root)["job_wait"] == ids
    assert road.step() == "roadmap:paused:coordinator_blocked"  # unknown ids never resume by themselves

    def record(job_id: str, status: str) -> None:
        jobs.write_json_atomic(road.root / "jobs" / f"{job_id}.json",
                               {"schema": jobs.SCHEMA, "id": job_id, "status": status, "pid": None, "pid_start": None})
    record(ids[0], "succeeded")
    record(ids[1], "running")
    monkeypatch.setattr(jobs, "is_wrapper", lambda job, proc_root=Path("/proc"): True)
    assert road.step() == "roadmap:paused:coordinator_blocked"
    record(ids[1], terminal)
    assert road.step().startswith("roadmap:submitted:")
    assert not rc.load_roadmap(road.root).get("job_wait")


JOB_PROBE = r'''
import os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import relay_continuity as rc
jobs = rc.relay_jobs
assert rc.become_subreaper()
cwd = sys.argv[2]
turn = ("import sys; sys.path.insert(0, %r); import relay_jobs as j; from pathlib import Path; "
        "j.allowed_roots = lambda: (Path(%r),); j.ledger = lambda *a: None; "
        "print(j.start(['sh', '-c', 'sleep 400; echo never'], label='cfd', authority='spec 218 probe', "
        "cwd=Path(%r), wall_hours=1)['id'])") % (sys.argv[1], cwd, cwd)
# Turn 1 registers a job and leaves a stray server behind; then two handoffs each reap what their turn left.
out = subprocess.run(["sh", "-c", f"setsid sleep 401 >/dev/null 2>&1 & {sys.executable} -c \"{turn}\""], start_new_session=True,
                     check=True, capture_output=True, text=True).stdout.split()[-1]
time.sleep(0.5)
stray1 = rc.reap_descendants(grace=2.0, keep=jobs.live_roots)
subprocess.run(["sh", "-c", "setsid sleep 402 &"], start_new_session=True, check=True)
time.sleep(0.3)
stray2 = rc.reap_descendants(grace=2.0, keep=jobs.live_roots)
alive = jobs.effective_status(jobs.load(out))
jobs.cancel(out)
end = time.time() + 20
while jobs.effective_status(jobs.load(out)) == "running" and time.time() < end:
    time.sleep(0.1)
time.sleep(0.3)
rc._reap_zombies()
print(alive, jobs.effective_status(jobs.load(out)), ",".join(stray1), ",".join(stray2),
      len(rc.descendants(os.getpid())))
'''


@pytest.mark.skipif(not Path("/proc/self/task").exists(), reason="Linux process table")
def test_registered_job_survives_two_turn_handoffs_while_strays_are_reaped(tmp_path: Path) -> None:
    env = {**os.environ, "RELAY_CONTINUITY_DIR": str(tmp_path / "control")}
    out = subprocess.run([sys.executable, "-c", JOB_PROBE, str(SCRIPT.parent), str(tmp_path)], capture_output=True,
                         text=True, timeout=90, env=env)
    assert out.returncode == 0, out.stderr
    alive, final, stray1, stray2, left = out.stdout.split()
    assert (alive, final, left) == ("running", "cancelled", "0")
    assert stray1 == "sleep" and stray2 == "sleep"  # the strays went; the job tree was never named


def test_concurrent_starts_never_exceed_the_job_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    job_env(tmp_path, monkeypatch)
    results: list[str] = []

    def one(index: int) -> None:
        try:
            jobs.start(["true", str(index)], label="race", authority="spec 218", cwd=tmp_path, wall_hours=1,
                       spawn=lambda c: time.sleep(0.05), wait=0)
            results.append("ok")
        except ValueError:
            results.append("refused")
    threads = [threading.Thread(target=one, args=(i,)) for i in range(3 * jobs.MAX_RUNNING)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == jobs.MAX_RUNNING  # unlaunched `starting` records hold their slots


@pytest.mark.skipif(not Path("/proc/self/task").exists(), reason="Linux process table")
def test_job_end_stops_everything_the_command_left_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job_env(tmp_path, monkeypatch)
    marker = tmp_path / "bg.pid"
    job = jobs.start(["sh", "-c", f"setsid sh -c 'echo $$ > {marker}; exec sleep 300' >/dev/null 2>&1 & sleep 0.5"],
                     label="leaky", authority="spec 218 test", cwd=tmp_path, wall_hours=1)
    done = wait_status(job["id"], "succeeded")
    assert done["leftovers_stopped"] >= 1 and "survivors" not in done
    background = int(marker.read_text())
    assert not rc._running(background, Path("/proc"))  # stopped (at most a zombie awaiting its new parent)


STOP_TREE_PROBE = r'''
import os, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
import relay_jobs as j
assert j.become_subreaper()
# The command exits on SIGTERM; a detached grandchild ignores SIGTERM and must still be stopped.
child = subprocess.Popen(["sh", "-c", "setsid sh -c 'trap \"\" TERM; while :; do sleep 1; done' & exec sleep 300"])
time.sleep(0.5)
before = len(j._descendants(os.getpid()))
t0 = time.monotonic()
others = j._stop_tree(child, 1.0)
print(before, others, len(j._descendants(os.getpid())), child.returncode, round(time.monotonic() - t0))
'''


@pytest.mark.skipif(not Path("/proc/self/task").exists(), reason="Linux process table")
def test_stop_tree_escalates_for_descendants_that_ignore_sigterm(tmp_path: Path) -> None:
    out = subprocess.run([sys.executable, "-c", STOP_TREE_PROBE, str(SCRIPT.parent)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    before, others, after, code, seconds = out.stdout.split()
    assert int(before) >= 2 and int(others) >= 1 and after == "0"
    assert code == str(-signal.SIGTERM) and int(seconds) <= 5


def test_wrapper_publishes_no_terminal_status_while_a_descendant_survives(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    # Peer review P1: a worker stuck in uninterruptible I/O outlives SIGKILL for a while. The job must stay
    # `running` (holding its slot and any JOB-WAIT) until it is really gone.
    root = job_env(tmp_path, monkeypatch)
    job = jobs.start(["true"], label="stuck", authority="spec 218", cwd=tmp_path, wall_hours=1, spawn=lambda c: None,
                     wait=0)

    class Child:
        pid, returncode = 4242, 0

        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0
    survive = {"rounds": 0}
    writes: list[tuple[str, int]] = []

    def descendants(pid: int, proc_root: Path = Path("/proc")) -> set[int]:
        return {4243} if survive["rounds"] < 400 else set()  # beyond the bounded SIGTERM/SIGKILL rounds

    def sleep(seconds: float) -> None:
        survive["rounds"] += 1
    real_write = jobs.write_json_atomic
    monkeypatch.setattr(jobs, "write_json_atomic", lambda p, d: (writes.append((d["status"], survive["rounds"])),
                                                                 real_write(p, d)))
    monkeypatch.setattr(jobs, "become_subreaper", lambda: True)
    monkeypatch.setattr(jobs, "_descendants", descendants)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: Child())
    monkeypatch.setattr(jobs.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(jobs.time, "sleep", sleep)
    monkeypatch.setattr(jobs.signal, "signal", lambda *a: None)
    assert jobs.run_wrapper(job["id"], root, grace=0.0) == 0
    assert writes[-1] == ("succeeded", 400)  # written only after the last descendant had gone
    assert all(status == "running" for status, _ in writes[:-1])


def test_final_status_comes_from_the_reply_not_relays_rewritten_state(tmp_path: Path) -> None:
    # Cycle 87: the reply ended propose_done, but Relay's word scan of the whole stream recorded `continue`.
    session = tmp_path / ".agent-relay" / "sessions" / HEAD
    reply = ('done\nRELAY_STATE: {"summary":"x"}\nRELAY_STATUS: {"status":"propose_done","reason":"merged",'
             '"remaining_work":[],"verification":["ci"]}')
    write_turn(session, [{"type": "result", "subtype": "success", "is_error": False, "terminal_reason": "completed",
                          "result": reply}], status="continue")
    assert rc.final_relay_status(tmp_path, HEAD) == "propose_done"
    # A later, malformed or unknown line never overrides an earlier valid one; no line falls back to the state.
    assert rc.reply_relay_status(reply + "\nRELAY_STATUS: {broken") == "propose_done"
    assert rc.reply_relay_status('RELAY_STATUS: {"status":"done"}') is None
    write_turn(session, [{"type": "result", "subtype": "success", "is_error": False, "terminal_reason": "completed",
                          "result": "no status line"}], status="blocked")
    assert rc.final_relay_status(tmp_path, HEAD) == "blocked"
    assert rc.final_relay_status(tmp_path, None) is None


def test_roadmap_unknown_after_namespace_restart_wakes_once_for_diagnosis(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    road = Roadmap(tmp_path, monkeypatch, "r0", "r1", "r2")
    rc.main(["roadmap", "start", "--authority", DIRECTIVE])
    first = road.step().split(":")[2]
    road.finish(first, relay_status="blocked")
    ids = ["j-20261025T080000Z-aaaaaa", "j-20261025T080001Z-bbbbbb"]

    def wait_reply(mid: str) -> None:
        state = rc.mission_store(road.root, mid).load()
        turn = rc.last_turn_dir(road.repo / ".agent-relay/sessions" / state["head_session"])
        (turn / "last-message.txt").write_text("\n".join(f"JOB-WAIT: {i}" for i in ids) + "\nRELAY_STATUS blocked")

    wait_reply(first)
    for jid in ids:
        jobs.write_json_atomic(jobs.job_path(jid, road.root),
                               {"schema": jobs.SCHEMA, "id": jid, "status": "running"})
    statuses = {ids[0]: "unknown", ids[1]: "running"}
    monkeypatch.setattr(jobs, "effective_status", lambda job: statuses[job["id"]])
    assert road.step() == "roadmap:paused:coordinator_blocked"
    assert road.step() == "roadmap:paused:coordinator_blocked"  # verified work still alive: no premature wake
    statuses[ids[1]] = "orphaned"
    assert road.step() == "roadmap:paused:coordinator_blocked"
    statuses[ids[1]] = "succeeded"
    second = road.step().split(":")[2]
    assert second != first  # one diagnostic turn, not a fabricated terminal outcome
    assert jobs.load(ids[0], road.root)["status"] == "running"
    road.finish(second, relay_status="blocked")
    wait_reply(second)
    assert road.step() == "roadmap:paused:coordinator_blocked"
    assert road.step() == "roadmap:paused:coordinator_blocked"  # durable dedupe, not a polling-turn loop
    statuses[ids[0]] = "lost"
    assert road.step().startswith("roadmap:submitted:")


def test_idle_owner_reaps_adopted_jobs_before_next_mission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(tmp_path / "control"))
    monkeypatch.setattr(rc, "in_sandbox", lambda: True)
    monkeypatch.setattr(rc, "become_subreaper", lambda: True)
    monkeypatch.setattr(rc, "sandbox_provider_env", lambda home: {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude")})
    monkeypatch.setattr(rc, "link_relay_claude_credentials", lambda *args: None)
    monkeypatch.setattr(rc, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(rc.shutil, "which", lambda name: "/test/claude")
    monkeypatch.setattr(rc.signal, "signal", lambda *args: None)
    calls = []
    monkeypatch.setattr(rc, "_reap_zombies", lambda: calls.append("reap"))
    monkeypatch.setattr(rc, "serve_step", lambda *args, **kwargs: calls.append("serve") or "empty")
    monkeypatch.setattr(rc, "roadmap_step", lambda *args, **kwargs: calls.append("roadmap") or "roadmap:waiting:jobs")
    assert rc.main(["serve", "--once", "--no-notify"]) == 0
    assert calls == ["reap", "serve", "roadmap"]


# ---------------------------------------------------------------------------
# Spec 218 amendment (2026-10-26): a running job blocks only the work that depends on it


def test_prompts_keep_working_on_job_independent_work_while_a_job_runs() -> None:
    mission = rc.MISSION_PROMPT
    assert "blocks only the work that depends on its result" in mission
    assert "only when no such job-independent authorized work remains" in mission
    assert "focused tests only, full suites to CI" in mission
    assert "While a registered job runs, take the next item in the order the standing directives give" in rc.ROADMAP_TASK
    assert "A running registered job never idles the cycle" not in rc.ROADMAP_TASK
    assert "end with `RELAY_STATUS continue` and a `JOB-WAIT:` or `CI-WAIT:` line" in rc.ROADMAP_TASK
    assert "next directive item" in rc.ROADMAP_TASK
    # The JOB-WAIT pause itself is unchanged: a cycle that does end blocked on jobs still pauses until they finish.
    assert "`JOB-WAIT: <job id>` lines, which pause roadmap mode until those jobs finish" in mission


def _routed_launch(tmp_path: Path) -> tuple[Env, str]:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.relay.readings = room(env, "codex")
    env.host.worker_routing = rc.worker_routing_block
    env.head(fixture="claude_session_limit")
    env.run()
    assert [launch["agent"] for launch in env.relay.launches] == ["codex"]
    return env, env.relay.launches[0]["task"]


def test_launch_prompt_carries_the_worker_routing_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc.worker_routing, "cached_summary", lambda: "worker quota: gemini 99% free (5h 100%)")
    _, task = _routed_launch(tmp_path)
    assert "devctx.py recover" in task  # the recovery prompt is intact
    block = task[task.index("Worker routing"):]
    assert "worker quota: gemini 99% free (5h 100%)" in block
    assert "python3 scripts/worker_routing.py pick --difficulty D --risk R" in block
    assert "durable registered job that outlives the coordinator turn" in block
    assert "a later turn collects it with `relay_jobs.py list`" in block
    assert "do not implement delegable work yourself unless pick returns no_worker" in block
    assert len(block.splitlines()) <= 8


def test_worker_routing_block_survives_a_reader_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode() -> str:
        raise OSError("quota readers crashed")

    monkeypatch.setattr(rc.worker_routing, "cached_summary", explode)
    _, task = _routed_launch(tmp_path)
    assert "worker quota: unreadable" in task and "pick --difficulty D --risk R" in task


def test_worker_routing_failure_never_blocks_a_launch(tmp_path: Path) -> None:
    def broken() -> str:
        raise RuntimeError("no block at all")

    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.relay.readings = room(env, "codex")
    env.host.worker_routing = broken
    env.head(fixture="claude_session_limit")
    result, _ = env.run()
    assert result == "idle" and "Worker routing" not in env.relay.launches[0]["task"]
    assert any("worker routing block skipped" in line for line in env.logs)


def test_hosts_without_worker_routing_launch_unchanged(tmp_path: Path) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.relay.readings = room(env, "codex")
    env.head(fixture="claude_session_limit")
    env.run()
    assert "Worker routing" not in env.relay.launches[0]["task"]


def test_supervisor_block_persists_the_quota_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pools = {"codex": rc.worker_routing.Pool("codex", {"5h": 1.0, "weekly": 0.9}, 0.9),
             "gemini": rc.worker_routing.Pool("gemini", {"5h": 1.0, "weekly": 0.99}, 0.99)}
    monkeypatch.setattr(rc.worker_routing, "read_pools", lambda: pools)
    rc.worker_routing._cache.update(at=None, line=None, ttl=0.0)
    assert "worker quota: codex 90% free" in rc.worker_routing_block(tmp_path)
    saved = json.loads((tmp_path / ".agent-relay" / "worker-quota.json").read_text())
    assert saved["schema"] == "worker-quota.v1" and saved["pools"]["codex"]["windows"]["weekly"] == 0.9
    rc.worker_routing._cache.update(at=None, line=None, ttl=0.0)


# ---------------------------------------------------------------------------
# Spec 254: never stop for checkable, compound push probe, park and continue


PUSH_COMPLETED = {
    "show-ref": (0, ""),
    "rev-parse --verify": (0, "a" * 40 + "\n"),
    "ls-remote": (0, f"{'a' * 40}\trefs/heads/b\n"),
}


@pytest.mark.parametrize(("command", "responses", "expected"), [
    (
        "cd /r && git add -A && git commit -m x && git push origin b",
        {**PUSH_COMPLETED, "status --porcelain": (0, "")},
        "completed",
    ),
    (
        "cd /r && git add -A && git commit -m x && git push origin b",
        {"show-ref": (0, ""), "rev-parse --verify": (0, "a" * 40 + "\n"), "ls-remote": (0, "")},
        "not_started",
    ),
    (
        "cd /r && git add -A && git commit -m x && git push origin b",
        {**PUSH_COMPLETED, "status --porcelain": (0, " M a.py\n")},
        None,
    ),
    (
        "git add -A && git commit -m x && git push origin b",
        {**PUSH_COMPLETED, "status --porcelain": (0, "")},
        None,
    ),
    (
        "cd /r && rm -rf x && git push origin b",
        {**PUSH_COMPLETED, "status --porcelain": (0, "")},
        None,
    ),
    (
        "cd /r && git commit -m x && gh pr create --fill",
        {**PUSH_COMPLETED, "status --porcelain": (0, "")},
        None,
    ),
])
def test_compound_commit_and_push_is_settled_by_the_push_probe(
    command: str, responses: dict[str, tuple[int, str]], expected: str | None,
) -> None:
    run = fake_runner(responses)
    probed = rc.probe_call(bash(command), Path("/r"), run)
    assert (probed[0] if probed else None) == expected
    for args in run.calls:
        cmd = args[4] if "--no-optional-locks" in args else (args[3] if args[:2] == ["git", "-C"] else " ".join(args[1:3]))
        assert cmd in ("rev-parse", "ls-remote", "merge-base", "show-ref", "status")


@pytest.mark.parametrize("command", [
    "cd /r && git add -A && git commit -m x && git push origin b",
    "cd /r && git push origin b",
    "cd /r && git push origin b && gh pr create --fill",
    "gh pr comment 7 --body done",
])
def test_interrupted_commit_push_and_comment_park_once_and_continue(tmp_path: Path, command: str) -> None:
    env = Env(tmp_path, [("codex", CODEX_SUCCESS)])
    env.host.probe = lambda call: None
    env.head([tool_use("x", "Bash", command=command), *QUOTA])
    result, sup = env.run()
    assert result == "idle" and sup.state["hold"] is None
    assert len(env.relay.launches) == 1
    task = env.relay.launches[0]["task"]
    label = rc.classify_command(command)[1]
    assert rc.PARKED_TEXT in task and label in task
    assert sum(1 for kind, text in env.ledger if kind == "finding" and "parked interrupted external operation(s)" in text) == 1
    assert sum(1 for text in env.sent if "ha messo da parte un'operazione non verificabile" in text) == 1
    env.relay.script = [("codex", CODEX_SUCCESS)]
    result2, sup2 = env.run()
    assert result2 == "idle" and sup2.state["hold"] is None
    assert sum(1 for kind, text in env.ledger if kind == "finding" and "parked interrupted external operation(s)" in text) == 1
    assert sum(1 for text in env.sent if "ha messo da parte un'operazione non verificabile" in text) == 1


def test_a_successful_turn_with_an_unresolved_external_operation_continues(tmp_path: Path) -> None:
    env = Env(tmp_path)
    mission = mission_def("first", "second")
    start_mission(env, mission)
    env.relay.script = [("claude", [IN_FLIGHT["gh pr comment"], *SUCCESS]), ("claude", SUCCESS)]
    result, sup = run_mission(env, mission)
    assert result == "idle" and sup.state["hold"] is None
    assert len(env.relay.launches) == 2
    assert rc.PARKED_TEXT in env.relay.launches[1]["task"]
    assert sum(1 for kind, text in env.ledger if kind == "finding" and "parked interrupted" in text) == 1


def test_turn_failed_is_sent_only_from_the_second_consecutive_failure(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", CRASH), ("claude", CRASH), ("claude", SUCCESS)])
    failed_counts_at_start: list[int] = []
    real_start = env.relay.start

    def record_start(agent: str, parent: str, task: str, turns: int) -> FakeProc:
        failed_counts_at_start.append(
            sum(1 for t in env.sent if t.startswith("Alcuni tentativi di lavoro di seguito non sono riusciti"))
        )
        return real_start(agent, parent, task, turns)

    env.relay.start = record_start  # type: ignore[method-assign]
    env.head(CRASH)
    result, sup = env.run()
    assert result == "idle" and sup.state["hold"] is None
    assert failed_counts_at_start == [0, 0, 1]
    turn_failed_events = [e for e in env.events() if e.startswith("Alcuni tentativi di lavoro di seguito non sono riusciti")]
    assert len(turn_failed_events) == 1


def test_no_supervisor_event_tells_the_maintainer_to_write_in_chat() -> None:
    assert not any("scrivi a Claude in chat" in text for text, _ in rc.EVENTS.values())


def test_roadmap_task_readiness_is_yours_to_give() -> None:
    assert "readiness is yours to give" in rc.ROADMAP_TASK
    assert "still requires explicit readiness" not in rc.ROADMAP_TASK


# ---------------------------------------------------------------------------
# Spec 264 (SESSION-BRIEF-1): operator mission brief and standing directives at session start


def test_no_session_context_configured_journal_and_prompt_unchanged(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)])
    env.head(SUCCESS)
    assert env.host.session_context is None
    result, sup = env.run(task="do next step {nonce}")
    assert result == "idle"
    launch_events = [j for j in journal(env) if j.get("event") == "launch"]
    assert len(launch_events) == 1
    assert "brief" not in launch_events[0]
    assert "directives" not in launch_events[0]
    launched_task = env.relay.launches[0]["task"]
    assert rc.BRIEF_HEADER not in launched_task
    assert rc.DIRECTIVES_HEADER not in launched_task


def test_session_context_block_no_setting_no_authority(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    dev_env = rc.devctx.Env(repo=repo, control=control, relay_sessions=None)
    text, fields = rc.session_context_block(control, dev_env, None)
    assert text == ""
    assert fields == {"brief": None, "directives": []}


def test_setting_present_fresh_launch_includes_brief_before_worker_routing(tmp_path: Path) -> None:
    brief_file = tmp_path / "operator_brief.md"
    brief_file.write_text("Execute the plan strictly in order.", encoding="utf-8")
    control = tmp_path / "control"
    control.mkdir(parents=True)
    (control / "mission-brief").write_text(str(brief_file), encoding="utf-8")

    env = Env(tmp_path, [("claude", SUCCESS)])
    env.host.worker_routing = lambda: "## Worker routing\npick: auto"
    dev_env = rc.devctx.Env(repo=env.repo, control=control, relay_sessions=None)
    env.host.session_context = lambda authority: rc.session_context_block(control, dev_env, authority)
    sup = env.supervisor()
    sup.state["permissions_digest"] = env.host.permissions_digest()

    result = sup._launch("claude", "fresh turn {nonce}", previous=None)
    assert isinstance(result, rc.TurnReport) or result in ("success", "idle", "waiting")
    assert env.relay.launches[0]["parent"] is None
    launched_task = env.relay.launches[0]["task"]
    assert launched_task.count(rc.BRIEF_HEADER) == 1
    assert "Execute the plan strictly in order." in launched_task
    assert "## Worker routing" in launched_task
    assert launched_task.index(rc.BRIEF_HEADER) < launched_task.index("## Worker routing")

    launch_events = [j for j in journal(env) if j.get("event") == "launch"]
    assert len(launch_events) == 1
    assert launch_events[0]["brief"] == hashlib.sha256(b"Execute the plan strictly in order.").hexdigest()[:12]
    assert launch_events[0]["directives"] == []


def test_resumed_same_agent_turn_skips_session_context(tmp_path: Path) -> None:
    brief_file = tmp_path / "operator_brief.md"
    brief_file.write_text("Private brief.", encoding="utf-8")
    control = tmp_path / "control"
    control.mkdir(parents=True)
    (control / "mission-brief").write_text(str(brief_file), encoding="utf-8")

    env = Env(tmp_path, [("claude", SUCCESS)])
    dev_env = rc.devctx.Env(repo=env.repo, control=control, relay_sessions=None)
    calls: list[str | None] = []

    def mock_context(authority: str | None) -> tuple[str, dict[str, Any]]:
        calls.append(authority)
        return rc.session_context_block(control, dev_env, authority)

    env.host.session_context = mock_context
    sup = env.supervisor()
    sup.state["head_session"] = "20261018-000001-bbbbbb"
    sup.state["agent"] = "claude"
    sup.state["permissions_digest"] = env.host.permissions_digest()
    sup.save()

    report = rc.TurnReport(
        turn_id="20261018-000001-bbbbbb/turn-001",
        outcome="success",
        agent="claude",
        detail="turn complete",
    )
    sup._launch("claude", "continue cycle {nonce}", report)
    assert calls == []
    launched_task = env.relay.launches[0]["task"]
    assert rc.BRIEF_HEADER not in launched_task
    launch_events = [j for j in journal(env) if j.get("event") == "launch"]
    assert len(launch_events) == 1
    assert launch_events[0]["brief"] is None
    assert launch_events[0]["directives"] == []


def test_native_retry_resuming_conversation_skips_session_context(tmp_path: Path) -> None:
    brief_file = tmp_path / "operator_brief.md"
    brief_file.write_text("Private brief for native agent.", encoding="utf-8")
    control = tmp_path / "control"
    control.mkdir(parents=True)
    (control / "mission-brief").write_text(str(brief_file), encoding="utf-8")

    env = Env(tmp_path, [("argon", SUCCESS)])
    dev_env = rc.devctx.Env(repo=env.repo, control=control, relay_sessions=None)
    calls: list[str | None] = []

    def mock_context(authority: str | None) -> tuple[str, dict[str, Any]]:
        calls.append(authority)
        return rc.session_context_block(control, dev_env, authority)

    env.host.session_context = mock_context
    sup = env.supervisor()
    sup.state["head_session"] = "20261018-000001-bbbbbb"
    sup.state["agent"] = "argon"
    sup.state["permissions_digest"] = env.host.permissions_digest()
    sup.save()

    # Create previous native turn output with conversation_id
    turn_dir = env.relay.sessions_dir() / "20261018-000001-bbbbbb" / "turns" / "turn-001"
    turn_dir.mkdir(parents=True)
    cid = "5b934b69-1847-40af-93ec-eefda93178a5"
    (turn_dir / "output.jsonl").write_text(
        json.dumps({"event": "init", "conversation_id": cid, "init": {"model": "argon"}}) + "\n",
        encoding="utf-8",
    )
    # Store file exists and is within cap: conversation is resumable
    (env.relay.conversation_store() / f"{cid}.db").write_bytes(b"x" * 1024)

    report = rc.TurnReport(
        turn_id="20261018-000001-bbbbbb/turn-001",
        outcome="transient",
        agent="argon",
        detail="network blip",
    )
    sup._launch("argon", "retry turn {nonce}", report)
    # Native retry resumes the same conversation, so session_context must NOT be attached:
    assert calls == []
    launched_task = env.relay.launches[0]["task"]
    assert rc.BRIEF_HEADER not in launched_task
    assert env.relay.launches[0]["resume"] == cid
    launch_events = [j for j in journal(env) if j.get("event") == "launch"]
    assert len(launch_events) == 1
    assert launch_events[0]["resume"] == cid
    assert launch_events[0]["brief"] is None
    assert launch_events[0]["directives"] == []


def test_native_retry_missing_conversation_store_attaches_session_context(tmp_path: Path) -> None:
    brief_file = tmp_path / "operator_brief.md"
    brief_file.write_text("Private brief for native agent.", encoding="utf-8")
    control = tmp_path / "control"
    control.mkdir(parents=True)
    (control / "mission-brief").write_text(str(brief_file), encoding="utf-8")

    env = Env(tmp_path, [("argon", SUCCESS)])
    dev_env = rc.devctx.Env(repo=env.repo, control=control, relay_sessions=None)
    calls: list[str | None] = []

    def mock_context(authority: str | None) -> tuple[str, dict[str, Any]]:
        calls.append(authority)
        return rc.session_context_block(control, dev_env, authority)

    env.host.session_context = mock_context
    sup = env.supervisor()
    sup.state["head_session"] = "20261018-000001-bbbbbb"
    sup.state["agent"] = "argon"
    sup.state["permissions_digest"] = env.host.permissions_digest()
    sup.save()

    # Create previous native turn output with conversation_id, but NO store file
    turn_dir = env.relay.sessions_dir() / "20261018-000001-bbbbbb" / "turns" / "turn-001"
    turn_dir.mkdir(parents=True)
    cid = "5b934b69-1847-40af-93ec-eefda93178a5"
    (turn_dir / "output.jsonl").write_text(
        json.dumps({"event": "init", "conversation_id": cid, "init": {"model": "argon"}}) + "\n",
        encoding="utf-8",
    )
    # Notice: env.relay.conversation_store() / f"{cid}.db" does NOT exist (missing store)

    report = rc.TurnReport(
        turn_id="20261018-000001-bbbbbb/turn-001",
        outcome="transient",
        agent="argon",
        detail="network blip",
    )
    sup._launch("argon", "retry turn {nonce}", report)
    # Missing store causes runner rejection -> fresh context actually starts -> session_context MUST be attached:
    assert len(calls) == 1
    launched_task = env.relay.launches[0]["task"]
    assert rc.BRIEF_HEADER in launched_task
    assert "Private brief for native agent." in launched_task
    assert env.relay.launches[0]["resume"] is None
    launch_events = [j for j in journal(env) if j.get("event") == "launch"]
    assert len(launch_events) == 1
    assert "resume" not in launch_events[0] or launch_events[0]["resume"] is None
    assert launch_events[0]["brief"] is not None
    # Verify logs do NOT leak brief content
    assert any("native conversation store missing; starting fresh context" in line for line in env.logs)
    assert not any("Private brief for native agent." in line for line in env.logs)


def test_native_retry_oversize_conversation_store_attaches_session_context(tmp_path: Path) -> None:
    brief_file = tmp_path / "operator_brief.md"
    brief_file.write_text("Private brief for native agent.", encoding="utf-8")
    control = tmp_path / "control"
    control.mkdir(parents=True)
    (control / "mission-brief").write_text(str(brief_file), encoding="utf-8")

    env = Env(tmp_path, [("argon", SUCCESS)])
    dev_env = rc.devctx.Env(repo=env.repo, control=control, relay_sessions=None)
    calls: list[str | None] = []

    def mock_context(authority: str | None) -> tuple[str, dict[str, Any]]:
        calls.append(authority)
        return rc.session_context_block(control, dev_env, authority)

    env.host.session_context = mock_context
    sup = env.supervisor()
    sup.state["head_session"] = "20261018-000001-bbbbbb"
    sup.state["agent"] = "argon"
    sup.state["permissions_digest"] = env.host.permissions_digest()
    sup.save()

    # Create previous native turn output with conversation_id
    turn_dir = env.relay.sessions_dir() / "20261018-000001-bbbbbb" / "turns" / "turn-001"
    turn_dir.mkdir(parents=True)
    cid = "5b934b69-1847-40af-93ec-eefda93178a5"
    (turn_dir / "output.jsonl").write_text(
        json.dumps({"event": "init", "conversation_id": cid, "init": {"model": "argon"}}) + "\n",
        encoding="utf-8",
    )
    # Store file exists but is oversize (> RESUME_MAX_BYTES)
    with (env.relay.conversation_store() / f"{cid}.db").open("wb") as handle:
        handle.truncate(rc.relay_argon.RESUME_MAX_BYTES + 1)

    report = rc.TurnReport(
        turn_id="20261018-000001-bbbbbb/turn-001",
        outcome="transient",
        agent="argon",
        detail="network blip",
    )
    sup._launch("argon", "retry turn {nonce}", report)
    # Oversize store causes runner rejection -> fresh context actually starts -> session_context MUST be attached:
    assert len(calls) == 1
    launched_task = env.relay.launches[0]["task"]
    assert rc.BRIEF_HEADER in launched_task
    assert "Private brief for native agent." in launched_task
    assert env.relay.launches[0]["resume"] is None
    launch_events = [j for j in journal(env) if j.get("event") == "launch"]
    assert len(launch_events) == 1
    assert "resume" not in launch_events[0] or launch_events[0]["resume"] is None
    assert launch_events[0]["brief"] is not None
    # Verify logs do NOT leak brief content
    assert any("oversize" in line and "starting fresh context" in line for line in env.logs)
    assert not any("Private brief for native agent." in line for line in env.logs)


def test_agent_switch_with_non_null_parent_attaches_block(tmp_path: Path) -> None:
    brief_file = tmp_path / "operator_brief.md"
    brief_file.write_text("Operator brief on switch.", encoding="utf-8")
    control = tmp_path / "control"
    control.mkdir(parents=True)
    (control / "mission-brief").write_text(str(brief_file), encoding="utf-8")

    env = Env(tmp_path, [("claude", SUCCESS)])
    dev_env = rc.devctx.Env(repo=env.repo, control=control, relay_sessions=None)
    calls: list[str | None] = []

    def mock_context(authority: str | None) -> tuple[str, dict[str, Any]]:
        calls.append(authority)
        return rc.session_context_block(control, dev_env, authority)

    env.host.session_context = mock_context
    sup = env.supervisor()
    sup.state["head_session"] = "20261018-000001-bbbbbb"
    sup.state["agent"] = "claude_prior"
    sup.state["permissions_digest"] = env.host.permissions_digest()
    sup.save()

    report = rc.TurnReport(
        turn_id="20261018-000001-bbbbbb/turn-001",
        outcome="success",
        agent="claude_prior",
        detail="turn complete",
    )
    sup._launch("claude", "takeover turn {nonce}", report)
    assert len(calls) == 1
    launched_task = env.relay.launches[0]["task"]
    assert rc.BRIEF_HEADER in launched_task
    assert "Operator brief on switch." in launched_task
    launch_events = [j for j in journal(env) if j.get("event") == "launch"]
    assert len(launch_events) == 1
    assert launch_events[0]["brief"] == hashlib.sha256(b"Operator brief on switch.").hexdigest()[:12]


def test_brief_failure_modes_produce_one_line_notice_without_path(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir(parents=True)
    setting = control / "mission-brief"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    dev_env = rc.devctx.Env(repo=repo, control=control, relay_sessions=None)

    # 1. Relative path
    setting.write_text("relative/path/brief.md", encoding="utf-8")
    logs: list[str] = []
    text, fields = rc.session_context_block(control, dev_env, None, log=logs.append)
    assert text == "Operator mission brief not attached: not an absolute path"
    assert "relative" not in text
    assert fields["brief"] is None
    assert logs == ["Operator mission brief not attached: not an absolute path"]

    # 2. Missing file
    missing = tmp_path / "missing_brief.md"
    setting.write_text(str(missing), encoding="utf-8")
    logs.clear()
    text, fields = rc.session_context_block(control, dev_env, None, log=logs.append)
    assert text == "Operator mission brief not attached: missing"
    assert str(tmp_path) not in text
    assert fields["brief"] is None
    assert logs == ["Operator mission brief not attached: missing"]

    # 3. Unreadable file
    unreadable = tmp_path / "unreadable_brief.md"
    unreadable.write_text("Secret content", encoding="utf-8")
    unreadable.chmod(0o000)
    setting.write_text(str(unreadable), encoding="utf-8")
    logs.clear()
    try:
        text, fields = rc.session_context_block(control, dev_env, None, log=logs.append)
        assert text == "Operator mission brief not attached: unreadable"
        assert str(tmp_path) not in text
        assert fields["brief"] is None
        assert logs == ["Operator mission brief not attached: unreadable"]
    finally:
        unreadable.chmod(0o644)

    # 4. Oversize file (> 24,000 bytes)
    oversize = tmp_path / "oversize_brief.md"
    oversize.write_bytes(b"A" * 24_001)
    setting.write_text(str(oversize), encoding="utf-8")
    logs.clear()
    text, fields = rc.session_context_block(control, dev_env, None, log=logs.append)
    assert text == "Operator mission brief not attached: larger than 24000 bytes"
    assert str(tmp_path) not in text
    assert fields["brief"] is None
    assert logs == ["Operator mission brief not attached: larger than 24000 bytes"]

    # Verify launch proceeds with notice
    env = Env(tmp_path, [("claude", SUCCESS)])
    env.host.session_context = lambda authority: rc.session_context_block(control, dev_env, authority)
    sup = env.supervisor()
    sup.state["permissions_digest"] = env.host.permissions_digest()
    result = sup._launch("claude", "fresh turn with oversize brief {nonce}", previous=None)
    assert isinstance(result, rc.TurnReport) or result in ("success", "idle", "waiting")
    assert env.relay.launches[0]["parent"] is None
    launched = env.relay.launches[0]["task"]
    assert "Operator mission brief not attached: larger than 24000 bytes" in launched
    assert str(tmp_path) not in launched


def test_directives_authority_superseded_mismatch_and_cap(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    dev_env = rc.devctx.Env(repo=repo, control=control, relay_sessions=None)

    # D1: Older than authority -> must not be included
    e1 = rc.devctx.record_directive(dev_env, "D1 older directive", source=None, supersedes=None, author="maintainer")
    # D2: Authority directive -> included
    e2 = rc.devctx.record_directive(dev_env, "D2 authority directive text", source=None, supersedes=None, author="maintainer")
    # D3: Recorded after authority, but later superseded -> absent
    e3 = rc.devctx.record_directive(dev_env, "D3 old standing directive", source=None, supersedes=None, author="maintainer")
    # D4: Supersedes D3 -> included
    e4 = rc.devctx.record_directive(dev_env, "D4 supersedes D3 directive text", source=None, supersedes=[e3["id"]], author="maintainer")
    # D5: Hash mismatched -> replaced by notice
    e5 = rc.devctx.record_directive(dev_env, "D5 original text", source=None, supersedes=None, author="maintainer")
    Path(e5["directive"]["path"]).write_text("D5 tampered text", encoding="utf-8")

    text, fields = rc.session_context_block(control, dev_env, e2["id"])
    assert rc.DIRECTIVES_HEADER in text
    assert e1["id"] not in text
    assert f"### Directive {e2['id']}\nD2 authority directive text" in text
    assert e3["id"] not in text
    assert f"### Directive {e4['id']}\nD4 supersedes D3 directive text" in text
    assert f"Directive {e5['id']} not attached: file modified after recording" in text
    # D2 and D4 were attached in order; D5 was replaced by notice, so not in attached directives
    assert fields["directives"] == [e2["id"], e4["id"]]
    # Oldest-first order verification
    pos_d2 = text.index(f"### Directive {e2['id']}")
    pos_d4 = text.index(f"### Directive {e4['id']}")
    pos_d5 = text.index(f"Directive {e5['id']} not attached")
    assert pos_d2 < pos_d4 < pos_d5

    # Cap exceeded test: add a large directive D6 (23,950 bytes) so total exceeds 24,000 bytes
    e6 = rc.devctx.record_directive(dev_env, "X" * 23_950, source=None, supersedes=None, author="maintainer")
    text_capped, fields_capped = rc.session_context_block(control, dev_env, e2["id"])
    # Newest (D6) fits, older (D2, D4, D5) are omitted for length
    assert f"### Directive {e6['id']}" in text_capped
    assert "Older standing directives omitted for length:" in text_capped
    assert f"{e2['id']}, {e4['id']}, {e5['id']}" in text_capped
    assert "(devctx.py show <id>)" in text_capped
    assert fields_capped["directives"] == [e6["id"]]


def test_directives_body_limit_24000_and_24001_bytes(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    dev_env = rc.devctx.Env(repo=repo, control=control, relay_sessions=None)

    # 1. Exactly 24,000 bytes directive body: MUST be attached (heading not counted against limit)
    exact_text = "E" * 24_000
    e_exact = rc.devctx.record_directive(dev_env, exact_text, source=None, supersedes=None, author="maintainer")
    text_exact, fields_exact = rc.session_context_block(control, dev_env, e_exact["id"])
    assert fields_exact["directives"] == [e_exact["id"]]
    assert f"### Directive {e_exact['id']}\n{exact_text}" in text_exact
    assert "Older standing directives omitted for length:" not in text_exact

    # 2. 24,001 bytes directive body: exceeds limit, MUST NOT be attached and must be explicitly omitted
    over_text = "O" * 24_001
    e_over = rc.devctx.record_directive(dev_env, over_text, source=None, supersedes=None, author="maintainer")
    text_over, fields_over = rc.session_context_block(control, dev_env, e_over["id"])
    assert fields_over["directives"] == []
    assert f"### Directive {e_over['id']}" not in text_over
    assert "Standing directives omitted for length:" in text_over
    assert "Older standing directives omitted for length:" not in text_over
    assert e_over["id"] in text_over


def test_oversize_newest_directive_preserves_earlier_authority_directive(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    dev_env = rc.devctx.Env(repo=repo, control=control, relay_sessions=None)

    # D1 is short mission authority directive (fits well within 24,000 bytes)
    e1 = rc.devctx.record_directive(dev_env, "D1 authority short directive text", source=None, supersedes=None, author="maintainer")
    # D2 is newest directive, exceeds 24,000 bytes (e.g. 24,500 bytes)
    e2 = rc.devctx.record_directive(dev_env, "Y" * 24_500, source=None, supersedes=None, author="maintainer")

    text, fields = rc.session_context_block(control, dev_env, e1["id"])
    # Earlier short mission authority directive MUST NOT be suppressed by oversize newest directive:
    assert fields["directives"] == [e1["id"]]
    assert f"### Directive {e1['id']}\nD1 authority short directive text" in text
    assert f"### Directive {e2['id']}" not in text

    # Omission notice must be accurate: D2 is newer than kept directives, so not labeled as "Older":
    assert "Newer standing directives omitted for length:" in text
    assert e2["id"] in text
    assert "Older standing directives omitted for length:" not in text


def test_multiple_directives_older_and_newer_omission_notices(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    dev_env = rc.devctx.Env(repo=repo, control=control, relay_sessions=None)

    # D1 (authority): 50 bytes
    e1 = rc.devctx.record_directive(dev_env, "D1 " + "1" * 47, source=None, supersedes=None, author="maintainer")
    # D2: 100 bytes
    e2 = rc.devctx.record_directive(dev_env, "D2 " + "2" * 97, source=None, supersedes=None, author="maintainer")
    # D3: 23,900 bytes
    e3 = rc.devctx.record_directive(dev_env, "D3 " + "3" * 23_897, source=None, supersedes=None, author="maintainer")
    # D4: 25,000 bytes (oversize newest)
    e4 = rc.devctx.record_directive(dev_env, "D4 " + "4" * 24_997, source=None, supersedes=None, author="maintainer")

    text, fields = rc.session_context_block(control, dev_env, e1["id"])
    # D4 (>24k) is oversize omitted.
    # D3 (23,900) fits.
    # D2 (100) fits (23,900 + 100 = 24,000 <= 24,000).
    # D1 (50) exceeds remaining capacity (24,000 + 50 > 24,000), so older omitted.
    assert fields["directives"] == [e2["id"], e3["id"]]
    assert f"### Directive {e2['id']}" in text
    assert f"### Directive {e3['id']}" in text
    assert f"### Directive {e1['id']}" not in text
    assert f"### Directive {e4['id']}" not in text

    # Both accurate omission notices must be recorded:
    assert "Newer standing directives omitted for length:" in text
    assert e4["id"] in text
    assert "Older standing directives omitted for length:" in text
    assert e1["id"] in text


def test_directives_authority_not_found(tmp_path: Path) -> None:
    control = tmp_path / "control"
    control.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    dev_env = rc.devctx.Env(repo=repo, control=control, relay_sessions=None)
    text, fields = rc.session_context_block(control, dev_env, "L20261113000000-0000")
    assert text == "Directive L20261113000000-0000 not attached: not found in ledger"
    assert fields["directives"] == []


def test_launch_journal_records_brief_and_directives_never_text_or_path(tmp_path: Path) -> None:
    brief_file = tmp_path / "operator_secret_brief.md"
    brief_file.write_text("Strict secret instructions: deploy alpha first.", encoding="utf-8")
    control = tmp_path / "control"
    control.mkdir(parents=True)
    (control / "mission-brief").write_text(str(brief_file), encoding="utf-8")

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    dev_env = rc.devctx.Env(repo=repo, control=control, relay_sessions=None)
    d_auth = rc.devctx.record_directive(dev_env, "Maintainer directive content", source=None, supersedes=None, author="maintainer")

    env = Env(tmp_path, [("claude", SUCCESS)])
    env.host.session_context = lambda authority: rc.session_context_block(control, dev_env, authority)
    sup = env.supervisor()
    sup.state["permissions_digest"] = env.host.permissions_digest()
    sup.mission = {"id": "M100", "authority": d_auth["id"], "tasks": [{"text": "Task A"}]}
    sup.state["mission"] = {"id": "M100", "next": 0, "total": 1}

    sup._launch("claude", "execute task {nonce}", previous=None)

    launch_events = [j for j in journal(env) if j.get("event") == "launch"]
    assert len(launch_events) == 1
    event = launch_events[0]
    expected_brief_digest = hashlib.sha256(b"Strict secret instructions: deploy alpha first.").hexdigest()[:12]
    assert event["brief"] == expected_brief_digest
    assert event["directives"] == [d_auth["id"]]

    event_str = json.dumps(event)
    assert "Strict secret instructions" not in event_str
    assert "Maintainer directive content" not in event_str
    assert str(brief_file) not in event_str
    assert str(tmp_path) not in event_str


def test_session_context_raising_logs_and_launches_without_it(tmp_path: Path) -> None:
    env = Env(tmp_path, [("claude", SUCCESS)])

    def failing_context(authority: str | None) -> tuple[str, dict[str, Any]]:
        raise ValueError("simulated session context reader crash")

    env.host.session_context = failing_context
    sup = env.supervisor()
    sup.state["permissions_digest"] = env.host.permissions_digest()
    result = sup._launch("claude", "perform turn {nonce}", previous=None)
    assert isinstance(result, rc.TurnReport) or result in ("success", "idle", "waiting")
    assert any("session context skipped: ValueError" in log for log in env.logs)

    launch_events = [j for j in journal(env) if j.get("event") == "launch"]
    assert len(launch_events) == 1
    assert launch_events[0]["brief"] is None
    assert launch_events[0]["directives"] == []


def test_gitignore_contains_agent_relay() -> None:
    gitignore_path = Path(__file__).resolve().parents[2] / ".gitignore"
    assert gitignore_path.exists()
    lines = [line.strip() for line in gitignore_path.read_text(encoding="utf-8").splitlines()]
    assert ".agent-relay/" in lines


def test_make_host_wires_session_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    host = rc.make_host(repo)
    assert host.session_context is not None
    assert callable(host.session_context)
    text, fields = host.session_context(None)
    assert text == ""
    assert fields == {"brief": None, "directives": []}


# ---------------------------------------------------------------------------
# Spec 267: Session continuity across external waits


def test_wait_parsing_strict_and_deduplicated() -> None:
    text = (
        "Some thoughts and progress...\n"
        "RELAY_STATUS continue\n"
        "JOB-WAIT: j-20261113T120000Z-111111\n"
        "CI-WAIT: 42@0123456789abcdef0123456789abcdef01234567\n"
        "JOB-WAIT: j-20261113T120000Z-222222\n"
        "JOB-WAIT: j-20261113T120000Z-111111\n"  # duplicate
        "CI-WAIT: 42@0123456789abcdef0123456789abcdef01234567\n"  # duplicate
        "JOB-WAIT: `j-20261113T120000Z-333333`\n"  # backticks: rejected
        "JOB-WAIT: j-20261113T120000Z-444444; rm -rf /\n"  # shell injection: rejected
        "CI-WAIT: 42@0123456789abcdef0123456789abcdef01234567 | cat\n"  # shell pipe: rejected
        "CI-WAIT: 42@shortsha\n"  # short sha: rejected
        "CI-WAIT: https://github.com/org/repo/pull/42\n"  # url: rejected
        "CI-WAIT: 0@0123456789abcdef0123456789abcdef01234567\n"  # pr 0: rejected
        "JOB-WAIT: \n"  # empty: rejected
        "CI-WAIT: \n"  # empty: rejected
    )
    jobs, ci_waits = rc.parse_wait_directives(text)
    assert jobs == ["j-20261113T120000Z-111111", "j-20261113T120000Z-222222"]
    assert ci_waits == [(42, "0123456789abcdef0123456789abcdef01234567")]
    _, zero_waits = rc.parse_wait_directives("CI-WAIT: 0@0123456789abcdef0123456789abcdef01234567")
    assert zero_waits == []


def test_job_wait_satisfaction_and_resume_prompt(tmp_path: Path) -> None:
    env = Env(tmp_path)
    mission = mission_def("run worker jobs")
    start_mission(env, mission)
    t0 = env.clock.now()

    def job_status(jid: str) -> dict[str, Any] | None:
        if jid == "j-20261113T120000Z-111111":
            if (env.clock.now() - t0).total_seconds() >= 15:
                return {"status": "failed"}
            return {"status": "running"}
        if jid == "j-20261113T120000Z-222222":
            return {"status": "succeeded"}
        return None

    env.host.job_status = job_status

    turn1_msg = (
        "Started background solver jobs.\n"
        "RELAY_STATUS continue\n"
        "JOB-WAIT: j-20261113T120000Z-111111\n"
        "JOB-WAIT: j-20261113T120000Z-222222"
    )
    env.relay.script = [
        ("claude", SUCCESS, "continue", turn1_msg),
        ("claude", SUCCESS, "propose_done"),
    ]
    result, sup = run_mission(env, mission)
    assert result == "idle"
    assert len(env.relay.launches) == 2
    l2 = env.relay.launches[1]
    assert l2["parent"] is not None
    assert "External wait satisfied:" in l2["task"]
    assert "- JOB j-20261113T120000Z-111111: failed" in l2["task"]
    assert "- JOB j-20261113T120000Z-222222: succeeded" in l2["task"]
    assert f"Resume work on mission {mission['id']} task 1/1." in l2["task"]


def test_ci_wait_satisfaction_and_unrelated_ci_ignored(tmp_path: Path) -> None:
    env = Env(tmp_path)
    mission = mission_def("wait for PR checks")
    start_mission(env, mission)
    t0 = env.clock.now()
    head_sha = "1111111111111111111111111111111111111111"

    # Unrelated CI run on another PR / head exists
    env.ci[(99, "9999999999999999999999999999999999999999")] = {
        "observed_head": "9999999999999999999999999999999999999999",
        "state": "open",
        "required_checks": ["backend"],
        "checks": [{"name": "backend", "status": "completed", "conclusion": "success"}],
    }

    def pr_status(pr: int, sha: str) -> dict[str, Any] | None:
        if pr == 42 and sha == head_sha:
            if (env.clock.now() - t0).total_seconds() >= 60:
                return {
                    "observed_head": head_sha,
                    "state": "open",
                    "required_checks": ["backend"],
                    "checks": [{"name": "backend", "status": "completed", "conclusion": "success"}],
                }
            return {
                "observed_head": head_sha,
                "state": "open",
                "required_checks": ["backend"],
                "checks": [{"name": "backend", "status": "in_progress", "conclusion": ""}],
            }
        return env.ci.get((pr, sha))

    env.host.pr_ci_status = pr_status

    turn1_msg = f"Waiting for PR checks.\nRELAY_STATUS continue\nCI-WAIT: 42@{head_sha}"
    env.relay.script = [
        ("claude", SUCCESS, "continue", turn1_msg),
        ("claude", SUCCESS, "propose_done"),
    ]
    result, sup = run_mission(env, mission)
    assert result == "idle"
    assert len(env.relay.launches) == 2
    l2 = env.relay.launches[1]
    assert l2["parent"] is not None
    assert "External wait satisfied:" in l2["task"]
    assert f"- CI PR #42@{head_sha}: success" in l2["task"]
    assert sum(env.clock.slept) >= rc.CI_POLL_INTERVAL_SECONDS


def test_pr_head_change_or_closure_wakes_immediately_with_uncertainty(tmp_path: Path) -> None:
    head_sha = "1111111111111111111111111111111111111111"

    # Part A: Head changes
    env_a = Env(tmp_path / "a")
    mission_a = mission_def("pr head advances")
    start_mission(env_a, mission_a)
    env_a.jobs["j-20261113T120000Z-111111"] = {"status": "running"}
    env_a.ci[(42, head_sha)] = {
        "observed_head": "2222222222222222222222222222222222222222",  # advanced!
        "state": "open",
        "required_checks": ["backend"],
        "checks": [{"name": "backend", "status": "in_progress", "conclusion": ""}],
    }
    msg_a = f"RELAY_STATUS continue\nJOB-WAIT: j-20261113T120000Z-111111\nCI-WAIT: 42@{head_sha}"
    env_a.relay.script = [
        ("claude", SUCCESS, "continue", msg_a),
        ("claude", SUCCESS, "propose_done"),
    ]
    result_a, sup_a = run_mission(env_a, mission_a)
    assert result_a == "idle"
    assert len(env_a.relay.launches) == 2
    l2_a = env_a.relay.launches[1]
    assert "External wait terminated (head_changed):" in l2_a["task"]
    assert "- JOB j-20261113T120000Z-111111: uncertain" in l2_a["task"]
    assert f"- CI PR #42@{head_sha}: head_changed (uncertain)" in l2_a["task"]

    # Part B: PR closed
    env_b = Env(tmp_path / "b")
    mission_b = mission_def("pr closed")
    start_mission(env_b, mission_b)
    env_b.ci[(42, head_sha)] = {
        "observed_head": head_sha,
        "state": "closed",
        "required_checks": ["backend"],
        "checks": [],
    }
    msg_b = f"RELAY_STATUS continue\nCI-WAIT: 42@{head_sha}"
    env_b.relay.script = [
        ("claude", SUCCESS, "continue", msg_b),
        ("claude", SUCCESS, "propose_done"),
    ]
    result_b, sup_b = run_mission(env_b, mission_b)
    assert result_b == "idle"
    assert len(env_b.relay.launches) == 2
    l2_b = env_b.relay.launches[1]
    assert "External wait terminated (pr_closed):" in l2_b["task"]
    assert f"- CI PR #42@{head_sha}: pr_closed (uncertain)" in l2_b["task"]


def test_unregistered_job_wakes_immediately_with_uncertainty(tmp_path: Path) -> None:
    env = Env(tmp_path)
    mission = mission_def("unregistered job")
    start_mission(env, mission)
    env.jobs["j-20261113T120000Z-888888"] = {"status": "running"}  # registered
    # j-20261113T120000Z-999999 is NOT in env.jobs

    turn_msg = (
        "RELAY_STATUS continue\n"
        "JOB-WAIT: j-20261113T120000Z-999999\n"
        "JOB-WAIT: j-20261113T120000Z-888888"
    )
    env.relay.script = [
        ("claude", SUCCESS, "continue", turn_msg),
        ("claude", SUCCESS, "propose_done"),
    ]
    result, sup = run_mission(env, mission)
    assert result == "idle"
    assert len(env.relay.launches) == 2
    l2 = env.relay.launches[1]
    assert "External wait terminated (unregistered_job):" in l2["task"]
    assert "- JOB j-20261113T120000Z-999999: unregistered_job (uncertain)" in l2["task"]
    assert "- JOB j-20261113T120000Z-888888: uncertain" in l2["task"]


def test_wait_timeout_wakes_with_uncertainty(tmp_path: Path) -> None:
    env = Env(tmp_path)
    mission = mission_def("wait timeout")
    start_mission(env, mission)
    env.jobs["j-20261113T120000Z-111111"] = {"status": "running"}  # never finishes

    turn_msg = "RELAY_STATUS continue\nJOB-WAIT: j-20261113T120000Z-111111"
    env.relay.script = [
        ("claude", SUCCESS, "continue", turn_msg),
        ("claude", SUCCESS, "propose_done"),
    ]
    result, sup = run_mission(env, mission)
    assert result == "idle"
    assert len(env.relay.launches) == 2
    l2 = env.relay.launches[1]
    assert "External wait terminated (wait_timeout):" in l2["task"]
    assert "- JOB j-20261113T120000Z-111111: wait_timeout (uncertain)" in l2["task"]
    assert sum(env.clock.slept) >= rc.WAIT_TIMEOUT_SECONDS


def test_no_checks_or_unreadable_ci_fails_closed(tmp_path: Path) -> None:
    head_sha = "1111111111111111111111111111111111111111"

    # Part A: Zero checks after 120s
    env_a = Env(tmp_path / "a")
    mission_a = mission_def("no checks")
    start_mission(env_a, mission_a)
    env_a.ci[(42, head_sha)] = {
        "observed_head": head_sha,
        "state": "open",
        "required_checks": ["backend"],
        "checks": [],
    }
    msg_a = f"RELAY_STATUS continue\nCI-WAIT: 42@{head_sha}"
    env_a.relay.script = [
        ("claude", SUCCESS, "continue", msg_a),
        ("claude", SUCCESS, "propose_done"),
    ]
    result_a, _ = run_mission(env_a, mission_a)
    assert result_a == "idle"
    l2_a = env_a.relay.launches[1]
    assert "External wait terminated (no_checks_reported):" in l2_a["task"]
    assert f"- CI PR #42@{head_sha}: no_checks_reported (uncertain)" in l2_a["task"]
    assert sum(env_a.clock.slept) >= 120

    # Part B: Unreadable through timeout
    env_b = Env(tmp_path / "b")
    mission_b = mission_def("unreadable ci")
    start_mission(env_b, mission_b)
    env_b.ci[(42, head_sha)] = {"unreadable": True}
    msg_b = f"RELAY_STATUS continue\nCI-WAIT: 42@{head_sha}"
    env_b.relay.script = [
        ("claude", SUCCESS, "continue", msg_b),
        ("claude", SUCCESS, "propose_done"),
    ]
    result_b, _ = run_mission(env_b, mission_b)
    assert result_b == "idle"
    l2_b = env_b.relay.launches[1]
    assert "External wait terminated (remote_unreadable):" in l2_b["task"]
    assert f"- CI PR #42@{head_sha}: remote_unreadable (uncertain)" in l2_b["task"]
    assert sum(env_b.clock.slept) >= rc.WAIT_TIMEOUT_SECONDS


def test_satisfied_waits_do_not_consume_checkpoint_limit(tmp_path: Path) -> None:
    env = Env(tmp_path)
    mission = mission_def("checkpoints test")
    start_mission(env, mission)
    env.jobs["j-20261113T120000Z-111111"] = {"status": "succeeded"}
    msg = "RELAY_STATUS continue\nJOB-WAIT: j-20261113T120000Z-111111"

    # 12 consecutive satisfied waits
    env.relay.script = [("claude", SUCCESS, "continue", msg) for _ in range(12)]
    env.relay.script.append(("claude", SUCCESS, "propose_done"))
    result, sup = run_mission(env, mission)
    assert result == "idle"
    assert sup.state["hold"] is None
    # Verify checkpoints were not consumed by satisfied waits
    rec = sup.state["mission"]
    assert rec.get("checkpoints", {}).get("0", 0) == 0

    # Now run 12 bare checkpoints:
    env2 = Env(tmp_path / "bare")
    mission2 = mission_def("bare checkpoints")
    start_mission(env2, mission2)
    env2.relay.script = [("claude", SUCCESS, "continue", "bare checkpoint") for _ in range(13)]
    result2, sup2 = run_mission(env2, mission2)
    assert result2 == "held"
    assert sup2.state["hold"]["reason"] == "task_checkpoint_limit"


def test_bare_checkpoint_enforces_minimum_interval(tmp_path: Path) -> None:
    env = Env(tmp_path)
    mission = mission_def("bare checkpoint interval")
    start_mission(env, mission)
    env.relay.script = [
        ("claude", SUCCESS, "continue", "first checkpoint"),
        ("claude", SUCCESS, "propose_done"),
    ]
    result, sup = run_mission(env, mission)
    assert result == "idle"
    # Bare continue turn must sleep MIN_CHECKPOINT_INTERVAL_SECONDS (60s) before relaunch
    assert sum(env.clock.slept) >= rc.MIN_CHECKPOINT_INTERVAL_SECONDS


def test_context_cap_and_resume_limits_force_fresh_context(tmp_path: Path) -> None:
    jid = "j-20261113T120000Z-111111"
    msg = f"RELAY_STATUS continue\nJOB-WAIT: {jid}"

    # Under cap (< 2M tokens) and under 4 resumes: continues session
    env1 = Env(tmp_path / "under")
    mission1 = mission_def("under cap")
    start_mission(env1, mission1)
    env1.jobs[jid] = {"status": "succeeded"}
    env1.relay.script = [
        ("claude", SUCCESS, "continue", msg, 1_400_000),
        ("claude", SUCCESS, "propose_done"),
    ]
    result1, sup1 = run_mission(env1, mission1)
    assert result1 == "idle"
    assert env1.relay.launches[1]["parent"] is not None
    assert "External wait satisfied:" in env1.relay.launches[1]["task"]

    # Over cap (> 2M tokens): forces fresh context (parent = None)
    env2 = Env(tmp_path / "over")
    mission2 = mission_def("over cap")
    start_mission(env2, mission2)
    env2.jobs[jid] = {"status": "succeeded"}
    env2.relay.script = [
        ("claude", SUCCESS, "continue", msg, 2_100_000),
        ("claude", SUCCESS, "propose_done"),
    ]
    result2, sup2 = run_mission(env2, mission2)
    assert result2 == "idle"
    assert env2.relay.launches[1]["parent"] is None
    assert "[Durable handoff summary]" in env2.relay.launches[1]["task"]

    # Missing context_used_tokens: does not force fresh context
    env3 = Env(tmp_path / "missing")
    mission3 = mission_def("missing tokens")
    start_mission(env3, mission3)
    env3.jobs[jid] = {"status": "succeeded"}
    env3.relay.script = [
        ("claude", SUCCESS, "continue", msg, None),
        ("claude", SUCCESS, "propose_done"),
    ]
    result3, sup3 = run_mission(env3, mission3)
    assert result3 == "idle"
    assert env3.relay.launches[1]["parent"] is not None

    # 5th consecutive resume forces fresh context
    env4 = Env(tmp_path / "fifth")
    mission4 = mission_def("consecutive resumes")
    start_mission(env4, mission4)
    env4.jobs[jid] = {"status": "succeeded"}
    # 5 "continue"+JOB-WAIT turns then propose_done
    env4.relay.script = [
        ("claude", SUCCESS, "continue", msg, 100_000),
        ("claude", SUCCESS, "continue", msg, 100_000),
        ("claude", SUCCESS, "continue", msg, 100_000),
        ("claude", SUCCESS, "continue", msg, 100_000),
        ("claude", SUCCESS, "continue", msg, 100_000),
        ("claude", SUCCESS, "propose_done"),
    ]
    result4, sup4 = run_mission(env4, mission4)
    assert result4 == "idle"
    for i in range(1, 5):
        assert env4.relay.launches[i]["parent"] is not None
        assert env4.relay.launches[i]["task"].startswith("[relay-continuity resume")
    assert env4.relay.launches[5]["parent"] is None
    assert "[Durable handoff summary]" in env4.relay.launches[5]["task"]


def test_provider_change_and_native_agents_force_fresh_context(tmp_path: Path) -> None:
    jid = "j-20261113T120000Z-111111"
    msg = f"RELAY_STATUS continue\nJOB-WAIT: {jid}"

    # Provider switch from claude to codex forces parent = None
    env1 = Env(tmp_path / "switch")
    mission1 = mission_def("switch agent")
    start_mission(env1, mission1)
    env1.jobs[jid] = {"status": "succeeded"}
    env1.relay.readings = room(env1, "codex")
    env1.head(SUCCESS, agent="claude")
    sup1 = env1.supervisor()
    sup1.mission = mission1
    sup1.state = env1.store.load()
    sup1.state["head_session"] = HEAD
    sup1.state["agent"] = "claude"
    sup1.state["cooldowns"]["claude"] = {"until": rc.iso(env1.clock.now() + timedelta(hours=2)), "reason": "quota"}
    sup1.save()
    turn_dir = env1.relay.sessions_dir() / HEAD / "turns" / "turn-001"
    (turn_dir / "last-message.txt").write_text(msg, encoding="utf-8")
    (turn_dir / "state.json").write_text(json.dumps({"agent_key": "claude", "status": "continue"}))
    env1.relay.script = [("codex", CODEX_SUCCESS, "propose_done")]
    result1 = sup1.run(HEAD)
    assert result1 == "idle"
    assert env1.relay.launches[0]["agent"] == "codex"
    assert env1.relay.launches[0]["parent"] is None

    # Native agent (argon) forces parent = None
    env2 = Env(tmp_path / "native")
    mission2 = mission_def("native agent")
    start_mission(env2, mission2)
    env2.jobs[jid] = {"status": "succeeded"}
    env2.relay.argon_limits = lambda agent: {"windows": [{"used_percent": 10}]} if agent == "argon" else None

    ra = rc.relay_argon
    argon_ev = [
        {"event": "init", "conversation_id": "c1", "init": {"model": ra.MODEL}},
        {"event": "step_update", "step_update": {"step_index": 2, "state": "ACTIVE", "conversation_id": "c1",
         "step_type": "tool", "tool_name": "read_file", "tool_info": {"parameters": {"Path": "/tmp/test"}}}},
        {"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "conversation_id": "c1",
         "step_type": "tool", "tool_name": "read_file", "tool_info": {"parameters": {"Path": "/tmp/test"}}}},
        {"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS", "response": msg}},
    ]
    meta = {"schema": "argon-coordinator.v1", "launched": True, "exit_code": 0, "quota": [], "reason": None, "model": ra.MODEL}

    turn_dir2 = env2.relay.sessions_dir() / HEAD / "turns" / "turn-001"
    turn_dir2.mkdir(parents=True, exist_ok=True)
    (turn_dir2 / "last-message.txt").write_text(msg, encoding="utf-8")
    (turn_dir2 / "state.json").write_text(json.dumps({"agent_key": "argon", "status": "continue"}))
    (turn_dir2 / "output.jsonl").write_text("".join(json.dumps(e) + "\n" for e in argon_ev))
    (turn_dir2 / "native-result.json").write_text(json.dumps(meta))
    pin_to_t0(turn_dir2)

    sup2 = env2.supervisor()
    sup2.mission = mission2
    sup2.state = env2.store.load()
    sup2.state["head_session"] = HEAD
    sup2.state["agent"] = "argon"
    sup2.save()
    env2.relay.script = [("argon", argon_ev, "propose_done")]
    result2 = sup2.run(HEAD)
    assert result2 == "idle"
    assert len(env2.relay.launches) == 1
    assert env2.relay.launches[0]["agent"] == "argon"
    assert env2.relay.launches[0]["parent"] is None


def test_wait_persists_across_restart_and_honors_stop(tmp_path: Path) -> None:
    jid = "j-20261113T120000Z-111111"
    env = Env(tmp_path)
    mission = mission_def("wait persists")
    start_mission(env, mission)
    env.jobs[jid] = {"status": "running"}

    msg = f"RELAY_STATUS continue\nJOB-WAIT: {jid}"
    env.head(SUCCESS, agent="claude")
    turn_dir = env.relay.sessions_dir() / HEAD / "turns" / "turn-001"
    (turn_dir / "last-message.txt").write_text(msg, encoding="utf-8")
    (turn_dir / "state.json").write_text(json.dumps({"agent_key": "claude", "status": "continue"}))

    sup = env.supervisor()
    sup.mission = mission
    sup.state["mission"] = {"id": mission["id"], "next": 0, "total": 1, "done": []}

    # Request stop before running
    env.store.stop_path.write_text("stop requested")
    res = sup.run(HEAD)
    assert res == "stopped"
    assert sup.state["phase"] == "stopped"
    assert sup.state["wait"] is not None
    assert sup.state["wait"]["jobs"] == [jid]
    # No new launch occurred
    assert len(env.relay.launches) == 0

    # Restart: clear stop, job is still running, stop again while waiting
    env.store.stop_path.unlink()

    def dynamic_job(job_id: str) -> dict[str, Any] | None:
        if env.clock.now() > T0 + timedelta(seconds=20):
            return {"status": "succeeded"}
        return {"status": "running"}

    env.host.job_status = dynamic_job
    env.relay.script = [("claude", SUCCESS, "propose_done")]

    sup2 = env.supervisor()
    sup2.mission = mission
    res2 = sup2.run(HEAD)
    assert res2 == "idle"
    assert len(env.relay.launches) == 1
    assert "External wait satisfied:" in env.relay.launches[0]["task"]


def test_side_effect_holds_preempt_external_waits(tmp_path: Path) -> None:
    jid = "j-20261113T120000Z-111111"
    msg = f"RELAY_STATUS continue\nJOB-WAIT: {jid}"

    # Part A: gate hold (output_unparseable) preempts wait
    env_a = Env(tmp_path / "a")
    mission_a = mission_def("gate hold preempts")
    start_mission(env_a, mission_a)
    env_a.head(SUCCESS, agent="claude")
    turn_a = env_a.relay.sessions_dir() / HEAD / "turns" / "turn-001"
    (turn_a / "last-message.txt").write_text(msg, encoding="utf-8")
    (turn_a / "state.json").write_text(json.dumps({"agent_key": "claude", "status": "continue"}))
    (turn_a / "output.jsonl").write_text("{bad json\n")
    sup_a = env_a.supervisor()
    sup_a.mission = mission_a
    sup_a.state["mission"] = {"id": mission_a["id"], "next": 0, "total": 1, "done": []}
    res_a = sup_a.run(HEAD)
    assert res_a == "held"
    assert sup_a.state["hold"]["reason"] == "output_unparseable"
    assert sup_a.state["wait"] is None

    # Part B: effects_uncertain (unresolved push) preempts wait
    env_b = Env(tmp_path / "b")
    mission_b = mission_def("effects uncertain preempts")
    start_mission(env_b, mission_b)
    env_b.head([IN_FLIGHT["git push"], *SUCCESS], agent="claude")
    turn_b = env_b.relay.sessions_dir() / HEAD / "turns" / "turn-001"
    (turn_b / "last-message.txt").write_text(msg, encoding="utf-8")
    (turn_b / "state.json").write_text(json.dumps({"agent_key": "claude", "status": "continue"}))
    sup_b = env_b.supervisor()
    sup_b.mission = mission_b
    sup_b.state["mission"] = {"id": mission_b["id"], "next": 0, "total": 1, "done": []}
    res_b = sup_b.run(HEAD)
    assert res_b == "held"
    assert sup_b.state["hold"]["reason"] == "effects_uncertain"
    assert sup_b.state["wait"] is None


def test_default_pr_ci_status_ruleset_and_fallback(tmp_path: Path) -> None:
    head = "a" * 40
    pr_json = json.dumps({
        "state": "OPEN",
        "headRefOid": head,
        "baseRefName": "main",
        "statusCheckRollup": [
            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "test", "status": "IN_PROGRESS", "conclusion": ""},
        ],
    })

    # Case 1: ruleset provides required checks
    ruleset_json = json.dumps([
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": "build"},
                    "test",
                ],
            },
        },
    ])

    def mock_run_ruleset(cmd: list[str], **kwargs: Any) -> tuple[int, str]:
        if cmd[:3] == ["gh", "pr", "view"]:
            return 0, pr_json
        if cmd[:4] == ["gh", "api", "repos/:owner/:repo/rules/branches/main"]:
            return 0, ruleset_json
        return 1, "not found"

    res = rc.default_pr_ci_status(tmp_path, 42, head, run=mock_run_ruleset)
    assert not res.get("unreadable")
    assert res["required_checks"] == ["build", "test"]
    assert res["observed_head"] == head
    assert res["base_ref"] == "main"

    # Case 2: ruleset fails, branch protection fallback provides required checks
    prot_json = json.dumps({
        "contexts": ["lint"],
        "checks": [{"context": "backend"}],
        "enforcement_level": "non_admins",
    })

    def mock_run_fallback(cmd: list[str], **kwargs: Any) -> tuple[int, str]:
        if cmd[:3] == ["gh", "pr", "view"]:
            return 0, pr_json
        if cmd[:4] == ["gh", "api", "repos/:owner/:repo/rules/branches/main"]:
            return 1, "404 Not Found"
        if cmd[:4] == ["gh", "api", "repos/:owner/:repo/branches/main/protection/required_status_checks"]:
            return 0, prot_json
        return 1, "not found"

    res_fb = rc.default_pr_ci_status(tmp_path, 42, head, run=mock_run_fallback)
    assert not res_fb.get("unreadable")
    assert res_fb["required_checks"] == ["backend", "lint"]

    # Case 3: both surfaces fail or yield empty required checks -> fails closed
    def mock_run_fail_closed(cmd: list[str], **kwargs: Any) -> tuple[int, str]:
        if cmd[:3] == ["gh", "pr", "view"]:
            return 0, pr_json
        return 1, "404 Not Found"

    res_fc = rc.default_pr_ci_status(tmp_path, 42, head, run=mock_run_fail_closed)
    assert res_fc == {"unreadable": True}

    # Case 4: PR view fails -> fails closed
    def mock_run_pr_fail(cmd: list[str], **kwargs: Any) -> tuple[int, str]:
        return 1, "error"

    res_pf = rc.default_pr_ci_status(tmp_path, 42, head, run=mock_run_pr_fail)
    assert res_pf == {"unreadable": True}

    # Case 5: PR view JSON has no headRefOid -> fails closed
    pr_json_no_head = json.dumps({
        "state": "OPEN",
        "baseRefName": "main",
        "statusCheckRollup": [
            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ],
    })

    def mock_run_no_head(cmd: list[str], **kwargs: Any) -> tuple[int, str]:
        if cmd[:3] == ["gh", "pr", "view"]:
            return 0, pr_json_no_head
        if cmd[:4] == ["gh", "api", "repos/:owner/:repo/rules/branches/main"]:
            return 0, ruleset_json
        return 1, "not found"

    res_nh = rc.default_pr_ci_status(tmp_path, 42, head, run=mock_run_no_head)
    assert res_nh == {"unreadable": True}


def test_status_context_ci_checks(tmp_path: Path) -> None:
    head = "a" * 40

    def make_mock(rollup: list[dict[str, Any]]) -> rc.Runner:
        pr_json = json.dumps({
            "state": "OPEN",
            "headRefOid": head,
            "baseRefName": "main",
            "statusCheckRollup": rollup,
        })
        ruleset_json = json.dumps([
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": "ci/jenkins"},
                    ]
                },
            },
        ])

        def mock_run(cmd: list[str], **kwargs: Any) -> tuple[int, str]:
            if cmd[:3] == ["gh", "pr", "view"]:
                return 0, pr_json
            if cmd[:4] == ["gh", "api", "repos/:owner/:repo/rules/branches/main"]:
                return 0, ruleset_json
            return 1, "not found"

        return mock_run

    # 1. Success case: SUCCESS state maps to completed success
    runner_success = make_mock([{"context": "ci/jenkins", "state": "SUCCESS"}])
    res_success = rc.default_pr_ci_status(tmp_path, 42, head, run=runner_success)
    assert not res_success.get("unreadable")
    assert res_success["checks"] == [{"name": "ci/jenkins", "status": "success", "conclusion": "success"}]
    assert rc.evaluate_pr_ci(res_success, head, 0.0) == ("completed", "success")

    # 2. Failure case: FAILURE state maps to failure
    runner_failure = make_mock([{"context": "ci/jenkins", "state": "FAILURE"}])
    res_failure = rc.default_pr_ci_status(tmp_path, 42, head, run=runner_failure)
    assert not res_failure.get("unreadable")
    assert res_failure["checks"] == [{"name": "ci/jenkins", "status": "failure", "conclusion": "failure"}]
    assert rc.evaluate_pr_ci(res_failure, head, 0.0) == ("immediate_wake", "check_failed")

    # 3. Error case: ERROR state maps to failure (error in CI_FAILURE_CONCLUSIONS)
    runner_error = make_mock([{"context": "ci/jenkins", "state": "ERROR"}])
    res_error = rc.default_pr_ci_status(tmp_path, 42, head, run=runner_error)
    assert not res_error.get("unreadable")
    assert res_error["checks"] == [{"name": "ci/jenkins", "status": "error", "conclusion": "error"}]
    assert rc.evaluate_pr_ci(res_error, head, 0.0) == ("immediate_wake", "check_failed")

    # 4. Pending cases: PENDING / EXPECTED state stays pending
    runner_pending = make_mock([{"context": "ci/jenkins", "state": "PENDING"}])
    res_pending = rc.default_pr_ci_status(tmp_path, 42, head, run=runner_pending)
    assert not res_pending.get("unreadable")
    assert res_pending["checks"] == [{"name": "ci/jenkins", "status": "pending", "conclusion": "pending"}]
    assert rc.evaluate_pr_ci(res_pending, head, 0.0) == ("pending", "in_progress")

    runner_expected = make_mock([{"context": "ci/jenkins", "state": "EXPECTED"}])
    res_expected = rc.default_pr_ci_status(tmp_path, 42, head, run=runner_expected)
    assert not res_expected.get("unreadable")
    assert res_expected["checks"] == [{"name": "ci/jenkins", "status": "expected", "conclusion": "expected"}]
    assert rc.evaluate_pr_ci(res_expected, head, 0.0) == ("pending", "in_progress")


def test_evaluate_pr_ci_acceptance_criteria() -> None:
    head = "a" * 40
    # 1. unreadable input
    assert rc.evaluate_pr_ci({"unreadable": True}, head, 0.0) == ("unreadable", "remote_unreadable")
    assert rc.evaluate_pr_ci({}, head, 0.0) == ("unreadable", "remote_unreadable")
    # missing or empty required_checks fails closed
    empty_req = {"observed_head": head, "state": "open", "required_checks": [], "checks": []}
    assert rc.evaluate_pr_ci(empty_req, head, 0.0) == ("unreadable", "remote_unreadable")
    # missing or empty observed_head fails closed
    info_no_head = {
        "state": "open",
        "required_checks": ["backend"],
        "checks": [{"name": "backend", "status": "completed", "conclusion": "success"}],
    }
    assert rc.evaluate_pr_ci(info_no_head, head, 0.0) == ("unreadable", "remote_unreadable")
    info_empty_head = {
        "observed_head": "",
        "state": "open",
        "required_checks": ["backend"],
        "checks": [{"name": "backend", "status": "completed", "conclusion": "success"}],
    }
    assert rc.evaluate_pr_ci(info_empty_head, head, 0.0) == ("unreadable", "remote_unreadable")

    # 2. head changed -> immediate wake
    info_changed = {"observed_head": "b" * 40, "state": "open", "required_checks": ["backend"], "checks": []}
    assert rc.evaluate_pr_ci(info_changed, head, 0.0) == ("immediate_wake", "head_changed")

    # 3. PR closed / merged -> immediate wake
    info_closed = {"observed_head": head, "state": "closed", "required_checks": ["backend"], "checks": []}
    assert rc.evaluate_pr_ci(info_closed, head, 0.0) == ("immediate_wake", "pr_closed")
    info_merged = {"observed_head": head, "state": "merged", "required_checks": ["backend"], "checks": []}
    assert rc.evaluate_pr_ci(info_merged, head, 0.0) == ("immediate_wake", "pr_closed")

    # 4. Immediate wake on required check failure
    info_failed = {
        "observed_head": head,
        "state": "open",
        "required_checks": ["backend", "frontend"],
        "checks": [
            {"name": "backend", "status": "completed", "conclusion": "failure"},
            {"name": "frontend", "status": "in_progress", "conclusion": ""},
        ],
    }
    assert rc.evaluate_pr_ci(info_failed, head, 0.0) == ("immediate_wake", "check_failed")

    # 5. Non-required check failure does not fail if required checks pass
    info_non_req_failed = {
        "observed_head": head,
        "state": "open",
        "required_checks": ["backend"],
        "checks": [
            {"name": "backend", "status": "completed", "conclusion": "success"},
            {"name": "optional_check", "status": "completed", "conclusion": "failure"},
        ],
    }
    assert rc.evaluate_pr_ci(info_non_req_failed, head, 0.0) == ("completed", "success")

    # 6. Zero checks reported rule: 120s threshold
    info_zero = {"observed_head": head, "state": "open", "required_checks": ["backend"], "checks": []}
    assert rc.evaluate_pr_ci(info_zero, head, 60.0) == ("pending", "no_checks_reported")
    assert rc.evaluate_pr_ci(info_zero, head, 120.0) == ("immediate_wake", "no_checks_reported")
    assert rc.evaluate_pr_ci(info_zero, head, 150.0) == ("immediate_wake", "no_checks_reported")

    # 7. In progress pending
    info_running = {
        "observed_head": head,
        "state": "open",
        "required_checks": ["backend"],
        "checks": [{"name": "backend", "status": "in_progress", "conclusion": ""}],
    }
    assert rc.evaluate_pr_ci(info_running, head, 60.0) == ("pending", "in_progress")


def test_validate_state_spec_267_fields() -> None:
    state = rc.default_state()
    assert rc.validate_state(state) is None

    # resumes validation
    bad_resumes = {**state, "resumes": -1}
    assert rc.validate_state(bad_resumes) == "bad resumes"
    bad_resumes_bool = {**state, "resumes": True}
    assert rc.validate_state(bad_resumes_bool) == "bad resumes"
    valid_resumes = {**state, "resumes": 3}
    assert rc.validate_state(valid_resumes) is None

    # last_checkpoint_at validation
    bad_cp = {**state, "last_checkpoint_at": "not-a-timestamp"}
    assert rc.validate_state(bad_cp) == "bad last_checkpoint_at"
    valid_cp = {**state, "last_checkpoint_at": rc.iso(T0)}
    assert rc.validate_state(valid_cp) is None

    # wait validation
    bad_wait_empty = {**state, "wait": {"jobs": [], "ci": [], "started_at": rc.iso(T0)}}
    assert rc.validate_state(bad_wait_empty) == "bad wait record"
    bad_wait_job = {**state, "wait": {"jobs": ["invalid_job_id"], "ci": [], "started_at": rc.iso(T0)}}
    assert rc.validate_state(bad_wait_job) == "bad wait record"
    bad_wait_ci_sha = {**state, "wait": {"jobs": [], "ci": [{"pr": 1, "sha": "short"}], "started_at": rc.iso(T0)}}
    assert rc.validate_state(bad_wait_ci_sha) == "bad wait record"
    bad_wait_ci_pr = {**state, "wait": {"jobs": [], "ci": [{"pr": 0, "sha": "a" * 40}], "started_at": rc.iso(T0)}}
    assert rc.validate_state(bad_wait_ci_pr) == "bad wait record"
    bad_wait_ts = {**state, "wait": {"jobs": ["j-20261113T120000Z-111111"], "ci": [], "started_at": "bogus"}}
    assert rc.validate_state(bad_wait_ts) == "bad wait record"

    valid_wait = {
        **state,
        "wait": {
            "jobs": ["j-20261113T120000Z-111111"],
            "ci": [{"pr": 42, "sha": "a" * 40}],
            "started_at": rc.iso(T0),
            "last_job_poll": rc.iso(T0),
            "last_ci_poll": None,
        },
    }
    assert rc.validate_state(valid_wait) is None


# ---------------------------------------------------------------------------
# Spec 268: Bounded turns and provider failure handling


def test_relay_turn_wall_limit_terminates_process_group_and_settles(tmp_path: Path) -> None:
    """Criterion 1: Fake Relay turn running past wall limit is terminated with process group,
    reaped, settled as not successful with wall limit named in report, and leaves cooldowns unchanged."""
    signals_sent: list[tuple[int, int]] = []
    fake_pid = 42424

    class HungProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode = None
            self.alive = True

        def poll(self) -> int | None:
            return None if self.alive else self.returncode

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode or 0

    hung = HungProc(fake_pid)

    def fake_killpg(pgid: int, sig: int) -> None:
        signals_sent.append((pgid, sig))
        if sig == signal.SIGKILL:
            hung.alive = False
            hung.returncode = -signal.SIGKILL
        elif sig == signal.SIGTERM:
            pass  # simulate hanging on SIGTERM

    sim_time = [0.0]

    def fake_clock() -> float:
        return sim_time[0]

    def fake_sleep(duration: float) -> None:
        sim_time[0] += duration

    adapter = rc.RelayAdapter(
        tmp_path,
        turn_wall=10.0,
        clock=fake_clock,
        sleep=fake_sleep,
        killpg=fake_killpg,
    )
    result = adapter.finish(hung)
    assert not result.raw_ok
    assert "turn wall limit (10s) exceeded" in result.diagnostics
    assert (fake_pid, signal.SIGTERM) in signals_sent
    assert (fake_pid, signal.SIGKILL) in signals_sent

    # Integration with Supervisor: verify settlement as unsuccessful, naming limit in report, cooldowns unchanged
    env = Env(tmp_path)
    env.host.relay.turn_wall = 10.0
    env.host.relay.clock = fake_clock
    env.host.relay.sleep = fake_sleep
    env.host.relay.killpg = fake_killpg
    hung2 = HungProc(fake_pid)
    launches: list[str] = []

    def hung_start(agent: str, parent: str | None, text: str, turns: int, **kw: Any) -> Any:
        launches.append(text)
        return hung2

    def hung_finish(proc: Any, timeout: float | None = None) -> rc.RunResult:
        return adapter.finish(hung2)

    env.relay.start = hung_start
    env.relay.finish = hung_finish
    sid = "20261018-000001-bbbbbb"
    t_dir = env.relay.sessions_dir() / sid / "turns" / "turn-001"
    t_dir.mkdir(parents=True)
    (t_dir / "state.json").write_text(json.dumps({"agent_key": "claude"}))
    (t_dir / "output.jsonl").write_text("")
    (t_dir / "prompt.md").write_text(f"## Task\n[relay-continuity attempt {env.store.root.name}]\ntask")

    env.relay.find_by_nonce = lambda nonce, since: sid
    sup = env.supervisor()
    sup.state["permissions_digest"] = env.perm["v"]
    report = sup._launch("claude", "[relay-continuity attempt {nonce}]\ntask", None)
    assert isinstance(report, rc.TurnReport)
    assert report.outcome != "success"
    assert "turn wall limit" in report.detail.lower()
    assert sup.state["cooldowns"] == {}
    assert "Your turn has a hard limit of 60 minutes." in launches[0]


def test_pre_launch_reachable_probe_waiting_and_single_ledger_finding(tmp_path: Path) -> None:
    """Criterion 2: Failing Host.reachable makes _launch return 'waiting' with attempts, pending,
    counters and cooldowns unchanged and nothing passed to relay.start; repeated failing probes write
    one ledger finding; once probe succeeds launch proceeds."""
    env = Env(tmp_path, [("claude", SUCCESS)])
    reachable_status = [False]
    env.host.reachable = lambda: reachable_status[0]

    sup = env.supervisor()
    sup.state["permissions_digest"] = env.perm["v"]
    # First failing probe
    res1 = sup._launch("claude", "[relay-continuity attempt {nonce}]\ntask", None)
    assert res1 == "waiting"
    assert sup.attempts == 0
    assert sup.state["pending"] is None
    assert sup.state["cooldowns"] == {}
    assert sup.state["counters"]["consecutive_failures"] == 0
    assert len(env.relay.launches) == 0
    findings1 = [t for k, t in env.ledger if k == "finding" and "probe failed" in t]
    assert len(findings1) == 1

    # Second failing probe (repeated failure during outage)
    res2 = sup._launch("claude", "[relay-continuity attempt {nonce}]\ntask", None)
    assert res2 == "waiting"
    assert sup.attempts == 0
    assert sup.state["pending"] is None
    assert len(env.relay.launches) == 0
    # Exactly one ledger finding per outage
    findings2 = [t for k, t in env.ledger if k == "finding" and "probe failed" in t]
    assert len(findings2) == 1

    # Network recovers: probe succeeds
    reachable_status[0] = True
    res3 = sup._launch("claude", "[relay-continuity attempt {nonce}]\ntask", None)
    assert res3 != "waiting"
    assert sup.attempts == 1
    assert len(env.relay.launches) == 1


def test_auth_failure_writes_single_question_with_two_options_and_cooldown_until_none(tmp_path: Path) -> None:
    """Criterion 3: An 'auth' outcome writes exactly one ledger question whose text
    parse_open_questions accepts with two options, records a cooldown with until: None and source: 'auth',
    and no further question or retry follows on later decisions, restarts or polls."""
    obs_spec = importlib.util.spec_from_file_location("relay_observe", SCRIPT.parent / "relay_observe.py")
    assert obs_spec and obs_spec.loader
    obs = importlib.util.module_from_spec(obs_spec)
    obs_spec.loader.exec_module(obs)

    env = Env(tmp_path, [("any", SUCCESS)])
    entries: list[dict[str, Any]] = []

    def fake_ledger(kind: str, text: str) -> str:
        qid = f"L{env.clock.now().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(2)}"
        entry = {"id": qid, "kind": kind, "text": text, "ts": rc.iso(env.clock.now())}
        entries.append(entry)
        env.ledger.append((kind, text))
        return qid

    env.host.ledger = fake_ledger
    env.host.read_ledger = lambda: list(entries)

    events = [claude_result("Invalid API key · Please run /login", error=True, status=401)]
    env.head(events)
    result, sup = env.run()
    assert result == "idle"

    cooldown = sup.state["cooldowns"]["claude"]
    assert cooldown["reason"] == "auth"
    assert cooldown["until"] is None
    assert cooldown["source"] == "auth"
    qid = cooldown["question"]
    assert qid is not None

    # Check the question with parse_open_questions
    q_entries = [e for e in entries if e["kind"] == "question" and e["id"] == qid]
    assert len(q_entries) == 1
    parsed = obs.parse_open_questions(q_entries)
    assert len(parsed) == 1
    opts = parsed[0]["options"]
    assert len(opts) == 2
    assert opts[0]["index"] == 1
    assert opts[0]["recommended"] is True
    assert opts[1]["index"] == 2
    assert opts[1]["recommended"] is False

    # Deduplication: later decision/set_cooldown produces no new question
    rep = rc.TurnReport(outcome="auth", agent="claude", detail="unauthorized", turn_id="t1")
    sup.set_cooldown("claude", rep)
    assert len([e for e in entries if e["kind"] == "question"]) == 1

    # Restart produces no new question and no retry of claude
    sup2 = rc.Supervisor(env.store, env.host, env.limits)
    sup2.state = env.store.load()
    assert not sup2.available("claude")
    assert len([e for e in entries if e["kind"] == "question"]) == 1


def test_clearing_auth_cooldown_by_decision_or_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Criterion 4: Maintainer answer 1 to that question makes agent available again; answer 2 or other text
    leaves it excluded; release --clear-cooldown clears it; limits reading with room does not."""
    env = Env(tmp_path)
    qid = "L20261018080000-q001"
    sup = env.supervisor()
    sup.state["cooldowns"]["claude"] = {
        "until": None, "source": "auth", "reason": "auth", "since": rc.iso(T0), "turn": "turn-1", "question": qid,
    }
    env.store.save(sup.state, T0)

    # 1. Limits reading with room does NOT clear auth cooldown
    env.relay.readings = room(env, "claude")
    assert not sup.available("claude")
    assert "claude" in sup.state["cooldowns"]

    # 2. Answer 2 leaves it excluded
    ledger_entries = [
        {"id": "L20261018080500-d001", "kind": "decision", "status": "accepted",
         "text": f"Maintainer answer to question {qid} (via slack): 2 - Mantieni escluso",
         "supersedes": [qid]},
    ]
    env.host.read_ledger = lambda: list(ledger_entries)
    assert not sup.available("claude")
    assert "claude" in sup.state["cooldowns"]

    # 3. Answer with other text leaves it excluded
    ledger_entries = [
        {"id": "L20261018080500-d001", "kind": "decision", "status": "accepted",
         "text": f"Maintainer answer to question {qid} (via slack): altro - non so",
         "supersedes": [qid]},
    ]
    assert not sup.available("claude")
    assert "claude" in sup.state["cooldowns"]

    # 4. Answer 1 clears cooldown and makes agent available
    ledger_entries = [
        {"id": "L20261018080500-d002", "kind": "decision", "status": "accepted",
         "text": f"Maintainer answer to question {qid} (via slack): 1 - Ho ripristinato l'accesso",
         "supersedes": [qid]},
    ]
    assert sup.available("claude")
    assert "claude" not in sup.state["cooldowns"]

    # 5. release --clear-cooldown claude clears it
    sup.state["cooldowns"]["claude"] = {
        "until": None, "source": "auth", "reason": "auth", "since": rc.iso(T0), "turn": "turn-1", "question": qid,
    }
    env.store.save(sup.state, T0)
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(env.store.root))
    monkeypatch.setattr(rc, "make_host", lambda repo: env.host)
    monkeypatch.setattr(rc, "repo_root", lambda: env.repo)
    env.host.read_ledger = lambda: []  # no decision
    assert rc.main(["release", "--clear-cooldown", "claude", "--note", "manual clear"]) == 0
    sup2 = env.supervisor()
    sup2.state = env.store.load()
    assert "claude" not in sup2.state["cooldowns"]


def test_fresh_reading_ends_limits_or_message_cooldown_for_relay_and_native(tmp_path: Path) -> None:
    """Criterion 5: A future 'limits' or 'message' cooldown with a fresh reading showing room and
    newer than 'since' makes the agent available and drops the cooldown, for a Relay agent and for a native agent."""
    env = Env(tmp_path)
    sup = env.supervisor()
    cooldown_since = T0 - timedelta(minutes=10)
    cooldown_until = T0 + timedelta(hours=4)
    as_of = T0 - timedelta(minutes=5)

    # 1. Relay agent with limits cooldown
    sup.state["cooldowns"]["claude"] = {
        "until": rc.iso(cooldown_until), "source": "limits", "reason": "quota", "since": rc.iso(cooldown_since),
    }
    env.relay.readings = lambda: {
        "claude": reading("claude", 5.0, T0 + timedelta(days=1), as_of),
        "codex": reading("codex", 10.0, T0 + timedelta(days=1), as_of),
    }
    assert sup.available("claude")
    assert "claude" not in sup.state["cooldowns"]
    assert sup.state["counters"]["reading_clear_claude"] == 1
    findings = [t for k, t in env.ledger if k == "finding" and "cleared limits cooldown for claude" in t]
    assert len(findings) == 1

    # 2. Relay agent with message cooldown
    sup.state["cooldowns"]["codex"] = {
        "until": rc.iso(cooldown_until), "source": "message", "reason": "quota", "since": rc.iso(cooldown_since),
    }
    assert sup.available("codex")
    assert "codex" not in sup.state["cooldowns"]
    assert sup.state["counters"]["reading_clear_codex"] == 1

    # 3. Native agent with limits cooldown
    sup.state["cooldowns"]["argon"] = {
        "until": rc.iso(cooldown_until), "source": "limits", "reason": "quota", "since": rc.iso(cooldown_since),
    }
    env.relay.argon_limits = lambda agent: {
        "as_of": rc.iso(as_of),
        "windows": [{"used_percent": 15.0, "disabled": False, "resets_at": rc.iso(T0 + timedelta(days=1))}],
    } if agent == "argon" else None
    assert sup.available("argon")
    assert "argon" not in sup.state["cooldowns"]
    assert sup.state["counters"]["reading_clear_argon"] == 1


def test_reading_clear_only_once_until_successful_turn(tmp_path: Path) -> None:
    """Criterion 6: After a reading clear, a new quota failure records a cooldown that a further reading with
    room does not clear until a successful turn resets the counter."""
    env = Env(tmp_path)
    sup = env.supervisor()
    cooldown_since = T0 - timedelta(minutes=10)
    cooldown_until = T0 + timedelta(hours=4)
    as_of = T0 - timedelta(minutes=5)

    # First clear succeeds
    sup.state["cooldowns"]["claude"] = {
        "until": rc.iso(cooldown_until), "source": "limits", "reason": "quota", "since": rc.iso(cooldown_since),
    }
    env.relay.readings = lambda: {"claude": reading("claude", 5.0, T0 + timedelta(days=1), as_of)}
    assert sup.available("claude")
    assert "claude" not in sup.state["cooldowns"]
    assert sup.state["counters"]["reading_clear_claude"] == 1

    # New quota failure occurs
    report_quota = rc.TurnReport(outcome="quota", agent="claude", detail="hit usage limit", reset_at=rc.iso(cooldown_until))
    sup.set_cooldown("claude", report_quota)
    assert "claude" in sup.state["cooldowns"]

    # Even with fresh reading with room, second clear does NOT happen
    as_of2 = T0 - timedelta(minutes=2)
    env.relay.readings = lambda: {"claude": reading("claude", 5.0, T0 + timedelta(days=1), as_of2)}
    assert not sup.available("claude")
    assert "claude" in sup.state["cooldowns"]

    # A successful turn resets the counter
    success_report = rc.TurnReport(outcome="success", agent="claude", detail="result ok")
    sup._decide(success_report, None)
    assert "reading_clear_claude" not in sup.state["counters"]

    # Now a third quota cooldown CAN be cleared by a reading with room
    cooldown_since3 = T0
    sup.state["cooldowns"]["claude"] = {
        "until": rc.iso(cooldown_until), "source": "limits", "reason": "quota", "since": rc.iso(cooldown_since3),
    }
    as_of3 = T0 + timedelta(minutes=1)
    env.clock.sleep(120)
    sup._reading = None
    env.relay.readings = lambda: {"claude": reading("claude", 5.0, T0 + timedelta(days=1), as_of3)}
    assert sup.available("claude")
    assert "claude" not in sup.state["cooldowns"]
    assert sup.state["counters"]["reading_clear_claude"] == 1


def test_reading_clear_boundaries_estimate_manual_stale_missing_unparseable_exhausted(tmp_path: Path) -> None:
    """Criterion 7: estimate and manual cooldowns are not cleared by a reading with room;
    stale, missing, unparseable and exhausted readings leave limits and message cooldowns intact."""
    env = Env(tmp_path)
    sup = env.supervisor()
    cooldown_since = T0 - timedelta(minutes=10)
    cooldown_until = T0 + timedelta(hours=4)
    as_of = T0 - timedelta(minutes=5)
    def good_readings() -> dict[str, Any]:
        return {"claude": reading("claude", 5.0, T0 + timedelta(days=1), as_of)}

    # 1. estimate cooldown not cleared
    sup.state["cooldowns"]["claude"] = {
        "until": rc.iso(cooldown_until), "source": "estimate", "reason": "transient", "since": rc.iso(cooldown_since),
    }
    env.relay.readings = good_readings
    assert not sup.available("claude")
    assert sup.state["cooldowns"]["claude"]["source"] == "estimate"

    # 2. manual cooldown not cleared
    sup.state["cooldowns"]["claude"] = {
        "until": None, "source": "manual", "reason": "spend_limit", "since": rc.iso(cooldown_since),
    }
    assert not sup.available("claude")
    assert sup.state["cooldowns"]["claude"]["source"] == "manual"

    # 3. stale reading (as_of <= since) leaves limits cooldown intact
    sup.state["cooldowns"]["claude"] = {
        "until": rc.iso(cooldown_until), "source": "limits", "reason": "quota", "since": rc.iso(cooldown_since),
    }
    stale_as_of = cooldown_since - timedelta(minutes=1)
    env.relay.readings = lambda: {"claude": reading("claude", 5.0, T0 + timedelta(days=1), stale_as_of)}
    assert not sup.available("claude")
    assert "claude" in sup.state["cooldowns"]

    # 4. missing reading leaves limits cooldown intact
    env.relay.readings = lambda: {}
    assert not sup.available("claude")
    assert "claude" in sup.state["cooldowns"]

    # 5. unparseable reading leaves limits cooldown intact
    env.relay.readings = lambda: {"claude": {"status": "error", "windows": "bad"}}
    assert not sup.available("claude")
    assert "claude" in sup.state["cooldowns"]

    # 6. exhausted reading (>= 99.5% used) leaves limits cooldown intact
    env.relay.readings = lambda: {"claude": reading("claude", 100.0, T0 + timedelta(hours=2), as_of)}
    assert not sup.available("claude")
    assert "claude" in sup.state["cooldowns"]


