"""Relay and host adapters for relay_continuity (spec 265)."""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
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
    LEGACY_MARKERS,
    NATIVE_AGENTS,
    Limits,
    devctx,
    iso,
    now_utc,
    relay_argon,
)

# ---------------------------------------------------------------------------
# Relay and host adapters (injectable for tests)


@dataclass
class RunResult:
    session_id: str | None
    exit_code: int | None
    raw_ok: bool
    diagnostics: str = ""
    timed_out: bool = False


class CatalogRefused(OSError):
    """A catalog preflight refused a launch before any provider turn began."""


class RelayAdapter:
    def __init__(self, repo: Path, binary: str = "agent-relay", env: dict[str, str] | None = None,
                 turn_wall: float = Limits.turn_wall, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, killpg: Callable[[int, int], None] = os.killpg):
        self.repo = repo
        self.binary = binary
        self.env = env  # extra environment for launched runs (the sandboxed mission profile); None: inherited
        self.turn_wall = turn_wall
        self.clock = clock
        self.sleep = sleep
        self.killpg = killpg
        self._outputs: dict[int, tuple[Any, Any]] = {}
        self._argon_leases: dict[int, Any] = {}
        self._resolved_models: dict[int, str] = {}
        self._model_launches: dict[int, tuple[str | None, str | None]] = {}

    def conversation_store(self) -> Path:
        return relay_argon.conversation_store()

    def limits(self) -> dict[str, Any] | None:
        try:
            proc = subprocess.run([self.binary, "--json", "limits", "--refresh"], capture_output=True, text=True,
                                  timeout=90, check=False, cwd="/tmp")
            data = json.loads(proc.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return None
        found = {a.get("agent_key"): a for a in data.get("agents", []) if isinstance(a, dict)}
        if relay_argon.account_models.enabled({**os.environ, **(self.env or {})}):
            # Relay discovery is a bundled alias list, not account entitlement evidence.
            found.pop("claude", None)
            binary = shutil.which("codex")
            try:
                if binary is None:
                    raise relay_argon.account_models.Refused("catalog_unavailable")
                relay_argon.account_models.resolve("coordinator_codex", binary, {**os.environ, **(self.env or {})})
            except relay_argon.account_models.Refused:
                found.pop("codex", None)
        return found

    def argon_limits(self, agent: str = "argon") -> dict[str, Any] | None:
        # The selected native family owns its quota cache. Probe results never authorize a turn:
        # the native runner repeats the fail-closed preflight immediately before executing its model.
        cached = getattr(self, "_argon_reading", {}).get(agent)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1]
        try:
            if os.environ.get("JARVISOS_MISSION_PROFILE", (self.env or {}).get("JARVISOS_MISSION_PROFILE")) != "1":
                return None
            binary = shutil.which("agy")
            if binary is None:
                return None
            env = {**os.environ, **(self.env or {})}
            if relay_argon.account_models.enabled(env):
                relay_argon.prepare_login()
                relay_argon.resolved_model_for(agent, binary, env)
            else:
                relay_argon.model_for(agent, env)
            reading = relay_argon.read_quota(binary, env, agent=agent)
            result = {"as_of": iso(now_utc()),
                      "windows": [{"id": b.get("id"), "used_percent": 100 * (1 - b["remaining_fraction"]),
                                   "resets_at": b.get("reset_time"), "disabled": b.get("disabled", False)}
                                  for b in reading]}
        except relay_argon.Refused:
            result = None
        if not hasattr(self, "_argon_reading"):
            self._argon_reading = {}
        self._argon_reading[agent] = (time.monotonic(), result)
        return result

    def start(self, agent: str, parent: str | None, task: str, turns: int,
              resume: str | None = None, resume_model: str | None = None) -> subprocess.Popen:
        # A mission's first task has no parent: Relay opens a new session for it.
        lease = None
        env = {**os.environ, **self.env} if self.env else dict(os.environ)
        resolved_model = None
        if agent in NATIVE_AGENTS:
            if relay_argon.account_models.enabled(env):
                binary = shutil.which("agy")
                if binary is None:
                    raise CatalogRefused("catalog_unavailable")
                try:
                    relay_argon.prepare_login()
                    relay_argon.subscription_settings()
                    if resume is not None and (not isinstance(resume_model, str) or not resume_model.strip()):
                        raise relay_argon.Refused("resume_model_missing")
                    resolved_model = relay_argon.resolved_model_for(agent, binary, env, exact=resume_model)
                except relay_argon.Refused as exc:
                    raise CatalogRefused(str(exc)) from exc
            lease_path = self.repo / COORDINATOR_LEASE
            lease_path.parent.mkdir(parents=True, exist_ok=True)
            lease = lease_path.open("a+")
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lease.close()
                raise
            env["JARVISOS_ARGON_LEASE_FD"] = str(lease.fileno())
            try:
                session, _ = relay_argon.new_session(self.repo, task, agent, resume=resume,
                                                     resume_model=resume_model, selected_model=resolved_model)
            except OSError:
                lease.close()
                raise
            args = [sys.executable, str(Path(relay_argon.__file__)), "--repo", str(self.repo), "--session", session, "--agent", agent]
        else:
            if relay_argon.account_models.enabled(env):
                if agent != "codex":
                    raise CatalogRefused("catalog_unavailable")
                binary = shutil.which("codex")
                if binary is None:
                    raise CatalogRefused("catalog_unavailable")
                if parent:
                    try:
                        previous = json.loads((self.sessions_dir() / parent / "selected-model.json").read_text())
                        if not isinstance(previous, dict):
                            raise CatalogRefused("resume_model_missing")
                        candidate_model = previous.get("model")
                        if not isinstance(candidate_model, str) or not candidate_model.strip():
                            raise CatalogRefused("resume_model_missing")
                        resume_model = candidate_model
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        raise CatalogRefused("resume_model_missing") from exc
                try:
                    resolved_model = relay_argon.account_models.resolve(
                        "coordinator_codex", binary, env, exact=resume_model)
                except relay_argon.account_models.Refused as exc:
                    raise CatalogRefused(exc.reason) from exc
            args = [self.binary, "--json", "run", agent, *(["--continue", parent] if parent else []), "-t", task,
                    "-n", str(turns), "-y", "--repo", str(self.repo)]
            if resolved_model is not None:
                args.extend(("--model", resolved_model))
        # stdin is closed: `codex exec` otherwise waits on an inherited open stdin (spec 201 readiness). Output
        # goes to anonymous files, not pipes: a process the turn detached may keep an inherited descriptor open
        # for hours, and the run ends when Relay exits, not when every holder of its output has closed it.
        out, err = tempfile.TemporaryFile("w+"), tempfile.TemporaryFile("w+")
        try:
            proc = subprocess.Popen(args, cwd=self.repo, stdin=subprocess.DEVNULL, stdout=out, stderr=err, text=True,
                                    start_new_session=True, env=env if self.env or lease else None,
                                    pass_fds=(lease.fileno(),) if lease else ())
        except OSError:
            out.close()
            err.close()
            if lease:
                lease.close()
            raise
        self._outputs[id(proc)] = (out, err)
        if resolved_model is not None and agent not in NATIVE_AGENTS:
            self._resolved_models[id(proc)] = resolved_model
            marker = re.search(r"\[relay-continuity (?:attempt|resume) (rc-[0-9TZ]+-[0-9a-f]+)\]", task)
            self._model_launches[id(proc)] = (marker.group(1) if marker else None, parent)
        if lease:
            self._argon_leases[id(proc)] = lease
        return proc

    def release_turn(self, proc: subprocess.Popen) -> None:
        # The parent retains the same flock until its normal descendant reaper has finished.
        lease = self._argon_leases.pop(id(proc), None)
        if lease:
            lease.close()

    def _proc_is_running(self, proc: Any) -> bool:
        if hasattr(proc, "poll"):
            try:
                return proc.poll() is None
            except Exception:  # noqa: BLE001
                return False
        return False

    def _group_is_running(self, pgid: int) -> bool:
        try:
            self.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True

    def finish(self, proc: subprocess.Popen, timeout: float | None = None) -> RunResult:
        wall_limit = self.turn_wall if timeout is None else timeout
        start = self.clock()
        timed_out = False
        if self.clock is time.monotonic and self.sleep is time.sleep and hasattr(proc, "wait"):
            try:
                proc.wait(timeout=max(0.0, wall_limit))
            except (subprocess.TimeoutExpired, TimeoutError):
                timed_out = True
        else:
            while True:
                if not self._proc_is_running(proc):
                    break
                if self.clock() - start >= wall_limit:
                    timed_out = True
                    break
                self.sleep(min(1.0, max(0.01, wall_limit - (self.clock() - start))))

        if timed_out:
            pgid = getattr(proc, "pid", None)
            if pgid:
                try:
                    self.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            term_start = self.clock()
            if self.clock is time.monotonic and self.sleep is time.sleep and hasattr(proc, "wait"):
                try:
                    proc.wait(timeout=30.0)
                except (subprocess.TimeoutExpired, TimeoutError):
                    pass
                while pgid and self._group_is_running(pgid) and self.clock() - term_start < 30.0:
                    self.sleep(min(1.0, max(0.01, 30.0 - (self.clock() - term_start))))
            else:
                while (self._proc_is_running(proc) or (pgid and self._group_is_running(pgid))) and self.clock() - term_start < 30.0:
                    self.sleep(min(1.0, max(0.01, 30.0 - (self.clock() - term_start))))
            if pgid and (self._proc_is_running(proc) or self._group_is_running(pgid)):
                try:
                    self.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            if hasattr(proc, "wait"):
                try:
                    if self.clock is time.monotonic and self.sleep is time.sleep:
                        proc.wait(timeout=1.0)
                    else:
                        proc.wait()
                except Exception:  # noqa: BLE001
                    pass

        files = self._outputs.pop(id(proc), None)
        texts = []
        if files:
            for handle in files:
                try:
                    handle.seek(0)
                    texts.append(handle.read())
                    handle.close()
                except Exception:  # noqa: BLE001
                    texts.append("")
        out, err = texts if len(texts) == 2 else ("", "")
        diagnostics = (err or "")[-8192:]
        session_id = None
        errors: list[str] = []
        try:
            data = json.loads(out)
            session_id = data.get("session_id")
            errors = [str(data.get(k)) for k in ("run_error", "error", "stop_reason") if data.get(k)]
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

        selected = self._resolved_models.pop(id(proc), None)
        launch_nonce, launch_parent = self._model_launches.pop(id(proc), (None, None))
        if selected is not None and (not isinstance(session_id, str)
                                     or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id)):
            return RunResult(None, getattr(proc, "returncode", None), False, "model_record_failed")
        if selected is not None:
            try:
                session = self.sessions_dir() / session_id
                if launch_parent is not None and session_id != launch_parent:
                    raise ValueError("resume_session_mismatch")
                if launch_nonce is None:
                    raise ValueError("launch_nonce_missing")
                marker = re.compile(r"\[relay-continuity (?:attempt|resume) (rc-[0-9TZ]+-[0-9a-f]+)\]")
                own_turns = [p for p in (session / "turns").glob("turn-*/prompt.md")
                             if task_marker(p.read_text(encoding="utf-8", errors="replace"), marker) == launch_nonce]
                if len(own_turns) != 1:
                    raise ValueError("launch_nonce_mismatch")
                target = session / "selected-model.json"
                if launch_parent is not None:
                    if json.loads(target.read_text(encoding="utf-8")) != {"model": selected}:
                        raise ValueError("resume_model_mismatch")
                else:
                    with target.open("x", encoding="utf-8") as handle:
                        handle.write(json.dumps({"model": selected}) + "\n")
            except (OSError, ValueError):
                return RunResult(session_id, getattr(proc, "returncode", None), False, "model_record_failed")

        if timed_out:
            wall_diag = f"turn wall limit ({int(wall_limit)}s) exceeded"
            diagnostics = f"{diagnostics}\n{wall_diag}".strip() if diagnostics else wall_diag
            return RunResult(session_id, getattr(proc, "returncode", None), False, diagnostics, timed_out=True)

        if session_id is not None or errors:
            return RunResult(session_id, getattr(proc, "returncode", None), True, "\n".join([diagnostics, *errors]))
        return RunResult(None, getattr(proc, "returncode", None), False, diagnostics)

    def sessions_dir(self) -> Path:
        return self.repo / ".agent-relay" / "sessions"

    def find_by_nonce(self, nonce: str, since: datetime) -> str | None:
        root = self.sessions_dir()
        floor = (since - timedelta(minutes=2)).strftime("%Y%m%d-%H%M%S")
        marker = re.compile(r"\[relay-continuity (?:attempt|resume) (rc-[0-9TZ]+-[0-9a-f]+)\]")
        found = []
        for session in sorted(root.glob("*-*-*"), reverse=True):
            if session.name[:15] < floor:
                break
            prompt = session / "turns" / "turn-001" / "prompt.md"
            try:
                own = task_marker(prompt.read_text(encoding="utf-8", errors="replace"), marker)
            except OSError:
                continue
            # Only the marker that opens the session's own task counts; a prompt quoting a nonce does not.
            if own == nonce:
                found.append(session.name)
        return found[0] if len(found) == 1 else None


