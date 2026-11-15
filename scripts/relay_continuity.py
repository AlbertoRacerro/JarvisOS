#!/usr/bin/env python3
"""Relay provider continuity supervisor and Slack outbound monitor (spec 195).

Agent Relay owns sessions, transcripts and execution; this tool only reads its turn artifacts and
invokes its headless runner. It adds what Relay does not do reliably: deterministic classification
of a turn from raw evidence, ownership of every process a turn leaves behind (stopped when the turn
ends), a side-effect gate that holds only when an interrupted external operation cannot be settled
by a deterministic probe, per-provider cooldowns with failover between Claude and Codex, and
sanitized outbound notices.
Spec 201 adds durable missions: `submit` queues an ordered list of maintainer-authorized tasks and
`serve` is their single persistent owner, advancing a task only on a deterministic successful turn.

Standard library only. Never modifies the repository, Relay's configuration, permissions or
session journals. Durable state lives under ~/jarvis-control/work/continuity/.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import signal
import sys
import time
from collections.abc import Callable
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
    DIRECTIVE_ID_RE,
    devctx,
    iso,
    load_sibling,
    now_utc,
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
    reopen_unverified,
    task_outcome,
    unsettled_nonces,
)

load_sibling("relay_continuity_notify", "relay_continuity_notify.py")
from relay_continuity_notify import (  # noqa: E402
    Notifier,
    load_slack_bot,
    mention_urgent,
    poll_slack_questions,
    render,
    slack_post,
)

load_sibling("relay_continuity_supervisor", "relay_continuity_supervisor.py")
from relay_continuity_supervisor import (  # noqa: E402
    control_root,
    make_host,
    repo_root,
)

load_sibling("relay_continuity_missions", "relay_continuity_missions.py")
from relay_continuity_missions import (  # noqa: E402
    command_run,
    init_mission_state,
    load_missions,
    mission_status,
    mission_store,
    missions_dir,
    validate_check,
    webhook_sender,
    write_json_atomic,
)

load_sibling("relay_continuity_roadmap", "relay_continuity_roadmap.py")
from relay_continuity_roadmap import (  # noqa: E402
    ROADMAP_LOCK_WAIT,
    command_serve,
    load_roadmap,
    missions_journal,
    read_jsonl,
    roadmap_lock,
    roadmap_path,
    submit_mission,
)

_SIBLING_NAMES = (
    "relay_continuity_common",
    "relay_continuity_turns",
    "relay_continuity_state",
    "relay_continuity_notify",
    "relay_continuity_adapters",
    "relay_continuity_probes",
    "relay_continuity_supervisor",
    "relay_continuity_missions",
    "relay_continuity_roadmap",
)

_HERE_GLOBALS = globals()
for _mod_name in _SIBLING_NAMES:
    _mod = load_sibling(_mod_name, f"{_mod_name}.py")
    for _sym, _val in vars(_mod).items():
        if not _sym.startswith("__") and _sym != "annotations":
            _HERE_GLOBALS.setdefault(_sym, _val)

# ---------------------------------------------------------------------------
# Notify-only watch (REPL sessions and integration events)


def claude_permission_mode(output: Path) -> str | None:
    """Read the provider's effective launch mode from its first init event."""
    try:
        with output.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "system" and event.get("subtype") == "init":
                    return event.get("permissionMode")
    except OSError:
        pass
    return None


