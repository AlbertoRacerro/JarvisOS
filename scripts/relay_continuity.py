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
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DEVCTX_PATH = Path(__file__).resolve().parent / "devctx.py"
_spec = importlib.util.spec_from_file_location("devctx", _DEVCTX_PATH)
assert _spec and _spec.loader
devctx = sys.modules.get("devctx") or importlib.util.module_from_spec(_spec)
if "devctx" not in sys.modules:
    sys.modules["devctx"] = devctx
    _spec.loader.exec_module(devctx)

_HOSTOPS_SPEC = importlib.util.spec_from_file_location("host_ops", Path(__file__).resolve().parent / "host_ops.py")
assert _HOSTOPS_SPEC and _HOSTOPS_SPEC.loader
host_ops = sys.modules.get("host_ops") or importlib.util.module_from_spec(_HOSTOPS_SPEC)
if "host_ops" not in sys.modules:
    sys.modules["host_ops"] = host_ops
    _HOSTOPS_SPEC.loader.exec_module(host_ops)

_JOBS_SPEC = importlib.util.spec_from_file_location("relay_jobs", Path(__file__).resolve().parent / "relay_jobs.py")
assert _JOBS_SPEC and _JOBS_SPEC.loader
relay_jobs = sys.modules.get("relay_jobs") or importlib.util.module_from_spec(_JOBS_SPEC)
if "relay_jobs" not in sys.modules:
    sys.modules["relay_jobs"] = relay_jobs
    _JOBS_SPEC.loader.exec_module(relay_jobs)

_ARGON_SPEC = importlib.util.spec_from_file_location("relay_argon", Path(__file__).resolve().parent / "relay_argon.py")
assert _ARGON_SPEC and _ARGON_SPEC.loader
relay_argon = sys.modules.get("relay_argon") or importlib.util.module_from_spec(_ARGON_SPEC)
if "relay_argon" not in sys.modules:
    sys.modules["relay_argon"] = relay_argon
    _ARGON_SPEC.loader.exec_module(relay_argon)

_ROUTING_SPEC = importlib.util.spec_from_file_location("worker_routing", Path(__file__).resolve().parent / "worker_routing.py")
assert _ROUTING_SPEC and _ROUTING_SPEC.loader
worker_routing = sys.modules.get("worker_routing") or importlib.util.module_from_spec(_ROUTING_SPEC)
if "worker_routing" not in sys.modules:
    sys.modules["worker_routing"] = worker_routing
    _ROUTING_SPEC.loader.exec_module(worker_routing)

SCHEMA = "relay-continuity.v1"
AGENTS = ("claude", "codex", "argon", "agy_opus")
PREFERRED_AGENTS = ("claude", "argon", "agy_opus")
NATIVE_AGENTS = ("argon", "agy_opus")
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
MISSION_SCHEMA = "relay-mission.v1"
MISSION_ID_RE = re.compile(r"m-\d{8}T\d{6}Z-[0-9a-f]{6}")
DIRECTIVE_ID_RE = re.compile(r"L\d{14}-[0-9a-f]{4}")
MAX_TASKS = 20
MAX_TASK_CHARS = 8000
MAX_TASK_CHECKPOINTS = 12  # spec 204: durable checkpoints of one task before it holds, across owner restarts
SPEC_ID_RE = re.compile(r"\d{3}[a-z]?")
SANDBOX_HOSTNAME = "jarvis-sandbox"
LEGACY_MARKERS = ("jarvis-control/autopilot/run.sh", "jarvis-control/autopilot/watchdog.sh",
                  "jarvis-control/autopilot/resume-after-reset.sh", "jarvis-control/autopilot/drain-guard.sh")


@dataclass
class Limits:
    """Bounds that make every loop finite. Provider failures are bounded by their cooldowns and `max_wait`;
    turns that fail without a provider reason (crash, unknown) by `max_consecutive_failures`."""
    max_consecutive_failures: int = 8
    transient_base: float = 60.0
    transient_cap: float = 1_800.0
    recheck: timedelta = timedelta(minutes=30)
    recheck_cap: timedelta = timedelta(hours=4)  # estimated cooldowns double per repeated failure up to this
    max_wait: timedelta = timedelta(days=8)
    reading_freshness: timedelta = timedelta(minutes=15)
    max_reset_ahead: timedelta = timedelta(days=8)
    reap_grace: float = 5.0  # seconds a leftover process gets to exit on SIGTERM before SIGKILL


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


# ---------------------------------------------------------------------------
# Turn classification (raw evidence only; Relay's status/run_error never decide)


@dataclass
class ToolCall:
    id: str
    tool: str
    kind: str
    label: str
    done: bool
    background_open: bool = False
    background_closed: bool = False
    parent: str | None = None
    detached: bool = False
    ended_abnormally: bool = False  # killed/failed/stopped task, or an error tool result
    command_detached: bool = False  # the command itself detaches (`&`, nohup, tmux, a daemon): no event proves it ended
    probe: dict[str, Any] = field(default_factory=dict, repr=False)  # raw inputs for the deterministic probes; never persisted

    @property
    def pending(self) -> bool:
        """Still possibly executing: no result yet, or a background launch whose completion was never seen
        (its tool result is only the launch acknowledgement)."""
        return (not self.done or self.background_open or (self.detached and not self.background_closed)
                or self.command_detached)


@dataclass
class TurnReport:
    outcome: str
    agent: str | None
    detail: str
    reset_at: str | None = None
    calls: list[ToolCall] = field(default_factory=list)
    bad_lines: int = 0
    terminal: bool = False
    relay_status: str | None = None
    no_evidence: bool = False  # the turn left no output of its own: nothing shows what it did
    turn_id: str | None = None  # "<session>/<turn>" of the classified turn
    spend_cap: bool = False  # the provider message named a spend/credit cap (even if it also printed a reset)
    reaped: list[str] | None = None  # processes the turn left running, stopped at its end; None: not launched here

    @property
    def in_flight(self) -> list[ToolCall]:
        return [c for c in self.calls if c.pending and c.kind != "read_only"]

    @property
    def effects(self) -> list[ToolCall]:
        return [c for c in self.calls if not c.pending and c.kind != "read_only"]

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        for call in data["calls"]:
            call.pop("probe", None)
        data["in_flight"] = [c.label for c in self.in_flight]
        return data


SPEND_RE = re.compile(r"(?i)monthly spend limit|spend limit|credit balance is too low|purchase more credits to continue")
QUOTA_RE = re.compile(r"(?i)hit your (session|weekly|usage|daily|5-hour|five-hour) limit|usage limit|rate[ _-]?limit|"
                      r"limit reached|too many requests|quota")
AUTH_RE = re.compile(r"(?i)invalid api key|please run /login|/login|oauth token (has )?expired|not logged in|"
                     r"unauthori[sz]ed|authentication (failed|error|required)|log ?in again|credentials? (expired|invalid)")
TRANSIENT_RE = re.compile(r"(?i)EAI_AGAIN|ENOTFOUND|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EPIPE|can'?t reach the api|"
                          r"couldn'?t reach|unable to connect|network error|socket hang up|stream disconnected|"
                          r"error sending request|connection (reset|refused|closed|error)|overloaded|"
                          r"routing discovery failed|reconnecting\.\.\. \d+/\d+|couldn.t reach|"
                          r"internal server error|bad gateway|service unavailable|gateway timeout|timed? ?out")
CLAUDE_RESET_RE = re.compile(
    r"(?i)resets\s+(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+-]+)+)\)")
CODEX_RESET_RE = re.compile(
    r"(?i)try again (?:at|after)\s+(?:(?P<mon>[A-Z][a-z]{2})[a-z]*\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?:\d{4}\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)")
STALE_RESET_WINDOW = timedelta(hours=6)
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def classify_text(text: str, status: int | None) -> str:
    if SPEND_RE.search(text):
        return "spend_limit"
    if status == 429 or QUOTA_RE.search(text):
        return "quota"
    if status in (401, 403) or AUTH_RE.search(text):
        return "auth"
    if (status is not None and (500 <= status <= 599)) or TRANSIENT_RE.search(text):
        return "transient"
    return "unknown"


def parse_reset(text: str, ref: datetime, local_tz: Any = None) -> datetime | None:
    """Next occurrence after `ref` of a provider-printed reset time, or None."""
    if match := CLAUDE_RESET_RE.search(text):
        try:
            tz: Any = ZoneInfo(match["tz"])
        except (ZoneInfoNotFoundError, ValueError):
            return None
    elif match := CODEX_RESET_RE.search(text):
        tz = local_tz or datetime.now().astimezone().tzinfo
    else:
        return None
    hour = int(match["h"]) % 12 + (12 if match["ap"].lower() == "pm" else 0)
    minute = int(match["m"] or 0)
    local_ref = ref.astimezone(tz)
    if match["mon"] and match["mon"][:3].lower() in MONTHS:
        month, day = MONTHS[match["mon"][:3].lower()], int(match["day"])
        for year in (local_ref.year, local_ref.year + 1):
            try:
                cand = datetime(year, month, day, hour, minute, tzinfo=tz)
            except ValueError:
                return None
            if cand > local_ref - timedelta(minutes=1):
                return cand.astimezone(UTC)
        return None
    cand = local_ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= local_ref - timedelta(minutes=1):
        if local_ref - cand < STALE_RESET_WINDOW:
            # A time-only reset just behind the turn's end is a stale message, not tomorrow's reset;
            # report it as due now so the cooldown falls back to a short recheck instead of a day's stall.
            return ref.astimezone(UTC)
        cand += timedelta(days=1)
    return cand.astimezone(UTC)


READ_ONLY_CMDS = {
    "ls", "cat", "head", "tail", "grep", "rg", "egrep", "fgrep", "wc", "sort", "uniq", "cut", "tr", "awk", "echo",
    "printf", "pwd", "which", "type", "file", "stat", "du", "df", "ps", "pgrep", "date", "sleep", "true", "false",
    "test", "[", "diff", "cmp", "sha256sum", "sha1sum", "md5sum", "jq", "column", "basename", "dirname", "realpath",
    "readlink", "id", "whoami", "uname", "hostname", "nproc", "free", "uptime", "tree", "less", "more", "strings",
    "xxd", "od", "hexdump", "nvidia-smi", "comm", "paste", "join", "seq", "cd", "export", "set", "exit", "wait",
    "nl", "fold", "rev", "tac", "zcat", "lsof", "ss", "getent", "printenv", "locale", "tput", ":",
    "read", "mapfile", "readarray", "SUBST", "let", "shopt", "ulimit", "umask", "hash", "alias",
}
SHELL_KEYWORDS = {"for", "do", "done", "while", "until", "if", "then", "elif", "else", "fi", "case", "esac", "in",
                  "{", "}", "(", ")", "!", "function", "select", "time", "local", "declare", "return", "break",
                  "continue", "shift", "unset", "trap"}
LOCAL_WRITE_CMDS = {
    "rm", "mv", "cp", "mkdir", "rmdir", "touch", "chmod", "chown", "ln", "tee", "truncate", "dd", "tar", "unzip",
    "zip", "gzip", "gunzip", "patch", "install", "pip", "pip3", "npm", "pnpm", "yarn", "uv", "make", "pytest",
    "kill", "pkill", "killall", "systemctl", "crontab", "sqlite3", "alembic", "ruff", "mypy", "black", "prettier",
    "eslint", "tsc", "vite", "vitest", "playwright",
}
EXTERNAL_CMDS = {"ssh", "scp", "sftp", "nc", "ncat", "telnet", "sendmail", "mail", "twine", "notify-slack.sh"}
WRAPPERS = {"timeout", "nice", "ionice", "stdbuf", "command", "builtin", "exec", "xvfb-run", "env"}
BACKGROUND_WRAPPERS = {"nohup", "setsid", "disown", "systemd-run", "daemonize"}
DETACH_FLAGS = {"--detach", "--daemon", "--daemonize", "--background"}
# Git subcommands whose `--detach` means a detached HEAD, not a detached process (`git daemon --detach` still is).
GIT_HEAD_DETACH = {"git checkout", "git switch", "git worktree"}
TMUX_GLOBAL_VALUE_OPTS = {"-L", "-S", "-f", "-c", "-T"}
SCREEN_VALUE_OPTS = {"-S", "-c", "-e", "-h", "-p", "-s", "-t", "-T", "-Logfile"}
XXD_VALUE_OPTS = {"-c", "-cols", "-g", "-groupsize", "-l", "-len", "-o", "-s", "-seek", "-n", "-name"}
TMUX_LAUNCH = {"new-session", "new", "new-window", "neww", "split-window", "splitw", "send-keys", "send",
               "run-shell", "run", "respawn-pane", "respawnp", "respawn-window", "respawnw"}
GIT_READ = {"status", "log", "diff", "show", "fetch", "rev-parse", "ls-files", "ls-tree", "cat-file", "merge-base",
            "merge-tree", "describe", "blame", "grep", "for-each-ref", "rev-list", "shortlog", "remote", "reflog",
            "show-ref", "name-rev", "check-ignore", "count-objects", "verify-commit", "range-diff", "whatchanged"}
GH_READ = {("pr", "view"), ("pr", "list"), ("pr", "checks"), ("pr", "diff"), ("pr", "status"), ("run", "view"),
           ("run", "list"), ("run", "watch"), ("repo", "view"), ("issue", "view"), ("issue", "list"),
           ("release", "view"), ("release", "list"), ("auth", "status"), ("search", "prs"), ("search", "issues"),
           ("search", "code"), ("workflow", "list"), ("workflow", "view"), ("cache", "list")}
# awk programs that run commands (system(), pipes to/from commands) or print into files.
# awk runs commands through system(), getline from a pipe, or print/printf into a pipe; it writes with
# `print > file`. Any pipe or output redirection in the program is treated as execution (`||` included).
AWK_EXEC = re.compile(r"system\s*\(|\||print[f]?\b[^;}]*>")
SED_ADDRESS = re.compile(r"\s*(?:(?:\d+(?:~\d+)?|\$|/(?:\\.|[^/\\])*/[IM]*|\\(.)(?:\\.|(?!\1).)*\1[IM]*)"
                         r"(?:\s*,\s*(?:[+~]?\d+|\$|/(?:\\.|[^/\\])*/[IM]*))?)?\s*!?\s*")


def sed_scripts(args: list[str]) -> tuple[list[str], bool]:
    """Every sed program text (`-e S`, `-eS`, `--expression=S`, bare S), and whether one is read from a file."""
    scripts, from_file, i = [], False, 0
    while i < len(args):
        a = args[i]
        if a in ("-f", "--file") or a.startswith("--file="):
            from_file = True
        elif a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a in ("-e", "--expression") and i + 1 < len(args):
            scripts.append(args[i + 1])
            i += 1
        elif a.startswith("-") and not a.startswith("--"):
            cluster = a[1:]
            for j, ch in enumerate(cluster):
                if ch == "f":
                    from_file = True
                    break
                if ch == "e":
                    if cluster[j + 1:]:
                        scripts.append(cluster[j + 1:])
                    elif i + 1 < len(args):
                        scripts.append(args[i + 1])
                        i += 1
                    break
        elif not a.startswith("-"):
            scripts.append(a)
        i += 1
    return scripts, from_file


def sed_executes(script: str) -> bool:
    """Whether a sed program can run commands or write files: an `e`, `w` or `W` command, or an `s` command
    with an `e`/`w` flag, after any address. Unparseable programs count as executing."""
    i, n = 0, len(script)
    while i < n:
        while i < n and script[i] in " \t\n;{}":
            i += 1
        if i >= n:
            return False
        match = SED_ADDRESS.match(script, i)
        i = match.end() if match else i
        if i >= n:
            return False
        cmd = script[i]
        if cmd in "ewW":
            return True
        if cmd == "s":
            if i + 1 >= n:
                return True
            delim, j, parts = script[i + 1], i + 2, 0
            while j < n and parts < 2:
                if script[j] == "\\":
                    j += 2
                    continue
                if script[j] == delim:
                    parts += 1
                j += 1
            if parts < 2:
                return True  # unterminated substitution: cannot tell
            flags = re.match(r"[A-Za-z0-9]*", script[j:]).group(0)
            if set(flags) & set("ewW"):
                return True
            i = j + len(flags)
        elif cmd in "aic":
            # text commands consume the rest of the line
            nl = script.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif cmd in "rRbtTvy:#":
            nl = script.find("\n", i)
            if cmd == "y":
                # y/src/dst/: the opening delimiter, then exactly two more
                if i + 1 >= n:
                    return True
                delim, j, parts = script[i + 1], i + 2, 0
                while j < n and parts < 2:
                    if script[j] == "\\":
                        j += 2
                        continue
                    if script[j] == delim:
                        parts += 1
                    j += 1
                if parts < 2:
                    return True
                i = j
            elif cmd in "rR":
                return True  # reads a file: harmless, but its argument runs to end of line; stay conservative
            elif cmd == "#":
                i = n if nl == -1 else nl + 1
            else:  # b/t/T/v/: take a label that ends at `;` or a newline (GNU); later commands still count
                ends = [k for k in (script.find(";", i), nl) if k != -1]
                i = min(ends) + 1 if ends else n
        else:
            i += 1
    return False

REDIRECT = re.compile(r"(?<![<>])(?:[0-9]*|&)>{1,2}(?!&)\s*(?!/dev/(?:null|stdout|stderr)\b)(\S+)")


def _rank(kind: str) -> int:
    return EFFECT_ORDER.index(kind)


def _worst(kinds: list[str]) -> str:
    return max(kinds, key=_rank) if kinds else "read_only"


def _operands(args: list[str], value_opts: set[str]) -> list[str]:
    """Non-option arguments, skipping the separate value of each option in `value_opts`."""
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a.startswith("-") and a != "-":
            skip = a in value_opts
        else:
            out.append(a)
    return out


TMUX_READ = {"ls", "list-sessions", "has-session", "has", "capture-pane", "capturep", "list-windows", "lsw",
             "list-panes", "lsp", "display-message", "display", "show-options", "show"}


def _tmux_commands(args: list[str]) -> list[str]:
    """The command name of every entry in a tmux command list (`a \\; b`, a `;` word after shlex), after the
    global options."""
    groups: list[list[str]] = [[]]
    for a in args:
        if a in (";", "\\;"):
            groups.append([])
        else:
            groups[-1].append(a)
    first = next(iter(_operands(groups[0], TMUX_GLOBAL_VALUE_OPTS)), "")
    return [c for c in [first, *(g[0] for g in groups[1:] if g)] if c]


def _tmux_launches(command: str) -> bool:
    if command in TMUX_LAUNCH or command in ("source-file", "source"):
        return True
    # tmux accepts any unambiguous prefix (`new-s`); a prefix of a launch command counts as one
    return len(command) >= 3 and any(c.startswith(command) for c in TMUX_LAUNCH | {"source-file"} if "-" in c)


