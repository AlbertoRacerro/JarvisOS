#!/usr/bin/env python3
"""JarvisOS host operations (spec 207): typed host actions requested from jarvis-sandbox, signed host receipts.

An agent inside the sandbox writes a structured request into the spool. The host executor (a copy of this file
installed from a merged master revision, outside every path the sandbox can write) validates it against the
host-only policy, performs one narrow typed action and writes an Ed25519-signed receipt. Anyone can verify a
receipt with the published public key; nobody without the host-only private key can forge one.

    host_ops.py request OP [--args JSON] [--sha SHA] [--wait SECONDS]   (sandbox or host)
    host_ops.py verify ID [--op OP] [--sha SHA]                          (sandbox or host)
    host_ops.py cancel ID                                               (sandbox or host)
    host_ops.py status                                                   (sandbox or host)
    host_ops.py serve                                                    (host executor; systemd unit)
    host_ops.py install --sha SHA [--with-watch]                           (host; maintainer/host session only)
    host_ops.py health                                                   (host)
    host_ops.py preflight                                                (host)

Stdlib only: the installed copy must not import anything from a checkout an agent can write.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import io
import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REQUEST_SCHEMA = "jarvisos.hostops.request.v1"
RECEIPT_SCHEMA = "jarvisos.hostops.receipt.v1"
POLICY_SCHEMA = "jarvisos.hostops.policy.v1"
SIGNER = "jarvisos-hostops"
SIG_NAMESPACE = "jarvisos-hostops-receipt"
REPO = "AlbertoRacerro/JarvisOS"
REPO_URL = f"https://github.com/{REPO}"
UNIT = "jarvisos-hostops.service"

ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REQUEST_NAME_RE = re.compile(r"^([0-9a-f]{32})\.json$")
MAX_REQUEST_BYTES = 64 * 1024
MAX_REQUEST_AGE = timedelta(hours=1)
MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_EVAL_ARCHIVE_BYTES = 32 << 20
MAX_EVAL_EXTRACTED_BYTES = 256 << 20
MAX_EVAL_ARCHIVE_MEMBERS = 20000
_BENCH_OUTPUT_MAX = 1 << 20
_BENCH_LOG_MAX = 64 << 20
OUTCOMES = ("ok", "failed", "refused", "cancelled", "interrupted", "timeout")
BENCH_IGNORED_COMPONENTS = frozenset({".venv", "node_modules", "dist", ".vite", ".pytest_cache", ".ruff_cache", ".mypy_cache"})


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def canonical(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


@dataclass(frozen=True)
class Paths:
    """Every location derives from one home, so tests run on a temporary one."""

    home: Path

    @property
    def spool(self) -> Path:  # sandbox read-write
        return self.home / "jarvis-control" / "work" / "hostops"

    @property
    def requests(self) -> Path:
        return self.spool / "requests"

    @property
    def receipts(self) -> Path:
        return self.spool / "receipts"

    @property
    def outbox(self) -> Path:
        return self.spool / "outbox"

    @property
    def heartbeat(self) -> Path:
        return self.spool / "heartbeat.json"

    @property
    def config(self) -> Path:  # host only: the sandbox has its own ~/.config
        return self.home / ".config" / "jarvisos-hostops"

    @property
    def policy(self) -> Path:
        return self.config / "policy.json"

    @property
    def key(self) -> Path:
        return self.config / "receipt_ed25519"

    @property
    def webhook(self) -> Path:
        return self.home / ".config" / "jarvis" / "slack_webhook_url"

    @property
    def units(self) -> Path:
        return self.home / ".config" / "systemd" / "user"

    @property
    def install(self) -> Path:  # host only: not mounted in the sandbox at all
        return self.home / ".local" / "share" / "jarvisos-hostops"

    @property
    def guard(self) -> Path:
        return self.install / "guard"

    @property
    def state(self) -> Path:
        return self.install / "state"

    @property
    def release_pending(self) -> Path:
        return self.state / "release-pending.json"

    @property
    def release_history(self) -> Path:
        return self.state / "release-history.jsonl"

    @property
    def published(self) -> Path:  # read-only inside the sandbox
        return self.home / ".local" / "share" / "jarvisos" / "hostops"

    @property
    def allowed_signers(self) -> Path:
        return self.published / "allowed_signers"

    @property
    def owner_serve_state(self) -> Path:
        continuity_dir = os.environ.get("RELAY_CONTINUITY_DIR")
        if continuity_dir:
            return Path(continuity_dir).expanduser() / "serve" / "state.json"
        return self.home / "jarvis-control" / "work" / "continuity" / "serve" / "state.json"


# ---------------------------------------------------------------------------
# Filesystem access that never follows a symlink planted in the agent-writable spool


def open_dir(path: Path, *, create: bool = False) -> int:
    """A directory fd reached component by component without following symlinks."""
    if not path.is_absolute():
        raise ValueError("absolute path required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=fd)
                nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_regular(dir_fd: int, name: str, limit: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not (st.st_mode & 0o170000 == 0o100000) or st.st_size > limit:
            raise ValueError("not a bounded regular file")
        chunks, total = [], 0
        while chunk := os.read(fd, 65536):
            total += len(chunk)
            if total > limit:
                raise ValueError("file grew past its limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_at(dir_fd: int, name: str, data: bytes, mode: int = 0o644) -> None:
    """Atomic write: a fresh O_EXCL temporary renamed over `name`, so a planted symlink is replaced, never followed."""
    tmp = f".{name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def unlink_at(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def exists_at(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Text that may leave the host (Slack) or be echoed into receipts


_SECRET_RE = re.compile(
    r"(gh[opsur]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|hooks\.slack\.com|[A-Za-z0-9+/]{48,}={0,2})")
_SLACK_LINK_RE = re.compile(r"<([^<>]*)>")
_ALLOWED_LINK_RE = re.compile(re.escape(REPO_URL) + r"/(?:issues|pull)/\d{1,6}(?:\|[^<>|]{1,80})?")
_BARE_URL_RE = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]{1,15}://|www\.|mailto:)")


def message_problem(text: Any) -> str | None:
    """Why a Slack message body is refused, or None."""
    if not isinstance(text, str) or not 1 <= len(text) <= 3000:
        return "text must be 1-3000 characters"
    if re.search(r"[\x00-\x09\x0b-\x1f\x7f]", text):
        return "control characters"
    if _SECRET_RE.search(text):
        return "matches a secret pattern"
    for inner in _SLACK_LINK_RE.findall(text):
        if not _ALLOWED_LINK_RE.fullmatch(inner):
            return "Slack markup other than links to this repository's issues/PRs"
    if _BARE_URL_RE.search(_SLACK_LINK_RE.sub("", text)):
        return "links are allowed only as <repository issue/PR link|label>"
    return None


def scrub(text: str, limit: int = 2000) -> str:
    """Bounded diagnostic output for a receipt: secrets and host paths removed."""
    text = _SECRET_RE.sub("[redacted]", text.replace("\r", ""))
    text = re.sub(r"/home/[^/\s]+/\.(?:config|ssh|claude|codex)\S*", "[path]", text)
    return text[-limit:]


def scrub_json(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [scrub_json(item) for item in value]
    if isinstance(value, dict):
        return {scrub(str(key)): scrub_json(item) for key, item in value.items()}
    return value


# ---------------------------------------------------------------------------
# Signing and verification


def sign(key: Path, data: bytes) -> bytes:
    proc = subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n", SIG_NAMESPACE], input=data,
                          capture_output=True, timeout=30, check=False)
    if proc.returncode != 0 or b"BEGIN SSH SIGNATURE" not in proc.stdout:
        raise RuntimeError("receipt signing failed")
    return proc.stdout


def verify_signature(allowed_signers: Path, data: bytes, signature: bytes) -> bool:
    with tempfile.TemporaryDirectory(prefix="hostops-verify-") as tmp:
        sig = Path(tmp) / "receipt.sig"
        sig.write_bytes(signature)
        proc = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(allowed_signers), "-I", SIGNER,
                               "-n", SIG_NAMESPACE, "-s", str(sig)], input=data, capture_output=True, timeout=30,
                              check=False)
    return proc.returncode == 0


def load_receipt(paths: Paths, request_id: str) -> tuple[dict[str, Any] | None, str]:
    """A receipt whose signature verifies against the published host key, or (None, why)."""
    if not ID_RE.fullmatch(request_id):
        return None, "bad request id"
    try:
        fd = open_dir(paths.receipts)
    except OSError:
        return None, "no receipts directory"
    try:
        data = read_regular(fd, f"{request_id}.json", 1 << 20)
        signature = read_regular(fd, f"{request_id}.json.sig", 16 << 10)
    except (OSError, ValueError):
        return None, "no receipt yet"
    finally:
        os.close(fd)
    if not paths.allowed_signers.is_file():
        return None, "host public key is not published"
    if not verify_signature(paths.allowed_signers, data, signature):
        return None, "signature does not verify"
    try:
        receipt = json.loads(data)
    except json.JSONDecodeError:
        return None, "unparseable receipt"
    if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("request_id") != request_id:
        return None, "receipt is for another request"
    return receipt, ""


def receipt_problem(receipt: dict[str, Any], *, op: str | None = None, sha: str | None = None) -> str | None:
    if op is not None and receipt.get("op") != op:
        return f"receipt is for {receipt.get('op')}, not {op}"
    if sha is not None and receipt.get("sha") != sha:
        return f"receipt is for {receipt.get('sha')}, not {sha}"
    if receipt.get("outcome") != "ok":
        return f"outcome {receipt.get('outcome')}: {receipt.get('error') or 'no detail'}"
    return None


def find_ok_receipt(paths: Paths, op: str, sha: str | None = None) -> dict[str, Any] | None:
    """Newest verified `ok` receipt for `op` (and `sha`): what a deterministic completion check consumes."""
    try:
        names = sorted(p.name for p in paths.receipts.iterdir() if REQUEST_NAME_RE.fullmatch(p.name))
    except OSError:
        return None
    best = None
    for name in names:
        receipt, _ = load_receipt(paths, name[:-5])
        if (receipt and receipt_problem(receipt, op=op, sha=sha) is None
                and (best is None or str(receipt.get("finished_at")) > str(best.get("finished_at")))):
            best = receipt
    return best


# ---------------------------------------------------------------------------
# Requests (sandbox side)


def make_request(op: str, args: dict[str, Any], sha: str | None, requester: str, now: datetime) -> dict[str, Any]:
    return {"schema": REQUEST_SCHEMA, "id": secrets.token_hex(16), "op": op, "args": args, "sha": sha,
            "requested_at": iso(now), "requester": requester[:120]}


def submit(paths: Paths, request: dict[str, Any]) -> str:
    fd = open_dir(paths.requests, create=True)
    try:
        write_at(fd, f"{request['id']}.json", canonical(request))
    finally:
        os.close(fd)
    return request["id"]


def request_problem(request: Any, now: datetime) -> str | None:
    if not isinstance(request, dict) or request.get("schema") != REQUEST_SCHEMA:
        return "unknown schema"
    if set(request) - {"schema", "id", "op", "args", "sha", "requested_at", "requester"}:
        return "unknown fields"
    if not ID_RE.fullmatch(str(request.get("id"))):
        return "bad id"
    if not isinstance(request.get("op"), str) or request["op"] not in OPERATIONS:
        return "unknown operation"
    if not isinstance(request.get("args"), dict):
        return "args must be an object"
    if request.get("sha") is not None and not SHA_RE.fullmatch(str(request["sha"])):
        return "sha must be a full 40-hex commit id"
    at = parse_ts(request.get("requested_at"))
    if at is None or now - at > MAX_REQUEST_AGE or at - now > MAX_CLOCK_SKEW:
        return "requested_at missing, too old or in the future (replay protection)"
    if not isinstance(request.get("requester"), str) or len(request["requester"]) > 120:
        return "bad requester"
    return None


# ---------------------------------------------------------------------------
# Operations. Each validates its own arguments against the host policy, then acts.


class Refused(Exception):
    """The request is outside the policy: fail closed, nothing was done."""


class Failed(Exception):
    """The action ran and did not succeed."""

    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail or {}


@dataclass
class Context:
    paths: Paths
    policy: dict[str, Any]
    request: dict[str, Any]
    run: Callable[..., subprocess.CompletedProcess]
    post: Callable[[str, bytes], int]
    now: Callable[[], datetime]
    cancelled: Callable[[], bool]
    journal: list[dict[str, Any]]
    restart_required: bool = False

    def conf(self, op: str) -> dict[str, Any]:
        conf = (self.policy.get("ops") or {}).get(op)
        if not isinstance(conf, dict):
            raise Refused(f"{op} is not enabled in the host policy")
        return conf


def _args(ctx: Context, allowed: set[str]) -> dict[str, Any]:
    args = ctx.request["args"]
    if set(args) - allowed:
        raise Refused(f"unknown arguments: {sorted(set(args) - allowed)}")
    return args


def _systemctl(target: Context | Executor, *argv: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return target.run(["systemctl", "--user", *argv], timeout=timeout)


def _unit_state(target: Context | Executor, unit: str, *, timeout: float = 30) -> dict[str, str]:
    props = "ActiveState,SubState,UnitFileState,MainPID,NRestarts,ExecMainStatus,ActiveEnterTimestamp"
    raw = _systemctl(target, "show", unit, "-p", props, timeout=timeout).stdout
    out = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return {k: v for k, _, v in (line.partition("=") for line in out.splitlines()) if k}


def _unit_arg(ctx: Context, args: dict[str, Any], allowed: Any) -> str:
    unit = args.get("unit")
    if not isinstance(allowed, list) or unit not in allowed:
        raise Refused(f"unit {unit!r} is not allowed by the host policy")
    return unit


def op_slack_deliver(ctx: Context) -> dict[str, Any]:
    conf = ctx.conf("slack.deliver")
    args = _args(ctx, {"text"})
    if problem := message_problem(args.get("text")):
        raise Refused(problem)
    hour_ago = iso(ctx.now() - timedelta(hours=1))
    recent = sum(1 for e in ctx.journal if e.get("op") == "slack.deliver" and e.get("outcome") == "ok"
                 and str(e.get("finished_at")) >= hour_ago)
    if recent >= int(conf.get("max_per_hour", 6)):
        raise Refused("slack.deliver rate limit reached")
    sha = ctx.request.get("sha")
    header = f":wrench: *host-ops* `{ctx.request['id'][:8]}`" + (f" · `{sha[:8]}`" if sha else "")
    text = f"{header}\n{args['text']}"
    status = ctx.post(text, b"")
    detail = {"http_status": status, "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "chars": len(text),
              "proves": "the Slack incoming webhook accepted the message (HTTP 2xx); not channel visibility"}
    if not 200 <= status < 300:
        raise Failed(f"webhook answered HTTP {status}", detail)
    return detail


def op_service_status(ctx: Context) -> dict[str, Any]:
    conf = ctx.conf("service.status")
    unit = _unit_arg(ctx, _args(ctx, {"unit"}), conf.get("units"))
    return {"unit": unit, "state": _unit_state(ctx, unit)}


OWNER_ACTIONS = {"jarvisos-mission-owner.service": ["start"]}


def op_service_control(ctx: Context) -> dict[str, Any]:
    conf = ctx.conf("service.control")
    args = _args(ctx, {"unit", "action"})
    unit = _unit_arg(ctx, args, conf.get("units"))
    action = args.get("action")
    if action not in ("start", "stop", "restart"):
        raise Refused("action must be start, stop or restart")
    # Stopping or restarting the mission owner kills a running provider turn; the graceful path is its STOP file.
    limited = conf.get("actions") if isinstance(conf.get("actions"), dict) else OWNER_ACTIONS
    if unit in limited and action not in (limited[unit] or []):
        raise Refused(f"{action} of {unit} is not allowed by the host policy")
    proc = _systemctl(ctx, action, unit)
    want = "inactive" if action == "stop" else "active"
    state: dict[str, str] = {}
    for _ in range(10):
        state = _unit_state(ctx, unit)
        if state.get("ActiveState") == want:
            break
        time.sleep(1)
    detail = {"unit": unit, "action": action, "returncode": proc.returncode, "state": state}
    if proc.returncode != 0 or state.get("ActiveState") != want:
        raise Failed(f"{unit} is {state.get('ActiveState')} after {action}", detail)
    return detail


UNIT_TEMPLATES = {
    "jarvisos-relay-watch": (
        "[Unit]\nDescription=JarvisOS Relay observer: Slack digest and incident alerts (spec 205)\n"
        "[Service]\nWorkingDirectory=%h/src/JarvisOS\n"
        "ExecStart=/usr/bin/python3 -I %h/.local/share/jarvisos-hostops/current/scripts/relay_continuity.py watch --digest-hours 2\n"
        "Restart=on-failure\nRestartPreventExitStatus=2 3\nRestartSec=60\n"
        "[Install]\nWantedBy=default.target\n"),
    "jarvisos-mission-owner": (
        "[Unit]\nDescription=JarvisOS supervised mission owner (spec 201)\n"
        "[Service]\nWorkingDirectory=%h/src/JarvisOS\n"
        "ExecStart=%h/.local/bin/jarvis-sandbox python3 %h/.local/share/jarvisos/hostops/current/scripts/relay_continuity.py serve --interval 60\n"
        "KillMode=process\nRestart=on-failure\nRestartPreventExitStatus=2 3\nRestartSec=120\n"
        "[Install]\nWantedBy=default.target\n"),
}


def op_service_install(ctx: Context) -> dict[str, Any]:
    conf = ctx.conf("service.install")
    args = _args(ctx, {"template"})
    name = args.get("template")
    if not isinstance(name, str) or name not in UNIT_TEMPLATES or name not in (conf.get("templates") or []):
        raise Refused(f"template {name!r} is not allowed by the host policy")
    unit = f"{name}.service"
    body = UNIT_TEMPLATES[name].encode()
    fd = open_dir(ctx.paths.units, create=True)
    try:
        try:
            current = read_regular(fd, unit, 1 << 16)
        except FileNotFoundError:
            current = None
        if current is not None and current != body:
            sfd = open_dir(ctx.paths.state / "unit-backups", create=True)
            try:
                write_at(sfd, f"{unit}.{ctx.request['id'][:8]}", current)
            finally:
                os.close(sfd)
        if current != body:
            write_at(fd, unit, body)
    finally:
        os.close(fd)
    reload = _systemctl(ctx, "daemon-reload")
    enable = _systemctl(ctx, "enable", "--now", unit)
    time.sleep(3)
    state = _unit_state(ctx, unit)
    detail = {"unit": unit, "unit_sha256": hashlib.sha256(body).hexdigest(), "changed": current != body,
              "returncodes": [reload.returncode, enable.returncode], "state": state}
    if reload.returncode != 0 or enable.returncode != 0 or state.get("ActiveState") != "active":
        raise Failed(f"{unit} is {state.get('ActiveState')} after install", detail)
    return detail


_WIN_PATH_RE = re.compile(r"^[A-Za-z]:\\[A-Za-z0-9 ._\\-]{1,240}$")
_EXPLORER = "/mnt/c/Windows/explorer.exe"
_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def _windows_path(ctx: Context, path: Path) -> str:
    proc = ctx.run(["wslpath", "-w", str(path)], timeout=10)
    value = str(proc.stdout).strip()
    if proc.returncode != 0 or not _WIN_PATH_RE.fullmatch(value):
        raise Failed("wslpath returned an unusable Windows path", {})
    return value


def _handoff_dir(ctx: Context, conf: dict[str, Any]) -> Path:
    raw = conf.get("dir")
    if not isinstance(raw, str) or not re.fullmatch(r"/mnt/[a-z]/[A-Za-z0-9 ._/-]+", raw) or ".." in raw:
        raise Refused("wsl.handoff has no valid /mnt/<drive> directory in the host policy")
    return Path(raw)


def op_wsl_probe(ctx: Context) -> dict[str, Any]:
    conf = ctx.conf("wsl.probe")
    _args(ctx, set())
    detail: dict[str, Any] = {}
    try:
        detail["wsl_kernel"] = "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        detail["wsl_kernel"] = False
    detail["interop_registered"] = Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists() or \
        Path("/proc/sys/fs/binfmt_misc/WSLInterop-late").exists()
    detail["explorer_present"] = Path(_EXPLORER).is_file()
    ver = ctx.run(["/mnt/c/Windows/System32/cmd.exe", "/d", "/c", "ver"], timeout=20, cwd="/mnt/c")
    detail["windows_version"] = scrub(str(ver.stdout).strip(), 120) if ver.returncode == 0 else None
    if (handoff := (ctx.policy.get("ops") or {}).get("wsl.handoff")) and isinstance(handoff, dict):
        target = _handoff_dir(ctx, handoff)
        detail["handoff_dir_exists"] = target.is_dir()
        if target.is_dir():
            detail["handoff_windows_path"] = _windows_path(ctx, target)
    del conf
    if not (detail["wsl_kernel"] and detail["interop_registered"] and detail["windows_version"]):
        raise Failed("WSL interop is not usable", detail)
    return detail


def op_wsl_handoff(ctx: Context) -> dict[str, Any]:
    conf = ctx.conf("wsl.handoff")
    args = _args(ctx, {"file", "sha256", "open"})
    name, want = args.get("file"), args.get("sha256")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", name):
        raise Refused("file must be a plain outbox file name")
    ext = os.path.splitext(name)[1].lower()
    if ext not in (conf.get("extensions") or []):
        raise Refused(f"extension {ext!r} is not allowed by the host policy")
    if not isinstance(want, str) or not re.fullmatch(r"[0-9a-f]{64}", want):
        raise Refused("sha256 must be 64 lowercase hex characters")
    if "open" in args and not isinstance(args["open"], bool):
        raise Refused("open must be a boolean")
    if args.get("open") and not conf.get("open"):
        raise Refused("opening handed-off files is not enabled in the host policy")
    target_dir = _handoff_dir(ctx, conf)
    limit = int(conf.get("max_bytes", 200 << 20))
    sfd = open_dir(ctx.paths.outbox)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=sfd)
        except OSError:
            raise Refused("outbox file must be a regular file, never a symlink") from None
    finally:
        os.close(sfd)
    out_name = f"{want[:12]}-{ctx.request['id'][:8]}{ext}"
    try:
        tfd = open_dir(target_dir, create=True)
    except BaseException:
        os.close(fd)
        raise
    tmp = f".{out_name}.{secrets.token_hex(4)}.tmp"
    digest, size = hashlib.sha256(), 0
    try:
        st = os.fstat(fd)
        if not st.st_mode & 0o170000 == 0o100000 or st.st_size > limit:
            raise Refused("outbox file is not a regular file within the size limit")
        ofd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=tfd)
        try:
            while chunk := os.read(fd, 1 << 20):
                size += len(chunk)
                if size > limit:
                    raise Refused("outbox file grew past the size limit")
                digest.update(chunk)
                os.write(ofd, chunk)
        finally:
            os.close(ofd)
        if digest.hexdigest() != want:
            raise Refused("outbox file does not match the requested sha256")
        os.replace(tmp, out_name, src_dir_fd=tfd, dst_dir_fd=tfd)
        tmp = ""
    finally:
        os.close(fd)
        if tmp:
            unlink_at(tfd, tmp)
        os.close(tfd)
    dest = target_dir / out_name
    detail: dict[str, Any] = {"handoff_path": str(dest), "sha256": want, "bytes": size,
                              "windows_path": _windows_path(ctx, dest), "opened": False}
    if args.get("open"):
        started = ctx.now()
        try:
            ctx.run([_EXPLORER, detail["windows_path"]], timeout=15, cwd="/mnt/c")
        except subprocess.TimeoutExpired:
            raise Failed("explorer.exe did not return within 15 s", detail) from None
        detail["opened"] = "handed to explorer.exe (association decides the program)"
        seconds = int((ctx.now() - started).total_seconds()) + 5
        script = (f"Get-Process | Where-Object {{ $_.StartTime -ge (Get-Date).AddSeconds(-{seconds}) }} | "
                  "Select-Object -ExpandProperty ProcessName")
        procs = ctx.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], timeout=40, cwd="/mnt/c")
        names = sorted({n.strip() for n in str(procs.stdout).splitlines() if re.fullmatch(r"[A-Za-z0-9._ -]{1,60}", n.strip())})
        detail["windows_processes_started"] = names[:30]
    return detail


_LOCAL_URL_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost):(\d{2,5})(/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*)?$")
VIEWPORTS = {"wide": (1440, 900), "narrow": (390, 844)}


def op_browser_screenshot(ctx: Context) -> dict[str, Any]:
    conf = ctx.conf("browser.screenshot")
    args = _args(ctx, {"url", "viewport", "scheme", "wait_ms"})
    url = args.get("url")
    match = _LOCAL_URL_RE.fullmatch(url) if isinstance(url, str) else None
    if not match or not 1024 <= int(match.group(1)) <= 65535:
        raise Refused("url must be http://127.0.0.1:<port>/... (or localhost) with an unprivileged port")
    viewport = args.get("viewport", "wide")
    scheme = args.get("scheme", "light")
    wait = args.get("wait_ms", 3000)
    if not isinstance(viewport, str) or viewport not in VIEWPORTS or scheme not in ("light", "dark") or not isinstance(wait, int) \
            or isinstance(wait, bool) or not 0 <= wait <= 15000:
        raise Refused("viewport wide|narrow, scheme light|dark, wait_ms 0-15000")
    chrome = conf.get("chrome")
    if not isinstance(chrome, str) or not Path(chrome).is_file():
        raise Refused("no Chromium binary configured in the host policy")
    width, height = VIEWPORTS[viewport]
    with tempfile.TemporaryDirectory(dir=ctx.paths.spool, prefix="shot-") as tmp:
        shot = Path(tmp) / "shot.png"
        os.close(open_dir(ctx.paths.receipts, create=True))
        argv = [chrome, "--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-gpu",
                "--disable-extensions", "--disable-sync", "--hide-scrollbars",
                f"--user-data-dir={tmp}/profile", f"--window-size={width},{height}", f"--virtual-time-budget={wait}",
                f"--blink-settings=preferredColorScheme={0 if scheme == 'dark' else 1}",
                f"--screenshot={shot}", url]
        launcher = str(conf.get("launcher") or ctx.paths.home / ".local/bin/jarvis-sandbox")
        proc = ctx.run([launcher, *argv], timeout=90, cwd=str(ctx.paths.home / "src/JarvisOS"))
        if not shot.is_file() or shot.stat().st_size == 0:
            raise Failed("Chromium wrote no screenshot", {"returncode": proc.returncode,
                                                           "stderr": scrub(str(proc.stderr), 600)})
        shot_fd = open_dir(Path(tmp))
        try:
            data = read_regular(shot_fd, "shot.png", 32 << 20)
            unlink_at(shot_fd, "shot.png")  # through the verified descriptor: the spool path may be swapped
        finally:
            os.close(shot_fd)
    fd = open_dir(ctx.paths.receipts, create=True)
    try:
        write_at(fd, f"{ctx.request['id']}.png", data)
    finally:
        os.close(fd)
    return {"url": url, "viewport": [width, height], "scheme": scheme, "png": f"{ctx.request['id']}.png",
            "png_sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def op_http_smoke(ctx: Context) -> dict[str, Any]:
    conf = ctx.conf("http.smoke")
    args = _args(ctx, {"target"})
    targets = conf.get("targets") or {}
    target = targets.get(args.get("target")) if isinstance(targets, dict) and isinstance(args.get("target"), str) else None
    if not isinstance(target, dict) or not _LOCAL_URL_RE.fullmatch(str(target.get("url"))):
        raise Refused(f"target {args.get('target')!r} is not defined in the host policy")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())
    started = time.monotonic()
    try:
        with opener.open(urllib.request.Request(target["url"], method="GET"), timeout=15) as response:
            status, body = response.status, response.read(1 << 20)
    except urllib.error.HTTPError as exc:
        status, body = exc.code, b""
    except (OSError, urllib.error.URLError) as exc:
        raise Failed(f"request failed: {type(exc).__name__}", {"url": target["url"]}) from None
    detail = {"target": args["target"], "url": target["url"], "status": status,
              "ms": int((time.monotonic() - started) * 1000), "body_sha256": hashlib.sha256(body).hexdigest()}
    if status != int(target.get("expect", 200)):
        raise Failed(f"HTTP {status}, expected {target.get('expect', 200)}", detail)
    return detail


def op_bench_run(ctx: Context) -> dict[str, Any]:
    """A policy-registered command, run inside jarvis-sandbox (never with host privileges) under cgroup limits."""
    conf = ctx.conf("bench.run")
    args = _args(ctx, {"bench", "worktree"})
    if not isinstance(args.get("bench"), str):
        raise Refused("bench must name a registered benchmark")
    bench = (conf.get("benchmarks") or {}).get(args.get("bench"))
    if not isinstance(bench, dict) or not isinstance(bench.get("argv"), list):
        raise Refused(f"benchmark {args.get('bench')!r} is not registered in the host policy")
    worktree = args.get("worktree")
    if not isinstance(worktree, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", worktree):
        raise Refused("worktree must be a directory name under ~/src/worktrees")
    sha = ctx.request.get("sha")
    if not sha:
        raise Refused("bench.run needs the candidate sha")
    launcher = str(conf.get("launcher") or ctx.paths.home / ".local" / "bin" / "jarvis-sandbox")
    root = ctx.paths.home / "src" / "worktrees" / worktree
    sub = str(bench.get("cwd") or ".")
    if ".." in Path(sub).parts or Path(sub).is_absolute():
        raise Refused("benchmark cwd must be relative")
    result_name = bench.get("result_file")
    if result_name is not None and (not isinstance(result_name, str) or not result_name
                                    or Path(result_name).is_absolute() or ".." in Path(result_name).parts):
        raise Refused("benchmark result_file must be relative")
    result_path = root / sub / result_name if result_name else None
    if "secret_env" in bench:
        raise Refused("benchmark secret_env is forbidden")
    # The checkout is agent-controlled: its revision is read inside the sandbox, never by host git.
    git = [launcher, "git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-C", str(root)]
    head = ctx.run([*git, "rev-parse", "HEAD"], timeout=60, cwd=str(root))
    dirty = ctx.run([*git, "status", "--porcelain", "-z", "--ignored", "--untracked-files=all"],
                    timeout=60, cwd=str(root))
    flags = ctx.run([*git, "ls-files", "-v"], timeout=60, cwd=str(root))
    reported = str(head.stdout).strip()
    hidden = any(line and (line[0].islower() or line[0] == "S") for line in str(flags.stdout).splitlines())
    entries = str(dirty.stdout).split("\0")
    if entries[-1] == "":
        entries.pop()
    unexpected = any(not (entry.startswith("!! ") and
                          (BENCH_IGNORED_COMPONENTS.intersection(Path(entry[3:]).parts) or
                           ("__pycache__" in Path(entry[3:]).parts and Path(entry[3:]).suffix == ".pyc")))
                     for entry in entries)
    if (head.returncode != 0 or dirty.returncode != 0 or flags.returncode != 0
            or reported != sha or unexpected or hidden):
        raise Refused(f"worktree {worktree} is not a clean checkout of {sha[:12]} (sandbox reports {reported[:12] or '?'})")
    return _run_benchmark(ctx, args["bench"], bench, root, sub, result_path, reported, worktree)


def _run_benchmark(ctx: Context, bench_name: str, bench: dict[str, Any], root: Path, sub: str,
                   result_path: Path | None, reported: str, worktree: str | None,
                   remaining: float | None = None, scrub_result: bool = True,
                   close_scope: bool = False, deadline: float | None = None,
                   json_stdout: bool = False) -> dict[str, Any]:
    launcher = str(ctx.conf("bench.run").get("launcher") or ctx.paths.home / ".local" / "bin" / "jarvis-sandbox")
    if result_path is not None and result_path.exists():
        raise Refused("benchmark result_file already exists before the run")
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Failed("local evaluation exceeded its time limit", {})
    limits = {"timeout_s": min(int(bench.get("timeout_s", 1800)), 4 * 3600, remaining) if remaining is not None else min(int(bench.get("timeout_s", 1800)), 4 * 3600),
              "memory_max": str(bench.get("memory_max", "8G")), "cpu_quota": str(bench.get("cpu_quota", "400%")),
              "tasks_max": int(bench.get("tasks_max", 512))}
    scope = f"hostops-bench-{ctx.request['id'][:12]}"
    argv = ["systemd-run", "--user", "--scope", "--quiet", f"--unit={scope}",
            "-p", f"MemoryMax={limits['memory_max']}", "-p", f"CPUQuota={limits['cpu_quota']}",
            "-p", f"TasksMax={limits['tasks_max']}", "--",
            "timeout", "-k", "15", str(limits["timeout_s"]), launcher, "env", "-C", str(root / sub),
            *(["PYTHONDONTWRITEBYTECODE=1"] if close_scope else []),
            *[str(a) for a in bench["argv"]]]
    started = time.monotonic()
    proc = None
    cancelled = False
    expired = False
    oversized = False
    output_max = _BENCH_OUTPUT_MAX if json_stdout else _BENCH_LOG_MAX
    output_bytes = bytearray()
    output_exceeded = threading.Event()
    reader = None
    def read_output() -> None:
        assert proc is not None and proc.stdout is not None
        while True:
            try:
                chunk = proc.stdout.read(min(65536, output_max + 1 - len(output_bytes)))
            except (OSError, ValueError):  # closed during cleanup
                break
            if not chunk:
                break
            output_bytes.extend(chunk)
            if len(output_bytes) > output_max:
                output_exceeded.set()
                break
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                                start_new_session=True,
                                env=_host_env(), cwd=root / sub)
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        while proc.poll() is None:
            if output_exceeded.is_set():
                oversized = True
                break
            if deadline is not None and time.monotonic() >= deadline:
                expired = True
                break
            if ctx.cancelled():
                cancelled = True
                break
            time.sleep(min(1, max(0, deadline - time.monotonic())) if deadline is not None else 1)
        if proc.poll() is not None:
            reader.join(timeout=1)
        oversized = oversized or output_exceeded.is_set()
    finally:
        try:
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                proc.wait(timeout=60)
        finally:
            try:
                if close_scope:
                    _close_eval_scope(scope)
                elif cancelled or oversized or (reader is not None and reader.is_alive()):
                    subprocess.run(["systemctl", "--user", "stop", f"{scope}.scope"], check=False,
                                   capture_output=True, timeout=60, env=_host_env())
            finally:
                if reader is not None:
                    reader.join(timeout=1)
                if proc is not None and proc.stdout is not None:
                    proc.stdout.close()
                if reader is not None:
                    reader.join(timeout=1)
    if reader is not None and reader.is_alive():
        raise Failed("benchmark output did not close", {"bench": bench_name, "worktree": worktree,
                                                       "limits": limits})
    oversized = oversized or output_exceeded.is_set()
    output = bytes(output_bytes) if not oversized else b""
    if oversized:
        raise Failed("benchmark output exceeded its size limit",
                     {"bench": bench_name, "worktree": worktree, "limits": limits,
                      "wall_s": round(time.monotonic() - started, 2)})
    if expired or (deadline is not None and time.monotonic() >= deadline):
        raise Failed("local evaluation exceeded its time limit", {})
    detail = {"bench": bench_name, "worktree": worktree, "sandbox_head": reported, "limits": limits,
              "returncode": proc.returncode, "wall_s": round(time.monotonic() - started, 2),
              "output_sha256": hashlib.sha256(output).hexdigest(),
              "output_tail": "" if json_stdout else scrub(output.decode("utf-8", "replace"), 1500)}
    if cancelled:
        raise Cancelled(detail)
    if proc.returncode == 124:
        raise Failed("benchmark exceeded its time limit", detail)
    if proc.returncode != 0:
        raise Failed(f"benchmark exited {proc.returncode}", detail)
    if json_stdout:
        try:
            if len(output) > _BENCH_OUTPUT_MAX:
                raise ValueError("oversized output")
            detail["result"] = json.loads(output)
        except (ValueError, UnicodeError):
            raise Failed("benchmark JSON output is invalid", {}) from None
    if result_path is not None:
        try:
            fd = open_dir(result_path.parent)
            try:
                raw_result = read_regular(fd, result_path.name, 1 << 20)
            finally:
                os.close(fd)
            result = json.loads(raw_result)
            if not isinstance(result, (dict, list)):
                raise TypeError("result is not a JSON object or array")
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            raise Failed(f"benchmark result_file is missing or invalid ({type(exc).__name__})", detail) from None
        detail["result"] = scrub_json(result) if scrub_result else result
    return detail


def _close_eval_scope(scope: str) -> None:
    """Credential access requires evidence that the prompt scope has no live members."""
    unit = f"{scope}.scope"
    try:
        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True,
                       timeout=30, env=_host_env(), check=False)
        state = subprocess.run(["systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "LoadState"],
                               capture_output=True, text=True, timeout=30, env=_host_env(), check=False)
        props = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
        if state.returncode == 0 and props.get("ActiveState") == "inactive" and props.get("LoadState") in ("loaded", "not-found"):
            return
    except (OSError, subprocess.SubprocessError):
        pass
    raise Failed("local evaluator prompt scope could not be stopped", {})


def _eval_response_body(response: Any, remaining: Callable[[], float]) -> bytes:
    """Read at most 1 MiB while enforcing the evaluation's absolute deadline."""
    body = bytearray()
    sock = response.fp.raw._sock
    expired = threading.Event()

    def stop_read() -> None:
        expired.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    timer = threading.Timer(remaining(), stop_read)
    timer.start()
    try:
        while len(body) < 1 << 20:
            try:
                sock.settimeout(remaining())
                chunk = response.read1(min(64 << 10, (1 << 20) - len(body)))
            except (OSError, ValueError, http.client.HTTPException):
                if expired.is_set():
                    remaining()
                raise ValueError("invalid local model response") from None
            remaining()
            if not chunk:
                break
            body.extend(chunk)
            if getattr(response, "isclosed", lambda: False)():
                remaining()
                break
    finally:
        timer.cancel()
    return bytes(body)


