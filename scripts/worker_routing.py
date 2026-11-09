#!/usr/bin/env python3
"""Quota-aware worker router for coordinators (difficulty x risk x capability).

Coordinators plan, review and integrate; bounded implementation goes to the cheapest worker that is adequate for
the task and whose subscription pool still has headroom. Required tier = max(difficulty, risk), low=1, medium=2,
high=3. A model qualifies when its capability is at least that tier and its pool's free fraction (the minimum over
the pool's windows) is at least the pool's reserve (RESERVES); a window that resets within SPEND_DOWN may be spent to 0
(use it or lose it, spec 255). A pool that is disabled or unreadable is skipped (fail-closed per pool). Capability is
per effort, never per brand: Flash and Argon at medium take tier 2, at high tier 3, until measured results say
otherwise. Candidates are ordered by another family than `--review-of` first (never required), then smallest capability
surplus (effort follows the task), then the gemini family, then the quota that would expire unused soonest (binding
window's free fraction per hour to its reset), then most free quota, then table order. One `agy /quota` reading feeds
the gemini, argon (plus its own weekly bucket) and agy3p (third-party Sonnet/Opus) pools. Nothing qualifying exits 3
with reason `no_worker`: the coordinator then does the work itself or waits.

Quota is read without a model turn: Codex through `codex app-server` `account/rateLimits/read`, Gemini through
`agy -p /quota`, Copilot through `gh api copilot_internal/user`, Claude through `agent-relay --json limits --refresh`. This tool only reads quota and prints a decision; it launches nothing, writes nothing and holds no
credentials. Launch lines require exported absolute WORKTREE and TASK_FILE, LABEL, AUTHORITY and SPEC (WALL_HOURS optional). Stdlib only.
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import math
import os
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_ARGON_SPEC = importlib.util.spec_from_file_location("relay_argon", Path(__file__).resolve().parent / "relay_argon.py")
assert _ARGON_SPEC and _ARGON_SPEC.loader
relay_argon = sys.modules.get("relay_argon") or importlib.util.module_from_spec(_ARGON_SPEC)
if "relay_argon" not in sys.modules:
    sys.modules["relay_argon"] = relay_argon
    _ARGON_SPEC.loader.exec_module(relay_argon)

RESERVE = 0.05
# Per-pool reserve. The Claude pool is also what the coordinator spends: if it is skipped the coordinator would do
# the work itself at higher cost, so delegating to a cheaper Claude worker beats `no_worker`; it is skipped only
# when practically exhausted. Below CLAUDE_LOW_FREE a chosen Claude worker carries a warning note.
RESERVES = {"claude": 0.03, "codex": RESERVE, "copilot": RESERVE, "gemini": RESERVE, "argon": RESERVE, "agy3p": RESERVE}
# Use it or lose it (spec 255): a window that resets within this horizon may be spent down to 0.
SPEND_DOWN = timedelta(hours=24)
UNKNOWN_RESET_HOURS = 168.0
CLAUDE_LOW_FREE = 0.40
CLAUDE_LOW_NOTE = ("claude_pool_low — keep worker task small; coordinator should prefer waiting for a cheaper "
                   "pool reset if the task is not urgent")
# Among equally adequate models the gemini family goes first; the others follow the use-it-or-lose-it score.
FIRST_POOLS = ("gemini", "argon")
FRESHNESS = 900  # seconds a Claude (agent-relay) reading may be old, as the supervisor's reading_freshness
READ_TIMEOUT = 15  # timeout for readers other than agy; all readers run in parallel within TOTAL_TIMEOUT
AGY_READ_TIMEOUT = 40  # agy /quota is slow, typically ~8 s with spikes over 15 s
TOTAL_TIMEOUT = AGY_READ_TIMEOUT + 5
CACHE_TTL = 900  # seconds a summary stays valid inside a long-lived supervisor
FAILED_CACHE_TTL = 60
LEVELS = {"low": 1, "medium": 2, "high": 3}
REVIEW = {"high": "coordinator_full_diff_review", "medium": "coordinator_spot_check", "low": "tests_only"}
NO_WORKER_EXIT = 3
GEMINI_BUCKETS = {"gemini-5h": "5h", "gemini-weekly": "weekly"}
# One `agy /quota` document feeds three pools: the general Gemini pool, Argon (which also has its own weekly
# bucket) and the third-party buckets agy Sonnet and Opus run on.
AGY_POOLS = {"gemini": GEMINI_BUCKETS, "argon": {**GEMINI_BUCKETS, "gemini-argon-wtu-weekly": "argon-weekly"},
             "agy3p": {"3p-5h": "5h", "3p-weekly": "weekly"}}
AGY_DOC_TTL = 60
CODEX_WINDOWS = {"primary": "5h", "secondary": "weekly"}
CLAUDE_FIVE_HOUR = {"session", "five_hour", "5h"}
CLAUDE_WEEKLY = {"weekly", "seven_day", "week", "7d"}
UNREADABLE = "worker quota: unreadable"
# The supervisor persists its last good reading here so a coordinator whose sandbox cannot read the quotas live
# still routes on a recent value. Per-pool entries, each aged on its own `as_of`.
SNAPSHOT_SCHEMA = "worker-quota.v1"
SNAPSHOT_NAME = Path(".agent-relay") / "worker-quota.json"
SNAPSHOT_MAX_AGE = 1800
RECORD_NAME = Path(".agent-relay") / "worker-outcomes.jsonl"
VALID_KINDS = ("docs", "tests", "ui", "feature", "review", "fix")
VALID_FAILURES = ("none", "harness", "model")
VALID_SALVAGES = ("salvaged", "restarted", "n/a")
VALID_FIRST_PASS = ("pass", "fail", "n/a")
RISK_GUIDE = ("risk = harm if the worker is wrong: low docs/tests/UI copy; medium feature code covered by tests; "
              "high security, auth, egress, migrations/data loss, supervisor/relay/sandbox code, irreversible ops")


@dataclass(frozen=True)
class Model:
    key: str
    kind: str  # launch kind: codex | agy | copilot | claude
    model: str
    pool: str
    capability: int  # 1..3, the highest required tier it may take
    cost: int  # relative cost, informational (the ordering uses capability surplus, then headroom)


# Capability is provisional and per effort, never per brand (spec 255, directive L20261109084309-afcd): until
# measured, Flash and Argon at high take tier 3. Effort follows the task: the smallest adequate tier is chosen first.
# agy keys are agy_worker.py aliases.
MODELS: tuple[Model, ...] = (
    Model("luna", "codex", "gpt-6-luna", "codex", 1, 1),
    Model("flash", "agy", "gemini-3.8-flash-low", "gemini", 1, 1),
    Model("copilot-auto", "copilot", "auto", "copilot", 1, 1),
    Model("haiku", "claude", "claude-haiku-5-5", "claude", 1, 2),
    Model("flash-medium", "agy", "gemini-3.8-flash-medium", "gemini", 2, 2),
    Model("argon-medium", "agy", "gemini-4-argon-medium", "argon", 2, 3),
    Model("terra", "codex", "gpt-6.1-terra", "codex", 2, 3),
    Model("sonnet", "claude", "claude-sonnet-5-5", "claude", 2, 5),
    Model("flash-high", "agy", "gemini-3.8-flash-high", "gemini", 3, 3),
    Model("argon-high", "agy", "gemini-4-argon-high", "argon", 3, 5),
    Model("agy-sonnet", "agy", "claude-sonnet-5-5-high", "agy3p", 3, 6),
    Model("agy-opus", "agy", "claude-opus-5-5-high", "agy3p", 3, 8),
    Model("sol", "codex", "gpt-6-sol", "codex", 3, 8),
)


def family(model: Model) -> str:
    """The model family an independent review should differ from (agy Sonnet/Opus are Claude models)."""
    name = model.model
    return ("claude" if name.startswith("claude") else "gemini" if name.startswith("gemini")
            else "codex" if name.startswith("gpt") else model.pool)


@dataclass(frozen=True)
class Pool:
    """A subscription pool reading: windows maps a window label to its free fraction. free is None when the pool
    is unreadable or disabled (reason says why); otherwise it is the minimum over the windows."""

    name: str
    windows: dict[str, float]
    free: float | None
    reason: str | None = None
    source: str = "live"  # live | snapshot
    resets: dict[str, datetime] = field(default_factory=dict, compare=False)  # window label -> reset, when known


def _reset_ok(pool: Pool, label: str, now: datetime, horizon: timedelta) -> bool:
    reset = pool.resets.get(label)
    return reset is not None and now < reset <= now + horizon


def below_reserve(pool: Pool, now: datetime | None = None) -> bool:
    """A window under the reserve blocks the pool unless it resets within SPEND_DOWN (use it or lose it)."""
    now = now or datetime.now(UTC)
    reserve = RESERVES.get(pool.name, RESERVE)
    return any(free < reserve and not (free > 0 and _reset_ok(pool, label, now, SPEND_DOWN))
               for label, free in pool.windows.items())


def expiry_score(pool: Pool, now: datetime | None = None) -> float:
    """Quota that would expire unused: the binding (least free) window's free fraction per hour to its reset.
    An unknown reset counts as a week away."""
    now = now or datetime.now(UTC)
    if not pool.windows:
        return 0.0
    label = min(pool.windows, key=lambda k: pool.windows[k])
    reset = pool.resets.get(label)
    hours = (reset - now).total_seconds() / 3600 if reset is not None and reset > now else UNKNOWN_RESET_HOURS
    return pool.windows[label] / max(hours, 1.0)


def tier(difficulty: str, risk: str) -> int:
    return max(LEVELS[difficulty], LEVELS[risk])


def unreadable(name: str, reason: str) -> Pool:
    return Pool(name, {}, None, reason)


def _fraction(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return float(value)


def _json_document(text: str) -> dict[str, Any] | None:
    for candidate in [text, *reversed(text.strip().splitlines())]:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _host_which(command: str) -> str | None:
    """Resolve a CLI as the Relay gateway does: services do not inherit a login PATH."""
    entries = [str(Path.home() / ".local" / "bin")]
    entries += sorted(glob.glob(str(Path.home() / ".nvm" / "versions" / "node" / "*" / "bin")), reverse=True)
    entries.append(os.environ.get("PATH", "/usr/bin:/bin"))
    return shutil.which(command, path=os.pathsep.join(entries))


def _buckets(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        return []
    if isinstance(node, list):
        return [b for item in node for b in _buckets(item, depth + 1)]
    if not isinstance(node, dict):
        return []
    own = node.get("buckets")
    found = [b for b in own if isinstance(b, dict)] if isinstance(own, list) else []
    return found + [b for key, value in node.items() if key != "buckets" for b in _buckets(value, depth + 1)]


def parse_gemini(reading: dict[str, Any] | None, name: str = "gemini") -> Pool:
    """An agy pool (AGY_POOLS) from an `agy /quota` document: every one of its buckets must be present and enabled."""
    if reading is None:
        return unreadable(name, "no_reading")
    wanted = AGY_POOLS[name]
    found = {b.get("id"): b for b in _buckets(reading) if b.get("id") in wanted}
    if set(found) != set(wanted):
        return unreadable(name, "bucket_missing")
    windows: dict[str, float] = {}
    resets: dict[str, datetime] = {}
    for bucket_id, label in wanted.items():
        bucket = found[bucket_id]
        fraction = _fraction(bucket.get("remaining_fraction"))
        if fraction is None or not isinstance(bucket.get("disabled", False), bool):
            return unreadable(name, "invalid_reading")
        if bucket.get("disabled"):
            return Pool(name, {}, None, f"{label}_disabled")
        windows[label] = fraction
        if (reset := _parse_instant(bucket.get("reset_time"))) is not None:
            resets[label] = reset
    return Pool(name, windows, min(windows.values()), resets=resets)


def parse_codex(limits: dict[str, Any] | None) -> Pool:
    """The codex pool from the `rateLimits` object: every window present must be valid, and at least one present."""
    if not isinstance(limits, dict):
        return unreadable("codex", "no_reading")
    windows: dict[str, float] = {}
    for key, label in CODEX_WINDOWS.items():
        window = limits.get(key)
        if window is None:
            continue
        used = window.get("usedPercent") if isinstance(window, dict) else None
        if isinstance(used, bool) or not isinstance(used, int | float) or not math.isfinite(used) or not 0 <= used <= 100:
            return unreadable("codex", "invalid_reading")
        windows[label] = 1 - used / 100
    if not windows:
        return unreadable("codex", "no_windows")
    return Pool("codex", windows, min(windows.values()))


def _parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def parse_claude(entry: Any, now: datetime | None = None) -> Pool:
    """The claude pool from the `claude` entry of `agent-relay --json limits`: status ok, an `as_of` no older than
    FRESHNESS, and every window with a valid used_percent. Anything else (e.g. an expired session) is unreadable."""
    if not isinstance(entry, dict):
        return unreadable("claude", "no_reading")
    if entry.get("status") not in (None, "ok"):
        return unreadable("claude", "unavailable")
    as_of = _parse_instant(entry.get("as_of"))
    if as_of is None:
        return unreadable("claude", "no_as_of")
    if abs(((now or datetime.now(UTC)) - as_of).total_seconds()) > FRESHNESS:
        return unreadable("claude", "stale")
    raw = entry.get("windows")
    if not isinstance(raw, list) or not raw:
        return unreadable("claude", "no_windows")
    windows: dict[str, float] = {}
    for window in raw:
        used = window.get("used_percent") if isinstance(window, dict) else None
        if isinstance(used, bool) or not isinstance(used, int | float) or not math.isfinite(used) or not 0 <= used <= 100:
            return unreadable("claude", "invalid_reading")
        ident = str(window.get("id", ""))
        label = "5h" if ident in CLAUDE_FIVE_HOUR else "weekly" if ident in CLAUDE_WEEKLY else ident or "window"
        windows[label] = min(windows.get(label, 1.0), 1 - used / 100)
    return Pool("claude", windows, min(windows.values()))


def parse_copilot(reading: dict[str, Any] | None) -> Pool:
    """Premium interactions from GitHub's no-model-turn entitlement endpoint."""
    if not isinstance(reading, dict):
        return unreadable("copilot", "no_reading")
    if reading.get("cli_enabled") is False:
        return unreadable("copilot", "cli_disabled")
    snapshots = reading.get("quota_snapshots")
    prem = snapshots.get("premium_interactions") if isinstance(snapshots, dict) else None
    if not isinstance(prem, dict):
        return unreadable("copilot", "bucket_missing")
    if prem.get("unlimited") is True:
        return Pool("copilot", {"monthly": 1.0}, 1.0)
    if prem.get("has_quota") is False:
        if prem.get("entitlement") in (0, 0.0):
            return unreadable("copilot", "no_entitlement")
        return Pool("copilot", {"monthly": 0.0}, 0.0)
    pct_rem = prem.get("percent_remaining")
    if (isinstance(pct_rem, bool) or not isinstance(pct_rem, int | float)
            or not math.isfinite(pct_rem) or not 0 <= pct_rem <= 100):
        return unreadable("copilot", "invalid_reading")
    remaining = prem.get("remaining")
    if remaining is not None and (isinstance(remaining, bool) or not isinstance(remaining, int | float)
                                  or not math.isfinite(remaining)):
        return unreadable("copilot", "invalid_reading")
    fraction = float(pct_rem) / 100.0
    if remaining is not None and remaining <= 0:
        fraction = 0.0
    return Pool("copilot", {"monthly": fraction}, fraction)


