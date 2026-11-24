"""Supervisor and host factory for relay_continuity (spec 265)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

if "relay_continuity_common" not in sys.modules:
    _SPEC = importlib.util.spec_from_file_location(
        "relay_continuity_common", Path(__file__).resolve().parent / "relay_continuity_common.py"
    )
    assert _SPEC and _SPEC.loader
    _MOD = importlib.util.module_from_spec(_SPEC)
    sys.modules["relay_continuity_common"] = _MOD
    _SPEC.loader.exec_module(_MOD)
from relay_continuity_common import (  # noqa: E402
    AGENTS,
    CI_POLL_INTERVAL_SECONDS,
    DEFAULT_CONTROL,
    HOLD_REASONS,
    JOB_POLL_INTERVAL_SECONDS,
    MAX_SESSION_RESUMES,
    MAX_TASK_CHECKPOINTS,
    MAX_UNSATISFIED_WAKES,
    MIN_CHECKPOINT_INTERVAL_SECONDS,
    NATIVE_AGENTS,
    PREFERRED,
    PREFERRED_AGENTS,
    RESUME_CONTEXT_CAP_TOKENS,
    SAME_AGENT_CAUSES,
    WAIT_TIMEOUT_SECONDS,
    Limits,
    UnmeasurableContextTokens,
    devctx,
    host_ops,
    iso,
    load_sibling,
    now_utc,
    parse_ts,
    relay_argon,
    relay_jobs,
    worker_routing,
)

load_sibling("relay_continuity_turns", "relay_continuity_turns.py")
from relay_continuity_turns import (  # noqa: E402
    ToolCall,
    TurnReport,
    classify_turn,
    external,
    gate,
    last_turn_dir,
)

load_sibling("relay_continuity_state", "relay_continuity_state.py")
from relay_continuity_state import (  # noqa: E402
    StateCorrupt,
    Store,
    default_state,
    redecidable,
    reopen_unverified,
    task_outcome,
    unsettled_nonces,
    waivers,
)

load_sibling("relay_continuity_notify", "relay_continuity_notify.py")
from relay_continuity_notify import (  # noqa: E402
    Notifier,
)

load_sibling("relay_continuity_adapters", "relay_continuity_adapters.py")
from relay_continuity_adapters import (  # noqa: E402
    CatalogRefused,
    RelayAdapter,
    fingerprint,
    live_conflicts,
    reap_descendants,
)

load_sibling("relay_continuity_probes", "relay_continuity_probes.py")
from relay_continuity_probes import (  # noqa: E402
    PARKED_TEXT,
    SETTLED,
    VERDICT_TEXT,
    Runner,
    check_task,
    default_pr_ci_status,
    evaluate_pr_ci,
    final_relay_status,
    final_text,
    job_record_status,
    parse_wait_directives,
    probe_call,
    turn_context_tokens,
)

# ---------------------------------------------------------------------------
# Supervisor

RECOVERY_PROMPT = """[relay-continuity attempt {nonce}]
Automatic continuation by the continuity supervisor. The previous coordinator turn ({prev_agent}) ended with outcome `{outcome}`; you are now running as {agent}.
This message is not a new maintainer instruction and grants no new authority: approval gates, review requirements and authority boundaries are exactly as before.
Before acting, run `python3 scripts/devctx.py recover` and verify fresh Git/GitHub state, then continue the interrupted work from where it actually stands.
Operations the interrupted turn completed (they already happened; verify their effect, do not repeat them blindly):
{effects}
{processes}
An operation listed as INTERRUPTED may have done part of its work before it was stopped. Inspect its effects (files, Git, GitHub, logs) before running it again, and repeat it only when that is safe. An operation listed as UNRESOLVED (parked) was checked by the supervisor and could not be settled. If an operation may already have had an external effect (a network request, a remote API call, a message) that you cannot verify and that must not happen twice, do not repeat it: record a `devctx.py note blocker` for that one item, park only the work that depends on it and continue other authorized work; end with RELAY_STATUS blocked only when no such work remains.
Commit and push in a separate short command, never chained with a long test run, well before the turn deadline.
If the work now needs a maintainer decision, stop and report instead of continuing."""

PROCESSES_STOPPED = ("Nothing the previous turn started is still running, except durable jobs registered with "
                     "`python3 scripts/relay_jobs.py start` (see `relay_jobs.py list`): the supervisor "
                     "stopped every other process it left behind. Restart any server or worker you need yourself.")
PROCESSES_UNKNOWN = ("The previous turn ran outside this supervisor's ownership (before a restart), so processes it "
                     "started may still be running: check them (`ps`, listening ports, worker lanes) before starting a "
                     "server or worker again, and never run a second copy beside a live one.")
MISSION_HEADER = "## Mission task"
MISSION_PROMPT = """[relay-continuity attempt {{nonce}}]
Supervised mission {mission}, task {number}/{total}, queued by the maintainer under directive {authority}.
The task text below is the maintainer's; it grants no authority beyond that directive. Approval gates, review requirements and authority boundaries are unchanged.
Before acting, run `python3 scripts/devctx.py recover` and verify fresh Git/GitHub state: earlier tasks of this mission, or an interrupted attempt at this one, may already have taken effect.
If the task needs a maintainer decision, stop and report instead of continuing.
Long bounded work (solver runs, studies, benchmarks) goes through `python3 scripts/relay_jobs.py start`, which survives this turn; `relay_jobs.py list` shows earlier jobs and their results. Before launching a scientific case, inspect registered jobs and existing result/partial evidence for the same commit and inputs; reuse them and never duplicate a live or completed case without a documented technical reason. A failed/lost job or an uncertain observation after namespace replacement returns to you for diagnosis, not automatically to the maintainer. Unknown/orphaned job records reserve their slot: inspect prior logs, results and partial evidence; do not rerun them or infer success/failure from missing PIDs. Repeating the same uncertain JOB-WAIT cannot trigger repeated diagnostic turns in the same namespace. To wait for your own job or PR checks, end the turn with a wait line, never a sleeping command: end with `RELAY_STATUS continue` and a `JOB-WAIT:` or `CI-WAIT:` line; the supervisor resumes you when it completes. Commit and push in a separate short command, never chained with a long test run, well before the turn deadline; checkpoint and end the turn, and a later turn collects. A running job blocks only the work that depends on its result: meanwhile continue with other authorized work that does not depend on it, on its own worktree and files, delegating per docs/PROCESS.md, and keep it light on the machine (focused tests only, full suites to CI, no extra solver beyond the cores the job left free). As a roadmap cycle, end blocked with `JOB-WAIT: <job id>` lines, which pause roadmap mode until those jobs finish, only when no such job-independent authorized work remains.
{contract}
End with RELAY_STATUS propose_done only when that evidence exists, RELAY_STATUS continue after a durable checkpoint (the task stays pending and is resumed, never recorded complete), or RELAY_STATUS blocked after recording a `devctx.py note blocker` or `question`.

""" + MISSION_HEADER + """
{text}"""


def describe_check(check: dict[str, Any] | None) -> str:
    """The completion contract as the provider is told it (spec 204). The owner, not the provider, evaluates it."""
    if check is None:
        return ("Completion contract: this task has no independent completion check, so the owner never records it "
                "complete on your report; it holds for a maintainer-attached contract.")
    what = {"host_receipt": lambda c: f"a signed ok host receipt exists for {c['op']} at {c.get('sha') or 'any commit'}",
            "pr_merged": lambda c: f"PR #{c['pr']} is merged on GitHub",
            "path_exists": lambda c: f"`{c['path']}` exists in the repository",
            "spec_merged": lambda c: f"spec {c['spec']} is `merged` in docs/specs/STATUS.md on GitHub master, or "
                                     "`in_review` there with every linked PR merged",
            "coordinator_cycle": lambda c: "the cycle ends with a final RELAY_STATUS while the remote heads are readable; "
                                           "the cycle is recorded settled, not as verified delivery of its work"}
    return f"Completion contract: the owner records this task complete only after its own read-only check that {what[check['kind']](check)}."


WORKER_ROUTING_BLOCK = """Worker routing
{summary}
Classify each delegable task by difficulty and risk, run `python3 scripts/worker_routing.py pick --difficulty D --risk R`, and delegate implementation to the returned worker. The launch line runs the worker as a durable registered job that outlives the coordinator turn, and a later turn collects it with `relay_jobs.py list`. Coordinators plan, review and integrate; do not implement delegable work yourself unless pick returns no_worker.
Risk is the harm if the worker is wrong: low docs/tests/UI copy; medium feature code covered by tests; high security, auth, egress, migrations/data loss, supervisor/relay/sandbox code, irreversible operations. Review the returned worker's diff as `pick` requires."""


def worker_routing_block(repo: Path | None = None) -> str:
    """The prompt's worker-routing block. The quota line is cached (<=15 min) and a failed read never blocks or
    fails a turn: it only reads `worker quota: unreadable`. With a repo, a successful reading is also persisted to
    its gitignored `.agent-relay/worker-quota.json` so a coordinator that cannot read quotas live can fall back."""
    try:
        summary = (worker_routing.cached_summary() if repo is None
                   else worker_routing.cached_summary(snapshot_file=repo / worker_routing.SNAPSHOT_NAME))
    except Exception:  # noqa: BLE001 - the turn must not depend on a quota reading
        summary = worker_routing.UNREADABLE
    return WORKER_ROUTING_BLOCK.format(summary=summary)


BRIEF_MAX_BYTES = 24_000
DIRECTIVES_MAX_BYTES = 24_000
BRIEF_HEADER = (
    "This is the operator's private mission brief; read it before the recovery packet; "
    "nothing from it may be copied into the repository, PRs, commits, issues or public notes; "
    "it grants no authority beyond the recorded directives."
)
DIRECTIVES_HEADER = "Standing maintainer directives for this mission (full text, oldest first):"


