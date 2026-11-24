"""Roadmap continuation and serve command for relay_continuity (spec 265)."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
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
    MISSION_SCHEMA,
    PREFERRED,
    devctx,
    host_ops,
    iso,
    load_sibling,
    now_utc,
    parse_ts,
    relay_jobs,
)

load_sibling("relay_continuity_turns", "relay_continuity_turns.py")
from relay_continuity_turns import (  # noqa: E402
    classify_turn,
    last_turn_dir,
)

load_sibling("relay_continuity_state", "relay_continuity_state.py")
from relay_continuity_state import (  # noqa: E402
    Lock,
    StateCorrupt,
    Store,
    default_state,
)

load_sibling("relay_continuity_notify", "relay_continuity_notify.py")
from relay_continuity_notify import (  # noqa: E402
    SAFE_MISSION_FILE_RE,
    Notifier,
)

free_model_monitor = load_sibling("free_model_monitor", "free_model_monitor.py")

load_sibling("relay_continuity_adapters", "relay_continuity_adapters.py")
from relay_continuity_adapters import (  # noqa: E402
    _reap_zombies,
    become_subreaper,
    live_conflicts,
)

load_sibling("relay_continuity_probes", "relay_continuity_probes.py")
from relay_continuity_probes import (  # noqa: E402
    Runner,
    final_relay_status,
    final_text,
)

load_sibling("relay_continuity_supervisor", "relay_continuity_supervisor.py")
from relay_continuity_supervisor import (  # noqa: E402
    Supervisor,
    check_and_request_release_update,
    control_root,
    fast_forward_main_checkout,
    make_host,
    release_sha,
    repo_root,
)

load_sibling("relay_continuity_missions", "relay_continuity_missions.py")
from relay_continuity_missions import (  # noqa: E402
    in_sandbox,
    mission_status,
    mission_store,
    missions_dir,
    serve_step,
    unreadable_mission,
    validate_mission,
    webhook_sender,
    write_json_atomic,
)

# ---------------------------------------------------------------------------
# Roadmap continuation (spec 203): the owner keeps one coordinator cycle queued; the turn chooses the work

ROADMAP_NO_PROGRESS = 1  # a ledger-only cycle must not launch another model turn
ROADMAP_LOCK_WAIT = 120.0  # seconds `roadmap start|stop` waits for an owner step (bounded by its ls-remote)
ROADMAP_TASK = (
    "Roadmap continuation cycle {cycle}. Act as the coordinator described in docs/PROCESS.md for this repository. "
    "Fresh GitHub state and docs/specs/STATUS.md are the backlog; the maintainer directives and holds shown by `devctx.py recover` bind you. "
    "When an operator mission brief is attached, it states what the work is for: judge value against it, and say in the cycle note when you depart from its suggested order. "
    "Recover existing PRs, worktrees and claims before creating new ones. Choose the highest-value authorized work "
    "and drive it to a verified, merged and reconciled result, or to a durable checkpoint (a pushed branch or PR "
    "and a ledger note) when it cannot finish in this turn. While a registered job runs, take the next item in the order the standing directives give; only when none is left, other authorized work. To wait for your own job or PR checks, end with `RELAY_STATUS continue` and a `JOB-WAIT:` or `CI-WAIT:` line; the supervisor resumes you when it completes. End the turn with a wait line, never a sleeping command. Run `devctx.py recover` once at cycle start (never in "
    "parallel) and start from its Last cycle handoff; before ending, record a `devctx.py note` naming the head, "
    "remaining items, the next directive item and next step. Do not submit missions or change roadmap mode: the "
    "mission owner schedules the next cycle. End with RELAY_STATUS propose_done when this cycle's work is merged "
    "or checkpointed. End with RELAY_STATUS blocked, after recording a `devctx.py note question` or `blocker`, "
    "only when no authorized useful work remains across the relevant lanes. A human-dependent step blocks only "
    "its dependent work: record the question and continue safe disjoint authorized work under docs/PROCESS.md, "
    "including owed live activation and investigation/planning/readiness for the "
    "maintainer's already-directed next goal. Planned product implementation starts from `ready`; readiness is yours to give after your own critical review (docs/PROCESS.md section 3), and with nothing ready you prepare the next item of the directive. "
    "Apply only the maintainer interruption classes in docs/PROCESS.md; ordinary reversible technical decisions are yours."
)


def roadmap_path(root: Path) -> Path:
    return root / "roadmap.json"


def load_roadmap(root: Path) -> dict[str, Any] | None:
    """The roadmap record, None when roadmap mode was never started, or {"corrupt": True}."""
    try:
        data = json.loads(roadmap_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return {"corrupt": True}
    return data if isinstance(data, dict) else {"corrupt": True}


def remote_fingerprint(repo: Path, run: Runner = devctx.run) -> str | None:
    """Digest of the remote branch heads; None when they cannot be read."""
    code, out = run(["git", "ls-remote", "--heads", "origin"], cwd=repo, timeout=60)
    return hashlib.sha256(out.encode()).hexdigest()[:16] if code == 0 and out.strip() else None


def roadmap_job_ids(root: Path) -> list[str]:
    """Registered jobs are durable work even before their remote result is committed."""
    return sorted(job["id"] for job in relay_jobs.all_jobs(root))


def roadmap_job_states(root: Path) -> dict[str, str]:
    return {job["id"]: relay_jobs.effective_status(job) for job in relay_jobs.all_jobs(root)}


def roadmap_pr_ids(repo: Path, run: Runner = devctx.run) -> list[int] | None:
    """Open PR creation is progress even if its branch was pushed in an earlier cycle."""
    code, out = run(["gh", "pr", "list", "--state", "open", "--limit", "1000", "--json", "number"],
                    cwd=repo, timeout=30)
    if code != 0:
        return None
    try:
        rows = json.loads(out)
        return sorted(row["number"] for row in rows) if isinstance(rows, list) else None
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def roadmap_ci_fingerprint(repo: Path, run: Runner = devctx.run) -> str | None:
    """Read recent CI transitions without launching a model turn."""
    code, out = run(["gh", "run", "list", "--limit", "20", "--json",
                     "databaseId,status,conclusion,updatedAt,headSha"], cwd=repo, timeout=30)
    if code != 0:
        return None
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return None
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:16] if isinstance(rows, list) else None


def roadmap_lock(root: Path, wait: float = 0.0) -> Lock | None:
    """Serializes every read-modify-write of roadmap.json (owner step, start, stop). None: still busy after `wait`."""
    lock = Lock(root / "roadmap.lock")
    deadline = time.monotonic() + wait
    while not lock.acquire():
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)
    return lock


def _human_answer_author(author: object) -> bool:
    name = str(author or "")
    if name.startswith("relay-"):
        return False
    session = re.fullmatch(r"(\S+) pid \d+", name)
    return session is None or session.group(1) not in devctx.AGENT_NAMES


def _ledger_human_after(repo: Path, paused_at: str | None) -> bool | None:
    """Human ledger input after the pause; None means the ledger could not be read."""
    if not paused_at:
        return False
    try:
        entries = devctx.read_ledger(devctx.default_env(repo))
    except OSError:
        return None
    answerable_ids: set[str] = set()
    for entry in entries:
        kind = entry.get("kind")
        if str(entry.get("ts") or "") > paused_at:
            if (kind == "directive" and
                    (str(entry.get("author") or "").startswith("maintainer") or
                     isinstance(entry.get("directive"), dict))):
                return True
            if kind == "decision":
                text = str(entry.get("text") or "")
                if (str(entry.get("author") or "").startswith("maintainer (") and
                        text.startswith("Maintainer answer to question ")):
                    return True
                if _human_answer_author(entry.get("author")) and any(ref in answerable_ids for ref in re.findall(
                        r"(?<![A-Za-z0-9])L\d{14}-[0-9a-f]{4}(?![A-Za-z0-9])", text)):
                    return True
        if kind in ("blocker", "question"):
            answerable_ids.add(str(entry.get("id") or ""))
    return False


def maintainer_or_hostops_after(repo: Path, paused_at: str | None) -> bool:
    """Durable human input or a verified completed host operation wakes a stalled roadmap."""
    ledger_wake = _ledger_human_after(repo, paused_at)
    if ledger_wake is not False:
        return ledger_wake is True
    if not paused_at:
        return False
    try:
        paths = host_ops.Paths(Path.home())
        for path in paths.receipts.iterdir():
            if not host_ops.REQUEST_NAME_RE.fullmatch(path.name):
                continue
            receipt, _ = host_ops.load_receipt(paths, path.name[:-5])
            if (receipt and receipt.get("outcome") in ("ok", "refused", "failed", "cancelled", "timeout")
                    and str(receipt.get("finished_at") or "") > paused_at):
                return True
    except OSError:
        return False
    return False


def roadmap_step(root: Path, repo: Path, *, fingerprint: Callable[[], str | None],
                 submit: Callable[[str, list[dict[str, Any]], str], tuple[dict[str, Any] | None, str | None]],
                 now: Callable[[], datetime] = now_utc) -> str | None:
    """Called only when every mission is complete or cancelled. Settles the last cycle once, then submits the next
    or pauses. None: roadmap mode is off. Otherwise `roadmap:submitted:<id>`, `roadmap:paused:<reason>`,
    `roadmap:waiting:<why>` or `roadmap:corrupt`."""
    lock = roadmap_lock(root)
    if lock is None:
        return "roadmap:waiting:busy"  # a `roadmap start|stop` is changing it
    try:
        return _roadmap_step(root, repo, fingerprint=fingerprint, submit=submit, now=now)
    finally:
        lock.release()


def _roadmap_step(root: Path, repo: Path, *, fingerprint: Callable[[], str | None],
                  submit: Callable[[str, list[dict[str, Any]], str], tuple[dict[str, Any] | None, str | None]],
                  now: Callable[[], datetime]) -> str | None:
    road = load_roadmap(root)
    if road is not None and road.get("corrupt"):
        return "roadmap:corrupt"  # never read as "off": nothing is submitted until it is inspected
    if road is None or not road.get("enabled"):
        return None
    if road.get("paused"):
        waits = road.get("hostops_wait")
        jobs = road.get("job_wait")
        uncertain = []
        observed_jobs = {}
        if isinstance(jobs, list):
            for job_id in jobs:
                if not isinstance(job_id, str) or not relay_jobs.ID_RE.fullmatch(job_id):
                    continue
                job = relay_jobs.load(job_id, root)
                if job is not None:
                    observed_jobs[job_id] = relay_jobs.effective_status(job)
                    if observed_jobs[job_id] == "unknown":
                        uncertain.append(job_id)
        observation_key = None
        if uncertain:
            try:
                observation_key = os.readlink("/proc/self/ns/pid") + ":" + ",".join(sorted(uncertain))
            except OSError:
                pass
        if (road["paused"] == "coordinator_blocked" and isinstance(waits, list) and waits
                and all(isinstance(i, str) and host_ops.ID_RE.fullmatch(i)
                        and host_ops.load_receipt(host_ops.Paths(Path.home()), i)[0] for i in waits)):
            road.update(paused=None, paused_at=None)
            road.pop("hostops_wait", None)
            write_json_atomic(roadmap_path(root), road)
            missions_journal(root, {"event": "roadmap_hostops_resumed", "at": iso(now()), "ids": waits})
        elif road["paused"] == "coordinator_blocked" and isinstance(jobs, list) and jobs and all(
                isinstance(i, str) and relay_jobs.ID_RE.fullmatch(i) and (observed_jobs.get(i) in relay_jobs.TERMINAL or i in uncertain) for i in jobs) and (
                not uncertain or observation_key and observation_key not in road.get("job_observations", [])):
            road.update(paused=None, paused_at=None)
            road.pop("job_wait", None)
            if uncertain:
                road.setdefault("job_observations", []).append(observation_key)
            write_json_atomic(roadmap_path(root), road)
            missions_journal(root, {"event": "roadmap_jobs_resumed", "at": iso(now()), "ids": jobs,
                                    "uncertain": uncertain})
        elif (road["paused"] == "coordinator_blocked" and not waits and not jobs and
              _ledger_human_after(repo, road.get("paused_at"))):
            road.update(paused=None, paused_at=None)
            write_json_atomic(roadmap_path(root), road)
            missions_journal(root, {"event": "roadmap_answer_resumed", "at": iso(now())})
        elif road["paused"] == "no_progress":
            human_wake = maintainer_or_hostops_after(repo, road.get("paused_at"))
            heads = fingerprint()
            if heads is None and not human_wake:
                return "roadmap:waiting:remote_unreadable"
            job_states = roadmap_job_states(root)
            wake = (heads != road.get("fingerprint") or job_states != road.get("job_states")
                    or human_wake)
            pr_ids = roadmap_pr_ids(repo)
            if pr_ids is not None and road.get("pr_ids") is not None:
                wake = wake or bool(set(pr_ids) - set(road["pr_ids"]))
            if not wake and (deadline := parse_ts(road.get("wake_at"))) is not None:
                wake = now() >= deadline
            last_ci_check = parse_ts(road.get("ci_checked_at"))
            if not wake and (last_ci_check is None or now() - last_ci_check >= timedelta(minutes=1)):
                ci = roadmap_ci_fingerprint(repo)
                road["ci_checked_at"] = iso(now())
                if ci is not None and road.get("ci_fingerprint") is None:
                    road["ci_fingerprint"] = ci
                elif ci is not None and ci != road.get("ci_fingerprint"):
                    wake = True
                write_json_atomic(roadmap_path(root), road)
            if not wake:
                return "roadmap:paused:no_progress"
            road.update(paused=None, paused_at=None, no_progress=0,
                        fingerprint=heads if heads is not None else road.get("fingerprint"),
                        job_ids=sorted(job_states), pr_ids=pr_ids)
            for key in ("job_states", "ci_fingerprint", "ci_checked_at", "wake_at"):
                road.pop(key, None)
            write_json_atomic(roadmap_path(root), road)
            missions_journal(root, {"event": "roadmap_progress_resumed", "at": iso(now())})
        else:
            return f"roadmap:paused:{road['paused']}"

    def pause(reason: str) -> str:
        road["paused"], road["paused_at"] = reason, iso(now())
        if reason == "no_progress":
            road["job_states"] = roadmap_job_states(root)
            road["pr_ids"] = roadmap_pr_ids(repo)
            road["ci_fingerprint"] = roadmap_ci_fingerprint(repo)
            road["ci_checked_at"] = iso(now())
            state = mission_status(mission_store(root, road["last_mission"]))[1] or {}
            future = [u for c in (state.get("cooldowns") or {}).values() if isinstance(c, dict)
                      if (u := parse_ts(c.get("until"))) is not None and u > now()]
            road["wake_at"] = iso(min(future)) if future else None
        write_json_atomic(roadmap_path(root), road)
        missions_journal(root, {"event": "roadmap_paused", "at": iso(now()), "reason": reason,
                                "mission": road.get("last_mission")})
        return f"roadmap:paused:{reason}"

    def queue(mission_id: str) -> str:
        # The cycle's id is recorded before its mission exists, so a crash in between cannot orphan a cycle: a
        # recorded id without a mission file is submitted again under the same id.
        mission, problem = submit(road["authority"], [{"text": ROADMAP_TASK.format(cycle=road["cycle"]),
                                                       "check": {"kind": "coordinator_cycle"}}], mission_id)
        if mission is None:
            return f"roadmap:waiting:refused ({problem})"
        missions_journal(root, {"event": "roadmap_cycle_submitted", "at": iso(now()), "cycle": road["cycle"],
                                "mission": mission_id, "authority": road["authority"]})
        return f"roadmap:submitted:{mission_id}"

    fresh: dict[str, str] = {}

    def remote_readable() -> bool:
        """Every submission needs the remote heads read in this step (spec 203), not a cached reading."""
        if "heads" not in fresh and (current := fingerprint()) is not None:
            fresh["heads"] = current
        return "heads" in fresh

    last = road.get("last_mission")
    if last and not (missions_dir(root) / f"{last}.json").exists():
        return queue(last) if remote_readable() else "roadmap:waiting:remote_unreadable"
    if last and road.get("settled") != last:
        status, state = mission_status(mission_store(root, last))
        if status not in ("complete", "cancelled"):
            return f"roadmap:waiting:{last}"
        road["settled"] = last
        if status == "cancelled":
            return pause("cycle_cancelled")
        if final_relay_status(repo, (state or {}).get("head_session")) == "blocked":
            session = (state or {}).get("head_session")
            turn = last_turn_dir(repo / ".agent-relay/sessions" / session) if session else None
            waits = sorted(set(re.findall(r"HOSTOPS-WAIT: ([0-9a-f]{32})(?![0-9a-f])", final_text(turn)))) if turn else []
            if waits:
                road["hostops_wait"] = waits
            else:
                road.pop("hostops_wait", None)
            jobs = sorted(set(re.findall(r"JOB-WAIT: (j-\d{8}T\d{6}Z-[0-9a-f]{6})(?![0-9a-f])", final_text(turn)))) if turn else []
            if jobs and not waits:
                road["job_wait"] = jobs
            else:
                road.pop("job_wait", None)
            return pause("coordinator_blocked")
        if not remote_readable():
            return "roadmap:waiting:remote_unreadable"  # nothing written: this cycle is settled again later
        jobs = roadmap_job_ids(root)
        previous_jobs = road.get("job_ids", [])
        prs = roadmap_pr_ids(repo)
        previous_prs = road.get("pr_ids")
        progressed = (fresh["heads"] != road.get("fingerprint") or bool(set(jobs) - set(previous_jobs))
                      or (prs is not None and previous_prs is not None and bool(set(prs) - set(previous_prs))))
        road["no_progress"] = 0 if progressed else road.get("no_progress", 0) + 1
        road["fingerprint"] = fresh["heads"]
        road["job_ids"] = jobs
        road["pr_ids"] = prs
        if road["no_progress"] >= ROADMAP_NO_PROGRESS:
            return pause("no_progress")
    if not remote_readable():
        return "roadmap:waiting:remote_unreadable"
    if road.get("fingerprint") is None:
        road["fingerprint"] = fresh["heads"]
        road["job_ids"] = roadmap_job_ids(root)
        road["pr_ids"] = roadmap_pr_ids(repo)
    road.update(cycle=road.get("cycle", 0) + 1, last_mission=new_mission_id(now()))
    write_json_atomic(roadmap_path(root), road)
    return queue(road["last_mission"])


def sandbox_provider_env(home: Path) -> dict[str, str]:
    """The sandbox's own provider login state. The owner pins it rather than inheriting whatever its launcher
    (systemd, a Codex shell, a Claude shell) exported: an unset CODEX_HOME sends Codex to the sandbox's empty
    tmpfs `~/.codex`, where it has no login and Relay finds no quota reading. The project-scoped GitHub login
    and its git credential helper are pinned the same way when installed: without them a mission cannot push or
    open a PR, and spec 197's `gh`/`git ls-remote` probes see no authentication."""
    state = home / ".local/share/jarvisos-sandbox/state"
    env = {"CLAUDE_CONFIG_DIR": str(state / "claude"), "CODEX_HOME": str(state / "codex")}
    if (state / "gh" / "hosts.yml").is_file():
        env["GH_CONFIG_DIR"] = str(state / "gh")
    if (state / "gitconfig").is_file():
        env["GIT_CONFIG_GLOBAL"] = str(state / "gitconfig")
    return env