def read_copilot() -> Pool:
    """Read entitlement using coordinator credentials; never start a model turn."""
    binary = os.environ.get("JARVISOS_GH_BIN") or _host_which("gh")
    if not binary:
        return unreadable("copilot", "gh_missing")
    env = os.environ.copy()
    token = env.get("JARVISOS_COPILOT_GH_TOKEN") or env.get("COPILOT_GITHUB_TOKEN")
    if not token:
        try:
            token = (Path.home() / ".local/share/jarvisos-sandbox/state/copilot/token").read_text().strip()
        except OSError:
            pass
    if token:
        env["GH_TOKEN"] = token
    try:
        done = subprocess.run([binary, "api", "copilot_internal/user"], capture_output=True, text=True,
                              timeout=READ_TIMEOUT, check=False, cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
                              env=env)
    except subprocess.TimeoutExpired:
        return unreadable("copilot", "timeout")
    except OSError:
        return unreadable("copilot", "gh_missing")
    if done.returncode != 0:
        output = (done.stdout + done.stderr).lower()
        if any(marker in output for marker in ("401", "bad credentials", "gh auth login", "not logged into", "notsignedin")):
            return unreadable("copilot", "auth_required")
        return unreadable("copilot", "no_reading")
    pool = parse_copilot(_json_document(done.stdout))
    if pool.free is not None and not (token or
            (Path.home() / ".local/share/jarvisos-sandbox/state/copilot/copilot-home").is_dir()):
        return unreadable("copilot", "auth_missing")
    configured = os.environ.get("JARVISOS_COPILOT_BIN")
    if pool.free is not None and not (shutil.which(configured) if configured else _host_which("copilot")):
        return unreadable("copilot", "copilot_missing")
    return pool


