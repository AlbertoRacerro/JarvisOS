#!/usr/bin/env python3
"""Durable registered jobs (spec 218): long, bounded, attributable work whose lifetime is not a coordinator turn.

The mission owner's reaper stops everything a finished turn left running (spec 195). A job registered here is the
one exception: its wrapper process records who asked for it, under which authority, from which commit and inputs,
with which limits, and the reaper leaves that process tree alone while the record proves it is still that wrapper.
The wrapper itself enforces the wall-time limit and cancellation and records the exit, so no turn and no owner tick
has to watch it. A later turn reads the result with `list`/`show`/`log`.

Jobs live inside the owner's process namespace. Missing wrappers are never solver outcomes;
readers distinguish verified loss, surviving children and uncertain namespace visibility.
Stdlib only.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = "relay-jobs.v1"
DEFAULT_CONTROL = Path.home() / "jarvis-control" / "work" / "continuity"
ID_RE = re.compile(r"j-\d{8}T\d{6}Z-[0-9a-f]{6}")
ACTIVE = ("starting", "running")
UNRESOLVED = ("orphaned", "unknown")
TERMINAL = ("succeeded", "failed", "cancelled", "timeout", "interrupted", "lost")
MAX_RUNNING = 4
MAX_WALL_HOURS = 24.0
STOP_GRACE = 30.0  # seconds a job gets to exit on SIGTERM before SIGKILL
POLL = 2.0
LOST_GRACE = timedelta(minutes=15)
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.:/+-]{0,79}")


def control_root() -> Path:
    return Path(os.environ.get("RELAY_CONTINUITY_DIR", DEFAULT_CONTROL)).expanduser()


def jobs_dir(root: Path | None = None) -> Path:
    return (root or control_root()) / "jobs"


def allowed_roots() -> tuple[Path, ...]:
    home = Path.home()
    return (home / "src", home / "jarvis-control" / "work")


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pid_start(pid: int, proc_root: Path = Path("/proc")) -> int | None:
    try:
        return int((proc_root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    with open(tmp, "x", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def job_path(job_id: str, root: Path | None = None) -> Path:
    if not ID_RE.fullmatch(job_id):
        raise ValueError(f"not a job id: {job_id!r}")
    return jobs_dir(root) / f"{job_id}.json"


def load(job_id: str, root: Path | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads(job_path(job_id, root).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA and data.get("id") == job_id else None


def all_jobs(root: Path | None = None) -> list[dict[str, Any]]:
    found = []
    for path in sorted(jobs_dir(root).glob("j-*.json")):
        if (job := load(path.stem, root)) is not None:
            found.append(job)
    return found


# Cache only PID translations; every use revalidates identity. Missing mappings are
# rescanned at most once per 10 seconds, including when several readers poll a job.
_PID_CACHE: dict[tuple[str, str, int], tuple[float, int | None]] = {}
_VISIBLE_NAMESPACES: set[tuple[str, str, str]] = set()


def process_identity(pid: int, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    base = proc_root / str(pid)
    try:
        return {"pid": pid, "pid_start": pid_start(pid, proc_root),
                "pid_namespace": os.readlink(base / "ns/pid"),
                "boot_id": (proc_root / "sys/kernel/random/boot_id").read_text().strip()}
    except OSError:
        return {"pid": pid, "pid_start": pid_start(pid, proc_root)}


def _matches(job: dict[str, Any], pid: int, proc_root: Path, *, child: bool = False) -> bool:
    identity = job.get("child", {}) if child else job
    if pid_start(pid, proc_root) != identity.get("pid_start") or identity.get("pid_start") is None:
        return False
    base = proc_root / str(pid)
    try:
        if identity.get("pid_namespace") and os.readlink(base / "ns/pid") != identity["pid_namespace"]:
            return False
        if identity.get("boot_id") and (proc_root / "sys/kernel/random/boot_id").read_text().strip() != identity["boot_id"]:
            return False
        if child:
            # Popen established lineage. Exec changes argv but preserves these
            # immutable kernel identifiers; cmdline must not revoke ownership.
            return bool(identity.get("pid_namespace") and identity.get("boot_id"))
        argv = (base / "cmdline").read_bytes().split(b"\0")
        # The script must be an argv element, not merely text embedded in another command.
        return any(Path(os.fsdecode(arg)).name == "relay_jobs.py" and argv[i + 1:i + 3] ==
                   [b"_run", job["id"].encode()] for i, arg in enumerate(argv))
    except (OSError, ValueError):
        return False


def _remember_namespace(identity: dict[str, Any], proc_root: Path) -> None:
    try:
        if identity.get("pid_namespace"):
            _VISIBLE_NAMESPACES.add((str(proc_root), os.readlink(proc_root / "self/ns/pid"), identity["pid_namespace"]))
    except OSError:
        return


def observed_pid(job: dict[str, Any], proc_root: Path = Path("/proc"), *, child: bool = False) -> int | None:
    identity = job.get("child", {}) if child else job
    pid = identity.get("pid")
    if not isinstance(pid, int) or not isinstance(identity.get("pid_start"), int):
        return None
    if _matches(job, pid, proc_root, child=child):
        _remember_namespace(identity, proc_root)
        return pid
    key = (str(proc_root), job["id"] + (":child" if child else ""), identity["pid_start"])
    stamp, cached = _PID_CACHE.get(key, (float("-inf"), None))
    if cached is not None and _matches(job, cached, proc_root, child=child):
        _remember_namespace(identity, proc_root)
        return cached
    if time.monotonic() - stamp < 10:
        return None
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            nspid = next(line.split()[1:] for line in (entry / "status").read_text().splitlines()
                         if line.startswith("NSpid:"))
        except (OSError, StopIteration):
            continue
        # NSpid alone never proves ownership: start time, namespace/boot (new
        # records) and the wrapper argv or child's pinned command must also match.
        if str(pid) in nspid and _matches(job, int(entry.name), proc_root, child=child):
            _remember_namespace(identity, proc_root)
            _PID_CACHE[key] = (time.monotonic(), int(entry.name))
            return int(entry.name)
    _PID_CACHE[key] = (time.monotonic(), None)
    return None


def _alive(pid: int, proc_root: Path) -> bool:
    try:
        return (proc_root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (OSError, IndexError):
        return False


def is_wrapper(job: dict[str, Any], proc_root: Path = Path("/proc")) -> bool:
    pid = observed_pid(job, proc_root)
    return pid is not None and _alive(pid, proc_root)


def effective_status(job: dict[str, Any], proc_root: Path = Path("/proc")) -> str:
    """Persistent outcomes prevail; absence never implies solver success/failure."""
    status = str(job.get("status"))
    if status not in ACTIVE + UNRESOLVED:
        return status
    if is_wrapper(job, proc_root):
        return "running"
    child = observed_pid(job, proc_root, child=True)
    if child is not None and (_alive(child, proc_root) or _descendants(child, proc_root)):
        return "orphaned"
    if status == "starting":
        try:
            young = now_utc() - datetime.strptime(str(job.get("created_at")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) < timedelta(seconds=30)
        except ValueError:
            young = False
        if young:
            return "starting"
    # A reader in another/hidden namespace cannot prove absence. Cache this
    # uncertainty rather than scanning the process table again on every poll.
    if job.get("pid_namespace"):
        try:
            observer = os.readlink(proc_root / "self/ns/pid")
            if observer != job["pid_namespace"] and (str(proc_root), observer, job["pid_namespace"]) not in _VISIBLE_NAMESPACES:
                return "unknown"
        except OSError:
            return "unknown"
    return "lost"


def parse_iso(ts: str | None) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        try:
            return datetime.fromisoformat(ts).astimezone(UTC)
        except ValueError:
            return None


def _is_stale_unknown(job: dict[str, Any], status: str, now: datetime) -> bool:
    """An `unknown` record past its deadline by more than LOST_GRACE: its wrapper would have ended it (spec 254)."""
    if status != "unknown":
        return False
    deadline = parse_iso(job.get("deadline"))
    return deadline is not None and (now - deadline > LOST_GRACE)


def live_roots(root: Path | None = None, proc_root: Path = Path("/proc")) -> set[int]:
    """Reaper exemptions are verified local process IDs, never stored namespace IDs."""
    roots = set()
    for job in all_jobs(root):
        if job.get("status") not in ACTIVE + UNRESOLVED:
            continue
        for child in (False, True):
            pid = observed_pid(job, proc_root, child=child)
            if pid is not None:
                if _alive(pid, proc_root):
                    roots.add(pid)
                elif child:
                    roots.update(_descendants(pid, proc_root))
    return roots


def _git_state(cwd: Path) -> dict[str, Any]:
    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None
    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    return {"head": head, "dirty": None if status is None else bool(status)}


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == r or r in path.parents for r in (r.resolve() for r in roots))


def ledger(kind: str, text: str, refs: dict[str, Any]) -> None:
    """Best effort: an attributable ledger line next to the job record (the record stays the authority)."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("devctx", Path(__file__).resolve().parent / "devctx.py")
        if spec is None or spec.loader is None:
            return
        devctx = sys.modules.get("devctx") or importlib.util.module_from_spec(spec)
        if "devctx" not in sys.modules:
            sys.modules["devctx"] = devctx
            spec.loader.exec_module(devctx)
        env = devctx.default_env(Path(refs.get("cwd") or "."))
        clean = {k: v for k, v in refs.items() if k in ("spec", "sha", "branch", "evidence") and v}
        devctx.append_entry(env, devctx.make_entry(env, kind, text, refs=clean, author=f"relay-jobs pid {os.getpid()}"))
    except Exception:  # noqa: BLE001 - the ledger line is a convenience, never a reason to fail the job
        pass