_POINT_RE = re.compile(r"\[\[\s*point\s*:\s*([\w.:-]+)\s*(?:\|([^\]]*))?\]\]", re.IGNORECASE)
_EVAL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")
SEMANTIC_RESTORE_S = 300
SEMANTIC_CONNECT_S = 5
SEMANTIC_REPLIES_MAX_BYTES = 32 * 1024 * 1024


def _eval_prompts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise Refused("evaluator prompts must be an array of 1 to 50 entries")
    names: set[str] = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {"name", "prompt", "correct", "plausible"}:
            raise Refused("evaluator prompt entry has invalid fields")
        name, prompt = row["name"], row["prompt"]
        if not isinstance(name, str) or not _EVAL_ID_RE.fullmatch(name) or name in names:
            raise Refused("evaluator prompt name is invalid or repeated")
        names.add(name)
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= 16000 or "\x00" in prompt:
            raise Refused("evaluator prompt is empty or too long")
        for field in ("correct", "plausible"):
            ids = row[field]
            minimum = 1 if field == "correct" else 0
            if (not isinstance(ids, list) or not minimum <= len(ids) <= 20
                    or any(not isinstance(item, str) or not _EVAL_ID_RE.fullmatch(item) for item in ids)
                    or len(set(ids)) != len(ids)):
                raise Refused(f"evaluator {field} ids are invalid")
        if set(row["correct"]) & set(row["plausible"]):
            raise Refused("evaluator correct and plausible ids overlap")
    return value