def read_claude() -> Pool:
    """Use a fresh cached Relay reading before requesting a refresh."""
    binary = os.environ.get("JARVISOS_AGENT_RELAY_BIN") or _host_which("agent-relay")
    if not binary:
        return unreadable("claude", "relay_missing")

    def read(refresh: bool) -> Pool:
        try:
            done = subprocess.run([binary, "--json", "limits", *(["--refresh"] if refresh else [])],
                                  capture_output=True, text=True, timeout=READ_TIMEOUT, check=False,
                                  cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return unreadable("claude", "timeout")
        except OSError:
            return unreadable("claude", "relay_missing")
        data = _json_document(done.stdout)
        agents = data.get("agents") if data else None
        entry = next((a for a in agents if isinstance(a, dict) and a.get("agent_key") == "claude"), None) \
            if isinstance(agents, list) else None
        return parse_claude(entry)

    cached = read(False)
    if cached.free is not None:
        return cached
    return read(True)


_agy_doc: dict[str, Any] = {"at": None, "reading": None}
_agy_lock = threading.Lock()


def read_agy(name: str) -> Pool:
    """One `agy /quota` read feeds every agy pool: concurrent readers share it for AGY_DOC_TTL seconds."""
    with _agy_lock:
        now = time.monotonic()
        if _agy_doc["at"] is None or now - _agy_doc["at"] > AGY_DOC_TTL:
            _agy_doc.update(at=now, reading=read_agy_document())
        reading = _agy_doc["reading"]
    return unreadable(name, reading) if isinstance(reading, str) else parse_gemini(reading, name)


def read_gemini() -> Pool:
    """A fresh reading of the general Gemini pool (read_pools shares one reading across the agy pools)."""
    reading = read_agy_document()
    return unreadable("gemini", reading) if isinstance(reading, str) else parse_gemini(reading, "gemini")


def read_agy_document() -> dict[str, Any] | str:
    """The `agy /quota` document, or the reason it could not be read."""
    if relay_argon.sandbox():
        try:
            relay_argon.prepare_login()
        except relay_argon.Refused as exc:
            if str(exc) != "login_state_missing":
                return str(exc)
        except OSError:
            return "login_path_conflict"
    binary = os.environ.get("JARVISOS_AGY_BIN") or _host_which("agy")
    if not binary:
        return "agy_missing"
    try:
        done = subprocess.run([binary, "-p", "/quota", "--output-format", "json"], capture_output=True, text=True,
                              timeout=AGY_READ_TIMEOUT, check=False, cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        return "auth_required" if _agy_auth_required(exc.stdout, exc.stderr) else "timeout"
    except OSError:
        return "agy_missing"
    if _agy_auth_required(done.stdout, done.stderr):
        return "auth_required"
    reading = _json_document(done.stdout)
    if reading is None or reading.get("status") != "SUCCESS":
        return "no_reading"
    return reading


def _agy_auth_required(*outputs: str | bytes | None) -> bool:
    return any("Authentication required" in (output.decode(errors="replace") if isinstance(output, bytes) else output or "")
               for output in outputs)


def read_codex() -> Pool:
    """Ask the Codex CLI for current account limits; never creates a thread, runs a turn or touches credentials."""
    binary = os.environ.get("JARVISOS_CODEX_BIN") or _host_which("codex")
    if not binary:
        return unreadable("codex", "codex_missing")
    try:
        proc = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                cwd=tempfile.gettempdir())
    except OSError:
        return unreadable("codex", "codex_missing")
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
    deadline = time.monotonic() + READ_TIMEOUT

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
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "worker_routing", "version": "0.1.0"}}})
        if response(1) is None:
            return unreadable("codex", "no_reading")
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/rateLimits/read", "params": {}})
        result = response(2)
        if result is None:
            return unreadable("codex", "no_reading")
        by_id = result.get("rateLimitsByLimitId")
        limits = by_id.get("codex") if isinstance(by_id, dict) else None
        return parse_codex(limits if isinstance(limits, dict) else result.get("rateLimits"))
    except queue.Empty:
        return unreadable("codex", "timeout")
    except (OSError, ValueError):
        return unreadable("codex", "no_reading")
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        reader.join(timeout=1)
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