def start(argv: list[str], *, label: str, authority: str, cwd: Path, wall_hours: float, spec: str | None = None,
          inputs: list[Path] | None = None, cores: int | None = None, root: Path | None = None,
          spawn: Callable[[list[str]], Any] | None = None, wait: float = 10.0, retry_reason: str | None = None) -> dict[str, Any]:
    """Register and launch one job. Raises ValueError on a refused request; returns the job record."""
    if not argv:
        raise ValueError("no command given")
    if not LABEL_RE.fullmatch(label):
        raise ValueError("label must be 1-80 characters of letters, digits, space and _.:/+-")
    if not authority.strip() or len(authority) > 300:
        raise ValueError("authority must name the spec/directive/ledger entry that authorizes this job (<= 300 chars)")
    if not (0 < wall_hours <= MAX_WALL_HOURS):
        raise ValueError(f"wall-hours must be in (0, {MAX_WALL_HOURS:g}]")
    cwd = cwd.resolve()
    if not cwd.is_dir() or not _within(cwd, allowed_roots()):
        raise ValueError(f"cwd must be a directory under {', '.join(str(r) for r in allowed_roots())}")
    digests = {}
    for item in inputs or []:
        path = (cwd / item).resolve() if not item.is_absolute() else item.resolve()
        if not path.is_file():
            raise ValueError(f"input is not a file: {item}")
        digests[str(path)] = _digest(path)
    created = now_utc()
    job_id = f"j-{created.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
    log = jobs_dir(root) / f"{job_id}.log"
    job: dict[str, Any] = {
        "schema": SCHEMA, "id": job_id, "label": label, "authority": authority.strip(), "spec": spec,
        "argv": argv, "cwd": str(cwd), "git": _git_state(cwd), "inputs": digests, "cores": cores,
        "wall_s": int(wall_hours * 3600), "created_at": iso(created),
        "deadline": iso(created + timedelta(seconds=int(wall_hours * 3600))),
        "requester": f"pid {os.getppid()}", "log": str(log), "status": "starting",
        "pid": None, "pid_start": None, "exit_code": None, "ended_at": None, "retry_reason": retry_reason,
    }
    jobs_dir(root).mkdir(parents=True, exist_ok=True)
    with open(jobs_dir(root) / ".admission.lock", "a+") as admission:
        # Counting and registering are one step: concurrent starts cannot both take the last slot. A fresh
        # `starting` record counts as active, so the slot stays reserved until its wrapper is up.
        fcntl.flock(admission, fcntl.LOCK_EX)
        previous = all_jobs(root)
        running = [j for j in previous if (status := effective_status(j)) in ACTIVE + UNRESOLVED
                   and not _is_stale_unknown(j, status, created)]
        duplicate = next((j for j in sorted(previous, key=lambda j: j not in running) if j.get("argv") == argv and j.get("cwd") == str(cwd)
                          and j.get("inputs") == digests and j.get("git") == job["git"]), None)
        if duplicate is not None and (duplicate in running or not retry_reason or not retry_reason.strip()):
            raise ValueError(f"matching job already running or recorded: {duplicate['id']} (inspect its evidence before retrying)")
        if len(running) >= MAX_RUNNING:
            raise ValueError(f"{len(running)} jobs already running (limit {MAX_RUNNING}): "
                             + ", ".join(j["id"] for j in running))
        write_json_atomic(job_path(job_id, root), job)
    command = [sys.executable, "-I", str(Path(__file__).resolve()), "_run", job_id]
    env = dict(os.environ)
    if root is not None:
        env["RELAY_CONTINUITY_DIR"] = str(root)
    if spawn is not None:
        spawn(command)
    else:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True, env=env, cwd=str(cwd))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        current = load(job_id, root) or job
        if current.get("status") != "starting":
            job = current
            break
        time.sleep(0.1)
    ledger("attempt", f"Durable job {job_id} ({label}) started under {authority.strip()[:160]}: "
           f"{len(argv)}-arg command in {cwd}, wall limit {wall_hours:g} h, log {log}.",
           {"spec": spec, "sha": job["git"].get("head"), "cwd": str(cwd), "evidence": str(log)})
    return job


