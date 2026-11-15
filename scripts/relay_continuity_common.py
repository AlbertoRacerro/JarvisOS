"""Shared constants, helpers, and sibling loaders for relay_continuity (spec 265)."""
from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def load_sibling(name: str, filename: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


devctx = load_sibling("devctx", "devctx.py")
host_ops = load_sibling("host_ops", "host_ops.py")
relay_jobs = load_sibling("relay_jobs", "relay_jobs.py")
relay_argon = load_sibling("relay_argon", "relay_argon.py")
worker_routing = load_sibling("worker_routing", "worker_routing.py")

SCHEMA = "relay-continuity.v1"
AGENTS = ("claude", "argon", "agy_opus", "codex", "hydrogen")
PREFERRED_AGENTS = ("claude", "argon", "agy_opus")
NATIVE_AGENTS = ("argon", "agy_opus", "hydrogen")
PREFERRED = "claude"
OUTCOMES = ("success", "quota", "spend_limit", "transient", "auth", "crash", "unknown")
FAILURES = tuple(o for o in OUTCOMES if o != "success")
EFFECT_ORDER = ("read_only", "local_write", "background", "external", "unknown")
PHASES = ("idle", "running", "waiting", "held", "stopped")
HOLD_REASONS = (
    "effects_uncertain", "output_unparseable", "output_missing", "attempt_unresolved", "circuit_failures",
    "no_provider_available", "wait_exceeded", "permissions_changed", "state_corrupt", "relay_invocation_failed",
    "task_check_failed", "task_blocked", "task_checkpoint_limit",
)
# Holds the earlier controller raised for reasons this one no longer treats as blocking (a provider failure, live
# or uncertain local work, an unavailable diagnosis, an unverified report). Such a hold on the chain's latest turn is
# decided again under the current policy on the next run, which still holds if the turn really needs review.
REDECIDED_HOLDS = (
    "effects_uncertain", "auth_failure", "unknown_failure", "crash_repeated", "circuit_switches", "circuit_attempts",
    "circuit_transient", "repl_active", "live_work", "reconcile_unresolved", "reconcile_insufficient_permissions",
    "reconcile_failed", "reconcile_no_sandbox", "task_report_unverified",
)
ROADMAP_PAUSES = ("maintainer_stop", "coordinator_blocked", "no_progress", "cycle_cancelled")
STOP_REASONS = ("stop_requested", "signal", "idle", "held", "waiting", "legacy_autopilot", "lock_busy", "error")
DEFAULT_CONTROL = Path.home() / "jarvis-control" / "work" / "continuity"
WEBHOOK_FILE = Path.home() / ".config" / "jarvis" / "slack_webhook_url"
SLACK_BOT_FILE = Path.home() / ".config" / "jarvis" / "slack_bot.json"
MISSION_SCHEMA = "relay-mission.v1"
MISSION_ID_RE = re.compile(r"m-\d{8}T\d{6}Z-[0-9a-f]{6}")
DIRECTIVE_ID_RE = re.compile(r"L\d{14}-[0-9a-f]{4}")
MAX_TASKS = 20
MAX_TASK_CHARS = 8000
MAX_TASK_CHECKPOINTS = 12  # spec 204: durable checkpoints of one task before it holds, across owner restarts
WAIT_TIMEOUT_SECONDS = 7200  # spec 267: elapsed wall-clock seconds before an external wait terminates (2 hours)
CI_POLL_INTERVAL_SECONDS = 60  # spec 267: minimum seconds between PR CI check queries
JOB_POLL_INTERVAL_SECONDS = 15  # spec 267: minimum seconds between job status queries
class UnmeasurableContextTokens(int):
    """Sentinel integer forcing fresh context when Claude context usage is unmeasurable."""


MAX_SESSION_RESUMES = 4  # spec 267: consecutive resumes of the same session
RESUME_CONTEXT_CAP_TOKENS = 150_000  # spec 277: context size cap above which a turn starts a fresh context
TURN_USAGE_CAP_TOKENS = RESUME_CONTEXT_CAP_TOKENS  # spec 267 alias, superseded by spec 277
UNMEASURABLE_CONTEXT_TOKENS = UnmeasurableContextTokens(
    RESUME_CONTEXT_CAP_TOKENS + 1
)  # spec 277: sentinel forcing fresh context when Claude usage is missing/malformed
MIN_CHECKPOINT_INTERVAL_SECONDS = 60  # spec 267: minimum seconds between bare checkpoints

JOB_WAIT_LINE_RE = re.compile(r"^JOB-WAIT:\s*(j-\d{8}T\d{6}Z-[0-9a-f]{6})\s*$")
CI_WAIT_LINE_RE = re.compile(r"^CI-WAIT:\s*([1-9]\d*)@([0-9a-f]{40})\s*$")
FORBIDDEN_WAIT_CHARS = frozenset("`$;|><&()")
CI_SUCCESS_CONCLUSIONS = frozenset(("success", "neutral", "skipped"))
CI_FAILURE_CONCLUSIONS = frozenset(("failure", "timed_out", "action_required", "cancelled", "error"))
SPEC_ID_RE = re.compile(r"\d{3}[a-z]?")
SANDBOX_HOSTNAME = "jarvis-sandbox"
LEGACY_MARKERS = ("jarvis-control/autopilot/run.sh", "jarvis-control/autopilot/watchdog.sh",
                  "jarvis-control/autopilot/resume-after-reset.sh", "jarvis-control/autopilot/drain-guard.sh")


SAME_AGENT_CAUSES = frozenset(("provider_network", "provider_auth_blip"))


@dataclass
class Limits:
    """Bounds that make every loop finite. Provider failures are bounded by their cooldowns and `max_wait`;
    turns that fail without a provider reason (crash, unknown) by `max_consecutive_failures`."""
    max_consecutive_failures: int = 8
    transient_base: float = 60.0
    transient_cap: float = 1_800.0
    same_agent_retries: int = 3  # network/auth blips retried on the same agent before the normal cooldown applies
    recheck: timedelta = timedelta(minutes=30)
    recheck_cap: timedelta = timedelta(hours=4)  # estimated cooldowns double per repeated failure up to this
    max_wait: timedelta = timedelta(days=8)
    reading_freshness: timedelta = timedelta(minutes=15)
    max_reset_ahead: timedelta = timedelta(days=8)
    min_free_percent: float = 25.0
    reap_grace: float = 5.0  # seconds a leftover process gets to exit on SIGTERM before SIGKILL
    turn_wall: float = 3600.0  # seconds RelayAdapter.finish waits before terminating the process group


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(ts: datetime | None) -> str | None:
    return None if ts is None else ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


