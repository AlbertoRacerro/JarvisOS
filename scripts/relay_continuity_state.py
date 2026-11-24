"""Durable state for relay_continuity (spec 265)."""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import sys
from datetime import datetime
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
    CI_WAIT_LINE_RE,
    JOB_WAIT_LINE_RE,
    PHASES,
    REDECIDED_HOLDS,
    SCHEMA,
    iso,
    parse_ts,
)

# ---------------------------------------------------------------------------
# Durable state


class StateCorrupt(Exception):
    pass


def default_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA, "phase": "idle", "head_session": None, "chain": [], "agent": None,
        "hold": None, "cooldowns": {}, "switches": [], "last_switch_at": None,
        "counters": {"consecutive_failures": 0, "transient_streak": 0, "crash_retries": 0},
        "permissions_digest": None, "permissions_pinned": False, "pending": None, "waiting_since": None, "last_outcome": None,
        "notify": {"sent": {}, "window": [], "last_digest": None, "failures": 0, "retry": None, "dropped": 0},
        "watch": {"seen": {}, "master": None}, "acknowledged": None, "spend_caps": {}, "updated_at": None,
        "parked_turns": [], "wait": None, "resumes": 0, "last_checkpoint_at": None, "network_outage": False,
        "network_outage_since": None,
        "unreadable_alerts": [],
    }


STATE_TYPES: dict[str, tuple[type, ...]] = {
    "head_session": (str, type(None)), "chain": (list,), "agent": (str, type(None)), "hold": (dict, type(None)),
    "cooldowns": (dict,), "switches": (list,), "last_switch_at": (str, type(None)), "counters": (dict,),
    "permissions_digest": (str, type(None)), "permissions_pinned": (bool,), "pending": (dict, type(None)),
    "waiting_since": (str, type(None)), "last_outcome": (str, type(None)), "notify": (dict,), "watch": (dict,),
    "acknowledged": (dict, type(None)), "spend_caps": (dict,), "updated_at": (str, type(None)),
    "wait": (dict, type(None)), "resumes": (int,), "last_checkpoint_at": (str, type(None)),
}
# Keys added after the first schema: a state written by an older release lacks them and gets their defaults on load.
# A new key of `default_state()` goes here; any other absent key means a damaged file.
ADDED_STATE_KEYS = ("parked_turns", "wait", "resumes", "last_checkpoint_at", "network_outage", "network_outage_since", "unreadable_alerts")
PENDING_KEYS = ("nonce", "agent", "parent", "started_at", "pid", "pid_start", "fingerprint", "attempt")


GATE_HOLDS = ("effects_uncertain", "output_unparseable", "output_missing", "reconcile_unresolved",
              "reconcile_insufficient_permissions", "reconcile_failed", "reconcile_no_sandbox", "live_work")


def waivers(ack: dict[str, Any] | None) -> frozenset[str]:
    """What the maintainer's releases of a held turn reviewed, and so may waive when that turn is continued."""
    return frozenset({"gate"} if set((ack or {}).get("reasons") or []) & set(GATE_HOLDS) else set())


def validate_state(data: dict[str, Any]) -> str | None:
    for key, types in STATE_TYPES.items():
        if key not in data:
            return f"missing {key}"
        if not isinstance(data[key], types):
            return f"bad type for {key}"
    if not all(isinstance(data["counters"].get(k), int) for k in ("consecutive_failures", "transient_streak", "crash_retries")):
        return "bad counters"
    pending = data["pending"]
    if pending is not None and (any(k not in pending for k in PENDING_KEYS) or not isinstance(pending["nonce"], str)):
        return "bad pending attempt"
    # Optional: written only by the earlier spec 197 controller; dropped on the next run.
    rec = data.get("reconcile")
    if rec is not None and (not isinstance(rec, dict) or not isinstance(rec.get("turn"), str)
                            or not isinstance(rec.get("attempts"), int) or not isinstance(rec.get("verdicts"), dict)
                            or not isinstance(rec.get("since"), str)):
        return "bad reconcile record"
    diag = (rec or {}).get("diag")
    if diag is not None and (not isinstance(diag, dict) or any(k not in diag for k in PENDING_KEYS)
                             or not isinstance(diag["nonce"], str)):
        return "bad diagnostic attempt"
    if not isinstance(data.get("diagnostics", []), list):
        return "bad type for diagnostics"
    if not isinstance(data.get("unreadable_alerts", []), list):
        return "bad type for unreadable_alerts"
    if not isinstance(data.get("network_outage", False), bool):
        return "bad network_outage"
    nos = data.get("network_outage_since")
    if nos is not None and (not isinstance(nos, str) or parse_ts(nos) is None):
        return "bad network_outage_since"
    resumes = data.get("resumes")
    if isinstance(resumes, bool) or not isinstance(resumes, int) or resumes < 0:
        return "bad resumes"
    last_cp = data.get("last_checkpoint_at")
    if last_cp is not None and (not isinstance(last_cp, str) or parse_ts(last_cp) is None):
        return "bad last_checkpoint_at"
    wait = data.get("wait")
    if wait is not None:
        if not isinstance(wait, dict):
            return "bad wait record"
        jobs = wait.get("jobs")
        ci = wait.get("ci")
        if not isinstance(jobs, list) or not isinstance(ci, list) or (not jobs and not ci):
            return "bad wait record"
        if any(not isinstance(j, str) or not JOB_WAIT_LINE_RE.match(f"JOB-WAIT: {j}") for j in jobs):
            return "bad wait record"
        for item in ci:
            if not isinstance(item, dict):
                return "bad wait record"
            pr = item.get("pr")
            sha = item.get("sha")
            if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
                return "bad wait record"
            if not isinstance(sha, str) or len(sha) != 40 or not CI_WAIT_LINE_RE.match(f"CI-WAIT: {pr}@{sha}"):
                return "bad wait record"
        if not isinstance(wait.get("started_at"), str) or parse_ts(wait.get("started_at")) is None:
            return "bad wait record"
        if wait.get("last_job_poll") is not None and (not isinstance(wait.get("last_job_poll"), str)
                                                      or parse_ts(wait.get("last_job_poll")) is None):
            return "bad wait record"
        if wait.get("last_ci_poll") is not None and (not isinstance(wait.get("last_ci_poll"), str)
                                                     or parse_ts(wait.get("last_ci_poll")) is None):
            return "bad wait record"
    # Optional (spec 201): present only in a mission's own store.
    mission = data.get("mission")
    if mission is not None and (not isinstance(mission, dict) or not isinstance(mission.get("id"), str)
                                or not isinstance(mission.get("next"), int) or not isinstance(mission.get("total"), int)
                                or not 0 <= mission["next"] <= mission["total"] or not isinstance(mission.get("done"), list)):
        return "bad mission record"
    return None