def task_marker(prompt: str, marker: re.Pattern[str]) -> str | None:
    """Nonce of the attempt marker that is the first line of the prompt's `## Task` section, if any."""
    head, sep, rest = prompt.partition("\n## Task\n")
    if not sep:
        return None
    first = next((line for line in rest.splitlines() if line.strip()), "")
    match = marker.fullmatch(first.strip())
    return match.group(1) if match else None


def fingerprint(repo: Path) -> str:
    digest = hashlib.sha256()
    for args in (["for-each-ref", "--format=%(refname) %(objectname)"], ["worktree", "list", "--porcelain"]):
        digest.update((devctx.run(["git", *args], cwd=repo)[1] or "").encode())
    for line in (devctx.run(["git", "worktree", "list", "--porcelain"], cwd=repo)[1] or "").splitlines():
        if line.startswith("worktree ") and Path(line[9:]).is_dir():
            digest.update(line.encode())
            digest.update((devctx.run(["git", "status", "--porcelain=v1"], cwd=Path(line[9:]))[1] or "").encode())
    return digest.hexdigest()


def _relay_argv(argv: list[str]) -> list[str] | None:
    """Normalize native entry points and Python's installed-script argv layout."""
    entry_names = {"relay", "agent-relay", "relay_safe.py"}
    if not argv:
        return None
    name = Path(argv[0]).name
    if name in entry_names:
        return argv
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", name) and len(argv) > 1 and Path(argv[1]).name in entry_names:
        return argv[1:]
    return None