READERS: dict[str, Callable[[], Pool]] = {
    "codex": read_codex, "gemini": lambda: read_agy("gemini"), "copilot": read_copilot, "claude": read_claude,
    "argon": lambda: read_agy("argon"), "agy3p": lambda: read_agy("agy3p"),
}


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def default_snapshot_paths() -> list[Path]:
    """Where the supervisor's snapshot may be: this checkout's `.agent-relay`, and, when this is a linked
    worktree, the main checkout's (the supervisor runs against the main checkout)."""
    root = Path(__file__).resolve().parent.parent
    paths = [root / SNAPSHOT_NAME]
    marker = root / ".git"
    if marker.is_file():
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text.startswith("gitdir:"):
            gitdir = (root / text[len("gitdir:"):].strip()).resolve()
            if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                paths.append(gitdir.parent.parent.parent / SNAPSHOT_NAME)
    return paths


def default_record_path() -> Path:
    """Where worker outcomes are recorded: in a linked worktree, the main checkout's .agent-relay/worker-outcomes.jsonl."""
    root = Path(__file__).resolve().parent.parent
    marker = root / ".git"
    if marker.is_file():
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text.startswith("gitdir:"):
            gitdir = (root / text[len("gitdir:"):].strip()).resolve()
            if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                return gitdir.parent.parent.parent / RECORD_NAME
    return root / RECORD_NAME


def resolve_record_path(path: Path | None = None) -> Path:
    return path if path is not None else default_record_path()