def unsettled_nonces(journal: Path) -> list[str]:
    """Launched attempts with no settle record and not dropped by a maintainer release, oldest first."""
    launched: dict[str, None] = {}
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn final line is the only kind a crash can leave; the state's pending covers it
        if not isinstance(record, dict):
            continue
        if record.get("event") == "launch" and isinstance(record.get("nonce"), str) \
                and record.get("purpose") != "diagnostic":  # earlier read-only diagnoses changed nothing
            launched[record["nonce"]] = None
        elif record.get("event") == "settle":
            launched.pop(record.get("nonce"), None)
        elif record.get("event") == "release":
            for nonce in record.get("dropped") or []:
                launched.pop(nonce, None)
    return list(launched)


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.state_path = root / "state.json"
        self.journal_path = root / "attempts.jsonl"
        self.stop_path = root / "STOP"

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return default_state()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateCorrupt(f"state file unreadable: {type(exc).__name__}") from exc
        if not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("phase") not in PHASES:
            raise StateCorrupt("state file has an unknown schema or phase")
        defaults = default_state()
        for key in ADDED_STATE_KEYS:
            data.setdefault(key, defaults[key])
        if problem := validate_state(data):
            raise StateCorrupt(f"state file is structurally invalid: {problem}")
        return data

    def save(self, state: dict[str, Any], now: datetime) -> None:
        state["updated_at"] = iso(now)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f".state.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=1, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.state_path)
        dir_fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def journal(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.journal_path, "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle, fcntl.LOCK_UN)


class Lock:
    """Exclusive non-blocking flock; released by the kernel if the holder dies."""

    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            return False
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return True

    def release(self) -> None:
        if self.handle:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None




def task_outcome(done: dict[str, Any]) -> str:
    """How a recorded task completion is known (spec 204). Entries written before 204 carry no outcome: they are
    a successful provider return, never verified delivery."""
    return done.get("outcome") or ("maintainer_attested" if done.get("by") == "maintainer" else "legacy_unverified")


def reopen_unverified(state: dict[str, Any]) -> list[int]:
    """Spec 204 migration of an unfinished mission: tasks the pre-204 owner counted complete on a successful
    provider return alone are reopened, from the first such task on, so they are resumed and verified rather than
    skipped. The history is kept under `reopened`; definitions and finished missions are never touched."""
    rec = state.get("mission") or {}
    if rec.get("completed_at") or rec.get("cancelled_at"):
        return []
    legacy = [d["task"] for d in rec.get("done") or [] if task_outcome(d) == "legacy_unverified"]
    if not legacy:
        return []
    first = min(legacy)
    rec["reopened"] = (rec.get("reopened") or []) + [d for d in rec["done"] if d["task"] >= first]
    rec["done"] = [d for d in rec["done"] if d["task"] < first]
    rec["next"] = first
    return sorted(d["task"] for d in rec["reopened"] if d["task"] >= first)




def redecidable(state: dict[str, Any] | None) -> bool:
    """A hold the current policy may not raise, not yet decided again: the supervisor decides its turn once more."""
    hold = (state or {}).get("hold") or {}
    return (hold.get("reason") in REDECIDED_HOLDS and bool(hold.get("turn")) and hold.get("resumable", True)
            and bool((state or {}).get("head_session")))