def _loopback_base(value: Any) -> str:
    if not isinstance(value, str):
        raise Refused("local evaluation base_url is invalid")
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        raise Refused("local evaluation base_url is invalid") from None
    if (parts.scheme != "http" or not re.fullmatch(r"(?:127\.0\.0\.1|\[::1\]):[0-9]+", parts.netloc)
            or not port or parts.username or parts.password or parts.query or parts.fragment
            or parts.path != "/v1" or value != f"http://{parts.netloc}/v1"):
        raise Refused("local evaluation requires a loopback /v1 base_url")
    return value


def _eval_credential(value: Any, paths: Paths) -> str:
    if not isinstance(value, str) or not Path(value).is_absolute() or Path(value).parent != paths.config:
        raise Refused("local evaluation credential path is invalid")
    path = Path(value)
    try:
        fd = open_dir(path.parent)
        try:
            st = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise ValueError("insecure credential")
            token = read_regular(fd, path.name, 4096).decode("utf-8").strip()
        finally:
            os.close(fd)
    except (OSError, ValueError, UnicodeError):
        raise Refused("local evaluation credential is missing or insecure") from None
    if not token or re.search(r"[\x00-\x20\x7f]", token):
        raise Refused("local evaluation credential is invalid")
    return token


def _eval_score(text: str, correct: list[str], plausible: list[str]) -> tuple[str, str | None]:
    match = _POINT_RE.search(text)
    if not match:
        return "none", None
    first = match.group(1)
    return ("correct" if first in correct else "plausible" if first in plausible else "wrong"), first


def _eval_reachable(sha: str, remaining: Callable[[], float]) -> None:
    def status(head: str) -> str:
        result = _gh([f"repos/{REPO}/compare/{head}...{sha}", "--jq", ".status"],
                     timeout=min(60, remaining())).strip()
        remaining()
        return result

    try:
        if status("master") in ("identical", "behind"):
            return
        heads = _gh([f"repos/{REPO}/pulls?state=open&per_page=100", "--paginate",
                     "--jq", ".[].head.sha"], timeout=min(60, remaining())).splitlines()
        remaining()
        for head in heads:
            if SHA_RE.fullmatch(head) and (head == sha or status(head) in ("identical", "behind")):
                return
    except (RuntimeError, ValueError, TypeError, subprocess.TimeoutExpired):
        raise Refused("local evaluator reachability could not be verified on GitHub") from None
    raise Refused("local evaluator commit is not reachable from origin/master or an open PR head")