def link_relay_claude_credentials(config_dir: Path, home: Path) -> None:
    """Relay 0.7.7 reads Claude's usage token only from `~/.claude/.credentials.json`, which the sandbox's
    private tmpfs home lacks. Without a Claude reading, a return to Claude after a Codex takeover is never
    confirmed. Point that one path at the sandbox's own login; an existing file or link is left alone."""
    link, target = home / ".claude" / ".credentials.json", config_dir / ".credentials.json"
    if link.exists() or link.is_symlink() or not target.exists():
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)


def command_serve(args: argparse.Namespace) -> int:
    root = control_root()
    if not in_sandbox():
        print("serve runs only inside the certified jarvis-sandbox boundary (spec 201 D1); refusing", file=sys.stderr)
        return 2
    if (root / "STOP").exists() and not args.clear_stop:
        print("mission owner is stopped; use serve --clear-stop to resume explicitly", file=sys.stderr)
        return 0
    if not become_subreaper():
        print("cannot own the processes a turn leaves behind (PR_SET_CHILD_SUBREAPER failed); refusing", file=sys.stderr)
        return 2
    owner = Lock(root / "serve.lock")
    if not owner.acquire():
        print("another mission owner is serving; refusing to start", file=sys.stderr)
        return 3
    repo = repo_root()
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    if args.clear_stop:
        (root / "STOP").unlink(missing_ok=True)
    sender = None if args.no_notify else webhook_sender()
    provider_env = sandbox_provider_env(Path.home())
    os.environ.update(provider_env)  # the read-only probes inherit it too
    link_relay_claude_credentials(Path(provider_env["CLAUDE_CONFIG_DIR"]), Path.home())
    # Relay's headless runner pins Claude to `default` and ignores the REPL permission-mode variable.
    # Its mission-only entrypoint removes that flag and adds bypassPermissions inside the OS boundary.
    claude = shutil.which("claude")
    if claude is None:
        print("Claude CLI is unavailable inside the sandbox", file=sys.stderr)
        owner.release()
        return 2
    mission_bin = Path(__file__).resolve().parent / "relay_mission_bin"
    relay_env = {"AGENT_RELAY_CLAUDE_PERMISSION_MODE": "bypassPermissions", **provider_env,
                 "JARVISOS_MISSION_PROFILE": "1", "RELAY_CLAUDE_REAL_BIN": claude,
                 "PATH": str(mission_bin) + os.pathsep + os.environ["PATH"]}
    serve_store = Store(root / "serve")
    curr_sha = release_sha()

    def log(msg: str) -> None:
        print(f"{iso(now_utc())} {msg}", file=sys.stderr, flush=True)

    def load_serve_state() -> dict[str, Any]:
        try:
            return serve_store.load()
        except StateCorrupt as exc:
            log(f"serve: state corrupt ({exc}); using defaults")
            return default_state()

    reported_release = False
    unreadable_logged: set[str] = set()

    def maybe_report_release() -> None:
        nonlocal reported_release
        if reported_release or not curr_sha:
            return
        bad_id = unreadable_mission(root)
        if bad_id is not None:
            if bad_id not in unreadable_logged:
                unreadable_logged.add(bad_id)
                log(f"owner: release {curr_sha[:12]} not reported: mission {bad_id} unreadable")
            alert_key = f"{curr_sha}:{bad_id}"
            state = load_serve_state()
            if alert_key not in state.setdefault("unreadable_alerts", []):
                safe_mission = bad_id if SAFE_MISSION_FILE_RE.fullmatch(bad_id) else "m-unreadable"
                bad_hash = hashlib.sha256(bad_id.encode("utf-8")).hexdigest()[:20]
                if Notifier(state, sender=sender, log=log).send(
                    "release_withheld", now_utc(),
                    dedupe_key=f"unreadable|{curr_sha}|{bad_hash}",
                    owner="control-plane", mission=safe_mission, sha=curr_sha,
                ):
                    state["unreadable_alerts"].append(alert_key)
                    serve_store.save(state, now_utc())
            return
        state = load_serve_state()
        state["release_sha"] = curr_sha
        state["release_updated_at"] = iso(now_utc())
        serve_store.save(state, now_utc())
        reported_release = True

    maybe_report_release()

    def factory(store: Store, mission: dict[str, Any]) -> Supervisor:
        supervisor = Supervisor(store, make_host(repo, relay_env), turns=args.turns, sender=sender)
        signal.signal(signal.SIGTERM, lambda *_: (stop.update(flag=True), setattr(supervisor, "signalled", True)))
        signal.signal(signal.SIGINT, lambda *_: (stop.update(flag=True), setattr(supervisor, "signalled", True)))
        return supervisor

    log(f"mission owner serving {missions_dir(root)} (pid {os.getpid()})")
    previous = None
    last_ff_saved = load_serve_state().get("last_ff_logged_sha")
    last_ff_logged: dict[str, str | None] = {"sha": last_ff_saved}
    try:
        while not stop["flag"] and not (root / "STOP").exists():
            _reap_zombies()  # adopted durable wrappers also finish while the owner is idle
            try:
                def send_free_model(kind: str, message: str) -> bool:
                    state = load_serve_state()
                    sent = Notifier(state, sender=sender, log=log).send_text(kind, message, now_utc())
                    serve_store.save(state, now_utc())
                    return sent
                free_model_monitor.maybe_run_daily(
                    control_dir=root, notify_fn=send_free_model if sender is not None else None, log=log)
            except Exception:  # noqa: BLE001 - monitoring must never interrupt the mission owner
                log("free model monitor: supervisor hook failed")
            if check_and_request_release_update(repo, root, log=log):
                log("owner: release install requested; stopping at task boundary without taking a new task")
                return 0
            fast_forward_main_checkout(repo, log=log, last_logged=last_ff_logged)
            if last_ff_logged.get("sha") != last_ff_saved:
                state = load_serve_state()
                state["last_ff_logged_sha"] = last_ff_logged.get("sha")
                serve_store.save(state, now_utc())
                last_ff_saved = last_ff_logged.get("sha")
            maybe_report_release()
            outcome = serve_step(root, factory, conflicts=lambda: live_conflicts(repo, {os.getpid()}), log=log)
            if outcome == "empty":
                outcome = roadmap_step(root, repo, fingerprint=lambda: remote_fingerprint(repo),
                                       submit=lambda authority, tasks, mid: submit_mission(root, repo, authority, tasks,
                                                                                           mission_id=mid),
                                       ) or outcome
            if outcome != previous:
                log(f"owner: {outcome}")
                if outcome.startswith("roadmap:paused:"):
                    state = load_serve_state()
                    Notifier(state, sender=sender, log=log).send("roadmap_paused", now_utc(), owner="control-plane",
                                                                 reason=outcome.split(":", 2)[2])
                    serve_store.save(state, now_utc())
                if outcome.startswith("waiting:"):
                    _, mission_id, reason = outcome.split(":")
                    state = load_serve_state()
                    Notifier(state, sender=sender, log=log).send(
                        "mission_waiting", now_utc(), dedupe_key=f"wait|{mission_id}|{reason}",
                        owner="control-plane", mission=mission_id,
                        task="0/0" if not (st := mission_status(mission_store(root, mission_id))[1])
                        else f"{st['mission']['next'] + 1}/{st['mission']['total']}",
                        agent=(st or {}).get("agent") or PREFERRED, reason=reason, next_check_at="none")
                    serve_store.save(state, now_utc())
            previous = outcome
            if outcome.endswith((":stopped", ":signal")) or args.once:
                break
            if (outcome.startswith("ran:") and outcome.endswith(":idle")) or outcome.startswith("roadmap:submitted:"):
                continue  # the mission completed, or a roadmap cycle was queued: take the next one now
            for _ in range(int(args.interval)):
                if stop["flag"] or (root / "STOP").exists():
                    break
                time.sleep(1)
        log("mission owner stopped")
        return 0
    finally:
        owner.release()


