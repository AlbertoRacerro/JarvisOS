"""Read-only provider quota rings for Coding → Runtime (spec 217).

Claude usage windows come from cached or refreshed ``agent-relay --json limits`` readings.
Copilot premium usage comes from the host gh CLI entitlement endpoint.
Codex windows come from the CLI's status-only ``account/rateLimits/read`` method, without a model turn.
Antigravity buckets come from the agy CLI's own ``/quota`` command run headless (``agy -p /quota --output-format
json``), which starts no model turn and spends no quota. Automatic reads share a TTL cache; explicit refresh
requests a new status reading. All sources are normalised here;
an unreadable source is reported as not available, never estimated. Nothing in this module writes owner, mission, Relay or credential state, and no
tool output, path or secret reaches the payload: only validated numbers, timestamps and short identifiers.
"""

from __future__ import annotations

import glob
import json
import math
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.modules.coding import relay_monitor

PROVIDERS = (
    ("claude", "Claude", "claude"),
    ("codex", "Codex (ChatGPT)", "codex"),
    ("antigravity", "Antigravity", "agy"),
    ("copilot", "GitHub Copilot", "copilot"),
)
EXHAUSTED_PERCENT = 99.5
STALE_AFTER = timedelta(minutes=30)
DEFAULT_TTL_S = 600
MIN_TTL_S, MAX_TTL_S = 60, 3600
READ_TIMEOUT_S = 15

FIVE_HOUR_IDS = {"five_hour", "5h", "session", "primary", "hourly"}
WEEKLY_IDS = {"seven_day", "weekly", "week", "7d", "secondary"}
AUTH_STATUSES = {"unauthenticated", "not_authenticated", "auth", "auth_required", "logged_out", "not_logged_in"}
_ID = re.compile(r"^[A-Za-z0-9_.&·()\- ]{1,40}$")
_TOKENISH = re.compile(r"[A-Za-z0-9]{20,}")

Reader = Callable[[], "dict[str, Any] | None"]


def ttl_seconds() -> int:
    try:
        value = int(os.environ.get("JARVISOS_PROVIDER_QUOTA_TTL_S", DEFAULT_TTL_S))
    except ValueError:
        value = DEFAULT_TTL_S
    return max(MIN_TTL_S, min(MAX_TTL_S, value))


def host_which(command: str) -> str | None:
    """Resolve a host CLI as the Relay gateway does: services started by the launcher do not inherit a login PATH."""
    entries = [str(Path.home() / ".local" / "bin")]
    entries += sorted(glob.glob(str(Path.home() / ".nvm" / "versions" / "node" / "*" / "bin")), reverse=True)
    entries.append(os.getenv("PATH", "/usr/bin:/bin"))
    return shutil.which(command, path=os.pathsep.join(entries))


def _run(env_key: str, command: str, args: list[str], *, env: dict[str, str] | None = None) -> tuple[str | None, str | None]:
    """The CLI's stdout, or None and a short failure code (``not_found``, ``timeout``); stderr is never kept."""
    binary = os.environ.get(env_key) or host_which(command) or command
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=READ_TIMEOUT_S, check=False,
                              cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "not_found"
    return proc.stdout, None


CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_READ_TIMEOUT_S = 10
CLAUDE_MAX_RESPONSE_BYTES = 65536
_CLAUDE_OPENER: Any = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so credentials are never forwarded to another location."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _fetch_claude_usage(token: str, *, timeout: float = CLAUDE_READ_TIMEOUT_S, opener: Any = None) -> tuple[dict[str, Any] | None, str | None]:
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": "claude-code/0.1.0",
    }
    req = urllib.request.Request(CLAUDE_USAGE_URL, headers=headers)
    active_opener = opener or _CLAUDE_OPENER
    if active_opener is None:
        active_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    try:
        if hasattr(active_opener, "open"):
            resp = active_opener.open(req, timeout=timeout)
        else:
            resp = active_opener(req, timeout=timeout)
        if isinstance(resp, dict):
            return resp, None
        with resp as response:
            raw_data = response.read(CLAUDE_MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return None, "session_expired"
        if exc.code == 429:
            return None, "rate_limited"
        return None, "provider_unreachable"
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, "provider_unreachable"

    try:
        text = raw_data.decode("utf-8") if isinstance(raw_data, (bytes, bytearray)) else str(raw_data)
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "invalid_response"

    if not isinstance(data, dict):
        return None, "invalid_response"
    return data, None


def _resolve_claude_credentials_path() -> tuple[Path | None, bool]:
    """Returns (target_credentials_file, is_configured_or_sandbox)."""
    env_dir = os.environ.get("JARVISOS_CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        p = Path(env_dir)
        target = p if p.is_file() else p / ".credentials.json"
        return target, True
    sandbox_cred = Path.home() / ".local" / "share" / "jarvisos-sandbox" / "state" / "claude" / ".credentials.json"
    if sandbox_cred.is_file():
        return sandbox_cred, True
    return None, False


def _parse_claude_usage_windows(data: dict[str, Any]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for key, block in data.items():
        if not isinstance(block, dict):
            continue
        used = block.get("utilization")
        if used is None:
            used = block.get("used_percent")
        if used is None or isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= float(used) <= 100:
            continue
        resets_at = _ts(block.get("resets_at"))
        label = None
        if key in ("five_hour", "5h"):
            label = "5-hour limit"
        elif key in ("seven_day", "weekly"):
            label = "Weekly limit"
        elif "opus" in key.lower():
            label = "Opus weekly"
        windows.append({
            "id": key,
            "label": label,
            "used_percent": float(used),
            "resets_at": _iso(resets_at),
        })

    order = {"five_hour": 0, "5h": 0, "seven_day": 1, "weekly": 1}
    windows.sort(key=lambda w: order.get(str(w["id"]), 2))
    return windows


def _attempt_claude_refresh(target: Path, *, opener: Any = None) -> tuple[dict[str, Any] | None, str | None]:
    """Attempt a bounded refresh through `agent-relay --json limits --refresh` with CLAUDE_CONFIG_DIR set to target's parent."""
    config_dir = str(target.parent)
    sub_env = os.environ.copy()
    sub_env["CLAUDE_CONFIG_DIR"] = config_dir
    sub_env["JARVISOS_CLAUDE_CONFIG_DIR"] = config_dir
    binary = os.environ.get("JARVISOS_AGENT_RELAY_BIN") or host_which("agent-relay") or "agent-relay"
    try:
        proc = subprocess.run(
            [binary, "--json", "limits", "--refresh"],
            capture_output=True,
            text=True,
            timeout=READ_TIMEOUT_S,
            check=False,
            cwd=tempfile.gettempdir(),
            stdin=subprocess.DEVNULL,
            env=sub_env,
        )
        stdout = proc.stdout
    except (subprocess.TimeoutExpired, OSError):
        stdout = None

    if stdout:
        doc = _json_document(stdout)
        if isinstance(doc, dict):
            agents = doc.get("agents")
            if isinstance(agents, list):
                claude_entry = next((a for a in agents if isinstance(a, dict) and a.get("agent_key") == "claude"), None)
                if claude_entry is not None and claude_entry.get("status") == "ok":
                    raw_windows = claude_entry.get("windows")
                    if isinstance(raw_windows, list) and len(raw_windows) > 0:
                        return claude_entry, None

    # Re-read target and retry _fetch_claude_usage once if token was refreshed on disk
    try:
        raw_text = target.read_text(encoding="utf-8")
        cred_data = json.loads(raw_text)
        if isinstance(cred_data, dict):
            oauth = cred_data.get("claudeAiOauth")
            if isinstance(oauth, dict):
                new_token = oauth.get("accessToken")
                if isinstance(new_token, str) and new_token.strip():
                    new_token = new_token.strip()
                    expires_at = oauth.get("expiresAt")
                    exp_s = None
                    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
                        exp_s = expires_at / 1000.0 if expires_at > 1e11 else float(expires_at)
                    if exp_s is None or exp_s > time.time():
                        data, error = _fetch_claude_usage(new_token, opener=opener)
                        if error is None and isinstance(data, dict):
                            windows = _parse_claude_usage_windows(data)
                            if windows:
                                return {"status": "ok", "as_of": _iso(datetime.now(UTC)), "windows": windows}, None
    except Exception:
        pass

    return {"status": "unavailable", "reason": "session_expired", "windows": []}, "session_expired"


def read_claude_limits(*, refresh: bool = False, opener: Any = None) -> tuple[dict[str, Any] | None, str | None]:
    target, is_configured = _resolve_claude_credentials_path()
    if not is_configured:
        relay_data, error = read_relay_limits(refresh=refresh)
        agents = relay_data.get("agents") if isinstance(relay_data, dict) else None
        entry = next((a for a in agents if isinstance(a, dict) and a.get("agent_key") == "claude"), None) if isinstance(agents, list) else None
        if entry is not None:
            return entry, None
        err = f"relay_{error}" if error in READ_ERRORS else (error or "credentials_missing")
        return None, err

    if target is None or not target.is_file():
        return {"status": "unavailable", "reason": "credentials_missing", "windows": []}, "credentials_missing"

    try:
        raw_text = target.read_text(encoding="utf-8")
        cred_data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError):
        return {"status": "unavailable", "reason": "credentials_missing", "windows": []}, "credentials_missing"

    if not isinstance(cred_data, dict):
        return {"status": "unavailable", "reason": "credentials_missing", "windows": []}, "credentials_missing"

    oauth = cred_data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return {"status": "unavailable", "reason": "session_expired", "windows": []}, "session_expired"

    token = oauth.get("accessToken")
    refresh_token = oauth.get("refreshToken")
    has_refresh = isinstance(refresh_token, str) and bool(refresh_token.strip())

    token_str = token.strip() if isinstance(token, str) else ""
    is_expired = not bool(token_str)

    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        exp_s = expires_at / 1000.0 if expires_at > 1e11 else float(expires_at)
        if exp_s <= time.time():
            is_expired = True

    if is_expired:
        if has_refresh:
            return _attempt_claude_refresh(target, opener=opener)
        return {"status": "unavailable", "reason": "session_expired", "windows": []}, "session_expired"

    data, error = _fetch_claude_usage(token_str, opener=opener)
    if error == "session_expired":
        if has_refresh:
            return _attempt_claude_refresh(target, opener=opener)
        return {"status": "unavailable", "reason": "session_expired", "windows": []}, "session_expired"

    if error is not None or data is None:
        err = error or "invalid_response"
        return {"status": "unavailable", "reason": err, "windows": []}, err

    windows = _parse_claude_usage_windows(data)
    if not windows:
        return {"status": "unavailable", "reason": "invalid_response", "windows": []}, "invalid_response"

    return {"status": "ok", "as_of": _iso(datetime.now(UTC)), "windows": windows}, None


def read_relay_limits(*, refresh: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    """Prefer a fresh cached Claude reading and retain it if a refresh fails."""
    def read(force: bool) -> tuple[dict[str, Any] | None, str | None]:
        args = ["--json", "limits"] + (["--refresh"] if force else [])
        stdout, error = _run("JARVISOS_AGENT_RELAY_BIN", "agent-relay", args)
        data = _json_document(stdout) if stdout is not None else None
        return data, None if data is not None else error or "no_json"

    def claude(data: dict[str, Any] | None) -> dict[str, Any] | None:
        agents = data.get("agents") if data else None
        return next((a for a in agents if isinstance(a, dict) and a.get("agent_key") == "claude"), None) if isinstance(agents, list) else None

    cached, _ = read(False)
    entry = claude(cached)
    now = datetime.now(UTC)
    usable = normalize_agent(entry, now)["status"] == "ok"
    observed = _ts(entry.get("as_of")) if entry else None
    if not refresh and usable and observed and abs((now - observed).total_seconds()) < ttl_seconds():
        return cached, None
    fresh, error = read(True)
    if usable and normalize_agent(claude(fresh), now)["status"] != "ok":
        return cached, None
    return fresh, error


def read_copilot_quota() -> tuple[dict[str, Any] | None, str | None]:
    stdout, error = _run("JARVISOS_GH_BIN", "gh", ["api", "copilot_internal/user"])
    data = _json_document(stdout) if stdout is not None else None
    return data, None if data is not None else error or "no_json"


def _json_document(text: str) -> dict[str, Any] | None:
    """The CLI's JSON object: the whole output, or else its last line that parses as an object."""
    for candidate in [text, *reversed(text.strip().splitlines())]:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def read_agy_quota() -> tuple[dict[str, Any] | None, str | None]:
    """One headless ``agy`` ``/quota`` reading (no model turn), or None and why: agy is missing, slow or prints no JSON."""
    stdout, error = _run("JARVISOS_AGY_BIN", "agy", ["-p", "/quota", "--output-format", "json"])
    data = _json_document(stdout) if stdout is not None else None
    return data, None if data is not None else error or "no_json"


def _query_codex_app_server(codex_home: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Execute codex app-server for a specific CODEX_HOME, distinguishing failure reasons without leaking secrets or paths."""
    binary = os.environ.get("JARVISOS_CODEX_BIN") or host_which("codex") or "codex"
    env = os.environ.copy()
    if codex_home:
        env["CODEX_HOME"] = codex_home
    try:
        proc = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                cwd=tempfile.gettempdir(), env=env)
    except OSError:
        return None, "not_found"

    messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
    stderr_lines: list[str] = []

    def collect_stdout() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                data = _json_document(line)
                if data is not None:
                    messages.put(data)
        finally:
            messages.put(None)

    def collect_stderr() -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                stderr_lines.append(line)
        except Exception:
            pass

    reader = threading.Thread(target=collect_stdout, daemon=True)
    reader.start()
    stderr_reader = threading.Thread(target=collect_stderr, daemon=True)
    stderr_reader.start()

    deadline = time.monotonic() + READ_TIMEOUT_S
    detected_error: list[str] = []

    def _check_stderr_auth() -> bool:
        text = "".join(stderr_lines).lower()
        return any(w in text for w in ("auth", "login", "unauthorized", "unauthenticated", "token", "credential"))

    def _stop_proc_and_drain() -> None:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except Exception:
                    pass
        stderr_reader.join(timeout=1)
        reader.join(timeout=1)

    def _decide_error(default: str = "no_json") -> str:
        _stop_proc_and_drain()
        if "auth_required" in detected_error or _check_stderr_auth():
            return "auth_required"
        return detected_error[0] if detected_error else default

    def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def response(request_id: int) -> dict[str, Any] | None:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Empty
            message = messages.get(timeout=remaining)
            if message is None:
                return None
            if message.get("id") == request_id and "error" in message:
                err_val = message.get("error")
                err_str = str(err_val).lower()
                if any(w in err_str for w in ("auth", "login", "unauthorized", "unauthenticated", "token", "credential")):
                    detected_error.append("auth_required")
                else:
                    detected_error.append("no_json")
                return None
            if message.get("id") == request_id:
                result = message.get("result")
                return result if isinstance(result, dict) else None

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "jarvisos_usage", "version": "0.1.0"}}})
        if response(1) is None:
            return None, _decide_error("no_json")
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        result = response(2)
        if result is None:
            return None, _decide_error("no_json")
        buckets = result.get("rateLimitsByLimitId")
        limits = buckets.get("codex") if isinstance(buckets, dict) else None
        if not isinstance(limits, dict):
            limits = result.get("rateLimits")
        if not isinstance(limits, dict):
            return None, _decide_error("no_json")
        windows = []
        for key in ("primary", "secondary"):
            window = limits.get(key)
            if not isinstance(window, dict):
                continue
            reset = window.get("resetsAt")
            try:
                resets_at = _iso(datetime.fromtimestamp(reset, UTC)) if isinstance(reset, (int, float)) and not isinstance(reset, bool) else None
            except (OverflowError, OSError, ValueError):
                resets_at = None
            windows.append({"id": key, "window_minutes": window.get("windowDurationMins"),
                            "used_percent": window.get("usedPercent"), "resets_at": resets_at})
        if not windows:
            return None, _decide_error("no_json")
        return {"status": "ok", "as_of": _iso(datetime.now(UTC)), "windows": windows}, None
    except queue.Empty:
        return None, _decide_error("timeout")
    except (OSError, ValueError):
        return None, _decide_error("no_json")
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                    proc.wait()
                except Exception:
                    pass
        reader.join(timeout=1)
        stderr_reader.join(timeout=1)
        for s in (proc.stdin, proc.stdout, proc.stderr):
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass


def _find_snapshot_paths() -> list[Path]:
    paths: list[Path] = []
    env_file = os.environ.get("JARVISOS_WORKER_QUOTA_FILE")
    if env_file:
        paths.append(Path(env_file).expanduser())
    paths.append(Path.cwd() / ".agent-relay" / "worker-quota.json")
    repo_root = Path(__file__).resolve().parents[4]
    paths.append(repo_root / ".agent-relay" / "worker-quota.json")
    marker = repo_root / ".git"
    if marker.is_file():
        try:
            text = marker.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                gitdir = (repo_root / text[len("gitdir:"):].strip()).resolve()
                if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                    paths.append(gitdir.parent.parent.parent / ".agent-relay" / "worker-quota.json")
        except OSError:
            pass
    ctrl = _resolve_control_root()
    if ctrl:
        paths.append(ctrl / ".agent-relay" / "worker-quota.json")
        paths.append(ctrl.parent / ".agent-relay" / "worker-quota.json")
        paths.append(ctrl / "worker-quota.json")
        paths.append(ctrl.parent / "worker-quota.json")
    paths.append(Path.home() / "jarvis-control" / "work" / ".agent-relay" / "worker-quota.json")
    paths.append(Path.home() / "jarvis-control" / "work" / "worker-quota.json")
    paths.append(Path.home() / ".agent-relay" / "worker-quota.json")
    return paths


def _load_codex_snapshot() -> tuple[dict[str, Any] | None, str | None]:
    """Read a fresh supervisor worker-quota.json Codex reading (if present and valid), preserving exact as_of."""
    now = datetime.now(UTC)
    override = os.environ.get("JARVISOS_WORKER_QUOTA_FILE")
    candidate_paths = [Path(override).expanduser()] if override else _find_snapshot_paths()
    for p in candidate_paths:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("schema") != "worker-quota.v1":
            continue
        pools = data.get("pools")
        if not isinstance(pools, dict):
            continue
        entry = pools.get("codex")
        if not isinstance(entry, dict):
            continue
        as_of_str = entry.get("as_of") or data.get("as_of")
        as_of_dt = _ts(as_of_str)
        if as_of_dt is None:
            continue
        age_s = (now - as_of_dt).total_seconds()
        if not (-60 <= age_s <= 1800):
            continue
        raw_windows = entry.get("windows")
        if not isinstance(raw_windows, dict) or not raw_windows:
            continue
        windows: list[dict[str, Any]] = []
        for key, val in raw_windows.items():
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                v_flt = float(val)
                used_pct = round((1.0 - v_flt) * 100.0, 1) if 0 <= v_flt <= 1 else round(v_flt, 1)
                window_mins = 300 if key in ("5h", "primary") else 10080 if key in ("weekly", "secondary") else None
                windows.append({
                    "id": str(key),
                    "window_minutes": window_mins,
                    "used_percent": used_pct,
                    "resets_at": None,
                })
        if windows:
            return {"status": "ok", "as_of": as_of_str, "windows": windows}, None
    return None, None


def read_codex_limits() -> tuple[dict[str, Any] | None, str | None]:
    """Ask the CLI for current account limits; never create a thread, run a turn or handle credentials."""
    codex_home = os.environ.get("JARVISOS_CODEX_HOME") or os.environ.get("CODEX_HOME")
    if not codex_home:
        sandbox_codex = Path.home() / ".local" / "share" / "jarvisos-sandbox" / "state" / "codex"
        if sandbox_codex.is_dir():
            codex_home = str(sandbox_codex)

    result, error = _query_codex_app_server(codex_home)
    if result is not None:
        return result, None

    # Retry once with default user ~/.codex if distinct and present
    user_codex = Path.home() / ".codex"
    is_distinct = True
    if codex_home:
        try:
            is_distinct = Path(codex_home).resolve() != user_codex.resolve()
        except Exception:
            is_distinct = True

    if user_codex.is_dir() and is_distinct:
        retry_res, retry_err = _query_codex_app_server(str(user_codex))
        if retry_res is not None:
            return retry_res, None
        if retry_err:
            error = retry_err

    # Fall back to reading a fresh supervisor worker-quota.json Codex reading
    snapshot_res, _ = _load_codex_snapshot()
    if snapshot_res is not None:
        return snapshot_res, None

    return None, error or "no_json"


def _resolve_control_root() -> Path | None:
    try:
        return relay_monitor.control_root()
    except Exception:
        pass
    env_dir = os.environ.get("RELAY_CONTINUITY_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return None


def _resolve_temp_pools_path(control_dir: Path | None = None) -> Path | None:
    override = os.environ.get("TEMP_POOLS_FILE")
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None
    if control_dir is not None:
        for c in (control_dir / "temp-pools.json", control_dir.parent / "temp-pools.json"):
            if c.is_file():
                return c
        return None
    root = _resolve_control_root()
    if root is not None:
        for c in (root / "temp-pools.json", root.parent / "temp-pools.json"):
            if c.is_file():
                return c
    default_path = Path.home() / "jarvis-control" / "work" / "temp-pools.json"
    if default_path.is_file():
        return default_path
    return None


def _resolve_reference_score(data: dict[str, Any]) -> float | None:
    if not isinstance(data, dict):
        return None
    val = data.get("reference_score")
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    ref_name = data.get("reference")
    scores = data.get("scores")
    if isinstance(ref_name, str) and isinstance(scores, dict):
        v = scores.get(ref_name)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _resolve_coding_reference_score(data: dict[str, Any]) -> float | None:
    if not isinstance(data, dict):
        return None
    val = data.get("reference_coding_index") or data.get("coding_reference_score")
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    coding_ref = data.get("coding_reference") or data.get("reference")
    coding_scores = data.get("coding_scores")
    if isinstance(coding_ref, str) and isinstance(coding_scores, dict):
        v = coding_scores.get(coding_ref)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _load_lists_doc(pools_path: Path | None) -> dict[str, Any]:
    if pools_path is None:
        return {}
    lists_file = pools_path.parent / "temp-pool-lists.json"
    if not lists_file.is_file():
        return {}
    try:
        data = json.loads(lists_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_bridge_states(pools_path: Path | None) -> dict[str, str]:
    data = _load_lists_doc(pools_path)
    bridge = data.get("bridge")
    if not isinstance(bridge, dict):
        return {}
    result: dict[str, str] = {}
    for k, v in bridge.items():
        if isinstance(k, str) and isinstance(v, dict):
            st = v.get("state")
            if isinstance(st, str):
                result[k] = st
    return result


def _verify_free_pool_liveness_and_quality(
    raw: dict[str, Any],
    pools_doc: dict[str, Any],
    lists_doc: dict[str, Any],
    now: datetime,
) -> bool:
    # 1. Dual curated indexes: index_score and coding_index must both meet reference
    ref_score = _resolve_reference_score(pools_doc)
    ref_coding = _resolve_coding_reference_score(pools_doc)
    if ref_score is None or ref_coding is None:
        return False

    score = raw.get("index_score") if raw.get("index_score") is not None else raw.get("intelligence_index")
    if score is None:
        score = raw.get("score")
    coding = raw.get("coding_index") if raw.get("coding_index") is not None else raw.get("coding_score")
    if score is None or coding is None:
        return False
    if isinstance(score, bool) or isinstance(coding, bool):
        return False
    if not isinstance(score, (int, float)) or not isinstance(coding, (int, float)):
        return False
    if score < ref_score or coding < ref_coding:
        return False

    # 2. Expiry check: future expires_at is never proof, but expired is immediately disqualified
    exp = _ts(raw.get("expires_at"))
    if exp is not None and exp <= now:
        return False

    # 3. Source free-list check: model ID must be present in a fresh source list (read_at < 3600s)
    model_id = str(raw.get("model_id") or raw.get("model") or "")
    if not model_id:
        return False
    pool_name = str(raw.get("name") or raw.get("id") or "")
    source_name = str(raw.get("source") or raw.get("provider") or raw.get("free_list") or "")

    sources = lists_doc.get("sources")
    if not isinstance(sources, dict) or not sources:
        return False

    matching_source: dict[str, Any] | None = None
    for src_v in sources.values():
        if isinstance(src_v, dict) and model_id in src_v.get("ids", []):
            matching_source = src_v
            break
    if matching_source is None and source_name in sources and isinstance(sources[source_name], dict):
        matching_source = sources[source_name]

    if matching_source is None:
        return False

    source_ids = matching_source.get("ids")
    if not isinstance(source_ids, list) or model_id not in source_ids:
        return False

    src_read_at = _ts(matching_source.get("read_at"))
    if src_read_at is None:
        return False
    if not (-60 <= (now - src_read_at).total_seconds() <= 3600):
        return False

    # 4. Zero price check where published: prompt and completion must both be 0
    pricing = (
        raw.get("pricing")
        or matching_source.get("pricing", {}).get(model_id)
        or matching_source.get("prices", {}).get(model_id)
        or lists_doc.get("pricing", {}).get(model_id)
        or lists_doc.get("prices", {}).get(model_id)
    )
    if pricing is not None:
        if not isinstance(pricing, dict):
            return False
        p = pricing.get("prompt") if "prompt" in pricing else pricing.get("prompt_price")
        c = pricing.get("completion") if "completion" in pricing else pricing.get("completion_price")
        def _is_zero(val: Any) -> bool:
            if isinstance(val, bool) or val is None:
                return False
            try:
                return float(val) == 0.0
            except (ValueError, TypeError):
                return False
        if not _is_zero(p) or not _is_zero(c):
            return False

    # 5. Bounded recent free-only response proof exists and is fresh
    candidates: list[dict[str, Any]] = []
    if isinstance(raw.get("live_probe"), dict):
        candidates.append(raw["live_probe"])
    if isinstance(raw.get("probe"), dict):
        candidates.append(raw["probe"])
    if raw.get("live_status") and raw.get("live_ok_at"):
        candidates.append({"status": raw["live_status"], "at": raw["live_ok_at"]})
    for d in (lists_doc.get("probes"), lists_doc.get("live_probes"), matching_source.get("probes"), lists_doc.get("bridge")):
        if isinstance(d, dict):
            for k in (model_id, pool_name, f"temp-{pool_name}"):
                val = d.get(k)
                if isinstance(val, dict):
                    candidates.append(val)

    if not candidates:
        return False

    # Fail closed immediately if ANY candidate proof reports 404 or 402 with fresh or missing timestamp
    for proof in candidates:
        p_status = str(proof.get("live_status") or proof.get("status") or proof.get("state") or "").lower()
        p_code = proof.get("status_code") or proof.get("code")
        ts_raw = proof.get("live_ok_at") or proof.get("at") or proof.get("timestamp") or proof.get("read_at")
        proof_dt = _ts(ts_raw)
        is_fresh = proof_dt is not None and (-60 <= (now - proof_dt).total_seconds() <= 3600)
        if p_code in (404, "404") or p_status in ("404", "not_found"):
            if proof_dt is None or is_fresh:
                return False
        if p_code in (402, "402") or p_status in ("402", "payment_required", "payment_response"):
            if proof_dt is None or is_fresh:
                return False

    # Find the most recent fresh OK proof
    newest_ok_dt: datetime | None = None
    has_fresh_ok = False
    for proof in candidates:
        p_status = str(proof.get("live_status") or proof.get("status") or proof.get("state") or "").lower()
        p_code = proof.get("status_code") or proof.get("code")
        if p_code in (404, 402, 500, 502, 503, 504) or p_status in (
            "404", "not_found", "402", "payment_required", "payment_response", "error", "timeout", "failed", "unusable"
        ):
            continue
        if p_status not in ("ok", "success", "active", "throttled", "exhausted"):
            continue
        ts_raw = proof.get("live_ok_at") or proof.get("at") or proof.get("timestamp") or proof.get("read_at")
        proof_dt = _ts(ts_raw)
        if proof_dt is not None and (-60 <= (now - proof_dt).total_seconds() <= 3600):
            has_fresh_ok = True
            if newest_ok_dt is None or proof_dt > newest_ok_dt:
                newest_ok_dt = proof_dt

    if not has_fresh_ok:
        return False

    # Check if any error/unusable proof beats the newest OK proof
    for proof in candidates:
        p_status = str(proof.get("live_status") or proof.get("status") or proof.get("state") or "").lower()
        p_code = proof.get("status_code") or proof.get("code")
        if p_code in (500, 502, 503, 504) or p_status in ("error", "timeout", "failed", "unusable"):
            ts_raw = proof.get("live_ok_at") or proof.get("at") or proof.get("timestamp") or proof.get("read_at")
            proof_dt = _ts(ts_raw)
            if proof_dt is None or newest_ok_dt is None or proof_dt >= newest_ok_dt:
                return False

    return True


def normalize_free_model(
    raw: Any,
    reference_score: float | None = None,
    reference_coding_index: float | None = None,
    bridge_states: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    raw_id = raw.get("id") or raw.get("name")
    pool_id = _short(str(raw_id)) if raw_id else None
    if not pool_id:
        return None

    raw_label = raw.get("label") or raw.get("name") or raw.get("model") or pool_id
    label = _short(str(raw_label)) or pool_id

    raw_source = raw.get("source") or raw.get("provider") or raw.get("free_list") or "free"
    source = _short(str(raw_source)) or "free"

    raw_model = str(raw.get("model_id") or raw.get("model") or "")
    model_id = ""
    if raw_model and re.match(r"^[A-Za-z0-9_.&·()/: -]{1,80}$", raw_model):
        try:
            if not relay_monitor.observe().devctx.find_secret(raw_model):
                model_id = raw_model
        except Exception:
            model_id = raw_model

    score = raw.get("intelligence_index")
    if score is None:
        score = raw.get("index_score")
    if score is None:
        score = raw.get("score")
    intelligence_index: int | None = None
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        intelligence_index = int(round(float(score)))

    coding_val = raw.get("coding_index") if raw.get("coding_index") is not None else raw.get("coding_score")
    coding_index: int | None = None
    if isinstance(coding_val, (int, float)) and not isinstance(coding_val, bool):
        coding_index = int(round(float(coding_val)))

    bridge = bridge_states or {}
    bridge_st = (
        bridge.get(pool_id)
        or bridge.get(str(raw_id))
        or bridge.get(f"temp-{pool_id}")
        or bridge.get(f"temp-{raw_id}")
    )
    raw_status = str(raw.get("status") or "").lower()

    rem = raw.get("remaining_percent")
    if rem is None and raw.get("used_percent") is not None:
        u = raw.get("used_percent")
        if isinstance(u, (int, float)) and not isinstance(u, bool) and 0 <= u <= 100:
            rem = 100.0 - float(u)

    remaining_percent: float | None = None
    if isinstance(rem, (int, float)) and not isinstance(rem, bool):
        remaining_percent = round(max(0.0, min(100.0, float(rem))), 1)

    limit_kind_raw = str(raw.get("limit_kind") or "").lower()
    if limit_kind_raw in ("ring", "live_dot"):
        limit_kind = limit_kind_raw
    elif remaining_percent is not None:
        limit_kind = "ring"
    else:
        limit_kind = "live_dot"

    is_exhausted = (
        raw.get("exhausted") is True
        or raw_status == "exhausted"
        or bridge_st == "exhausted"
        or (remaining_percent is not None and remaining_percent <= 0.5)
    )

    is_throttled = (
        raw.get("throttled") is True
        or raw_status == "throttled"
        or bridge_st in ("throttled", "suspect")
        or raw.get("throttled_until") is not None
    )

    is_unqualified = (
        raw.get("unqualified") is True
        or raw_status == "unqualified"
        or raw.get("admission") in ("below_index", "below_coding_index", "unindexed", "no_reference")
        or (reference_score is not None and intelligence_index is not None and intelligence_index < reference_score)
        or (reference_coding_index is not None and coding_index is not None and coding_index < reference_coding_index)
    )

    if is_exhausted:
        status = "exhausted"
    elif is_throttled:
        status = "throttled"
    elif is_unqualified:
        status = "unqualified"
    elif raw_status in ("active", "exhausted", "throttled", "unqualified"):
        status = raw_status
    else:
        status = "active"

    res = {
        "id": pool_id,
        "label": label,
        "source": source,
        "model_id": model_id,
        "intelligence_index": intelligence_index,
        "status": status,
        "limit_kind": limit_kind,
        "remaining_percent": remaining_percent,
        "exhausted": bool(is_exhausted or status == "exhausted"),
    }
    if coding_index is not None:
        res["coding_index"] = coding_index
    return res


def read_free_models(control_dir: Path | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    moment = now or datetime.now(UTC)
    path = _resolve_temp_pools_path(control_dir)
    if path is None or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    pools = data if isinstance(data, list) else data.get("pools") if isinstance(data, dict) else None
    if not isinstance(pools, list):
        return []

    ref_score = _resolve_reference_score(data)
    ref_coding = _resolve_coding_reference_score(data)
    if ref_score is None or ref_coding is None:
        return []

    lists_doc = _load_lists_doc(path)
    bridge_states = _load_bridge_states(path)
    results: list[dict[str, Any]] = []
    for raw in pools:
        if not isinstance(raw, dict):
            continue
        if not _verify_free_pool_liveness_and_quality(raw, data, lists_doc, moment):
            continue
        norm = normalize_free_model(raw, reference_score=ref_score, reference_coding_index=ref_coding, bridge_states=bridge_states)
        if norm is not None:
            results.append(norm)
    return results


def _resolve_prepaid_balances_path(control_dir: Path | None = None) -> Path | None:
    override = os.environ.get("PREPAID_BALANCES_FILE")
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None
    if control_dir is not None:
        for c in (control_dir / "prepaid-balances.json", control_dir.parent / "prepaid-balances.json"):
            if c.is_file():
                return c
        return None
    root = _resolve_control_root()
    if root is not None:
        for c in (root / "prepaid-balances.json", root.parent / "prepaid-balances.json"):
            if c.is_file():
                return c
    default_path = Path.home() / "jarvis-control" / "work" / "prepaid-balances.json"
    if default_path.is_file():
        return default_path
    return None


def normalize_prepaid_balance(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    provider_name = _short(str(raw.get("provider") or raw.get("name") or ""))
    if not provider_name:
        return None
    raw_label = raw.get("label") or raw.get("provider") or provider_name
    label = _short(str(raw_label)) or provider_name

    b_usd = raw.get("balance_usd")
    balance_usd: float | None = None
    if isinstance(b_usd, (int, float)) and not isinstance(b_usd, bool):
        balance_usd = round(float(b_usd), 2)

    s_eur = raw.get("spent_eur")
    spent_eur: float | None = None
    if isinstance(s_eur, (int, float)) and not isinstance(s_eur, bool):
        spent_eur = round(float(s_eur), 2)

    c_eur = raw.get("cap_eur")
    cap_eur: float | None = None
    if isinstance(c_eur, (int, float)) and not isinstance(c_eur, bool):
        cap_eur = round(float(c_eur), 2)

    raw_status = str(raw.get("status") or "").lower()
    status = "ok" if raw_status in ("ok", "active", "available") else "unavailable"
    if not raw_status and (balance_usd is not None or spent_eur is not None or cap_eur is not None):
        status = "ok"

    raw_reason = raw.get("reason")
    reason = _short(str(raw_reason)) if raw_reason else None

    return {
        "provider": provider_name,
        "label": label,
        "mode": "read_only",
        "balance_usd": balance_usd,
        "spent_eur": spent_eur,
        "cap_eur": cap_eur,
        "status": status,
        "reason": reason,
    }


def read_prepaid_balances(control_dir: Path | None = None) -> list[dict[str, Any]]:
    path = _resolve_prepaid_balances_path(control_dir)
    if path is None or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    entries = (
        data
        if isinstance(data, list)
        else (data.get("balances") or data.get("accounts") or data.get("prepaid_balances"))
        if isinstance(data, dict)
        else None
    )
    if not isinstance(entries, list):
        return []

    results: list[dict[str, Any]] = []
    for raw in entries:
        norm = normalize_prepaid_balance(raw)
        if norm is not None:
            results.append(norm)
    return results


def read_all(*, refresh: bool = False) -> dict[str, Any]:
    # Independent status sources share one bounded wait.
    with ThreadPoolExecutor(max_workers=4) as executor:
        jobs = [executor.submit(read_claude_limits, refresh=refresh), executor.submit(read_agy_quota),
                executor.submit(read_codex_limits), executor.submit(read_copilot_quota)]
        (claude, claude_error), (agy, agy_error), (codex, codex_error), (copilot, copilot_error) = [job.result() for job in jobs]
    return {"claude": claude, "agy": agy, "codex": codex, "copilot": copilot,
            "errors": {"claude": claude_error, "agy": agy_error, "codex": codex_error, "copilot": copilot_error}}


def _buckets(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Every ``buckets`` entry in the agy reading (documented under ``command.data.groups[]``), bounded in depth."""
    if depth > 6:
        return []
    if isinstance(node, list):
        return [b for item in node for b in _buckets(item, depth + 1)]
    if not isinstance(node, dict):
        return []
    found = [b for b in node.get("buckets", []) if isinstance(b, dict)] if isinstance(node.get("buckets"), list) else []
    return found + [b for key, value in node.items() if key != "buckets" for b in _buckets(value, depth + 1)]


FRONTIER_MARKERS = ("opus", "argon")


def agy_entry(reading: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    """The agy reading as a Relay-style entry: Gemini 5-hour/weekly buckets fill the fixed rows, Argon is the
    frontier-model row. Claude & GPT's shared 3p windows retain their AGY source and pool identity."""
    if reading is None:
        return None
    windows = []
    for bucket in _buckets(reading):
        bucket_id = _short(bucket.get("id"))
        fraction = bucket.get("remaining_fraction")
        if bucket_id is None or bucket.get("disabled") is True:
            continue
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 <= fraction <= 1:
            continue
        family, _, span = bucket_id.lower().rpartition("-")
        if family == "3p":
            if span not in ("5h", "weekly"):
                continue
            kind, label = "pool", f"Claude & GPT · {'5-hour' if span == '5h' else span}"
        elif family == "gemini":
            kind, label = {"5h": "five_hour", "weekly": "weekly"}.get(span, "pool"), None
        else:
            name = family.replace("-", " ").capitalize() or bucket_id
            kind, label = "pool", f"{name} · {'5-hour' if span == '5h' else span}"
        windows.append({"id": bucket_id, "label": label, "kind": kind, "used_percent": round((1 - fraction) * 100, 3),
                        "resets_at": bucket.get("reset_time")})
    return {"status": "ok", "as_of": _iso(now), "windows": windows}


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _short(value: Any) -> str | None:
    """A short window id or label; anything token-like or secret-like is dropped, never shown."""
    if not isinstance(value, str) or not _ID.match(value) or _TOKENISH.search(value):
        return None
    return None if relay_monitor.observe().devctx.find_secret(value) else value


def window_kind(window: dict[str, Any]) -> str:
    """``five_hour``, ``weekly``, ``frontier`` (the provider's top model pool: Claude Opus, Antigravity Argon, shown
    on one shared row) or ``pool`` (any other model-specific window, shown on its own row)."""
    key = str(window.get("id") or "").strip().lower()
    if any(marker in key for marker in FRONTIER_MARKERS):
        return "frontier"
    if window.get("kind") in ("five_hour", "weekly", "pool"):
        return str(window["kind"])
    minutes = window.get("window_minutes")
    if isinstance(minutes, int) and not isinstance(minutes, bool):
        if minutes == 300:
            return "five_hour"
        if minutes == 10080:
            return "weekly"
    if key in FIVE_HOUR_IDS:
        return "five_hour"
    if key in WEEKLY_IDS:
        return "weekly"
    return "pool"


def normalize_window(window: Any, now: datetime, as_of: datetime | None) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    used = window.get("used_percent")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
        return None
    resets_at = _ts(window.get("resets_at"))
    reset_passed = resets_at is not None and resets_at <= now
    return {
        "id": _short(window.get("id")) or "window",
        "label": _short(window.get("label")),
        "kind": window_kind(window),
        "used_percent": round(float(used), 1),
        "remaining_percent": round(100.0 - float(used), 1),
        "resets_at": _iso(resets_at),
        "exhausted": used >= EXHAUSTED_PERCENT and not reset_passed,
        # A reset in the past means the provider has refilled since this reading: the number is out of date.
        "stale": reset_passed or as_of is None or now - as_of > STALE_AFTER,
    }


def normalize_agent(entry: Any, now: datetime) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {"status": "unavailable", "reason": "no_reading", "windows": []}
    status = str(entry.get("status") or "ok").lower()
    if status in AUTH_STATUSES:
        return {"status": "unauthenticated", "reason": "not_authenticated", "windows": []}
    if status != "ok":
        return {"status": "unavailable", "reason": _short(entry.get("reason")) or _short(status) or "provider_error", "windows": []}
    as_of = _ts(entry.get("as_of"))
    raw = entry.get("windows")
    windows = [w for w in (normalize_window(item, now, as_of) for item in raw) if w] if isinstance(raw, list) else []
    if not windows:
        return {"status": "unavailable", "reason": "no_usage_windows", "as_of": _iso(as_of), "windows": []}
    return {"status": "ok", "as_of": _iso(as_of), "windows": windows}


def copilot_entry(reading: dict[str, Any], now: datetime) -> dict[str, Any]:
    def unavailable(reason: str) -> dict[str, Any]:
        return {"status": "unavailable", "reason": reason, "windows": []}

    if reading.get("cli_enabled") is False:
        return unavailable("cli_disabled")
    snapshots = reading.get("quota_snapshots")
    bucket = snapshots.get("premium_interactions") if isinstance(snapshots, dict) else None
    if not isinstance(bucket, dict):
        return unavailable("bucket_missing")
    if bucket.get("unlimited") is True:
        percent = 100.0
    elif bucket.get("has_quota") is False:
        if bucket.get("entitlement") in (0, 0.0):
            return unavailable("no_entitlement")
        percent = 0.0
    else:
        raw_percent = bucket.get("percent_remaining")
        remaining = bucket.get("remaining")
        if (isinstance(raw_percent, bool) or not isinstance(raw_percent, (int, float)) or not math.isfinite(raw_percent)
                or not 0 <= raw_percent <= 100 or (remaining is not None and
                (isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not math.isfinite(remaining)))):
            return unavailable("invalid_reading")
        percent = 0.0 if remaining is not None and remaining <= 0 else float(raw_percent)
    reset = reading.get("quota_reset_date")
    try:
        reset_at = datetime.strptime(reset, "%Y-%m-%d").replace(tzinfo=UTC) if isinstance(reset, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", reset) else None
    except ValueError:
        reset_at = None
    return {"status": "ok", "as_of": _iso(now), "windows": [{"id": "premium_interactions",
            "label": "Monthly premium requests", "kind": "pool", "used_percent": 100 - percent,
            "resets_at": _iso(reset_at)}]}


READ_ERRORS = ("not_found", "timeout", "no_json", "auth_required")
CLAUDE_ERRORS = {"session_expired", "credentials_missing", "rate_limited", "provider_unreachable", "invalid_response"}
UNREADABLE = {"relay": "relay_limits_unreadable", "claude": "credentials_missing", "agy": "agy_quota_unreadable", "codex": "codex_limits_unreadable", "copilot": "copilot_quota_unreadable"}


def _reason(source: str, error: Any) -> str:
    """``relay_not_found``, ``agy_timeout``…, or the generic unreadable reason when the cause is unknown."""
    if error in CLAUDE_ERRORS or str(error).startswith("relay_"):
        return str(error)
    return f"{source}_{error}" if error in READ_ERRORS else UNREADABLE.get(source, "provider_error")


def normalize(readings: dict[str, Any], now: datetime, *, read_at: datetime | None = None) -> dict[str, Any]:
    relay = readings.get("relay")
    raw_errors = readings.get("errors")
    errors: dict[str, Any] = raw_errors if isinstance(raw_errors, dict) else {}
    agents = relay.get("agents") if isinstance(relay, dict) else None
    by_key = {a.get("agent_key"): a for a in agents if isinstance(a, dict)} if isinstance(agents, list) else {}
    providers = []
    for key, label, source_key in PROVIDERS:
        if source_key == "agy":
            source, entry = "agy /quota", agy_entry(readings.get("agy"), read_at or now)
            body = normalize_agent(entry, now) if entry else {"status": "unavailable", "reason": _reason("agy", errors.get("agy")), "windows": []}
        elif source_key == "codex":
            source, entry = "codex account/rateLimits/read", readings.get("codex")
            body = (normalize_agent(entry, now) if isinstance(entry, dict)
                    else {"status": "unavailable", "reason": _reason("codex", errors.get("codex")), "windows": []})
        elif source_key == "copilot":
            source, entry = "gh copilot_internal/user", readings.get("copilot")
            if isinstance(entry, dict):
                body = copilot_entry(entry, read_at or now)
                if body["status"] == "ok":
                    body = normalize_agent(body, now)
            else:
                body = {"status": "unavailable", "reason": _reason("copilot", errors.get("copilot")), "windows": []}
        else:
            claude_entry = readings.get("claude")
            if claude_entry is None and "relay" in readings and by_key.get(source_key):
                source = "agent-relay limits"
                body = normalize_agent(by_key.get(source_key), now)
            elif isinstance(claude_entry, dict):
                source = "agent-relay limits" if "agent_key" in claude_entry else "claude live"
                body = normalize_agent(claude_entry, now)
            else:
                claude_err = errors.get("claude") or errors.get("relay")
                if claude_err in CLAUDE_ERRORS:
                    source = "claude live"
                    body = {"status": "unavailable", "reason": claude_err, "windows": []}
                else:
                    source = "agent-relay limits"
                    body = (normalize_agent(by_key.get(source_key), now) if isinstance(relay, dict)
                            else {"status": "unavailable", "reason": _reason("relay", errors.get("relay") or errors.get("claude")), "windows": []})
        providers.append({"provider": key, "label": label, "source": source, **body})

    raw_free = readings.get("free_models")
    if raw_free is None:
        free_models = read_free_models(now=now)
    elif isinstance(raw_free, list):
        free_models = [m for item in raw_free if (m := normalize_free_model(item)) is not None]
    else:
        free_models = []

    raw_prepaid = readings.get("prepaid_balances")
    if raw_prepaid is None:
        prepaid_balances = read_prepaid_balances()
    elif isinstance(raw_prepaid, list):
        prepaid_balances = [b for item in raw_prepaid if (b := normalize_prepaid_balance(item)) is not None]
    else:
        prepaid_balances = []

    return {
        "state": "ok",
        "read_at": _iso(now),
        "providers": providers,
        "free_models": free_models,
        "prepaid_balances": prepaid_balances,
    }


class QuotaCache:
    """At most one limits reading per TTL; concurrent requests share the one in flight."""

    def __init__(self, reader: Reader = read_all, clock: Callable[[], datetime] | None = None):
        self.reader = reader
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._reading: dict[str, Any] = {}
        self._read_at: datetime | None = None
        self._generation = 0
        self._last_good: dict[str, dict[str, Any]] = {}

    def get(self, *, refresh: bool = False) -> dict[str, Any]:
        generation = self._generation
        with self._lock:
            now = self.clock()
            ttl = timedelta(seconds=ttl_seconds())
            if self._read_at is None or now - self._read_at >= ttl or (refresh and generation == self._generation):
                self._reading = (read_all(refresh=refresh) if self.reader is read_all else self.reader()) or {}
                self._read_at = self.clock()
                self._generation += 1
            payload = normalize(self._reading, self.clock(), read_at=self._read_at)
            for index, body in enumerate(payload["providers"]):
                key = body["provider"]
                if body["status"] == "ok":
                    self._last_good[key] = body
                elif body["status"] == "unavailable" and key in self._last_good:
                    previous = self._last_good[key]
                    restored = normalize_agent(previous, self.clock())
                    payload["providers"][index] = {**previous, **restored, "last_error": body["reason"]}
            payload["read_at"] = _iso(self._read_at)
            payload["next_read_at"] = _iso(self._read_at + ttl)
            return payload


_cache = QuotaCache()


def quotas(*, refresh: bool = False) -> dict[str, Any]:
    return _cache.get(refresh=refresh)