def _eval_checkout(sha: str, root: Path, remaining: Callable[[], float]) -> tuple[dict[Path, tuple[int, str]], set[Path]]:
    files: dict[Path, tuple[int, str]] = {}
    directories: set[Path] = set()
    try:
        proc = subprocess.Popen(["gh", "api", f"repos/{REPO}/tarball/{sha}"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=_host_env())
        try:
            data = bytearray()
            assert proc.stdout is not None
            while True:
                readable, _, _ = select.select([proc.stdout], [], [], min(1, remaining()))
                if not readable:
                    continue
                chunk = os.read(proc.stdout.fileno(), min(65536, MAX_EVAL_ARCHIVE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_EVAL_ARCHIVE_BYTES:
                    raise Refused("local evaluation archive download exceeds size limit")
            if proc.wait(timeout=remaining()) != 0:
                raise Refused("local evaluator checkout could not be fetched from GitHub")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdout.close()
        extracted = 0
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            prefix = None
            for count, member in enumerate(archive, 1):
                remaining()
                if count > MAX_EVAL_ARCHIVE_MEMBERS:
                    raise Refused("local evaluation archive has too many members")
                parts = Path(member.name).parts
                if (member.name.startswith("/") or not parts or ".." in parts
                        or not (member.isdir() or member.isfile())
                        or (len(parts) == 1 and not member.isdir())
                        or (prefix is not None and parts[0] != prefix)):
                    raise Refused("unsafe evaluation archive member")
                prefix = parts[0]
                if len(parts) == 1:
                    continue
                if member.isfile():
                    if member.size > MAX_EVAL_EXTRACTED_BYTES - extracted:
                        raise Refused("local evaluation archive exceeds extracted size limit")
                    extracted += member.size
                target = root.joinpath(*parts[1:])
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    directories.add(Path(*parts[1:]))
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    directories.update(parent for parent in Path(*parts[1:]).parents if parent != Path("."))
                    digest = hashlib.sha256()
                    size = 0
                    with archive.extractfile(member) as source, target.open("wb") as output:
                        while chunk := source.read(65536):
                            remaining()
                            output.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
                    files[Path(*parts[1:])] = (size, digest.hexdigest())
        remaining()
        return files, directories
    except (OSError, tarfile.TarError, subprocess.SubprocessError):
        raise Refused("local evaluator checkout could not be fetched from GitHub") from None


def _eval_check_checkout(root: Path, files: dict[Path, tuple[int, str]],
                         directories: set[Path], result: Path | set[Path]) -> None:
    """Compare the host-visible checkout with the extracted archive manifest."""
    seen: set[Path] = set()

    def walk(directory: Path) -> None:
        for entry in os.scandir(root / directory):
            path = directory / entry.name
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                if path not in directories:
                    raise Refused("local evaluator checkout changed during evaluation")
                walk(path)
            elif stat.S_ISREG(mode):
                if path in files:
                    fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    try:
                        actual = os.fstat(fd)
                        if not stat.S_ISREG(actual.st_mode):
                            raise Refused("local evaluator checkout changed during evaluation")
                        digest = hashlib.sha256()
                        while chunk := os.read(fd, 65536):
                            digest.update(chunk)
                        if (actual.st_size, digest.hexdigest()) != files[path]:
                            raise Refused("local evaluator checkout changed during evaluation")
                    finally:
                        os.close(fd)
                    seen.add(path)
                elif path not in (result if isinstance(result, set) else {result}):
                    raise Refused("local evaluator checkout changed during evaluation")
            else:
                raise Refused("local evaluator checkout changed during evaluation")

    try:
        walk(Path("."))
    except OSError:
        raise Refused("local evaluator checkout changed during evaluation") from None
    if seen != files.keys():
        raise Refused("local evaluator checkout changed during evaluation")


@contextmanager
def _eval_tempdir(parent: Path):
    """Remove an evaluator checkout even if its sandboxed child changes directory modes."""
    if parent.is_symlink() or not parent.is_dir():
        raise Refused("local evaluator checkout parent is unsafe")
    root = Path(tempfile.mkdtemp(prefix=".hostops-eval-", dir=parent))
    try:
        yield root
    finally:
        if root.is_symlink():
            raise Failed("local evaluator temporary checkout is unsafe", {})
        root.chmod(0o700)
        for current, dirs, _ in os.walk(root, followlinks=False):
            for name in dirs:
                child = Path(current) / name
                if not child.is_symlink():
                    child.chmod(0o700)
        shutil.rmtree(root)


def _semantic_settings(ctx: Context, name: str, setting: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    allowed = {"kind", "model", "base_url", "credential_file", "thinking", "verification",
               "max_tokens", "per_request_timeout_s", "timeout_s", "attempts", "temperature",
               "server_unit", "stop_default", "backend_port"}
    if set(setting) - allowed:
        raise Refused("semantic policy has unknown settings")
    bench = (ctx.conf("bench.run").get("benchmarks") or {}).get(name)
    if not isinstance(bench, dict):
        raise Refused("semantic benchmark is not registered")
    argv, grade_argv, result = bench.get("argv"), bench.get("grade_argv"), bench.get("result_file")
    if (not isinstance(argv, list) or any(not isinstance(a, str) for a in argv)
            or not isinstance(result, str) or not result or Path(result).is_absolute()
            or ".." in Path(result).parts or argv.count("--emit-prompts") != 1
            or argv.index("--emit-prompts") + 1 >= len(argv)
            or argv[argv.index("--emit-prompts") + 1] != result
            or "--grade" in argv or not isinstance(grade_argv, list)
            or any(not isinstance(a, str) for a in grade_argv)
            or grade_argv.count("--grade") != 1 or "--emit-prompts" in grade_argv
            or grade_argv.index("--grade") + 1 >= len(grade_argv)
            or grade_argv[grade_argv.index("--grade") + 1] != "{replies_file}"
            or "secret_env" in bench):
        raise Refused("semantic benchmark policy is invalid")
    _loopback_base(setting.get("base_url"))
    model = setting.get("model")
    if not isinstance(model, str) or not _EVAL_ID_RE.fullmatch(model):
        raise Refused("semantic model is invalid")
    for key, low, high, default in (("max_tokens", 1, 8192, None),
                                     ("per_request_timeout_s", 1, 1800, None),
                                     ("timeout_s", 1, 28800, None),
                                     ("attempts", 1, 8, None)):
        value = setting.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise Refused(f"semantic {key} is invalid")
    attempts = setting["attempts"]
    temperature = setting.get("temperature")
    if (type(temperature) not in (int, float) or not 0 <= temperature <= 2
            or (attempts == 1 and temperature != 0)):
        raise Refused("semantic temperature is invalid")
    if setting.get("thinking") not in ("on", "off"):
        raise Refused("semantic thinking is invalid")
    if "verification" in setting and type(setting["verification"]) is not bool:
        raise Refused("semantic verification is invalid")
    if type(setting.get("stop_default", False)) is not bool:
        raise Refused("semantic stop_default is invalid")
    unit = setting.get("server_unit")
    if unit is not None:
        units = ctx.conf("service.control").get("units")
        if not isinstance(unit, str) or not isinstance(units, list) or unit not in units:
            raise Refused("semantic server_unit is not allowed")
        limited = ctx.conf("service.control").get("actions")
        limited = limited if isinstance(limited, dict) else OWNER_ACTIONS
        if unit in limited and any(a not in limited[unit] for a in ("start", "stop")):
            raise Refused("semantic server_unit actions are not allowed")
        if not setting.get("stop_default", False):
            raise Refused("semantic server_unit requires stop_default")
    port = setting.get("backend_port")
    if (setting.get("stop_default", False) or port is not None) and (
            type(port) is not int or not 1 <= port <= 65535):
        raise Refused("semantic backend_port is invalid")
    return bench, setting


def _semantic_prompts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise Refused("semantic prompts are invalid")
    names: set[str] = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {"name", "prompt", "category", "answer"}:
            raise Refused("semantic prompt row is invalid")
        name, prompt, category, answer = (row[k] for k in ("name", "prompt", "category", "answer"))
        if not isinstance(name, str) or not _EVAL_ID_RE.fullmatch(name) or name in names:
            raise Refused("semantic prompt name is invalid")
        names.add(name)
        if not isinstance(prompt, str) or not 100 <= len(prompt) <= 16000 or "\x00" in prompt:
            raise Refused("semantic prompt text is invalid")
        if category not in ("validity_domain", "contradiction", "insufficient_data", "control"):
            raise Refused("semantic prompt category is invalid")
        if (not isinstance(answer, dict) or set(answer) != {"verdict", "options", "missing"}
                or answer["verdict"] not in ("no_issue", "invalid_assumption", "cannot_conclude", "contradiction")):
            raise Refused("semantic answer is invalid")
        for field in ("options", "missing"):
            labels = answer[field]
            if (not isinstance(labels, list) or len(labels) > 20
                    or any(not isinstance(label, str) or not _EVAL_ID_RE.fullmatch(label) for label in labels)
                    or len(labels) != len(set(labels))):
                raise Refused("semantic answer labels are invalid")
    return value


@contextmanager
def _semantic_lock(paths: Paths):
    fd = open_dir(paths.state, create=True)
    try:
        lock = os.open("semantic-eval.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    finally:
        os.close(fd)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("another semantic evaluation is running") from None
        yield
    finally:
        os.close(lock)


class _SemanticHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> Any:
        class Connection(http.client.HTTPConnection):
            def connect(self) -> None:
                original = self.timeout
                self.timeout = min(original, SEMANTIC_CONNECT_S)
                try:
                    super().connect()
                finally:
                    self.timeout = original
                self.sock.settimeout(original)
                req._semantic_socket = self.sock

        return self.do_open(Connection, req)


def _semantic_http(opener: Any, url: str, deadline: Callable[[], float], *,
                   data: bytes | None = None, token: str | None = None,
                   cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET", headers=headers)
    if cancelled is not None and cancelled():
        raise Cancelled({})
    end = time.monotonic() + deadline()
    stopped = threading.Event()
    timed_out = threading.Event()
    was_cancelled = threading.Event()

    def watch() -> None:
        while not stopped.is_set():
            if cancelled is not None and cancelled():
                was_cancelled.set()
            if time.monotonic() >= end:
                timed_out.set()
            if timed_out.is_set() or was_cancelled.is_set():
                sock = getattr(req, "_semantic_socket", None)
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            wait = 0.25 if timed_out.is_set() or was_cancelled.is_set() else min(0.25, max(0, end - time.monotonic()))
            stopped.wait(wait)

    watchdog = threading.Thread(target=watch, daemon=True)
    watchdog.start()
    result: dict[str, Any] = {}
    failure: BaseException | None = None
    try:
        try:
            with opener.open(req, timeout=deadline()) as response:
                status = response.status
                if type(status) is not int or not 100 <= status <= 599:
                    raise Failed("local HTTP returned an invalid status", {})
                if status != 200:
                    raise Failed(f"local HTTP returned status {status}", {})
                if data is None and not url.endswith(("/local-ai/runtime/llama-cpp", "/models")):
                    result = {}
                else:
                    raw = _eval_response_body(response, deadline)
                    result = json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            status = exc.code if type(exc.code) is int and 100 <= exc.code <= 599 else "invalid"
            raise Failed(f"local HTTP returned status {status}", {}) from None
        except (OSError, urllib.error.URLError):
            deadline()
            raise Failed("local HTTP endpoint is unavailable", {}) from None
        except (ValueError, TypeError):
            raise Failed("local HTTP response is invalid", {}) from None
    except BaseException as exc:
        failure = exc
    finally:
        stopped.set()
        watchdog.join()
    if timed_out.is_set():
        deadline()
    if was_cancelled.is_set() or (cancelled is not None and cancelled()):
        raise Cancelled({})
    deadline()
    if failure is not None:
        raise failure
    return result


def _semantic_unit(ctx: Context, action: str, unit: str, remaining: Callable[[], float],
                   *, check_cancel: bool = True) -> None:
    if check_cancel and ctx.cancelled():
        raise Cancelled({})
    proc = _systemctl(ctx, action, unit, timeout=min(60, remaining()))
    want = "active" if action == "start" else "inactive"
    if proc.returncode != 0:
        raise Failed(f"candidate server {action} failed", {})
    while True:
        if check_cancel and ctx.cancelled():
            raise Cancelled({})
        remaining()
        state = _unit_state(ctx, unit, timeout=min(30, remaining())).get("ActiveState")
        if state == want or (action == "stop" and state == "failed"):
            return
        if state == "failed":
            raise Failed("candidate server start failed", {})
        if check_cancel and ctx.cancelled():
            raise Cancelled({})
        time.sleep(min(1, remaining()))


def _semantic_memory(stop: threading.Event, peaks: dict[str, int | None]) -> None:
    while not stop.is_set():
        try:
            values = Path("/proc/meminfo").read_text().splitlines()
            mem = {line.split(":")[0]: int(line.split(":")[1].strip().split()[0]) for line in values}
            used = (mem["MemTotal"] - mem["MemAvailable"]) * 1024
            peaks["ram_bytes"] = max(peaks["ram_bytes"] or 0, used)
        except (OSError, ValueError, KeyError, IndexError):
            pass
        try:
            proc = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                  capture_output=True, text=True, timeout=5, check=False, env=_host_env())
            if proc.returncode == 0:
                used = sum(int(value.strip()) for value in proc.stdout.splitlines())
                peaks["vram_mib"] = max(peaks["vram_mib"] or 0, used)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        stop.wait(0.25)


def _semantic_grade(value: Any, prompts: list[dict[str, Any]], attempts: int) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"cases", "categories"}:
        raise Failed("semantic grader output is invalid", {})
    cases, categories = value["cases"], value["categories"]
    if not isinstance(cases, dict) or set(cases) != {row["name"] for row in prompts}:
        raise Failed("semantic grader cases are invalid", {})
    expected_categories: dict[str, int] = {}
    for row in prompts:
        expected_categories[row["category"]] = expected_categories.get(row["category"], 0) + 1
    if not isinstance(categories, dict) or set(categories) != set(expected_categories):
        raise Failed("semantic grader categories are invalid", {})
    for category, entry in categories.items():
        if (not isinstance(entry, dict) or set(entry) != {"score", "count"}
                or type(entry["score"]) is not int or type(entry["count"]) is not int
                or not 0 <= entry["score"] <= entry["count"] == expected_categories[category]):
            raise Failed("semantic grader totals are invalid", {})
    def valid_grade(item: Any) -> bool:
        return (isinstance(item, dict)
                and set(item) == {"verdict", "options", "missing", "verdict_ok", "options_ok",
                                  "missing_ok", "score", "parse_error"}
                and (item["verdict"] is None or item["verdict"] in (
                    "no_issue", "invalid_assumption", "cannot_conclude", "contradiction"))
                and all(item[k] is None or (isinstance(item[k], list)
                    and len(item[k]) <= 26
                    and all(isinstance(v, str) and re.fullmatch(r"[A-Z]\Z", v) for v in item[k])
                    and len(set(item[k])) == len(item[k])) for k in ("options", "missing"))
                and all(type(item[k]) is bool for k in ("verdict_ok", "options_ok", "missing_ok", "parse_error"))
                and type(item["score"]) is int and item["score"] in (0, 1))
    for case in cases.values():
        if (not isinstance(case, dict) or set(case) != {"attempts", "majority", "disagreement",
                                                        "attempts_scored", "score"}
                or not isinstance(case["attempts"], list) or len(case["attempts"]) != attempts
                or not all(valid_grade(item) for item in case["attempts"])
                or not isinstance(case["majority"], dict)
                or set(case["majority"]) != {"verdict", "options", "missing", "score"}
                or not valid_grade({**case["attempts"][0], **case["majority"]})
                or type(case["disagreement"]) is not bool
                or type(case["attempts_scored"]) is not int
                or case["attempts_scored"] != sum(item["score"] for item in case["attempts"])
                or type(case["score"]) is not int or case["score"] != case["majority"]["score"]):
            raise Failed("semantic grader case is invalid", {})
        def key(item: dict[str, Any]) -> tuple[Any, ...]:
            return (item["verdict"], tuple(item["options"]) if item["options"] is not None else None,
                    tuple(item["missing"]) if item["missing"] is not None else None)
        keys = [key(item) for item in case["attempts"]]
        top = max(keys.count(item) for item in keys)
        leaders = {item for item in keys if keys.count(item) == top}
        first = (case["attempts"][keys.index(next(iter(leaders)))]
                 if len(leaders) == 1 and top > attempts / 2 else None)
        expected = ({k: first[k] for k in ("verdict", "options", "missing", "score")}
                    if first else {"verdict": None, "options": None, "missing": None, "score": 0})
        if (case["majority"] != expected
                or case["disagreement"] != (len(set(keys)) > 1)):
            raise Failed("semantic grader majority is inconsistent", {})
    for category in categories:
        total = sum(cases[row["name"]]["score"] for row in prompts if row["category"] == category)
        if categories[category]["score"] != total:
            raise Failed("semantic grader totals are inconsistent", {})
    return cases, categories


def _semantic_check_result(detail: dict[str, Any], token: str, prompts: list[dict[str, Any]],
                           replies: dict[str, list[str]], restore: dict[str, str]) -> None:
    encoded = json.dumps(detail, ensure_ascii=False)
    windows = {encoded[index:index + 32] for index in range(len(encoded) - 31)}
    if (token in encoded or any(row["prompt"] in encoded for row in prompts)
            or any(row["prompt"][index:index + 32] in windows
                   for row in prompts for index in range(len(row["prompt"]) - 31))
            or any(len(text) >= 32 and text in encoded
                   for texts in replies.values() for text in texts)):
        raise Failed("semantic evaluation result is invalid", {"restore": restore})


def _op_semantic_eval(ctx: Context, name: str, setting: dict[str, Any]) -> dict[str, Any]:
    bench, _ = _semantic_settings(ctx, name, setting)
    sha = ctx.request.get("sha")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise Refused("local evaluation needs a candidate sha")
    deadline = time.monotonic() + setting["timeout_s"]
    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise Failed("local evaluation exceeded its time limit", {})
        return left
    with _semantic_lock(ctx.paths):
        _eval_reachable(sha, remaining)
        with _eval_tempdir(ctx.paths.home / "src" / "worktrees") as root:
            files, directories = _eval_checkout(sha, root, remaining)
            sub = str(bench.get("cwd") or ".")
            result_name = bench["result_file"]
            if Path(sub).is_absolute() or ".." in Path(sub).parts or not (root / sub).is_dir():
                raise Refused("semantic benchmark cwd is invalid")
            result = Path(sub) / result_name
            _eval_check_checkout(root, files, directories, result)
            try:
                phase = _run_benchmark(ctx, name, bench, root, sub, root / result, sha, None,
                                       scrub_result=False, close_scope=True, deadline=deadline)
                _eval_check_checkout(root, files, directories, result)
                prompts = _semantic_prompts(phase["result"])
            except Cancelled:
                raise Cancelled({}) from None
            except (Failed, Refused):
                raise Refused("semantic prompt phase failed") from None
            (root / result).unlink()
            _eval_check_checkout(root, files, directories, result)
            token = _eval_credential(setting.get("credential_file"), ctx.paths)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects(),
                                                 _SemanticHTTPHandler())
            stop_default = setting.get("stop_default", False)
            unit = setting.get("server_unit")
            backend_url = f"http://127.0.0.1:{setting.get('backend_port')}/local-ai/runtime/llama-cpp"
            default_stopped = False
            unit_started = False
            restore = {"default": "not_needed", "server_unit": "not_needed"}
            replies: dict[str, list[str]] = {row["name"]: [] for row in prompts}
            replies_bytes = 0
            measurements: dict[str, list[dict[str, Any]]] = {row["name"]: [] for row in prompts}
            peaks: dict[str, int | None] = {"ram_bytes": None, "vram_mib": None}
            stop_sample = threading.Event()
            sampler: threading.Thread | None = None
            sampler_started = False
            readiness_errors = 0
            restarts = 0
            initial_restarts: int | None = None
            error: BaseException | None = None
            try:
                if stop_default:
                    status = _semantic_http(opener, backend_url, remaining, cancelled=ctx.cancelled)
                    def valid_default_status(value: Any) -> bool:
                        return (isinstance(value, dict)
                                and type(value.get("runtime_reachable")) is bool
                                and ("spawned_by_jarvis" not in value
                                     or type(value["spawned_by_jarvis"]) is bool)
                                and (value.get("pid") is None or type(value["pid"]) is int))
                    if not valid_default_status(status):
                        raise Failed("default runtime status is invalid", {})
                    if status.get("reason_code") == "LLAMACPP_RECOVERING":
                        raise Refused("default runtime is recovering")
                    if (status["runtime_reachable"] or status.get("spawned_by_jarvis") is True
                            or status.get("pid") is not None
                            or status.get("reason_code") == "LLAMACPP_LOADING"):
                        default_stopped = True
                        stopped = _semantic_http(opener, backend_url + "/stop", remaining,
                                                 data=b"{}", cancelled=ctx.cancelled)
                        if not isinstance(stopped, dict) or type(stopped.get("runtime_reachable")) is not bool:
                            stopped = _semantic_http(opener, backend_url, remaining, cancelled=ctx.cancelled)
                        if not valid_default_status(stopped):
                            raise Failed("default runtime status is invalid", {})
                        if (stopped["runtime_reachable"] or stopped.get("spawned_by_jarvis") is True
                                or stopped.get("pid") is not None
                                or stopped.get("reason_code") in ("LLAMACPP_LOADING", "LLAMACPP_RECOVERING")):
                            raise Failed("default runtime stop was not confirmed", {})
                if unit:
                    unit_started = True
                    initial = _unit_state(ctx, unit, timeout=min(30, remaining())).get("NRestarts", "0")
                    initial_restarts = int(initial) if initial.isdecimal() else None
                    _semantic_unit(ctx, "start", unit, remaining)
                while True:
                    if ctx.cancelled():
                        raise Cancelled({})
                    try:
                        models = _semantic_http(opener, setting["base_url"] + "/models", remaining,
                                                token=token, cancelled=ctx.cancelled)
                        if (not isinstance(models, dict) or not isinstance(models.get("data"), list)
                                or not any(isinstance(model, dict) and model.get("id") == setting["model"]
                                           for model in models["data"])):
                            raise Failed("local model is not ready", {})
                        break
                    except Failed:
                        readiness_errors = min(readiness_errors + 1, 1_000_000)
                        if ctx.cancelled():
                            raise Cancelled({}) from None
                        time.sleep(min(1, remaining()))
                sampler = threading.Thread(target=_semantic_memory, args=(stop_sample, peaks), daemon=True)
                sampler.start()
                sampler_started = True
                for row in prompts:
                    for attempt in range(setting["attempts"]):
                        remaining()
                        if ctx.cancelled():
                            raise Cancelled({})
                        per_deadline = min(deadline, time.monotonic() + setting["per_request_timeout_s"])
                        def per_remaining(limit: float = per_deadline) -> float:
                            left = limit - time.monotonic()
                            if left <= 0:
                                raise Failed("local model request exceeded its time limit", {})
                            return left
                        kwargs = {"enable_thinking": setting["thinking"] == "on"}
                        if "verification" in setting:
                            kwargs["verification"] = setting["verification"]
                        body = json.dumps({"model": setting["model"], "messages": [
                            {"role": "user", "content": row["prompt"]}], "temperature": setting["temperature"],
                            "max_tokens": setting["max_tokens"], "seed": attempt, "stream": False,
                            "chat_template_kwargs": kwargs}).encode()
                        started = time.monotonic()
                        payload = _semantic_http(opener, setting["base_url"] + "/chat/completions",
                                                 per_remaining, data=body, token=token, cancelled=ctx.cancelled)
                        try:
                            message = payload["choices"][0]["message"]
                            content = message["content"]
                            reply = "" if content is None else content
                            tokens = payload["usage"]["completion_tokens"]
                            if not isinstance(reply, str) or type(tokens) is not int or tokens < 0:
                                raise ValueError
                            reasoning_content = message.get("reasoning_content")
                            if reasoning_content is not None and not isinstance(reasoning_content, str):
                                raise ValueError
                            reasoning = bool(reasoning_content)
                        except (KeyError, IndexError, TypeError, ValueError):
                            raise Failed("local model response is invalid", {}) from None
                        replies[row["name"]].append(reply)
                        replies_bytes += len(reply.encode("utf-8"))
                        if replies_bytes > SEMANTIC_REPLIES_MAX_BYTES:
                            raise Failed("local model replies exceeded their size limit", {})
                        measurements[row["name"]].append({"completion_tokens": tokens,
                                                           "reasoning_present": reasoning,
                                                           "seconds": round(time.monotonic() - started, 2)})
                remaining()
            except Exception as exc:  # noqa: BLE001 - restore must run after every operation failure
                error = exc
            finally:
                restore_deadline = time.monotonic() + SEMANTIC_RESTORE_S
                def restore_remaining() -> float:
                    left = restore_deadline - time.monotonic()
                    if left <= 0:
                        raise Failed("semantic restore exceeded its time limit", {})
                    return left
                stop_sample.set()
                if sampler_started and sampler is not None:
                    try:
                        sampler.join(timeout=6)
                    except Exception:  # noqa: BLE001 - still stop the unit and restore the default
                        if error is None:
                            error = Failed("semantic memory sampler cleanup failed", {})
                if unit_started:
                    try:
                        current = _unit_state(ctx, unit, timeout=min(30, restore_remaining())).get("NRestarts", "0")
                        if initial_restarts is not None and current.isdecimal():
                            restarts = min(max(int(current) - initial_restarts, 0), 1_000_000)
                    except Exception:  # noqa: BLE001 - still stop the unit and restore the default
                        restarts = 0
                    try:
                        _semantic_unit(ctx, "stop", unit, restore_remaining, check_cancel=False)
                        restore["server_unit"] = "ok"
                    except Exception:  # noqa: BLE001 - report failed restore without starting another model
                        restore["server_unit"] = "failed"
                if default_stopped and restore["server_unit"] == "failed":
                    restore["default"] = "failed"
                elif default_stopped:
                    try:
                        restore_deadline = time.monotonic() + SEMANTIC_RESTORE_S
                        started = _semantic_http(opener, backend_url + "/start", restore_remaining,
                                                 data=b"{}")
                        if started.get("reason_code") in ("LLAMACPP_NOT_CONFIGURED", "LLAMACPP_FILE_MISSING",
                                                          "LLAMACPP_UNSUPPORTED_WINDOWS_WSL_TOPOLOGY",
                                                          "LLAMACPP_NON_LOOPBACK_ENDPOINT"):
                            raise Failed("default runtime cannot start", {})
                        while True:
                            status = _semantic_http(opener, backend_url, restore_remaining)
                            if status.get("runtime_reachable") is True:
                                break
                            time.sleep(min(1, restore_remaining()))
                        restore["default"] = "ok"
                    except Exception:  # noqa: BLE001 - report failed restore without leaking response text
                        restore["default"] = "failed"
            if "failed" in restore.values():
                raise Failed("semantic evaluation restore failed", {"restore": restore}) from None
            if error is not None:
                if isinstance(error, Refused):
                    raise Refused(str(error)) from None
                if isinstance(error, Failed):
                    raise Failed(str(error), {"restore": restore}) from None
                if isinstance(error, Cancelled):
                    raise Cancelled({"restore": restore}) from None
                raise Failed("semantic evaluation failed", {"restore": restore}) from None
            reply_file = "semantic-replies.json"
            if (root / sub / reply_file).exists() or Path(sub, reply_file) in files:
                raise Refused("semantic replies path is occupied")
            reply_fd = open_dir(root / sub)
            try:
                write_at(reply_fd, reply_file, json.dumps(replies).encode(), mode=0o600)
            finally:
                os.close(reply_fd)
            grade_bench = {**bench, "argv": [reply_file if arg == "{replies_file}" else arg
                                              for arg in bench["grade_argv"]], "result_file": None}
            try:
                graded = _run_benchmark(ctx, name, grade_bench, root, sub, None, sha, None,
                                        close_scope=True, deadline=deadline, json_stdout=True)
                _eval_check_checkout(root, files, directories, {Path(sub, reply_file)})
                cases, categories = _semantic_grade(graded["result"], prompts, setting["attempts"])
            except Cancelled:
                raise Cancelled({"restore": restore}) from None
            except (Failed, Refused):
                raise Failed("semantic grading failed", {"restore": restore}) from None
            finally:
                reply_fd = open_dir(root / sub)
                try:
                    unlink_at(reply_fd, reply_file)
                finally:
                    os.close(reply_fd)
            rows = []
            total_tokens = 0
            total_seconds = 0.0
            for index, row in enumerate(prompts, 1):
                grades = cases[row["name"]]
                attempts = []
                for grade, measure in zip(grades["attempts"], measurements[row["name"]], strict=True):
                    attempts.append({**{k: grade[k] for k in ("verdict", "options", "missing", "score", "parse_error")},
                                     **measure})
                    total_tokens += measure["completion_tokens"]
                    total_seconds += measure["seconds"]
                rows.append({"case": f"case-{index:02d}", "category": row["category"], "attempts": attempts,
                             "majority": grades["majority"], "disagreement": grades["disagreement"],
                             "attempts_scored": grades["attempts_scored"], "score": grades["score"]})
            detail = {"model": setting["model"], "head": sha, "rows": rows, "categories": categories,
                      "completion_tokens": total_tokens,
                      "tokens_per_second": round(total_tokens / total_seconds, 2) if total_seconds else None,
                      "peak_vram_mib": peaks["vram_mib"], "peak_ram_bytes": peaks["ram_bytes"],
                      "errors": readiness_errors, "restarts": restarts, "restore": restore}
            _semantic_check_result(detail, token, prompts, replies, restore)
            return detail


def op_local_eval_run(ctx: Context) -> dict[str, Any]:
    conf = ctx.conf("local_eval.run")
    args = _args(ctx, {"bench"})
    bench_name = args.get("bench")
    evaluations = conf.get("evaluations")
    evaluation = evaluations.get(bench_name) if isinstance(evaluations, dict) and isinstance(bench_name, str) else None
    if not isinstance(evaluation, dict):
        raise Refused("local evaluator is not registered")
    kind = evaluation.get("kind", "pointing")
    if kind == "semantic_judgment":
        return _op_semantic_eval(ctx, bench_name, evaluation)
    if kind != "pointing":
        raise Refused("local evaluator kind is invalid")
    bench_conf = ctx.conf("bench.run")
    bench = (bench_conf.get("benchmarks") or {}).get(bench_name)
    if not isinstance(bench, dict) or not isinstance(bench.get("argv"), list):
        raise Refused("local evaluator benchmark is not registered")
    result_file = bench.get("result_file")
    argv = bench["argv"]
    if (not isinstance(result_file, str) or not result_file
            or argv.count("--emit-prompts") != 1
            or argv.index("--emit-prompts") + 1 >= len(argv)
            or argv[argv.index("--emit-prompts") + 1] != result_file
            or argv.count("--ids") != 1 or argv.index("--ids") + 1 >= len(argv)
            or argv[argv.index("--ids") + 1] != evaluation.get("ids")):
        raise Refused("local evaluator must export registered prompts and ids")
    base_url = _loopback_base(evaluation.get("base_url"))
    model = evaluation.get("model")
    ids = evaluation.get("ids")
    label = evaluation.get("label")
    max_tokens = evaluation.get("max_tokens")
    timeout_s = evaluation.get("timeout_s", 300)
    if (not isinstance(model, str) or not _EVAL_ID_RE.fullmatch(model)
            or ids not in ("old", "new") or not isinstance(label, str) or not _EVAL_ID_RE.fullmatch(label)
            or type(max_tokens) is not int or not 1 <= max_tokens <= 4096
            or type(timeout_s) is not int or not 1 <= timeout_s <= 300):
        raise Refused("local evaluator policy settings are invalid")
    sha = ctx.request.get("sha")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise Refused("local evaluation needs a candidate sha")
    deadline = time.monotonic() + timeout_s

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise Failed("local evaluation exceeded its time limit", {})
        return left

    _eval_reachable(sha, remaining)
    try:
        with _eval_tempdir(ctx.paths.home / "src" / "worktrees") as root:
            files, directories = _eval_checkout(sha, root, remaining)
            sub = str(bench.get("cwd") or ".")
            if Path(sub).is_absolute() or ".." in Path(sub).parts or not (root / sub).is_dir():
                raise Refused("local evaluator benchmark cwd is invalid")
            if (not isinstance(result_file, str) or Path(result_file).is_absolute()
                    or ".." in Path(result_file).parts or "secret_env" in bench):
                raise Refused("local evaluator benchmark policy is invalid")
            result = Path(sub) / result_file
            _eval_check_checkout(root, files, directories, result)
            # A transient modify-and-restore race during the run needs a launcher-private bind to close.
            phase_one = _run_benchmark(ctx, bench_name, bench, root, sub, root / sub / result_file,
                                       sha, None, scrub_result=False, close_scope=True, deadline=deadline)
            _eval_check_checkout(root, files, directories, result)
            remaining()
        prompts = _eval_prompts(phase_one.get("result"))
    except Failed as exc:
        if str(exc) == "local evaluation exceeded its time limit" or time.monotonic() >= deadline:
            raise Failed("local evaluation exceeded its time limit", {}) from None
        raise Refused("local evaluator prompt phase failed validation or execution") from None
    except (Refused, Cancelled):
        raise Refused("local evaluator prompt phase failed validation or execution") from None
    remaining()
    token = _eval_credential(evaluation.get("credential_file"), ctx.paths)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())
    rows = []
    tally = {key: 0 for key in ("correct", "plausible", "wrong", "none")}
    for row in prompts:
        remaining()
        if ctx.cancelled():
            raise Cancelled({})
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": row["prompt"]}],
                           "temperature": 0, "max_tokens": max_tokens, "stream": False,
                           "chat_template_kwargs": {"enable_thinking": False}}).encode()
        req = urllib.request.Request(f"{base_url}/chat/completions", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        started = time.monotonic()
        try:
            with opener.open(req, timeout=min(remaining(), timeout_s)) as response:
                status = response.status
                if type(status) is not int or not 100 <= status <= 599:
                    raise Failed("local model returned an invalid HTTP status", {})
                if status != 200:
                    raise Failed(f"local model returned HTTP {status}", {})
                payload = json.loads(_eval_response_body(response, remaining))
            remaining()
            reply = payload["choices"][0]["message"]["content"] or ""
            if not isinstance(reply, str):
                raise TypeError("invalid content")
        except urllib.error.HTTPError as exc:
            status = exc.code if type(exc.code) is int and 100 <= exc.code <= 599 else "invalid"
            raise Failed(f"local model returned HTTP {status}", {}) from None
        except (OSError, urllib.error.URLError):
            remaining()
            raise Failed("local model is unavailable", {}) from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise Failed("local model response is invalid", {}) from None
        verdict, first = _eval_score(reply, row["correct"], row["plausible"])
        if first is not None and len(first) > 80:
            raise Failed("local model response is invalid", {})
        tally[verdict] += 1
        rows.append({"name": row["name"], "first": first, "verdict": verdict,
                     "seconds": round(time.monotonic() - started, 1)})
    remaining()
    detail = {"model": model, "base_url": base_url, "head": ctx.request["sha"], "ids": label,
              "tally": tally, "rows": rows}
    if token in json.dumps(detail, ensure_ascii=False):
        raise Failed("local evaluation result is invalid", {})
    return detail


class Cancelled(Exception):
    pass


OPERATIONS: dict[str, Callable[[Context], dict[str, Any]]] = {
    "slack.deliver": op_slack_deliver,
    "service.status": op_service_status,
    "service.control": op_service_control,
    "service.install": op_service_install,
    "wsl.probe": op_wsl_probe,
    "wsl.handoff": op_wsl_handoff,
    "browser.screenshot": op_browser_screenshot,
    "http.smoke": op_http_smoke,
    "bench.run": op_bench_run,
    "local_eval.run": op_local_eval_run,
}


# ---------------------------------------------------------------------------
# Executor


def _host_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("HOME", "USER", "LOGNAME", "LANG", "PATH", "WSL_INTEROP",
                                                         "WSL_DISTRO_NAME", "DBUS_SESSION_BUS_ADDRESS")}
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env["XDG_RUNTIME_DIR"] = os.environ.get("XDG_RUNTIME_DIR") or env["XDG_RUNTIME_DIR"]
    return env