COORDINATOR_LEASE = Path(".agent-relay/locks/coordinator.lease")


def coordinator_lease_held(repo: Path) -> bool:
    """A Relay coordinator in any sandbox instance holds this shared flock (scripts/relay_safe.py). Its process
    is invisible here when it runs in another PID namespace. An unreadable lease counts as held."""
    try:
        handle = (repo / COORDINATOR_LEASE).open("r")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def live_conflicts(repo: Path, own_pids: set[int]) -> list[str]:
    """Reasons another coordinator/supervisor may be acting: legacy autopilot or a live Relay REPL/run, seen in
    this process table or through the coordinator lease."""
    found = ["repl_active"] if coordinator_lease_held(repo) else []
    proc_root = Path("/proc")
    for entry in proc_root.iterdir() if proc_root.exists() else []:
        if not entry.name.isdigit() or int(entry.name) in own_pids:
            continue
        try:
            argv = [a.decode(errors="replace") for a in (entry / "cmdline").read_bytes().split(b"\0") if a]
            cwd = os.readlink(entry / "cwd")
        except OSError:
            continue
        joined = " ".join(argv)
        relay_argv = _relay_argv(argv)
        if any(marker in joined for marker in LEGACY_MARKERS):
            found.append("legacy_autopilot")
        elif relay_argv and not any(a in ("daemon", "hook") for a in relay_argv[1:3]) \
                and Path(cwd).resolve() == repo.resolve():
            found.append("repl_active")
    return sorted(set(found))