def _screen_detached(args: list[str]) -> bool:
    """`screen -dm`, `screen -d -m`, `screen -DmS name`: the session runs on after screen returns."""
    letters: set[str] = set()
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a.startswith("-") and not a.startswith("--"):
            letters.update(a[1:])
            skip = a in SCREEN_VALUE_OPTS
        else:
            break  # the program and its own arguments
    return bool(letters & {"d", "D"}) and "m" in letters


def _container_detached(args: list[str]) -> bool:
    """`run -d`, `exec -d`, `compose up -d`, `start`/`restart`: containers keep running after the CLI returns."""
    # Token-based on purpose: option values (`-f compose.dev.yml`, `-p name`) make positional subcommand parsing
    # fragile, and an extra hold on a coincidental token is acceptable where a missed detachment is not.
    if any(a in ("--detach", "--wait") or a.startswith("--detach=")  # `up --wait` implies -d
           or (a.startswith("-") and not a.startswith("--") and "d" in a[1:]) for a in args):  # -d, -dt, -itd
        return True
    return (any(a in ("start", "restart", "unpause") for a in args if not a.startswith("-"))
            and "-a" not in args and "--attach" not in args)


GIT_BRANCH_READ_OPTS = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "-l", "--list", "--show-current",
                        "--merged", "--no-merged", "--contains", "--no-contains", "--points-at", "--color",
                        "--no-color", "--column", "--no-column", "-i", "--ignore-case", "--omit-empty"}
GIT_BRANCH_FILTERS = {"-l", "--list", "--merged", "--no-merged", "--contains", "--no-contains", "--points-at"}


def _git_branch_lists(rest: list[str]) -> bool:
    """`git branch` only lists when every option is a listing option; a bare name creates a branch."""
    for a in rest:
        if a.startswith("-") and a not in GIT_BRANCH_READ_OPTS and not a.startswith(
                ("--format=", "--sort=", "--color=", "--column=", "--merged=", "--no-merged=", "--contains=",
                 "--no-contains=", "--points-at=")):
            return False
    names = [a for a in rest if not a.startswith("-")]
    return not names or any(a in GIT_BRANCH_FILTERS for a in rest)


def _git_kind(args: list[str]) -> tuple[str, str]:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    sub = args[i] if i < len(args) else ""
    rest = args[i + 1:]
    label = f"git {sub}".strip()
    if sub == "push":
        return "external", label
    if sub in GIT_READ:
        if any(a == "--output" or a.startswith("--output=") for a in rest):
            return "local_write", f"{label} --output"  # diff-family `--output` creates or truncates a file
        if sub == "remote":
            ops = [a for a in rest if a not in ("-v", "--verbose")]
            return ("read_only" if not ops or ops[0] in ("show", "get-url") else "local_write"), label
        if sub == "reflog" and any(a in ("expire", "delete", "drop") for a in rest):
            return "local_write", label
        return "read_only", label
    if sub == "branch" and _git_branch_lists(rest):
        return "read_only", label
    if sub == "worktree" and rest[:1] == ["list"]:
        return "read_only", "git worktree list"
    if sub == "stash" and rest[:1] in (["list"], ["show"]):
        return "read_only", label
    if sub == "tag" and (not rest or rest[0] in ("-l", "--list")):
        return "read_only", label
    if sub == "config" and rest[:1] in (["--get"], ["--list"], ["-l"], ["--get-all"]):
        return "read_only", label
    return "local_write", label


def _gh_kind(args: list[str]) -> tuple[str, str]:
    words = [a for a in args if not a.startswith("-")]
    pair = tuple(words[:2])
    label = "gh " + " ".join(words[:2])
    if words[:1] == ["api"]:
        method, fields = None, False
        for i, a in enumerate(args):
            if a in ("-X", "--method") and i + 1 < len(args):
                method = args[i + 1].upper()
            elif a.startswith("--method="):
                method = a.split("=", 1)[1].upper()
            elif a.startswith("-X") and len(a) > 2:
                method = a[2:].upper()
            elif a.startswith(("-f", "-F", "--field", "--raw-field", "--input")):
                fields = True  # `-f k=v`, attached `-fk=v`, `--field=k=v`: gh switches to POST
        if "graphql" in words[1:2]:
            method = method or "POST"
        method = method or ("POST" if fields else "GET")
        return ("read_only" if method in ("GET", "HEAD") else "external"), f"gh api {method}"
    if pair in GH_READ:
        return "read_only", label
    return "external", label


def _curl_kind(word: str, args: list[str]) -> tuple[str, str]:
    if word == "curl" and any(a == "--config" or a.startswith("--config=")
                              or (a.startswith("-") and not a.startswith("--") and "K" in a[1:]) for a in args):
        return "unknown", "curl --config"  # a config file can set any method, body or output
    writes = _download_writes(word, args)
    for i, a in enumerate(args):
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if a.startswith(("--request", "--method")):
            method = (a.split("=", 1)[1] if "=" in a else nxt).upper()
            if method not in ("GET", "HEAD"):
                return "external", f"{word} {method or '?'}"
        elif a.startswith(("--data", "--form", "--upload-file", "--json", "--post", "--body", "--put")):
            return "external", f"{word} POST"
        elif a.startswith("-") and not a.startswith("--"):
            # Short-option cluster (`-sXPOST`, `-sd@f`): X/d/F/T take the rest of the cluster or the next word.
            for j, ch in enumerate(a[1:], 1):
                if ch == "X":
                    method = (a[j + 1:] or nxt).upper()
                    if method not in ("GET", "HEAD"):
                        return "external", f"{word} {method or '?'}"
                    break
                if ch in "dFT":
                    return "external", f"{word} POST"
                if ch in "oOHuAeEbcKw":  # options whose value follows; stop scanning the cluster
                    break
    return ("local_write", f"{word} > file") if writes else ("read_only", f"{word} GET")


def _download_writes(word: str, args: list[str]) -> bool:
    """Whether curl/wget writes the response (or headers/cookies) to a file rather than stdout."""
    def to_file(value: str) -> bool:
        return value not in ("-", "/dev/null", "/dev/stdout", "/dev/stderr", "")
    if word == "wget":
        # wget saves to a file unless the output document is stdout or /dev/null, or it only checks (--spider).
        if any(a in ("--output-file", "--append-output", "--background") or a.startswith(("--output-file=", "--append-output="))
               or (a.startswith("-") and not a.startswith("--") and set(a[1:]) & set("oab")) for a in args):
            return True  # -o/-a log files, and -b writes wget-log
        if "--spider" in args:
            return False
        for i, a in enumerate(args):
            if a in ("-O", "--output-document") and i + 1 < len(args):
                return to_file(args[i + 1])
            if a.startswith("--output-document="):
                return to_file(a.split("=", 1)[1])
            if a.startswith("-O") and len(a) > 2:
                return to_file(a[2:])
            if a.startswith("-") and not a.startswith("--") and "O" in a[1:]:
                rest = a[a.index("O") + 1:]
                return to_file(rest or (args[i + 1] if i + 1 < len(args) else ""))
        return True
    for i, a in enumerate(args):
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if a in ("--remote-name", "--remote-name-all", "--output-dir", "--create-dirs"):
            return True
        if a.startswith(("--output", "--dump-header", "--cookie-jar", "--trace", "--stderr")):
            value = a.split("=", 1)[1] if "=" in a else nxt
            if to_file(value):
                return True
        elif a.startswith("-") and not a.startswith("--"):
            for j, ch in enumerate(a[1:], 1):
                if ch == "O":
                    return True
                if ch in "oDc":
                    if to_file(a[j + 1:] or nxt):
                        return True
                    break
                if ch in "XdFTHuAeEbKw":
                    break
    return False


