"""Read-only provider quota rings for Coding → Runtime (spec 217).

Claude usage windows come from ``agent-relay --json limits --refresh`` (the source spec 195 uses).
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
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
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


def read_relay_limits() -> tuple[dict[str, Any] | None, str | None]:
    """One ``agent-relay`` limits reading, or None and why: the tool is missing, slow or prints no JSON."""
    stdout, error = _run("JARVISOS_AGENT_RELAY_BIN", "agent-relay", ["--json", "limits", "--refresh"])
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
    try:
        proc = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                cwd=tempfile.gettempdir())
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


def read_all() -> dict[str, Any]:
    # Independent status sources share one bounded wait, so a slow CLI cannot delay starting the other reads.
    with ThreadPoolExecutor(max_workers=3) as executor:
        jobs = [executor.submit(reader) for reader in (read_relay_limits, read_agy_quota, read_codex_limits)]
        (relay, relay_error), (agy, agy_error), (codex, codex_error) = [job.result() for job in jobs]
    return {"relay": relay, "agy": agy, "codex": codex,
            "errors": {"relay": relay_error, "agy": agy_error, "codex": codex_error}}


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
        return {"status": "unavailable", "reason": _short(status) or "provider_error", "windows": []}
    as_of = _ts(entry.get("as_of"))
    raw = entry.get("windows")
    windows = [w for w in (normalize_window(item, now, as_of) for item in raw) if w] if isinstance(raw, list) else []
    if not windows:
        return {"status": "unavailable", "reason": "no_usage_windows", "as_of": _iso(as_of), "windows": []}
    return {"status": "ok", "as_of": _iso(as_of), "windows": windows}


READ_ERRORS = ("not_found", "timeout", "no_json")
UNREADABLE = {"relay": "relay_limits_unreadable", "agy": "agy_quota_unreadable", "codex": "codex_limits_unreadable"}


def _reason(source: str, error: Any) -> str:
    """``relay_not_found``, ``agy_timeout``…, or the generic unreadable reason when the cause is unknown."""
    return f"{source}_{error}" if error in READ_ERRORS else UNREADABLE[source]


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
        else:
            source = "agent-relay limits"
            body = (normalize_agent(by_key.get(source_key), now) if isinstance(relay, dict)
                    else {"status": "unavailable", "reason": _reason("relay", errors.get("relay")), "windows": []})
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

    def get(self, *, refresh: bool = False) -> dict[str, Any]:
        generation = self._generation
        with self._lock:
            now = self.clock()
            ttl = timedelta(seconds=ttl_seconds())
            if self._read_at is None or now - self._read_at >= ttl or (refresh and generation == self._generation):
                self._reading = self.reader() or {}
                self._read_at = self.clock()
                self._generation += 1
            payload = normalize(self._reading, self.clock(), read_at=self._read_at)
            payload["read_at"] = _iso(self._read_at)
            payload["next_read_at"] = _iso(self._read_at + ttl)
            return payload


_cache = QuotaCache()


def quotas(*, refresh: bool = False) -> dict[str, Any]:
    return _cache.get(refresh=refresh)
