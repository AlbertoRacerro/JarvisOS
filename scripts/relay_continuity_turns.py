"""Turn classification for relay_continuity (spec 265)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shlex
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    EFFECT_ORDER,
    NATIVE_AGENTS,
    iso,
    now_utc,
    parse_ts,
    relay_argon,
)

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
    exit_status: int | None = None  # a native command's recorded exit status; non-zero is a finished effect, not a failure
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
    # Set when the runner's own evidence shows the turn ended for a harness or provider reason (time limit, quota,
    # auth blip, network). It is never a verdict on the model's capability; capability is judged on delivered work.
    harness_cause: str | None = None
    spend_cap: bool = False  # the provider message named a spend/credit cap (even if it also printed a reset)
    reaped: list[str] | None = None  # processes the turn left running, stopped at its end; None: not launched here
    context_used_tokens: int | None = None  # spec 267: cumulative reported tokens per turn
    timed_out: bool = False
    usage: dict[str, int] | None = None

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


def _extract_claude_usage(events: list[dict[str, Any]], terminal: dict[str, Any] | None) -> dict[str, int] | None:
    if isinstance(terminal, dict) and terminal.get("type") == "result":
        mu = terminal.get("modelUsage")
        if isinstance(mu, dict) and mu:
            tot_in = 0
            tot_out = 0
            tot_cache = 0
            has_valid = False
            for m_entry in mu.values():
                if isinstance(m_entry, dict):
                    inp = m_entry.get("inputTokens")
                    out = m_entry.get("outputTokens")
                    cr = m_entry.get("cacheReadInputTokens", 0) or 0
                    cc = m_entry.get("cacheCreationInputTokens", 0) or 0
                    if (isinstance(inp, int) and not isinstance(inp, bool) and inp >= 0
                            and isinstance(out, int) and not isinstance(out, bool) and out >= 0
                            and isinstance(cr, int) and not isinstance(cr, bool) and cr >= 0
                            and isinstance(cc, int) and not isinstance(cc, bool) and cc >= 0):
                        tot_in += inp
                        tot_out += out
                        tot_cache += cr + cc
                        has_valid = True
            if has_valid:
                return {
                    "input_tokens": tot_in,
                    "output_tokens": tot_out,
                    "cache_tokens": tot_cache,
                    "total_tokens": tot_in + tot_out + tot_cache,
                }

        tu = terminal.get("usage")
        if isinstance(tu, dict):
            inp = tu.get("input_tokens")
            out = tu.get("output_tokens")
            cr = tu.get("cache_read_input_tokens", 0) or 0
            cc = tu.get("cache_creation_input_tokens", 0) or 0
            if (isinstance(inp, int) and not isinstance(inp, bool) and inp >= 0
                    and isinstance(out, int) and not isinstance(out, bool) and out >= 0
                    and isinstance(cr, int) and not isinstance(cr, bool) and cr >= 0
                    and isinstance(cc, int) and not isinstance(cc, bool) and cc >= 0):
                c_tok = cr + cc
                return {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_tokens": c_tok,
                    "total_tokens": inp + out + c_tok,
                }

    top_assistants = [e for e in events if e.get("type") == "assistant" and not e.get("parent_tool_use_id")]
    assistants = top_assistants if top_assistants else [e for e in events if e.get("type") == "assistant"]

    tot_in = 0
    tot_out = 0
    tot_cache = 0
    has_valid = False
    for e in assistants:
        u = e.get("usage")
        if not isinstance(u, dict):
            msg = e.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                u = msg["usage"]
            else:
                u = None
        if isinstance(u, dict):
            inp = u.get("input_tokens")
            out = u.get("output_tokens", 0) or 0
            cr = u.get("cache_read_input_tokens", 0) or 0
            cc = u.get("cache_creation_input_tokens", 0) or 0
            if (isinstance(inp, int) and not isinstance(inp, bool) and inp >= 0
                    and isinstance(out, int) and not isinstance(out, bool) and out >= 0
                    and isinstance(cr, int) and not isinstance(cr, bool) and cr >= 0
                    and isinstance(cc, int) and not isinstance(cc, bool) and cc >= 0):
                tot_in += inp
                tot_out += out
                tot_cache += cr + cc
                has_valid = True

    if has_valid:
        return {
            "input_tokens": tot_in,
            "output_tokens": tot_out,
            "cache_tokens": tot_cache,
            "total_tokens": tot_in + tot_out + tot_cache,
        }
    return None


def _extract_codex_usage(events: list[dict[str, Any]], terminal: dict[str, Any] | None) -> dict[str, int] | None:
    cand = None
    if isinstance(terminal, dict) and terminal.get("type") == "turn.completed" and isinstance(terminal.get("usage"), dict):
        cand = terminal["usage"]
    else:
        for e in reversed(events):
            if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict):
                cand = e["usage"]
                break
    if cand is not None:
        raw_in = cand.get("input_tokens")
        out_tok = cand.get("output_tokens")
        cached = cand.get("cached_input_tokens", 0) or 0
        cache_write = cand.get("cache_write_input_tokens", 0) or 0
        if (isinstance(raw_in, int) and not isinstance(raw_in, bool) and raw_in >= 0
                and isinstance(out_tok, int) and not isinstance(out_tok, bool) and out_tok >= 0
                and isinstance(cached, int) and not isinstance(cached, bool) and cached >= 0
                and isinstance(cache_write, int) and not isinstance(cache_write, bool) and cache_write >= 0):
            inp_tok = max(0, raw_in - cached)
            cache_tok = cached + cache_write
            total_tokens = inp_tok + out_tok + cache_tok
            return {
                "input_tokens": inp_tok,
                "output_tokens": out_tok,
                "cache_tokens": cache_tok,
                "total_tokens": total_tokens,
            }
    return None


def _extract_native_usage(events: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, int] | None:
    res_obj = None
    if results:
        res_obj = results[-1]
    else:
        for e in reversed(events):
            if e.get("event") == "result" and isinstance(e.get("result"), dict):
                res_obj = e["result"]
                break
    if isinstance(res_obj, dict) and isinstance(res_obj.get("usage"), dict):
        u = res_obj["usage"]
        inp_tok = u.get("input_tokens")
        out_tok = u.get("output_tokens")
        cache_read = u.get("cache_read_tokens", 0) or 0
        raw_total = u.get("total_tokens")
        if (isinstance(inp_tok, int) and not isinstance(inp_tok, bool) and inp_tok >= 0
                and isinstance(out_tok, int) and not isinstance(out_tok, bool) and out_tok >= 0
                and isinstance(cache_read, int) and not isinstance(cache_read, bool) and cache_read >= 0):
            cache_tokens = cache_read
            total_tokens = (raw_total if (isinstance(raw_total, int) and not isinstance(raw_total, bool)
                                         and raw_total >= inp_tok + out_tok)
                            else (inp_tok + out_tok + cache_tokens))
            return {
                "input_tokens": inp_tok,
                "output_tokens": out_tok,
                "cache_tokens": cache_tokens,
                "total_tokens": total_tokens,
            }
    return None


def _apply_usage_fallback(report: TurnReport) -> None:
    if (report.usage is None and isinstance(report.context_used_tokens, int)
            and not isinstance(report.context_used_tokens, bool) and report.context_used_tokens > 0):
        report.usage = {
            "input_tokens": report.context_used_tokens,
            "output_tokens": 0,
            "cache_tokens": 0,
            "total_tokens": report.context_used_tokens,
        }


def _apply_turn_usage(report: TurnReport, agent: str | None, events: list[dict[str, Any]],
                      terminal: dict[str, Any] | None) -> None:
    if agent == "codex":
        report.usage = _extract_codex_usage(events, terminal)
    elif agent == "claude":
        report.usage = _extract_claude_usage(events, terminal)
    else:
        report.usage = _extract_codex_usage(events, terminal) or _extract_claude_usage(events, terminal)
    _apply_usage_fallback(report)


def classify_turn(turn_dir: Path, *, ref: datetime | None = None, local_tz: Any = None,
                  diagnostics: str = "", timed_out: bool = False) -> TurnReport:
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
        report = classify_argon_turn(turn_dir, events, bad, agent=agent, ref=ref, timed_out=timed_out)
        _apply_usage_fallback(report)
        return report
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
    meta = state.get("metadata")
    tokens = meta.get("context_used_tokens") if isinstance(meta, dict) else None
    if tokens is None:
        tokens = state.get("context_used_tokens")
    used_tok = int(tokens) if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) else None
    report = TurnReport(outcome="crash", agent=agent, detail="no terminal event", bad_lines=bad,
                        relay_status=state.get("status"), turn_id=f"{turn_dir.parent.parent.name}/{turn_dir.name}",
                        context_used_tokens=used_tok)
    if not events:
        report.detail = "output missing" if not output.exists() else ("output unparseable" if bad else "output empty")
        report.no_evidence = not bad
        _apply_usage_fallback(report)
        return no_terminal_evidence(report, turn_dir, diagnostics)
    if same_output_as_previous_turn(turn_dir):
        # Relay keeps the previous turn's output when it captures nothing ("No response captured"); the agent
        # may still have worked for hours. Those events are not this turn's evidence.
        report.detail, report.no_evidence = "no output of its own (previous turn's output kept)", True
        _apply_usage_fallback(report)
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
    _apply_turn_usage(report, agent, events, terminal)
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
                        *, agent: str = "argon", ref: datetime | None = None,
                        timed_out: bool = False) -> TurnReport:
    """Classify the retained native protocol, never model prose masquerading as execution evidence."""
    report = TurnReport(outcome="unknown", agent=agent, detail="native turn incomplete", bad_lines=bad,
                        turn_id=f"{turn_dir.parent.parent.name}/{turn_dir.name}",
                        timed_out=timed_out)
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
        if report.outcome == "transient":
            report.harness_cause = "provider_network"  # e.g. the quota read could not reach the provider
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
    if agent == "hydrogen":
        allowed_models = relay_argon.HYDROGEN_MODELS
        expected_label = "Gemini 4 Hydrogen"
    elif agent == "agy_opus":
        allowed_models = relay_argon.OPUS_MODELS
        expected_label = "Claude Opus"
    else:
        allowed_models = relay_argon.MODELS
        expected_label = "Gemini 4 Argon"
    _ = expected_label
    for index, event in enumerate(events):
        typ = event.get("event")
        if typ == "init":
            if initialized or index != 0 or not event.get("conversation_id"):
                report.bad_lines += 1
            initialized = True
            conversation = event.get("conversation_id")
            init = event.get("init")
            if not isinstance(init, dict) or init.get("model") != meta.get("model") or init.get("model") not in allowed_models:
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
                    # The stream reports `edit_file`, `write_file` and `read_file` for the file tools whatever their
                    # advertised names are (all retained turns, 2026-11); an edit is a local write, never external.
                    # manage_task controls the CLI's own background tasks on this workstation (status, kill).
                    local = {"write_to_file", "replace_file_content", "multi_replace_file_content", "notebook_edit",
                             "edit_file", "write_file", "manage_task"}
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
                    else:
                        # A non-zero exit (a search with no match, a failing test run, a retried push) ended:
                        # it is a finished effect, like an error tool result on the other coordinator paths.
                        # External effects are reconciled from observable state, never from this status.
                        call.exit_status = int(exit_match[1])
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
    report.usage = _extract_native_usage(events, results)
    _apply_usage_fallback(report)
    if events and not initialized:
        report.bad_lines += 1
    # The runner's structured error (stderr), when present, names a harness or provider cause. It is applied only
    # to turns that did not succeed, and never touches the effects: interrupted operations stay in flight and are
    # still gated and reconciled from observable state.
    try:
        native = relay_argon.native_error((turn_dir / "stderr.log").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        native = None
    cause = relay_argon.harness_cause(native) or (("unknown", "turn_time_limit") if reason == "timeout" else None)

    if timed_out:
        report.outcome = "unknown"
        report.harness_cause = "turn_time_limit"
        if "turn_time_limit" not in report.detail:
            report.detail += "; harness: turn_time_limit"
        return report

    valid_record = initialized and not report.bad_lines and bool(meta) and not report.no_evidence
    if cause is not None and cause[1] == "turn_time_limit" and not valid_record:
        cause = None

    def harness(report: TurnReport) -> TurnReport:
        if cause is not None and report.outcome != "success":
            report.outcome, report.harness_cause = cause
            report.detail += f"; runner: {cause[1]}"
        return report

    # A failed tool call that cannot act beyond this workstation (a missing file, an edit that did not apply, a
    # finished-task kill) is the normal back-and-forth of a turn: it is finished and does not hide the outcome.
    # A failed or cancelled external call stays strict: what it did is not known.
    if not initialized or report.bad_lines or len(results) != 1 or not meta \
            or any(c.pending or (c.ended_abnormally and external(c)) for c in calls.values()):
        return harness(report)
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
        harness(report)
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
        report.no_evidence = False
    elif "wall limit" in text.lower():
        report.detail = f"{report.detail}; {diagnostics.strip()}"
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
    if report.timed_out:
        return None
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