# Process ownership: everything a turn starts belongs to the supervisor, and none of it outlives the turn. The
# supervisor is a child subreaper (prctl PR_SET_CHILD_SUBREAPER), so a process the turn detached (`&`, `nohup`,
# `setsid`, a double fork, a daemon) is re-parented to the supervisor, not to init. When the turn ends, every
# descendant still running is stopped. An interrupted operation is therefore never still running when the next
# turn starts: its effects are whatever it did before it was stopped, and the next turn inspects them.
# The one exception is a durable job registered through `relay_jobs.py` (spec 218): while its record proves the
# process is still that job's wrapper, the wrapper and everything below it are left running. The wrapper enforces
# its own wall-time limit and cancellation, so nothing it starts is unbounded.

PR_SET_CHILD_SUBREAPER = 36


def become_subreaper() -> bool:
    try:
        import ctypes
        return ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def _running(pid: int, proc_root: Path) -> bool:
    try:
        return (proc_root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (OSError, IndexError):
        return False


def descendants(pid: int, proc_root: Path = Path("/proc")) -> set[int]:
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
    return {c for c in found if _running(c, proc_root)}


def _reap_zombies() -> None:
    try:
        while os.waitpid(-1, os.WNOHANG)[0] > 0:
            pass
    except ChildProcessError:
        pass


def reap_descendants(grace: float = 5.0, *, pid: int | None = None, proc_root: Path = Path("/proc"),
                     kill: Callable[[int, int], None] = os.kill, sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic,
                     keep: Callable[[], set[int]] | None = None) -> list[str]:
    """Stop every process still running below this one: SIGTERM, then SIGKILL once `grace` seconds have passed,
    repeated until none is left (a process may fork while being stopped). Called only when no launched run is
    still awaited. Returns the stopped processes' names, never their arguments. Raises RuntimeError when a process
    survives (for example one stuck in uninterruptible I/O): the turn is not over until it has gone.
    `keep` names the registered durable-job wrappers (spec 218); they and their descendants are spared."""
    pid = os.getpid() if pid is None else pid
    names: set[str] = set()
    deadline, sig = clock() + grace, signal.SIGTERM

    def unowned() -> set[int]:
        live = descendants(pid, proc_root)
        roots = (keep() if keep is not None else set()) & live
        spared = set(roots)
        for root in roots:
            spared |= descendants(root, proc_root)
        return live - spared
    for _ in range(600):
        live = unowned()
        if not live:
            break
        for child in live:
            try:
                names.add(re.sub(r"[^A-Za-z0-9_.:-]", "_", (proc_root / str(child) / "comm").read_text().strip())[:32])
            except OSError:
                pass
            try:
                kill(child, sig)
            except (ProcessLookupError, PermissionError):
                pass
        sleep(0.1)
        _reap_zombies()
        if clock() >= deadline:
            sig = signal.SIGKILL
    if survivors := unowned():
        raise RuntimeError(f"{len(survivors)} process(es) survived SIGKILL")
    return sorted(names)