def new_mission_id(now: datetime) -> str:
    return f"m-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def submit_mission(root: Path, repo: Path, authority: Any, tasks: Any, *, session: str | None = None,
                   now: datetime | None = None, mission_id: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and durably queue a mission before any owner can see it. (mission, None) or (None, problem)."""
    now = now or now_utc()
    mission = {"schema": MISSION_SCHEMA, "id": mission_id or new_mission_id(now),
               "authority": authority, "tasks": tasks, "submitted_at": iso(now), "session": session, "repo": str(repo)}
    if problem := validate_mission(mission):
        return None, problem
    env = devctx.default_env(repo)
    if not any(e.get("id") == mission["authority"] and e.get("kind") == "directive" for e in devctx.read_ledger(env)):
        return None, f"{mission['authority']} is not a recorded maintainer directive"
    if session:
        turn = last_turn_dir(repo / ".agent-relay" / "sessions" / session)
        if turn is None or classify_turn(turn).outcome != "success":
            return None, "the session's last turn is not a completed successful turn"
    write_json_atomic(missions_dir(root) / f"{mission['id']}.json", mission)  # durable before any owner sees it
    missions_journal(root, {"event": "mission_submitted", "at": iso(now), "mission": mission["id"],
                            "authority": mission["authority"], "tasks": len(mission["tasks"])})
    try:
        devctx.append_entry(env, devctx.make_entry(
            env, "note", f"relay-continuity: mission {mission['id']} submitted under {mission['authority']} "
                         f"({len(mission['tasks'])} task(s)); owner: `relay_continuity.py serve`.",
            refs={"spec": "201"}, author=f"relay-continuity pid {os.getpid()}"))
    except Exception as exc:  # noqa: BLE001 - the mission file is the record; the ledger note is a courtesy
        print(f"ledger append failed: {type(exc).__name__}", file=sys.stderr)
    return mission, None


def missions_journal(root: Path, record: dict[str, Any]) -> None:
    missions_dir(root).mkdir(parents=True, exist_ok=True)
    with open(missions_dir(root) / "journal.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Records of a JSONL journal; unreadable lines are skipped (callers only look for positive evidence)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out