def session_context_block(
    control: Path,
    env: devctx.Env,
    authority: str | None,
    log: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Operator mission brief and standing directive text at session start (spec 264)."""
    brief_part: str | None = None
    brief_digest: str | None = None
    setting_path = control / "mission-brief"

    if setting_path.exists():
        try:
            raw_setting = setting_path.read_text(encoding="utf-8").strip()
        except OSError:
            raw_setting = None

        reason: str | None = None
        if raw_setting is None:
            reason = "unreadable"
        elif not Path(raw_setting).is_absolute():
            reason = "not an absolute path"
        else:
            brief_file = Path(raw_setting)
            if not brief_file.exists():
                reason = "missing"
            else:
                try:
                    if brief_file.stat().st_size > BRIEF_MAX_BYTES:
                        reason = "larger than 24000 bytes"
                    else:
                        raw_bytes = brief_file.read_bytes()
                        if len(raw_bytes) > BRIEF_MAX_BYTES:
                            reason = "larger than 24000 bytes"
                        else:
                            brief_digest = hashlib.sha256(raw_bytes).hexdigest()[:12]
                            brief_text = raw_bytes.decode(encoding="utf-8", errors="replace").strip()
                            brief_part = f"{BRIEF_HEADER}\n{brief_text}"
                except OSError:
                    reason = "unreadable"

        if reason is not None:
            brief_part = f"Operator mission brief not attached: {reason}"
            if log is not None:
                log(f"Operator mission brief not attached: {reason}")

    directives_block: str | None = None
    attached_directive_ids: list[str] = []

    if authority is not None:
        ledger = devctx.read_ledger(env)
        superseded = {ref for e in ledger for ref in e.get("supersedes", [])}
        auth_index = next((i for i, e in enumerate(ledger) if e.get("id") == authority), None)
        if auth_index is None:
            directives_block = f"Directive {authority} not attached: not found in ledger"
        else:
            candidates: list[dict[str, Any]] = []
            for i, e in enumerate(ledger[auth_index:], start=auth_index):
                if e.get("kind") != "directive":
                    continue
                d_id = e.get("id")
                if not d_id:
                    continue
                if i != auth_index and d_id in superseded:
                    continue
                d_info = e.get("directive") if isinstance(e.get("directive"), dict) else {}
                raw_path = d_info.get("path")
                expected_sha = d_info.get("sha256")
                path = Path(raw_path) if raw_path else None
                if path is None or not path.exists():
                    missing_msg = f"Directive {d_id} not attached: file missing"
                    candidates.append({
                        "id": d_id,
                        "text": missing_msg,
                        "attached": False,
                        "body_bytes": len(missing_msg.encode("utf-8")),
                        "index": i,
                    })
                else:
                    try:
                        content_bytes = path.read_bytes()
                    except OSError:
                        missing_msg = f"Directive {d_id} not attached: file missing"
                        candidates.append({
                            "id": d_id,
                            "text": missing_msg,
                            "attached": False,
                            "body_bytes": len(missing_msg.encode("utf-8")),
                            "index": i,
                        })
                    else:
                        actual_sha = hashlib.sha256(content_bytes).hexdigest()
                        if actual_sha != expected_sha:
                            tampered_msg = f"Directive {d_id} not attached: file modified after recording"
                            candidates.append({
                                "id": d_id,
                                "text": tampered_msg,
                                "attached": False,
                                "body_bytes": len(tampered_msg.encode("utf-8")),
                                "index": i,
                            })
                        else:
                            d_text = content_bytes.decode(encoding="utf-8", errors="replace").strip()
                            candidates.append({
                                "id": d_id,
                                "text": f"### Directive {d_id}\n{d_text}",
                                "attached": True,
                                "body_bytes": len(d_text.encode("utf-8")),
                                "index": i,
                            })

            if candidates:
                total_bytes = 0
                kept_rev: list[dict[str, Any]] = []
                cap_reached = False
                older_omitted_rev: list[str] = []
                oversize_omitted_rev: list[str] = []
                for c in reversed(candidates):
                    # Count directive body against the limit, excluding the heading (spec 264)
                    body_bytes = c["body_bytes"]
                    if body_bytes > DIRECTIVES_MAX_BYTES:
                        oversize_omitted_rev.append(c["id"])
                    elif not cap_reached and total_bytes + body_bytes <= DIRECTIVES_MAX_BYTES:
                        kept_rev.append(c)
                        total_bytes += body_bytes
                    else:
                        cap_reached = True
                        older_omitted_rev.append(c["id"])

                kept = list(reversed(kept_rev))
                older_omitted = list(reversed(older_omitted_rev))
                oversize_omitted = list(reversed(oversize_omitted_rev))

                lines = [DIRECTIVES_HEADER]
                if oversize_omitted:
                    if kept:
                        lines.append(f"Newer standing directives omitted for length: {', '.join(oversize_omitted)} (devctx.py show <id>)")
                    else:
                        lines.append(f"Standing directives omitted for length: {', '.join(oversize_omitted)} (devctx.py show <id>)")
                if older_omitted:
                    lines.append(f"Older standing directives omitted for length: {', '.join(older_omitted)} (devctx.py show <id>)")
                for c in kept:
                    lines.append(c["text"])
                directives_block = "\n\n".join(lines)
                attached_directive_ids = [c["id"] for c in kept if c["attached"]]

    parts = []
    if brief_part:
        parts.append(brief_part)
    if directives_block:
        parts.append(directives_block)

    text_block = "\n\n".join(parts)
    journal_fields = {
        "brief": brief_digest,
        "directives": attached_directive_ids,
    }
    return text_block, journal_fields


def _connect_endpoint(host: str, port: int, timeout: float, result: list[bool], done: threading.Event) -> None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            result[0] = True
    except Exception:  # noqa: BLE001
        pass
    finally:
        done.set()


def default_reachable(host: str = "github.com", port: int = 443, timeout: float = 5.0) -> bool:
    result = [False]
    done = threading.Event()
    bound = max(0.0, timeout)
    threading.Thread(target=_connect_endpoint, args=(host, port, max(0.01, bound), result, done), daemon=True).start()
    done.wait(timeout=bound)
    return result[0]


def format_offline_window(start: datetime | None, end: datetime, *, tz: Any = None) -> tuple[str, str]:
    """Format offline window as local/readable HH:MM (or YYYY-MM-DD HH:MM if spanning dates)."""
    end_aware = end if end.tzinfo else end.replace(tzinfo=UTC)
    if tz is None:
        try:
            tz = end.astimezone().tzinfo or UTC
        except Exception:  # noqa: BLE001
            tz = UTC
    elif isinstance(tz, str):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz)
        except Exception:  # noqa: BLE001
            pass
    try:
        end_local = end_aware.astimezone(tz)
    except Exception:  # noqa: BLE001
        end_local = end_aware
    if start is None:
        return "ora sconosciuta", end_local.strftime("%H:%M")
    start_aware = start if start.tzinfo else start.replace(tzinfo=UTC)
    try:
        start_local = start_aware.astimezone(tz)
    except Exception:  # noqa: BLE001
        start_local = start_aware
    if start_local.date() == end_local.date():
        return start_local.strftime("%H:%M"), end_local.strftime("%H:%M")
    return start_local.strftime("%Y-%m-%d %H:%M"), end_local.strftime("%Y-%m-%d %H:%M")


def _valid_percent(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and (isinstance(value, int) or math.isfinite(value))
            and 0 <= value <= 100)


def _safe_auth_detail(detail: str | None) -> str:
    text = (detail or "").strip()
    if text in ("output missing; runner reported an authentication failure",
                "output empty; runner reported an authentication failure",
                "output unparseable; runner reported an authentication failure"):
        return text
    if text.startswith("native preflight refused: "):
        reason = text.removeprefix("native preflight refused: ").strip()
        if reason in ("login_state_missing", "login_path_conflict", "agy_missing", "not_sandbox"):
            return text
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,40} status=(?:None|\d{1,3})", text):
        return text
    if re.fullmatch(r"codex [A-Za-z0-9_.-]{1,40}", text):
        return text
    if re.fullmatch(r"native [A-Za-z0-9_.-]{1,20} failed or empty result", text):
        return text
    return "classified auth failure"


@dataclass
class Host:
    relay: Any
    now: Callable[[], datetime] = now_utc
    sleep: Callable[[float], None] = time.sleep
    fingerprint: Callable[[], str] = lambda: ""
    conflicts: Callable[[set[int]], list[str]] = lambda own: []
    pid_alive: Callable[[int, int | None], bool] = lambda pid, start: devctx.pid_start(pid) == start and start is not None
    ledger: Callable[[str, str], Any] = lambda kind, text: None
    permissions_digest: Callable[[], str | None] = lambda: None
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr, flush=True)
    probe: Callable[[ToolCall], tuple[str, str] | None] = lambda call: None
    reap: Callable[[], list[str]] = lambda: []  # stops what a finished turn left running; returns process names
    task_check: Callable[[dict[str, Any]], str | None] = lambda check: None  # spec 201: None when the check passes
    worker_routing: Callable[[], str] | None = None  # text appended to every launched prompt
    session_context: Callable[[str | None], tuple[str, dict[str, Any]]] | None = None
    job_status: Callable[[str], dict[str, Any] | None] | None = None  # spec 267: None when unregistered
    pr_ci_status: Callable[[int, str], dict[str, Any] | None] | None = None  # spec 267: check results for (pr, head_sha)
    reachable: Callable[[], bool] = default_reachable
    read_ledger: Callable[[], list[dict[str, Any]]] | None = None


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    return parse_ts(value)


def usable_reading(reading: Any) -> bool:
    if not isinstance(reading, dict):
        return False
    windows = reading.get("windows")
    if not isinstance(windows, list) or not windows:
        return False
    for w in windows:
        if not isinstance(w, dict) or w.get("disabled"):
            return False
        used = w.get("used_percent")
        if not _valid_percent(used):
            return False
    return True


def free_percent(reading: Any) -> float | None:
    if not usable_reading(reading):
        return None
    return min(100.0 - float(w["used_percent"]) for w in reading["windows"])


class _DecisionSnapshot:
    """A per-decision snapshot of provider quota readings so availability, headroom,
    and coordinator ranking evaluate against a single consistent observation per agent."""

    def __init__(self, sup: Supervisor):
        self.sup = sup
        self._entries: dict[str, Any] = {}
        self._rooms: dict[str, float | None] = {}

    def get_entry(self, agent: str) -> Any:
        if agent not in self._entries:
            if agent in NATIVE_AGENTS:
                self._entries[agent] = self.sup.host.relay.argon_limits(agent)
            else:
                self._entries[agent] = self.sup._raw_reading(agent)
        return self._entries[agent]

    def free_room(self, agent: str) -> float | None:
        if agent not in self._rooms:
            entry = self.get_entry(agent)
            self._rooms[agent] = self.sup._calculate_free_room(agent, entry)
        return self._rooms[agent]


class Supervisor:
    def __init__(self, store: Store, host: Host, limits: Limits | None = None, *, turns: int = 1,
                 sender: Callable[[str], str | None] | None = None):
        self.store = store
        self.resume_turn: str | None = None
        self.resumed_hold: dict[str, Any] | None = None
        self.host = host
        self.limits = limits or Limits()
        self.turns = turns
        self.sender = sender
        self.state: dict[str, Any] = default_state()
        self.notifier = Notifier(self.state, sender=sender, log=host.log)
        self.signalled = False
        self.attempts = 0
        self.deciding: str | None = None
        self._reading: dict[str, Any] | None = None
        self._reading_at: datetime | None = None
        self._snapshot: _DecisionSnapshot | None = None
        self.mission: dict[str, Any] | None = None  # spec 201: the validated mission this store executes

    # -- persistence helpers
    def save(self) -> None:
        self.store.save(self.state, self.host.now())

    def notify(self, event: str, **fields: Any) -> None:
        if "session" in fields and fields["session"] is None:
            fields["session"] = "none"  # a mission before its first launch has no session yet
        try:
            self.notifier.send(event, self.host.now(), **fields)
        except Exception as exc:  # noqa: BLE001 - notification can never interrupt supervision
            self.host.log(f"notify: internal error {type(exc).__name__}")

    def record(self, kind: str, text: str) -> str | None:
        try:
            return self.host.ledger(kind, text)
        except Exception as exc:  # noqa: BLE001
            self.host.log(f"ledger: append failed {type(exc).__name__}")
            return None

    def _read_ledger(self) -> list[dict[str, Any]]:
        if getattr(self.host, "read_ledger", None) is not None:
            try:
                return self.host.read_ledger() or []
            except Exception:  # noqa: BLE001
                return []
        try:
            repo = getattr(getattr(self.host, "relay", None), "repo", None)
            if repo:
                return devctx.read_ledger(devctx.default_env(repo))
        except Exception:  # noqa: BLE001
            pass
        return []

    def stop_requested(self) -> bool:
        return self.signalled or self.store.stop_path.exists()

    def hold(self, reason: str, attempt: int | None = None, turn: str | None = None) -> str:
        """Refuse automatic continuation. `turn` names the turn whose evidence the maintainer must review; a
        release acknowledges exactly that turn and nothing later."""
        assert reason in HOLD_REASONS
        turn = turn or self.deciding  # holds raised while deciding a turn (waits, circuits) name that turn
        now = self.host.now()
        self.state["phase"] = "held"
        self.state["hold"] = {"reason": reason, "at": iso(now), "attempt": attempt, "turn": turn}
        self.save()
        self.store.journal({"event": "hold", "at": iso(now), "reason": reason, "session": self.state["head_session"]})
        previous = self.resumed_hold or {}
        if previous.get("reason") == reason and previous.get("turn") == turn:
            # Decided again with the same result: kept, not re-announced, and not decided again on every poll.
            self.state["hold"]["resumable"] = False
            self.save()
            self.host.log(f"hold: {reason} (unchanged when decided again; not re-announced)")
            return "held"
        event = "circuit_open" if reason.startswith("circuit_") else "hold"
        fields = {"session": self.state["head_session"], "reason": reason}
        if event == "hold":
            fields["attempt"] = str(attempt or 0)
        self.notify(event, **fields)
        if (rec := self.state.get("mission")) is not None and self.mission is not None:
            self.notify("mission_held", owner="control-plane", mission=self.mission["id"],
                        task=f"{min(rec['next'] + 1, rec['total'])}/{rec['total']}",
                        agent=self.state.get("agent") or PREFERRED, session=self.state["head_session"], reason=reason)
        self.record("blocker", f"relay-continuity HOLD ({reason}) on session {self.state['head_session']}; "
                               f"automatic continuation refused until `relay_continuity.py release`.")
        self.host.log(f"hold: {reason}")
        return "held"

    # -- provider availability
    def _raw_reading(self, agent: str) -> dict[str, Any] | None:
        now = self.host.now()
        if self._reading is None or self._reading_at is None or now - self._reading_at > timedelta(minutes=2):
            profile = self.mission.get("coordinator_profile", "standard") if self.mission else "standard"
            self._reading = (self.host.relay.limits(coordinator_profile=profile) if profile != "standard"
                             else self.host.relay.limits()) or {}
            self._reading_at = now
        entry = self._reading.get(agent)
        if not isinstance(entry, dict) or entry.get("status") not in (None, "ok"):
            return None
        as_of = parse_ts(entry.get("as_of"))
        if as_of is None or abs(now - as_of) > self.limits.reading_freshness:
            return None
        return entry

    def reading(self, agent: str) -> dict[str, Any] | None:
        """A validated limits reading for `agent` if one was fetched recently, else fetch a fresh one."""
        if self._snapshot is not None and agent not in NATIVE_AGENTS:
            return self._snapshot.get_entry(agent)
        return self._raw_reading(agent)

    def exhausted_until(self, agent: str) -> datetime | None | bool:
        """Validated reset time when a fresh reading shows the agent out of quota; False when it shows room."""
        entry = self.reading(agent)
        if entry is None:
            return None
        now = self.host.now()
        resets = []
        windows = entry.get("windows")
        if not isinstance(windows, list) or not windows:
            return None  # no usage windows: no evidence either way
        for window in windows:
            used = window.get("used_percent") if isinstance(window, dict) else None
            if not _valid_percent(used):
                return None  # an unusable window is not evidence of room
            reset = parse_ts(window.get("resets_at"))
            if used >= 99.5:
                if reset and now < reset <= now + self.limits.max_reset_ahead:
                    resets.append(reset)
                else:
                    return None  # exhausted but the reset time is stale or implausible: not evidence
        return max(resets) if resets else False

    def available(self, agent: str) -> bool:
        """Whether `agent` may be tried now. Its own runs are the authority: a cooldown recorded from a real
        failure ends at its reset, and a fresh, plausible limits reading that shows it out of quota defers it to
        that reset. A missing, stale or failed reading is no evidence either way, so the agent is tried and its
        run decides; a quota failure then records the cooldown that keeps retries bounded."""
        now = self.host.now()
        cooldown = self.state["cooldowns"].get(agent)
        if cooldown:
            source = cooldown.get("source")
            if source == "auth":
                qid = cooldown.get("question")
                cleared = False
                if qid:
                    for entry in reversed(self._read_ledger()):
                        if entry.get("kind") == "decision" and entry.get("status") == "accepted" and qid in (entry.get("supersedes") or []):
                            text = str(entry.get("text") or "")
                            if ": " in text:
                                _, ans = text.split(": ", 1)
                                if ans.strip().startswith("1"):
                                    cleared = True
                            break
                if cleared:
                    self.state["cooldowns"].pop(agent, None)
                    cooldown = None
                else:
                    return False
            else:
                until = parse_ts(cooldown.get("until"))
                if (until is not None and now < until) or (until is None and source != "manual"):
                    if source in ("limits", "message"):
                        counters = self.state["counters"]
                        if counters.get(f"reading_clear_{agent}", 0) == 0:
                            since = parse_ts(cooldown.get("since"))
                            if since is not None:
                                if agent in NATIVE_AGENTS:
                                    native = self._snapshot.get_entry(agent) if self._snapshot is not None else self.host.relay.argon_limits(agent)
                                    if isinstance(native, dict):
                                        as_of = parse_ts(native.get("as_of"))
                                        windows = native.get("windows")
                                        plausible = (isinstance(windows, list) and len(windows) > 0 and
                                                     all(isinstance(w, dict) and
                                                         _valid_percent(w.get("used_percent")) for w in windows))
                                        fresh = as_of is not None and abs(now - as_of) <= self.limits.reading_freshness
                                        room = plausible and all(not w.get("disabled") and w["used_percent"] < 100 for w in windows)
                                        if fresh and as_of > since and room:
                                            self.state["cooldowns"].pop(agent, None)
                                            counters[f"reading_clear_{agent}"] = 1
                                            self.record("finding", f"relay-continuity: cleared {source} cooldown for {agent} via fresh limits reading")
                                            return True
                                else:
                                    entry = self.reading(agent)
                                    if entry is not None and (as_of := parse_ts(entry.get("as_of"))) is not None and as_of <= since:
                                        self._reading = None
                                        self._reading_at = None
                                        if self._snapshot is not None:
                                            self._snapshot._entries.pop(agent, None)
                                            self._snapshot._rooms.pop(agent, None)
                                        entry = self.reading(agent)
                                    if entry is not None:
                                        as_of = parse_ts(entry.get("as_of"))
                                        room = (self.exhausted_until(agent) is False)
                                        if as_of is not None and as_of > since and room:
                                            self.state["cooldowns"].pop(agent, None)
                                            counters[f"reading_clear_{agent}"] = 1
                                            self.record("finding", f"relay-continuity: cleared {source} cooldown for {agent} via fresh limits reading")
                                            return True
                    return False
                if until is None:
                    return False
        if agent in NATIVE_AGENTS:
            native = self._snapshot.get_entry(agent) if self._snapshot is not None else self.host.relay.argon_limits(agent)
            if native is None:
                return False
            windows = native["windows"]
            blocked = [w for w in windows if w.get("disabled") or w["used_percent"] >= 100]
            if blocked:
                resets = [parse_ts(w.get("resets_at")) for w in blocked]
                known = [t for t in resets if t is not None and now < t <= now + self.limits.max_reset_ahead]
                self.state["cooldowns"][agent] = {"until": iso(max(known)) if len(known) == len(blocked) else None,
                                                  "source": "limits", "reason": "quota", "since": iso(now)}
                return False
            if cooldown:
                self.state["cooldowns"].pop(agent, None)
            return True
        reading = self.exhausted_until(agent)
        if isinstance(reading, datetime):
            self.state["cooldowns"][agent] = {"until": iso(reading), "source": "limits", "reason": "quota",
                                              "since": iso(now)}
            return False
        if cooldown:
            self.state["cooldowns"].pop(agent, None)
        return True

    def _calculate_free_room(self, agent: str, entry: Any) -> float | None:
        native = agent in NATIVE_AGENTS
        if not isinstance(entry, dict) or not isinstance(entry.get("windows"), list) or not entry["windows"]:
            return None
        now = self.host.now()
        used_values = []
        for window in entry["windows"]:
            if not isinstance(window, dict):
                return None
            used = window.get("used_percent")
            if not _valid_percent(used):
                return None
            if not native:
                reset = parse_ts(window.get("resets_at"))
                if reset is None or not now < reset <= now + self.limits.max_reset_ahead:
                    return None
            used_values.append(used)
        return min(100 - used for used in used_values)

    def free_room(self, agent: str) -> float | None:
        """The smallest free room (100 - used_percent) over windows of a usable reading, or None."""
        if self._snapshot is not None:
            return self._snapshot.free_room(agent)
        native = agent in NATIVE_AGENTS
        entry = self.host.relay.argon_limits(agent) if native else self.reading(agent)
        return self._calculate_free_room(agent, entry)

    def low_headroom(self, agent: str) -> bool:
        """Whether a usable quota reading shows less than the minimum free room in any window."""
        room = self.free_room(agent)
        return room is not None and room < self.limits.min_free_percent

    def set_cooldown(self, agent: str, report: TurnReport) -> None:
        now = self.host.now()
        existing = self.state["cooldowns"].get(agent)
        if isinstance(existing, dict) and existing.get("until") is None:
            return  # a manual cap stays until `release --clear-cooldown`; no reclassification shortens it
        if isinstance(existing, dict) and report.turn_id and existing.get("turn") == report.turn_id:
            return  # this turn's cooldown is already recorded: a restart re-deciding it neither extends nor backs off
        is_network_like = (report.outcome in ("transient", "unknown", "timeout") or report.timed_out)
        if self.state.get("network_outage", False) and is_network_like:
            return
        if is_network_like:
            reachable_fn = getattr(self.host, "reachable", None) or default_reachable
            try:
                is_reachable = bool(reachable_fn())
            except Exception:  # noqa: BLE001
                is_reachable = False
            if not is_reachable:
                return
        caps = self.state["spend_caps"]
        earlier = caps.get(agent)
        if report.spend_cap and report.outcome == "quota" and isinstance(earlier, dict) \
                and earlier.get("turn") != report.turn_id:
            # A second turn hit the spend cap after the session reset it printed (no success since): a real cap.
            report.outcome = "spend_limit"
        if report.spend_cap:
            caps[agent] = {"turn": report.turn_id, "at": iso(now)}
        counters = self.state["counters"]
        if report.outcome == "auth":
            until, source = None, "auth"
            existing = self.state["cooldowns"].get(agent)
            if isinstance(existing, dict) and existing.get("source") == "auth":
                qid = existing.get("question")
            else:
                safe_detail = _safe_auth_detail(report.detail)
                q_text = (
                    f"Autenticazione fallita per l'agente {agent} ({safe_detail}). Come procedere?\n"
                    f"1. (consigliata) Accesso ripristinato per {agent}, riprova — conseguenza: riprova l'agente al prossimo turno\n"
                    f"2. Mantieni {agent} escluso — conseguenza: l'agente resta escluso fino al comando release"
                )
                self.state["cooldowns"][agent] = {
                    "until": None, "source": "auth", "reason": report.outcome,
                    "since": iso(now), "turn": report.turn_id, "question": None,
                }
                self.save()
                qid = self.record("question", q_text)
            self.state["cooldowns"][agent] = {
                "until": None, "source": "auth", "reason": report.outcome,
                "since": iso(now), "turn": report.turn_id, "question": qid,
            }
            self.save()
            self.notify("provider_exhausted", dedupe_key=f"exhausted|{agent}|auth", agent=agent,
                        outcome=report.outcome, reset_at="manual", reset_source=source)
            return
        elif report.outcome == "spend_limit":
            until, source = None, "manual"
        elif report.outcome == "transient":
            # An outage: back off this agent briefly (the other one may still be reachable).
            strikes = counters.get(f"transient_{agent}", 0)
            counters[f"transient_{agent}"] = strikes + 1
            until = now + timedelta(seconds=min(self.limits.transient_base * 2 ** strikes, self.limits.transient_cap))
            source = "estimate"
        else:
            reading = self.exhausted_until(agent) if report.outcome == "quota" else None
            message = parse_ts(report.reset_at)
            if isinstance(reading, datetime):
                until, source = reading, "limits"
            elif message and now < message <= now + self.limits.max_reset_ahead:
                until, source = message, "message"
            else:
                # No reliable reset (or a login problem): retry periodically, backing off while the agent keeps
                # failing (reset on success).
                strikes = counters.get(f"estimated_{agent}", 0)
                counters[f"estimated_{agent}"] = strikes + 1
                until, source = now + min(self.limits.recheck * 2 ** strikes, self.limits.recheck_cap), "estimate"
        self.state["cooldowns"][agent] = {"until": iso(until), "source": source, "reason": report.outcome,
                                          "since": iso(now), "turn": report.turn_id}
        if report.outcome == "transient":
            self.host.log(f"{agent} transient failure; retry after {iso(until)}")
            return
        self.notify("provider_exhausted", dedupe_key=f"exhausted|{agent}|{iso(until)}", agent=agent,
                    outcome=report.outcome, reset_at=iso(until) or "manual", reset_source=source)
        self.record("finding", f"relay-continuity: {agent} {report.outcome}; cooldown until "
                               f"{iso(until) or 'maintainer release'} (source {source}).")

    # -- main loop
    def run(self, session: str, *, task: str | None = None) -> str:
        now = self.host.now()
        try:
            self.state = self.store.load()
        except StateCorrupt:
            self.host.log("state file corrupt; refusing to act (hold). Inspect and move it aside to reset.")
            Notifier(default_state(), sender=self.sender, log=self.host.log).send(
                "hold", now, session=session, reason="state_corrupt", attempt="0")
            return "held"
        self.notifier = Notifier(self.state, sender=self.sender, log=self.host.log)
        if self.state["phase"] == "held":
            hold = self.state.get("hold") or {}
            if not (redecidable(self.state) and session == self.state.get("head_session")):
                self.host.log(f"chain is held ({self.state['hold']}); run `release` after review")
                return "held"
            # A hold the current policy may not raise is decided again, for that exact turn only. Every other
            # hold still needs `release`.
            self.resume_turn = hold["turn"]
            self.resumed_hold = hold
            self.store.journal({"event": "hold_redecided", "at": iso(now), "hold": hold})
            self.host.log(f"deciding held turn {hold['turn']} again under the current policy")
            self.state["hold"] = None
        head = self.state.get("head_session")
        if self.state.get("pending") and head and head != session:
            self.host.log(f"a launched attempt from chain {head} is unresolved; resume that chain first")
            return "held"
        if head != session and session not in self.state.get("chain", []):
            kept_clears = {k: v for k, v in (self.state.get("counters") or {}).items() if k.startswith("reading_clear_")}
            self.state.update({"head_session": session, "chain": [session], "agent": self.state.get("agent") if head is None else None, "switches": [],
                               "last_switch_at": None, "waiting_since": None, "last_attempt": None,
                               "counters": {"consecutive_failures": 0, "transient_streak": 0, "crash_retries": 0, **kept_clears}})
        if isinstance(legacy := self.state.pop("reconcile", None), dict) and isinstance(legacy.get("diag"), dict):
            # A read-only diagnosis of the earlier controller (it could change nothing): abandoned, never adopted.
            self.store.journal({"event": "settle", "purpose": "diagnostic", "outcome": "abandoned",
                                "nonce": legacy["diag"].get("nonce"), "session": None})
        own = {(self.state.get("pending") or {}).get("nonce")}
        if (unsettled := [n for n in unsettled_nonces(self.store.journal_path) if n not in own]):
            # The journal records a launch that the state does not account for: never assume it did not run.
            self.host.log(f"journal has unsettled attempts not in state: {', '.join(unsettled)}")
            return self.hold("attempt_unresolved")
        digest = self.host.permissions_digest()
        if self.state.get("permissions_pinned") and digest != self.state["permissions_digest"]:
            return self.hold("permissions_changed")
        self.state["permissions_digest"] = digest
        self.state["permissions_pinned"] = True
        self.state["phase"] = "waiting" if self.state.get("wait") else "running"
        self.save()
        self.notify("supervisor_started", dedupe_key=f"start|{self.state['head_session']}|{iso(now)[:13]}",
                    session=self.state["head_session"], agent=self.state.get("agent") or PREFERRED)
        self.notifier.retry_pending(now)
        try:
            result = self._loop(task)
        finally:
            if self.state["phase"] == "running":
                self.state["phase"] = "stopped"
            self.save()
        reason = {"held": "held", "idle": "idle", "waiting": "waiting", "stopped": "stop_requested",
                  "signal": "signal"}.get(result, "error")
        self.notify("supervisor_stopped", dedupe_key=f"stop|{self.state['head_session']}|{result}|{iso(self.host.now())[:13]}",
                    session=self.state["head_session"], reason=reason, phase=self.state["phase"])
        return result

    def _loop(self, task: str | None) -> str:
        report = self._resolve_pending()
        acknowledged: dict[str, Any] | None = None
        if report == "held":
            return "held"
        if report is None and self.mission is not None and self._mission_task() is None:
            # Every task is recorded (`release --advance` on the last one): no settled attempt is left to decide,
            # so the mission completes instead of relaunching a task that does not exist.
            return self._mission_complete()
        if report is None and self.mission is not None and not self.state.get("head_session"):
            # A new mission has no turn yet: its first task opens a new Relay session.
            report = TurnReport(outcome="success", agent=None, detail="mission start", terminal=True,
                                turn_id=f"{self.mission['id']}/start")
        if report is None:
            turn = last_turn_dir(self.host.relay.sessions_dir() / self.state["head_session"])
            if turn is None:
                self.host.log("head session has no turns")
                return self.hold("attempt_unresolved")
            report = classify_turn(turn)
            report.agent = report.agent or self.state.get("agent")
            ack = self.state.get("acknowledged") or {}
            if ack.get("turn") and ack.get("turn") == report.turn_id:
                acknowledged = ack
        if self.resume_turn and report.turn_id != self.resume_turn:
            self.host.log("the chain moved since the held turn; keeping the hold (not decided again)")
            self.state["phase"] = "held"
            self.state["hold"] = {**(self.resumed_hold or {}), "resumable": False}
            self.save()
            return "held"
        if self.state.get("wait"):
            self.state["phase"] = "waiting"
            wait = self.state["wait"]
            session_id = self.state.get("head_session")
            turn = last_turn_dir(self.host.relay.sessions_dir() / session_id) if session_id else None
            report = classify_turn(turn) if turn else TurnReport(outcome="success", agent=self.state.get("agent"), detail="restored wait")
            report.agent = report.agent or self.state.get("agent")
            if wait.get("woken"):
                agent = wait.get("wake_agent") or self.state.get("agent") or PREFERRED
                prompt = wait.get("wake_prompt", "")
                if not self.available(agent):
                    chosen = self._choose(agent)
                    if chosen is None:
                        wait_res = self._wait_or_hold(lambda a: self._make_wake_prompt(a, wait))
                        if isinstance(wait_res, str):
                            return wait_res
                        agent, prompt = wait_res
                    else:
                        agent = chosen
                        prompt = self._make_wake_prompt(agent, wait)
                    wait["wake_agent"] = agent
                    wait["wake_prompt"] = prompt
                    self.save()
                new_report = self._launch(agent, prompt, report)
                if isinstance(new_report, str):
                    return new_report
                report = new_report
            else:
                wait_res = self._poll_external_wait(wait, report)
                if isinstance(wait_res, str):
                    return wait_res
                agent, prompt = wait_res
                new_report = self._launch(agent, prompt, report)
                if isinstance(new_report, str):
                    return new_report
                report = new_report
        while True:
            self.state["last_outcome"] = report.outcome
            self.state["agent"] = report.agent or self.state.get("agent") or PREFERRED
            self.deciding = report.turn_id
            decision = self._decide(report, task, waive=waivers(acknowledged), cleared=tuple((acknowledged or {}).get("cleared") or ()))
            acknowledged = None
            if isinstance(decision, str):
                return decision
            if self.stop_requested():
                self.state["phase"] = "stopped"
                return "signal" if self.signalled else "stopped"
            agent, prompt = decision
            task = None
            new_report = self._launch(agent, prompt, report)
            if isinstance(new_report, str):
                return new_report
            report = new_report

    def _park(self, report: TurnReport, parked: list[ToolCall]) -> None:
        if not parked or not report.turn_id:
            return
        parked_turns = self.state.setdefault("parked_turns", [])
        if report.turn_id in parked_turns:
            return
        parked_turns.append(report.turn_id)
        del parked_turns[:-100]  # dedupe only needs recent turns
        labels = ", ".join(op.label for op in parked)
        self.record("finding", f"relay-continuity: parked interrupted external operation(s) of turn "
                               f"{report.turn_id} that no probe could settle: {labels}; "
                               "they may have taken effect and are never repeated.")
        self.notify("effects_parked", dedupe_key=f"parked|{report.turn_id}",
                    session=self.state["head_session"],
                    agent=report.agent or self.state.get("agent") or PREFERRED,
                    count=str(len(parked)))

    def _decide(self, report: TurnReport, task: str | None, *, waive: frozenset[str] = frozenset(),
                cleared: tuple[str, ...] = ()) -> tuple[str, str] | str:
        """Next (agent, prompt) or a terminal result. `waive` holds what a maintainer release of this turn
        reviewed ("gate": its side-effect evidence). `cleared`: agents whose cooldown the release cleared."""
        released = bool(waive or cleared)
        counters = self.state["counters"]
        failed_agent = report.agent or self.state.get("agent") or PREFERRED
        if report.outcome == "success":
            # A successful turn can still have left a detached job running, now stopped: an external one is gated
            # like any interrupted operation, before anything is advanced or completed.
            parked: list[ToolCall] = []
            if "gate" not in waive:
                if (reason := gate(report)) is not None:
                    return self.hold(reason, None, report.turn_id)
                _verdicts, parked = self._settle_external(report)
                if parked:
                    self._park(report, parked)
            counters.update({"consecutive_failures": 0, "transient_streak": 0, "crash_retries": 0})
            counters.pop(f"estimated_{failed_agent}", None)
            counters.pop(f"transient_{failed_agent}", None)
            counters.pop(f"reading_clear_{failed_agent}", None)
            self.state["spend_caps"].pop(failed_agent, None)
            self.state["waiting_since"] = None
            if task is None and self.mission is not None:
                self._reopen_legacy()
                self._reverify_released()
                task = self._mission_task()
                if task is None:
                    return self._mission_complete()

            session_id = self.state.get("head_session")
            turn = last_turn_dir(self.host.relay.sessions_dir() / session_id) if session_id else None
            reply = final_text(turn) if turn else ""
            status = final_relay_status(self.host.relay.repo, session_id)
            jobs, ci_waits = parse_wait_directives(reply)

            if status == "continue" and (jobs or ci_waits):
                if "gate" not in waive and parked:
                    return self.hold("effects_uncertain", None, report.turn_id)
                wait = {
                    "jobs": jobs,
                    "ci": [{"pr": pr, "sha": sha} for pr, sha in ci_waits],
                    "started_at": iso(self.host.now()),
                    "last_job_poll": None,
                    "last_ci_poll": None,
                    "turn": report.turn_id,
                }
                self.state["wait"] = wait
                self.state["phase"] = "waiting"
                self.save()
                return self._poll_external_wait(wait, report)
            if task is None:
                self.state["phase"] = "idle"
                self.save()
                self.notify("turn_completed", dedupe_key=f"done|{self.state['head_session']}",
                            session=self.state["head_session"], agent=failed_agent)
                return "idle"
            prompt = f"[relay-continuity attempt {{nonce}}]\n{task}"
            if report.reaped:
                prompt += ("\n\nThe supervisor stopped processes the previous turn left running ("
                           + ", ".join(report.reaped) + "); restart any you still need.")
            elif report.reaped is None and report.in_flight:
                prompt += "\n\n" + PROCESSES_UNKNOWN
            if parked:
                prompt += "\n\n" + "\n".join(f"- [{c.kind}] {c.label}: {PARKED_TEXT}" for c in parked)
            agent = self._choose(failed_agent)
            return (agent, prompt) if agent is not None else self._wait_or_hold(lambda _agent: prompt)

        is_network_like = (report.outcome in ("transient", "unknown", "timeout") or report.timed_out)
        if is_network_like:
            reachable_fn = getattr(self.host, "reachable", None) or default_reachable
            try:
                is_reachable = bool(reachable_fn())
            except Exception:  # noqa: BLE001
                is_reachable = False

            if not is_reachable:
                if not self.state.get("network_outage", False):
                    self.state["network_outage"] = True
                    self.record("finding", "relay-continuity: host offline mid-turn; waiting for network")
                if not self.state.get("network_outage_since"):
                    self.state["network_outage_since"] = iso(self.host.now())
                self.host.log(f"turn ended with {report.outcome} while host offline; waiting without cooldown or strikes")
                self.state["phase"] = "waiting"
                self.save()
                return "waiting"

        from_outage = bool(self.state.get("network_outage", False)) and is_network_like

        if not from_outage and counters["consecutive_failures"] >= self.limits.max_consecutive_failures:
            return self.hold("circuit_failures")
        settled: dict[str, dict[str, str]] = {}
        parked: list[ToolCall] = []
        if "gate" not in waive:
            reason = gate(report)
            if reason is not None:
                if counters["consecutive_failures"] >= 2:
                    self.notify("turn_failed", session=self.state["head_session"], agent=failed_agent,
                                outcome=report.outcome, action="hold")
                return self.hold(reason, None, report.turn_id)
            settled, parked = self._settle_external(report)
            if parked:
                self._park(report, parked)
        # A network or auth blip is infrastructure, not the model: retry the SAME agent after a short backoff instead
        # of switching to another provider (maintainer directive L20261109091909-a4bb). Only a bounded run of them
        # (a real outage) falls through to the normal cooldown, which may switch.
        retry_same = (not from_outage and report.outcome == "transient" and report.harness_cause in SAME_AGENT_CAUSES
                      and failed_agent not in cleared
                      and counters.get(f"transient_{failed_agent}", 0) < self.limits.same_agent_retries)
        if not from_outage and not retry_same and report.outcome in ("quota", "spend_limit", "transient", "auth") and failed_agent not in cleared:
            # A provider failure makes only that provider unavailable, until its reset or a backed-off retry.
            self.set_cooldown(failed_agent, report)

        parked_ids = {c.id for c in parked}
        listed = [f"- [{c.kind}] {c.label}" for c in report.effects]
        for c in report.in_flight:
            verdict = settled.get(c.id)
            if verdict:
                status_text = f"{VERDICT_TEXT[verdict['verdict']]} ({verdict['evidence']})"
            elif c.id in parked_ids:
                status_text = PARKED_TEXT
            else:
                status_text = "INTERRUPTED"
            listed.append(f"- [{c.kind}] {c.label}: {status_text}")
        effects = "\n".join(listed) or "- none recorded"
        if report.reaped:
            effects += "\nProcesses the turn left running, stopped by the supervisor: " + ", ".join(report.reaped)
        if released:
            effects += "\n(The maintainer reviewed and released this turn.)"

        processes = (PROCESSES_STOPPED if report.reaped is not None else PROCESSES_UNKNOWN)

        def make_prompt(chosen: str) -> str:
            if released and task is not None:
                return f"[relay-continuity attempt {{nonce}}]\n{task}"
            return RECOVERY_PROMPT.format(nonce="{nonce}", prev_agent=failed_agent, outcome=report.outcome,
                                          agent=chosen, effects=effects, processes=processes)

        if retry_same:
            return failed_agent, make_prompt(failed_agent)
        agent = self._choose(failed_agent)
        if agent is None:
            return self._wait_or_hold(make_prompt)
        if counters["consecutive_failures"] >= 2:
            self.notify("turn_failed", session=self.state["head_session"], agent=failed_agent, outcome=report.outcome,
                        action="switch" if agent != failed_agent else "retry")
        return agent, make_prompt(agent)

    def _settle_external(self, report: TurnReport) -> tuple[dict[str, dict[str, str]], list[ToolCall]]:
        """The side-effect gate. The turn's processes are gone, so an operation in flight was interrupted, not
        running. A local one is left to the next turn, which inspects and redoes it. An external one is settled by
        a deterministic read-only probe (a push, a merge, a PR creation); otherwise it is parked, because repeating it
        could not be taken back. Returns (verdicts, parked)."""
        verdicts: dict[str, dict[str, str]] = {}
        uncertain = []
        for op in report.in_flight:
            if not external(op):
                continue
            try:
                probed = self.host.probe(op)
            except Exception as exc:  # noqa: BLE001 - a failed probe settles nothing
                self.host.log(f"probe failed for {op.label}: {type(exc).__name__}")
                probed = None
            if probed and probed[0] in SETTLED:
                verdicts[op.id] = {"verdict": probed[0], "evidence": str(probed[1])[:240]}
            else:
                uncertain.append(op)
        if uncertain or verdicts:
            self.store.journal({"event": "reconcile", "at": iso(self.host.now()), "turn": report.turn_id,
                                "result": "effects_uncertain" if uncertain else "settled",
                                "verdicts": [{"id": op.id, "kind": op.kind, "label": op.label,
                                              **verdicts.get(op.id, {"verdict": "unresolved"})}
                                             for op in report.in_flight if external(op)]})
        if uncertain:
            self.host.log("interrupted external operation(s) no probe can settle: "
                          + ", ".join(op.label for op in uncertain))
        if verdicts:
            self.record("decision", f"relay-continuity: settled interrupted external operation(s) of turn "
                                    f"{report.turn_id} by probe: " + "; ".join(
                                        f"{op.label} = {verdicts[op.id]['verdict']}" for op in report.in_flight
                                        if op.id in verdicts))
        return verdicts, uncertain

    def is_hydrogen_spend_down(self, now: datetime | None = None) -> bool:
        if now is None:
            now = self.host.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        if not self.available("hydrogen"):
            return False
        reading = None
        if hasattr(self.limits, "read") and callable(self.limits.read):
            try:
                reading = self.limits.read("hydrogen", now=now)
            except TypeError:
                reading = self.limits.read("hydrogen")
        if reading is None:
            if self._snapshot is not None:
                reading = self._snapshot.get_entry("hydrogen")
            else:
                reading = self.host.relay.argon_limits("hydrogen")
        if not usable_reading(reading):
            return False
        fp = free_percent(reading)
        if fp is None or fp < self.limits.min_free_percent:
            return False
        for w in reading.get("windows", []):
            if not isinstance(w, dict) or w.get("id") != "gemini-weekly":
                continue
            if w.get("disabled"):
                return False
            resets_at = _parse_iso_timestamp(w.get("resets_at"))
            if resets_at is None:
                return False
            if resets_at.tzinfo is None:
                resets_at = resets_at.replace(tzinfo=UTC)
            diff = resets_at - now
            if not (timedelta(0) < diff <= worker_routing.SPEND_DOWN):
                return False
            used = float(w.get("used_percent", 0.0))
            return 100.0 - used >= self.limits.min_free_percent
        return False

    hydrogen_spend_down = is_hydrogen_spend_down
    _hydrogen_spend_down = is_hydrogen_spend_down

    def _choose(self, current: str) -> str | None:
        """Prefer available coordinators with quota room, retaining the first available if all are low."""
        prev = self._snapshot
        self._snapshot = _DecisionSnapshot(self)
        try:
            first_available = None
            spend_down_applied = False
            if current in PREFERRED_AGENTS and self.available(current):
                first_available = current
                room = self.free_room(current)
                if room is None or room >= self.limits.min_free_percent:
                    agent = current
                else:
                    agent = None
            else:
                agent = None

            if agent is None:
                spend_down = self.is_hydrogen_spend_down()
                remaining = [a for a in PREFERRED_AGENTS if a != current] if current in PREFERRED_AGENTS else list(PREFERRED_AGENTS)
                avail = {a: self.available(a) for a in remaining}
                rooms = {a: self.free_room(a) for a in remaining}
                sorted_remaining = sorted(
                    remaining,
                    key=lambda a: ((0, -rooms[a]) if rooms[a] is not None else (1, 0.0)),
                )
                if spend_down:
                    candidates = ["hydrogen", *sorted_remaining, "codex"]
                else:
                    candidates = [*sorted_remaining, "codex", "hydrogen"]

                for candidate in candidates:
                    is_avail = avail[candidate] if candidate in avail else self.available(candidate)
                    if is_avail:
                        first_available = first_available or candidate
                        room = rooms[candidate] if candidate in rooms else self.free_room(candidate)
                        if room is not None and room < self.limits.min_free_percent:
                            continue
                        agent = candidate
                        if spend_down and candidate == "hydrogen":
                            spend_down_applied = True
                        break
                else:
                    agent = first_available
            if agent is not None:
                if agent != current:
                    now = self.host.now()
                    self.state["switches"] = [t for t in self.state["switches"]
                                              if (ts := parse_ts(t)) and now - ts < timedelta(days=1)] + [iso(now)]
                    self.state["last_switch_at"] = iso(now)
                    self.notify("provider_switched", session=self.state["head_session"], from_agent=current,
                                agent=agent, outcome=self.state.get("last_outcome") or "success")
                    spend_tag = " (gemini_spend_down)" if (agent == "hydrogen" and spend_down_applied) else ""
                    self.record("decision", f"relay-continuity: switched coordinator {current} -> {agent}{spend_tag} "
                                            f"after {self.state.get('last_outcome')} on {self.state['head_session']}.")
                return agent
            return None
        finally:
            self._snapshot = prev

    def _wait_or_hold(self, make_prompt: Callable[[str], str]) -> tuple[str, str] | str:
        """No agent can run now: sleep until the earliest cooldown ends (at most one recheck interval at a time),
        then take whichever agent is usable. Only caps with no reset (spend limits) hold, and so does a wait
        longer than `max_wait`."""
        current = self.state.get("agent") or PREFERRED
        while True:
            now = self.host.now()
            known = [u for a, c in self.state["cooldowns"].items() if a in AGENTS and (u := parse_ts(c.get("until")))]
            if not known:
                return self.hold("no_provider_available")
            since = parse_ts(self.state.get("waiting_since")) or now
            self.state["waiting_since"] = iso(since)
            if now - since > self.limits.max_wait:
                return self.hold("wait_exceeded")
            future = [u for u in known if u > now]
            wake = min(min(future), now + self.limits.recheck) if future else now + self.limits.recheck
            self.state["phase"] = "waiting"
            self.save()
            self.notify("all_exhausted", dedupe_key=f"all|{iso(min(known))}", next_check_at=iso(wake))
            if (rec := self.state.get("mission")) is not None and self.mission is not None:
                self.notify("mission_waiting", dedupe_key=f"wait|{self.mission['id']}|{iso(wake)}",
                            owner="control-plane", mission=self.mission["id"],
                            task=f"{rec['next'] + 1}/{rec['total']}", agent=current,
                            reason="no_provider_available", next_check_at=iso(wake))
            if self._sleep((wake - now).total_seconds()):
                return "signal" if self.signalled else "stopped"
            self.state["phase"] = "running"
            self._reading = None
            if (agent := self._choose(current)) is not None:
                self.state["waiting_since"] = None
                return agent, make_prompt(agent)

    def _get_job(self, job_id: str) -> dict[str, Any] | None:
        if self.host.job_status is not None:
            return self.host.job_status(job_id)
        job = relay_jobs.load(job_id)
        if job is not None:
            return job
        alt_path = Path.home() / "jarvis-control" / "work" / "jobs" / f"{job_id}.json"
        if alt_path.exists():
            try:
                return json.loads(alt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return None

    def _get_ci(self, pr_number: int, expected_head: str) -> dict[str, Any]:
        if self.host.pr_ci_status is not None:
            res = self.host.pr_ci_status(pr_number, expected_head)
            if res is not None:
                return res
            return {"unreadable": True}
        return default_pr_ci_status(self.host.relay.repo, pr_number, expected_head)

    def _log_resume_decision(self, site: str, tokens: int | None, resume: bool) -> None:
        if tokens is None:
            tokens_desc = "unmeasured"
        elif isinstance(tokens, UnmeasurableContextTokens):
            tokens_desc = "unmeasurable"
        else:
            tokens_desc = f"{tokens} tokens"
        decision = "resume" if resume else "fresh"
        self.host.log(f"resume decision ({site}): last-call context {tokens_desc}; {decision}")

    def _make_wake_prompt(self, chosen: str, wait: dict[str, Any]) -> str:
        reason = wait.get("wake_reason") or "satisfied"
        items_text = wait.get("wake_items")
        outcome_header = ("External wait satisfied:" if reason == "satisfied"
                          else f"External wait terminated ({reason}):")
        if items_text is None:
            wake_prompt = wait.get("wake_prompt", "")
            if outcome_header in wake_prompt:
                part = wake_prompt.split(outcome_header, 1)[1]
                part = part.split("Resume work", 1)[0].split("[Durable handoff", 1)[0].strip()
                items_text = part
            elif "[Durable handoff summary]" in wake_prompt:
                part = wake_prompt.split("[Durable handoff summary]", 1)[1].strip()
                items_text = part
            else:
                lines = [f"- JOB {j}: {reason}" for j in wait.get("jobs", [])]
                lines += [f"- CI PR #{item['pr']}@{item['sha']}: {reason}" for item in wait.get("ci", [])]
                items_text = "\n".join(lines)

        rec = self.state.get("mission")
        if rec and self.mission:
            mission_line = f"Resume work on mission {self.mission['id']} task {rec['next'] + 1}/{rec['total']}."
        else:
            mission_line = "Resume work."

        short_resume_prompt = (
            f"[relay-continuity resume {{nonce}}]\n"
            f"{outcome_header}\n"
            f"{items_text}\n"
            f"{mission_line}"
        )

        parent_candidate = (None if chosen in NATIVE_AGENTS or self.state.get("agent") in NATIVE_AGENTS
                            or (chosen == "codex" and self.state.get("agent") not in (None, "codex"))
                            else self.state.get("head_session"))

        resumes = self.state.get("resumes", 0)
        turn_dir = last_turn_dir(self.host.relay.sessions_dir() / self.state["head_session"]) if self.state.get("head_session") else None
        tokens = turn_context_tokens(turn_dir)

        if (parent_candidate is not None
                and resumes < MAX_SESSION_RESUMES
                and (tokens is None or tokens <= RESUME_CONTEXT_CAP_TOKENS)):
            self.state["resumes"] = resumes + 1
            self._log_resume_decision("wake", tokens, resume=True)
            return short_resume_prompt
        else:
            self.state["resumes"] = 0
            self.state["force_fresh_context"] = True
            self._log_resume_decision("wake", tokens, resume=False)
            task_text = self._mission_task() if (self.mission and rec) else None
            handoff = f"{outcome_header}\n{items_text}"
            if task_text:
                full_prompt = f"{task_text}\n\n[Durable handoff summary]\n{handoff}"
            else:
                full_prompt = f"[relay-continuity attempt {{nonce}}]\n[Durable handoff summary]\n{handoff}"
            return full_prompt

    def _wake_wait(self, reason: str, wait: dict[str, Any], report: TurnReport,
                   job_statuses: dict[str, str], ci_statuses: dict[str, str],
                   failed_job: str | None = None, failed_pr: str | None = None) -> tuple[str, str] | str:
        self.state["phase"] = "running"
        rec = self.state.get("mission")
        if rec and self.mission:
            task_idx = str(rec["next"])
            if reason == "satisfied":
                rec.setdefault("unsatisfied_wakes", {}).pop(task_idx, None)
            elif not wait.get("counted"):
                # Counted once per wait: a pass that waits for an agent or is stopped before launching re-uses it.
                counts = rec.setdefault("unsatisfied_wakes", {})
                current_wakes = counts.get(task_idx, 0)
                if current_wakes >= MAX_UNSATISFIED_WAKES:
                    return self.hold("task_wait_limit", None, report.turn_id)
                counts[task_idx] = current_wakes + 1
                wait["counted"] = True

        lines = []
        for j in wait.get("jobs", []):
            st = job_statuses.get(j)
            if j == failed_job:
                lines.append(f"- JOB {j}: {reason} (uncertain)")
            elif st in relay_jobs.TERMINAL:
                lines.append(f"- JOB {j}: {st}")
            elif reason == "satisfied":
                lines.append(f"- JOB {j}: {st or 'succeeded'}")
            elif reason in ("wait_timeout", "remote_unreadable"):
                lines.append(f"- JOB {j}: {reason} (uncertain)")
            else:
                lines.append(f"- JOB {j}: uncertain")

        for item in wait.get("ci", []):
            pr_key = f"{item['pr']}@{item['sha']}"
            st = ci_statuses.get(pr_key)
            if pr_key == failed_pr:
                if reason == "check_failed":
                    lines.append(f"- CI PR #{item['pr']}@{item['sha']}: failure")
                else:
                    lines.append(f"- CI PR #{item['pr']}@{item['sha']}: {reason} (uncertain)")
            elif st == "success" or reason == "satisfied":
                lines.append(f"- CI PR #{item['pr']}@{item['sha']}: success")
            elif reason in ("wait_timeout", "remote_unreadable"):
                lines.append(f"- CI PR #{item['pr']}@{item['sha']}: {reason} (uncertain)")
            else:
                lines.append(f"- CI PR #{item['pr']}@{item['sha']}: uncertain")

        items_text = "\n".join(lines)
        wait["woken"] = True
        wait["wake_reason"] = reason
        wait["wake_items"] = items_text

        failed_agent = report.agent or self.state.get("agent") or PREFERRED
        agent = self._choose(failed_agent)
        if agent is None:
            chosen = self._wait_or_hold(lambda a: self._make_wake_prompt(a, wait))
            if isinstance(chosen, str):
                return chosen
            agent, prompt = chosen
        else:
            prompt = self._make_wake_prompt(agent, wait)

        wait["wake_agent"] = agent
        wait["wake_prompt"] = prompt
        self.save()
        return agent, prompt

    def _poll_external_wait(self, wait: dict[str, Any], report: TurnReport) -> tuple[str, str] | str:
        wait_started_at = parse_ts(wait.get("started_at")) or self.host.now()
        jobs = wait.get("jobs", [])
        ci_items = wait.get("ci", [])
        job_statuses = dict(wait.get("job_statuses", {}))
        ci_statuses = dict(wait.get("ci_statuses", {}))
        last_job_poll = parse_ts(wait.get("last_job_poll"))
        last_ci_poll = parse_ts(wait.get("last_ci_poll"))

        while True:
            if self.stop_requested():
                self.state["phase"] = "stopped"
                self.save()
                return "signal" if self.signalled else "stopped"

            now = self.host.now()
            elapsed = (now - wait_started_at).total_seconds()
            if elapsed >= WAIT_TIMEOUT_SECONDS:
                unreadable_all = wait.get("unreadable_streak", False)
                reason = "remote_unreadable" if unreadable_all else "wait_timeout"
                return self._wake_wait(reason, wait, report, job_statuses, ci_statuses)

            job_poll_needed = bool(jobs and any(job_statuses.get(j) not in relay_jobs.TERMINAL for j in jobs))
            if job_poll_needed and (last_job_poll is None or (now - last_job_poll).total_seconds() >= JOB_POLL_INTERVAL_SECONDS):
                last_job_poll = now
                wait["last_job_poll"] = iso(now)
                for job_id in jobs:
                    if job_statuses.get(job_id) in relay_jobs.TERMINAL:
                        continue
                    job = self._get_job(job_id)
                    if job is None:
                        return self._wake_wait("unregistered_job", wait, report, job_statuses, ci_statuses, failed_job=job_id)
                    st = job_record_status(job)
                    job_statuses[job_id] = st

            ci_poll_needed = bool(ci_items and any(ci_statuses.get(f"{item['pr']}@{item['sha']}") != "success" for item in ci_items))
            if ci_poll_needed and (last_ci_poll is None or (now - last_ci_poll).total_seconds() >= CI_POLL_INTERVAL_SECONDS):
                last_ci_poll = now
                wait["last_ci_poll"] = iso(now)
                unreadable_this_round = False
                for item in ci_items:
                    pr_key = f"{item['pr']}@{item['sha']}"
                    if ci_statuses.get(pr_key) == "success":
                        continue
                    ci_info = self._get_ci(item["pr"], item["sha"])
                    st_type, reason = evaluate_pr_ci(ci_info, item["sha"], elapsed)
                    if st_type == "immediate_wake":
                        return self._wake_wait(reason, wait, report, job_statuses, ci_statuses, failed_pr=pr_key)
                    elif st_type == "unreadable":
                        unreadable_this_round = True
                    elif st_type == "completed":
                        ci_statuses[pr_key] = "success"
                if unreadable_this_round:
                    wait["unreadable_streak"] = True
                else:
                    wait.pop("unreadable_streak", None)

            wait["job_statuses"] = job_statuses
            wait["ci_statuses"] = ci_statuses
            self.save()

            all_jobs_done = all(job_statuses.get(j) in relay_jobs.TERMINAL for j in jobs)
            all_ci_done = all(ci_statuses.get(f"{item['pr']}@{item['sha']}") == "success" for item in ci_items)
            if all_jobs_done and all_ci_done:
                return self._wake_wait("satisfied", wait, report, job_statuses, ci_statuses)

            now = self.host.now()
            sleeps = []
            if any(job_statuses.get(j) not in relay_jobs.TERMINAL for j in jobs):
                if last_job_poll:
                    rem_job = JOB_POLL_INTERVAL_SECONDS - (now - last_job_poll).total_seconds()
                    sleeps.append(max(0.1, rem_job))
                else:
                    sleeps.append(JOB_POLL_INTERVAL_SECONDS)
            if any(ci_statuses.get(f"{item['pr']}@{item['sha']}") != "success" for item in ci_items):
                if last_ci_poll:
                    rem_ci = CI_POLL_INTERVAL_SECONDS - (now - last_ci_poll).total_seconds()
                    sleeps.append(max(0.1, rem_ci))
                else:
                    sleeps.append(CI_POLL_INTERVAL_SECONDS)
            rem_timeout = WAIT_TIMEOUT_SECONDS - (now - wait_started_at).total_seconds()
            sleeps.append(max(0.1, rem_timeout))
            if ci_items and (now - wait_started_at).total_seconds() < 120.0:
                rem_120 = 120.0 - (now - wait_started_at).total_seconds()
                sleeps.append(max(0.1, rem_120))

            sleep_sec = min(sleeps)
            self.state["phase"] = "waiting"
            self.save()
            if self._sleep(sleep_sec):
                self.state["phase"] = "stopped"
                self.save()
                return "signal" if self.signalled else "stopped"

    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep in short chunks against the host clock; True when a stop was requested."""
        deadline = self.host.now() + timedelta(seconds=seconds)
        while True:
            if self.stop_requested():
                return True
            remaining = (deadline - self.host.now()).total_seconds()
            if remaining <= 0:
                return False
            self.host.sleep(min(5.0, remaining))

    def _pre_launch_probe(self) -> str | None:
        """Probe reachability and coordinator conflicts before launching. None if clear to proceed."""
        if conflicts := self.host.conflicts({os.getpid()}):
            self.host.log(f"another coordinator is active ({', '.join(conflicts)}); waiting")
            self.state["phase"] = "waiting"
            self.save()
            return "waiting"
        reachable_fn = getattr(self.host, "reachable", None) or default_reachable
        try:
            reachable = bool(reachable_fn())
        except Exception:  # noqa: BLE001
            reachable = False
        if not reachable:
            if not self.state.get("network_outage", False):
                self.state["network_outage"] = True
                self.state["network_outage_since"] = iso(self.host.now())
                self.record("finding", "relay-continuity: pre-launch probe failed (github.com:443 unreachable); waiting for network")
            self.host.log("pre-launch network probe failed (github.com:443 unreachable); waiting")
            self.state["phase"] = "waiting"
            self.save()
            return "waiting"
        if self.state.get("network_outage", False):
            ended_at = self.host.now()
            start_iso = self.state.get("network_outage_since")
            start_dt = parse_ts(start_iso)
            start_str, end_str = format_offline_window(start_dt, ended_at)
            notice_text = f"Ero offline da {start_str} a {end_str}, riprendo."
            self.state["network_outage"] = False
            self.state["network_outage_since"] = None
            try:
                self.notifier.send_text(
                    f"offline_resume|{start_iso or 'unknown'}|{iso(ended_at)}",
                    notice_text,
                    ended_at,
                )
            except Exception as exc:  # noqa: BLE001
                self.host.log(f"notify: offline resume error {type(exc).__name__}")
            self.save()
            self.host.log(f"network restored ({notice_text})")
        else:
            self.state["network_outage"] = False
        return None

    def _launch(self, agent: str, prompt: str, previous: TurnReport) -> TurnReport | str:
        if self.mission is not None and self._mission_task() is None:
            return self._mission_complete()  # every task is recorded: never launch past the last one
        # Only this process is excluded: a Relay REPL that is our ancestor is still a live coordinator.
        if (probe_res := self._pre_launch_probe()) is not None:
            return probe_res
        digest = self.host.permissions_digest()
        if digest != self.state.get("permissions_digest"):
            return self.hold("permissions_changed")
        if (previous is not None and previous.agent == agent
                and previous.outcome == "transient"
                and previous.harness_cause in SAME_AGENT_CAUSES):
            strikes = self.state["counters"].get(f"transient_{agent}", 0)
            backoff = min(self.limits.transient_base * 2 ** strikes, self.limits.transient_cap)
            self.host.log(f"{agent} {previous.harness_cause}; retrying the same agent in {int(backoff)}s")
            self.save()
            if self._sleep(backoff):
                return "signal" if self.signalled else "stopped"
        now = self.host.now()
        self.attempts += 1
        nonce = f"rc-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
        text = prompt.replace("{nonce}", nonce)
        wall_limit = (
            getattr(self.limits, "native_turn_wall", Limits.native_turn_wall)
            if agent in NATIVE_AGENTS
            else getattr(self.limits, "turn_wall", Limits.turn_wall)
        )
        if agent not in NATIVE_AGENTS:
            limit_min = int(wall_limit // 60)
            wall_sentence = f"Your turn has a hard limit of {limit_min} minutes."
            if f"hard limit of {limit_min} minutes" not in text:
                attempt_hdr = f"[relay-continuity attempt {nonce}]"
                resume_hdr = f"[relay-continuity resume {nonce}]"
                if attempt_hdr in text:
                    text = text.replace(attempt_hdr, f"{attempt_hdr}\n{wall_sentence}", 1)
                elif resume_hdr in text:
                    text = text.replace(resume_hdr, f"{resume_hdr}\n{wall_sentence}", 1)
                else:
                    text = f"{text}\n\n{wall_sentence}"
        # A takeover starts a new native provider context; the task and durable chain carry recovery.
        # A Claude -> Codex takeover must start a new native Codex context. Relay's --continue otherwise
        # revives its prior thread, which reached 1.5-2.7M input tokens in the measured roadmap cycles.
        # The recovery prompt and mission task carry the handoff; the supervisor keeps its own exact chain.
        # A native agent retried after its own unsuccessful turn resumes that agy conversation (its full context and
        # scratch artifacts, which the retained stream does not carry); a new task or another agent starts fresh.
        resume = None
        resume_model = None
        if agent in NATIVE_AGENTS and previous is not None and previous.agent == agent \
                and previous.outcome != "success" and previous.turn_id:
            try:
                session_id, _, turn_name = previous.turn_id.partition("/")  # "<session>/<turn>"
                candidate = relay_argon.conversation_of(self.host.relay.sessions_dir() / session_id / "turns" / turn_name)
                if candidate is not None:
                    # Resume only what prepare_resume will accept; otherwise this turn needs the session context.
                    size = (self.host.relay.conversation_store() / f"{candidate}.db").stat().st_size
                    if size > relay_argon.RESUME_MAX_BYTES:
                        self.host.log(f"native conversation {candidate} oversize ({size} bytes); starting fresh context")
                    else:
                        resume = candidate
                        if relay_argon.account_models.enabled(dict(os.environ)):
                            try:
                                prior = json.loads((self.host.relay.sessions_dir() / session_id / "turns" /
                                                    turn_name / "native-result.json").read_text(encoding="utf-8"))
                                if (isinstance(prior, dict) and prior.get("catalog_verified") is True
                                        and isinstance(prior.get("model"), str) and prior["model"].strip()):
                                    resume_model = prior["model"]
                            except (OSError, ValueError, AttributeError):
                                resume_model = None
            except OSError:
                self.host.log("native conversation store missing; starting fresh context")
            except Exception:  # noqa: BLE001 - resuming is an optimisation; the turn launches fresh without it
                resume = None
        force_fresh = self.state.pop("force_fresh_context", False)
        turn_dir = last_turn_dir(self.host.relay.sessions_dir() / self.state["head_session"]) if self.state.get("head_session") else None
        tokens = turn_context_tokens(turn_dir)
        parent = (None if agent in NATIVE_AGENTS or self.state.get("agent") in NATIVE_AGENTS
                  or (agent == "codex" and self.state.get("agent") not in (None, "codex"))
                  or force_fresh
                  or (tokens is not None and tokens > RESUME_CONTEXT_CAP_TOKENS)
                  else self.state["head_session"])
        if parent is None:
            self.state["resumes"] = 0
        if self.state.get("head_session"):
            self._log_resume_decision("launch", tokens, resume=(parent is not None))
        fresh = resume is None and (parent is None or self.state.get("agent") != agent)
        pending = {"nonce": nonce, "agent": agent, "parent": parent, "started_at": iso(now),
                   "pid": None, "pid_start": None, "fingerprint": self.host.fingerprint(), "attempt": self.attempts,
                   "coordinator_profile": self.mission.get("coordinator_profile", "standard") if self.mission else "standard"}
        if (rec := self.state.get("mission")) is not None and self.mission is not None:
            # Every launch until the task completes (recoveries and failovers included) serves that task.
            pending["task"] = rec["next"]
            if fresh and text.startswith(f"[relay-continuity resume {nonce}]"):
                text = text.replace(f"[relay-continuity resume {nonce}]", f"[relay-continuity attempt {nonce}]", 1)
            if MISSION_HEADER not in text and (not text.startswith("[relay-continuity resume ") or fresh):
                task = self.mission["tasks"][rec["next"]]["text"]
                text += (f"\n\nThe interrupted work is mission {self.mission['id']} task {rec['next'] + 1}/{rec['total']}, "
                         f"which has not been recorded complete:\n{MISSION_HEADER}\n{task}")
        session_journal: dict[str, Any] = {}
        if self.host.session_context is not None:
            session_journal = {"brief": None, "directives": []}
            if fresh:
                authority = self.mission["authority"] if self.mission is not None else None
                try:
                    ctx_text, fields = self.host.session_context(authority)
                    if ctx_text:
                        text += "\n\n" + ctx_text
                    if isinstance(fields, dict):
                        session_journal["brief"] = fields.get("brief")
                        session_journal["directives"] = fields.get("directives", [])
                except Exception as exc:  # noqa: BLE001 - session context is optional; the turn still launches
                    self.host.log(f"session context skipped: {type(exc).__name__}")
        if self.host.worker_routing is not None:
            try:
                text += "\n\n" + self.host.worker_routing()
            except Exception as exc:  # noqa: BLE001 - routing advice is optional; the turn still launches
                self.host.log(f"worker routing block skipped: {type(exc).__name__}")
        if (previous is not None and previous.agent == agent
                and previous.outcome == "transient"
                and previous.harness_cause in SAME_AGENT_CAUSES):
            pending["transient_retry"] = agent
        prior_wait = self.state.get("wait")
        self.state["wait"] = None
        self.state["pending"] = pending  # intent is durable before anything is launched
        self.state["acknowledged"] = None  # the reviewed turn is now behind the chain
        self.state["agent"] = agent
        self.save()
        launch_journal = {"event": "launch", **{k: v for k, v in pending.items() if k != "fingerprint"},
                          **({"resume": resume} if resume else {})}
        if self.host.session_context is not None:
            launch_journal.update(session_journal)
        self.store.journal(launch_journal)
        try:
            launch_options = {"resume": resume} if resume else {}
            if pending["coordinator_profile"] != "standard":
                launch_options["coordinator_profile"] = pending["coordinator_profile"]
            if resume and relay_argon.account_models.enabled(dict(os.environ)):
                launch_options["resume_model"] = resume_model
            proc = self.host.relay.start(agent, parent, text, self.turns, **launch_options)
        except CatalogRefused as exc:
            # Preflight rejected this account before a turn existed. Settle the intent and
            # let the normal provider cooldown/selection path try another eligible agent.
            self.state["pending"] = None
            if prior_wait is not None:
                self.state["wait"] = prior_wait
            self.store.journal({"event": "settle", "nonce": nonce, "session": None, "outcome": "not_started"})
            self.save()
            return TurnReport(outcome="quota", agent=agent, detail=str(exc), terminal=True)
        except OSError:
            # Relay could not be executed at all: nothing started, so the attempt settles as not started.
            self.state["pending"] = None
            if prior_wait is not None:
                self.state["wait"] = prior_wait
            self.store.journal({"event": "settle", "nonce": nonce, "session": None, "outcome": "not_started"})
            return self.hold("relay_invocation_failed", self.attempts)
        pending["pid"] = getattr(proc, "pid", None)
        pending["pid_start"] = devctx.pid_start(pending["pid"]) if pending["pid"] else None
        if (retry_agent := pending.get("transient_retry")) and not pending.get("strike_counted"):
            strikes = self.state["counters"].get(f"transient_{retry_agent}", 0)
            self.state["counters"][f"transient_{retry_agent}"] = strikes + 1
            pending["strike_counted"] = True
        self.save()
        try:
            try:
                result = self.host.relay.finish(proc, timeout=wall_limit)
            except TypeError:
                result = self.host.relay.finish(proc)
            timed_out = bool(getattr(result, "timed_out", False))
            if timed_out:
                pending["timed_out"] = True
                if result.diagnostics:
                    pending["diagnostics"] = result.diagnostics
                self.save()
            reaped = self._reap()
        finally:
            self.host.relay.release_turn(proc)
        if reaped is None:
            # Stopped while a process of the turn still survived: the attempt stays pending, and a restart adopts it.
            return "signal" if self.signalled else "stopped"
        return self._settle(pending, result.session_id, result.diagnostics, reaped, timed_out=timed_out)

    def _reap(self) -> list[str] | None:
        """Stop whatever the finished turn left running, so nothing of it overlaps the next turn. While a process
        survives, nothing is launched: retry every 30 s. None when a stop request ends that wait."""
        names: set[str] = set()
        while True:
            try:
                names.update(self.host.reap())
                break
            except Exception as exc:  # noqa: BLE001 - a failed cleanup is never taken as a clean one
                self.host.log(f"processes of the finished turn survive ({type(exc).__name__}: {exc}); "
                              "waiting before anything is launched")
                self.state["phase"] = "waiting"
                self.save()
                if self._sleep(30.0):
                    return None
                self.state["phase"] = "running"
        if names:
            self.host.log(f"stopped processes the turn left running: {', '.join(sorted(names))}")
        return sorted(names)

    def _settle(self, pending: dict[str, Any], session_id: str | None, diagnostics: str = "",
                reaped: list[str] | None = None, timed_out: bool = False) -> TurnReport | str:
        session_id = session_id or self.host.relay.find_by_nonce(pending["nonce"], parse_ts(pending["started_at"]) or self.host.now())
        self.state["last_attempt"] = {k: pending.get(k, "") for k in ("nonce", "agent", "attempt", "fingerprint")}
        if "task" in pending:
            self.state["last_attempt"]["task"] = pending["task"]
        self.state["pending"] = None
        if (wait := self.state.get("wait")) and wait.get("woken"):
            self.state["wait"] = None
        if session_id is None:
            # Remote effects (a merge, a PR comment) leave no local trace, so an unchanged fingerprint proves
            # nothing: the attempt's outcome is unknown and only the maintainer can resolve it. The hold is
            # saved together with the cleared pending record, and only then is the attempt journalled settled.
            result = self.hold("attempt_unresolved", pending.get("attempt"))
            self.store.journal({"event": "settle", "nonce": pending["nonce"], "session": None, "outcome": "unresolved"})
            return result
        turn = last_turn_dir(self.host.relay.sessions_dir() / session_id)
        report = classify_turn(turn, diagnostics=diagnostics, timed_out=timed_out) if turn else TurnReport(
            outcome="unknown" if (timed_out and pending.get("agent") in NATIVE_AGENTS) else "crash",
            agent=pending["agent"],
            detail=f"session without turns ({diagnostics.strip()})" if diagnostics else "session without turns",
            no_evidence=True, turn_id=f"{session_id}/none",
            harness_cause="turn_time_limit" if (timed_out and pending.get("agent") in NATIVE_AGENTS) else None,
            timed_out=bool(timed_out and pending.get("agent") in NATIVE_AGENTS))
        if timed_out:
            if pending.get("agent") in NATIVE_AGENTS or report.agent in NATIVE_AGENTS:
                report.outcome = "unknown"
                report.harness_cause = "turn_time_limit"
                report.timed_out = True
            elif report.outcome in ("success", "quota", "spend_limit", "auth", "transient"):
                report.outcome = "crash"
            if diagnostics and diagnostics.strip() not in report.detail:
                report.detail = f"{report.detail}; {diagnostics.strip()}" if report.detail else diagnostics.strip()
        report.agent = pending["agent"]
        report.reaped = reaped
        retry_agent = pending.get("transient_retry")
        if retry_agent is None and "transient_retry" not in pending:
            head_sid = self.state.get("head_session")
            if head_sid and (prev_turn := last_turn_dir(self.host.relay.sessions_dir() / head_sid)):
                try:
                    prev_report = classify_turn(prev_turn)
                    if (prev_report.agent == pending.get("agent")
                            and prev_report.outcome == "transient"
                            and prev_report.harness_cause in SAME_AGENT_CAUSES
                            and pending.get("pid") is None):
                        retry_agent = pending.get("agent")
                except Exception:  # noqa: BLE001 - advisory turn classification for legacy pending states
                    pass
        self.state["head_session"] = session_id
        self.state["chain"] = (self.state.get("chain") or []) + [session_id]
        # Counted once per launched attempt. Provider failures are bounded by their cooldowns instead.
        counters = self.state["counters"]
        is_offline = False
        if report.outcome in ("transient", "unknown", "timeout") or report.timed_out:
            reachable_fn = getattr(self.host, "reachable", None) or default_reachable
            try:
                is_offline = not bool(reachable_fn())
            except Exception:  # noqa: BLE001
                is_offline = True
        if retry_agent and not pending.get("strike_counted") and not is_offline:
            counters[f"transient_{retry_agent}"] = counters.get(f"transient_{retry_agent}", 0) + 1
            pending["strike_counted"] = True
        # Only an unbroken run of crash/unknown turns counts: any other outcome ends the streak.
        # A turn cut by our own time limit still counts here: this is the loop guard (eight unbroken one-hour cuts
        # is a real problem), not a capability verdict; `harness_cause` in the journal keeps the two apart.
        if not is_offline:
            counters["consecutive_failures"] = (counters["consecutive_failures"] + 1
                                                if report.outcome in ("crash", "unknown") else 0)
        coord_tokens = (report.usage["total_tokens"]
                        if isinstance(report.usage, dict)
                        and isinstance(report.usage.get("total_tokens"), int)
                        and not isinstance(report.usage.get("total_tokens"), bool)
                        else None)
        self.store.journal({"event": "settle", "nonce": pending["nonce"], "session": session_id,
                            "at": iso(self.host.now()),
                            "outcome": report.outcome, "harness_cause": report.harness_cause,
                            "agent": report.agent, "reaped": report.reaped,
                            "effects": [c.label for c in report.effects], "in_flight": [c.label for c in report.in_flight],
                            "usage": report.usage, "coordinator_tokens": coord_tokens})
        # Keep the launched intent durable until mission progress is saved. If this process dies while
        # checking a successful task, restart adopts the turn instead of launching that task again.
        if (held := self._advance(pending, report, session_id)) is not None:
            return held
        self.save()
        return report

    # -- missions (spec 201)
    def _mission_task(self) -> str | None:
        """The prompt of the mission's next incomplete task, or None when every task is recorded complete."""
        assert self.mission is not None
        rec = self.state["mission"]
        if rec["next"] >= rec["total"]:
            return None
        return MISSION_PROMPT.format(mission=self.mission["id"], number=rec["next"] + 1, total=rec["total"],
                                     authority=self.mission["authority"], text=self.mission["tasks"][rec["next"]]["text"],
                                     contract=describe_check(self._task_check(rec["next"])))

    def _task_check(self, index: int) -> dict[str, Any] | None:
        """The task's completion contract: its immutable definition's, else one attached to a legacy task (spec 204)."""
        assert self.mission is not None
        return self.mission["tasks"][index].get("check") or (self.state["mission"].get("contracts") or {}).get(str(index))

    def _settles(self, index: int, status: str | None) -> bool:
        """Whether a final RELAY_STATUS claims the task finished: propose_done; a roadmap cycle also settles blocked."""
        cycle = (self._task_check(index) or {}).get("kind") == "coordinator_cycle"
        return status == "propose_done" or (cycle and status == "blocked")

    def _advance(self, pending: dict[str, Any], report: TurnReport, session_id: str) -> str | None:
        """Spec 204: a successful provider turn is execution, not delivery. Its final RELAY_STATUS only routes it:
        `blocked` holds for the maintainer, and a claimed finish advances only after the task's independent
        completion check passes. Anything else (`continue`, a missing or unknown status, a claim the check does not
        confirm) is a durable checkpoint: the task stays pending and is resumed, a bounded number of times."""
        rec = self.state.get("mission")
        if self.mission is None or rec is None or report.outcome != "success" or pending.get("task") != rec["next"]:
            return None
        index = rec["next"]
        status = final_relay_status(self.host.relay.repo, session_id)
        event = {"at": iso(self.host.now()), "mission": self.mission["id"], "task": index, "session": session_id}
        if status == "blocked" and not self._settles(index, status):
            self.store.journal({"event": "task_blocked", **event})
            return self.hold("task_blocked", pending.get("attempt"), report.turn_id)
        if self._settles(index, status):
            if self._task_check(index) is None:
                # Nothing can confirm the claim until the maintainer attaches a completion contract (spec 204).
                self.store.journal({"event": "task_check_failed", **event, "problem": "no completion check"})
                return self.hold("task_check_failed", pending.get("attempt"), report.turn_id)
            if not (problem := self._verify(index)):
                self._record_done(index, session_id, report.agent, status)
                return None
            self.store.journal({"event": "task_check_failed", **event, "problem": problem[:240]})
        elif status != "continue":
            self.store.journal({"event": "task_report_unverified", **event, "reported_status": str(status)[:40]})

        turn = last_turn_dir(self.host.relay.sessions_dir() / session_id) if session_id else None
        reply = final_text(turn) if turn else ""
        jobs, ci_waits = parse_wait_directives(reply)
        is_external_wait = status == "continue" and bool(jobs or ci_waits)

        if not is_external_wait:
            rec.setdefault("unsatisfied_wakes", {}).pop(str(index), None)
            counts = rec.setdefault("checkpoints", {})
            counts[str(index)] = counts.get(str(index), 0) + 1
            self.store.journal({"event": "task_checkpointed", **event, "checkpoints": counts[str(index)]})
            if counts[str(index)] >= MAX_TASK_CHECKPOINTS:
                return self.hold("task_checkpoint_limit", pending.get("attempt"), report.turn_id)

            last_cp = parse_ts(self.state.get("last_checkpoint_at") or rec.get("last_checkpoint_at") or pending.get("started_at"))
            now = self.host.now()
            if last_cp is not None:
                elapsed = (now - last_cp).total_seconds()
                if elapsed < MIN_CHECKPOINT_INTERVAL_SECONDS and self._sleep(MIN_CHECKPOINT_INTERVAL_SECONDS - elapsed):
                    return "signal" if self.signalled else "stopped"
            now_iso = iso(self.host.now())
            rec["last_checkpoint_at"] = now_iso
            self.state["last_checkpoint_at"] = now_iso
        else:
            self.store.journal({"event": "task_wait_suspended", **event, "jobs": jobs, "ci": [f"{pr}@{sha}" for pr, sha in ci_waits]})

        return None

    def _verify(self, index: int) -> str | None:
        """The problem that keeps the task's completion contract unsatisfied, or None. Unreadable evidence fails."""
        check = self._task_check(index)
        if check is None:
            problem = "task has no independent completion check"
        else:
            try:
                problem = self.host.task_check(check)
            except Exception as exc:  # noqa: BLE001 - an unverifiable check is not a passed one
                problem = f"check unverifiable ({type(exc).__name__})"
        if problem:
            assert self.mission is not None
            self.host.log(f"mission {self.mission['id']} task {index + 1}: check not satisfied: {problem}")
        return problem

    def _record_done(self, index: int, session_id: str | None, agent: str | None, status: str | None) -> None:
        assert self.mission is not None
        rec = self.state["mission"]
        check = self._task_check(index)
        outcome = "cycle_settled" if (check or {}).get("kind") == "coordinator_cycle" else "verified"
        now = self.host.now()
        rec["next"] = index + 1
        rec["done"].append({"task": index, "session": session_id, "agent": agent, "at": iso(now), "outcome": outcome,
                            "check": check, "status": status})
        self.save()
        self.store.journal({"event": "task_completed", "at": iso(now), "mission": self.mission["id"], "task": index,
                            "session": session_id, "agent": agent, "outcome": outcome, "check": check})
        self.notify("task_completed", mission=self.mission["id"], task=f"{index + 1}/{rec['total']}",
                    agent=agent, session=session_id)
        self.record("finding", f"relay-continuity: mission {self.mission['id']} task {index + 1}/{rec['total']} "
                               f"{outcome} ({check['kind'] if check else 'no check'}) after {agent} in {session_id}.")
        self.host.log(f"mission {self.mission['id']}: task {index + 1}/{rec['total']} {outcome}")

    def _reopen_legacy(self) -> None:
        """The spec 204 migration, also after a restart settled a pending attempt (which `serve_step` defers it for):
        no task is chosen and no mission completes while a pre-204 completion is still counted."""
        if not self.state.get("pending") and (reopened := reopen_unverified(self.state)):
            assert self.mission is not None
            self.save()
            self.store.journal({"event": "mission_reopened", "at": iso(self.host.now()), "mission": self.mission["id"],
                                "tasks": reopened, "why": "counted complete before spec 204 on a provider return alone"})
            self.host.log(f"mission {self.mission['id']}: reopened unverified task(s) {', '.join(str(t + 1) for t in reopened)}")

    def _reverify_released(self) -> None:
        """After a release, a task whose last attempt already claimed it finished is re-checked before any relaunch:
        a contract attached since, or a deliverable merged by hand, completes it without duplicating the work."""
        assert self.mission is not None
        rec, last = self.state["mission"], self.state.get("last_attempt") or {}
        session = self.state.get("head_session")
        if rec["next"] >= rec["total"] or last.get("task") != rec["next"] or not session:
            return
        status = final_relay_status(self.host.relay.repo, session)
        if not self._settles(rec["next"], status) or self._verify(rec["next"]):
            return  # the task is attempted again; its next claimed finish is checked as usual
        self._record_done(rec["next"], session, last.get("agent"), status)

    def _mission_complete(self) -> str:
        assert self.mission is not None
        rec = self.state["mission"]
        now = self.host.now()
        outcomes = [task_outcome(d) for d in rec["done"]]
        rec["completed_at"] = rec.get("completed_at") or iso(now)
        self.state["phase"] = "idle"
        self.save()
        self.store.journal({"event": "mission_completed", "at": iso(now), "mission": self.mission["id"],
                            "outcomes": outcomes})
        self.notify("mission_completed", mission=self.mission["id"], task=f"{rec['next']}/{rec['total']}",
                    session=self.state["head_session"])
        self.record("decision", f"relay-continuity: mission {self.mission['id']} completed "
                                f"({rec['total']} task(s): {', '.join(outcomes)}; last session {self.state['head_session']}).")
        self.host.log(f"mission {self.mission['id']} completed")
        return "idle"

    def _resolve_pending(self) -> TurnReport | str | None:
        """Restart recovery: settle a launched attempt whose outcome was never recorded."""
        pending = self.state.get("pending")
        if not pending:
            return None
        self.host.log(f"resolving unsettled attempt {pending['nonce']}")
        started = parse_ts(pending.get("started_at")) or self.host.now()
        timed_out = bool(pending.get("timed_out"))
        diagnostics = pending.get("diagnostics") or ""
        killpg_fn = getattr(self.host.relay, "killpg", os.killpg)
        group_running_fn = getattr(self.host.relay, "_group_is_running", None)

        def group_alive(pgid: int) -> bool:
            if callable(group_running_fn):
                return bool(group_running_fn(pgid))
            try:
                killpg_fn(pgid, 0)
                return True
            except ProcessLookupError:
                return False
            except (PermissionError, OSError):
                return True

        pid = pending.get("pid")
        pid_start = pending.get("pid_start")
        if pid and self.host.pid_alive(pid, pid_start):
            self.host.log(f"attempt still running as pid {pid}; waiting for it (never relaunched)")
            wall_limit = (
                getattr(self.limits, "native_turn_wall", Limits.native_turn_wall)
                if pending.get("agent") in NATIVE_AGENTS
                else getattr(self.limits, "turn_wall", Limits.turn_wall)
            )
            while self.host.pid_alive(pid, pid_start):
                elapsed = (self.host.now() - started).total_seconds()
                if elapsed >= wall_limit:
                    timed_out = True
                    wall_diag = f"turn wall limit ({int(wall_limit)}s) exceeded"
                    diagnostics = f"{diagnostics}\n{wall_diag}".strip() if diagnostics else wall_diag
                    pending["timed_out"] = True
                    pending["diagnostics"] = diagnostics
                    self.save()
                    try:
                        killpg_fn(pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                    term_deadline = self.host.now() + timedelta(seconds=30.0)
                    while (self.host.pid_alive(pid, pid_start) or group_alive(pid)) and self.host.now() < term_deadline:
                        rem = max(0.01, (term_deadline - self.host.now()).total_seconds())
                        if self._sleep(min(1.0, rem)):
                            return "signal" if self.signalled else "stopped"
                    if self.host.pid_alive(pid, pid_start) or group_alive(pid):
                        try:
                            killpg_fn(pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, OSError):
                            pass
                    break
                rem_wall = max(0.01, wall_limit - elapsed)
                if self._sleep(min(30.0, rem_wall)):
                    return "signal" if self.signalled else "stopped"
        if timed_out:
            reaped = self._reap()
            if reaped is None:
                return "signal" if self.signalled else "stopped"
        else:
            reaped = None
        session_id = self.host.relay.find_by_nonce(pending["nonce"], started)
        # An adopted run was started by an earlier owner: unless it was terminated under the wall limit,
        # what it left behind is not this process's to stop.
        return self._settle(pending, session_id, diagnostics, reaped, timed_out=timed_out)




def control_root() -> Path:
    return Path(os.environ.get("RELAY_CONTINUITY_DIR", DEFAULT_CONTROL)).expanduser()


def repo_root() -> Path:
    code, out = devctx.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"])
    return Path(out.strip()).parent if code == 0 and out.strip() else Path.cwd()


def release_sha() -> str | None:
    env_sha = os.environ.get("JARVISOS_RELEASE_SHA")
    if env_sha and host_ops.SHA_RE.fullmatch(env_sha):
        return env_sha
    try:
        here = Path(__file__).resolve().parent.parent.name
        return here if host_ops.SHA_RE.fullmatch(here) else None
    except (OSError, ValueError):
        return None


def check_and_request_release_update(
    repo: Path,
    root: Path,
    *,
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr, flush=True),
    run: Runner = devctx.run,
    now: Callable[[], datetime] = now_utc,
    paths: host_ops.Paths | None = None,
    current_sha: str | None = None,
) -> bool:
    """At a task boundary, request release.install for origin/master if it is newer than the owner's
    own release and has green required checks. Returns True if an install was requested and the owner
    should exit 0 without taking another task."""
    owner_sha = current_sha if current_sha is not None else release_sha()
    if not owner_sha:
        return False

    run(["git", "fetch", "-q", "origin", "master"], cwd=repo, timeout=60)
    code, out = run(["git", "rev-parse", "--verify", "origin/master"], cwd=repo)
    if code != 0 or not out.strip():
        return False
    candidate_sha = out.strip()
    if candidate_sha == owner_sha:
        return False

    code, _ = run(["git", "merge-base", "--is-ancestor", owner_sha, candidate_sha], cwd=repo)
    is_newer = (code == 0 and candidate_sha != owner_sha)
    if not is_newer:
        try:
            status = host_ops._gh([f"repos/{host_ops.REPO}/compare/{owner_sha}...{candidate_sha}", "--jq", ".status"]).strip()
            if status == "ahead":
                is_newer = True
        except (RuntimeError, subprocess.SubprocessError, OSError):
            is_newer = False

    if not is_newer:
        return False

    problem = host_ops.required_checks_problem(candidate_sha)
    if problem is not None:
        log(f"owner: candidate {candidate_sha[:12]} checks not green: {problem}")
        return False

    target_paths = paths or host_ops.Paths(Path.home())
    for h_entry in host_ops.read_release_history(target_paths):
        if h_entry.get("candidate_sha") == candidate_sha and h_entry.get("status") in ("refused", "returned"):
            log(f"owner: candidate {candidate_sha[:12]} was previously refused ({h_entry.get('reason', 'unhealthy')})")
            return False

    try:
        for receipt_file in target_paths.receipts.glob("*.json"):
            if receipt_file.name.endswith(".sig"):
                continue
            try:
                receipt, _ = host_ops.load_receipt(target_paths, receipt_file.stem)
                if (
                    receipt
                    and receipt.get("op") == "release.install"
                    and (receipt.get("sha") == candidate_sha or (receipt.get("args") or {}).get("sha") == candidate_sha)
                    and receipt.get("outcome") in ("refused", "failed")
                ):
                    log(f"owner: candidate {candidate_sha[:12]} was previously refused in receipt {receipt_file.stem[:8]}")
                    return False
            except (OSError, ValueError):
                continue
    except OSError:
        pass

    req = host_ops.make_request(
        op="release.install",
        args={"sha": candidate_sha},
        sha=candidate_sha,
        requester="relay-continuity:owner",
        now=now(),
    )
    req_id = host_ops.submit(target_paths, req)
    log(f"owner: requested release.install for {candidate_sha[:12]} (request {req_id[:8]})")
    return True


_LAST_FF_LOGGED: dict[str, str | None] = {"sha": None}


def fast_forward_main_checkout(
    repo: Path,
    *,
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr, flush=True),
    run: Runner = devctx.run,
    last_logged: dict[str, str | None] | None = None,
) -> bool:
    """At a task boundary, fast-forward a clean main checkout on master to origin/master.

    A dirty tree (tracked modifications), another branch, or a non-fast-forward
    leaves the checkout untouched and logs once per origin/master sha.
    """
    tracker = last_logged if last_logged is not None else _LAST_FF_LOGGED

    def _log_refusal(target_sha: str, reason: str) -> None:
        if tracker.get("sha") != target_sha:
            short = target_sha[:8] if host_ops.SHA_RE.fullmatch(target_sha) else target_sha
            log(f"owner: fast-forward to origin/master ({short}) refused/failed: {reason}")
            tracker["sha"] = target_sha

    # 1. Fetch origin master
    code, fetch_out = run(["git", "fetch", "-q", "origin", "master"], cwd=repo, timeout=60.0)
    if code != 0:
        _, target_sha = run(["git", "rev-parse", "origin/master"], cwd=repo, timeout=20.0)
        target_sha = target_sha.strip()
        ref_sha = target_sha if host_ops.SHA_RE.fullmatch(target_sha) else "unknown"
        _log_refusal(ref_sha, f"fetch origin master failed ({fetch_out.strip() or 'exit ' + str(code)})")
        return False

    code, target_sha = run(["git", "rev-parse", "origin/master"], cwd=repo, timeout=20.0)
    target_sha = target_sha.strip()
    if code != 0 or not host_ops.SHA_RE.fullmatch(target_sha):
        ref_sha = target_sha if host_ops.SHA_RE.fullmatch(target_sha) else "unknown"
        _log_refusal(ref_sha, "rev-parse origin/master failed")
        return False

    # 2. Check branch is 'master'
    code, branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, timeout=20.0)
    branch = branch.strip()
    if code != 0 or branch != "master":
        _log_refusal(target_sha, f"checkout is on branch '{branch or 'unknown'}', not 'master'")
        return False

    # 3. Check tracked files are clean (untracked files are ignored)
    code, status = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo, timeout=20.0)
    if code != 0 or status.strip():
        count = len(status.strip().splitlines()) if status.strip() else 0
        _log_refusal(target_sha, f"checkout is dirty ({count} tracked modification{'s' if count != 1 else ''})")
        return False

    # 4. Check if HEAD is already at origin/master
    code, head_sha = run(["git", "rev-parse", "HEAD"], cwd=repo, timeout=20.0)
    head_sha = head_sha.strip()
    if code == 0 and head_sha == target_sha:
        return True

    # Re-check tracked files immediately before merge
    code, status = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo, timeout=20.0)
    if code != 0 or status.strip():
        count = len(status.strip().splitlines()) if status.strip() else 0
        _log_refusal(target_sha, f"checkout is dirty ({count} tracked modification{'s' if count != 1 else ''})")
        return False

    # 5. Fast-forward merge only
    # The remaining window is safe because git merge --ff-only refuses to overwrite local modifications to files the incoming commits touch.
    code, merge_out = run(["git", "merge", "--ff-only", "origin/master"], cwd=repo, timeout=60.0)
    if code != 0:
        _log_refusal(target_sha, f"merge --ff-only failed: {merge_out.strip() or 'exit ' + str(code)}")
        return False

    log(f"owner: fast-forwarded main checkout to origin/master ({target_sha[:8]})")
    tracker["sha"] = None
    return True


