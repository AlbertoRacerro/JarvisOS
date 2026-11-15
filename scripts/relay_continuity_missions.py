"""Missions submission and serve_step for relay_continuity (spec 265)."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import signal
import socket
import sys
from collections.abc import Callable
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
    DIRECTIVE_ID_RE,
    MAX_TASK_CHARS,
    MAX_TASKS,
    MISSION_ID_RE,
    MISSION_SCHEMA,
    PREFERRED,
    PREFERRED_AGENTS,
    SANDBOX_HOSTNAME,
    SPEC_ID_RE,
    WEBHOOK_FILE,
    devctx,
    host_ops,
    iso,
    load_sibling,
    now_utc,
    parse_ts,
)

load_sibling("relay_continuity_state", "relay_continuity_state.py")
from relay_continuity_state import (  # noqa: E402
    Lock,
    StateCorrupt,
    Store,
    default_state,
    redecidable,
    reopen_unverified,
)

load_sibling("relay_continuity_notify", "relay_continuity_notify.py")
from relay_continuity_notify import (  # noqa: E402
    post_webhook,
)

load_sibling("relay_continuity_adapters", "relay_continuity_adapters.py")
from relay_continuity_adapters import (  # noqa: E402
    become_subreaper,
    live_conflicts,
)

load_sibling("relay_continuity_supervisor", "relay_continuity_supervisor.py")
from relay_continuity_supervisor import (  # noqa: E402
    Supervisor,
    control_root,
    make_host,
    repo_root,
)

# ---------------------------------------------------------------------------
# Missions (spec 201): durable submission and a single persistent owner


CHECK_KINDS = ("host_receipt", "pr_merged", "path_exists", "spec_merged", "coordinator_cycle")


def validate_mission(data: Any) -> str | None:
    """Problem with a mission definition, or None. Definitions are immutable once submitted."""
    if not isinstance(data, dict):
        return "not an object"
    if data.get("schema") != MISSION_SCHEMA or not MISSION_ID_RE.fullmatch(str(data.get("id"))):
        return "unknown schema or id"
    if not DIRECTIVE_ID_RE.fullmatch(str(data.get("authority"))):
        return "authority must be a maintainer directive ledger id"
    if parse_ts(data.get("submitted_at")) is None:
        return "bad submitted_at"
    if data.get("session") is not None and not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", str(data["session"])):
        return "bad session"
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        return f"tasks must be a list of 1-{MAX_TASKS} entries"
    for n, task in enumerate(tasks, 1):
        if not isinstance(task, dict) or set(task) - {"text", "check"}:
            return f"task {n}: only text and check are allowed"
        text = task.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TASK_CHARS:
            return f"task {n}: text must be 1-{MAX_TASK_CHARS} characters"
        if devctx.find_secret(text):
            return f"task {n}: text matches a secret pattern"
        if task.get("check") is not None and (problem := validate_check(task["check"])):
            return f"task {n}: {problem}"
    return None


def validate_check(check: Any) -> str | None:
    """Problem with one completion contract, or None (spec 204)."""
    if not isinstance(check, dict) or check.get("kind") not in CHECK_KINDS:
        return f"check kind must be one of {', '.join(CHECK_KINDS)}"
    if check["kind"] == "host_receipt":
        if (set(check) - {"kind", "op", "sha"} or not isinstance(check.get("op"), str)
                or check["op"] not in host_ops.OPERATIONS):
            return "host_receipt takes a known op and optional full sha"
        if "sha" in check and (not isinstance(check["sha"], str) or not host_ops.SHA_RE.fullmatch(check["sha"])):
            return "host_receipt sha must be a full commit id"
    if check["kind"] == "pr_merged" and (set(check) != {"kind", "pr"} or not isinstance(check["pr"], int)
                                         or isinstance(check["pr"], bool) or check["pr"] <= 0):
        return "pr_merged takes a positive integer pr"
    if check["kind"] == "path_exists":
        path = check.get("path")
        if set(check) != {"kind", "path"} or not isinstance(path, str) or not path or Path(path).is_absolute() \
                or ".." in Path(path).parts:
            return "path_exists takes a relative repository path"
    if check["kind"] == "spec_merged" and (set(check) != {"kind", "spec"} or not isinstance(check["spec"], str)
                                           or not SPEC_ID_RE.fullmatch(check["spec"])):
        return "spec_merged takes a spec id such as \"204\""
    if check["kind"] == "coordinator_cycle" and set(check) != {"kind"}:
        return "coordinator_cycle takes no fields"
    return None


def missions_dir(root: Path) -> Path:
    return root / "missions"


def mission_store(root: Path, mission_id: str) -> Store:
    store = Store(missions_dir(root) / mission_id)
    store.stop_path = root / "STOP"  # one stop request stops the owner, whichever mission it executes
    return store


def load_missions(root: Path) -> list[tuple[dict[str, Any] | None, str, str | None]]:
    """(definition or None, id, problem) for every submitted mission, in submission (FIFO) order."""
    found = []
    for path in sorted(missions_dir(root).glob("m-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            found.append((None, path.stem, f"unreadable ({type(exc).__name__})"))
            continue
        problem = validate_mission(data) or (None if data.get("id") == path.stem else "id does not match file name")
        found.append((None if problem else data, path.stem, problem))
    return sorted(found, key=lambda m: ((m[0] or {}).get("submitted_at") or "", m[1]))


def mission_status(store: Store) -> tuple[str, dict[str, Any] | None]:
    """queued | active | waiting | held | complete | cancelled | corrupt, with the store's state."""
    if not store.state_path.exists():
        return "queued", None
    try:
        state = store.load()
    except StateCorrupt:
        return "corrupt", None
    rec = state.get("mission") or {}
    if rec.get("cancelled_at"):
        return "cancelled", state
    if rec.get("completed_at"):
        return "complete", state
    if state["phase"] == "held":
        return "held", state
    if state["phase"] == "waiting":
        return "waiting", state
    return ("active" if state.get("chain") or state.get("pending") else "queued"), state