def run_wrapper(job_id: str, root: Path | None = None, *, poll: float = POLL, grace: float = STOP_GRACE) -> int:
    """The job's own process: launch the command, enforce deadline and cancellation, record the outcome."""
    job = load(job_id, root)
    if job is None or job.get("status") != "starting":
        return 2
    if not become_subreaper():
        # Without it a detached descendant would escape the wall-time limit; refuse rather than run unbounded.
        job.update(status="failed", ended_at=iso(now_utc()), reason="cannot own descendants (PR_SET_CHILD_SUBREAPER)")
        write_json_atomic(job_path(job_id, root), job)
        return 1
    cancel_marker = jobs_dir(root) / f"{job_id}.cancel"
    stopping = {"why": None}
    signal.signal(signal.SIGTERM, lambda *_: stopping.update(why="interrupted"))
    signal.signal(signal.SIGINT, lambda *_: stopping.update(why="interrupted"))
    job.update(**process_identity(os.getpid()), status="running", started_at=iso(now_utc()))
    write_json_atomic(job_path(job_id, root), job)
    deadline = datetime.strptime(job["deadline"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    with open(job["log"], "ab") as log:
        try:
            child = subprocess.Popen(job["argv"], cwd=job["cwd"], stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True,
                                     preexec_fn=lambda: os.nice(10))
        except OSError as exc:
            log.write(f"[relay-jobs] cannot start: {type(exc).__name__}: {exc}\n".encode())
            job.update(status="failed", exit_code=None, ended_at=iso(now_utc()), reason="cannot start")
            write_json_atomic(job_path(job_id, root), job)
            return 1
        why = None
        try:
            job["child"] = process_identity(child.pid)
            write_json_atomic(job_path(job_id, root), job)
            while child.poll() is None:
                if stopping["why"]:
                    why = stopping["why"]
                elif cancel_marker.exists():
                    why = "cancelled"
                elif now_utc() >= deadline:
                    why = "timeout"
                if why:
                    break
                time.sleep(poll)
        except Exception as exc:  # cleanup still owns every descendant if observation/persistence fails
            why = "interrupted"
            job["reason"] = f"wrapper error: {type(exc).__name__}"
            log.write(f"[relay-jobs] {job['reason']}\n".encode())
        finally:
            # Cleanup precedes every terminal publication, also on wrapper errors.
            leftovers = _stop_tree(child, grace if why else min(grace, 10.0))
        code = child.wait()
        status = why or ("succeeded" if code == 0 else "failed")
        if leftovers:
            log.write(f"[relay-jobs] stopped {leftovers} leftover process(es)\n".encode())
        log.write(f"[relay-jobs] {status} (exit {code})\n".encode())
    job.update(status=status, exit_code=code, ended_at=iso(now_utc()), leftovers_stopped=leftovers)
    write_json_atomic(job_path(job_id, root), job)
    cancel_marker.unlink(missing_ok=True)
    ledger("finding", f"Durable job {job_id} ({job['label']}) ended {status} (exit {code}); log {job['log']}.",
           {"spec": job.get("spec"), "sha": (job.get("git") or {}).get("head"), "cwd": job["cwd"],
            "evidence": job["log"]})
    return 0


def become_subreaper() -> bool:
    try:
        import ctypes
        return ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError):
        return False


def _descendants(pid: int, proc_root: Path = Path("/proc")) -> set[int]:
    """Every process below `pid` that is still running (zombies excluded)."""
    found: set[int] = set()
    todo = [pid]
    while todo:
        try:
            tasks = list((proc_root / str(todo.pop()) / "task").iterdir())
        except OSError:
            continue
        for task in tasks:
            try:
                children = {int(c) for c in (task / "children").read_text().split()}
            except (OSError, ValueError):
                continue
            todo.extend(children - found)
            found |= children
    alive = set()
    for c in found:
        try:
            if (proc_root / str(c) / "stat").read_text().rsplit(")", 1)[1].split()[0] not in ("Z", "X"):
                alive.add(c)
        except (OSError, IndexError):
            pass
    return alive


def _stop_tree(child: subprocess.Popen[bytes], grace: float) -> int:
    """Stop every process below the wrapper (the command and anything it left behind): SIGTERM, then SIGKILL once
    `grace` has passed, until none is left. The command's own exit status is collected by polling it, never by a
    blind wait, so it is not lost. Does not return while any descendant survives (one stuck in uninterruptible
    I/O, say): the job stays `running` until it has gone. Returns how many processes other than the command had
    to be stopped."""
    others: set[int] = set()
    end, sig = time.monotonic() + grace, signal.SIGTERM
    for _ in range(int(grace * 10) + 300):
        child.poll()
        live = _descendants(os.getpid())
        if not live:
            break
        others |= live - {child.pid}
        for pid in live:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.1)
        if time.monotonic() >= end:
            sig = signal.SIGKILL
    while live := _descendants(os.getpid()):
        child.poll()
        others |= live - {child.pid}
        for pid in live:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(1.0)
    return len(others)


