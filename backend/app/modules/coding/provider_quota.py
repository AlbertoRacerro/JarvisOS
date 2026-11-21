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


def _run(env_key: str, command: str, args: list[str]) -> tuple[str | None, str | None]:
    """The CLI's stdout, or None and a short failure code (``not_found``, ``timeout``); stderr is never kept."""
    binary = os.environ.get(env_key) or host_which(command) or command
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=READ_TIMEOUT_S, check=False,
                              cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL)
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
    if not isinstance(token, str) or not token.strip():
        return {"status": "unavailable", "reason": "session_expired", "windows": []}, "session_expired"
    token = token.strip()

    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        exp_s = expires_at / 1000.0 if expires_at > 1e11 else float(expires_at)
        if exp_s <= time.time():
            return {"status": "unavailable", "reason": "session_expired", "windows": []}, "session_expired"

    data, error = _fetch_claude_usage(token, opener=opener)
    if error is not None or data is None:
        err = error or "invalid_response"
        return {"status": "unavailable", "reason": err, "windows": []}, err

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

    if not windows:
        return {"status": "unavailable", "reason": "invalid_response", "windows": []}, "invalid_response"

    order = {"five_hour": 0, "5h": 0, "seven_day": 1, "weekly": 1}
    windows.sort(key=lambda w: order.get(str(w["id"]), 2))
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


def read_codex_limits() -> tuple[dict[str, Any] | None, str | None]:
    """Ask the CLI for current account limits; never create a thread, run a turn or handle credentials."""
    binary = os.environ.get("JARVISOS_CODEX_BIN") or host_which("codex") or "codex"
    env = os.environ.copy()
    codex_home = os.environ.get("JARVISOS_CODEX_HOME") or os.environ.get("CODEX_HOME")
    if not codex_home:
        sandbox_codex = Path.home() / ".local" / "share" / "jarvisos-sandbox" / "state" / "codex"
        if sandbox_codex.is_dir():
            codex_home = str(sandbox_codex)
    if codex_home:
        env["CODEX_HOME"] = codex_home
    try:
        proc = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                cwd=tempfile.gettempdir(), env=env)
    except OSError:
        return None, "not_found"
    messages: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def collect() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                data = _json_document(line)
                if data is not None:
                    messages.put(data)
        finally:
            messages.put(None)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    deadline = time.monotonic() + READ_TIMEOUT_S

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
            if message is None or (message.get("id") == request_id and "error" in message):
                return None
            if message.get("id") == request_id:
                result = message.get("result")
                return result if isinstance(result, dict) else None

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "jarvisos_usage", "version": "0.1.0"}}})
        if response(1) is None:
            return None, "no_json"
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        result = response(2)
        if result is None:
            return None, "no_json"
        buckets = result.get("rateLimitsByLimitId")
        limits = buckets.get("codex") if isinstance(buckets, dict) else None
        if not isinstance(limits, dict):
            limits = result.get("rateLimits")
        if not isinstance(limits, dict):
            return None, "no_json"
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
        return {"status": "ok", "as_of": _iso(datetime.now(UTC)), "windows": windows}, None
    except queue.Empty:
        return None, "timeout"
    except (OSError, ValueError):
        return None, "no_json"
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        reader.join(timeout=1)
        if proc.stdin is not None:
            proc.stdin.close()
        if proc.stdout is not None:
            proc.stdout.close()


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


READ_ERRORS = ("not_found", "timeout", "no_json")
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
    return {"state": "ok", "read_at": _iso(now), "providers": providers}


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