def real_run(argv: list[str], timeout: float, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd, env=_host_env())


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect rejected", headers, fp)


def webhook_poster(paths: Paths) -> Callable[[str, bytes], int]:
    def post(text: str, _unused: bytes) -> int:
        st = paths.webhook.stat()
        if st.st_mode & 0o077 or st.st_uid != os.getuid():
            raise Failed("webhook file must be owned by this user and mode 600", {})
        url = paths.webhook.read_text().strip()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname != "hooks.slack.com" or parts.port is not None \
                or not parts.path.startswith("/services/") or parts.query or parts.fragment:
            raise Failed("webhook file does not hold a hooks.slack.com URL", {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())
        request = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=15) as response:
                response.read(256)
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (OSError, urllib.error.URLError) as exc:
            raise Failed(f"webhook unreachable: {type(exc).__name__}", {}) from None
    return post


def load_policy(paths: Paths) -> tuple[dict[str, Any], str]:
    st = paths.policy.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise RuntimeError("policy must be owned by this user and not group/world writable")
    raw = paths.policy.read_bytes()
    policy = json.loads(raw)
    if not isinstance(policy, dict) or policy.get("schema") != POLICY_SCHEMA or not isinstance(policy.get("ops"), dict):
        raise RuntimeError("policy has an unknown schema")
    unknown = set(policy["ops"]) - set(OPERATIONS)
    if unknown:
        raise RuntimeError(f"policy enables unknown operations {sorted(unknown)}")
    return policy, hashlib.sha256(raw).hexdigest()


def executor_version() -> str:
    """The installed release directory name is the master commit the executor was installed from."""
    here = Path(__file__).resolve().parent.parent.name
    return here if SHA_RE.fullmatch(here) else "uninstalled"


TZ_OFFSETS: dict[str, int] = {
    "UTC": 0,
    "GMT": 0,
    "Z": 0,
    "CET": 3600,
    "CEST": 7200,
    "BST": 3600,
    "EET": 7200,
    "EEST": 10800,
    "WET": 0,
    "WEST": 3600,
    "EST": -18000,
    "EDT": -14400,
    "CST": -21600,
    "CDT": -18000,
    "MST": -25200,
    "MDT": -21600,
    "PST": -28800,
    "PDT": -25200,
}


def parse_systemd_ts(val: str) -> datetime | None:
    if not isinstance(val, str) or not val.strip():
        return None
    val = val.strip()
    parts = val.split()
    if len(parts) >= 3 and len(parts[0]) == 3 and parts[0].isalpha():
        val = " ".join(parts[1:])
    tokens = val.split()
    if len(tokens) >= 3:
        tz_str = tokens[-1]
        base = " ".join(tokens[:-1])
        offset = None
        if tz_str in TZ_OFFSETS:
            offset = TZ_OFFSETS[tz_str]
        elif hasattr(time, "tzname"):
            if tz_str == time.tzname[0]:
                offset = -time.timezone
            elif time.daylight and len(time.tzname) > 1 and tz_str == time.tzname[1]:
                offset = -getattr(time, "altzone", time.timezone)
        if offset is not None:
            for date_fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(base, date_fmt)
                    return dt.replace(tzinfo=timezone(timedelta(seconds=offset))).astimezone(UTC)
                except ValueError:
                    pass
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(val, fmt)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except ValueError:
            pass
    if val.isdigit():
        try:
            return datetime.fromtimestamp(int(val), tz=UTC)
        except (ValueError, OSError):
            pass
    return None


def switch_published_release(paths: Paths, sha: str) -> None:
    pub_release_dir = paths.published / "releases" / sha
    if not pub_release_dir.is_dir():
        raise Refused(f"published release directory missing for {sha}")
    tmp_link = paths.published / f".current.{secrets.token_hex(4)}"
    try:
        os.symlink(f"releases/{sha}", tmp_link)
        os.replace(tmp_link, paths.published / "current")
    finally:
        tmp_link.unlink(missing_ok=True)


def switch_install_release(paths: Paths, sha: str) -> None:
    release_dir = paths.install / "releases" / sha
    if not release_dir.is_dir():
        raise Refused(f"install release directory missing for {sha}")
    tmp_link = paths.install / f".current.{secrets.token_hex(4)}"
    try:
        os.symlink(release_dir, tmp_link)
        os.replace(tmp_link, paths.install / "current")
    finally:
        tmp_link.unlink(missing_ok=True)


def publish_release(paths: Paths, sha: str, *, switch_current: bool = True) -> Path:
    source_scripts = paths.install / "releases" / sha / "scripts"
    if not source_scripts.is_dir():
        raise Refused(f"installed release scripts missing for {sha}")
    pub_releases = paths.published / "releases"
    pub_releases.mkdir(parents=True, exist_ok=True)
    target_release = pub_releases / sha

    if not target_release.exists():
        with tempfile.TemporaryDirectory(dir=pub_releases, prefix=".publish-") as tmp:
            stage_dir = Path(tmp) / sha
            stage_scripts = stage_dir / "scripts"
            shutil.copytree(source_scripts, stage_scripts, symlinks=True)
            for root, dirs, files in os.walk(stage_dir):
                for f in files:
                    p = Path(root) / f
                    mode = p.stat().st_mode
                    p.chmod(0o555 if mode & 0o111 else 0o444)
                for d in dirs:
                    (Path(root) / d).chmod(0o755)
            stage_dir.chmod(0o755)
            os.rename(stage_dir, target_release)

    if switch_current:
        switch_published_release(paths, sha)
    return target_release


def return_published_release(paths: Paths, previous_sha: str) -> bool:
    target_release = paths.published / "releases" / previous_sha
    if not target_release.is_dir():
        if (paths.install / "releases" / previous_sha / "scripts").is_dir():
            try:
                publish_release(paths, previous_sha, switch_current=True)
                return True
            except Exception:  # noqa: BLE001
                return False
        return False
    try:
        switch_published_release(paths, previous_sha)
        return True
    except Exception:  # noqa: BLE001
        return False


def restore_release_links(paths: Paths, previous_sha: str | None) -> None:
    if previous_sha and (paths.install / "releases" / previous_sha).is_dir():
        try:
            switch_install_release(paths, previous_sha)
        except Exception:  # noqa: BLE001
            (paths.install / "current").unlink(missing_ok=True)
    else:
        (paths.install / "current").unlink(missing_ok=True)

    if previous_sha:
        ok = return_published_release(paths, previous_sha)
        if not ok:
            (paths.published / "current").unlink(missing_ok=True)
    else:
        (paths.published / "current").unlink(missing_ok=True)


