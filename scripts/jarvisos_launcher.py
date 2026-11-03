#!/usr/bin/env python3
"""One-click JarvisOS operator launcher (spec 154a).

Starts the product the way a human uses it: one same-origin FastAPI process
serving the built frontend plus the API on one loopback port, with the
backend-owned llama.cpp runtime and lazy Hermes worker configured from a
private per-user config file instead of per-launch shell exports.

    python3 scripts/jarvisos_launcher.py start     # update, prepare, start, open browser
    python3 scripts/jarvisos_launcher.py status
    python3 scripts/jarvisos_launcher.py stop
    python3 scripts/jarvisos_launcher.py setup [--from-pid PID]
    python3 scripts/jarvisos_launcher.py install-shortcut   # Windows desktop (from WSL)

Safety contract: the checkout is only ever fast-forwarded (never reset or
cleaned); dirty/diverged/non-master checkouts are started as-is with an
explicit warning; only processes positively identified as this checkout's
JarvisOS backend, or orphaned llama-servers running the configured binary,
are ever stopped. Stdlib only so it runs before any dependency exists.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CANONICAL_REPO = Path.home() / "src" / "JarvisOS"
CONFIG_FILE = Path(os.getenv("JARVISOS_LAUNCHER_CONFIG", Path.home() / ".config" / "jarvisos" / "operator.env"))
STATE_DIR = Path(os.getenv("JARVISOS_LAUNCHER_STATE", Path.home() / ".local" / "state" / "jarvisos"))
ENCRYPTED_CREDENTIALS_DIR = Path.home() / ".config" / "jarvisos" / "credentials.encrypted"
DEFAULT_PORT = 8000
DEV_ORIGINS = "http://127.0.0.1:5173,http://localhost:5173"
# Keys copied from a running backend by `setup --from-pid`. Secrets never are.
CAPTURE_PREFIXES = ("JARVISOS_", "JARVIS_HERMES_")
LAUNCHER_ONLY_KEYS = {"JARVISOS_PORT", "JARVISOS_NODE_BIN", "JARVISOS_GIT_REMOTE", "JARVISOS_READY_TIMEOUT_S", "JARVISOS_WSL_DISTRO"}
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
FRONTEND_INPUTS = ("src", "public", "index.html", "package.json", "package-lock.json", "vite.config.ts", "tsconfig.json")


class LaunchError(RuntimeError):
    """A failure the operator can act on; the message says how."""


def say(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)
    with contextlib.suppress(OSError):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "launcher.log").open("a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")


# --------------------------------------------------------------------------- config

def is_secret_key(key: str) -> bool:
    return any(word in key.upper() for word in SECRET_WORDS)


def parse_env_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            values[key] = value
    return values


def load_config() -> dict[str, str]:
    if not CONFIG_FILE.exists():
        return {}
    mode = CONFIG_FILE.stat().st_mode & 0o777
    if mode & 0o077:
        with contextlib.suppress(OSError):
            CONFIG_FILE.chmod(0o600)
    return parse_env_file(CONFIG_FILE.read_text(encoding="utf-8"))


def write_config(values: dict[str, str]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# JarvisOS operator launcher configuration (private; never commit).",
             "# Edit values here instead of exporting them in terminals.", ""]
    lines += [f"{key}={values[key]}" for key in sorted(values)]
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(CONFIG_FILE)


def capture_process_env(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    env = dict(item.decode(errors="replace").split("=", 1) for item in raw if b"=" in item)
    return {k: v for k, v in env.items() if k.startswith(CAPTURE_PREFIXES) and not is_secret_key(k)}


def port_of(config: dict[str, str]) -> int:
    try:
        port = int(config.get("JARVISOS_PORT", DEFAULT_PORT))
    except ValueError as exc:
        raise LaunchError(f"JARVISOS_PORT in {CONFIG_FILE} must be a TCP port number.") from exc
    if not 1 <= port <= 65535:
        raise LaunchError(f"JARVISOS_PORT in {CONFIG_FILE} must be between 1 and 65535.")
    return port


def backend_env(config: dict[str, str], port: int) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(CAPTURE_PREFIXES)}
    env.update({k: v for k, v in config.items() if k not in LAUNCHER_ONLY_KEYS})
    env.setdefault("JARVISOS_DATA_ROOT", str(Path.home() / ".local" / "share" / "JarvisOS"))
    origins = [f"http://127.0.0.1:{port}", f"http://localhost:{port}", *DEV_ORIGINS.split(",")]
    extra = [o for o in env.get("JARVISOS_CORS_ORIGINS", "").split(",") if o]
    env["JARVISOS_CORS_ORIGINS"] = ",".join(dict.fromkeys([*origins, *extra]))
    env["PATH"] = os.pathsep.join([str(REPO / "backend" / ".venv" / "bin"), *filter(None, [config.get("JARVISOS_NODE_BIN")]), env.get("PATH", "")])
    env["PYTHONUNBUFFERED"] = "1"
    return env


# --------------------------------------------------------------------------- git

@dataclass
class GitState:
    branch: str
    head: str
    dirty: list[str]
    fetched: bool
    fetch_error: str | None = None
    remote: str | None = None
    ahead: int = 0
    behind: int = 0


@dataclass
class UpdatePlan:
    action: str  # "fast-forward" | "none" | "skip"
    reasons: list[str] = field(default_factory=list)


def git(*args: str, timeout: float = 30, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(REPO), *args], check=False, capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"})
    if check and result.returncode:
        raise LaunchError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def read_git_state(fetch: bool, remote_branch: str = "master") -> GitState:
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    head = git("rev-parse", "HEAD")
    dirty = [line for line in git("status", "--porcelain", "--untracked-files=normal").splitlines() if line]
    state = GitState(branch=branch, head=head, dirty=dirty, fetched=False)
    if fetch:
        try:
            git("fetch", "--quiet", "origin", remote_branch, timeout=45)
            state.fetched = True
        except (LaunchError, subprocess.TimeoutExpired) as exc:
            state.fetch_error = str(exc).splitlines()[0][:300] if str(exc) else "timeout"
    with contextlib.suppress(LaunchError):
        state.remote = git("rev-parse", f"origin/{remote_branch}")
        counts = git("rev-list", "--left-right", "--count", f"HEAD...origin/{remote_branch}").split()
        state.ahead, state.behind = int(counts[0]), int(counts[1])
    return state


def plan_update(state: GitState) -> UpdatePlan:
    """Pure decision: fast-forward only when it cannot lose or mix local work."""
    reasons: list[str] = []
    if not state.fetched:
        reasons.append(f"GitHub could not be reached ({state.fetch_error or 'unknown error'}); starting the local copy as it is.")
    if state.branch != "master":
        reasons.append(f"The checkout is on branch '{state.branch}', not master; it is started as it is and not updated.")
    if state.dirty:
        reasons.append(f"The checkout has {len(state.dirty)} locally modified file(s); nothing is updated or discarded.")
    if state.ahead:
        reasons.append(f"Local master has {state.ahead} commit(s) not on GitHub; it is not updated automatically.")
    if reasons:
        return UpdatePlan("skip", reasons)
    return UpdatePlan("fast-forward" if state.behind else "none")


def update_checkout(no_update: bool) -> GitState:
    if no_update:
        state = read_git_state(fetch=False)
        say(f"Update skipped by request; running {state.branch}@{state.head[:10]}.")
        return state
    say("Checking GitHub for a newer JarvisOS master…")
    state = read_git_state(fetch=True)
    plan = plan_update(state)
    for reason in plan.reasons:
        say("SAFE MODE: " + reason)
    if plan.action == "fast-forward":
        say(f"Updating master {state.head[:10]} → {state.remote[:10] if state.remote else '?'} ({state.behind} new commit(s)).")
        git("merge", "--ff-only", "--quiet", f"origin/{state.branch}", timeout=120)
        state = read_git_state(fetch=False)
    elif plan.action == "none":
        say(f"Master is current ({state.head[:10]}).")
    return state


# --------------------------------------------------------------------------- dependencies & build

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stamp_path(name: str) -> Path:
    return STATE_DIR / "stamps" / name


def read_stamp(name: str) -> str | None:
    with contextlib.suppress(OSError):
        return stamp_path(name).read_text(encoding="utf-8").strip()
    return None


def write_stamp(name: str, value: str) -> None:
    path = stamp_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def ensure_backend_env() -> None:
    venv = REPO / "backend" / ".venv"
    python = venv / "bin" / "python"
    requirements = REPO / "backend" / "requirements.txt"
    fingerprint = sha256_bytes(requirements.read_bytes())
    if not python.exists():
        say("Creating the backend Python environment (first start only)…")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    elif read_stamp("backend-requirements") == fingerprint:
        return
    say("Installing backend Python dependencies (requirements changed)…")
    result = subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", str(requirements)], check=False,
                            capture_output=True, text=True)
    if result.returncode:
        raise LaunchError("Backend dependency installation failed:\n" + result.stderr[-2000:])
    write_stamp("backend-requirements", fingerprint)


def lock_packages(path: Path) -> dict[str, tuple[str, bool]] | None:
    try:
        packages = json.loads(path.read_text(encoding="utf-8")).get("packages", {})
    except (OSError, ValueError):
        return None
    return {name: (str(meta.get("version")), bool(meta.get("optional"))) for name, meta in packages.items()
            if name.startswith("node_modules/")}


def frontend_deps_current(frontend: Path) -> bool:
    """node_modules matches package-lock (npm's hidden lockfile records what is installed)."""
    wanted = lock_packages(frontend / "package-lock.json")
    installed = lock_packages(frontend / "node_modules" / ".package-lock.json")
    if wanted is None or installed is None:
        return False
    return all(installed.get(name, (None,))[0] == version for name, (version, optional) in wanted.items()
               if not optional or name in installed)


def node_command(config: dict[str, str], tool: str) -> str:
    node_bin = config.get("JARVISOS_NODE_BIN")
    if node_bin and (Path(node_bin) / tool).exists():
        return str(Path(node_bin) / tool)
    found = shutil.which(tool)
    if not found:
        raise LaunchError(f"'{tool}' was not found. Set JARVISOS_NODE_BIN in {CONFIG_FILE} to the folder containing node and npm.")
    return found


def ensure_frontend_deps(config: dict[str, str]) -> None:
    frontend = REPO / "frontend"
    if frontend_deps_current(frontend):
        return
    say("Installing frontend dependencies (package-lock changed)…")
    env = {**os.environ, "PATH": os.pathsep.join([str(Path(node_command(config, "node")).parent), os.environ.get("PATH", "")])}
    result = subprocess.run([node_command(config, "npm"), "ci", "--no-audit", "--no-fund"], check=False, cwd=frontend, env=env,
                            capture_output=True, text=True)
    if result.returncode:
        raise LaunchError("Frontend dependency installation failed:\n" + result.stderr[-2000:])


def frontend_fingerprint(frontend: Path) -> str:
    digest = hashlib.sha256(b"same-origin-v1\0")
    for name in FRONTEND_INPUTS:
        root = frontend / name
        paths = sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else [root] if root.is_file() else []
        for path in paths:
            digest.update(str(path.relative_to(frontend)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def ensure_frontend_build(config: dict[str, str]) -> bool:
    """Build the same-origin bundle when sources changed; returns True if rebuilt."""
    frontend = REPO / "frontend"
    dist = frontend / "dist"
    marker = dist / ".jarvisos-build.json"
    fingerprint = frontend_fingerprint(frontend)
    with contextlib.suppress(OSError, ValueError):
        if json.loads(marker.read_text(encoding="utf-8")).get("fingerprint") == fingerprint and (dist / "index.html").is_file():
            return False
    say("Building the JarvisOS interface (sources changed)…")
    staging = frontend / "dist.next"
    shutil.rmtree(staging, ignore_errors=True)
    node = node_command(config, "node")
    # Empty API base = same-origin relative requests: no port or CORS coupling.
    env = {**os.environ, "VITE_API_BASE_URL": "", "PATH": os.pathsep.join([str(Path(node).parent), os.environ.get("PATH", "")])}
    started = time.monotonic()
    result = subprocess.run([node, "node_modules/vite/bin/vite.js", "build", "--outDir", "dist.next", "--emptyOutDir", "--logLevel", "error"], check=False,
                            cwd=frontend, env=env, capture_output=True, text=True)
    if result.returncode or not (staging / "index.html").is_file():
        raise LaunchError("The interface build failed:\n" + (result.stderr or result.stdout)[-3000:])
    (staging / ".jarvisos-build.json").write_text(json.dumps({"fingerprint": fingerprint, "built_at": time.time()}), encoding="utf-8")
    old = frontend / "dist.old"
    shutil.rmtree(old, ignore_errors=True)
    if dist.exists():
        dist.rename(old)
    staging.rename(dist)
    shutil.rmtree(old, ignore_errors=True)
    say(f"Interface built in {time.monotonic() - started:.1f}s.")
    return True


def ensure_database(env: dict[str, str]) -> None:
    python = REPO / "backend" / ".venv" / "bin" / "python"
    result = subprocess.run([str(python), "-c", "from app.core.database import initialize_database; initialize_database()"], check=False,
                            cwd=REPO / "backend", env=env, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise LaunchError("The JarvisOS database could not be initialized:\n" + result.stderr[-2000:])


# --------------------------------------------------------------------------- processes

@dataclass
class ProcInfo:
    pid: int
    cmdline: list[str]
    cwd: str | None
    exe: str | None
    ppid: int
    start_ticks: int


def proc_info(pid: int) -> ProcInfo | None:
    base = Path(f"/proc/{pid}")
    try:
        cmdline = [part.decode(errors="replace") for part in (base / "cmdline").read_bytes().split(b"\0") if part]
        stat = (base / "stat").read_text()
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    cwd = exe = None
    with contextlib.suppress(OSError):
        cwd = os.readlink(base / "cwd")
    with contextlib.suppress(OSError):
        exe = os.readlink(base / "exe")
    return ProcInfo(pid, cmdline, cwd, exe, int(fields[1]), int(fields[19]))


def listening_pids(port: int) -> list[int]:
    """PIDs holding a TCP LISTEN socket on the port (own-user processes only)."""
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        with contextlib.suppress(OSError):
            for line in Path(table).read_text().splitlines()[1:]:
                parts = line.split()
                if parts[3] == "0A" and int(parts[1].rsplit(":", 1)[1], 16) == port:
                    inodes.add(parts[9])
    pids: set[int] = set()
    if not inodes:
        return []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        with contextlib.suppress(OSError):
            for fd in (entry / "fd").iterdir():
                target = os.readlink(fd)
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    pids.add(int(entry.name))
                    break
    return sorted(pids)


def port_in_use(port: int) -> bool:
    import socket
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def is_repo_backend(info: ProcInfo) -> bool:
    """A uvicorn serving app.main:app from this checkout's backend directory."""
    return ("app.main:app" in info.cmdline and any("uvicorn" in part for part in info.cmdline)
            and info.cwd is not None and Path(info.cwd).resolve() == (REPO / "backend").resolve())


def pidfile() -> Path:
    return STATE_DIR / "backend.json"


def read_owned() -> dict | None:
    try:
        record = json.loads(pidfile().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    info = proc_info(int(record.get("pid", 0)))
    if info is None or info.start_ticks != record.get("start_ticks") or not is_repo_backend(info):
        return None
    return record


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(request.full_url, code, "Launcher loopback probe refused a redirect", headers, fp)


_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())


def _local_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port is not None
    except ValueError:
        valid = False
    if not valid or parsed.username or parsed.password:
        raise LaunchError("Launcher health probes must use an HTTP 127.0.0.1 endpoint.")
    return url


def http_json(url: str, timeout: float = 3) -> tuple[int, object]:
    request = urllib.request.Request(_local_url(url), headers={"Accept": "application/json"})
    try:
        with _LOCAL_OPENER.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (OSError, ValueError):
        return 0, None


def http_text(url: str, timeout: float = 3) -> tuple[int, str]:
    request = urllib.request.Request(_local_url(url), headers={"Accept": "text/html"})
    try:
        with _LOCAL_OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read(200_000).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except OSError:
        return 0, ""


def terminate(pid: int, label: str, timeout: float = 45) -> None:
    say(f"Stopping {label} (pid {pid})…")
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc_info(pid) is None or _is_zombie(pid):
            return
        time.sleep(0.25)
    say(f"{label} did not exit after {timeout:.0f}s; forcing it to stop.")
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def _is_zombie(pid: int) -> bool:
    with contextlib.suppress(OSError):
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rfind(")") + 2] == "Z"
    return False


def orphan_llama_servers(config: dict[str, str]) -> list[ProcInfo]:
    """JarvisOS llama-servers with the configured binary, model, port and cwd."""
    binary = config.get("JARVISOS_LLAMACPP_BINARY")
    model = config.get("JARVISOS_LLAMACPP_MODEL")
    port = config.get("JARVISOS_LLAMACPP_PORT", "8080")
    if not binary or not model:
        return []
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        info = proc_info(int(entry.name))
        if info is None or info.exe is None or Path(info.exe).resolve() != Path(binary).resolve():
            continue
        if info.cwd is None or Path(info.cwd).resolve() != (REPO / "backend").resolve():
            continue
        if "--port" not in info.cmdline or info.cmdline[info.cmdline.index("--port") + 1] != port:
            continue
        if "--model" not in info.cmdline or info.cmdline[info.cmdline.index("--model") + 1] != model:
            continue
        parent = proc_info(info.ppid)
        if parent is not None and is_repo_backend(parent):
            continue  # owned by a live JarvisOS backend
        found.append(info)
    return found


# --------------------------------------------------------------------------- serve / start

def serve() -> None:
    """Foreground backend process (the WSL holder runs this hidden)."""
    if REPO.resolve() != CANONICAL_REPO.resolve():
        raise LaunchError(f"The backend must run from {CANONICAL_REPO}.")
    config = load_config()
    port = port_of(config)
    env = backend_env(config, port)
    logs = STATE_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "backend.log"
    if log.exists() and log.stat().st_size > 20_000_000:
        log.replace(logs / "backend.log.1")
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.chdir(REPO / "backend")
    info = proc_info(os.getpid())
    record = {"pid": os.getpid(), "start_ticks": info.start_ticks if info else None, "port": port,
              "head": git("rev-parse", "HEAD", check=False), "started_at": time.time()}
    pidfile().write_text(json.dumps(record), encoding="utf-8")
    print(f"\n==== JarvisOS backend starting {time.strftime('%Y-%m-%dT%H:%M:%S')} head={record['head'][:12]} port={port}", flush=True)
    python = str(REPO / "backend" / ".venv" / "bin" / "python")
    os.execve(python, [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)], env)


def running_under_wsl() -> bool:
    with contextlib.suppress(OSError):
        return "microsoft" in Path("/proc/version").read_text().lower()
    return False


def windows_tool(name: str) -> str:
    """Find Windows interop tools even when wsl.exe omits Windows PATH entries."""
    paths = {"powershell.exe": "System32/WindowsPowerShell/v1.0/powershell.exe",
             "cmd.exe": "System32/cmd.exe", "wsl.exe": "System32/wsl.exe"}
    standard = Path("/mnt/c/Windows") / paths[name]
    if standard.is_file():
        return str(standard)
    found = shutil.which(name)
    if found:
        return found
    raise LaunchError(f"Windows interop tool {name} is unavailable; check WSL interop or the Windows installation.")


def wsl_distro(config: dict[str, str]) -> str:
    name = config.get("JARVISOS_WSL_DISTRO") or os.getenv("WSL_DISTRO_NAME")
    if not name:
        result = subprocess.run([windows_tool("wsl.exe"), "--list", "--quiet"], check=True, capture_output=True, timeout=15)
        names = [line.strip() for line in result.stdout.decode("utf-16-le").splitlines() if line.strip()]
        if len(names) != 1:
            raise LaunchError(f"Set JARVISOS_WSL_DISTRO in {CONFIG_FILE} to this Ubuntu distro (Windows lists {len(names)} distros).")
        name = names[0]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise LaunchError(f"Invalid WSL distro name in {CONFIG_FILE}.")
    return name


def spawn_backend() -> None:
    script = str(Path(__file__).resolve())
    if running_under_wsl():
        # WSL stops a distro's background processes once no wsl.exe session is
        # attached; a hidden wsl.exe holder keeps the backend alive after this
        # launcher window closes.
        distro = wsl_distro(load_config())
        overrides = [f"{key}={os.environ[key]}" for key in ("JARVISOS_LAUNCHER_CONFIG", "JARVISOS_LAUNCHER_STATE") if key in os.environ]
        words = [*overrides, "/usr/bin/python3", script, "serve"]
        credential_files = []
        if os.path.lexists(ENCRYPTED_CREDENTIALS_DIR):
            metadata = ENCRYPTED_CREDENTIALS_DIR.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise LaunchError("The encrypted credential store is not a regular directory.")
            for path in sorted(ENCRYPTED_CREDENTIALS_DIR.iterdir()):
                if not re.fullmatch(r"[A-Z][A-Z0-9_]*", path.name) or path.is_symlink() or not path.is_file():
                    raise LaunchError("The encrypted credential store contains an invalid entry.")
                credential_files.append(path)
        if credential_files:
            account = pwd.getpwuid(os.getuid()).pw_name
            properties = ["-p", "Type=exec", "-p", f"User={account}", "-p", f"WorkingDirectory={REPO}"]
            for key in ("JARVISOS_LAUNCHER_CONFIG", "JARVISOS_LAUNCHER_STATE"):
                if key in os.environ:
                    properties.extend(["-p", f"Environment={key}={os.environ[key]}"])
            for path in credential_files:
                properties.extend(["-p", f"LoadCredentialEncrypted={path.name}:{path}"])
            words = ["/usr/bin/systemd-run", "--wait", "--collect", "--unit=jarvisos-backend", *properties,
                     "/usr/bin/python3", script, "serve"]
        if any(re.search(r"[\s'\"]", word) for word in [*words, str(REPO)]):
            raise LaunchError("Paths containing spaces or quotes are not supported by the WSL launcher.")
        root_option = "-u root " if credential_files else ""
        args = f"-d {distro} {root_option}--cd {REPO} -- /usr/bin/env {' '.join(words)}"
        command = f"Start-Process -WindowStyle Hidden -FilePath \"$env:WINDIR\\System32\\wsl.exe\" -ArgumentList '{args}'"
        subprocess.run([windows_tool("powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", command], check=True,
                       cwd="/mnt/c/Windows", capture_output=True, timeout=60)
    else:
        subprocess.Popen([sys.executable, script, "serve"], start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def readiness(port: int) -> tuple[str, str]:
    """(state, human message) where state is starting|ready|degraded."""
    status, _ = http_json(f"http://127.0.0.1:{port}/health")
    if status != 200:
        return "starting", "Waiting for the JarvisOS server…"
    status, page = http_text(f"http://127.0.0.1:{port}/design/process")
    if status != 200 or 'id="root"' not in page:
        return "degraded", "The server is up but the interface is not being served."
    status, options = http_json(f"http://127.0.0.1:{port}/ai/threads/conversation-options", timeout=10)
    routes = {row["route_class"]: row for row in (options or {}).get("routes", [])} if isinstance(options, dict) else {}
    llama = routes.get("local:llamacpp")
    hermes = routes.get("hermes:agent")
    if llama is None:
        return "degraded", "No local llama.cpp model is configured; Jarvis can only use other configured routes."
    availability = llama.get("availability", {})
    if not availability.get("runtime_reachable") or not availability.get("model_loaded"):
        if availability.get("reason_code") in {"LLAMACPP_LOADING", "LLAMACPP_RUNTIME_UNREACHABLE", None}:
            return "starting", f"Loading local model {llama.get('model_id')}… ({availability.get('message')})"
        return "degraded", f"Local model unavailable: {availability.get('message')}"
    if hermes is None or not hermes.get("availability", {}).get("runtime_reachable"):
        message = (hermes or {}).get("availability", {}).get("message", "Hermes is not configured.")
        return "degraded", f"Local model ready, but the Jarvis agent (Hermes) is unavailable: {message}"
    return "ready", f"Local model {llama.get('model_id')} loaded; Jarvis agent (Hermes) ready (starts on the first message)."


def wait_ready(port: int, timeout: float) -> tuple[str, str]:
    started = time.monotonic()
    last = ""
    state, message = "starting", ""
    while time.monotonic() - started < timeout:
        state, message = readiness(port)
        if message != last:
            say(message)
            last = message
        if state != "starting":
            return state, message
        if time.monotonic() - started > 20 and read_owned() is None and not port_in_use(port):
            raise LaunchError("The JarvisOS server exited during startup. See the backend log: " + str(STATE_DIR / "logs" / "backend.log"))
        time.sleep(1)
    if http_json(f"http://127.0.0.1:{port}/health")[0] == 200:
        return "degraded", message + " (still not ready after waiting; opening JarvisOS anyway)"
    raise LaunchError(f"JarvisOS did not become reachable within {timeout:.0f}s. See {STATE_DIR / 'logs' / 'backend.log'}")


def open_browser(url: str) -> None:
    if running_under_wsl():
        subprocess.run([windows_tool("cmd.exe"), "/c", "start", "", url], check=True, cwd="/mnt/c/Windows", capture_output=True, timeout=30)
    elif shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        say(f"Open {url} in your browser.")


def stop_owned(reason: str) -> bool:
    owned = read_owned()
    if owned is None:
        return False
    terminate(int(owned["pid"]), f"JarvisOS ({reason})")
    with contextlib.suppress(OSError):
        pidfile().unlink()
    return True


def resolve_port_conflict(port: int) -> None:
    """Report a conflicting listener without taking ownership of a manual session."""
    for pid in listening_pids(port):
        info = proc_info(pid)
        if info is not None and is_repo_backend(info):
            raise LaunchError(f"Port {port} has a manually started JarvisOS server (pid {pid}). Stop that session once before using the desktop shortcut; its work and processes were left untouched.")
        else:
            name = " ".join(info.cmdline[:3]) if info else f"pid {pid}"
            raise LaunchError(f"Port {port} is used by another program ({name}). Close it, or set JARVISOS_PORT in {CONFIG_FILE}.")
    deadline = time.monotonic() + 20
    while port_in_use(port) and time.monotonic() < deadline:
        time.sleep(0.25)
    if port_in_use(port):
        raise LaunchError(f"Port {port} is still in use by a process this launcher cannot identify (for example another user or Windows). Close it, or set JARVISOS_PORT in {CONFIG_FILE}.")


def relaunch(argv: list[str]) -> None:
    sys.stdout.flush()
    os.execv(argv[0], argv)


def start(args: argparse.Namespace) -> int:
    if REPO.resolve() != CANONICAL_REPO.resolve():
        raise LaunchError(f"This launcher starts only the canonical checkout at {CANONICAL_REPO}; this copy is at {REPO}.")
    started = time.monotonic()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = (STATE_DIR / "launcher.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        say("Another JarvisOS launch is already in progress; waiting for it…")
        fcntl.flock(lock, fcntl.LOCK_EX)
    config = load_config()
    if not config:
        say(f"No configuration at {CONFIG_FILE}; using defaults (local model and Hermes need `setup`).")
    port = port_of(config)
    url = f"http://127.0.0.1:{port}/"
    loaded_head = read_git_state(fetch=False).head
    state = update_checkout(args.no_update)
    if state.head != loaded_head:
        # This process still runs the code it loaded before the update; continue on the updated launcher.
        say("Continuing with the updated launcher…")
        lock.close()
        relaunch([sys.executable, str(REPO / "scripts" / "jarvisos_launcher.py"), *sys.argv[1:], "--no-update"])
    if state.branch != "master":
        raise LaunchError(f"The canonical checkout is on {state.branch}. Switch it to master without discarding local work before starting JarvisOS.")
    ensure_backend_env()
    ensure_frontend_deps(config)
    rebuilt = ensure_frontend_build(config)
    owned = read_owned()
    if owned is not None:
        if owned.get("head") != state.head or rebuilt:
            say("JarvisOS is running an older version; restarting it on the updated code.")
            stop_owned("update")
            owned = None
        else:
            say(f"JarvisOS is already running (pid {owned['pid']}); reusing it.")
    if owned is None:
        resolve_port_conflict(port)
        for orphan in orphan_llama_servers(config):
            terminate(orphan.pid, "an orphaned local-model server left by an earlier JarvisOS run")
        env = backend_env(config, port)
        ensure_database(env)
        say(f"Starting JarvisOS on {url} …")
        spawn_backend()
    ready_state, message = wait_ready(port, float(config.get("JARVISOS_READY_TIMEOUT_S", 420)))
    elapsed = time.monotonic() - started
    if ready_state == "ready":
        say(f"JarvisOS is ready in {elapsed:.0f}s — {message}")
    elif ready_state == "degraded":
        # A missing local model or Hermes must not lock the operator out of Studio.
        # Keep the backend/frontend availability gate; do not open a broken interface.
        ui_status, page = http_text(f"http://127.0.0.1:{port}/design/process")
        if ui_status != 200 or 'id="root"' not in page:
            raise LaunchError(
                f"JarvisOS is not ready after {elapsed:.0f}s: the interface is unavailable. "
                f"See {STATE_DIR / 'logs' / 'backend.log'}"
            )
        say(f"JarvisOS interface available in {elapsed:.0f}s; local AI is degraded: {message}")
    else:
        raise LaunchError(f"JarvisOS is not ready after {elapsed:.0f}s: {message}. See {STATE_DIR / 'logs' / 'backend.log'}")
    if not args.no_browser:
        open_browser(url)
    say(f"Logs: {STATE_DIR / 'logs'}")
    return 0


def status(_: argparse.Namespace) -> int:
    config = load_config()
    port = port_of(config)
    owned = read_owned()
    print(json.dumps({"config": str(CONFIG_FILE), "port": port, "owned_backend": owned,
                      "listeners": listening_pids(port), "readiness": readiness(port) if port_in_use(port) else ["stopped", "JarvisOS is not running."],
                      "orphan_llama_servers": [o.pid for o in orphan_llama_servers(config)]}, indent=2))
    return 0


def stop(_: argparse.Namespace) -> int:
    if REPO.resolve() != CANONICAL_REPO.resolve():
        raise LaunchError(f"Stop JarvisOS from its canonical checkout at {CANONICAL_REPO}.")
    if stop_owned("stop requested"):
        say("JarvisOS stopped.")
    else:
        say("No launcher-owned JarvisOS server is running.")
    for orphan in orphan_llama_servers(load_config()):
        terminate(orphan.pid, "an orphaned local-model server")
    return 0


def setup(args: argparse.Namespace) -> int:
    values = load_config()
    if args.from_pid:
        captured = capture_process_env(args.from_pid)
        captured.pop("JARVISOS_CORS_ORIGINS", None)
        values.update(captured)
        say(f"Captured {len(captured)} JarvisOS settings from process {args.from_pid} (secrets excluded).")
    values.setdefault("JARVISOS_DATA_ROOT", str(Path.home() / ".local" / "share" / "JarvisOS"))
    values.setdefault("JARVISOS_PORT", str(DEFAULT_PORT))
    node = shutil.which("node")
    if node and "JARVISOS_NODE_BIN" not in values:
        values["JARVISOS_NODE_BIN"] = str(Path(node).parent)
    values.setdefault("JARVISOS_MANAGE_LLAMACPP", "true" if values.get("JARVISOS_LLAMACPP_BINARY") else "false")
    write_config(values)
    say(f"Wrote {CONFIG_FILE} (mode 600) with {len(values)} settings.")
    return 0


def windows_desktop_shortcut_script(repo: Path, distro: str, icon: str | None) -> str:
    launcher = f"{repo}/scripts/jarvisos_launcher.py"
    def link(name: str, command: str, description: str) -> str:
        arguments = f"-d {distro} --cd {repo} -- /usr/bin/python3 {launcher} {command}"
        icon_line = f"$s.IconLocation = '{icon}';" if icon else ""
        return (f"$s = $shell.CreateShortcut((Join-Path $desktop '{name}.lnk'));"
                f"$s.TargetPath = \"$env:WINDIR\\System32\\wsl.exe\"; $s.Arguments = '{arguments}';"
                f"$s.Description = '{description}'; $s.WorkingDirectory = $env:USERPROFILE; {icon_line} $s.Save();")
    return ("$shell = New-Object -ComObject WScript.Shell; $desktop = [Environment]::GetFolderPath('Desktop');"
            + link("JarvisOS", "start --pause-on-error", "Update and open JarvisOS")
            + link("Stop JarvisOS", "stop --pause-on-error", "Stop JarvisOS and its local model")
            + "Write-Output $desktop")


def install_shortcut(_: argparse.Namespace) -> int:
    if REPO.resolve() != CANONICAL_REPO.resolve():
        raise LaunchError(f"Install the shortcut from its canonical checkout at {CANONICAL_REPO}.")
    if not running_under_wsl():
        raise LaunchError("install-shortcut must run inside WSL on the Windows machine.")
    icon = None
    source = REPO / "scripts" / "windows" / "jarvisos.ico"
    if source.is_file():
        local = subprocess.run([windows_tool("cmd.exe"), "/c", "echo %LOCALAPPDATA%"], check=True, cwd="/mnt/c/Windows", capture_output=True, text=True).stdout.strip()
        target_dir = Path(subprocess.run(["wslpath", "-u", local], check=True, capture_output=True, text=True).stdout.strip()) / "JarvisOS"
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target_dir / "jarvisos.ico")
        icon = f"{local}\\JarvisOS\\jarvisos.ico"
    script = windows_desktop_shortcut_script(REPO, wsl_distro(load_config()), icon)
    result = subprocess.run([windows_tool("powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", script], check=False, cwd="/mnt/c/Windows",
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise LaunchError("Shortcut creation failed: " + result.stderr.strip())
    say(f"Desktop shortcuts 'JarvisOS' and 'Stop JarvisOS' installed in {result.stdout.strip()}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "stop", "status", "setup", "install-shortcut"):
        command = sub.add_parser(name)
        command.add_argument("--pause-on-error", action="store_true", help="keep the window open on failure")
        if name == "start":
            command.add_argument("--no-update", action="store_true")
            command.add_argument("--no-browser", action="store_true")
            command.add_argument("--hold", type=float, default=4.0, help="seconds to keep the window open after success")
        if name == "setup":
            command.add_argument("--from-pid", type=int)
    sub.add_parser("serve")
    args = parser.parse_args(argv)
    if args.command == "serve":
        serve()
        return 0
    handlers = {"start": start, "stop": stop, "status": status, "setup": setup, "install-shortcut": install_shortcut}
    try:
        code = handlers[args.command](args)
        if args.command == "start" and args.pause_on_error:
            time.sleep(args.hold)
        return code
    except (LaunchError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        say(f"JarvisOS could not start: {exc}" if args.command == "start" else f"Failed: {exc}")
        say(f"Launcher log: {STATE_DIR / 'launcher.log'}")
        if getattr(args, "pause_on_error", False):
            with contextlib.suppress(EOFError):
                input("\nPress Enter to close this window.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