def strip_heredocs(command: str) -> str:
    """Remove heredoc bodies: they are data written somewhere, not commands. An unquoted delimiter's body
    still expands $(...) and backticks, so those substitutions are kept as commands."""
    lines, out, i = command.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        delims = re.findall(r"(?<!<)<<(?!<)-?\s*(['\"\\]?)([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        i += 1
        for quote, delim in delims:
            body = []
            while i < len(lines) and lines[i].strip() != delim:
                body.append(lines[i])
                i += 1
            i += 1
            if not quote:
                out.extend(f"$({sub})" for sub in substitutions("\n".join(body))[0])
    return "\n".join(out)


def split_segments(command: str) -> list[str]:
    """Split on ; & | && || and newlines outside quotes, $(...) and backslash escapes."""
    segments, current, quote, depth, i = [], [], None, 0, 0
    while i < len(command):
        ch = command[i]
        if quote:
            current.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                current.append(command[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch == "\\" and i + 1 < len(command):
            current.append(command[i:i + 2])
            i += 1
        elif ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif command.startswith("$(", i):
            depth += 1
            current.append("$(")
            i += 1
        elif ch == ")" and depth:
            depth -= 1
            current.append(ch)
        elif depth == 0 and ch in ";|\n":
            segments.append("".join(current))
            current = []
            if command.startswith(("||", "|&"), i):
                i += 1
        elif depth == 0 and ch == "&":
            if command.startswith("&&", i):
                segments.append("".join(current))
                current = []
                i += 1
            elif i > 0 and command[i - 1] in "<>" or command.startswith("&>", i) or command.startswith("&1", i) or command.startswith("&2", i):
                current.append(ch)
            else:
                current.append(ch)
                segments.append("".join(current))
                current = []
        else:
            current.append(ch)
        i += 1
    segments.append("".join(current))
    return [seg for seg in segments if seg.strip()]


def split_first_word(text: str) -> tuple[str | None, str]:
    """The first shell word of `text` (quotes removed) and the raw text after it, or (None, text)."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        first = lexer.get_token()
    except ValueError:
        return None, text
    if not first:
        return None, text
    return first, text[lexer.instream.tell():]


def substitutions(command: str) -> tuple[list[str], str]:
    """Bodies of $(...), <(...), >(...) and backtick substitutions (they run as commands), and the
    command text with each substitution replaced by a placeholder word."""
    bodies, outer, i = [], [], 0
    while i < len(command):
        if command.startswith(("$(", "<(", ">("), i) and not command.startswith("$((", i):
            depth, j = 1, i + 2
            while j < len(command) and depth:
                depth += {"(": 1, ")": -1}.get(command[j], 0)
                j += 1
            bodies.append(command[i + 2:j - 1])
            outer.append("SUBST")
            i = j
        elif command[i] == "`":
            j = command.find("`", i + 1)
            end = len(command) if j == -1 else j
            bodies.append(command[i + 1:end])
            outer.append("SUBST")
            i = end + 1
        else:
            outer.append(command[i])
            i += 1
    return [b for b in bodies if b.strip()], "".join(outer)


def classify_command(command: str, _depth: int = 0) -> tuple[str, str]:
    """Worst effect class over every shell segment, with a short label of the worst segment."""
    command = command.strip()
    if _depth > 4:
        return "unknown", "nested substitution"
    for prefix in ("bash -lc ", "bash -c ", "/bin/bash -lc ", "/bin/bash -c ", "sh -c ", "/bin/sh -c "):
        if command.startswith(prefix):
            program, rest = split_first_word(command[len(prefix):])
            if program is not None:
                # The wrapped program is classified as a command; whatever follows it in the outer shell
                # (redirections, `&`, `; more`) is classified too, with the program as a neutral placeholder.
                wrapper = prefix.split()[0]
                parts = [classify_command(program, _depth + 1)]
                if rest.strip():
                    outer_kind, outer_label = classify_command(f"true {rest}", _depth + 1)
                    outer_label = outer_label.replace("true", wrapper, 1)
                    if outer_label.startswith(wrapper) and outer_label.endswith(" &") and not parts[0][1].endswith(" &"):
                        parts[0] = (parts[0][0], f"{parts[0][1]} &")  # an outer `&` detaches the wrapped program
                    parts.append((outer_kind, outer_label))
                # Detachment from either side survives, independently of which side has the worse effect.
                return _combine(parts)
            break
    bodies, outer = substitutions(strip_heredocs(command))
    results: list[tuple[str, str]] = [classify_command(body, _depth + 1) for body in bodies]
    for raw in split_segments(outer):
        seg = raw.strip()
        if not seg:
            continue
        background = seg.endswith("&") and not seg.endswith("&&")
        seg = seg.rstrip("&").strip()
        try:
            words = shlex.split(seg, comments=True)
        except ValueError:
            words = seg.split()
        if words and words[0] == "case":
            # `case WORD in PATTERN) BODY`: the arm body runs, so it is classified; a bare header is skipped.
            arm = next((k for k in range(2, len(words)) if ")" in words[k] and "in" in words[1:k]), None)
            if arm is None:
                continue
            tail = words[arm].split(")", 1)[1]
            words = ([tail] if tail else []) + words[arm + 1:]
        # Subshell parentheses are grouping, not part of a command word.
        words = [w.rstrip(")") if w.count(")") > w.count("(") else w for w in words]
        while words and words[0].startswith("(") and not words[0].startswith("(("):
            words = ([words[0][1:]] if words[0][1:] else []) + words[1:]
        if words and words[0] in ("for", "select"):
            continue  # loop headers bind variables; their bodies are separate segments
        while words and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]) or words[0] in SHELL_KEYWORDS
                         or re.fullmatch(r"\d*[<>]{1,2}&?\S*", words[0])):
            bare_redirect = bool(re.fullmatch(r"\d*[<>]{1,2}", words[0]))
            words = words[2:] if bare_redirect else words[1:]
        while words and (words[0] in WRAPPERS or words[0] in BACKGROUND_WRAPPERS):
            background = background or words[0] in BACKGROUND_WRAPPERS
            words = words[1:]
            while words and (words[0].startswith("-") or re.fullmatch(r"[0-9.]+[smhd]?", words[0])
                             or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0])):
                words = words[1:]
        if not words:
            if REDIRECT.search(seg):
                results.append(("local_write", "> file"))  # a bare redirection truncates or creates a file
            continue
        word = Path(words[0]).name
        args = words[1:]
        if word == "git":
            kind, label = _git_kind(args)
        elif word == "gh":
            kind, label = _gh_kind(args)
        elif word in ("curl", "wget"):
            kind, label = _curl_kind(word, args)
        elif word == "sed":
            scripts, from_file = sed_scripts(args)
            if from_file or any(sed_executes(a) for a in scripts):
                kind, label = "unknown", "sed e/w"  # GNU sed can execute commands and write files
            else:
                in_place = any(a == "--in-place" or a.startswith("--in-place=")
                               or (a.startswith("-") and not a.startswith("--") and "i" in a[1:]) for a in args)
                kind, label = ("local_write" if in_place else "read_only"), "sed"
        elif word in ("awk", "gawk", "mawk", "nawk"):
            from_file = any(a in ("-f", "--file", "-i", "--include", "-l", "--load") or a.startswith(("-f", "--file=", "-i", "--include="))
                            for a in args)
            kind, label = ("unknown" if from_file or any(AWK_EXEC.search(a) for a in args) else "read_only"), word
        elif word == "find":
            if any(a in ("-delete", "-fprint", "-fprint0", "-fprintf", "-fls") for a in args):
                kind, label = "local_write", "find (writes)"
            elif any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in args):
                kind, label = "unknown", "find -exec"
            else:
                kind, label = "read_only", "find"
        elif word == "tmux":
            subs = _tmux_commands(args)
            if launched := next((c for c in subs if _tmux_launches(c)), None):
                # The command keeps running in the tmux server; the tool result only acknowledges the launch.
                kind, label, background = "unknown", f"tmux {launched}", True
            else:
                kind, label = ("read_only" if subs and all(c in TMUX_READ for c in subs) else "local_write"), "tmux"
        elif word == "screen" and (_screen_detached(args) or "-X" in args):
            kind, label, background = "unknown", "screen -dm", True
        elif word == "xxd" and len(_operands(args, XXD_VALUE_OPTS)) >= 2:
            kind, label = "local_write", "xxd > file"  # `xxd IN OUT` (and `xxd -r IN OUT`) writes OUT
        elif word in ("python", "python3") and args[:1] and args[0].endswith("devctx.py"):
            kind = "read_only" if args[1:2] in (["recover"], ["show"], ["search"], ["lanes"]) else "local_write"
            label = "devctx " + (args[1] if len(args) > 1 else "")
        elif word in ("python", "python3") and args[:1] and args[0].endswith("relay_continuity.py"):
            kind = "read_only" if args[1:2] in (["status"], ["classify"]) else "unknown"
            label = "relay_continuity " + (args[1] if len(args) > 1 else "")
        elif word == "rsync":
            kind, label = ("external" if any(":" in a and not a.startswith("-") for a in args) else "local_write"), "rsync"
        elif word in ("docker", "podman") and args[:1] == ["push"]:
            kind, label = "external", f"{word} push"
        elif word == "pm2" and args[:1] and args[0] in ("start", "restart", "reload", "resurrect"):
            kind, label, background = "unknown", f"pm2 {args[0]}", True  # pm2 runs the process under its daemon
        elif word == "gunicorn" and any(a == "-D" or (a.startswith("-") and not a.startswith("--") and "D" in a[1:])
                                        for a in args):
            kind, label, background = "unknown", "gunicorn -D", True
        elif word in ("docker", "podman", "docker-compose", "podman-compose") and _container_detached(args):
            kind, label, background = "unknown", f"{word} detached", True
        elif word in ("npm", "pnpm", "yarn") and args[:1] == ["publish"]:
            kind, label = "external", f"{word} publish"
        elif word == "sort" and any(a == "--output" or a.startswith("--output=")
                                    or (a.startswith("-") and not a.startswith("--") and "o" in a[1:]) for a in args):
            kind, label = "local_write", "sort -o"
        elif word == "uniq" and len([a for a in args if not a.startswith("-")]) >= 2:
            kind, label = "local_write", "uniq > file"  # `uniq IN OUT` writes OUT
        elif word in READ_ONLY_CMDS:
            kind, label = "read_only", word
        elif word in LOCAL_WRITE_CMDS:
            kind, label = "local_write", word
        elif word in EXTERNAL_CMDS:
            kind, label = "external", word
        else:
            kind, label = "unknown", word[:40]
        if REDIRECT.search(seg) and kind == "read_only":
            kind, label = "local_write", f"{label} > file"
        if not (word == "git" and label in GIT_HEAD_DETACH) and any(a.split("=", 1)[0] in DETACH_FLAGS
                                                                for a in args):
            # Any command asked to detach or daemonize outlives its tool result, whatever its kind.
            kind, background = (kind if _rank(kind) > _rank("unknown") else "unknown"), True
        if background:
            # The label keeps the detach marker whatever the kind: its tool result is only a launch.
            kind, label = (kind if _rank(kind) > _rank("background") else "background"), f"{label} &"
        results.append((kind, label))
    if not results:
        return "read_only", "(empty)"
    return _combine(results)


def _combine(results: list[tuple[str, str]]) -> tuple[str, str]:
    kind, label = max(results, key=lambda r: _rank(r[0]))
    detached = [r for r in results if r[1].endswith(" &")]
    if detached:
        # Any detached segment makes the whole call a background launch, whichever segment is the worst; the
        # label names the worst detached job, since that is what may still be running.
        label = max(detached, key=lambda r: _rank(r[0]))[1]
    return kind, label


TASK_DONE = ("completed", "failed", "killed", "stopped")
CLAUDE_TOOL_KINDS = {
    **{t: "read_only" for t in ("Read", "Grep", "Glob", "LS", "ToolSearch", "WebSearch", "WebFetch", "BashOutput",
                                "TaskOutput", "ListAgents", "NotebookRead", "TodoWrite", "Skill", "Monitor",
                                "ReportFindings", "ExitPlanMode")},
    **{t: "local_write" for t in ("Edit", "Write", "MultiEdit", "NotebookEdit", "EnterWorktree", "ExitWorktree",
                                  "TaskStop", "KillShell", "KillBash")},
    **{t: "external" for t in ("PushNotification", "SendMessage", "RemoteTrigger", "CronCreate", "CronDelete",
                               "ScheduleWakeup")},
}


def _claude_call(block: dict[str, Any]) -> ToolCall:
    name = str(block.get("name") or "?")
    inp = block.get("input") or {}
    if name == "Bash":
        kind, label = classify_command(str(inp.get("command") or ""))
    else:
        kind = CLAUDE_TOOL_KINDS.get(name, "unknown")
        label = name if not inp.get("file_path") else f"{name} {Path(str(inp['file_path'])).name}"
    command_detached = label.endswith(" &")
    detached = bool(inp.get("run_in_background")) or command_detached
    if inp.get("run_in_background"):
        # e.g. a background Agent or shell job: its tool result is a launch acknowledgement, not completion
        kind = kind if _rank(kind) > _rank("background") else "background"
        label = label if label.endswith(" &") else f"{label} &"
    probe = {k: inp[k] for k in ("command", "file_path", "old_string", "new_string", "content")
             if isinstance(inp.get(k), str)}
    return ToolCall(id=str(block.get("id")), tool=name, kind=kind, label=label[:80], done=False, detached=detached,
                    command_detached=command_detached, probe=probe)


def _codex_call(item: dict[str, Any]) -> ToolCall | None:
    typ = item.get("type")
    if typ == "command_execution":
        kind, label = classify_command(str(item.get("command") or ""))
    elif typ == "file_change":
        kind, label = "local_write", "file_change"
    elif typ == "mcp_tool_call":
        kind, label = "unknown", f"mcp {item.get('server', '?')}.{item.get('tool', '?')}"
    elif typ == "web_search":
        kind, label = "read_only", "web_search"
    else:
        return None
    probe = {"command": item["command"]} if isinstance(item.get("command"), str) else {}
    return ToolCall(id=str(item.get("id")), tool=str(typ), kind=kind, label=str(label)[:80], done=False,
                    detached=str(label).endswith(" &"), command_detached=str(label).endswith(" &"), probe=probe)


def read_events(path: Path) -> tuple[list[dict[str, Any]], int]:
    events, bad = [], 0
    if not path.exists():
        return events, 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            # Unparseable lines, including a torn final line from an interrupted writer, make effects uncertain.
            bad += 1
            continue
        if isinstance(item, dict):
            events.append(item)
    return events, bad


def classify_turn(turn_dir: Path, *, ref: datetime | None = None, local_tz: Any = None,
                  diagnostics: str = "") -> TurnReport:
    state: dict[str, Any] = {}
    try:
        state = json.loads((turn_dir / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        pass
    output = turn_dir / "output.jsonl"
    events, bad = read_events(output)
    agent = state.get("agent_key")
    if agent in NATIVE_AGENTS:
        events, bad = relay_argon.read_events(output)
        return classify_argon_turn(turn_dir, events, bad, agent=agent, ref=ref)
    if agent not in AGENTS:
        agent = "codex" if any(e.get("type") == "thread.started" for e in events) else (
            "claude" if any(e.get("type") in ("result", "system") for e in events) else None)
    if ref is None:
        try:
            ref = datetime.fromtimestamp(output.stat().st_mtime, UTC)
        except OSError:
            ref = now_utc()
    calls: dict[str, ToolCall] = {}
    task_calls: dict[str, str] = {}
    report = TurnReport(outcome="crash", agent=agent, detail="no terminal event", bad_lines=bad,
                        relay_status=state.get("status"), turn_id=f"{turn_dir.parent.parent.name}/{turn_dir.name}")
    if not events:
        report.detail = "output missing" if not output.exists() else ("output unparseable" if bad else "output empty")
        report.no_evidence = not bad
        return no_terminal_evidence(report, turn_dir, diagnostics)
    if same_output_as_previous_turn(turn_dir):
        # Relay keeps the previous turn's output when it captures nothing ("No response captured"); the agent
        # may still have worked for hours. Those events are not this turn's evidence.
        report.detail, report.no_evidence = "no output of its own (previous turn's output kept)", True
        return no_terminal_evidence(report, turn_dir, diagnostics)
    terminal: dict[str, Any] | None = None
    terminal_index = -1
    for idx, e in enumerate(events):
        typ = e.get("type")
        if typ == "assistant":
            for block in (e.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    call = _claude_call(block)
                    call.parent = e.get("parent_tool_use_id")
                    calls[call.id] = call
        elif typ == "user":
            content = (e.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                    calls[block["tool_use_id"]].done = True
                    if block.get("is_error") is True:
                        calls[block["tool_use_id"]].ended_abnormally = True
        elif typ == "system" and e.get("subtype") in ("task_started", "task_notification", "task_updated"):
            # Claude reports every tool task (foreground ones too). A task is open only once backgrounded,
            # and closes on a terminal status; updates and some notifications carry only the task id.
            sub, patch = e.get("subtype"), e.get("patch") if isinstance(e.get("patch"), dict) else {}
            if sub == "task_started" and e.get("task_id") and e.get("tool_use_id"):
                task_calls[str(e["task_id"])] = str(e["tool_use_id"])
            call = calls.get(e.get("tool_use_id") or task_calls.get(str(e.get("task_id"))))
            if call is None:
                pass
            elif (status := e.get("status") if sub == "task_notification" else patch.get("status")) in TASK_DONE:
                call.background_open, call.background_closed = False, True
                call.ended_abnormally = call.ended_abnormally or status != "completed"
            elif e.get("is_backgrounded") is True or patch.get("is_backgrounded") is True:
                call.background_open = True
        elif typ in ("item.started", "item.completed"):
            item = e.get("item") or {}
            if (call := _codex_call(item)) is not None:
                existing = calls.setdefault(call.id, call)
                if typ == "item.completed":
                    existing.done = True
        if typ in ("result", "turn.completed", "turn.failed", "error") and not e.get("parent_tool_use_id"):
            # A subagent's own result never decides the coordinator turn; the last top-level terminal does.
            # An empty, reason-less result is an internal restart artifact: it never overrides a real terminal.
            artifact = typ == "result" and not e.get("result") and e.get("terminal_reason") is None
            if not (artifact and terminal is not None):
                terminal, terminal_index = e, idx
    for call in calls.values():
        # A subagent's calls are settled by its parent call (results inside a subagent are not always streamed),
        # but only when the parent truly finished: a background subagent's acknowledgement settles nothing.
        parent = calls.get(call.parent) if call.parent else None
        if parent is not None and not parent.pending and not parent.ended_abnormally:
            call.done = True
    report.calls = list(calls.values())
    if terminal is None:
        return no_terminal_evidence(report, turn_dir, diagnostics)
    report.terminal = True
    typ = terminal.get("type")
    if typ == "result":
        if terminal.get("is_error") is False and terminal.get("terminal_reason") == "completed":
            report.outcome, report.detail = "success", "result ok"
        elif terminal.get("is_error") is not True:
            # Missing/null is_error, or an empty result without a completed reason, is not evidence of success.
            report.outcome, report.detail = "unknown", f"result without completion (reason={terminal.get('terminal_reason')})"
        else:
            text = str(terminal.get("result") or terminal.get("terminal_reason") or "")
            status = terminal.get("api_error_status")
            report.outcome = classify_text(text, status if isinstance(status, int) else None)
            report.detail = f"{terminal.get('terminal_reason') or 'error'} status={status}"
            settle_limit(report, text, ref, local_tz)
    elif typ == "turn.completed":
        report.outcome, report.detail = "success", "turn.completed"
    else:
        # Codex reports failure as `error` then `turn.failed`; take the latest message.
        text = str(terminal.get("message") or (terminal.get("error") or {}).get("message") or "")
        for e in events[terminal_index::-1]:
            if e.get("type") == "turn.completed":
                break
            if e.get("type") in ("error", "turn.failed"):
                text = str(e.get("message") or (e.get("error") or {}).get("message") or text)
                break
        report.outcome = classify_text(text, None)
        report.detail = f"codex {typ}"
        settle_limit(report, text, ref, local_tz)
    return report


def classify_argon_turn(turn_dir: Path, events: list[dict[str, Any]], bad: int,
                        *, agent: str = "argon", ref: datetime | None = None) -> TurnReport:
    """Classify the retained native protocol, never model prose masquerading as execution evidence."""
    report = TurnReport(outcome="unknown", agent=agent, detail="native turn incomplete", bad_lines=bad,
                        turn_id=f"{turn_dir.parent.parent.name}/{turn_dir.name}")
    try:
        meta = json.loads((turn_dir / "native-result.json").read_text())
        if not isinstance(meta, dict) or meta.get("schema") != "argon-coordinator.v1" or meta.get("agent", "argon") != agent:
            raise ValueError("metadata")
    except (OSError, json.JSONDecodeError, ValueError):
        meta = {}
    reason = meta.get("reason")
    if not events and reason and meta.get("launched") is False:
        # Deterministic refusal before a model call has no hidden tool effects.
        report.outcome = "quota" if reason == "quota_exhausted" else (
            "auth" if reason in ("login_state_missing", "login_path_conflict", "agy_missing", "not_sandbox") else "transient")
        report.detail = f"native preflight refused: {reason}"
        if report.outcome == "quota":
            resets = [parse_ts(b.get("reset_time")) for b in meta.get("quota", [])
                      if isinstance(b, dict) and (b.get("disabled") or b.get("remaining_fraction") == 0)]
            known = [t for t in resets if t is not None]
            report.reset_at = iso(max(known)) if known and len(known) == len(resets) else None
        return report
    report.no_evidence = not events
    if events and not meta:
        report.bad_lines += 1
    calls: dict[str, ToolCall] = {}
    inputs: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    initialized = False
    conversation: str | None = None
    for index, event in enumerate(events):
        typ = event.get("event")
        if typ == "init":
            if initialized or index != 0 or not event.get("conversation_id"):
                report.bad_lines += 1
            initialized = True
            conversation = event.get("conversation_id")
            init = event.get("init")
            if not isinstance(init, dict) or init.get("model") != meta.get("model") or init.get("model") not in (relay_argon.OPUS_MODELS if agent == "agy_opus" else relay_argon.MODELS):
                report.bad_lines += 1
        elif typ == "step_update":
            step = event.get("step_update")
            if not isinstance(step, dict):
                report.bad_lines += 1
                continue
            if step.get("conversation_id") != conversation:
                report.bad_lines += 1
            if step.get("step_type") != "tool":
                continue
            info = step.get("tool_info") or {}
            name = step.get("tool_name")
            params = info.get("parameters") if isinstance(info, dict) else None
            number = step.get("step_index")
            if not isinstance(name, str) or not isinstance(params, dict) or isinstance(number, bool) \
                    or not isinstance(number, int) or step.get("state") not in ("ACTIVE", "DONE", "ERROR", "CANCELLED"):
                report.bad_lines += 1
                continue
            cid = str(number)
            if cid not in calls:
                if step["state"] != "ACTIVE":
                    report.bad_lines += 1
                if name == "run_command" and isinstance(params.get("CommandLine"), str):
                    kind, label = classify_command(params["CommandLine"])
                    kind = "external" if kind == "unknown" else kind
                    probe = {"command": params["CommandLine"]}
                else:
                    local = {"write_to_file", "replace_file_content", "multi_replace_file_content", "notebook_edit"}
                    read = {"view_file", "read_file", "list_dir", "find_by_name", "grep_search", "sed_file", "search_web",
                            "read_url_content", "command_status", "wait", "wait_5_seconds"}
                    kind, label, probe = ("local_write" if name in local else "read_only" if name in read else "external"), name, {}
                detached = label.endswith(" &") or (name == "run_command" and "WaitMsBeforeAsync" in params)
                calls[cid] = ToolCall(id=cid, tool=name, kind=kind, label=label[:80], probe=probe, done=False,
                                      detached=detached, command_detached=detached)
                inputs[cid] = params
            elif calls[cid].done or inputs[cid] != params:
                report.bad_lines += 1
            call = calls[cid]
            if call.tool != name:
                report.bad_lines += 1
            if step["state"] == "DONE":
                call.done = True
                # Errors in native tool metadata never count as successfully completed effects.
                call.ended_abnormally = bool(step.get("error") or info.get("error") or step.get("is_error"))
                if name == "run_command":
                    # DONE can be an asynchronous launch acknowledgement. Only the native executor's
                    # recorded exit status establishes that the foreground command actually ended.
                    output = info.get("output")
                    # The native CLI can prepend this exact envelope when it truncates a long tool result.
                    # The exit status still precedes command stdout; never search arbitrary stdout for one.
                    truncation = (r"The tool's output was truncated due to being too long\. You can find the full "
                                  r"output in a file at file://[^\r\n]+\.\n\n"
                                  r"Here is a preview with the middle of the output truncated:\n")
                    exit_match = re.match(rf"(?:{truncation})?Command exited with code (-?\d+)\.\n", output) if isinstance(output, str) else None
                    if exit_match is None:
                        call.done = False
                    elif int(exit_match[1]) != 0:
                        call.ended_abnormally = True
            elif step["state"] in ("ERROR", "CANCELLED"):
                call.done, call.ended_abnormally = True, True
        elif typ == "result":
            if not isinstance(event.get("result"), dict) or index != len(events) - 1:
                report.bad_lines += 1
            else:
                results.append(event["result"])
                if event["result"].get("conversation_id") != conversation:
                    report.bad_lines += 1
        else:
            report.bad_lines += 1
    report.calls = list(calls.values())
    if events and not initialized:
        report.bad_lines += 1
    if not initialized or report.bad_lines or len(results) != 1 or not meta \
            or any(c.pending or c.ended_abnormally for c in calls.values()):
        return report
    terminal = results[0]
    report.terminal = True
    if terminal.get("denied_actions"):
        report.detail = "native tool permission denied"
        return report
    text = terminal.get("response")
    if meta.get("exit_code") == 0 and terminal.get("status") == "SUCCESS" and isinstance(text, str) and text.strip():
        report.outcome, report.detail = "success", f"native {agent} result SUCCESS"
    else:
        report.outcome = classify_text(str(text or terminal.get("error") or reason or ""), None)
        report.detail = f"native {agent} failed or empty result"
        if report.outcome == "success":
            report.outcome = "unknown"
        settle_limit(report, str(text or ""), ref or now_utc(), None)
    return report


def same_output_as_previous_turn(turn_dir: Path) -> bool:
    number = turn_dir.name.split("-")[-1]
    if not number.isdigit() or int(number) <= 1:
        return False
    previous = turn_dir.with_name(f"turn-{int(number) - 1:0{len(number)}d}") / "output.jsonl"
    current = turn_dir / "output.jsonl"
    try:
        if previous.stat().st_size != current.stat().st_size:
            return False
        return hashlib.sha256(previous.read_bytes()).digest() == hashlib.sha256(current.read_bytes()).digest()
    except OSError:
        return False


def no_terminal_evidence(report: TurnReport, turn_dir: Path, diagnostics: str) -> TurnReport:
    """Without a terminal event, an authentication diagnostic (runner stderr/run JSON, or a turn stderr.log)
    makes the turn `auth` (held), never a retryable crash. Nothing here can ever yield success."""
    texts = [diagnostics]
    try:
        texts.append((turn_dir / "stderr.log").read_text(encoding="utf-8", errors="replace")[-8192:])
    except OSError:
        pass
    text = "\n".join(t for t in texts if t)
    if text and classify_text(text, None) == "auth":
        report.outcome, report.detail = "auth", f"{report.detail}; runner reported an authentication failure"
    return report


def settle_limit(report: TurnReport, text: str, ref: datetime, local_tz: Any) -> None:
    """Attach the provider-printed reset. A spend-limit message that also prints a timed reset (Claude's
    "monthly spend limit · your session limit resets 4pm") is a session quota with that reset."""
    if report.outcome not in ("quota", "spend_limit"):
        return
    reset = parse_reset(text, ref, local_tz)
    report.reset_at = iso(reset)
    report.spend_cap = report.outcome == "spend_limit"
    if report.spend_cap and reset is not None and reset <= ref:
        # The cap is still reported after the session reset it prints: this turn alone shows a real spend cap.
        report.detail += " (spend cap outlived its printed session reset)"
        return
    if report.outcome == "spend_limit" and reset is not None:
        report.outcome = "quota"
        report.detail += " (spend cap with timed session reset)"


def gate(report: TurnReport) -> str | None:
    """Hold reason when the turn's own record cannot show what it did, so no operation of it can be judged."""
    if report.bad_lines:
        return "output_unparseable"
    if report.no_evidence:
        return "output_missing"
    return None


def external(call: ToolCall) -> bool:
    """Whether an operation can act beyond this workstation (a push, a PR action, a remote API, a message), where a
    repeat cannot be taken back. MCP tools count: their servers may be remote."""
    return call.kind == "external" or call.tool.startswith("mcp__") or call.tool == "mcp_tool_call"


def last_turn_dir(session_dir: Path) -> Path | None:
    turns = sorted((p for p in (session_dir / "turns").glob("turn-*") if p.is_dir()),
                   key=lambda p: int(p.name.split("-")[-1]) if p.name.split("-")[-1].isdigit() else -1)
    return turns[-1] if turns else None


# ---------------------------------------------------------------------------
# Durable state


class StateCorrupt(Exception):
    pass


def default_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA, "phase": "idle", "head_session": None, "chain": [], "agent": None,
        "hold": None, "cooldowns": {}, "switches": [], "last_switch_at": None,
        "counters": {"consecutive_failures": 0, "transient_streak": 0, "crash_retries": 0},
        "permissions_digest": None, "permissions_pinned": False, "pending": None, "waiting_since": None, "last_outcome": None,
        "notify": {"sent": {}, "window": [], "last_digest": None, "failures": 0, "retry": None, "dropped": 0},
        "watch": {"seen": {}, "master": None}, "acknowledged": None, "spend_caps": {}, "updated_at": None,
    }


STATE_TYPES: dict[str, tuple[type, ...]] = {
    "head_session": (str, type(None)), "chain": (list,), "agent": (str, type(None)), "hold": (dict, type(None)),
    "cooldowns": (dict,), "switches": (list,), "last_switch_at": (str, type(None)), "counters": (dict,),
    "permissions_digest": (str, type(None)), "permissions_pinned": (bool,), "pending": (dict, type(None)),
    "waiting_since": (str, type(None)), "last_outcome": (str, type(None)), "notify": (dict,), "watch": (dict,),
    "acknowledged": (dict, type(None)), "spend_caps": (dict,), "updated_at": (str, type(None)),
}
PENDING_KEYS = ("nonce", "agent", "parent", "started_at", "pid", "pid_start", "fingerprint", "attempt")


GATE_HOLDS = ("effects_uncertain", "output_unparseable", "output_missing", "reconcile_unresolved",
              "reconcile_insufficient_permissions", "reconcile_failed", "reconcile_no_sandbox", "live_work")


def waivers(ack: dict[str, Any] | None) -> frozenset[str]:
    """What the maintainer's releases of a held turn reviewed, and so may waive when that turn is continued."""
    return frozenset({"gate"} if set((ack or {}).get("reasons") or []) & set(GATE_HOLDS) else set())


def validate_state(data: dict[str, Any]) -> str | None:
    for key, types in STATE_TYPES.items():
        if key not in data:
            return f"missing {key}"
        if not isinstance(data[key], types):
            return f"bad type for {key}"
    if not all(isinstance(data["counters"].get(k), int) for k in ("consecutive_failures", "transient_streak", "crash_retries")):
        return "bad counters"
    pending = data["pending"]
    if pending is not None and (any(k not in pending for k in PENDING_KEYS) or not isinstance(pending["nonce"], str)):
        return "bad pending attempt"
    # Optional: written only by the earlier spec 197 controller; dropped on the next run.
    rec = data.get("reconcile")
    if rec is not None and (not isinstance(rec, dict) or not isinstance(rec.get("turn"), str)
                            or not isinstance(rec.get("attempts"), int) or not isinstance(rec.get("verdicts"), dict)
                            or not isinstance(rec.get("since"), str)):
        return "bad reconcile record"
    diag = (rec or {}).get("diag")
    if diag is not None and (not isinstance(diag, dict) or any(k not in diag for k in PENDING_KEYS)
                             or not isinstance(diag["nonce"], str)):
        return "bad diagnostic attempt"
    if not isinstance(data.get("diagnostics", []), list):
        return "bad type for diagnostics"
    # Optional (spec 201): present only in a mission's own store.
    mission = data.get("mission")
    if mission is not None and (not isinstance(mission, dict) or not isinstance(mission.get("id"), str)
                                or not isinstance(mission.get("next"), int) or not isinstance(mission.get("total"), int)
                                or not 0 <= mission["next"] <= mission["total"] or not isinstance(mission.get("done"), list)):
        return "bad mission record"
    return None


def unsettled_nonces(journal: Path) -> list[str]:
    """Launched attempts with no settle record and not dropped by a maintainer release, oldest first."""
    launched: dict[str, None] = {}
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn final line is the only kind a crash can leave; the state's pending covers it
        if not isinstance(record, dict):
            continue
        if record.get("event") == "launch" and isinstance(record.get("nonce"), str) \
                and record.get("purpose") != "diagnostic":  # earlier read-only diagnoses changed nothing
            launched[record["nonce"]] = None
        elif record.get("event") == "settle":
            launched.pop(record.get("nonce"), None)
        elif record.get("event") == "release":
            for nonce in record.get("dropped") or []:
                launched.pop(nonce, None)
    return list(launched)


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.state_path = root / "state.json"
        self.journal_path = root / "attempts.jsonl"
        self.stop_path = root / "STOP"

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return default_state()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateCorrupt(f"state file unreadable: {type(exc).__name__}") from exc
        if not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("phase") not in PHASES:
            raise StateCorrupt("state file has an unknown schema or phase")
        if problem := validate_state(data):
            raise StateCorrupt(f"state file is structurally invalid: {problem}")
        return data

    def save(self, state: dict[str, Any], now: datetime) -> None:
        state["updated_at"] = iso(now)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f".state.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=1, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.state_path)
        dir_fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def journal(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.journal_path, "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle, fcntl.LOCK_UN)


class Lock:
    """Exclusive non-blocking flock; released by the kernel if the holder dies."""

    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            return False
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return True

    def release(self) -> None:
        if self.handle:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


# ---------------------------------------------------------------------------
# Outbound notification (Phase 2): fixed templates, allowlisted structured fields only

ISO_RE = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
FIELD_RULES: dict[str, Callable[[str], bool]] = {
    "session": lambda v: v == "none" or bool(re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", v)),
    "agent": lambda v: v in AGENTS,
    "from_agent": lambda v: v in AGENTS,
    "outcome": lambda v: v in OUTCOMES,
    "action": lambda v: v in ("switch", "retry", "wait", "hold", "idle", "notify"),
    "reason": lambda v: v in HOLD_REASONS + STOP_REASONS + ROADMAP_PAUSES,
    "reset_at": lambda v: v in ("none", "manual") or bool(re.fullmatch(ISO_RE, v)),
    "next_check_at": lambda v: v == "none" or bool(re.fullmatch(ISO_RE, v)),
    "claude_until": lambda v: v in ("none", "manual") or bool(re.fullmatch(ISO_RE, v)),
    "codex_until": lambda v: v in ("none", "manual") or bool(re.fullmatch(ISO_RE, v)),
    "argon_until": lambda v: v in ("none", "manual") or bool(re.fullmatch(ISO_RE, v)),
    "reset_source": lambda v: v in ("limits", "message", "estimate", "manual"),
    "attempt": lambda v: bool(re.fullmatch(r"\d{1,4}", v)),
    "attempts": lambda v: bool(re.fullmatch(r"\d{1,6}", v)),
    "holds": lambda v: bool(re.fullmatch(r"\d{1,6}", v)),
    "sha": lambda v: bool(re.fullmatch(r"[0-9a-f]{7,40}", v)),
    "pr": lambda v: v == "none" or bool(re.fullmatch(r"\d{1,6}", v)),
    "phase": lambda v: v in PHASES,
    "mission": lambda v: bool(MISSION_ID_RE.fullmatch(v)),
    "task": lambda v: bool(re.fullmatch(r"\d{1,2}/\d{1,2}", v)),
    "owner": lambda v: v == "control-plane",
}
OPTIONAL_FIELDS = ("reset_at", "next_check_at", "claude_until", "codex_until", "argon_until", "pr")
EVENTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "supervisor_started": ("JarvisOS ha ripreso il lavoro.", ("session", "agent")),
    "supervisor_stopped": ("JarvisOS ha interrotto il lavoro.", ("session", "reason", "phase")),
    "provider_switched": ("JarvisOS ha cambiato assistente e continua il lavoro.", ("session", "from_agent", "agent", "outcome")),
    "provider_exhausted": ("Un assistente non è disponibile; JarvisOS prova a proseguire.", ("agent", "outcome", "reset_at", "reset_source")),
    "all_exhausted": ("Gli assistenti non sono disponibili; JarvisOS riprenderà quando possibile.", ("next_check_at",)),
    "turn_failed": ("Un tentativo di lavoro non è riuscito; JarvisOS verifica come proseguire.", ("session", "agent", "outcome", "action")),
    "hold": ("JarvisOS richiede una verifica: scrivi a Claude in chat.", ("session", "reason", "attempt")),
    "circuit_open": ("JarvisOS ha sospeso i tentativi: scrivi a Claude in chat.", ("session", "reason")),
    "turn_completed": ("JarvisOS ha completato un passaggio di lavoro.", ("session", "agent")),
    "master_advanced": ("Una modifica è stata integrata in JarvisOS.", ("sha", "pr")),
    "repl_turn_failed": ("Un tentativo interattivo non è riuscito; controlla la chat.", ("session", "agent", "outcome", "reset_at")),
    "repl_claude_dont_ask": ("La sessione interattiva potrebbe non poter eseguire comandi: controlla la chat.", ("session",)),
    "test": ("Prova delle notifiche di JarvisOS.", ()),
    "task_completed": ("JarvisOS ha completato una parte del lavoro richiesto.", ("mission", "task", "agent", "session")),
    "mission_completed": ("JarvisOS ha completato il lavoro richiesto.", ("mission", "task", "session")),
    "mission_waiting": ("JarvisOS è in attesa di poter proseguire.",
                        ("owner", "mission", "task", "agent", "reason", "next_check_at")),
    "mission_held": ("JarvisOS richiede una verifica: scrivi a Claude in chat.",
                     ("owner", "mission", "task", "agent", "session", "reason")),
    "roadmap_paused": ("Il lavoro programmato è in pausa: scrivi a Claude in chat.", ("owner", "reason")),
}


def render(event: str, fields: dict[str, Any]) -> str | None:
    """Fixed template text, or None when any field is missing, unexpected or fails its validator."""
    if event not in EVENTS:
        return None
    title, allowed = EVENTS[event]
    if set(fields) - set(allowed):
        return None
    for key in allowed:
        value = fields.get(key)
        if value is None and key in OPTIONAL_FIELDS:
            value = "none"
        if value is None:
            return None
        value = str(value)
        if not FIELD_RULES[key](value):
            return None
    text = title
    return None if devctx.find_secret(text) else text


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201 - stdlib signature
        raise urllib.error.HTTPError(req.full_url, code, "redirect rejected", headers, fp)


# Proxies disabled and redirects rejected: the webhook body only ever reaches hooks.slack.com directly.
_WEBHOOK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())


def _valid_webhook_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname == "hooks.slack.com" and port is None
            and parts.username is None and parts.password is None and parts.path.startswith("/services/")
            and not parts.query and not parts.fragment)


def post_webhook(text: str, *, webhook_file: Path = WEBHOOK_FILE, timeout: float = 10.0) -> str | None:
    """POST to the incoming webhook. Returns None on success, else an error class (never the URL)."""
    try:
        url = webhook_file.read_text().strip()
    except OSError:
        return "webhook_missing"
    if not _valid_webhook_url(url):
        return "webhook_invalid"
    request = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with _WEBHOOK_OPENER.open(request, timeout=timeout) as response:
            response.read(256)
        return None
    except urllib.error.HTTPError as exc:
        return f"http_{exc.code}"
    except Exception as exc:  # noqa: BLE001 - delivery must never raise into the supervisor
        return type(exc).__name__


class Notifier:
    def __init__(self, state: dict[str, Any], *, sender: Callable[[str], str | None] | None,
                 log: Callable[[str], None], max_per_hour: int = 12,
                 dedupe: timedelta = timedelta(hours=6)):
        self.state = state
        self.sender = sender
        self.log = log
        self.max_per_hour = max_per_hour
        self.dedupe = dedupe

    def send(self, event: str, now: datetime, *, dedupe_key: str | None = None, **fields: Any) -> bool:
        if self.sender is None:
            return False
        notify = self.state["notify"]
        text = render(event, fields)
        if text is None:
            notify["dropped"] = notify.get("dropped", 0) + 1
            self.log(f"notify: dropped {event} (field validation)")
            return False
        key = hashlib.sha256((dedupe_key or f"{event}|{json.dumps(fields, sort_keys=True)}").encode()).hexdigest()[:20]
        return self._post(event, text, key, now, {"event": event, "fields": fields, "key": key})

    def send_text(self, kind: str, text: str, now: datetime) -> bool:
        """A semantic message composed by `relay_observe` (spec 205): same secret check, dedupe on `kind`, rate
        limit, failure accounting and single retry as the fixed templates."""
        if self.sender is None:
            return False
        if not text or len(text) > 3800 or devctx.find_secret(text):
            self.state["notify"]["dropped"] = self.state["notify"].get("dropped", 0) + 1
            self.log("notify: dropped semantic message (validation)")
            return False
        key = hashlib.sha256(kind.encode()).hexdigest()[:20]
        return self._post(kind.split("|", 1)[0], text, key, now, None)  # the observer re-sends until delivered

    def _post(self, event: str, text: str, key: str, now: datetime, retry: dict[str, Any] | None) -> bool:
        notify = self.state["notify"]
        last = parse_ts(notify["sent"].get(key))
        if last and now - last < self.dedupe:
            return False
        window = [t for t in notify["window"] if (ts := parse_ts(t)) and now - ts < timedelta(hours=1)]
        if len(window) >= self.max_per_hour:
            notify["window"] = window
            notify["dropped"] = notify.get("dropped", 0) + 1
            self.log(f"notify: rate-limited {event}")
            return False
        error = self._deliver(text)
        if error:
            notify["failures"] = notify.get("failures", 0) + 1
            notify["retry"] = retry if notify.get("retry") is None else notify["retry"]
            self.log(f"notify: delivery failed ({error})")
            return False
        window.append(iso(now))
        notify["window"] = window
        notify["sent"][key] = iso(now)
        notify["sent"] = {k: v for k, v in notify["sent"].items() if (ts := parse_ts(v)) and now - ts < timedelta(days=8)}
        return True

    def retry_pending(self, now: datetime) -> None:
        notify = self.state["notify"]
        pending = notify.get("retry")
        if not pending or self.sender is None:
            return
        notify["retry"] = None  # at most one retry; dropped afterwards whatever happens
        window = [t for t in notify["window"] if (ts := parse_ts(t)) and now - ts < timedelta(hours=1)]
        if len(window) >= self.max_per_hour:
            notify["window"] = window
            notify["dropped"] = notify.get("dropped", 0) + 1
            self.log(f"notify: rate-limited retry of {pending['event']}")
            return
        text = render(pending["event"], pending["fields"])
        if text and not self._deliver(text):
            notify["sent"][pending["key"]] = iso(now)
            notify["window"] = window + [iso(now)]

    def _deliver(self, text: str) -> str | None:
        try:
            return self.sender(text) if self.sender else "no_sender"
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__


# ---------------------------------------------------------------------------
# Relay and host adapters (injectable for tests)


@dataclass
class RunResult:
    session_id: str | None
    exit_code: int | None
    raw_ok: bool
    diagnostics: str = ""


class RelayAdapter:
    def __init__(self, repo: Path, binary: str = "agent-relay", env: dict[str, str] | None = None):
        self.repo = repo
        self.binary = binary
        self.env = env  # extra environment for launched runs (the sandboxed mission profile); None: inherited
        self._outputs: dict[int, tuple[Any, Any]] = {}
        self._argon_leases: dict[int, Any] = {}

    def limits(self) -> dict[str, Any] | None:
        try:
            proc = subprocess.run([self.binary, "--json", "limits", "--refresh"], capture_output=True, text=True,
                                  timeout=90, check=False, cwd="/tmp")
            data = json.loads(proc.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return None
        return {a.get("agent_key"): a for a in data.get("agents", []) if isinstance(a, dict)}

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
            relay_argon.model_for(agent, env)
            reading = relay_argon.read_quota(binary, env, agent=agent)
            result = {"windows": [{"used_percent": 100 * (1 - b["remaining_fraction"]),
                                   "resets_at": b.get("reset_time"), "disabled": b.get("disabled", False)}
                                  for b in reading]}
        except relay_argon.Refused:
            result = None
        if not hasattr(self, "_argon_reading"):
            self._argon_reading = {}
        self._argon_reading[agent] = (time.monotonic(), result)
        return result

    def start(self, agent: str, parent: str | None, task: str, turns: int) -> subprocess.Popen:
        # A mission's first task has no parent: Relay opens a new session for it.
        lease = None
        env = {**os.environ, **self.env} if self.env else dict(os.environ)
        if agent in NATIVE_AGENTS:
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
                session, _ = relay_argon.new_session(self.repo, task, agent)
            except OSError:
                lease.close()
                raise
            args = [sys.executable, str(Path(relay_argon.__file__)), "--repo", str(self.repo), "--session", session, "--agent", agent]
        else:
            args = [self.binary, "--json", "run", agent, *(["--continue", parent] if parent else []), "-t", task,
                    "-n", str(turns), "-y", "--repo", str(self.repo)]
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
        if lease:
            self._argon_leases[id(proc)] = lease
        return proc

    def release_turn(self, proc: subprocess.Popen) -> None:
        # The parent retains the same flock until its normal descendant reaper has finished.
        lease = self._argon_leases.pop(id(proc), None)
        if lease:
            lease.close()

    def finish(self, proc: subprocess.Popen) -> RunResult:
        proc.wait()
        files = self._outputs.pop(id(proc))
        texts = []
        for handle in files:
            handle.seek(0)
            texts.append(handle.read())
            handle.close()
        out, err = texts
        diagnostics = (err or "")[-8192:]
        try:
            data = json.loads(out)
            errors = [str(data.get(k)) for k in ("run_error", "error", "stop_reason") if data.get(k)]
            return RunResult(data.get("session_id"), proc.returncode, True, "\n".join([diagnostics, *errors]))
        except (json.JSONDecodeError, TypeError, AttributeError):
            return RunResult(None, proc.returncode, False, diagnostics)

    def sessions_dir(self) -> Path:
        return self.repo / ".agent-relay" / "sessions"

    def find_by_nonce(self, nonce: str, since: datetime) -> str | None:
        root = self.sessions_dir()
        floor = (since - timedelta(minutes=2)).strftime("%Y%m%d-%H%M%S")
        marker = re.compile(r"\[relay-continuity attempt (rc-[0-9TZ]+-[0-9a-f]+)\]")
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


# ---------------------------------------------------------------------------
# Interrupted external operations (spec 197 as amended): settled only by deterministic read-only probes

VERDICTS = ("completed", "running", "not_started", "unresolved", "insufficient_permissions")
SETTLED = ("completed", "not_started")
VERDICT_TEXT = {"completed": "COMPLETED: it took effect; do not repeat it",
                "not_started": "NOT STARTED: it did not take effect; perform it only if still required after recover"}
PERMISSION_RE = re.compile(r"(?i)permission denied|could not read username|authentication failed|gh auth login|"
                           r"not logged in|requires authentication|resource not accessible|HTTP 40[13]\b")
PUSH_FLAGS = {"-u", "--set-upstream", "-q", "--quiet", "-v", "--verbose", "--no-verify", "--porcelain", "--atomic",
              "--progress", "--no-progress", "-f", "--force", "--force-if-includes"}
GH_VALUE_OPTS = {  # per subcommand: `gh pr merge -m` is --merge, `gh pr create -m` takes a milestone
    "merge": {"-b", "--body", "-F", "--body-file", "-t", "--subject", "-A", "--author-email", "--match-head-commit",
              "-R", "--repo"},
    "create": {"-b", "--body", "-F", "--body-file", "-t", "--title", "-R", "--repo", "-H", "--head", "-B", "--base",
               "-T", "--template", "-a", "--assignee", "-l", "--label", "-m", "--milestone", "-p", "--project",
               "-r", "--reviewer"},
}
Runner = Callable[..., tuple[int | None, str]]


def final_text(turn_dir: Path) -> str:
    """The agent's last top-level reply: Codex's `--output-last-message` file, else Claude's `result` or
    Codex's last `agent_message` event."""
    try:
        return (turn_dir / "last-message.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    events, _ = read_events(turn_dir / "output.jsonl")
    text = ""
    for e in events:
        if e.get("type") == "result" and not e.get("parent_tool_use_id"):
            text = str(e.get("result") or "")
        elif e.get("type") == "item.completed" and (e.get("item") or {}).get("type") == "agent_message":
            text = str(e["item"].get("text") or "")
    return text


def _unwrap(command: str) -> str:
    for prefix in ("bash -lc ", "bash -c ", "/bin/bash -lc ", "/bin/bash -c ", "sh -c ", "/bin/sh -c "):
        if command.strip().startswith(prefix):
            program, rest = split_first_word(command.strip()[len(prefix):])
            return program if program is not None and not rest.strip() else ""
    return command


def _simple_commands(command: str) -> list[tuple[Path | None, list[str]]] | None:
    """(cd target, words) of each simple command, or None when the command is too dynamic to probe."""
    command = re.sub(r"\s\d*>&\d\b|\s\d*>{1,2}\s*/dev/null\b", " ", _unwrap(command))  # stream redirections only
    if not command or any(ch in command for ch in "$`<>") or "\n" in command.strip():
        return None
    out: list[tuple[Path | None, list[str]]] = []
    cwd: Path | None = None
    for seg in split_segments(command):
        if seg.strip().endswith("&") and not seg.strip().endswith("&&"):
            return None  # a detached job: no probe can show it finished
        try:
            words = shlex.split(seg, comments=True)
        except ValueError:
            return None
        if words[:1] == ["cd"] and len(words) == 2:
            cwd = Path(words[1]) if cwd is None or Path(words[1]).is_absolute() else cwd / words[1]
            if not cwd.is_absolute():
                return None  # relative to an unknown working directory
        elif words:
            out.append((cwd, words))
    return out


def _denied(text: str) -> bool:
    return bool(PERMISSION_RE.search(text or ""))


def _probe_push(cwd: Path | None, args: list[str], run: Runner, repo: Path) -> tuple[str, str] | None:
    """One branch update only. HEAD, the upstream and the current branch are resolved only in a directory the
    command itself named (`cwd`); a named local branch is the same in every worktree."""
    force, positional = False, []
    for a in args:
        if a.startswith("--force-with-lease"):
            force = True
        elif a.startswith("-"):
            if a not in PUSH_FLAGS:
                return None  # --all, --tags, --delete, -o VALUE, --repo=...: not one branch update
            force = force or a in ("-f", "--force")
        else:
            positional.append(a)
    if len(positional) > 2:
        return None
    if len(positional) == 2:
        refspec = positional[1]
        force = force or refspec.startswith("+")
        src, _, dst = refspec.lstrip("+").partition(":")
        remote, dst = positional[0], dst or src
        if not src:
            return None  # `:branch` deletes it
    else:
        if cwd is None:
            return None
        code, upstream = run(["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        if code != 0 or "/" not in upstream.strip():
            return None
        up_remote, _, up_branch = upstream.strip().partition("/")
        remote, src, dst = (positional[0] if positional else up_remote), "HEAD", up_branch
    if src == "HEAD" or dst == "HEAD":
        if cwd is None:
            return None
        code, branch = run(["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"])
        if code != 0 or branch.strip() in ("", "HEAD"):
            return None
        dst = branch.strip() if dst == "HEAD" else dst
    src, dst = src.removeprefix("refs/heads/"), dst.removeprefix("refs/heads/")
    if dst.startswith("refs/") or src.startswith("refs/"):
        return None  # tags, notes and other refs are not probed
    cwd = cwd or repo
    if src != "HEAD" and run(["git", "-C", str(cwd), "show-ref", "--verify", "--quiet", f"refs/heads/{src}"])[0] != 0:
        return None  # not a local branch (a tag, a SHA): not a branch update
    code, local = run(["git", "-C", str(cwd), "rev-parse", "--verify", f"{src}^{{commit}}"])
    if code != 0:
        return None
    local = local.strip()
    code, listed = run(["git", "-C", str(cwd), "ls-remote", remote, f"refs/heads/{dst}"], timeout=60)
    if code != 0:
        return ("insufficient_permissions", f"`git ls-remote {remote}` was refused") if _denied(listed) else None
    remote_sha = listed.split()[0] if listed.split() else ""
    if remote_sha == local:
        return "completed", f"remote branch {dst} is at the pushed commit {local[:12]}"
    if force:
        return None  # a forced update may have replaced someone else's work: not settled by ancestry
    if not remote_sha:
        return "not_started", f"remote branch {dst} does not exist: the push did not take effect"
    code, _ = run(["git", "-C", str(cwd), "merge-base", "--is-ancestor", remote_sha, local])
    if code == 0:
        return "not_started", f"remote branch {dst} ({remote_sha[:12]}) is behind {local[:12]}: the push did not take effect"
    return None


def _gh_parse(args: list[str], value_opts: set[str]) -> tuple[list[str], dict[str, str], set[str]]:
    positional, values, flags, i = [], {}, set(), 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and "=" in a:
            key, value = a.split("=", 1)
            values[key] = value
        elif a in value_opts and i + 1 < len(args):
            values[a] = args[i + 1]
            i += 1
        elif a.startswith("-"):
            flags.add(a)
        else:
            positional.append(a)
        i += 1
    return positional, values, flags


def _probe_gh(cwd: Path | None, args: list[str], run: Runner) -> tuple[str, str] | None:
    """`cwd` is set only when the command itself named an absolute directory; otherwise the agent's working
    directory is unknown (it persists across calls), so nothing branch-relative is resolved."""
    sub = args[1] if len(args) > 1 and args[0] == "pr" else ""
    positional, values, flags = _gh_parse(args, GH_VALUE_OPTS.get(sub, set()))
    repo = values.get("-R") or values.get("--repo")
    repo_args = ["--repo", repo] if repo else []
    if positional[:2] == ["pr", "merge"]:
        if flags & {"--auto", "--disable-auto"} or len(positional) != 3 \
                or not re.fullmatch(r"\d+|https://github\.com/[^/\s]+/[^/\s]+/pull/\d+", positional[2]):
            return None  # auto-merge is armed, not performed; without an explicit PR the target is unknown
        target = positional[2:3]
        code, out = run(["gh", "pr", "view", *target, *repo_args, "--json", "number,state"], cwd=cwd, timeout=60)
        if code != 0:
            return ("insufficient_permissions", "`gh pr view` was refused") if _denied(out) else None
        try:
            pr = json.loads(out)
        except ValueError:
            return None
        if pr.get("state") == "MERGED":
            return "completed", f"PR #{pr.get('number')} is merged"
        if pr.get("state") == "OPEN":
            return "not_started", f"PR #{pr.get('number')} is still open: the merge did not take effect"
        return None
    if positional[:2] == ["pr", "create"] and len(positional) == 2:
        head = values.get("-H") or values.get("--head")
        if head is None:
            if cwd is None:
                return None
            code, branch = run(["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"])
            if code != 0 or branch.strip() in ("", "HEAD"):
                return None
            head = branch.strip()
        code, out = run(["gh", "pr", "list", "--head", head, "--state", "all", *repo_args, "--json", "number,state"],
                        cwd=cwd, timeout=60)
        if code != 0:
            return ("insufficient_permissions", "`gh pr list` was refused") if _denied(out) else None
        try:
            prs = [p for p in json.loads(out) if isinstance(p, dict)]
        except (ValueError, TypeError):
            return None
        live = [p for p in prs if p.get("state") == "OPEN"]  # a merged one may be an older PR from a reused name
        if live:
            return "completed", f"open PR #{live[0].get('number')} exists for branch {head}"
        if not prs:
            return "not_started", f"no PR exists for branch {head}: the creation did not take effect"
        return None
    return None


def _probe_file(probe: dict[str, Any], tool: str) -> tuple[str, str] | None:
    path = Path(probe.get("file_path") or "")
    if not path.is_absolute():
        return None
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else None
    except (OSError, UnicodeDecodeError):
        return None
    if tool == "Write" and "content" in probe:
        if text is None:
            return "not_started", f"{path.name} does not exist: the write did not take effect"
        if text == probe["content"]:
            return "completed", f"{path.name} holds exactly the written content"
        return None
    old, new = probe.get("old_string"), probe.get("new_string")
    if tool != "Edit" or text is None or not old or new is None or old in new or new in old:
        return None  # containment makes presence ambiguous
    if new in text and old not in text:
        return "completed", f"{path.name} contains the edit's replacement text and not the original"
    if old in text and new not in text:
        return "not_started", f"{path.name} still contains the original text: the edit did not take effect"
    return None


def probe_call(call: ToolCall, repo: Path, run: Runner = devctx.run) -> tuple[str, str] | None:
    """Deterministic read-only probe of one in-flight operation: (verdict, evidence), or None when only a
    broader investigation can tell. A command probes only when its one effectful step is the operation."""
    if call.tool in ("Edit", "Write"):
        return _probe_file(call.probe, call.tool)
    commands = _simple_commands(str(call.probe.get("command") or ""))
    if not commands:
        return None
    effectful = [(cwd, words) for cwd, words in commands if classify_command(shlex.join(words))[0] != "read_only"]
    if len(effectful) != 1:
        return None
    cd, words = effectful[0]
    # The agent's working directory persists across calls: only an absolute directory named here is known.
    cwd = cd if cd is not None and cd.is_absolute() else None
    if Path(words[0]).name == "git":
        args = words[1:]
        while args[:1] == ["-C"] and len(args) > 1:
            cwd = Path(args[1]) if Path(args[1]).is_absolute() else (cwd / args[1] if cwd else None)
            args = args[2:]
        return _probe_push(cwd, args[1:], run, repo) if args[:1] == ["push"] else None
    if Path(words[0]).name == "gh":
        return _probe_gh(cwd, words[1:], run)
    return None


# ---------------------------------------------------------------------------
# Supervisor

RECOVERY_PROMPT = """[relay-continuity attempt {nonce}]
Automatic continuation by the continuity supervisor. The previous coordinator turn ({prev_agent}) ended with outcome `{outcome}`; you are now running as {agent}.
This message is not a new maintainer instruction and grants no new authority: approval gates, review requirements and authority boundaries are exactly as before.
Before acting, run `python3 scripts/devctx.py recover` and verify fresh Git/GitHub state, then continue the interrupted work from where it actually stands.
Operations the interrupted turn completed (they already happened; verify their effect, do not repeat them blindly):
{effects}
{processes}
An operation listed as INTERRUPTED may have done part of its work before it was stopped. Inspect its effects (files, Git, GitHub, logs) before running it again, and repeat it only when that is safe. If it may already have had an external effect (a network request, a remote API call, a message) that you cannot verify and that must not happen twice, do not repeat it: record a `devctx.py note blocker` and end with RELAY_STATUS blocked.
If the work now needs a maintainer decision, stop and report instead of continuing."""

PROCESSES_STOPPED = ("Nothing the previous turn started is still running, except durable jobs registered with "
                     "`python3 scripts/relay_jobs.py start` (see `relay_jobs.py list`): the supervisor "
                     "stopped every other process it left behind. Restart any server or worker you need yourself.")
PROCESSES_UNKNOWN = ("The previous turn ran outside this supervisor's ownership (before a restart), so processes it "
                     "started may still be running: check them (`ps`, listening ports, worker lanes) before starting a "
                     "server or worker again, and never run a second copy beside a live one.")
MISSION_HEADER = "## Mission task"
MISSION_PROMPT = """[relay-continuity attempt {{nonce}}]
Supervised mission {mission}, task {number}/{total}, queued by the maintainer under directive {authority}.
The task text below is the maintainer's; it grants no authority beyond that directive. Approval gates, review requirements and authority boundaries are unchanged.
Before acting, run `python3 scripts/devctx.py recover` and verify fresh Git/GitHub state: earlier tasks of this mission, or an interrupted attempt at this one, may already have taken effect.
If the task needs a maintainer decision, stop and report instead of continuing.
Long bounded work (solver runs, studies, benchmarks) goes through `python3 scripts/relay_jobs.py start`, which survives this turn; `relay_jobs.py list` shows earlier jobs and their results. Before launching a scientific case, inspect registered jobs and existing result/partial evidence for the same commit and inputs; reuse them and never duplicate a live or completed case without a documented technical reason. A failed/lost job or an uncertain observation after namespace replacement returns to you for diagnosis, not automatically to the maintainer. Unknown/orphaned job records reserve their slot: inspect prior logs, results and partial evidence; do not rerun them or infer success/failure from missing PIDs. Repeating the same uncertain JOB-WAIT cannot trigger repeated diagnostic turns in the same namespace. Never keep a turn open to wait for a job or for CI: checkpoint and end the turn, and a later turn collects. A running job blocks only the work that depends on its result: meanwhile continue with other authorized work that does not depend on it, on its own worktree and files, delegating per docs/PROCESS.md, and keep it light on the machine (focused tests only, full suites to CI, no extra solver beyond the cores the job left free). As a roadmap cycle, end blocked with `JOB-WAIT: <job id>` lines, which pause roadmap mode until those jobs finish, only when no such job-independent authorized work remains.
{contract}
End with RELAY_STATUS propose_done only when that evidence exists, RELAY_STATUS continue after a durable checkpoint (the task stays pending and is resumed, never recorded complete), or RELAY_STATUS blocked after recording a `devctx.py note blocker` or `question`.

""" + MISSION_HEADER + """
{text}"""


def describe_check(check: dict[str, Any] | None) -> str:
    """The completion contract as the provider is told it (spec 204). The owner, not the provider, evaluates it."""
    if check is None:
        return ("Completion contract: this task has no independent completion check, so the owner never records it "
                "complete on your report; it holds for a maintainer-attached contract.")
    what = {"host_receipt": lambda c: f"a signed ok host receipt exists for {c['op']} at {c.get('sha') or 'any commit'}",
            "pr_merged": lambda c: f"PR #{c['pr']} is merged on GitHub",
            "path_exists": lambda c: f"`{c['path']}` exists in the repository",
            "spec_merged": lambda c: f"spec {c['spec']} is `merged` in docs/specs/STATUS.md on GitHub master and every "
                                     "PR linked in its row is merged",
            "coordinator_cycle": lambda c: "the cycle ends with a final RELAY_STATUS while the remote heads are readable; "
                                           "the cycle is recorded settled, not as verified delivery of its work"}
    return f"Completion contract: the owner records this task complete only after its own read-only check that {what[check['kind']](check)}."


WORKER_ROUTING_BLOCK = """Worker routing
{summary}
Classify each delegable task by difficulty and risk, run `python3 scripts/worker_routing.py pick --difficulty D --risk R`, and delegate implementation to the returned worker. Coordinators plan, review and integrate; do not implement delegable work yourself unless pick returns no_worker.
Risk is the harm if the worker is wrong: low docs/tests/UI copy; medium feature code covered by tests; high security, auth, egress, migrations/data loss, supervisor/relay/sandbox code, irreversible operations. Review the returned worker's diff as `pick` requires."""


def worker_routing_block(repo: Path | None = None) -> str:
    """The prompt's worker-routing block. The quota line is cached (<=15 min) and a failed read never blocks or
    fails a turn: it only reads `worker quota: unreadable`. With a repo, a successful reading is also persisted to
    its gitignored `.agent-relay/worker-quota.json` so a coordinator that cannot read quotas live can fall back."""
    try:
        summary = (worker_routing.cached_summary() if repo is None
                   else worker_routing.cached_summary(snapshot_file=repo / worker_routing.SNAPSHOT_NAME))
    except Exception:  # noqa: BLE001 - the turn must not depend on a quota reading
        summary = worker_routing.UNREADABLE
    return WORKER_ROUTING_BLOCK.format(summary=summary)


@dataclass
class Host:
    relay: Any
    now: Callable[[], datetime] = now_utc
    sleep: Callable[[float], None] = time.sleep
    fingerprint: Callable[[], str] = lambda: ""
    conflicts: Callable[[set[int]], list[str]] = lambda own: []
    pid_alive: Callable[[int, int | None], bool] = lambda pid, start: devctx.pid_start(pid) == start and start is not None
    ledger: Callable[[str, str], None] = lambda kind, text: None
    permissions_digest: Callable[[], str | None] = lambda: None
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr, flush=True)
    probe: Callable[[ToolCall], tuple[str, str] | None] = lambda call: None
    reap: Callable[[], list[str]] = lambda: []  # stops what a finished turn left running; returns process names
    task_check: Callable[[dict[str, Any]], str | None] = lambda check: None  # spec 201: None when the check passes
    worker_routing: Callable[[], str] | None = None  # text appended to every launched prompt


class Supervisor:
    def __init__(self, store: Store, host: Host, limits: Limits | None = None, *, turns: int = 1,
                 sender: Callable[[str], str | None] | None = None):
        self.store = store
        self.resume_turn: str | None = None
        self.resumed_hold: dict[str, Any] | None = None
        self.host = host
        self.limits = limits or Limits()
        self.turns = turns
        self.sender = sender
        self.state: dict[str, Any] = default_state()
        self.notifier = Notifier(self.state, sender=sender, log=host.log)
        self.signalled = False
        self.attempts = 0
        self.deciding: str | None = None
        self._reading: dict[str, Any] | None = None
        self._reading_at: datetime | None = None
        self.mission: dict[str, Any] | None = None  # spec 201: the validated mission this store executes

    # -- persistence helpers
    def save(self) -> None:
        self.store.save(self.state, self.host.now())

    def notify(self, event: str, **fields: Any) -> None:
        if "session" in fields and fields["session"] is None:
            fields["session"] = "none"  # a mission before its first launch has no session yet
        try:
            self.notifier.send(event, self.host.now(), **fields)
        except Exception as exc:  # noqa: BLE001 - notification can never interrupt supervision
            self.host.log(f"notify: internal error {type(exc).__name__}")

    def record(self, kind: str, text: str) -> None:
        try:
            self.host.ledger(kind, text)
        except Exception as exc:  # noqa: BLE001
            self.host.log(f"ledger: append failed {type(exc).__name__}")

    def stop_requested(self) -> bool:
        return self.signalled or self.store.stop_path.exists()

    def hold(self, reason: str, attempt: int | None = None, turn: str | None = None) -> str:
        """Refuse automatic continuation. `turn` names the turn whose evidence the maintainer must review; a
        release acknowledges exactly that turn and nothing later."""
        assert reason in HOLD_REASONS
        turn = turn or self.deciding  # holds raised while deciding a turn (waits, circuits) name that turn
        now = self.host.now()
        self.state["phase"] = "held"
        self.state["hold"] = {"reason": reason, "at": iso(now), "attempt": attempt, "turn": turn}
        self.save()
        self.store.journal({"event": "hold", "at": iso(now), "reason": reason, "session": self.state["head_session"]})
        previous = self.resumed_hold or {}
        if previous.get("reason") == reason and previous.get("turn") == turn:
            # Decided again with the same result: kept, not re-announced, and not decided again on every poll.
            self.state["hold"]["resumable"] = False
            self.save()
            self.host.log(f"hold: {reason} (unchanged when decided again; not re-announced)")
            return "held"
        event = "circuit_open" if reason.startswith("circuit_") else "hold"
        fields = {"session": self.state["head_session"], "reason": reason}
        if event == "hold":
            fields["attempt"] = str(attempt or 0)
        self.notify(event, **fields)
        if (rec := self.state.get("mission")) is not None and self.mission is not None:
            self.notify("mission_held", owner="control-plane", mission=self.mission["id"],
                        task=f"{min(rec['next'] + 1, rec['total'])}/{rec['total']}",
                        agent=self.state.get("agent") or PREFERRED, session=self.state["head_session"], reason=reason)
        self.record("blocker", f"relay-continuity HOLD ({reason}) on session {self.state['head_session']}; "
                               f"automatic continuation refused until `relay_continuity.py release`.")
        self.host.log(f"hold: {reason}")
        return "held"

    # -- provider availability
    def reading(self, agent: str) -> dict[str, Any] | None:
        now = self.host.now()
        if self._reading is None or self._reading_at is None or now - self._reading_at > timedelta(minutes=2):
            self._reading = self.host.relay.limits() or {}
            self._reading_at = now
        entry = self._reading.get(agent)
        if not isinstance(entry, dict) or entry.get("status") not in (None, "ok"):
            return None
        as_of = parse_ts(entry.get("as_of"))
        if as_of is None or abs(now - as_of) > self.limits.reading_freshness:
            return None
        return entry

    def exhausted_until(self, agent: str) -> datetime | None | bool:
        """Validated reset time when a fresh reading shows the agent out of quota; False when it shows room."""
        entry = self.reading(agent)
        if entry is None:
            return None
        now = self.host.now()
        resets = []
        windows = entry.get("windows")
        if not isinstance(windows, list) or not windows:
            return None  # no usage windows: no evidence either way
        for window in windows:
            used = window.get("used_percent") if isinstance(window, dict) else None
            if isinstance(used, bool) or not isinstance(used, (int, float)):
                return None  # an unusable window is not evidence of room
            reset = parse_ts(window.get("resets_at"))
            if used >= 99.5:
                if reset and now < reset <= now + self.limits.max_reset_ahead:
                    resets.append(reset)
                else:
                    return None  # exhausted but the reset time is stale or implausible: not evidence
        return max(resets) if resets else False

    def available(self, agent: str) -> bool:
        """Whether `agent` may be tried now. Its own runs are the authority: a cooldown recorded from a real
        failure ends at its reset, and a fresh, plausible limits reading that shows it out of quota defers it to
        that reset. A missing, stale or failed reading is no evidence either way, so the agent is tried and its
        run decides; a quota failure then records the cooldown that keeps retries bounded."""
        now = self.host.now()
        cooldown = self.state["cooldowns"].get(agent)
        if cooldown:
            until = parse_ts(cooldown.get("until"))
            if until is None or now < until:
                return False
        if agent in NATIVE_AGENTS:
            native = self.host.relay.argon_limits(agent)
            if native is None:
                return False
            windows = native["windows"]
            blocked = [w for w in windows if w.get("disabled") or w["used_percent"] >= 100]
            if blocked:
                resets = [parse_ts(w.get("resets_at")) for w in blocked]
                known = [t for t in resets if t is not None and now < t <= now + self.limits.max_reset_ahead]
                self.state["cooldowns"][agent] = {"until": iso(max(known)) if len(known) == len(blocked) else None,
                                                  "source": "limits", "reason": "quota", "since": iso(now)}
                return False
            if cooldown:
                self.state["cooldowns"].pop(agent, None)
            return True
        reading = self.exhausted_until(agent)
        if isinstance(reading, datetime):
            self.state["cooldowns"][agent] = {"until": iso(reading), "source": "limits", "reason": "quota",
                                              "since": iso(now)}
            return False
        if cooldown:
            self.state["cooldowns"].pop(agent, None)
        return True

    def set_cooldown(self, agent: str, report: TurnReport) -> None:
        now = self.host.now()
        existing = self.state["cooldowns"].get(agent)
        if isinstance(existing, dict) and existing.get("until") is None:
            return  # a manual cap stays until `release --clear-cooldown`; no reclassification shortens it
        if isinstance(existing, dict) and report.turn_id and existing.get("turn") == report.turn_id:
            return  # this turn's cooldown is already recorded: a restart re-deciding it neither extends nor backs off
        caps = self.state["spend_caps"]
        earlier = caps.get(agent)
        if report.spend_cap and report.outcome == "quota" and isinstance(earlier, dict) \
                and earlier.get("turn") != report.turn_id:
            # A second turn hit the spend cap after the session reset it printed (no success since): a real cap.
            report.outcome = "spend_limit"
        if report.spend_cap:
            caps[agent] = {"turn": report.turn_id, "at": iso(now)}
        counters = self.state["counters"]
        if report.outcome == "spend_limit":
            until, source = None, "manual"
        elif report.outcome == "transient":
            # An outage: back off this agent briefly (the other one may still be reachable).
            strikes = counters.get(f"transient_{agent}", 0)
            counters[f"transient_{agent}"] = strikes + 1
            until = now + timedelta(seconds=min(self.limits.transient_base * 2 ** strikes, self.limits.transient_cap))
            source = "estimate"
        else:
            reading = self.exhausted_until(agent) if report.outcome == "quota" else None
            message = parse_ts(report.reset_at)
            if isinstance(reading, datetime):
                until, source = reading, "limits"
            elif message and now < message <= now + self.limits.max_reset_ahead:
                until, source = message, "message"
            else:
                # No reliable reset (or a login problem): retry periodically, backing off while the agent keeps
                # failing (reset on success).
                strikes = counters.get(f"estimated_{agent}", 0)
                counters[f"estimated_{agent}"] = strikes + 1
                until, source = now + min(self.limits.recheck * 2 ** strikes, self.limits.recheck_cap), "estimate"
        self.state["cooldowns"][agent] = {"until": iso(until), "source": source, "reason": report.outcome,
                                          "since": iso(now), "turn": report.turn_id}
        if report.outcome == "transient":
            self.host.log(f"{agent} transient failure; retry after {iso(until)}")
            return
        self.notify("provider_exhausted", dedupe_key=f"exhausted|{agent}|{iso(until)}", agent=agent,
                    outcome=report.outcome, reset_at=iso(until) or "manual", reset_source=source)
        self.record("finding", f"relay-continuity: {agent} {report.outcome}; cooldown until "
                               f"{iso(until) or 'maintainer release'} (source {source}).")

    # -- main loop
    def run(self, session: str, *, task: str | None = None) -> str:
        now = self.host.now()
        try:
            self.state = self.store.load()
        except StateCorrupt:
            self.host.log("state file corrupt; refusing to act (hold). Inspect and move it aside to reset.")
            Notifier(default_state(), sender=self.sender, log=self.host.log).send(
                "hold", now, session=session, reason="state_corrupt", attempt="0")
            return "held"
        self.notifier = Notifier(self.state, sender=self.sender, log=self.host.log)
        if self.state["phase"] == "held":
            hold = self.state.get("hold") or {}
            if not (redecidable(self.state) and session == self.state.get("head_session")):
                self.host.log(f"chain is held ({self.state['hold']}); run `release` after review")
                return "held"
            # A hold the current policy may not raise is decided again, for that exact turn only. Every other
            # hold still needs `release`.
            self.resume_turn = hold["turn"]
            self.resumed_hold = hold
            self.store.journal({"event": "hold_redecided", "at": iso(now), "hold": hold})
            self.host.log(f"deciding held turn {hold['turn']} again under the current policy")
            self.state["hold"] = None
        head = self.state.get("head_session")
        if self.state.get("pending") and head and head != session:
            self.host.log(f"a launched attempt from chain {head} is unresolved; resume that chain first")
            return "held"
        if head != session and session not in self.state.get("chain", []):
            self.state.update({"head_session": session, "chain": [session], "agent": self.state.get("agent") if head is None else None, "switches": [],
                               "last_switch_at": None, "waiting_since": None, "last_attempt": None,
                               "counters": {"consecutive_failures": 0, "transient_streak": 0, "crash_retries": 0}})
        if isinstance(legacy := self.state.pop("reconcile", None), dict) and isinstance(legacy.get("diag"), dict):
            # A read-only diagnosis of the earlier controller (it could change nothing): abandoned, never adopted.
            self.store.journal({"event": "settle", "purpose": "diagnostic", "outcome": "abandoned",
                                "nonce": legacy["diag"].get("nonce"), "session": None})
        own = {(self.state.get("pending") or {}).get("nonce")}
        if (unsettled := [n for n in unsettled_nonces(self.store.journal_path) if n not in own]):
            # The journal records a launch that the state does not account for: never assume it did not run.
            self.host.log(f"journal has unsettled attempts not in state: {', '.join(unsettled)}")
            return self.hold("attempt_unresolved")
        digest = self.host.permissions_digest()
        if self.state.get("permissions_pinned") and digest != self.state["permissions_digest"]:
            return self.hold("permissions_changed")
        self.state["permissions_digest"] = digest
        self.state["permissions_pinned"] = True
        self.state["phase"] = "running"
        self.save()
        self.notify("supervisor_started", dedupe_key=f"start|{self.state['head_session']}|{iso(now)[:13]}",
                    session=self.state["head_session"], agent=self.state.get("agent") or PREFERRED)
        self.notifier.retry_pending(now)
        try:
            result = self._loop(task)
        finally:
            if self.state["phase"] == "running":
                self.state["phase"] = "stopped"
            self.save()
        reason = {"held": "held", "idle": "idle", "waiting": "waiting", "stopped": "stop_requested",
                  "signal": "signal"}.get(result, "error")
        self.notify("supervisor_stopped", dedupe_key=f"stop|{self.state['head_session']}|{result}|{iso(self.host.now())[:13]}",
                    session=self.state["head_session"], reason=reason, phase=self.state["phase"])
        return result

    def _loop(self, task: str | None) -> str:
        report = self._resolve_pending()
        acknowledged: dict[str, Any] | None = None
        if report == "held":
            return "held"
        if report is None and self.mission is not None and self._mission_task() is None:
            # Every task is recorded (`release --advance` on the last one): no settled attempt is left to decide,
            # so the mission completes instead of relaunching a task that does not exist.
            return self._mission_complete()
        if report is None and self.mission is not None and not self.state.get("head_session"):
            # A new mission has no turn yet: its first task opens a new Relay session.
            report = TurnReport(outcome="success", agent=None, detail="mission start", terminal=True,
                                turn_id=f"{self.mission['id']}/start")
        if report is None:
            turn = last_turn_dir(self.host.relay.sessions_dir() / self.state["head_session"])
            if turn is None:
                self.host.log("head session has no turns")
                return self.hold("attempt_unresolved")
            report = classify_turn(turn)
            report.agent = report.agent or self.state.get("agent")
            ack = self.state.get("acknowledged") or {}
            if ack.get("turn") and ack.get("turn") == report.turn_id:
                acknowledged = ack
        if self.resume_turn and report.turn_id != self.resume_turn:
            self.host.log("the chain moved since the held turn; keeping the hold (not decided again)")
            self.state["phase"] = "held"
            self.state["hold"] = {**(self.resumed_hold or {}), "resumable": False}
            self.save()
            return "held"
        while True:
            if self.stop_requested():
                self.state["phase"] = "stopped"
                return "signal" if self.signalled else "stopped"
            self.state["last_outcome"] = report.outcome
            self.state["agent"] = report.agent or self.state.get("agent") or PREFERRED
            self.deciding = report.turn_id
            decision = self._decide(report, task, waive=waivers(acknowledged), cleared=tuple((acknowledged or {}).get("cleared") or ()))
            acknowledged = None
            if isinstance(decision, str):
                return decision
            agent, prompt = decision
            task = None
            new_report = self._launch(agent, prompt, report)
            if isinstance(new_report, str):
                return new_report
            report = new_report

    def _decide(self, report: TurnReport, task: str | None, *, waive: frozenset[str] = frozenset(),
                cleared: tuple[str, ...] = ()) -> tuple[str, str] | str:
        """Next (agent, prompt) or a terminal result. `waive` holds what a maintainer release of this turn
        reviewed ("gate": its side-effect evidence). `cleared`: agents whose cooldown the release cleared."""
        released = bool(waive or cleared)
        counters = self.state["counters"]
        failed_agent = report.agent or self.state.get("agent") or PREFERRED
        if report.outcome == "success":
            # A successful turn can still have left a detached job running, now stopped: an external one is gated
            # like any interrupted operation, before anything is advanced or completed.
            if "gate" not in waive and isinstance(found := self._settle_external(report), str):
                return self.hold(found, None, report.turn_id)
            counters.update({"consecutive_failures": 0, "transient_streak": 0, "crash_retries": 0})
            counters.pop(f"estimated_{failed_agent}", None)
            counters.pop(f"transient_{failed_agent}", None)
            self.state["spend_caps"].pop(failed_agent, None)
            self.state["waiting_since"] = None
            if task is None and self.mission is not None:
                self._reopen_legacy()
                self._reverify_released()
                task = self._mission_task()
                if task is None:
                    return self._mission_complete()
            if task is None:
                self.state["phase"] = "idle"
                self.save()
                self.notify("turn_completed", dedupe_key=f"done|{self.state['head_session']}",
                            session=self.state["head_session"], agent=failed_agent)
                return "idle"
            prompt = f"[relay-continuity attempt {{nonce}}]\n{task}"
            if report.reaped:
                prompt += ("\n\nThe supervisor stopped processes the previous turn left running ("
                           + ", ".join(report.reaped) + "); restart any you still need.")
            elif report.reaped is None and report.in_flight:
                prompt += "\n\n" + PROCESSES_UNKNOWN
            agent = self._choose(failed_agent)
            return (agent, prompt) if agent is not None else self._wait_or_hold(lambda _agent: prompt)

        if counters["consecutive_failures"] >= self.limits.max_consecutive_failures:
            return self.hold("circuit_failures")
        settled: dict[str, dict[str, str]] = {}
        if "gate" not in waive:
            reason = gate(report)
            if reason is None:
                found = self._settle_external(report)
                if isinstance(found, str):
                    reason = found
                else:
                    settled = found
            if reason is not None:
                self.notify("turn_failed", session=self.state["head_session"], agent=failed_agent,
                            outcome=report.outcome, action="hold")
                return self.hold(reason, None, report.turn_id)
        if report.outcome in ("quota", "spend_limit", "transient", "auth") and failed_agent not in cleared:
            # A provider failure makes only that provider unavailable, until its reset or a backed-off retry.
            self.set_cooldown(failed_agent, report)

        listed = [f"- [{c.kind}] {c.label}" for c in report.effects]
        for c in report.in_flight:
            verdict = settled.get(c.id)
            listed.append(f"- [{c.kind}] {c.label}: " + (f"{VERDICT_TEXT[verdict['verdict']]} ({verdict['evidence']})"
                                                         if verdict else "INTERRUPTED"))
        effects = "\n".join(listed) or "- none recorded"
        if report.reaped:
            effects += "\nProcesses the turn left running, stopped by the supervisor: " + ", ".join(report.reaped)
        if released:
            effects += "\n(The maintainer reviewed and released this turn.)"

        processes = (PROCESSES_STOPPED if report.reaped is not None else PROCESSES_UNKNOWN)

        def make_prompt(chosen: str) -> str:
            if released and task is not None:
                return f"[relay-continuity attempt {{nonce}}]\n{task}"
            return RECOVERY_PROMPT.format(nonce="{nonce}", prev_agent=failed_agent, outcome=report.outcome,
                                          agent=chosen, effects=effects, processes=processes)

        agent = self._choose(failed_agent)
        if agent is None:
            return self._wait_or_hold(make_prompt)
        self.notify("turn_failed", session=self.state["head_session"], agent=failed_agent, outcome=report.outcome,
                    action="switch" if agent != failed_agent else "retry")
        return agent, make_prompt(agent)

    def _settle_external(self, report: TurnReport) -> dict[str, dict[str, str]] | str:
        """The side-effect gate. The turn's processes are gone, so an operation in flight was interrupted, not
        running. A local one is left to the next turn, which inspects and redoes it. An external one is settled by
        a deterministic read-only probe (a push, a merge, a PR creation); otherwise it holds, because repeating it
        could not be taken back. Returns the probe verdict per settled operation id, or the hold reason."""
        verdicts: dict[str, dict[str, str]] = {}
        uncertain = []
        for op in report.in_flight:
            if not external(op):
                continue
            try:
                probed = self.host.probe(op)
            except Exception as exc:  # noqa: BLE001 - a failed probe settles nothing
                self.host.log(f"probe failed for {op.label}: {type(exc).__name__}")
                probed = None
            if probed and probed[0] in SETTLED:
                verdicts[op.id] = {"verdict": probed[0], "evidence": str(probed[1])[:240]}
            else:
                uncertain.append(op)
        if uncertain or verdicts:
            self.store.journal({"event": "reconcile", "at": iso(self.host.now()), "turn": report.turn_id,
                                "result": "effects_uncertain" if uncertain else "settled",
                                "verdicts": [{"id": op.id, "kind": op.kind, "label": op.label,
                                              **verdicts.get(op.id, {"verdict": "unresolved"})}
                                             for op in report.in_flight if external(op)]})
        if uncertain:
            self.host.log("interrupted external operation(s) no probe can settle: "
                          + ", ".join(op.label for op in uncertain))
            return "effects_uncertain"
        if verdicts:
            self.record("decision", f"relay-continuity: settled interrupted external operation(s) of turn "
                                    f"{report.turn_id} by probe: " + "; ".join(
                                        f"{op.label} = {verdicts[op.id]['verdict']}" for op in report.in_flight
                                        if op.id in verdicts))
        return verdicts

    def _choose(self, current: str) -> str | None:
        """Keep an available preferred coordinator within its mission; use Codex only as emergency fallback."""
        # A completed mission rotates the next mission's seed; fallback Codex never outranks a preferred route.
        order = ([current] + [a for a in PREFERRED_AGENTS if a != current]) if current in PREFERRED_AGENTS else list(PREFERRED_AGENTS)
        for agent in [*order, "codex"]:
            if self.available(agent):
                if agent != current:
                    now = self.host.now()
                    self.state["switches"] = [t for t in self.state["switches"]
                                              if (ts := parse_ts(t)) and now - ts < timedelta(days=1)] + [iso(now)]
                    self.state["last_switch_at"] = iso(now)
                    self.notify("provider_switched", session=self.state["head_session"], from_agent=current,
                                agent=agent, outcome=self.state.get("last_outcome") or "success")
                    self.record("decision", f"relay-continuity: switched coordinator {current} -> {agent} "
                                            f"after {self.state.get('last_outcome')} on {self.state['head_session']}.")
                return agent
        return None

    def _wait_or_hold(self, make_prompt: Callable[[str], str]) -> tuple[str, str] | str:
        """No agent can run now: sleep until the earliest cooldown ends (at most one recheck interval at a time),
        then take whichever agent is usable. Only caps with no reset (spend limits) hold, and so does a wait
        longer than `max_wait`."""
        current = self.state.get("agent") or PREFERRED
        while True:
            now = self.host.now()
            known = [u for a, c in self.state["cooldowns"].items() if a in AGENTS and (u := parse_ts(c.get("until")))]
            if not known:
                return self.hold("no_provider_available")
            since = parse_ts(self.state.get("waiting_since")) or now
            self.state["waiting_since"] = iso(since)
            if now - since > self.limits.max_wait:
                return self.hold("wait_exceeded")
            future = [u for u in known if u > now]
            wake = min(min(future), now + self.limits.recheck) if future else now + self.limits.recheck
            self.state["phase"] = "waiting"
            self.save()
            self.notify("all_exhausted", dedupe_key=f"all|{iso(min(known))}", next_check_at=iso(wake))
            if (rec := self.state.get("mission")) is not None and self.mission is not None:
                self.notify("mission_waiting", dedupe_key=f"wait|{self.mission['id']}|{iso(wake)}",
                            owner="control-plane", mission=self.mission["id"],
                            task=f"{rec['next'] + 1}/{rec['total']}", agent=current,
                            reason="no_provider_available", next_check_at=iso(wake))
            if self._sleep((wake - now).total_seconds()):
                return "signal" if self.signalled else "stopped"
            self.state["phase"] = "running"
            self._reading = None
            if (agent := self._choose(current)) is not None:
                self.state["waiting_since"] = None
                return agent, make_prompt(agent)

    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep in short chunks against the host clock; True when a stop was requested."""
        deadline = self.host.now() + timedelta(seconds=seconds)
        while True:
            if self.stop_requested():
                return True
            remaining = (deadline - self.host.now()).total_seconds()
            if remaining <= 0:
                return False
            self.host.sleep(min(5.0, remaining))

    def _launch(self, agent: str, prompt: str, previous: TurnReport) -> TurnReport | str:
        if self.mission is not None and self._mission_task() is None:
            return self._mission_complete()  # every task is recorded: never launch past the last one
        # Only this process is excluded: a Relay REPL that is our ancestor is still a live coordinator.
        if conflicts := self.host.conflicts({os.getpid()}):
            # Another coordinator is acting on the repository: never a second writer. Nothing was launched, so
            # the same decision is taken again once it has gone.
            self.host.log(f"another coordinator is active ({', '.join(conflicts)}); waiting")
            self.state["phase"] = "waiting"
            self.save()
            return "waiting"
        digest = self.host.permissions_digest()
        if digest != self.state.get("permissions_digest"):
            return self.hold("permissions_changed")
        now = self.host.now()
        self.attempts += 1
        nonce = f"rc-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
        text = prompt.replace("{nonce}", nonce)
        # A takeover starts a new native provider context; the task and durable chain carry recovery.
        # A Claude -> Codex takeover must start a new native Codex context. Relay's --continue otherwise
        # revives its prior thread, which reached 1.5-2.7M input tokens in the measured roadmap cycles.
        # The recovery prompt and mission task carry the handoff; the supervisor keeps its own exact chain.
        parent = (None if agent in NATIVE_AGENTS or self.state.get("agent") in NATIVE_AGENTS
                  or (agent == "codex" and self.state.get("agent") not in (None, "codex"))
                  else self.state["head_session"])
        pending = {"nonce": nonce, "agent": agent, "parent": parent, "started_at": iso(now),
                   "pid": None, "pid_start": None, "fingerprint": self.host.fingerprint(), "attempt": self.attempts}
        if (rec := self.state.get("mission")) is not None and self.mission is not None:
            # Every launch until the task completes (recoveries and failovers included) serves that task.
            pending["task"] = rec["next"]
            if MISSION_HEADER not in text:
                task = self.mission["tasks"][rec["next"]]["text"]
                text += (f"\n\nThe interrupted work is mission {self.mission['id']} task {rec['next'] + 1}/{rec['total']}, "
                         f"which has not been recorded complete:\n{MISSION_HEADER}\n{task}")
        if self.host.worker_routing is not None:
            try:
                text += "\n\n" + self.host.worker_routing()
            except Exception as exc:  # noqa: BLE001 - routing advice is optional; the turn still launches
                self.host.log(f"worker routing block skipped: {type(exc).__name__}")
        self.state["pending"] = pending  # intent is durable before anything is launched
        self.state["acknowledged"] = None  # the reviewed turn is now behind the chain
        self.state["agent"] = agent
        self.save()
        self.store.journal({"event": "launch", **{k: v for k, v in pending.items() if k != "fingerprint"}})
        try:
            proc = self.host.relay.start(agent, parent, text, self.turns)
        except OSError:
            # Relay could not be executed at all: nothing started, so the attempt settles as not started.
            self.state["pending"] = None
            self.store.journal({"event": "settle", "nonce": nonce, "session": None, "outcome": "not_started"})
            return self.hold("relay_invocation_failed", self.attempts)
        pending["pid"] = getattr(proc, "pid", None)
        pending["pid_start"] = devctx.pid_start(pending["pid"]) if pending["pid"] else None
        self.save()
        try:
            result = self.host.relay.finish(proc)
            reaped = self._reap()
        finally:
            self.host.relay.release_turn(proc)
        if reaped is None:
            # Stopped while a process of the turn still survived: the attempt stays pending, and a restart adopts it.
            return "signal" if self.signalled else "stopped"
        return self._settle(pending, result.session_id, result.diagnostics, reaped)

    def _reap(self) -> list[str] | None:
        """Stop whatever the finished turn left running, so nothing of it overlaps the next turn. While a process
        survives, nothing is launched: retry every 30 s. None when a stop request ends that wait."""
        names: set[str] = set()
        while True:
            try:
                names.update(self.host.reap())
                break
            except Exception as exc:  # noqa: BLE001 - a failed cleanup is never taken as a clean one
                self.host.log(f"processes of the finished turn survive ({type(exc).__name__}: {exc}); "
                              "waiting before anything is launched")
                self.state["phase"] = "waiting"
                self.save()
                if self._sleep(30.0):
                    return None
                self.state["phase"] = "running"
        if names:
            self.host.log(f"stopped processes the turn left running: {', '.join(sorted(names))}")
        return sorted(names)

    def _settle(self, pending: dict[str, Any], session_id: str | None, diagnostics: str = "",
                reaped: list[str] | None = None) -> TurnReport | str:
        session_id = session_id or self.host.relay.find_by_nonce(pending["nonce"], parse_ts(pending["started_at"]) or self.host.now())
        self.state["last_attempt"] = {k: pending[k] for k in ("nonce", "agent", "attempt", "fingerprint")}
        if "task" in pending:
            self.state["last_attempt"]["task"] = pending["task"]
        self.state["pending"] = None
        if session_id is None:
            # Remote effects (a merge, a PR comment) leave no local trace, so an unchanged fingerprint proves
            # nothing: the attempt's outcome is unknown and only the maintainer can resolve it. The hold is
            # saved together with the cleared pending record, and only then is the attempt journalled settled.
            result = self.hold("attempt_unresolved", pending.get("attempt"))
            self.store.journal({"event": "settle", "nonce": pending["nonce"], "session": None, "outcome": "unresolved"})
            return result
        turn = last_turn_dir(self.host.relay.sessions_dir() / session_id)
        report = classify_turn(turn, diagnostics=diagnostics) if turn else TurnReport(
            outcome="crash", agent=pending["agent"], detail="session without turns", no_evidence=True,
            turn_id=f"{session_id}/none")
        report.agent = pending["agent"]
        report.reaped = reaped
        self.state["head_session"] = session_id
        self.state["chain"] = (self.state.get("chain") or []) + [session_id]
        # Counted once per launched attempt. Provider failures are bounded by their cooldowns instead.
        counters = self.state["counters"]
        # Only an unbroken run of crash/unknown turns counts: any other outcome ends the streak.
        counters["consecutive_failures"] = (counters["consecutive_failures"] + 1
                                            if report.outcome in ("crash", "unknown") else 0)
        self.store.journal({"event": "settle", "nonce": pending["nonce"], "session": session_id,
                            "outcome": report.outcome, "agent": report.agent, "reaped": report.reaped,
                            "effects": [c.label for c in report.effects], "in_flight": [c.label for c in report.in_flight]})
        # Keep the launched intent durable until mission progress is saved. If this process dies while
        # checking a successful task, restart adopts the turn instead of launching that task again.
        if (held := self._advance(pending, report, session_id)) is not None:
            return held
        self.save()
        return report

    # -- missions (spec 201)
    def _mission_task(self) -> str | None:
        """The prompt of the mission's next incomplete task, or None when every task is recorded complete."""
        assert self.mission is not None
        rec = self.state["mission"]
        if rec["next"] >= rec["total"]:
            return None
        return MISSION_PROMPT.format(mission=self.mission["id"], number=rec["next"] + 1, total=rec["total"],
                                     authority=self.mission["authority"], text=self.mission["tasks"][rec["next"]]["text"],
                                     contract=describe_check(self._task_check(rec["next"])))

    def _task_check(self, index: int) -> dict[str, Any] | None:
        """The task's completion contract: its immutable definition's, else one attached to a legacy task (spec 204)."""
        assert self.mission is not None
        return self.mission["tasks"][index].get("check") or (self.state["mission"].get("contracts") or {}).get(str(index))

    def _settles(self, index: int, status: str | None) -> bool:
        """Whether a final RELAY_STATUS claims the task finished: propose_done; a roadmap cycle also settles blocked."""
        cycle = (self._task_check(index) or {}).get("kind") == "coordinator_cycle"
        return status == "propose_done" or (cycle and status == "blocked")

    def _advance(self, pending: dict[str, Any], report: TurnReport, session_id: str) -> str | None:
        """Spec 204: a successful provider turn is execution, not delivery. Its final RELAY_STATUS only routes it:
        `blocked` holds for the maintainer, and a claimed finish advances only after the task's independent
        completion check passes. Anything else (`continue`, a missing or unknown status, a claim the check does not
        confirm) is a durable checkpoint: the task stays pending and is resumed, a bounded number of times."""
        rec = self.state.get("mission")
        if self.mission is None or rec is None or report.outcome != "success" or pending.get("task") != rec["next"]:
            return None
        index = rec["next"]
        status = final_relay_status(self.host.relay.repo, session_id)
        event = {"at": iso(self.host.now()), "mission": self.mission["id"], "task": index, "session": session_id}
        if status == "blocked" and not self._settles(index, status):
            self.store.journal({"event": "task_blocked", **event})
            return self.hold("task_blocked", pending.get("attempt"), report.turn_id)
        if self._settles(index, status):
            if self._task_check(index) is None:
                # Nothing can confirm the claim until the maintainer attaches a completion contract (spec 204).
                self.store.journal({"event": "task_check_failed", **event, "problem": "no completion check"})
                return self.hold("task_check_failed", pending.get("attempt"), report.turn_id)
            if not (problem := self._verify(index)):
                self._record_done(index, session_id, report.agent, status)
                return None
            self.store.journal({"event": "task_check_failed", **event, "problem": problem[:240]})
        elif status != "continue":
            self.store.journal({"event": "task_report_unverified", **event, "reported_status": str(status)[:40]})
        counts = rec.setdefault("checkpoints", {})
        counts[str(index)] = counts.get(str(index), 0) + 1
        self.store.journal({"event": "task_checkpointed", **event, "checkpoints": counts[str(index)]})
        if counts[str(index)] >= MAX_TASK_CHECKPOINTS:
            return self.hold("task_checkpoint_limit", pending.get("attempt"), report.turn_id)
        return None

    def _verify(self, index: int) -> str | None:
        """The problem that keeps the task's completion contract unsatisfied, or None. Unreadable evidence fails."""
        check = self._task_check(index)
        if check is None:
            problem = "task has no independent completion check"
        else:
            try:
                problem = self.host.task_check(check)
            except Exception as exc:  # noqa: BLE001 - an unverifiable check is not a passed one
                problem = f"check unverifiable ({type(exc).__name__})"
        if problem:
            assert self.mission is not None
            self.host.log(f"mission {self.mission['id']} task {index + 1}: check not satisfied: {problem}")
        return problem

    def _record_done(self, index: int, session_id: str | None, agent: str | None, status: str | None) -> None:
        assert self.mission is not None
        rec = self.state["mission"]
        check = self._task_check(index)
        outcome = "cycle_settled" if (check or {}).get("kind") == "coordinator_cycle" else "verified"
        now = self.host.now()
        rec["next"] = index + 1
        rec["done"].append({"task": index, "session": session_id, "agent": agent, "at": iso(now), "outcome": outcome,
                            "check": check, "status": status})
        self.save()
        self.store.journal({"event": "task_completed", "at": iso(now), "mission": self.mission["id"], "task": index,
                            "session": session_id, "agent": agent, "outcome": outcome, "check": check})
        self.notify("task_completed", mission=self.mission["id"], task=f"{index + 1}/{rec['total']}",
                    agent=agent, session=session_id)
        self.record("finding", f"relay-continuity: mission {self.mission['id']} task {index + 1}/{rec['total']} "
                               f"{outcome} ({check['kind'] if check else 'no check'}) after {agent} in {session_id}.")
        self.host.log(f"mission {self.mission['id']}: task {index + 1}/{rec['total']} {outcome}")

    def _reopen_legacy(self) -> None:
        """The spec 204 migration, also after a restart settled a pending attempt (which `serve_step` defers it for):
        no task is chosen and no mission completes while a pre-204 completion is still counted."""
        if not self.state.get("pending") and (reopened := reopen_unverified(self.state)):
            assert self.mission is not None
            self.save()
            self.store.journal({"event": "mission_reopened", "at": iso(self.host.now()), "mission": self.mission["id"],
                                "tasks": reopened, "why": "counted complete before spec 204 on a provider return alone"})
            self.host.log(f"mission {self.mission['id']}: reopened unverified task(s) {', '.join(str(t + 1) for t in reopened)}")

    def _reverify_released(self) -> None:
        """After a release, a task whose last attempt already claimed it finished is re-checked before any relaunch:
        a contract attached since, or a deliverable merged by hand, completes it without duplicating the work."""
        assert self.mission is not None
        rec, last = self.state["mission"], self.state.get("last_attempt") or {}
        session = self.state.get("head_session")
        if rec["next"] >= rec["total"] or last.get("task") != rec["next"] or not session:
            return
        status = final_relay_status(self.host.relay.repo, session)
        if not self._settles(rec["next"], status) or self._verify(rec["next"]):
            return  # the task is attempted again; its next claimed finish is checked as usual
        self._record_done(rec["next"], session, last.get("agent"), status)

    def _mission_complete(self) -> str:
        assert self.mission is not None
        rec = self.state["mission"]
        now = self.host.now()
        outcomes = [task_outcome(d) for d in rec["done"]]
        rec["completed_at"] = rec.get("completed_at") or iso(now)
        self.state["phase"] = "idle"
        self.save()
        self.store.journal({"event": "mission_completed", "at": iso(now), "mission": self.mission["id"],
                            "outcomes": outcomes})
        self.notify("mission_completed", mission=self.mission["id"], task=f"{rec['next']}/{rec['total']}",
                    session=self.state["head_session"])
        self.record("decision", f"relay-continuity: mission {self.mission['id']} completed "
                                f"({rec['total']} task(s): {', '.join(outcomes)}; last session {self.state['head_session']}).")
        self.host.log(f"mission {self.mission['id']} completed")
        return "idle"

    def _resolve_pending(self) -> TurnReport | str | None:
        """Restart recovery: settle a launched attempt whose outcome was never recorded."""
        pending = self.state.get("pending")
        if not pending:
            return None
        self.host.log(f"resolving unsettled attempt {pending['nonce']}")
        if pending.get("pid") and self.host.pid_alive(pending["pid"], pending.get("pid_start")):
            self.host.log(f"attempt still running as pid {pending['pid']}; waiting for it (never relaunched)")
            while self.host.pid_alive(pending["pid"], pending.get("pid_start")):
                if self._sleep(30.0):
                    return "signal" if self.signalled else "stopped"
        session_id = self.host.relay.find_by_nonce(pending["nonce"], parse_ts(pending["started_at"]) or self.host.now())
        # An adopted run was started by an earlier owner: what it left behind is not this process's to stop.
        return self._settle(pending, session_id)


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


def control_root() -> Path:
    return Path(os.environ.get("RELAY_CONTINUITY_DIR", DEFAULT_CONTROL)).expanduser()


def repo_root() -> Path:
    code, out = devctx.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"])
    return Path(out.strip()).parent if code == 0 and out.strip() else Path.cwd()


def permissions_digest(path: Path) -> str | None:
    """Digest of the authority-bearing Relay permission settings. Relay rewrites `model` whenever `--model`
    is passed; a model choice grants no authority, so it is excluded. An unreadable file digests as such."""
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "unreadable"
    for section in data.values():
        if isinstance(section, dict) and isinstance(section.get("backend"), dict):
            section["backend"].pop("model", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def make_host(repo: Path, relay_env: dict[str, str] | None = None) -> Host:
    env = devctx.default_env(repo)
    author = f"relay-continuity pid {os.getpid()}"

    def ledger(kind: str, text: str) -> None:
        devctx.append_entry(env, devctx.make_entry(env, kind, text, refs={"spec": "195"}, author=author))

    def permissions() -> str | None:
        return permissions_digest(repo / ".agent-relay" / "permissions.toml")

    return Host(relay=RelayAdapter(repo, env=relay_env), fingerprint=lambda: fingerprint(repo),
                conflicts=lambda own: live_conflicts(repo, own), ledger=ledger, permissions_digest=permissions,
                probe=lambda call: probe_call(call, repo),
                reap=lambda: reap_descendants(keep=relay_jobs.live_roots),
                task_check=lambda check: check_task(repo, check), worker_routing=lambda: worker_routing_block(repo))


# ---------------------------------------------------------------------------
# Missions (spec 201): durable submission and a single persistent owner


CHECK_KINDS = ("host_receipt", "pr_merged", "path_exists", "spec_merged", "coordinator_cycle")


def validate_mission(data: Any) -> str | None:
    """Problem with a mission definition, or None. Definitions are immutable once submitted."""
    if not isinstance(data, dict):
        return "not an object"
    if data.get("schema") != MISSION_SCHEMA or not MISSION_ID_RE.fullmatch(str(data.get("id"))):
        return "unknown schema or id"
    if not DIRECTIVE_ID_RE.fullmatch(str(data.get("authority"))):
        return "authority must be a maintainer directive ledger id"
    if parse_ts(data.get("submitted_at")) is None:
        return "bad submitted_at"
    if data.get("session") is not None and not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", str(data["session"])):
        return "bad session"
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        return f"tasks must be a list of 1-{MAX_TASKS} entries"
    for n, task in enumerate(tasks, 1):
        if not isinstance(task, dict) or set(task) - {"text", "check"}:
            return f"task {n}: only text and check are allowed"
        text = task.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TASK_CHARS:
            return f"task {n}: text must be 1-{MAX_TASK_CHARS} characters"
        if devctx.find_secret(text):
            return f"task {n}: text matches a secret pattern"
        if task.get("check") is not None and (problem := validate_check(task["check"])):
            return f"task {n}: {problem}"
    return None


def validate_check(check: Any) -> str | None:
    """Problem with one completion contract, or None (spec 204)."""
    if not isinstance(check, dict) or check.get("kind") not in CHECK_KINDS:
        return f"check kind must be one of {', '.join(CHECK_KINDS)}"
    if check["kind"] == "host_receipt":
        if (set(check) - {"kind", "op", "sha"} or not isinstance(check.get("op"), str)
                or check["op"] not in host_ops.OPERATIONS):
            return "host_receipt takes a known op and optional full sha"
        if "sha" in check and (not isinstance(check["sha"], str) or not host_ops.SHA_RE.fullmatch(check["sha"])):
            return "host_receipt sha must be a full commit id"
    if check["kind"] == "pr_merged" and (set(check) != {"kind", "pr"} or not isinstance(check["pr"], int)
                                         or isinstance(check["pr"], bool) or check["pr"] <= 0):
        return "pr_merged takes a positive integer pr"
    if check["kind"] == "path_exists":
        path = check.get("path")
        if set(check) != {"kind", "path"} or not isinstance(path, str) or not path or Path(path).is_absolute() \
                or ".." in Path(path).parts:
            return "path_exists takes a relative repository path"
    if check["kind"] == "spec_merged" and (set(check) != {"kind", "spec"} or not isinstance(check["spec"], str)
                                           or not SPEC_ID_RE.fullmatch(check["spec"])):
        return "spec_merged takes a spec id such as \"204\""
    if check["kind"] == "coordinator_cycle" and set(check) != {"kind"}:
        return "coordinator_cycle takes no fields"
    return None


def task_outcome(done: dict[str, Any]) -> str:
    """How a recorded task completion is known (spec 204). Entries written before 204 carry no outcome: they are
    a successful provider return, never verified delivery."""
    return done.get("outcome") or ("maintainer_attested" if done.get("by") == "maintainer" else "legacy_unverified")


def reopen_unverified(state: dict[str, Any]) -> list[int]:
    """Spec 204 migration of an unfinished mission: tasks the pre-204 owner counted complete on a successful
    provider return alone are reopened, from the first such task on, so they are resumed and verified rather than
    skipped. The history is kept under `reopened`; definitions and finished missions are never touched."""
    rec = state.get("mission") or {}
    if rec.get("completed_at") or rec.get("cancelled_at"):
        return []
    legacy = [d["task"] for d in rec.get("done") or [] if task_outcome(d) == "legacy_unverified"]
    if not legacy:
        return []
    first = min(legacy)
    rec["reopened"] = (rec.get("reopened") or []) + [d for d in rec["done"] if d["task"] >= first]
    rec["done"] = [d for d in rec["done"] if d["task"] < first]
    rec["next"] = first
    return sorted(d["task"] for d in rec["reopened"] if d["task"] >= first)


def missions_dir(root: Path) -> Path:
    return root / "missions"


def mission_store(root: Path, mission_id: str) -> Store:
    store = Store(missions_dir(root) / mission_id)
    store.stop_path = root / "STOP"  # one stop request stops the owner, whichever mission it executes
    return store


def load_missions(root: Path) -> list[tuple[dict[str, Any] | None, str, str | None]]:
    """(definition or None, id, problem) for every submitted mission, in submission (FIFO) order."""
    found = []
    for path in sorted(missions_dir(root).glob("m-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            found.append((None, path.stem, f"unreadable ({type(exc).__name__})"))
            continue
        problem = validate_mission(data) or (None if data.get("id") == path.stem else "id does not match file name")
        found.append((None if problem else data, path.stem, problem))
    return sorted(found, key=lambda m: ((m[0] or {}).get("submitted_at") or "", m[1]))


def redecidable(state: dict[str, Any] | None) -> bool:
    """A hold the current policy may not raise, not yet decided again: the supervisor decides its turn once more."""
    hold = (state or {}).get("hold") or {}
    return (hold.get("reason") in REDECIDED_HOLDS and bool(hold.get("turn")) and hold.get("resumable", True)
            and bool((state or {}).get("head_session")))


def mission_status(store: Store) -> tuple[str, dict[str, Any] | None]:
    """queued | active | waiting | held | complete | cancelled | corrupt, with the store's state."""
    if not store.state_path.exists():
        return "queued", None
    try:
        state = store.load()
    except StateCorrupt:
        return "corrupt", None
    rec = state.get("mission") or {}
    if rec.get("cancelled_at"):
        return "cancelled", state
    if rec.get("completed_at"):
        return "complete", state
    if state["phase"] == "held":
        return "held", state
    if state["phase"] == "waiting":
        return "waiting", state
    return ("active" if state.get("chain") or state.get("pending") else "queued"), state


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per write: an owner in a fresh PID namespace reuses its PID, so a temp file left by a killed write
    # must never collide with the retry.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    with open(tmp, "x", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def in_sandbox() -> bool:
    """The certified jarvis-sandbox boundary (spec 201 D1): its hostname, and no Windows or WSL interop paths."""
    return socket.gethostname() == SANDBOX_HOSTNAME and not Path("/mnt/c").exists() and not Path("/run/WSL").exists()


def check_task(repo: Path, check: dict[str, Any], run: Runner = devctx.run) -> str | None:
    """Deterministic, read-only completion check of a mission task. None: satisfied."""
    if check["kind"] == "host_receipt":
        return (None if host_ops.find_ok_receipt(host_ops.Paths(Path.home()), check["op"], check.get("sha"))
                else "no verified ok host receipt")
    if check["kind"] == "path_exists":
        target = (repo / check["path"]).resolve()
        if repo.resolve() not in target.parents and target != repo.resolve():
            return "path escapes the repository"
        return None if target.exists() else f"{check['path']} does not exist"
    if check["kind"] == "pr_merged":
        code, out = run(["gh", "pr", "view", str(check["pr"]), "--json", "state", "-q", ".state"], cwd=repo, timeout=60)
        if code != 0:
            return f"gh pr view {check['pr']} failed"
        return None if out.strip() == "MERGED" else f"PR #{check['pr']} is {out.strip() or 'unknown'}"
    if check["kind"] == "coordinator_cycle":
        code, out = run(["git", "ls-remote", "--heads", "origin"], cwd=repo, timeout=60)
        return None if code == 0 and out.strip() else "remote heads unreadable"
    if check["kind"] == "spec_merged":
        # The registry on GitHub master, not a local checkout a provider may have edited.
        code, out = run(["gh", "api", "-H", "Accept: application/vnd.github.raw",
                         "repos/{owner}/{repo}/contents/docs/specs/STATUS.md?ref=master"], cwd=repo, timeout=60)
        if code != 0 or not out.strip():
            return "STATUS.md on GitHub master is unreadable"
        rows = [cells for line in out.splitlines() if line.startswith("|")
                and len(cells := [c.strip() for c in line.strip().strip("|").split("|")]) >= 3 and cells[0] == check["spec"]]
        if len(rows) != 1:
            return f"spec {check['spec']} has {len(rows)} STATUS.md rows"
        if rows[0][1] != "merged":
            return f"spec {check['spec']} is {rows[0][1] or 'unknown'} in STATUS.md"
        prs = sorted({int(n) for n in re.findall(r"\[#(\d+)\]", rows[0][2])})
        if not prs:
            return f"spec {check['spec']} links no PR in STATUS.md"
        for pr in prs:
            if problem := check_task(repo, {"kind": "pr_merged", "pr": pr}, run):
                return problem
        return None
    return f"unknown check {check.get('kind')}"


def webhook_sender() -> Callable[[str], str | None] | None:
    if not WEBHOOK_FILE.exists():
        return None
    mode = WEBHOOK_FILE.stat().st_mode & 0o077
    if mode:
        print("refusing to use the webhook file: it must not be group/world readable (chmod 600)", file=sys.stderr)
        return None
    return lambda text: post_webhook(text)


def command_run(args: argparse.Namespace) -> int:
    root = control_root()
    lock = Lock(root / "supervisor.lock")
    if not lock.acquire():
        print("another continuity supervisor holds the lock; refusing to start", file=sys.stderr)
        return 3
    repo = repo_root()
    try:
        conflicts = live_conflicts(repo, {os.getpid()})
        if "legacy_autopilot" in conflicts:
            print("legacy autopilot scripts are running; stop them first (no concurrent supervisors)", file=sys.stderr)
            return 3
        task = None
        if args.task_file:
            task = Path(args.task_file).read_text(encoding="utf-8")
            if devctx.find_secret(task):
                print("task file matches a secret pattern; refusing", file=sys.stderr)
                return 2
        if not become_subreaper():
            print("cannot own the processes a turn leaves behind (PR_SET_CHILD_SUBREAPER failed); refusing", file=sys.stderr)
            return 2
        store = Store(root)
        store.stop_path.unlink(missing_ok=True)
        supervisor = Supervisor(store, make_host(repo), turns=args.turns,
                                sender=None if args.no_notify else webhook_sender())

        def on_signal(signum: int, _frame: Any) -> None:
            supervisor.signalled = True
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        result = supervisor.run(args.session, task=task)
        print(json.dumps({"result": result, "head_session": supervisor.state.get("head_session"),
                          "phase": supervisor.state.get("phase"), "hold": supervisor.state.get("hold")}))
        return {"idle": 0, "stopped": 0, "signal": 0, "waiting": 0, "held": 4}.get(result, 1)
    finally:
        lock.release()


def init_mission_state(store: Store, mission: dict[str, Any], state: dict[str, Any] | None,
                       now: Callable[[], datetime]) -> dict[str, Any]:
    """The mission's durable progress record, created (and journalled started) the first time it is needed."""
    if state is None or state.get("mission") is None:
        state = state or default_state()
        state["mission"] = {"id": mission["id"], "next": 0, "total": len(mission["tasks"]), "done": []}
        store.save(state, now())
        store.journal({"event": "mission_started", "at": iso(now()), "mission": mission["id"],
                       "authority": mission["authority"], "tasks": len(mission["tasks"])})
    return state


def serve_step(root: Path, factory: Callable[[Store, dict[str, Any]], Supervisor], *,
               conflicts: Callable[[], list[str]], now: Callable[[], datetime] = now_utc,
               log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr, flush=True)) -> str:
    """One scheduling decision of the persistent owner. Missions run strictly one at a time in submission
    order: a held or corrupt mission blocks the ones behind it rather than being skipped. Returns `busy`,
    `empty`, `held:<id>`, `corrupt:<id>`, `waiting:<id>:<reason>` or `ran:<id>:<supervisor result>`."""
    lock = Lock(root / "supervisor.lock")
    if not lock.acquire():
        return "busy"  # a `run`, `release` or `cancel` holds it; look again later
    try:
        last_completed_agent = None
        for mission, mission_id, problem in load_missions(root):
            if mission is None:
                log(f"mission {mission_id}: definition invalid ({problem}); queue held")
                return f"corrupt:{mission_id}"
            store = mission_store(root, mission_id)
            status, state = mission_status(store)
            if status in ("complete", "cancelled"):
                if status == "complete" and state:
                    last_completed_agent = state.get("agent")
                continue
            if status == "corrupt" or (status == "held" and not redecidable(state)):
                return f"{status}:{mission_id}"
            if state is not None:
                rec = state.get("mission")
                if not isinstance(rec, dict) or rec.get("id") != mission_id or rec.get("total") != len(mission["tasks"]):
                    log(f"mission {mission_id}: state does not match its definition; queue held")
                    return f"corrupt:{mission_id}"
            if found := conflicts():
                # A live REPL or legacy autopilot on the repository: never a concurrent writer, so wait.
                return f"waiting:{mission_id}:{'repl_active' if 'repl_active' in found else 'legacy_autopilot'}"
            if state is None and last_completed_agent:
                state = default_state()
                state["agent"] = (PREFERRED_AGENTS[(PREFERRED_AGENTS.index(last_completed_agent) + 1) % len(PREFERRED_AGENTS)]
                                  if last_completed_agent in PREFERRED_AGENTS else PREFERRED)
            state = init_mission_state(store, mission, state, now)
            if not state.get("pending") and (reopened := reopen_unverified(state)):
                store.save(state, now())
                store.journal({"event": "mission_reopened", "at": iso(now()), "mission": mission_id, "tasks": reopened,
                               "why": "counted complete before spec 204 on a provider return alone"})
                log(f"mission {mission_id}: reopened unverified task(s) {', '.join(str(t + 1) for t in reopened)}")
            supervisor = factory(store, mission)
            supervisor.mission = mission
            result = supervisor.run(state.get("head_session") or mission.get("session"))
            return f"ran:{mission_id}:{result}"
        return "empty"
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Roadmap continuation (spec 203): the owner keeps one coordinator cycle queued; the turn chooses the work

ROADMAP_NO_PROGRESS = 1  # a ledger-only cycle must not launch another model turn
ROADMAP_LOCK_WAIT = 120.0  # seconds `roadmap start|stop` waits for an owner step (bounded by its ls-remote)
ROADMAP_TASK = (
    "Roadmap continuation cycle {cycle}. Act as the coordinator described in docs/PROCESS.md for this repository. "
    "Fresh GitHub state and docs/specs/STATUS.md are the backlog; the maintainer directives and holds shown by `devctx.py recover` bind you. "
    "Recover existing PRs, worktrees and claims before creating new ones. Choose the highest-value authorized work "
    "and drive it to a verified, merged and reconciled result, or to a durable checkpoint (a pushed branch or PR "
    "and a ledger note) when it cannot finish in this turn. A running registered job never idles the cycle: while it runs, take job-independent authorized work. Run `devctx.py recover` once at cycle start (never in "
    "parallel) and start from its Last cycle handoff; before ending, record a `devctx.py note` naming the head, "
    "remaining items and next step. Do not submit missions or change roadmap mode: the "
    "mission owner schedules the next cycle. End with RELAY_STATUS propose_done when this cycle's work is merged "
    "or checkpointed. End with RELAY_STATUS blocked, after recording a `devctx.py note question` or `blocker`, "
    "only when no authorized useful work remains across the relevant lanes. A human-dependent step blocks only "
    "its dependent work: record the question and continue safe disjoint authorized work under docs/PROCESS.md, "
    "including owed live activation and investigation/planning/readiness for the "
    "maintainer's already-directed next goal. Planned product implementation still requires explicit readiness. "
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


RELAY_STATUS_LINE = re.compile(r"^RELAY_STATUS:\s*(\{.*\})\s*$", re.MULTILINE)
RELAY_STATUSES = ("continue", "propose_done", "blocked")


def reply_relay_status(text: str) -> str | None:
    """The status of the last well-formed `RELAY_STATUS: {...}` line of the agent's own final reply, or None."""
    for raw in reversed(RELAY_STATUS_LINE.findall(text)):
        try:
            status = json.loads(raw).get("status")
        except (json.JSONDecodeError, AttributeError):
            continue
        if status in RELAY_STATUSES:
            return status
    return None


def final_relay_status(repo: Path, session: str | None) -> str | None:
    """RELAY_STATUS the coordinator reported at the end of a session (continue | propose_done | blocked).
    The coordinator's own final line wins: Agent Relay 0.7.7 replaces the recorded status with `continue` whenever
    certain credential or connectivity words appear anywhere in a turn's stream, tool output included, so a
    finished task was relaunched (cycle 87, ledger L20261025085406-61ab). Relay's recorded status is only the
    fallback when the reply carries no such line."""
    turn = last_turn_dir(repo / ".agent-relay" / "sessions" / session) if session else None
    if turn is None:
        return None
    if (status := reply_relay_status(final_text(turn))) is not None:
        return status
    try:
        return json.loads((turn / "state.json").read_text()).get("status")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def roadmap_lock(root: Path, wait: float = 0.0) -> Lock | None:
    """Serializes every read-modify-write of roadmap.json (owner step, start, stop). None: still busy after `wait`."""
    lock = Lock(root / "roadmap.lock")
    deadline = time.monotonic() + wait
    while not lock.acquire():
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)
    return lock


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
        elif road["paused"] == "no_progress":
            heads = fingerprint()
            if heads is None:
                return "roadmap:waiting:remote_unreadable"
            job_states = roadmap_job_states(root)
            wake = heads != road.get("fingerprint") or job_states != road.get("job_states")
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
            road.update(paused=None, paused_at=None, no_progress=0, fingerprint=heads,
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

    def log(msg: str) -> None:
        print(f"{iso(now_utc())} {msg}", file=sys.stderr, flush=True)

    def factory(store: Store, mission: dict[str, Any]) -> Supervisor:
        supervisor = Supervisor(store, make_host(repo, relay_env), turns=args.turns, sender=sender)
        signal.signal(signal.SIGTERM, lambda *_: (stop.update(flag=True), setattr(supervisor, "signalled", True)))
        signal.signal(signal.SIGINT, lambda *_: (stop.update(flag=True), setattr(supervisor, "signalled", True)))
        return supervisor

    log(f"mission owner serving {missions_dir(root)} (pid {os.getpid()})")
    previous = None
    try:
        while not stop["flag"] and not (root / "STOP").exists():
            _reap_zombies()  # adopted durable wrappers also finish while the owner is idle
            outcome = serve_step(root, factory, conflicts=lambda: live_conflicts(repo, {os.getpid()}), log=log)
            if outcome == "empty":
                outcome = roadmap_step(root, repo, fingerprint=lambda: remote_fingerprint(repo),
                                       submit=lambda authority, tasks, mid: submit_mission(root, repo, authority, tasks,
                                                                                           mission_id=mid),
                                       ) or outcome
            if outcome != previous:
                log(f"owner: {outcome}")
                if outcome.startswith("roadmap:paused:"):
                    try:
                        state = serve_store.load()
                    except StateCorrupt:
                        state = default_state()
                    Notifier(state, sender=sender, log=log).send("roadmap_paused", now_utc(), owner="control-plane",
                                                                 reason=outcome.split(":", 2)[2])
                    serve_store.save(state, now_utc())
                if outcome.startswith("waiting:"):
                    _, mission_id, reason = outcome.split(":")
                    try:
                        state = serve_store.load()
                    except StateCorrupt:
                        state = default_state()
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
                                      "pending", "last_outcome", "switches", "updated_at")}
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
        state["counters"] = {"consecutive_failures": 0, "transient_streak": 0, "crash_retries": 0}
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
    try:
        while not stop["flag"]:
            now = now_utc()
            try:
                state = store.load()
            except StateCorrupt:
                state = default_state()
            notifier = Notifier(state, sender=None if args.no_notify else webhook_sender(), log=log)
            try:
                supervised_state = Store(root).load()
            except StateCorrupt:
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
                observe.observe_step(state["watch"].setdefault("observe", {}), observe.snapshot(root, repo, now), now,
                                     send=lambda kind, text, n=notifier, t=now: n.send_text(kind, text, t),
                                     digest_every=timedelta(hours=args.digest_hours),
                                     merges=lambda since: observe.merges_since(repo, since),
                                     failures=int(state["notify"].get("failures") or 0))
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