def _parse_findings(findings: Any) -> tuple[int, int, int, int]:
    if isinstance(findings, dict):
        try:
            return (int(findings.get("P0", 0)), int(findings.get("P1", 0)),
                    int(findings.get("P2", 0)), int(findings.get("P3", 0)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid findings: {findings}") from exc
    if isinstance(findings, (list, tuple)) and len(findings) == 4:
        try:
            return int(findings[0]), int(findings[1]), int(findings[2]), int(findings[3])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid findings: {findings}") from exc
    if isinstance(findings, str):
        parts = [p.strip() for p in findings.split(",") if p.strip()]
        if len(parts) == 4:
            try:
                nums = [int(p.split("=")[-1]) for p in parts]
                return nums[0], nums[1], nums[2], nums[3]
            except ValueError as exc:
                raise ValueError(f"invalid findings: {findings}") from exc
    raise ValueError(f"invalid findings: {findings}")


def _record_findings_all(r: dict[str, Any]) -> tuple[int, int, int, int]:
    f = r.get("findings")
    if isinstance(f, dict):
        try:
            return int(f.get("P0", 0)), int(f.get("P1", 0)), int(f.get("P2", 0)), int(f.get("P3", 0))
        except (TypeError, ValueError):
            return 0, 0, 0, 0
    if isinstance(f, (list, tuple)) and len(f) >= 4:
        try:
            return int(f[0]), int(f[1]), int(f[2]), int(f[3])
        except (TypeError, ValueError):
            return 0, 0, 0, 0
    return 0, 0, 0, 0


def _rework_score(record: dict[str, Any]) -> float:
    p0, p1, _, _ = _record_findings_all(record)
    rework = record.get("rework_rounds", 0)
    try:
        rework_int = int(rework)
    except (TypeError, ValueError):
        rework_int = 0
    return float(rework_int + p0 + p1)


def family_of_record(r: dict[str, Any], by_key: dict[str, Model]) -> str:
    worker = str(r.get("worker", ""))
    if worker in by_key:
        return family(by_key[worker])
    model_name = str(r.get("model", ""))
    if model_name.startswith("claude"):
        return "claude"
    if model_name.startswith("gemini"):
        return "gemini"
    if model_name.startswith("gpt"):
        return "codex"
    return str(r.get("pool", ""))


def load_records(path: Path | None = None) -> list[dict[str, Any]]:
    """Valid outcome records from the record file; malformed or unreadable lines are skipped."""
    target = resolve_record_path(path)
    if not target.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        with open(target, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(entry, dict):
                    records.append(entry)
    except OSError:
        return []
    return records


def record(task: str, kind: str, worker: str, *, tier: int | None = None, ci_first_pass: str = "n/a",
           findings: Any = "0,0,0,0", rework_rounds: int = 0, escalated: bool = False,
           salvage: str = "n/a", reason: str | None = None, failure: str = "none",
           coordinator_tokens: int | None = None, record_file: Path | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    """Append one worker outcome record atomically. Refuses invalid input."""
    by_key = {m.key: m for m in MODELS}
    if worker not in by_key:
        raise ValueError(f"unknown worker: {worker}")
    model_obj = by_key[worker]
    model = model_obj.model

    if tier is None:
        tier_val = model_obj.capability
    else:
        try:
            tier_val = int(tier)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid tier: {tier}") from exc
        if tier_val not in (1, 2, 3):
            raise ValueError(f"invalid tier: {tier}")

    if kind not in VALID_KINDS:
        raise ValueError(f"unknown kind: {kind}")
    if failure not in VALID_FAILURES:
        raise ValueError(f"unknown failure: {failure}")
    if salvage not in VALID_SALVAGES:
        raise ValueError(f"unknown salvage: {salvage}")
    if salvage == "restarted" and not (reason and str(reason).strip()):
        raise ValueError("salvage 'restarted' requires a non-empty reason")
    if ci_first_pass not in VALID_FIRST_PASS:
        raise ValueError(f"unknown ci_first_pass: {ci_first_pass}")

    p0, p1, p2, p3 = _parse_findings(findings)

    try:
        rework_int = int(rework_rounds)
        if rework_int < 0:
            raise ValueError(f"invalid rework_rounds: {rework_rounds}")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid rework_rounds: {rework_rounds}") from exc

    tokens_val: int | None = None
    if coordinator_tokens is not None:
        try:
            tokens_val = int(coordinator_tokens)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid coordinator_tokens: {coordinator_tokens}") from exc

    moment = now or datetime.now(UTC)
    entry: dict[str, Any] = {
        "at": _iso(moment),
        "task": str(task),
        "kind": kind,
        "worker": worker,
        "model": model,
        "tier": tier_val,
        "ci_first_pass": ci_first_pass,
        "findings": {"P0": p0, "P1": p1, "P2": p2, "P3": p3},
        "rework_rounds": rework_int,
        "escalated": bool(escalated),
        "salvage": salvage,
        "reason": str(reason).strip() if salvage == "restarted" else None,
        "failure": failure,
    }
    if tokens_val is not None:
        entry["coordinator_tokens"] = tokens_val

    target = resolve_record_path(record_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(target, flags, 0o644)
    try:
        written = 0
        while written < len(line):
            n = os.write(fd, line[written:])
            if n == 0:
                raise OSError("os.write returned 0 bytes")
            written += n
    finally:
        os.close(fd)

    return entry


def cmd_record(args: argparse.Namespace) -> int:
    try:
        record(
            task=args.task,
            kind=args.kind,
            worker=args.worker,
            tier=args.tier,
            ci_first_pass=args.ci_first_pass,
            findings=args.findings,
            rework_rounds=args.rework_rounds,
            escalated=args.escalated,
            salvage=args.salvage,
            reason=args.reason,
            failure=args.failure,
            coordinator_tokens=args.coordinator_tokens,
            record_file=args.record_file,
        )
        return 0
    except (ValueError, TypeError, OSError) as exc:
        sys.stderr.write(f"record: {exc}\n")
        return 2


def load_snapshot(paths: list[Path]) -> dict[str, tuple[datetime, dict[str, float]]]:
    """Valid per-pool entries (as_of, windows) from the given snapshot files, the freshest per pool. Anything
    malformed is ignored."""
    best: dict[str, tuple[datetime, dict[str, float]]] = {}
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("schema") != SNAPSHOT_SCHEMA or not isinstance(data.get("pools"), dict):
            continue
        for name, entry in data["pools"].items():
            if not isinstance(entry, dict) or not isinstance(entry.get("windows"), dict) or not entry["windows"]:
                continue
            at = _parse_ts(entry.get("as_of"))
            windows = {str(k): _fraction(v) for k, v in entry["windows"].items()}
            if at is None or any(v is None for v in windows.values()):
                continue
            if name not in best or at > best[name][0]:
                best[name] = (at, {k: v for k, v in windows.items() if v is not None})
    return best


def apply_snapshot(pools: dict[str, Pool], snapshot: dict[str, tuple[datetime, dict[str, float]]],
                   now: datetime, max_age: float = SNAPSHOT_MAX_AGE) -> dict[str, Pool]:
    """Pools that could not be read live fall back to a snapshot entry no older than `max_age`. A live reading
    always wins, and a pool the provider reported as disabled never falls back. Older or missing: unchanged."""
    out = dict(pools)
    for name, pool in pools.items():
        if (name == "copilot" or pool.free is not None or name not in snapshot
                or (pool.reason or "").endswith("_disabled")):
            continue
        at, windows = snapshot[name]
        if -60 <= (now - at).total_seconds() <= max_age:
            out[name] = Pool(name, windows, min(windows.values()), None, "snapshot")
    return out


def write_snapshot(path: Path, pools: dict[str, Pool], now: datetime | None = None) -> bool:
    """Persist the live-read pools atomically, keeping other pools' older entries. Writes nothing when no pool
    was read live. Never raises."""
    live = {n: p for n, p in pools.items() if p.free is not None and p.source == "live"}
    if not live:
        return False
    moment = now or datetime.now(UTC)
    entries: dict[str, Any] = {n: {"as_of": _iso(at), "windows": w} for n, (at, w) in load_snapshot([path]).items()}
    for name, pool in live.items():
        entries[name] = {"as_of": _iso(moment), "windows": {k: round(v, 4) for k, v in pool.windows.items()}}
    document = {"schema": SNAPSHOT_SCHEMA, "as_of": _iso(moment), "pools": entries}
    temp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".worker-quota.",
                                         suffix=".tmp", delete=False) as handle:
            temp = handle.name
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temp, 0o644)
        os.replace(temp, path)
        return True
    except OSError:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass
        return False


def read_pools(readers: dict[str, Callable[[], Pool]] | None = None, timeout: float = TOTAL_TIMEOUT) -> dict[str, Pool]:
    """Every pool that MODELS uses, read in parallel within one bounded wait. A reader that raises or is still
    running at the deadline leaves its pool unreadable; nothing here ever hangs or raises."""
    chosen = READERS if readers is None else readers
    names = list(dict.fromkeys(m.pool for m in MODELS))
    with _agy_lock:
        _agy_doc["at"] = None  # the agy pools share one fresh reading per call
    out: dict[str, Pool] = {name: unreadable(name, "timeout") for name in names}

    def run(name: str) -> None:
        try:
            reader = chosen.get(name)
            out[name] = reader() if reader else unreadable(name, "no_reader")
        except Exception:  # noqa: BLE001 - a broken reader makes only its pool unavailable
            out[name] = unreadable(name, "reader_error")

    threads = [threading.Thread(target=run, args=(name,), daemon=True) for name in names]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return dict(out)


def resolve_pools(quota_files: list[Path] | None = None, now: datetime | None = None) -> dict[str, Pool]:
    """Live readings first; a pool unreadable live falls back to the supervisor's snapshot when fresh enough."""
    pools = read_pools()
    if all(p.free is not None for p in pools.values()):
        return pools
    return apply_snapshot(pools, load_snapshot(quota_files or default_snapshot_paths()), now or datetime.now(UTC))


def pct(value: float) -> str:
    return f"{round(value * 100)}%"


def rank(required: int, pools: dict[str, Pool], models: tuple[Model, ...] = MODELS, review_of: str | None = None,
         now: datetime | None = None, task: str | None = None, kind: str | None = None,
         outcomes: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(eligible, skipped) for a required tier. Eligible: another family than `review_of` first (never required),
    then smallest capability surplus (effort by task), the gemini family, the quota that would expire unused first,
    then most free quota."""
    now = now or datetime.now(UTC)
    by_key = {m.key: m for m in models}
    eligible: list[tuple[tuple[int, int, int, float, float, int], dict[str, Any]]] = []
    skipped: list[dict[str, Any]] = []

    failed_families: set[str] = set()
    if task is not None and outcomes:
        for r in outcomes:
            if r.get("task") == task and r.get("failure") == "model":
                fam = family_of_record(r, by_key)
                if fam:
                    failed_families.add(fam)

    def check_measured_inadequate(candidate: Model) -> bool:
        if kind is None or not outcomes:
            return False
        c_records = [
            r for r in outcomes
            if (r.get("worker") == candidate.key or r.get("model") == candidate.model)
            and r.get("kind") == kind
            and r.get("failure") != "harness"
        ]
        judged = [r for r in c_records if r.get("ci_first_pass") in ("pass", "fail")]  # n/a has no first pass
        if len(judged) < 5:
            return False
        passes = sum(1 for r in judged if r.get("ci_first_pass") == "pass")
        if passes / len(judged) >= 0.50:
            return False
        c_mean = sum(_rework_score(r) for r in c_records) / len(c_records)

        c_fam = family(candidate)
        next_family: str | None = None
        for m in models:
            if m.capability < required:
                continue
            p = pools.get(m.pool) or unreadable(m.pool, "no_reading")
            if p.free is None or below_reserve(p, now):
                continue
            m_fam = family(m)
            if m_fam in failed_families:
                continue
            if m_fam != c_fam:
                next_family = m_fam
                break
        if next_family is None:
            return False
        next_records = [
            r for r in outcomes
            if family_of_record(r, by_key) == next_family
            and r.get("kind") == kind
            and r.get("failure") != "harness"
        ]
        if not next_records:
            return False
        next_mean = sum(_rework_score(r) for r in next_records) / len(next_records)
        return c_mean > next_mean

    for index, model in enumerate(models):
        if model.capability < required:
            continue
        pool = pools.get(model.pool) or unreadable(model.pool, "no_reading")
        entry: dict[str, Any] = {"worker": model.key, "kind": model.kind, "model": model.model, "pool": model.pool,
                                 "family": family(model), "capability": model.capability, "cost": model.cost,
                                 "free": None if pool.free is None else round(pool.free, 4), "source": pool.source}
        if pool.free is None:
            skipped.append({**entry, "skipped": pool.reason or "unreadable"})
        elif below_reserve(pool, now):
            skipped.append({**entry, "skipped": "below_reserve"})
        elif family(model) in failed_families:
            skipped.append({**entry, "skipped": "escalated_from_family"})
        elif check_measured_inadequate(model):
            skipped.append({**entry, "skipped": "measured_inadequate"})
        else:
            same = int(review_of is not None and family(model) == review_of)
            first = 0 if model.pool in FIRST_POOLS else 1
            eligible.append(((same, model.capability - required, first, -expiry_score(pool, now), -pool.free, index),
                             entry))
    return [entry for _, entry in sorted(eligible, key=lambda item: item[0])], skipped


def bare_command(model: Model) -> str:
    if model.kind == "codex":
        return f'codex exec --model {model.model} --cd "$WORKTREE" - < "$TASK_FILE"'
    if model.kind == "claude":
        # Headless, edits only: acceptEdits plus no permission prompts (anything that would prompt is denied).
        # Never bypassPermissions or --dangerously-skip-permissions for a worker.
        return (f'cd "$WORKTREE" && claude -p --model {model.model} --permission-mode acceptEdits '
                '--permission-prompts none --output-format json < "$TASK_FILE"')
    if model.kind == "copilot":
        return (f'python3 scripts/copilot_worker.py run --task-file "$TASK_FILE" '
                f'--worktree "$WORKTREE" --model {model.key}')
    return f'python3 scripts/agy_worker.py run --task-file "$TASK_FILE" --worktree "$WORKTREE" --model {model.key}'


def launch_command(model: Model) -> str:
    # --input digests the task file, so a new task for the same worker and worktree is not refused as a duplicate.
    return ('python3 scripts/relay_jobs.py start --label "$LABEL" --authority "$AUTHORITY" --spec "$SPEC" '
            '--wall-hours "${WALL_HOURS:-1}" --cwd "$WORKTREE" --input "$TASK_FILE" -- bash -c '
            + shlex.quote(bare_command(model)))


def decide(difficulty: str, risk: str, pools: dict[str, Pool], review_of: str | None = None,
           task: str | None = None, kind: str | None = None, outcomes: list[dict[str, Any]] | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    required = tier(difficulty, risk)
    eligible, skipped = rank(required, pools, review_of=review_of, now=now, task=task, kind=kind, outcomes=outcomes)
    decision: dict[str, Any] = {"difficulty": difficulty, "risk": risk, "required_tier": required,
                                "review": REVIEW[risk], "reserves": RESERVES}
    by_key = {m.key: m for m in MODELS}
    if not eligible:
        reason = "wait_for_escalation_pool" if any(s.get("skipped") == "escalated_from_family" for s in skipped) else "no_worker"
        return {**decision, "status": "no_worker", "reason": reason, "skipped": skipped}
    chosen = eligible[0]
    low = chosen["pool"] == "claude" and chosen["free"] < CLAUDE_LOW_FREE
    return {**decision, "status": "ok", "worker": chosen["worker"], "model": chosen["model"], "pool": chosen["pool"],
            "source": chosen["source"], "command": launch_command(by_key[chosen["worker"]]),
            "claude_pool_low": low, **({"note": CLAUDE_LOW_NOTE} if low else {}),
            "alternatives": eligible[1:], "skipped": skipped}


def summary_line(pools: dict[str, Pool]) -> str:
    parts = []
    for name in dict.fromkeys(m.pool for m in MODELS):
        pool = pools.get(name) or unreadable(name, "no_reading")
        if pool.free is None:
            parts.append(f"{name} unavailable ({pool.reason or 'unreadable'})")
            continue
        short = f" (5h {pct(pool.windows['5h'])})" if "5h" in pool.windows else ""
        low = " [below reserve]" if below_reserve(pool) else ""
        stale = " [snapshot]" if pool.source == "snapshot" else ""
        parts.append(f"{name} {pct(pool.free)} free{short}{low}{stale}")
    tiers = ", ".join(f"{m.key} t{m.capability}" for m in MODELS)
    return ("worker quota: " + " · ".join(parts) + " — effort by task tier, gemini first, then quota expiring "
            "unused; tiers: " + tiers)


_cache: dict[str, Any] = {"at": None, "line": None, "ttl": 0.0}


def cached_summary(ttl: float = CACHE_TTL, clock: Callable[[], float] = time.monotonic,
                   read: Callable[[], dict[str, Pool]] | None = None, snapshot_file: Path | None = None) -> str:
    """One compact quota line for a supervisor prompt, cached for `ttl` seconds. Never raises: any failure is the
    line `worker quota: unreadable`, itself cached briefly so a broken reader is not retried on every turn.
    With `snapshot_file` (the supervisor), a successful live reading is also persisted there for coordinators."""
    now = clock()
    if _cache["at"] is not None and now - _cache["at"] < _cache["ttl"]:
        return str(_cache["line"])
    try:
        pools = (read or read_pools)()
        if snapshot_file is not None:
            write_snapshot(snapshot_file, pools)
        line = summary_line(pools)
        lifetime = ttl if any(p.free is not None for p in pools.values()) else min(ttl, FAILED_CACHE_TTL)
    except Exception:  # noqa: BLE001 - the prompt must never depend on a reading
        line, lifetime = UNREADABLE, min(ttl, FAILED_CACHE_TTL)
    _cache.update(at=now, line=line, ttl=lifetime)
    return line


def render(decision: dict[str, Any]) -> str:
    if decision["status"] != "ok":
        skipped = ", ".join(f"{s['worker']}({s['skipped']})" for s in decision["skipped"]) or "none adequate"
        prefix = f"no_worker ({decision['reason']})" if decision.get("reason") and decision["reason"] != "no_worker" else "no_worker"
        return (f"{prefix}: nothing adequate for tier {decision['required_tier']} "
                f"(difficulty {decision['difficulty']}, risk {decision['risk']}); skipped: {skipped}\n"
                "The coordinator does the work itself or waits.")
    lines = [f"worker: {decision['worker']} ({decision['model']}, pool {decision['pool']}"
             + (", quota from snapshot)" if decision["source"] == "snapshot" else ")"),
             f"tier: {decision['required_tier']} (difficulty {decision['difficulty']}, risk {decision['risk']})",
             "launch requires exported absolute WORKTREE and TASK_FILE, LABEL (1-80 letters, digits, spaces or _.:/+-), AUTHORITY, SPEC; optional WALL_HOURS (default 1):",
             f"launch: {decision['command']}",
             f"review: {decision['review']}"]
    if decision.get("note"):
        lines.append(f"note: {decision['note']}")
    if decision["alternatives"]:
        lines.append("alternatives: " + ", ".join(
            f"{a['worker']} ({a['pool']} {pct(a['free'])} free{' snapshot' if a['source'] == 'snapshot' else ''})"
            for a in decision["alternatives"]))
    if decision["skipped"]:
        lines.append("skipped: " + ", ".join(f"{s['worker']}({s['skipped']})" for s in decision["skipped"]))
    return "\n".join(lines)


def cmd_pick(args: argparse.Namespace) -> int:
    outcomes = load_records(args.record_file)
    decision = decide(args.difficulty, args.risk, resolve_pools(args.quota_file), args.review_of,
                      task=args.task, kind=args.kind, outcomes=outcomes)
    print(json.dumps(decision, indent=2, sort_keys=True) if args.json else render(decision))
    return 0 if decision["status"] == "ok" else NO_WORKER_EXIT


def report(days: int = 7, record_file: Path | None = None, records: list[dict[str, Any]] | None = None,
           now: datetime | None = None) -> str:
    """A short summary in plain Italian of worker outcomes and net savings over the last N days."""
    moment = now or datetime.now(UTC)
    all_records = records if records is not None else load_records(record_file)
    cutoff = moment - timedelta(days=days)

    filtered: list[dict[str, Any]] = []
    for r in all_records:
        at = _parse_instant(r.get("at")) or _parse_ts(r.get("at"))
        if at is not None and at >= cutoff:
            filtered.append(r)

    if not filtered:
        return f"Nessun esito registrato negli ultimi {days} giorni."

    by_key = {m.key: m for m in MODELS}

    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in filtered:
        m_name = str(r.get("model") or r.get("worker") or "sconosciuto")
        grouped.setdefault(m_name, []).append(r)

    lines: list[str] = []
    for m_name, group in grouped.items():
        total = len(group)
        passes = sum(1 for r in group if r.get("ci_first_pass") == "pass")
        pass_rate = pct(passes / total) if total > 0 else "0%"
        p0 = sum(_record_findings_all(r)[0] for r in group)
        p1 = sum(_record_findings_all(r)[1] for r in group)
        p2 = sum(_record_findings_all(r)[2] for r in group)
        p3 = sum(_record_findings_all(r)[3] for r in group)
        rework = sum(_rework_score(r) - sum(_record_findings_all(r)[:2]) for r in group)
        escalations = sum(1 for r in group if r.get("escalated"))
        w_key = group[0].get("worker")
        w_tag = f" ({w_key})" if w_key and w_key != m_name else ""
        lines.append(
            f"- {m_name}{w_tag}: {total} compiti, prima prova {pass_rate}, "
            f"rilievi (P0: {p0}, P1: {p1}, P2: {p2}, P3: {p3}), rilavorazioni {rework}, escalation {escalations}"
        )

    gemini_avoided = [
        r for r in filtered
        if family_of_record(r, by_key) == "gemini" and not r.get("escalated")
    ]
    avoided_count = len(gemini_avoided)
    tokens = sum(r["coordinator_tokens"] for r in gemini_avoided
                 if isinstance(r.get("coordinator_tokens"), int) and not isinstance(r["coordinator_tokens"], bool))

    lines.append(
        f"Risparmio netto: {avoided_count} compiti Claude e GPT evitati ({avoided_count} compiti Gemini non escalati), "
        f"meno {tokens} token coordinatore registrati."
    )
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> int:
    print(report(days=args.days, record_file=args.record_file))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    print(cached_summary(read=lambda: resolve_pools(args.quota_file)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    quota_file = {"type": Path, "action": "append", "help": "snapshot file to fall back on (default: the supervisor's "
                                                           ".agent-relay/worker-quota.json)"}
    pick = sub.add_parser("pick", help="choose the cheapest adequate worker with quota headroom",
                          description=RISK_GUIDE)
    pick.add_argument("--difficulty", required=True, choices=sorted(LEVELS, key=LEVELS.get))
    pick.add_argument("--risk", required=True, choices=sorted(LEVELS, key=LEVELS.get), help=RISK_GUIDE)
    pick.add_argument("--json", action="store_true")
    pick.add_argument("--task", default=None, help="task id to route, checking previous worker outcomes for escalation")
    pick.add_argument("--kind", default=None, choices=VALID_KINDS, help="task kind to route")
    pick.add_argument("--review-of", choices=("claude", "codex", "gemini", "copilot"),
                      help="the author's model family: prefer another family for an independent review, never block on it")
    pick.add_argument("--quota-file", **quota_file)
    pick.add_argument("--record-file", type=Path, default=None,
                      help="record file of worker outcomes (default: main checkout .agent-relay/worker-outcomes.jsonl)")
    pick.set_defaults(func=cmd_pick)

    rec = sub.add_parser("record", help="append one worker outcome record to worker-outcomes.jsonl")
    rec.add_argument("--task", required=True, help="task id chosen by coordinator")
    rec.add_argument("--kind", required=True, help="kind of task (docs, tests, ui, feature, review, fix)")
    rec.add_argument("--worker", required=True, help="worker key from MODELS")
    rec.add_argument("--tier", type=int, choices=(1, 2, 3), default=None, help="tier of task (1..3)")
    rec.add_argument("--ci-first-pass", default="n/a", help="pass, fail, or n/a")
    rec.add_argument("--findings", default="0,0,0,0", help="counts P0,P1,P2,P3 (e.g. 0,1,0,0)")
    rec.add_argument("--rework-rounds", type=int, default=0, help="rework rounds count")
    rec.add_argument("--escalated", action=argparse.BooleanOptionalAction, default=False, help="whether task was escalated")
    rec.add_argument("--salvage", default="n/a", help="salvaged, restarted, or n/a")
    rec.add_argument("--reason", default=None, help="reason when salvage is restarted")
    rec.add_argument("--failure", default="none", help="none, harness, or model")
    rec.add_argument("--coordinator-tokens", type=int, default=None, help="coordinator tokens recorded on task")
    rec.add_argument("--record-file", type=Path, default=None,
                     help="record file of worker outcomes (default: main checkout .agent-relay/worker-outcomes.jsonl)")
    rec.set_defaults(func=cmd_record)

    rep = sub.add_parser("report", help="summary report of worker outcomes and net savings in Italian")
    rep.add_argument("--days", type=int, default=7, help="number of days to report (default: 7)")
    rep.add_argument("--record-file", type=Path, default=None,
                     help="record file of worker outcomes (default: main checkout .agent-relay/worker-outcomes.jsonl)")
    rep.set_defaults(func=cmd_report)
    summary = sub.add_parser("summary", help="one compact line of pool headroom")
    summary.add_argument("--quota-file", **quota_file)
    summary.set_defaults(func=cmd_summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