def write_release_pending(
    paths: Paths,
    *,
    previous_sha: str | None,
    candidate_sha: str,
    deadline: str,
    request_id: str | None,
    attempts: int = 0,
    watcher_active: bool = False,
    owner_active: bool = False,
    now: datetime | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    ts = now or now_utc()
    marker = {
        "previous_sha": previous_sha,
        "candidate_sha": candidate_sha,
        "deadline": deadline,
        "request_id": request_id,
        "attempts": attempts,
        "watcher_active": bool(watcher_active),
        "created_at": iso(ts),
    }
    if owner_active:
        marker["owner_active"] = True
    if kind is not None:
        marker["kind"] = kind
    state_fd = open_dir(paths.state, create=True)
    try:
        write_at(state_fd, "release-pending.json", canonical(marker))
    finally:
        os.close(state_fd)
    return marker


class MalformedPendingRelease(Exception):
    """Raised when release-pending.json exists but is corrupt, unreadable or malformed."""

    def __init__(self, message: str, raw_data: bytes | None = None):
        super().__init__(message)
        self.raw_data = raw_data


def load_release_pending(paths: Paths) -> dict[str, Any] | None:
    path = paths.release_pending
    if not path.exists():
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MalformedPendingRelease(f"cannot read pending marker: {exc}") from exc
    try:
        marker = json.loads(data)
    except (json.JSONDecodeError, ValueError) as exc:
        raise MalformedPendingRelease(f"unparseable JSON in pending marker: {exc}", raw_data=data) from exc
    if not isinstance(marker, dict):
        raise MalformedPendingRelease(f"pending marker is not a JSON object ({type(marker).__name__})", raw_data=data)
    candidate = marker.get("candidate_sha")
    if not candidate or not isinstance(candidate, str) or not SHA_RE.fullmatch(candidate):
        raise MalformedPendingRelease(f"pending marker missing valid candidate_sha: {candidate!r}", raw_data=data)
    return marker


def _quarantine_pending_marker(paths: Paths) -> Path | None:
    path = paths.release_pending
    if not path.exists():
        return None
    quarantine_name = f"release-pending.corrupt.{secrets.token_hex(4)}"
    dest = paths.state / quarantine_name
    try:
        path.rename(dest)
        return dest
    except OSError:
        path.unlink(missing_ok=True)
        return None


def notify_return(
    paths: Paths,
    candidate_sha: str,
    reason: str,
    *,
    post: Callable[[str, bytes], int] | None = None,
    kind: str | None = None,
) -> bool:
    for h_entry in read_release_history(paths):
        if h_entry.get("candidate_sha") == candidate_sha and h_entry.get("notified") is True:
            return False
    short_sha = candidate_sha[:8] if isinstance(candidate_sha, str) else "unknown"
    if kind == "return":
        text = f":warning: *host-ops* returned release {short_sha} failed confirmation: {reason}."
    else:
        text = f":warning: *host-ops* release {short_sha} failed: {reason}. Returned to previous release."
    poster = post or webhook_poster(paths)
    try:
        status = poster(text, b"")
        return 200 <= status < 300
    except Exception:  # noqa: BLE001 - failure to notify must not block rollback
        return False


def return_release(
    paths: Paths,
    *,
    marker: dict[str, Any] | None = None,
    reason: str = "unhealthy",
    post: Callable[[str, bytes], int] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = real_run,
    now: datetime | None = None,
    confirm_previous: bool = False,
) -> bool:
    current_time = now or now_utc()
    candidate_sha = marker.get("candidate_sha") if marker else None
    previous_sha = marker.get("previous_sha") if marker else None
    is_return_marker = bool(marker and marker.get("kind") == "return")

    # Resolve candidate if not in marker
    if not candidate_sha:
        current = paths.install / "current"
        if current.exists():
            try:
                candidate_sha = current.resolve().name
            except OSError:
                candidate_sha = None

    # Resolve previous_sha if not in marker (e.g. from guard RELEASE file), but never for a return confirmation
    if not is_return_marker and not previous_sha and (paths.guard / "RELEASE").is_file():
        try:
            pinned = (paths.guard / "RELEASE").read_text(encoding="utf-8").strip()
            if SHA_RE.fullmatch(pinned) and (paths.install / "releases" / pinned).is_dir():
                previous_sha = pinned
        except OSError:
            pass

    # Move current back to previous release if available (keep current where it is when kind == "return")
    if not is_return_marker:
        restore_release_links(paths, previous_sha)

    # Restore watcher if it was active
    if marker and marker.get("watcher_active"):
        unit = "jarvisos-relay-watch.service"
        try:
            run(["systemctl", "--user", "restart", unit], timeout=60)
        except Exception:  # noqa: BLE001, S110
            pass

    # Start owner on previous release if it was active
    if marker and marker.get("owner_active"):
        owner_unit = "jarvisos-mission-owner.service"
        try:
            run(["systemctl", "--user", "start", owner_unit], timeout=60)
        except Exception:  # noqa: BLE001, S110
            pass

    # Notification
    notified = False
    if candidate_sha and not (marker and marker.get("notified") is True):
        notified = notify_return(paths, candidate_sha, reason, post=post,
                                 kind=marker.get("kind") if marker else None)
    elif marker and marker.get("notified") is True:
        notified = True

    # Append refusal or return_unconfirmed to history
    entry = dict(marker) if marker else {}
    status = "return_unconfirmed" if is_return_marker else "refused"
    entry.update({
        "candidate_sha": candidate_sha,
        "previous_sha": previous_sha,
        "status": status,
        "reason": reason,
        "returned_at": iso(current_time),
        "notified": notified,
    })
    state_fd = open_dir(paths.state, create=True)
    try:
        with open(paths.release_history, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(state_fd)
    sync_published_release_history(paths)

    # Clean up or quarantine pending marker
    if marker is None and paths.release_pending.exists():
        _quarantine_pending_marker(paths)
    else:
        paths.release_pending.unlink(missing_ok=True)

    # Optionally set up confirmation for previous release (never when returning a return or missing previous release)
    if not is_return_marker and confirm_previous and previous_sha and (paths.install / "releases" / previous_sha).is_dir():
        deadline = iso(current_time + timedelta(minutes=10))
        write_release_pending(paths, previous_sha=None, candidate_sha=previous_sha,
                              deadline=deadline, request_id=None,
                              attempts=0, watcher_active=bool(marker and marker.get("watcher_active")),
                              now=current_time, kind="return")

    return True


def _guard_impl(
    paths: Paths,
    *,
    force_return: bool = False,
    reason: str | None = None,
    post: Callable[[str, bytes], int] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = real_run,
    now: datetime | None = None,
) -> int:
    current_time = now or now_utc()
    if force_return:
        if not paths.release_pending.exists():
            print("refusal: no release is pending", file=sys.stderr)
            return 2
        try:
            marker = load_release_pending(paths)
        except MalformedPendingRelease as exc:
            return_release(paths, reason=f"corrupt pending marker: {exc}", post=post, run=run, now=current_time)
            return 0
        return_release(
            paths,
            marker=marker,
            reason=reason or "manual return",
            post=post,
            run=run,
            now=current_time,
            confirm_previous=bool(marker and marker.get("previous_sha") and marker.get("kind") != "return"),
        )
        return 0

    if not paths.release_pending.exists():
        return 0

    try:
        marker = load_release_pending(paths)
    except MalformedPendingRelease as exc:
        return_release(paths, reason=f"corrupt pending marker: {exc}", post=post, run=run, now=current_time)
        return 0

    if marker is None:
        return 0

    # If candidate recorded failure, perform return_release now
    if unhealthy_reason := marker.get("unhealthy_reason"):
        return_release(
            paths,
            marker=marker,
            reason=str(unhealthy_reason),
            post=post,
            run=run,
            now=current_time,
            confirm_previous=bool(marker.get("previous_sha") and marker.get("kind") != "return"),
        )
        return 0

    # Increment attempts
    attempts = int(marker.get("attempts", 0)) + 1
    marker["attempts"] = attempts
    state_fd = open_dir(paths.state, create=True)
    try:
        write_at(state_fd, "release-pending.json", canonical(marker))
    finally:
        os.close(state_fd)

    # Start count check
    if attempts > 3:
        return_release(
            paths,
            marker=marker,
            reason=f"start count exceeded ({attempts})",
            post=post,
            run=run,
            now=current_time,
            confirm_previous=bool(marker.get("previous_sha") and marker.get("kind") != "return"),
        )
        return 0

    # Deadline check
    deadline_str = marker.get("deadline")
    if deadline_str:
        deadline_dt = parse_ts(deadline_str)
        if deadline_dt and current_time > deadline_dt:
            return_release(
                paths,
                marker=marker,
                reason="deadline expired",
                post=post,
                run=run,
                now=current_time,
                confirm_previous=bool(marker.get("previous_sha") and marker.get("kind") != "return"),
            )
            return 0

    return 0


def guard(
    paths: Paths,
    *,
    force_return: bool = False,
    reason: str | None = None,
    post: Callable[[str, bytes], int] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = real_run,
    now: datetime | None = None,
) -> int:
    try:
        return _guard_impl(paths, force_return=force_return, reason=reason, post=post, run=run, now=now)
    except Exception as exc:  # noqa: BLE001 - guard must never fail unit start
        print(f"hostops: guard error {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 0


def _pin_guard(paths: Paths, source_dir: Path, source_sha: str) -> None:
    guard_dir = paths.guard
    paths.install.mkdir(parents=True, exist_ok=True)
    tmp_guard = paths.install / f".guard-{secrets.token_hex(4)}"
    old_aside = paths.install / f".guard-old-{secrets.token_hex(4)}"
    shutil.copytree(source_dir, tmp_guard, symlinks=False)
    (tmp_guard / "RELEASE").write_text(f"{source_sha}\n", encoding="utf-8")
    has_old = False
    if guard_dir.exists() or guard_dir.is_symlink():
        os.rename(guard_dir, old_aside)
        has_old = True
    try:
        os.rename(tmp_guard, guard_dir)
    except BaseException:
        if has_old and not (guard_dir.exists() or guard_dir.is_symlink()):
            try:
                os.rename(old_aside, guard_dir)
            except OSError:
                # A partial copy could pass for a real guard and let the cleanup below delete the only good one;
                # a symlink either exists whole or not at all, and keeps the old copy in place.
                try:
                    guard_dir.symlink_to(old_aside)
                except OSError:
                    pass
        raise
    finally:
        if tmp_guard.exists() or tmp_guard.is_symlink():
            if tmp_guard.is_dir() and not tmp_guard.is_symlink():
                shutil.rmtree(tmp_guard, ignore_errors=True)
            else:
                tmp_guard.unlink(missing_ok=True)
        # Only clean up old_aside if guard_dir is safely in place as a real directory
        if (
            has_old
            and (guard_dir.exists() and not guard_dir.is_symlink())
            and (old_aside.exists() or old_aside.is_symlink())
        ):
            if old_aside.is_dir() and not old_aside.is_symlink():
                shutil.rmtree(old_aside, ignore_errors=True)
            else:
                old_aside.unlink(missing_ok=True)


def sync_published_release_history(paths: Paths) -> None:
    try:
        if paths.release_history.exists():
            paths.published.mkdir(parents=True, exist_ok=True)
            tmp = paths.published / f".release-history.{secrets.token_hex(4)}"
            tmp.write_bytes(paths.release_history.read_bytes())
            os.replace(tmp, paths.published / "release-history.jsonl")
    except OSError:
        pass


def read_release_history(paths: Paths) -> list[dict[str, Any]]:
    for candidate_file in (paths.release_history, paths.published / "release-history.jsonl"):
        try:
            lines = candidate_file.read_text(encoding="utf-8").splitlines()
            break
        except OSError:
            continue
    else:
        return []
    out = []
    for line in lines:
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                out.append(parsed)
        except json.JSONDecodeError:
            continue
    return out


class Executor:
    def __init__(self, paths: Paths, *, run: Callable[..., subprocess.CompletedProcess] = real_run,
                 post: Callable[[str, bytes], int] | None = None, now: Callable[[], datetime] = now_utc,
                 log: Callable[[str], None] = lambda m: print(m, file=sys.stderr, flush=True),
                 version: str | None = None):
        self.paths, self.run, self.now, self.log = paths, run, now, log
        self.post = post or webhook_poster(paths)
        self.restart_requested = False
        self._version = version
        self._watcher_seen_at: datetime | None = None
        self._watcher_initial_restarts: int | None = None

    @property
    def version(self) -> str:
        return self._version or executor_version()

    # Host-only durable state: one file per handled request id (replay protection and idempotency), an in-flight
    # marker (recovery) and a journal (rate limits, audit).
    def _state_fd(self, sub: str) -> int:
        return open_dir(self.paths.state / sub, create=True)

    def journal(self) -> list[dict[str, Any]]:
        try:
            lines = (self.paths.state / "journal.jsonl").read_text().splitlines()[-2000:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def _append_journal(self, entry: dict[str, Any]) -> None:
        os.close(open_dir(self.paths.state, create=True))
        with open(self.paths.state / "journal.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    def recover(self) -> None:
        """A request in flight when the executor died gets an `interrupted` receipt; it is never re-executed."""
        fd = self._state_fd("inflight")
        try:
            names = [n for n in os.listdir(fd) if REQUEST_NAME_RE.fullmatch(n)]
            for name in names:
                try:
                    request = json.loads(read_regular(fd, name, MAX_REQUEST_BYTES))
                except (OSError, ValueError):
                    request = {"id": name[:-5], "op": None, "args": {}, "sha": None, "requested_at": None}
                done_fd = self._state_fd("done")
                try:
                    if exists_at(done_fd, name[:-5]):
                        self._republish(done_fd, name[:-5])
                    else:
                        self._finish(request, "interrupted", {}, "the executor stopped while this request was running",
                                     started=None)
                finally:
                    os.close(done_fd)
                unlink_at(fd, name)
        finally:
            os.close(fd)

    def heartbeat(self, policy_sha: str | None, problem: str | None) -> None:
        fd = open_dir(self.paths.spool, create=True)
        try:
            write_at(fd, "heartbeat.json", canonical({"at": iso(self.now()), "version": self.version,
                                                       "policy_sha256": policy_sha, "problem": problem,
                                                       "operations": sorted(OPERATIONS)}))
        finally:
            os.close(fd)

    def step(self) -> int:
        """Handle every pending request once, oldest first. Returns how many were handled."""
        try:
            policy, policy_sha = load_policy(self.paths)
            problem = None
        except (OSError, ValueError, RuntimeError) as exc:
            policy, policy_sha, problem = {"ops": {}}, None, f"policy unusable: {exc}"
        if not self.paths.key.is_file():
            problem = "signing key missing"
        self.heartbeat(policy_sha, problem)
        if problem is not None and self.paths.release_pending.exists():
            try:
                marker = load_release_pending(self.paths)
            except MalformedPendingRelease as exc:
                marker = None
                self.log(f"hostops: corrupt pending release state: {exc}")
            self._record_unhealthy(marker, reason=f"unhealthy heartbeat: {problem}")
        if problem == "signing key missing":
            return 0
        rfd = open_dir(self.paths.requests, create=True)
        handled = 0
        try:
            entries = []
            for name in os.listdir(rfd):
                if m := REQUEST_NAME_RE.fullmatch(name):
                    try:
                        entries.append((os.stat(name, dir_fd=rfd, follow_symlinks=False).st_mtime, m.group(1), name))
                    except FileNotFoundError:
                        continue
            for _, request_id, name in sorted(entries):
                if handled:
                    try:
                        policy, policy_sha = load_policy(self.paths)
                    except (OSError, ValueError, RuntimeError):
                        policy, policy_sha = {"ops": {}}, None
                self._handle(rfd, request_id, name, policy, policy_sha)
                handled += 1
                if self.restart_requested:
                    break
        finally:
            os.close(rfd)
        if problem is None and not self.restart_requested:
            self.maybe_confirm_release()
        return handled

    def _handle(self, rfd: int, request_id: str, name: str, policy: dict[str, Any], policy_sha: str | None) -> None:
        done_fd = self._state_fd("done")
        try:
            try:
                raw = read_regular(rfd, name, MAX_REQUEST_BYTES)
            except (OSError, ValueError):
                unlink_at(rfd, name)
                if exists_at(done_fd, request_id):
                    self._republish(done_fd, request_id)
                else:
                    self._finish({"id": request_id}, "refused", {}, "request is not a bounded regular file",
                                 started=self.now(), policy_sha=policy_sha)
                return
            unlink_at(rfd, name)  # consumed: the spool copy is never read twice
            cancel = f"{request_id}.cancel"
            if exists_at(done_fd, request_id):
                self._republish(done_fd, request_id)  # idempotent: same id, same receipt, no second execution
                return
            try:
                request = json.loads(raw)
            except json.JSONDecodeError:
                request = None
            now = self.now()
            problem = request_problem(request, now)
            if problem or not isinstance(request, dict) or request.get("id") != request_id:
                stub = {"id": request_id, "op": (request or {}).get("op") if isinstance(request, dict) else None,
                        "args": {}, "sha": None, "requested_at": None}
                self._finish(stub, "refused", {}, problem or "id does not match the file name", started=now,
                             policy_sha=policy_sha, request_raw=raw)
                if isinstance(request, dict) and request.get("op") == "release.install":
                    self._recover_unsuccessful_install(request, "refused", problem or "id does not match the file name")
                return
            if exists_at(rfd, cancel):
                unlink_at(rfd, cancel)
                self._finish(request, "cancelled", {}, "cancelled before it started", started=now,
                             policy_sha=policy_sha, request_raw=raw)
                if isinstance(request, dict) and request.get("op") == "release.install":
                    self._recover_unsuccessful_install(request, "cancelled", "cancelled before it started")
                return
            ifd = self._state_fd("inflight")
            try:
                write_at(ifd, name, raw)
            finally:
                os.close(ifd)
            ctx = Context(self.paths, policy, request, self.run, self.post, self.now,
                          cancelled=lambda: exists_at(rfd, cancel), journal=self.journal())
            self.log(f"hostops: {request_id[:8]} {request['op']} started")
            outcome, detail, error = "ok", {}, None
            try:
                detail = OPERATIONS[request["op"]](ctx)
            except Refused as exc:
                outcome, error = "refused", str(exc)
            except Failed as exc:
                outcome, error, detail = "failed", str(exc), getattr(exc, "detail", {})
            except Cancelled as exc:
                outcome, error, detail = "cancelled", "cancelled while running", exc.args[0] if exc.args else {}
            except subprocess.TimeoutExpired:
                outcome, error = "timeout", "a host command exceeded its time limit"
            except Exception as exc:  # noqa: BLE001 - every request ends in a receipt
                outcome, error = "failed", f"executor error {type(exc).__name__}"
            unlink_at(rfd, cancel)
            self._finish(request, outcome, detail, error, started=now, policy_sha=policy_sha, request_raw=raw)
            if request.get("op") == "release.install" and outcome != "ok":
                self._recover_unsuccessful_install(request, outcome, error, ctx=ctx)
            self.restart_requested = ctx.restart_required
            ifd = self._state_fd("inflight")
            try:
                unlink_at(ifd, name)
            finally:
                os.close(ifd)
        finally:
            os.close(done_fd)

    def _finish(self, request: dict[str, Any], outcome: str, detail: dict[str, Any], error: str | None, *,
                started: datetime | None, policy_sha: str | None = None, request_raw: bytes = b"") -> None:
        receipt = {"schema": RECEIPT_SCHEMA, "request_id": request["id"], "op": request.get("op"),
                   "args": request.get("args") if outcome != "refused" else {}, "sha": request.get("sha"),
                   "requested_at": request.get("requested_at"), "requester": request.get("requester"),
                   "request_sha256": hashlib.sha256(request_raw).hexdigest() if request_raw else None,
                   "started_at": iso(started) if started else None, "finished_at": iso(self.now()),
                   "outcome": outcome, "error": error, "detail": detail,
                   "executor": {"version": self.version, "host": socket.gethostname(),
                                "policy_sha256": policy_sha}}
        data = canonical(receipt)
        signature = sign(self.paths.key, data)
        done_fd = self._state_fd("done")
        try:
            write_at(done_fd, request["id"], data + b"\x00" + signature)
        finally:
            os.close(done_fd)
        self._publish(request["id"], data, signature)
        self._append_journal({"id": request["id"], "op": request.get("op"), "outcome": outcome,
                              "finished_at": receipt["finished_at"], "sha": request.get("sha")})
        self.log(f"hostops: {request['id'][:8]} {request.get('op')} {outcome}" + (f" ({error})" if error else ""))

    def _start_owner(self) -> bool:
        unit = "jarvisos-mission-owner.service"
        try:
            proc = self.run(["systemctl", "--user", "start", unit], timeout=60)
            if proc.returncode != 0:
                self.log(f"hostops: owner start returned {proc.returncode}")
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            self.log(f"hostops: owner start failed: {exc}")
            return False

    def _recover_unsuccessful_install(
        self,
        request: dict[str, Any],
        outcome: str,
        error: str | None,
        *,
        ctx: Context | None = None,
    ) -> None:
        candidate = (request.get("args") or {}).get("sha") or request.get("sha")
        pub_curr = self.paths.published / "current"
        if pub_curr.is_symlink() or pub_curr.exists():
            try:
                if pub_curr.resolve().name == candidate:
                    prev = None
                    if (self.paths.guard / "RELEASE").is_file():
                        try:
                            g_sha = (self.paths.guard / "RELEASE").read_text(encoding="utf-8").strip()
                            if SHA_RE.fullmatch(g_sha) and g_sha != candidate:
                                prev = g_sha
                        except OSError:
                            pass
                    if prev:
                        return_published_release(self.paths, prev)
                    else:
                        pub_curr.unlink(missing_ok=True)
            except OSError:
                pass

        restart_req = ctx.restart_required if ctx is not None else False
        if not restart_req:
            inst_curr = self.paths.install / "current"
            if inst_curr.is_symlink() or inst_curr.exists():
                try:
                    if inst_curr.resolve().name == candidate:
                        prev = None
                        if (self.paths.guard / "RELEASE").is_file():
                            try:
                                g_sha = (self.paths.guard / "RELEASE").read_text(encoding="utf-8").strip()
                                if SHA_RE.fullmatch(g_sha) and g_sha != candidate:
                                    prev = g_sha
                            except OSError:
                                pass
                        restore_release_links(self.paths, prev)
                except OSError:
                    pass
            if self.paths.release_pending.exists():
                try:
                    p_marker = load_release_pending(self.paths)
                    if p_marker and p_marker.get("candidate_sha") == candidate:
                        self.paths.release_pending.unlink(missing_ok=True)
                except MalformedPendingRelease:
                    _quarantine_pending_marker(self.paths)

        if candidate and SHA_RE.fullmatch(str(candidate)):
            already_recorded = any(
                h.get("candidate_sha") == candidate and h.get("status") in ("refused", "returned")
                for h in read_release_history(self.paths)
            )
            if not already_recorded:
                entry = {
                    "candidate_sha": candidate,
                    "status": "refused",
                    "reason": error or outcome,
                    "returned_at": iso(self.now()),
                    "notified": False,
                }
                state_fd = open_dir(self.paths.state, create=True)
                try:
                    with open(self.paths.release_history, "a", encoding="utf-8") as handle:
                        handle.write(json.dumps(entry, sort_keys=True) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    os.close(state_fd)
                sync_published_release_history(self.paths)

        if request.get("requester") == "relay-continuity:owner" or (request.get("args") or {}).get("owner_active") is True:
            if not (self.paths.published / "current").exists():
                inst = None
                if (self.paths.install / "current").exists():
                    try:
                        inst = (self.paths.install / "current").resolve().name
                    except OSError:
                        pass
                if not inst and (self.paths.guard / "RELEASE").is_file():
                    try:
                        g_sha = (self.paths.guard / "RELEASE").read_text(encoding="utf-8").strip()
                        if SHA_RE.fullmatch(g_sha):
                            inst = g_sha
                    except OSError:
                        pass
                if inst:
                    return_published_release(self.paths, inst)
            self._start_owner()

    def _handle_owner_timeout(self, marker: dict[str, Any], candidate: str) -> None:
        reason = "owner release sha not reported within deadline"
        if not marker.get("previous_sha") and (self.paths.guard / "RELEASE").is_file():
            try:
                g_sha = (self.paths.guard / "RELEASE").read_text(encoding="utf-8").strip()
                if SHA_RE.fullmatch(g_sha) and g_sha != candidate:
                    marker["previous_sha"] = g_sha
            except OSError:
                pass
        self._record_unhealthy(marker, reason=reason)
        self.log(f"hostops: candidate {candidate} owner release sha not reported within deadline")

    def _publish(self, request_id: str, data: bytes, signature: bytes) -> None:
        fd = open_dir(self.paths.receipts, create=True)
        try:
            write_at(fd, f"{request_id}.json.sig", signature)
            write_at(fd, f"{request_id}.json", data)
        finally:
            os.close(fd)

    def _republish(self, done_fd: int, request_id: str) -> None:
        stored = read_regular(done_fd, request_id, 2 << 20)
        data, _, signature = stored.partition(b"\x00")
        self._publish(request_id, data, signature)

    def _record_unhealthy(self, marker: dict[str, Any] | None, reason: str) -> None:
        self.restart_requested = True
        if marker is not None:
            marker["unhealthy_reason"] = reason
            marker["unhealthy_at"] = iso(self.now())
            state_fd = open_dir(self.paths.state, create=True)
            try:
                write_at(state_fd, "release-pending.json", canonical(marker))
            finally:
                os.close(state_fd)

    def maybe_confirm_release(self) -> bool:
        try:
            marker = load_release_pending(self.paths)
        except MalformedPendingRelease as exc:
            self.log(f"hostops: corrupt pending release state: {exc}")
            self._record_unhealthy(None, reason=f"corrupt pending marker: {exc}")
            return False
        if marker is None:
            return False
        candidate = marker.get("candidate_sha")
        if not candidate or not SHA_RE.fullmatch(str(candidate)):
            self._record_unhealthy(marker, reason="invalid candidate sha in pending marker")
            return False
        if self.version != candidate:
            self.log(f"hostops: executor version {self.version} != candidate {candidate}")
            self._record_unhealthy(marker, reason=f"version mismatch ({self.version} != {candidate})")
            return False

        # Crash recovery window: check if already confirmed in release_history
        for h_entry in read_release_history(self.paths):
            if (h_entry.get("status") == "confirmed"
                    and h_entry.get("candidate_sha") == candidate
                    and h_entry.get("created_at") == marker.get("created_at")):
                state_fd = open_dir(self.paths.state, create=True)
                try:
                    unlink_at(state_fd, "release-pending.json")
                finally:
                    os.close(state_fd)
                return True

        if not marker.get("owner_active"):
            deadline_str = marker.get("deadline")
            if deadline_str:
                deadline_dt = parse_ts(deadline_str)
                if deadline_dt and self.now() > deadline_dt:
                    self.log(f"hostops: candidate {candidate} confirmation deadline expired")
                    self._record_unhealthy(marker, reason="deadline expired")
                    return False

        try:
            load_policy(self.paths)
        except (OSError, ValueError, RuntimeError) as exc:
            self.log(f"hostops: policy unusable during confirmation: {exc}")
            self._record_unhealthy(marker, reason=f"policy unusable: {exc}")
            return False

        beat = _heartbeat(self.paths)
        if not beat or beat.get("problem") is not None:
            prob = (beat or {}).get("problem") or "heartbeat missing"
            self.log(f"hostops: candidate {candidate} heartbeat unhealthy: {prob}")
            self._record_unhealthy(marker, reason=f"unhealthy heartbeat: {prob}")
            return False
        if beat.get("version") != candidate:
            self.log(f"hostops: heartbeat version {beat.get('version')} != candidate {candidate}")
            self._record_unhealthy(marker, reason=f"heartbeat version mismatch ({beat.get('version')} != {candidate})")
            return False
        beat_at = parse_ts(beat.get("at"))
        if not beat_at or abs((self.now() - beat_at).total_seconds()) > 120:
            self.log(f"hostops: candidate {candidate} heartbeat stale")
            self._record_unhealthy(marker, reason="heartbeat stale")
            return False

        if marker.get("watcher_active"):
            unit = "jarvisos-relay-watch.service"
            state = _unit_state(self, unit)
            active_state = state.get("ActiveState")
            if active_state in ("failed", "dead"):
                self.log(f"hostops: candidate {candidate} watcher service failed ({active_state})")
                self._record_unhealthy(marker, reason=f"watcher service failed ({active_state})")
                return False
            if active_state != "active":
                self._watcher_seen_at = None
                if "watcher_seen_at" in marker:
                    marker.pop("watcher_seen_at", None)
                    state_fd = open_dir(self.paths.state, create=True)
                    try:
                        write_at(state_fd, "release-pending.json", canonical(marker))
                    finally:
                        os.close(state_fd)
                return False

            raw_restarts = state.get("NRestarts", "0")
            current_restarts = int(raw_restarts) if raw_restarts.isdecimal() else 0
            if self._watcher_initial_restarts is None:
                if (saved_restarts := marker.get("watcher_initial_restarts")) is not None:
                    self._watcher_initial_restarts = int(saved_restarts)
                else:
                    self._watcher_initial_restarts = current_restarts
                    marker["watcher_initial_restarts"] = current_restarts
                    state_fd = open_dir(self.paths.state, create=True)
                    try:
                        write_at(state_fd, "release-pending.json", canonical(marker))
                    finally:
                        os.close(state_fd)

            if current_restarts > self._watcher_initial_restarts:
                self._watcher_initial_restarts = current_restarts
                marker["watcher_initial_restarts"] = current_restarts
                self._watcher_seen_at = self.now()
                marker["watcher_seen_at"] = iso(self.now())
                state_fd = open_dir(self.paths.state, create=True)
                try:
                    write_at(state_fd, "release-pending.json", canonical(marker))
                finally:
                    os.close(state_fd)
                return False

            enter_ts = parse_systemd_ts(state.get("ActiveEnterTimestamp", ""))
            marker_created = parse_ts(marker.get("created_at"))

            if self._watcher_seen_at is None:
                if saved_seen := parse_ts(marker.get("watcher_seen_at")):
                    self._watcher_seen_at = saved_seen
                elif (enter_ts is not None and marker_created is not None
                      and enter_ts >= marker_created - timedelta(seconds=5)
                      and enter_ts <= self.now()):
                    self._watcher_seen_at = enter_ts
                    marker["watcher_seen_at"] = iso(enter_ts)
                    state_fd = open_dir(self.paths.state, create=True)
                    try:
                        write_at(state_fd, "release-pending.json", canonical(marker))
                    finally:
                        os.close(state_fd)
                else:
                    self._watcher_seen_at = self.now()
                    marker["watcher_seen_at"] = iso(self.now())
                    state_fd = open_dir(self.paths.state, create=True)
                    try:
                        write_at(state_fd, "release-pending.json", canonical(marker))
                    finally:
                        os.close(state_fd)
                    return False

            if (self.now() - self._watcher_seen_at).total_seconds() < 60:
                return False
            if enter_ts is not None and (self.now() - enter_ts).total_seconds() < 60:
                return False

        if marker.get("owner_active"):
            if not marker.get("owner_started"):
                t_before = self.now()
                if not self._start_owner():
                    self.log(f"hostops: candidate {candidate} owner start failed")
                    deadline_str = marker.get("deadline")
                    deadline_dt = parse_ts(deadline_str) if deadline_str else None
                    if deadline_dt and self.now() > deadline_dt:
                        self._handle_owner_timeout(marker, candidate)
                    return False
                marker["owner_started"] = True
                marker["owner_started_at"] = iso(t_before)
                state_fd = open_dir(self.paths.state, create=True)
                try:
                    write_at(state_fd, "release-pending.json", canonical(marker))
                finally:
                    os.close(state_fd)

            owner_reported = False
            state_path = self.paths.owner_serve_state
            if state_path.is_file():
                try:
                    s_data = json.loads(state_path.read_text(encoding="utf-8"))
                    if isinstance(s_data, dict) and s_data.get("release_sha") == candidate:
                        started_at = parse_ts(marker.get("owner_started_at"))
                        updated_at = parse_ts(s_data.get("release_updated_at"))
                        if started_at is not None and updated_at is not None and updated_at >= started_at:
                            owner_reported = True
                except (OSError, json.JSONDecodeError):
                    pass

            if not owner_reported:
                deadline_str = marker.get("deadline")
                deadline_dt = parse_ts(deadline_str) if deadline_str else None
                if deadline_dt and self.now() > deadline_dt:
                    self._handle_owner_timeout(marker, candidate)
                    return False
                return False

            marker["owner_reported_at"] = iso(self.now())

        entry = dict(marker)
        entry["status"] = "confirmed"
        entry["confirmed_at"] = iso(self.now())
        state_fd = open_dir(self.paths.state, create=True)
        try:
            with open(self.paths.release_history, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            unlink_at(state_fd, "release-pending.json")
        finally:
            os.close(state_fd)
        sync_published_release_history(self.paths)
        self.log(f"hostops: release {candidate} confirmed")
        return True


def serve(paths: Paths, interval: float) -> int:
    if os.environ.get("JARVIS_SANDBOX") or socket.gethostname() == "jarvis-sandbox":
        return 2
    state_fd = open_dir(paths.state, create=True)
    try:
        lock_fd = os.open("serve.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=state_fd)
    finally:
        os.close(state_fd)
    import fcntl

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another host-ops executor is running", file=sys.stderr)
        os.close(lock_fd)
        return 3
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    executor = Executor(paths)
    executor.recover()
    version_str = getattr(executor, "version", None) or executor_version()
    executor.log(f"hostops: executor {version_str} serving")
    while not stop["flag"]:
        try:
            executor.step()
            if executor.restart_requested:
                os.close(lock_fd)
                return 75
        except Exception as exc:  # noqa: BLE001 - a bad spool entry never stops the executor
            executor.log(f"hostops: step error {type(exc).__name__}: {exc}")
        for _ in range(int(interval * 10)):
            if stop["flag"]:
                break
            time.sleep(0.1)
    os.close(lock_fd)
    return 0


# ---------------------------------------------------------------------------
# Installation (host session only): code from GitHub master, never from a local checkout


DEFAULT_POLICY: dict[str, Any] = {
    "schema": POLICY_SCHEMA,
    "ops": {
        "release.install": {"trust_master": False},
        "slack.deliver": {"max_per_hour": 6},
        "service.status": {"units": ["jarvisos-relay-watch.service", "jarvisos-mission-owner.service",
                                     "jarvisos-hostops.service", "agent-relay.service"]},
        "service.control": {"units": ["jarvisos-relay-watch.service", "jarvisos-mission-owner.service"],
                            "actions": OWNER_ACTIONS},
        "service.install": {"templates": ["jarvisos-relay-watch", "jarvisos-mission-owner"]},
        "wsl.probe": {},
        "wsl.handoff": {"dir": "/mnt/c/Users/user/JarvisOS-print", "open": True, "max_bytes": 200 << 20,
                        "extensions": [".3mf", ".stl", ".step", ".glb", ".png", ".pdf"]},
        "browser.screenshot": {"chrome": os.path.expanduser("~/.cache/ms-playwright/chromium-1187/chrome-linux/chrome")},
        "http.smoke": {"targets": {"backend-health": {"url": "http://127.0.0.1:8000/health", "expect": 200}}},
        "bench.run": {"benchmarks": {
            "relay-observe-tests": {"cwd": "backend", "timeout_s": 900, "memory_max": "4G", "cpu_quota": "200%",
                                    "argv": [os.path.expanduser("~/src/JarvisOS/backend/.venv/bin/python"), "-m", "pytest",
                                             "-q", "-p", "no:cacheprovider", "tests/test_relay_observe.py"]}}},
    },
}

SERVICE_UNIT = (
    "[Unit]\nDescription=JarvisOS host operations executor (spec 207)\n"
    "[Service]\nWorkingDirectory=%h\n"
    "ExecStartPre=/usr/bin/python3 -I %h/.local/share/jarvisos-hostops/guard/scripts/host_ops.py guard\n"
    "ExecStart=/usr/bin/python3 -I %h/.local/share/jarvisos-hostops/current/scripts/host_ops.py serve\n"
    "Restart=on-failure\nRestartPreventExitStatus=2 3\nRestartSec=30\n"
    "NoNewPrivileges=yes\nMemoryMax=2G\nTasksMax=256\nCPUQuota=200%\nRestartForceExitStatus=75\n"
    "[Install]\nWantedBy=default.target\n")


def _gh(args: list[str], timeout: float = 60) -> str:
    proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api {args[0]} failed")
    return proc.stdout


def _item_id(item: dict[str, Any]) -> int:
    val = item.get("id")
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return int(val)
    if isinstance(val, str) and val.isdigit():
        return int(val)
    return 0


def _parse_check_runs(raw: str) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    raw = raw.strip()
    if not raw:
        return runs
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(raw):
        while idx < len(raw) and raw[idx].isspace():
            idx += 1
        if idx >= len(raw):
            break
        obj, end = decoder.raw_decode(raw, idx)
        idx = end
        if isinstance(obj, dict):
            cr = obj.get("check_runs")
            if isinstance(cr, list):
                runs.extend(item for item in cr if isinstance(item, dict))
        elif isinstance(obj, list):
            runs.extend(item for item in obj if isinstance(item, dict))
    return runs


def _parse_statuses(raw: str) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    raw = raw.strip()
    if not raw:
        return statuses
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(raw):
        while idx < len(raw) and raw[idx].isspace():
            idx += 1
        if idx >= len(raw):
            break
        obj, end = decoder.raw_decode(raw, idx)
        idx = end
        if isinstance(obj, list):
            statuses.extend(item for item in obj if isinstance(item, dict) and "context" in item and "state" in item)
        elif isinstance(obj, dict):
            if "context" in obj and "state" in obj:
                statuses.append(obj)
            elif isinstance(obj.get("statuses"), list):
                statuses.extend(item for item in obj["statuses"] if isinstance(item, dict) and "context" in item and "state" in item)
    return statuses


def required_checks_problem(sha: str) -> str | None:
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        return "invalid commit sha"
    try:
        raw_rules = _gh([f"repos/{REPO}/rules/branches/master"])
        rules = json.loads(raw_rules)
    except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as exc:
        return f"unreadable ruleset: {exc}"
    if not isinstance(rules, list):
        return "unreadable ruleset: rules response is not a list"

    req_checks: set[tuple[str, int | None]] = set()
    for entry in rules:
        if isinstance(entry, dict) and entry.get("type") == "required_status_checks":
            params = entry.get("parameters")
            if isinstance(params, dict):
                checks = params.get("required_status_checks")
                if isinstance(checks, list):
                    for check in checks:
                        if isinstance(check, dict) and isinstance(check.get("context"), str):
                            iid = check.get("integration_id")
                            integration_id = int(iid) if (isinstance(iid, int) and not isinstance(iid, bool)) else None
                            req_checks.add((check["context"], integration_id))
                        elif isinstance(check, str):
                            req_checks.add((check, None))
    if not req_checks:
        return "empty ruleset: no required status checks found on master"

    try:
        raw_runs = _gh([f"repos/{REPO}/commits/{sha}/check-runs?per_page=100", "--paginate"])
        check_runs = _parse_check_runs(raw_runs)
    except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as exc:
        return f"unreadable check runs: {exc}"

    try:
        raw_statuses = _gh([f"repos/{REPO}/commits/{sha}/statuses?per_page=100", "--paginate"])
        commit_statuses = _parse_statuses(raw_statuses)
    except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as exc:
        return f"unreadable commit statuses: {exc}"

    for context, req_integration_id in sorted(req_checks, key=lambda c: (c[0], c[1] or -1)):
        matching_runs = [
            r for r in check_runs
            if r.get("name") == context and (
                req_integration_id is None
                or (isinstance(r.get("app"), dict) and r["app"].get("id") == req_integration_id)
            )
        ]
        if not matching_runs:
            return f"required check {context!r} is missing"

        for r in matching_runs:
            status = r.get("status")
            if status != "completed":
                return f"required check {context!r} is not completed ({status})"

        latest = max(matching_runs, key=_item_id)
        conclusion = latest.get("conclusion")
        if conclusion != "success":
            return f"required check {context!r} did not succeed ({conclusion})"

        matching_statuses = [s for s in commit_statuses if s.get("context") == context]
        if matching_statuses:
            latest_status = max(matching_statuses, key=_item_id)
            state = latest_status.get("state")
            if state != "success":
                return f"required check {context!r} commit status did not succeed ({state})"

    return None


def _run_preflight(argv: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _preflight_release(release_dir: Path, paths: Paths) -> None:
    for name in ("host_ops.py", "relay_continuity.py", "relay_jobs.py", "relay_observe.py", "devctx.py", "worker_routing.py"):
        if not (release_dir / "scripts" / name).is_file():
            raise Refused(f"release missing {name}")
    cmd = [sys.executable, "-I", str(release_dir / "scripts" / "host_ops.py"), "--home", str(paths.home), "preflight"]
    try:
        proc_pf = _run_preflight(cmd, timeout=60)
    except subprocess.TimeoutExpired:
        raise Refused("candidate preflight failed: timeout after 60s") from None
    if proc_pf.returncode != 0:
        stderr_text = proc_pf.stderr.decode() if isinstance(proc_pf.stderr, bytes) else (proc_pf.stderr or "")
        lines = stderr_text.strip().splitlines()
        first_line = lines[0].strip() if lines else f"exit code {proc_pf.returncode}"
        raise Refused(f"candidate preflight failed: {first_line}")


def _scan_release_tree(root: Path) -> tuple[dict[Path, int], set[Path]]:
    if not root.is_dir() or root.is_symlink():
        raise Refused(f"release path is not a directory: {root.name}")
    files: dict[Path, int] = {}
    directories: set[Path] = set()

    def walk(current: Path, rel: Path) -> None:
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise Refused(f"unreadable release directory: {exc}") from None
        for entry in sorted(entries, key=lambda e: e.name):
            child_rel = rel / entry.name if rel != Path(".") else Path(entry.name)
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise Refused(f"cannot stat release member {child_rel}: {exc}") from None
            mode = st.st_mode
            if stat.S_ISLNK(mode):
                raise Refused(f"unsafe release member: {child_rel}")
            if stat.S_ISDIR(mode):
                directories.add(child_rel)
                walk(Path(entry.path), child_rel)
            elif stat.S_ISREG(mode):
                files[child_rel] = mode & 0o777
            else:
                raise Refused(f"unsafe release member: {child_rel}")

    walk(root, Path("."))
    return files, directories


def _verify_existing_release(release: Path, stage: Path) -> None:
    stage_files, stage_dirs = _scan_release_tree(stage)
    release_files, release_dirs = _scan_release_tree(release)

    if stage_dirs != release_dirs:
        extra_dirs = release_dirs - stage_dirs
        missing_dirs = stage_dirs - release_dirs
        if extra_dirs:
            raise Refused(f"existing release has extra directory: {min(extra_dirs, key=str)}")
        raise Refused(f"existing release missing directory: {min(missing_dirs, key=str)}")

    if set(stage_files) != set(release_files):
        extra_files = set(release_files) - set(stage_files)
        missing_files = set(stage_files) - set(release_files)
        if extra_files:
            raise Refused(f"existing release has extra file: {min(extra_files, key=str)}")
        raise Refused(f"existing release missing file: {min(missing_files, key=str)}")

    for rel_path, stage_mode in sorted(stage_files.items(), key=lambda item: str(item[0])):
        release_mode = release_files[rel_path]
        if release_mode != stage_mode:
            raise Refused(f"existing release file mode mismatch for {rel_path} ({oct(release_mode)} != {oct(stage_mode)})")
        try:
            rel_bytes = (release / rel_path).read_bytes()
            stage_bytes = (stage / rel_path).read_bytes()
        except OSError as exc:
            raise Refused(f"cannot read release file {rel_path}: {exc}") from None
        if rel_bytes != stage_bytes:
            raise Refused(f"existing release content mismatch for {rel_path}")


def install_release(paths: Paths, sha: str, *, request_id: str | None = None,
                    watcher_active: bool = False, owner_active: bool = False, now: datetime | None = None) -> None:
    if os.environ.get("JARVIS_SANDBOX") or socket.gethostname() == "jarvis-sandbox":
        raise Refused("installation is host-only")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise Refused("sha must be a full commit id")
    for h_entry in read_release_history(paths):
        if h_entry.get("candidate_sha") == sha and h_entry.get("status") in ("refused", "returned"):
            reason = h_entry.get("reason", "unhealthy")
            raise Refused(f"candidate {sha} was previously refused ({reason})")
    pending = None
    if paths.release_pending.exists():
        try:
            pending = load_release_pending(paths)
            confirmed = False
            if pending:
                p_cand = pending.get("candidate_sha")
                p_created = pending.get("created_at")
                for h_entry in read_release_history(paths):
                    if (h_entry.get("status") == "confirmed"
                            and h_entry.get("candidate_sha") == p_cand
                            and h_entry.get("created_at") == p_created):
                        paths.release_pending.unlink(missing_ok=True)
                        confirmed = True
                        pending = None
                        break
            if not confirmed and pending:
                p_sha = pending.get("candidate_sha")
                if p_sha != sha:
                    raise Refused(f"another release is already pending ({p_sha})")
        except MalformedPendingRelease as exc:
            raise Refused("malformed pending release marker exists") from exc
    status = _gh([f"repos/{REPO}/compare/{sha}...master", "--jq", ".status"]).strip()
    if status not in ("ahead", "identical"):
        raise Refused(f"{sha} is not on GitHub master ({status})")
    if problem := required_checks_problem(sha):
        raise Refused(problem)
    current = paths.install / "current"
    installed = None
    resolved = None
    if current.is_symlink() or current.exists():
        try:
            resolved = current.resolve(strict=True)
            installed = resolved.name
        except OSError as exc:
            raise Refused(f"cannot resolve current release: {exc}") from None
        if not resolved.is_dir() or not SHA_RE.fullmatch(installed):
            raise Refused(f"current release is not a valid commit sha directory ({installed!r})")
        cmp_status = _gh([f"repos/{REPO}/compare/{installed}...{sha}", "--jq", ".status"]).strip()
        if cmp_status not in ("ahead", "identical"):
            raise Refused(f"candidate {sha} is not ahead or identical to installed {installed} ({cmp_status})")
    proc = subprocess.run(["gh", "api", f"repos/{REPO}/tarball/{sha}"],
                          capture_output=True, timeout=120, check=True)
    releases = paths.install / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / sha
    with tempfile.TemporaryDirectory(dir=releases, prefix=".install-") as tmp:
        stage = Path(tmp)
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:gz") as archive:
            for member in archive.getmembers():
                parts = Path(member.name).parts
                if member.name.startswith("/") or ".." in parts or not (member.isdir() or member.isfile()):
                    raise Refused("unsafe release archive member")
                if len(parts) < 2 or parts[1] != "scripts":
                    continue
                target = stage.joinpath(*parts[1:])
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source:
                        target.write_bytes(source.read())
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
        _preflight_release(stage, paths)
        if not release.exists():
            os.rename(stage, release)
        else:
            _verify_existing_release(release, stage)
            _preflight_release(release, paths)

    # Pin previous release (or candidate if first install) to guard
    source_dir = resolved if installed and resolved and resolved.is_dir() else release
    source_sha = installed or sha
    _pin_guard(paths, source_dir, source_sha)

    # Ensure installed release is published
    if installed and (paths.install / "releases" / installed / "scripts").is_dir():
        if not (paths.published / "releases" / installed).is_dir():
            try:
                publish_release(paths, installed, switch_current=False)
            except Exception:  # noqa: BLE001, S110
                pass
        if not (paths.published / "current").exists():
            try:
                switch_published_release(paths, installed)
            except Exception:  # noqa: BLE001, S110
                pass

    # Stage files for candidate under published/releases/sha without switching published/current
    publish_release(paths, sha, switch_current=False)

    ts = now or now_utc()
    deadline = (pending.get("deadline") if pending and pending.get("candidate_sha") == sha
                else iso(ts + timedelta(minutes=10)))
    p_prev = (pending.get("previous_sha") if pending and pending.get("candidate_sha") == sha
              else installed)
    p_req = (request_id or (pending.get("request_id") if pending and pending.get("candidate_sha") == sha else None))
    p_attempts = (pending.get("attempts", 0) if pending and pending.get("candidate_sha") == sha else 0)
    p_watch = bool(watcher_active or (pending and pending.get("watcher_active")))
    p_owner = bool(owner_active or (pending and pending.get("owner_active")))

    pending_written = False
    install_switched = False
    published_switched = False
    try:
        write_release_pending(paths, previous_sha=p_prev, candidate_sha=sha,
                              deadline=deadline, request_id=p_req,
                              attempts=p_attempts, watcher_active=p_watch,
                              owner_active=p_owner, now=ts)
        pending_written = True

        switch_install_release(paths, sha)
        install_switched = True

        switch_published_release(paths, sha)
        published_switched = True
    except BaseException:
        if install_switched or published_switched:
            restore_release_links(paths, p_prev)
        elif p_prev and (paths.published / "current").exists():
            try:
                if (paths.published / "current").resolve().name != p_prev:
                    restore_release_links(paths, p_prev)
            except OSError:
                restore_release_links(paths, p_prev)
        if pending_written:
            paths.release_pending.unlink(missing_ok=True)
        raise


def op_release_install(ctx: Context) -> dict[str, Any]:
    conf = ctx.conf("release.install")
    args = _args(ctx, {"sha"})
    if conf.get("trust_master") is not True:
        raise Refused("release.install requires trust_master: true in the host policy")
    unit = "jarvisos-relay-watch.service"
    watcher_active = _unit_state(ctx, unit).get("ActiveState") == "active"
    owner_active = (ctx.request.get("requester") == "relay-continuity:owner"
                    or (ctx.request.get("args") or {}).get("owner_active") is True)
    kwargs: dict[str, Any] = {"request_id": ctx.request.get("id"),
                              "watcher_active": watcher_active,
                              "now": ctx.now()}
    if owner_active:
        kwargs["owner_active"] = True

    current_link = ctx.paths.install / "current"
    installed_before = None
    if current_link.is_symlink() or current_link.exists():
        try:
            installed_before = current_link.resolve().name
        except OSError:
            installed_before = None

    install_release(ctx.paths, args["sha"], **kwargs)
    ctx.restart_required = True
    if watcher_active and _systemctl(ctx, "restart", unit).returncode:
        # If watcher restart failed, the install request failed.
        # Return both links through the pinned guard and settle the pending marker
        # before restarting the previous release.
        if ctx.paths.release_pending.exists():
            guard(ctx.paths, force_return=True, reason="watcher restart failed", post=ctx.post, run=ctx.run, now=ctx.now())
        else:
            restore_release_links(ctx.paths, installed_before)
        raise Failed("watcher restart failed")

    return {"sha": args["sha"], "executor_restart": True}


OPERATIONS["release.install"] = op_release_install


def install(paths: Paths, sha: str, with_watch: bool = False) -> int:
    try:
        install_release(paths, sha, watcher_active=with_watch)
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    paths.config.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(paths.config, 0o700)
    if not paths.key.is_file():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", SIGNER, "-f", str(paths.key)],
                       check=True, timeout=30)
    public = Path(f"{paths.key}.pub").read_text().split()
    paths.published.mkdir(parents=True, exist_ok=True)
    (paths.published / "allowed_signers").write_text(
        f'{SIGNER} namespaces="{SIG_NAMESPACE}" {public[0]} {public[1]}\n')
    if not paths.policy.exists():
        paths.policy.write_text(json.dumps(DEFAULT_POLICY, indent=1, sort_keys=True) + "\n")
        os.chmod(paths.policy, 0o600)
    paths.units.mkdir(parents=True, exist_ok=True)
    (paths.units / UNIT).write_text(SERVICE_UNIT)
    (paths.units / "jarvisos-relay-watch.service").write_text(UNIT_TEMPLATES["jarvisos-relay-watch"])
    env = _host_env()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=60, env=env)
    subprocess.run(["systemctl", "--user", "enable", UNIT], check=True, timeout=60, env=env)
    subprocess.run(["systemctl", "--user", "restart", UNIT], check=True, timeout=60, env=env)
    if with_watch:
        subprocess.run(["systemctl", "--user", "enable", "--now", "jarvisos-relay-watch.service"],
                       check=True, timeout=60, env=env)
    print(f"installed host-ops executor {sha} and restarted {UNIT}")
    return 0


def preflight(paths: Paths) -> int:
    try:
        if paths.policy.exists():
            policy, _ = load_policy(paths)
        else:
            policy = DEFAULT_POLICY
        ops = policy.get("ops")
        if not isinstance(ops, dict):
            raise TypeError("policy ops must be a dict")
        unknown = set(ops) - set(OPERATIONS)
        if unknown:
            raise RuntimeError(f"policy enables unknown operations {sorted(unknown)}")
        print(json.dumps({"ok": True, "version": executor_version()}))
        return 0
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def health(paths: Paths) -> int:
    problems = []
    if not paths.key.is_file() or paths.key.stat().st_mode & 0o077:
        problems.append("signing key missing or not mode 600")
    if not paths.allowed_signers.is_file():
        problems.append("public key not published")
    elif Path(f"{paths.key}.pub").is_file() and \
            Path(f"{paths.key}.pub").read_text().split()[1] not in paths.allowed_signers.read_text():
        problems.append("published public key does not match the signing key")
    try:
        load_policy(paths)
    except (OSError, ValueError, RuntimeError) as exc:
        problems.append(f"policy: {exc}")
    beat = _heartbeat(paths)
    if beat is None or (at := parse_ts(beat.get("at"))) is None or now_utc() - at > timedelta(minutes=2):
        problems.append("executor heartbeat missing or older than 2 minutes")
    elif beat.get("problem"):
        problems.append(f"executor reports: {beat['problem']}")
    print(json.dumps({"healthy": not problems, "problems": problems, "heartbeat": beat}, indent=1))
    return 0 if not problems else 1


def _heartbeat(paths: Paths) -> dict[str, Any] | None:
    try:
        data = json.loads(paths.heartbeat.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# CLI


def _print_receipt(receipt: dict[str, Any]) -> None:
    print(json.dumps({k: receipt.get(k) for k in ("request_id", "op", "sha", "outcome", "error", "finished_at",
                                                  "detail")}, indent=1, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="host_ops.py", description=__doc__.splitlines()[0])
    parser.add_argument("--home", default=os.environ.get("HOME", "/home/user"), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("request", help="submit a typed host operation request")
    p.add_argument("op", choices=sorted(OPERATIONS))
    p.add_argument("--args", default="{}", help="JSON object of operation arguments")
    p.add_argument("--sha", help="full candidate commit the operation is evidence for")
    p.add_argument("--requester", default=os.environ.get("USER", "agent"))
    p.add_argument("--wait", type=float, default=0, help="seconds to wait for the verified receipt")
    p = sub.add_parser("cancel", help="cancel a request (before it starts, or a running bench.run)")
    p.add_argument("id")
    p = sub.add_parser("verify", help="verify a receipt's host signature and outcome")
    p.add_argument("id")
    p.add_argument("--op")
    p.add_argument("--sha")
    sub.add_parser("status", help="executor heartbeat and recent receipts")
    p = sub.add_parser("serve", help="run the host executor (systemd unit)")
    p.add_argument("--interval", type=float, default=2.0)
    p = sub.add_parser("install", help="install the executor from a GitHub master commit (host session)")
    p.add_argument("--sha", required=True)
    p.add_argument("--with-watch", action="store_true")
    sub.add_parser("health", help="host-side health check")
    sub.add_parser("preflight", help="candidate preflight validation")
    p = sub.add_parser("guard", help="pre-start release guard (systemd ExecStartPre)")
    p.add_argument("--return", dest="do_return", action="store_true", help="force return to previous release")
    p.add_argument("--reason", default="manual return", help="reason for return")
    args = parser.parse_args(argv)
    paths = Paths(Path(args.home))

    if args.action == "request":
        try:
            op_args = json.loads(args.args)
        except json.JSONDecodeError:
            print("--args must be JSON", file=sys.stderr)
            return 2
        request = make_request(args.op, op_args, args.sha, args.requester, now_utc())
        if problem := request_problem(request, now_utc()):
            print(f"refusing to submit: {problem}", file=sys.stderr)
            return 2
        request_id = submit(paths, request)
        print(request_id)
        deadline = time.monotonic() + args.wait
        while args.wait and time.monotonic() < deadline:
            receipt, _ = load_receipt(paths, request_id)
            if receipt:
                _print_receipt(receipt)
                return 0 if receipt.get("outcome") == "ok" else 1
            time.sleep(1)
        if args.wait:
            print("no verified receipt yet; check `host_ops.py status`", file=sys.stderr)
            return 4
        return 0
    if args.action == "cancel":
        if not ID_RE.fullmatch(args.id):
            return 2
        fd = open_dir(paths.requests, create=True)
        try:
            write_at(fd, f"{args.id}.cancel", b"cancel\n")
        finally:
            os.close(fd)
        return 0
    if args.action == "verify":
        receipt, why = load_receipt(paths, args.id)
        if receipt is None:
            print(f"not verified: {why}", file=sys.stderr)
            return 1
        _print_receipt(receipt)
        if problem := receipt_problem(receipt, op=args.op, sha=args.sha):
            print(f"verified signature, but {problem}", file=sys.stderr)
            return 1
        return 0
    if args.action == "status":
        beat = _heartbeat(paths)
        recent = []
        try:
            names = sorted((p for p in paths.receipts.iterdir() if REQUEST_NAME_RE.fullmatch(p.name)),
                           key=lambda p: p.stat().st_mtime)[-10:]
        except OSError:
            names = []
        for path in names:
            receipt, why = load_receipt(paths, path.name[:-5])
            recent.append({"id": path.name[:-5], "verified": bool(receipt), "op": (receipt or {}).get("op"),
                           "outcome": (receipt or {}).get("outcome"), "why": why or None})
        alive = bool(beat and (at := parse_ts(beat.get("at"))) and now_utc() - at <= timedelta(minutes=2))
        print(json.dumps({"executor_alive": alive, "heartbeat": beat, "recent": recent}, indent=1))
        return 0
    if args.action == "serve":
        return serve(paths, args.interval)
    if args.action == "install":
        return install(paths, args.sha, args.with_watch)
    if args.action == "health":
        return health(paths)
    if args.action == "preflight":
        return preflight(paths)
    if args.action == "guard":
        return guard(paths, force_return=args.do_return, reason=args.reason)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
