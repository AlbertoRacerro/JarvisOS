#!/usr/bin/env python3
"""Daily encrypted backup with restic and a monthly restore test (spec 379).

Runs on the host only, never in a sandbox. The operator writes ``~/.config/jarvisos-hostops/backup.toml``::

    repositories = ["/media/drive/jarvisos-restic"]   # one or more restic repository strings
    paths = ["/home/me/src/pack", "/home/me/vault"]   # absolute directories
    exclude = ["*.tmp"]                               # optional restic --exclude globs
    password_command = "pass show jarvisos/restic"    # prints the key from the password manager

The key reaches restic only through ``--password-command``; it never enters argv, the environment, a file written
here or the status file. ``~/.local/state/jarvisos-backup/status.json`` (``JARVISOS_BACKUP_STATUS_FILE`` overrides)
is rewritten atomically after every command and holds only times, snapshot ids, byte counts and reason codes.
Standard library only; restic is a separately installed program.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

CONFIG_FILE = Path.home() / ".config" / "jarvisos-hostops" / "backup.toml"
STATUS_FILE_ENV = "JARVISOS_BACKUP_STATUS_FILE"
VAULT_PATH_ENV = "JARVISOS_VAULT_PATH"
STATUS_SCHEMA = "jarvisos-backup-status.v1"
KEEP = ["--keep-daily", "14", "--keep-weekly", "8", "--keep-monthly", "12"]
SAMPLE_MIN = 50
RESTIC_TIMEOUT_S = 6 * 3600
_ALLOWED_KEYS = {"repositories", "paths", "exclude", "password_command"}
_FORBIDDEN_ENV = ("RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE")


class Refused(Exception):
    """A configuration or key source that must not be used; carries a reason code only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_file() -> Path:
    override = os.environ.get(STATUS_FILE_ENV, "").strip()
    return Path(override) if override else Path.home() / ".local" / "state" / "jarvisos-backup" / "status.json"


def read_status() -> dict[str, Any]:
    try:
        data = json.loads(status_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": STATUS_SCHEMA, "repositories": {}}
    if not isinstance(data, dict) or not isinstance(data.get("repositories"), dict):
        return {"schema": STATUS_SCHEMA, "repositories": {}}
    return data


def write_status(data: dict[str, Any]) -> None:
    path = status_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True, indent=2)
    os.replace(tmp, path)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _in_git_checkout(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in [path, *path.parents])


def load_config(path: Path = CONFIG_FILE) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise Refused("config_unreadable") from None
    if any("password" in key.lower() and key != "password_command" for key in data):
        raise Refused("literal_password_in_config")
    if set(data) - _ALLOWED_KEYS:
        raise Refused("config_unknown_key")
    repositories, paths = data.get("repositories"), data.get("paths")
    if (not isinstance(repositories, list) or not repositories
            or not all(isinstance(r, str) and r.strip() for r in repositories)):
        raise Refused("config_repositories_invalid")
    if (not isinstance(paths, list) or not paths
            or not all(isinstance(p, str) and os.path.isabs(os.path.expanduser(p)) for p in paths)):
        raise Refused("config_paths_invalid")
    exclude = data.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(e, str) for e in exclude):
        raise Refused("config_exclude_invalid")
    command = data.get("password_command")
    if not isinstance(command, str) or not command.strip():
        raise Refused("password_command_missing")
    return {"repositories": repositories, "paths": [os.path.expanduser(p) for p in paths], "exclude": exclude,
            "password_command": command}


def check_key_source(config: dict[str, Any], env: dict[str, str] | None = None) -> None:
    """Refuse every key source other than a password command that lives outside the backed-up data."""
    env = dict(os.environ) if env is None else env
    if any(env.get(name) for name in _FORBIDDEN_ENV):
        raise Refused("password_in_environment")
    try:
        tokens = shlex.split(config["password_command"])
    except ValueError:
        raise Refused("password_command_invalid") from None
    guarded = [Path(p).resolve() for p in config["paths"]]
    vault = env.get(VAULT_PATH_ENV, "").strip()
    if vault:
        guarded.append(Path(os.path.expanduser(vault)).resolve())
    for token in tokens:
        value = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
        if not value.startswith(("/", "~", ".")):
            continue
        named = Path(os.path.expanduser(value)).resolve()
        if any(_inside(named, root) for root in guarded) or _in_git_checkout(named):
            raise Refused("password_command_inside_backed_up_data")


def _restic_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("RESTIC_PASSWORD")}


def restic(repository: str, config: dict[str, Any], *args: str, timeout: float = RESTIC_TIMEOUT_S,
           ) -> subprocess.CompletedProcess[str]:
    binary = shutil.which("restic")
    if binary is None:
        raise Refused("restic_missing")
    argv = [binary, "--repo", repository, "--password-command", config["password_command"], *args]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=_restic_env(), check=False)