def watch_once(state: dict[str, Any], notifier: Notifier, repo: Path, now: datetime, *,
               supervised: set[str], master: Callable[[], tuple[str | None, str | None]],
               horizon: timedelta = timedelta(hours=24)) -> None:
    seen = state["watch"]["seen"]
    sessions = repo / ".agent-relay" / "sessions"
    floor = (now - horizon).strftime("%Y%m%d-%H%M%S")
    for session in sorted(sessions.glob("*-*-*")) if sessions.is_dir() else []:
        turn = last_turn_dir(session)
        if turn is None:
            continue
        if session.name in supervised:
            seen[f"{session.name}/{turn.name}"] = "supervised"  # the supervisor reports its own chain
            continue
        try:
            mtime = (turn / "state.json").stat().st_mtime
        except OSError:
            continue  # a turn without state.json is still running
        if datetime.fromtimestamp(mtime, UTC) < now - horizon and session.name < floor:
            continue
        key = f"{session.name}/{turn.name}"
        if seen.get(key):
            continue
        report = classify_turn(turn)
        seen[key] = report.outcome
        if report.agent == "claude" and claude_permission_mode(turn / "output.jsonl") == "dontAsk":
            notifier.send("repl_claude_dont_ask", now, session=session.name)
        if report.outcome != "success" and report.agent in AGENTS:
            notifier.send("repl_turn_failed", now, session=session.name, agent=report.agent,
                          outcome=report.outcome, reset_at=report.reset_at or "none")
    state["watch"]["seen"] = dict(list(seen.items())[-500:])
    sha, pr = master()
    previous = state["watch"].get("master")
    if sha and previous and sha != previous:
        fields = {"sha": sha[:12]}
        if pr:
            fields["pr"] = pr
        notifier.send("master_advanced", now, **fields)
    if sha:
        state["watch"]["master"] = sha


def real_master(repo: Path) -> tuple[str | None, str | None]:
    devctx.run(["git", "fetch", "-q", "origin", "master"], cwd=repo, timeout=60)
    code, sha = devctx.run(["git", "rev-parse", "origin/master"], cwd=repo)
    if code != 0:
        return None, None
    _, subject = devctx.run(["git", "log", "-1", "--format=%s", "origin/master"], cwd=repo)
    match = re.match(r"Merge pull request #(\d+)", subject or "")
    return sha.strip(), match.group(1) if match else None


# ---------------------------------------------------------------------------
# CLI