def mission_mismatch(mission: dict[str, Any], mission_id: str, state: dict[str, Any]) -> bool:
    rec = state.get("mission")
    return not isinstance(rec, dict) or rec.get("id") != mission_id or rec.get("total") != len(mission["tasks"])


def unreadable_mission(root: Path) -> str | None:
    """Id of the first mission `serve_step` would report `corrupt`: its definition fails to load, its store is
    corrupt, or an open mission's state does not match its definition. None when the whole queue loads."""
    for mission, mission_id, _problem in load_missions(root):
        if mission is None:
            return mission_id
        status, state = mission_status(mission_store(root, mission_id))
        if status == "corrupt":
            return mission_id
        if status not in ("complete", "cancelled") and state is not None and mission_mismatch(mission, mission_id, state):
            return mission_id
    return None


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per write: an owner in a fresh PID namespace reuses its PID, so a temp file left by a killed write
    # must never collide with the retry.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    with open(tmp, "x", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def in_sandbox() -> bool:
    """The certified jarvis-sandbox boundary (spec 201 D1): its hostname, and no Windows or WSL interop paths."""
    return socket.gethostname() == SANDBOX_HOSTNAME and not Path("/mnt/c").exists() and not Path("/run/WSL").exists()


def webhook_sender() -> Callable[[str], str | None] | None:
    if not WEBHOOK_FILE.exists():
        return None
    mode = WEBHOOK_FILE.stat().st_mode & 0o077
    if mode:
        print("refusing to use the webhook file: it must not be group/world readable (chmod 600)", file=sys.stderr)
        return None
    return lambda text: post_webhook(text)