def _summary(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("message_type") == "summary":
            return message
    return {}


def _repo_entry(status: dict[str, Any], index: int) -> dict[str, Any]:
    return status["repositories"].setdefault(str(index), {})


def command_run(config: dict[str, Any], status: dict[str, Any]) -> bool:
    ok_all = True
    for index, repository in enumerate(config["repositories"]):
        entry = _repo_entry(status, index)
        excludes = [arg for pattern in config["exclude"] for arg in ("--exclude", pattern)]
        steps = [("backup_failed", ["backup", "--json", *excludes, *config["paths"]]),
                 ("forget_failed", ["forget", *KEEP, "--prune"]),
                 ("check_failed", ["check"])]
        reason, summary = None, {}
        for code, args in steps:
            proc = restic(repository, config, *args)
            if proc.returncode != 0:
                reason = code
                break
            if args[0] == "backup":
                summary = _summary(proc.stdout)
        if reason:
            ok_all = False
            entry.update(last_failure_at=_now(), last_failure_reason=reason)
        else:
            entry.update(last_success_at=_now(), snapshot_id=str(summary.get("snapshot_id", ""))[:64] or None,
                         bytes_added=int(summary.get("data_added", 0) or 0))
    if ok_all:
        status["last_success_at"] = _now()
    else:
        status["last_failure_at"] = _now()
        status["last_failure_reason"] = "repository_failed"
    return ok_all


def _json_lines(stdout: str) -> list[dict[str, Any]]:
    out = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_time(value: str) -> float:
    """RFC 3339 as restic writes it (nanoseconds, Z or an offset); a value without a zone is UTC."""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?", value.strip())
    if not match:
        raise ValueError("unrecognised snapshot time")
    stamp, fraction, zone = match.groups()
    parsed = datetime.fromisoformat(f"{stamp}.{(fraction or '0')[:6].ljust(6, '0')}")
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        parsed -= sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
    return parsed.replace(tzinfo=UTC).timestamp()


def restore_check(config: dict[str, Any], workdir: Path, rng: random.Random) -> str | None:
    """Return None when the latest snapshot of the first repository restores intact, else a reason code."""
    repository = config["repositories"][0]
    if restic(repository, config, "check", "--read-data-subset", "5%").returncode != 0:
        return "check_failed"
    listing = restic(repository, config, "ls", "--json", "latest")
    if listing.returncode != 0:
        return "ls_failed"
    items = _json_lines(listing.stdout)
    snapshot = next((item for item in items if "snapshot" in (item.get("struct_type"), item.get("message_type"))), None)
    files = [item for item in items if item.get("type") == "file" and isinstance(item.get("path"), str)]
    if snapshot is None or not files:
        return "snapshot_empty"
    if restic(repository, config, "restore", "latest", "--target", str(workdir)).returncode != 0:
        return "restore_failed"
    try:
        snapshot_time = _parse_time(str(snapshot.get("time")))
    except ValueError:
        return "snapshot_time_invalid"
    for item in files:
        restored = workdir / item["path"].lstrip("/")
        if not restored.is_file() or restored.stat().st_size != int(item.get("size", -1)):
            return "restore_size_mismatch"
    candidates = []
    for item in files:
        live = Path(item["path"])
        try:
            if live.is_file() and live.stat().st_mtime <= snapshot_time:
                candidates.append(item["path"])
        except OSError:
            continue
    sample = candidates if len(candidates) <= SAMPLE_MIN else rng.sample(candidates, SAMPLE_MIN)
    for path in sample:
        if _sha256(workdir / path.lstrip("/")) != _sha256(Path(path)):
            return "restore_hash_mismatch"
    return None


def command_restore_test(config: dict[str, Any], status: dict[str, Any], rng: random.Random | None = None) -> bool:
    workdir = Path(tempfile.mkdtemp(prefix="jarvisos-restore-test-"))
    try:
        reason = restore_check(config, workdir, rng or random.Random())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    status["last_restore_test_at"] = _now()
    status["last_restore_test_ok"] = reason is None
    status["last_restore_test_reason"] = reason
    return reason is None


def command_init(config: dict[str, Any], status: dict[str, Any]) -> bool:
    ok_all = True
    for index, repository in enumerate(config["repositories"]):
        if restic(repository, config, "cat", "config").returncode == 0:
            continue
        if restic(repository, config, "init").returncode != 0:
            ok_all = False
            _repo_entry(status, index).update(last_failure_at=_now(), last_failure_reason="init_failed")
    return ok_all


def _record_failure(status: dict[str, Any], command: str, reason: str) -> None:
    """A refused or timed-out command marks only what it was for: the backup line or the restore-test line."""
    if command == "restore-test":
        status.update(last_restore_test_at=_now(), last_restore_test_ok=False, last_restore_test_reason=reason)
    elif command == "run":
        status.update(last_failure_at=_now(), last_failure_reason=reason)
    else:
        status.update(last_init_failure_reason=reason)


COMMANDS = {"run": command_run, "restore-test": command_restore_test, "init": command_init}


def main(argv: list[str] | None = None, *, config_path: Path = CONFIG_FILE) -> int:
    parser = argparse.ArgumentParser(prog="backup.py", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    status = read_status()
    status["schema"] = STATUS_SCHEMA
    try:
        config = load_config(config_path)
        check_key_source(config)
        ok = COMMANDS[args.command](config, status)
        code = 0 if ok else 1
    except Refused as refused:
        _record_failure(status, args.command, refused.code)
        print(f"backup: refused: {refused.code}", file=sys.stderr)
        code = 2
    except subprocess.TimeoutExpired:
        _record_failure(status, args.command, "restic_timeout")
        code = 1
    write_status(status)
    if code == 0:
        print(f"backup: {args.command} ok")
    elif code == 1:
        print(f"backup: {args.command} failed", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