def permissions_digest(path: Path) -> str | None:
    """Digest of the authority-bearing Relay permission settings. Relay rewrites `model` whenever `--model`
    is passed; a model choice grants no authority, so it is excluded. An unreadable file digests as such."""
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "unreadable"
    for section in data.values():
        if isinstance(section, dict) and isinstance(section.get("backend"), dict):
            section["backend"].pop("model", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def make_host(repo: Path, relay_env: dict[str, str] | None = None) -> Host:
    env = devctx.default_env(repo)
    author = f"relay-continuity pid {os.getpid()}"

    def ledger(kind: str, text: str) -> str | None:
        entry = devctx.make_entry(env, kind, text, refs={"spec": "195"}, author=author)
        devctx.append_entry(env, entry)
        return entry.get("id")

    def permissions() -> str | None:
        return permissions_digest(repo / ".agent-relay" / "permissions.toml")

    return Host(relay=RelayAdapter(repo, env=relay_env), fingerprint=lambda: fingerprint(repo),
                conflicts=lambda own: live_conflicts(repo, own), ledger=ledger, permissions_digest=permissions,
                probe=lambda call: probe_call(call, repo),
                reap=lambda: reap_descendants(keep=relay_jobs.live_roots),
                task_check=lambda check: check_task(repo, check), worker_routing=lambda: worker_routing_block(repo),
                session_context=lambda authority: session_context_block(
                    control_root(), env, authority, log=lambda msg: print(msg, file=sys.stderr, flush=True)
                ),
                job_status=lambda jid: relay_jobs.load(jid),
                pr_ci_status=lambda pr, sha: default_pr_ci_status(repo, pr, sha),
                reachable=default_reachable,
                read_ledger=lambda: devctx.read_ledger(env))