def cancel(job_id: str, root: Path | None = None) -> str:
    job = load(job_id, root)
    if job is None:
        raise ValueError(f"unknown job {job_id}")
    status = effective_status(job)
    if status not in ACTIVE:
        return status
    (jobs_dir(root) / f"{job_id}.cancel").write_text(iso(now_utc()) + "\n")
    return "cancelling"


def summary(job: dict[str, Any]) -> str:
    status = effective_status(job)
    code = "" if job.get("exit_code") is None else f" exit {job['exit_code']}"
    when = job.get("ended_at") or f"deadline {job.get('deadline')}"
    return f"{job['id']} {status}{code} [{job.get('label')}] spec {job.get('spec') or '-'} {when} log {job.get('log')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay_jobs.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start", help="register and launch a durable job: start [options] -- COMMAND ...")
    p.add_argument("--label", required=True)
    p.add_argument("--authority", required=True, help="spec / directive / ledger id that authorizes this job")
    p.add_argument("--spec")
    p.add_argument("--wall-hours", type=float, required=True)
    p.add_argument("--cwd", type=Path, default=Path.cwd())
    p.add_argument("--input", type=Path, action="append", default=[], help="input file to digest (repeatable)")
    p.add_argument("--retry-reason", help="technical reason for repeating a completed command/commit/input set")
    p.add_argument("--cores", type=int, help="cores the command is expected to use (recorded)")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p = sub.add_parser("list", help="every job with its effective status")
    p.add_argument("--json", action="store_true")
    p.add_argument("--active", action="store_true", help="only running jobs")
    p = sub.add_parser("show", help="one job record")
    p.add_argument("id")
    p = sub.add_parser("log", help="last lines of a job's log")
    p.add_argument("id")
    p.add_argument("-n", type=int, default=40)
    p = sub.add_parser("cancel", help="stop a running job (SIGTERM, then SIGKILL)")
    p.add_argument("id")
    p = sub.add_parser("_run", help=argparse.SUPPRESS)
    p.add_argument("id")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "start":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            job = start(command, label=args.label, authority=args.authority, cwd=args.cwd,
                        wall_hours=args.wall_hours, spec=args.spec, inputs=args.input, cores=args.cores,
                        retry_reason=args.retry_reason)
            print(summary(job))
            return 1 if job.get("status") == "starting" else 0  # still starting: the wrapper never came up
        if args.cmd == "_run":
            return run_wrapper(args.id)
        if args.cmd == "list":
            jobs = [j for j in all_jobs() if not args.active or effective_status(j) in ACTIVE + UNRESOLVED]
            if args.json:
                print(json.dumps([{**j, "effective_status": effective_status(j)} for j in jobs], indent=1))
            else:
                print("\n".join(summary(j) for j in jobs) or "no jobs")
            return 0
        if args.cmd == "show":
            job = load(args.id)
            if job is None:
                raise ValueError(f"unknown job {args.id}")
            print(json.dumps({**job, "effective_status": effective_status(job)}, indent=1, sort_keys=True))
            return 0
        if args.cmd == "log":
            job = load(args.id)
            if job is None:
                raise ValueError(f"unknown job {args.id}")
            try:
                lines = Path(job["log"]).read_text(errors="replace").splitlines()
            except OSError:
                lines = []
            print("\n".join(lines[-max(args.n, 1):]))
            return 0
        if args.cmd == "cancel":
            print(cancel(args.id))
            return 0
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