def command_submit(args: argparse.Namespace) -> int:
    root = control_root()
    repo = repo_root()
    try:
        spec = json.loads(Path(args.mission_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"mission file unreadable: {type(exc).__name__}", file=sys.stderr)
        return 2
    if not isinstance(spec, dict) or set(spec) - {"authority", "tasks"}:
        print("mission file takes only authority and tasks", file=sys.stderr)
        return 2
    if any(isinstance(t, dict) and (t.get("check") or {}).get("kind") == "coordinator_cycle" for t in spec.get("tasks") or []):
        print("coordinator_cycle checks are only for roadmap cycles (spec 203); use pr_merged, spec_merged or path_exists",
              file=sys.stderr)
        return 2
    mission, problem = submit_mission(root, repo, spec.get("authority"), spec.get("tasks"),
                                      session=args.continue_session)
    if mission is None:
        print(f"mission refused: {problem}", file=sys.stderr)
        return 2
    position = sum(1 for m, _, _ in load_missions(root) if m and mission_status(mission_store(root, m["id"]))[0]
                   not in ("complete", "cancelled"))
    print(json.dumps({"mission": mission["id"], "tasks": len(mission["tasks"]), "queue_position": position}))
    return 0


def command_missions(args: argparse.Namespace) -> int:
    root = control_root()
    rows = []
    for mission, mission_id, problem in load_missions(root):
        status, state = mission_status(mission_store(root, mission_id))
        rec = (state or {}).get("mission") or {}
        rows.append({"mission": mission_id, "status": "invalid" if problem else status, "problem": problem,
                     "authority": (mission or {}).get("authority"), "submitted_at": (mission or {}).get("submitted_at"),
                     "done": rec.get("next", 0), "total": len((mission or {}).get("tasks") or []),
                     "outcomes": [task_outcome(d) for d in rec.get("done") or []],
                     "verified": bool(rec.get("done")) and all(task_outcome(d) == "verified" for d in rec["done"]),
                     "contracts": rec.get("contracts") or {},
                     "agent": (state or {}).get("agent"), "head_session": (state or {}).get("head_session"),
                     "phase": (state or {}).get("phase"), "hold": (state or {}).get("hold"),
                     "cooldowns": (state or {}).get("cooldowns"), "pending": bool((state or {}).get("pending"))})
    owner = Lock(root / "serve.lock")
    serving = not owner.acquire()
    owner.release()
    print(json.dumps({"owner_serving": serving, "stop_requested": (root / "STOP").exists(),
                      "roadmap": load_roadmap(root), "missions": rows}, indent=1))
    return 0


def command_roadmap(args: argparse.Namespace) -> int:
    """Roadmap mode (spec 203): `start` under a recorded directive, `stop` pauses it, `status` shows it. A running
    cycle is never interrupted; use `stop` (the owner's) for that."""
    root = control_root()
    if args.action == "status":
        print(json.dumps(load_roadmap(root), indent=1))
        return 0
    lock = roadmap_lock(root, wait=ROADMAP_LOCK_WAIT)
    if lock is None:
        print("the owner is updating roadmap mode; try again", file=sys.stderr)
        return 3
    try:
        return _command_roadmap(args, root)
    finally:
        lock.release()


def _command_roadmap(args: argparse.Namespace, root: Path) -> int:
    road = load_roadmap(root)
    if road and road.get("corrupt"):
        print(f"{roadmap_path(root)} is unreadable; inspect it before changing roadmap mode", file=sys.stderr)
        return 2
    road = road or {}
    at = iso(now_utc())
    if args.action == "start":
        if not DIRECTIVE_ID_RE.fullmatch(args.authority or ""):
            print("--authority must be a maintainer directive ledger id", file=sys.stderr)
            return 2
        env = devctx.default_env(repo_root())
        if not any(e.get("id") == args.authority and e.get("kind") == "directive" for e in devctx.read_ledger(env)):
            print(f"{args.authority} is not a recorded maintainer directive", file=sys.stderr)
            return 2
        road.pop("hostops_wait", None)
        road.pop("job_wait", None)
        road.update(enabled=True, authority=args.authority, started_at=at, paused=None, paused_at=None, no_progress=0)
        record = {"event": "roadmap_started", "at": at, "authority": args.authority}
    else:
        if not road.get("enabled") or not args.note:
            print("roadmap stop needs a started roadmap and --note", file=sys.stderr)
            return 2
        road.update(paused="maintainer_stop", paused_at=at, note=args.note)
        record = {"event": "roadmap_paused", "at": at, "reason": "maintainer_stop", "note": args.note}
    write_json_atomic(roadmap_path(root), road)
    missions_journal(root, record)
    print(json.dumps(road, indent=1))
    return 0


def command_cancel(args: argparse.Namespace) -> int:
    root = control_root()
    lock = Lock(root / "supervisor.lock")
    if not lock.acquire():
        print("the mission is executing; `stop` the owner first (a running turn is never killed)", file=sys.stderr)
        return 3
    try:
        if not (missions_dir(root) / f"{args.mission}.json").exists():
            print("no such mission", file=sys.stderr)
            return 2
        store = mission_store(root, args.mission)
        state = store.load()
        if state.get("pending"):
            print("the mission has an unsettled launched attempt; it must be resolved before cancelling", file=sys.stderr)
            return 3
        rec = state.get("mission") or {"id": args.mission, "next": 0, "total": 0, "done": []}
        rec["cancelled_at"] = iso(now_utc())
        state["mission"] = rec
        store.save(state, now_utc())
        store.journal({"event": "mission_cancelled", "at": iso(now_utc()), "mission": args.mission, "note": args.note[:500]})
        print(json.dumps({"cancelled": args.mission}))
        return 0
    finally:
        lock.release()


def command_contract(args: argparse.Namespace) -> int:
    """Spec 204: attach a completion contract to a pending task whose immutable definition has none (a legacy
    mission). It only ever makes completion depend on independent evidence; it never marks anything complete."""
    root = control_root()
    lock = Lock(root / "supervisor.lock")
    if not lock.acquire():
        print("the mission is executing; `stop` the owner first (a running turn is never killed)", file=sys.stderr)
        return 3
    try:
        mission = next((m for m, mid, _ in load_missions(root) if m and mid == args.mission), None)
        if mission is None:
            print("no such valid mission", file=sys.stderr)
            return 2
        try:
            check = json.loads(args.check)
        except json.JSONDecodeError:
            print("--check must be a JSON object", file=sys.stderr)
            return 2
        if problem := validate_check(check):
            print(f"contract refused: {problem}", file=sys.stderr)
            return 2
        if devctx.find_secret(args.note):
            print("note matches a secret pattern; refusing", file=sys.stderr)
            return 2
        index = args.task - 1
        if not 0 <= index < len(mission["tasks"]):
            print("no such task", file=sys.stderr)
            return 2
        if mission["tasks"][index].get("check"):
            print("the task's definition already carries its contract; definitions are immutable", file=sys.stderr)
            return 2
        if check["kind"] == "coordinator_cycle" and not any(
                r.get("event") == "roadmap_cycle_submitted" and r.get("mission") == args.mission
                for r in read_jsonl(missions_dir(root) / "journal.jsonl")):
            print("coordinator_cycle is only for a recorded roadmap cycle mission", file=sys.stderr)
            return 2
        store = mission_store(root, args.mission)
        status, state = mission_status(store)
        if status in ("complete", "cancelled", "corrupt"):
            print(f"the mission is {status}", file=sys.stderr)
            return 2
        state = init_mission_state(store, mission, state, now_utc)
        rec = state["mission"]
        if not state.get("pending") and (reopened := reopen_unverified(state)):
            # The owner would reopen these anyway; a reopened task may take a contract.
            store.journal({"event": "mission_reopened", "at": iso(now_utc()), "mission": args.mission, "tasks": reopened,
                           "why": "counted complete before spec 204 on a provider return alone"})
        if index < rec["next"]:
            print(f"task {args.task} is already recorded complete", file=sys.stderr)
            return 2
        rec.setdefault("contracts", {})[str(index)] = check
        store.save(state, now_utc())
        store.journal({"event": "contract_attached", "at": iso(now_utc()), "mission": args.mission, "task": index,
                       "check": check, "note": args.note[:500]})
        print(json.dumps({"mission": args.mission, "task": args.task, "check": check}))
        return 0
    finally:
        lock.release()


def command_classify(args: argparse.Namespace) -> int:
    print(json.dumps(classify_turn(Path(args.turn_dir)).to_json(), indent=1))
    return 0


def command_status(args: argparse.Namespace) -> int:
    store = Store(control_root())
    try:
        state = store.load()
    except StateCorrupt as exc:
        print(f"state corrupt: {exc}")
        return 4
    view = {k: state.get(k) for k in ("phase", "head_session", "agent", "hold", "cooldowns", "counters",
                                      "pending", "last_outcome", "switches", "updated_at", "wait")}
    view["chain_length"] = len(state.get("chain") or [])
    view["stop_requested"] = store.stop_path.exists()
    view["notify"] = {k: state["notify"].get(k) for k in ("failures", "dropped", "last_digest")}
    print(json.dumps(view, indent=1))
    return 0


def command_stop(args: argparse.Namespace) -> int:
    store = Store(control_root())
    store.root.mkdir(parents=True, exist_ok=True)
    store.stop_path.write_text(iso(now_utc()) or "")
    print("stop requested; the supervisor exits at its next safe point and never kills a running turn")
    return 0


def command_release(args: argparse.Namespace) -> int:
    root = control_root()
    lock = Lock(root / "supervisor.lock")
    if not lock.acquire():
        print("a supervisor is running; stop it before releasing", file=sys.stderr)
        return 3
    try:
        if devctx.find_secret(args.note):
            print("note matches a secret pattern; refusing", file=sys.stderr)
            return 2
        if args.mission and not (missions_dir(root) / f"{args.mission}.json").exists():
            print("no such mission", file=sys.stderr)
            return 2
        store = mission_store(root, args.mission) if args.mission else Store(root)
        state = store.load()
        released = state.get("hold")
        advanced = None
        if args.advance:
            rec = state.get("mission")
            if not args.mission or rec is None or rec["next"] >= rec["total"]:
                print("--advance needs a mission with an incomplete task", file=sys.stderr)
                return 2
            # The maintainer records the current task done (e.g. after a failed check was resolved by hand).
            advanced = rec["next"]
            rec["done"].append({"task": advanced, "session": None, "agent": None, "at": iso(now_utc()), "by": "maintainer",
                                "outcome": "maintainer_attested", "note": args.note[:500]})
            rec["next"] += 1
        if (released or {}).get("reason") == "task_checkpoint_limit" and (rec := state.get("mission")):
            (rec.get("checkpoints") or {}).pop(str(rec["next"]), None)  # the maintainer allows a fresh bound
        held_turn = (released or {}).get("turn")
        previous = state.get("acknowledged") or {}
        if held_turn:
            same = previous.get("turn") == held_turn
            state["acknowledged"] = {
                "turn": held_turn,
                "reasons": sorted(set(previous.get("reasons") or [] if same else []) | {released["reason"]}),
                "cleared": sorted(set(previous.get("cleared") or [] if same else []) | set(args.clear_cooldown or [])),
            }
        elif args.clear_cooldown and previous.get("turn"):
            previous["cleared"] = sorted(set(previous.get("cleared") or []) | set(args.clear_cooldown))
        state["hold"] = None
        state["phase"] = "idle"
        kept_clears = {k: v for k, v in (state.get("counters") or {}).items()
                       if k.startswith("reading_clear_") and k.removeprefix("reading_clear_") not in (args.clear_cooldown or [])}
        state["counters"] = {"consecutive_failures": 0, "transient_streak": 0, "crash_retries": 0, **kept_clears}
        for agent in args.clear_cooldown or []:
            state["cooldowns"].pop(agent, None)
            state["spend_caps"].pop(agent, None)  # e.g. the maintainer raised a spend cap
        dropped = []
        if args.drop_pending:
            dropped = unsettled_nonces(store.journal_path)
            state["pending"] = None
        if (released or {}).get("reason") == "permissions_changed":
            # The maintainer reviewed the new authority; the next run pins it.
            state["permissions_pinned"] = False
        store.save(state, now_utc())
        store.journal({"event": "release", "at": iso(now_utc()), "hold": released, "note": args.note[:500],
                       "cleared": args.clear_cooldown or [], "dropped_pending": bool(args.drop_pending),
                       "dropped": dropped, "mission": args.mission, "advanced": advanced})
        try:
            make_host(repo_root()).ledger("decision", f"relay-continuity hold released ({(released or {}).get('reason')}): "
                                                       f"{args.note[:400]}")
        except Exception as exc:  # noqa: BLE001
            print(f"ledger append failed: {type(exc).__name__}", file=sys.stderr)
        print(json.dumps({"released": released, "cleared": args.clear_cooldown or [], "mission": args.mission,
                          "advanced": advanced}))
        return 0
    finally:
        lock.release()


def load_observe() -> Any:
    """The spec 205 semantic adapter, sharing this module rather than loading a second copy of it."""
    sys.modules.setdefault("relay_continuity", sys.modules[__name__])
    spec = importlib.util.spec_from_file_location("relay_observe", Path(__file__).resolve().parent / "relay_observe.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def command_notify_test(args: argparse.Namespace) -> int:
    sender = webhook_sender()
    if sender is None:
        print("no usable webhook configured")
        return 2
    if args.digest or args.alert:
        observe, root, repo, now = load_observe(), control_root(), repo_root(), now_utc()
        snap = observe.snapshot(root, repo, now)
        since = iso(now - timedelta(hours=args.hours))
        text = (observe.digest_text(snap, since=since, merges=observe.merges_since(repo, since)) if args.digest else
                observe.alert_text("prova di allerta (nessun incidente reale): questo è il formato degli avvisi urgenti", snap))
    else:
        text = render("test", {}) or ""
    if devctx.find_secret(text):
        print("refusing: the message matches a secret pattern")
        return 1
    error = sender(text)
    print("sent" if error is None else f"failed: {error}")
    if error is None and (args.digest or args.alert):
        print(text)
    return 0 if error is None else 1


def mission_sessions(root: Path) -> set[str]:
    """Sessions of every mission's chain: the owner reports those itself, so the REPL watcher must not."""
    found: set[str] = set()
    for _, mission_id, _ in load_missions(root):
        status, state = mission_status(mission_store(root, mission_id))
        found.update((state or {}).get("chain") or [])
    return found


def command_watch(args: argparse.Namespace) -> int:
    if not 1 <= args.digest_hours <= 4:
        print("--digest-hours must be between 1 and 4", file=sys.stderr)
        return 2
    root = control_root()
    lock = Lock(root / "watch.lock")
    if not lock.acquire():
        print("another watch instance is running", file=sys.stderr)
        return 3
    repo = repo_root()
    store = Store(root / "watch")
    observe = load_observe()
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    log = lambda m: print(f"{iso(now_utc())} {m}", file=sys.stderr, flush=True)  # noqa: E731
    logged_corrupt: set[str] = set()

    def log_corrupt(where: str, exc: Exception) -> None:
        msg = f"{where}: state corrupt ({exc}); using defaults"
        if msg not in logged_corrupt:
            logged_corrupt.add(msg)
            log(msg)

    try:
        while not stop["flag"]:
            now = now_utc()
            try:
                state = store.load()
            except StateCorrupt as exc:
                log_corrupt("watch", exc)
                state = default_state()
            notifier = Notifier(state, sender=None if args.no_notify else webhook_sender(), log=log)
            try:
                supervised_state = Store(root).load()
            except StateCorrupt as exc:
                log_corrupt("supervisor", exc)
                supervised_state = default_state()
            try:
                owned = mission_sessions(root)
            except Exception as exc:  # noqa: BLE001 - an odd mission store never crash-loops the watcher
                owned = set()
                log(f"watch: mission sessions unreadable ({type(exc).__name__})")
            supervised = set(supervised_state.get("chain") or []) | owned
            first = not state["watch"].get("seen")
            if first:
                # Baseline: never alert on history that predates this watch.
                watch_once(state, Notifier(state, sender=None, log=lambda m: None), repo, now, supervised=supervised,
                           master=lambda: real_master(repo))
            else:
                notifier.retry_pending(now)
                watch_once(state, notifier, repo, now, supervised=supervised, master=lambda: real_master(repo))
            # Spec 205: incidents are announced on detection, the semantic digest every --digest-hours.
            try:
                obs = state["watch"].setdefault("observe", {})
                bot = None if args.no_notify else load_slack_bot()
                webhook = None if args.no_notify or bot else webhook_sender()

                def send_question(_qid: str, message: str, bot: Any = bot, webhook: Any = webhook) -> str | None:
                    if bot:
                        return slack_post(bot, mention_urgent(f"question|{_qid}", message, bot))
                    return "webhook" if webhook and webhook(message) is None else None

                observe.observe_step(obs, observe.snapshot(root, repo, now), now,
                                     send=lambda kind, text, n=notifier, t=now, b=bot:
                                         n.send_text(kind, mention_urgent(kind, text, b), t),
                                     digest_every=timedelta(hours=args.digest_hours),
                                     merges=lambda since: observe.merges_since(repo, since),
                                     failures=int(state["notify"].get("failures") or 0),
                                     question_send=send_question if bot or webhook else None)
                if bot:
                    poll_slack_questions(obs, bot, root, repo, observe, now)
            except Exception as exc:  # noqa: BLE001 - observation never stops the watcher
                log(f"observe: internal error {type(exc).__name__}")
            store.save(state, now)
            if args.once:
                break
            for _ in range(int(args.interval)):
                if stop["flag"]:
                    break
                time.sleep(1)
        return 0
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay_continuity.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("run", help="supervise continuation of one Relay session chain")
    p.add_argument("--session", required=True)
    p.add_argument("--task-file", help="maintainer task for the next turn (when the chain is idle)")
    p.add_argument("--turns", type=int, default=1, help="Relay turns per invocation (default 1)")
    p.add_argument("--no-notify", action="store_true")
    p.set_defaults(func=command_run)
    p = sub.add_parser("classify", help="classify one Relay turn directory")
    p.add_argument("turn_dir")
    p.set_defaults(func=command_classify)
    sub.add_parser("status", help="show durable supervisor state").set_defaults(func=command_status)
    sub.add_parser("stop", help="request a graceful stop").set_defaults(func=command_stop)
    p = sub.add_parser("release", help="clear a manual hold after maintainer review")
    p.add_argument("--note", required=True, help="why it is safe to continue (recorded)")
    p.add_argument("--clear-cooldown", action="append", choices=AGENTS)
    p.add_argument("--drop-pending", action="store_true", help="forget an unresolved launched attempt")
    p.add_argument("--mission", help="release a mission's hold instead of the `run` chain's")
    p.add_argument("--advance", action="store_true", help="also record the mission's current task done")
    p.set_defaults(func=command_release)
    p = sub.add_parser("submit", help="queue a mission of maintainer-authorized tasks (spec 201)")
    p.add_argument("--mission-file", required=True, help='JSON: {"authority": "<directive id>", "tasks": [...]}')
    p.add_argument("--continue-session", help="continue this idle session instead of opening a new one")
    p.set_defaults(func=command_submit)
    p = sub.add_parser("serve", help="persistent single owner that executes queued missions")
    p.add_argument("--interval", type=float, default=60.0, help="seconds between scheduling checks")
    p.add_argument("--turns", type=int, default=1, help="Relay turns per invocation (default 1)")
    p.add_argument("--once", action="store_true", help="one scheduling step, then exit")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--clear-stop", action="store_true", help="explicitly resume after an operator stop request")
    p.set_defaults(func=command_serve)
    sub.add_parser("missions", help="show every mission and the owner").set_defaults(func=command_missions)
    p = sub.add_parser("roadmap", help="roadmap continuation cycles through the owner (spec 203)")
    p.add_argument("action", choices=("start", "stop", "status"))
    p.add_argument("--authority", help="maintainer directive ledger id (start)")
    p.add_argument("--note", help="why roadmap mode is paused (stop)")
    p.set_defaults(func=command_roadmap)
    p = sub.add_parser("contract", help="attach a completion contract to a legacy task without one (spec 204)")
    p.add_argument("--mission", required=True)
    p.add_argument("--task", type=int, required=True, help="1-based task number")
    p.add_argument("--check", required=True, help='JSON, e.g. {"kind": "spec_merged", "spec": "204"}')
    p.add_argument("--note", required=True, help="why this evidence proves the task (recorded)")
    p.set_defaults(func=command_contract)
    p = sub.add_parser("cancel", help="cancel a mission that is not executing")
    p.add_argument("mission")
    p.add_argument("--note", required=True)
    p.set_defaults(func=command_cancel)
    p = sub.add_parser("notify-test", help="send one test notification (fixed, or the current digest/alert format)")
    p.add_argument("--digest", action="store_true", help="send the semantic digest of the current owner state")
    p.add_argument("--alert", action="store_true", help="send a sample urgent alert (marked as a test)")
    p.add_argument("--hours", type=float, default=2.0, help="digest look-back window")
    p.set_defaults(func=command_notify_test)
    p = sub.add_parser("watch", help="host-side observer: owner incidents, semantic digest, REPL sessions and master")
    p.add_argument("--interval", type=float, default=60.0)
    p.add_argument("--digest-hours", type=float, default=2.0, help="semantic digest interval, 1-4 hours")
    p.add_argument("--once", action="store_true")
    p.add_argument("--no-notify", action="store_true")
    p.set_defaults(func=command_watch)
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