def command_run(args: argparse.Namespace) -> int:
    root = control_root()
    lock = Lock(root / "supervisor.lock")
    if not lock.acquire():
        print("another continuity supervisor holds the lock; refusing to start", file=sys.stderr)
        return 3
    repo = repo_root()
    try:
        conflicts = live_conflicts(repo, {os.getpid()})
        if "legacy_autopilot" in conflicts:
            print("legacy autopilot scripts are running; stop them first (no concurrent supervisors)", file=sys.stderr)
            return 3
        task = None
        if args.task_file:
            task = Path(args.task_file).read_text(encoding="utf-8")
            if devctx.find_secret(task):
                print("task file matches a secret pattern; refusing", file=sys.stderr)
                return 2
        if not become_subreaper():
            print("cannot own the processes a turn leaves behind (PR_SET_CHILD_SUBREAPER failed); refusing", file=sys.stderr)
            return 2
        store = Store(root)
        store.stop_path.unlink(missing_ok=True)
        supervisor = Supervisor(store, make_host(repo), turns=args.turns,
                                sender=None if args.no_notify else webhook_sender())

        def on_signal(signum: int, _frame: Any) -> None:
            supervisor.signalled = True
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        result = supervisor.run(args.session, task=task)
        print(json.dumps({"result": result, "head_session": supervisor.state.get("head_session"),
                          "phase": supervisor.state.get("phase"), "hold": supervisor.state.get("hold")}))
        return {"idle": 0, "stopped": 0, "signal": 0, "waiting": 0, "held": 4}.get(result, 1)
    finally:
        lock.release()


def init_mission_state(store: Store, mission: dict[str, Any], state: dict[str, Any] | None,
                       now: Callable[[], datetime]) -> dict[str, Any]:
    """The mission's durable progress record, created (and journalled started) the first time it is needed."""
    if state is None or state.get("mission") is None:
        state = state or default_state()
        state["mission"] = {"id": mission["id"], "next": 0, "total": len(mission["tasks"]), "done": []}
        store.save(state, now())
        store.journal({"event": "mission_started", "at": iso(now()), "mission": mission["id"],
                       "authority": mission["authority"], "tasks": len(mission["tasks"])})
    return state


def serve_step(root: Path, factory: Callable[[Store, dict[str, Any]], Supervisor], *,
               conflicts: Callable[[], list[str]], now: Callable[[], datetime] = now_utc,
               log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr, flush=True)) -> str:
    """One scheduling decision of the persistent owner. Missions run strictly one at a time in submission
    order: a held or corrupt mission blocks the ones behind it rather than being skipped. Returns `busy`,
    `empty`, `held:<id>`, `corrupt:<id>`, `waiting:<id>:<reason>` or `ran:<id>:<supervisor result>`."""
    lock = Lock(root / "supervisor.lock")
    if not lock.acquire():
        return "busy"  # a `run`, `release` or `cancel` holds it; look again later
    try:
        last_completed_agent = None
        for mission, mission_id, problem in load_missions(root):
            if mission is None:
                log(f"mission {mission_id}: definition invalid ({problem}); queue held")
                return f"corrupt:{mission_id}"
            store = mission_store(root, mission_id)
            status, state = mission_status(store)
            if status in ("complete", "cancelled"):
                if status == "complete" and state:
                    last_completed_agent = state.get("agent")
                continue
            if status == "corrupt" or (status == "held" and not redecidable(state)):
                return f"{status}:{mission_id}"
            if state is not None and mission_mismatch(mission, mission_id, state):
                log(f"mission {mission_id}: state does not match its definition; queue held")
                return f"corrupt:{mission_id}"
            if found := conflicts():
                # A live REPL or legacy autopilot on the repository: never a concurrent writer, so wait.
                return f"waiting:{mission_id}:{'repl_active' if 'repl_active' in found else 'legacy_autopilot'}"
            if state is None and last_completed_agent:
                state = default_state()
                state["agent"] = last_completed_agent if last_completed_agent in PREFERRED_AGENTS else PREFERRED
            state = init_mission_state(store, mission, state, now)
            if not state.get("pending") and (reopened := reopen_unverified(state)):
                store.save(state, now())
                store.journal({"event": "mission_reopened", "at": iso(now()), "mission": mission_id, "tasks": reopened,
                               "why": "counted complete before spec 204 on a provider return alone"})
                log(f"mission {mission_id}: reopened unverified task(s) {', '.join(str(t + 1) for t in reopened)}")
            supervisor = factory(store, mission)
            supervisor.mission = mission
            result = supervisor.run(state.get("head_session") or mission.get("session"))
            return f"ran:{mission_id}:{result}"
        return "empty"
    finally:
        lock.release()


