"""Resolve exact CLI model IDs from bounded, account-local catalog metadata.

Catalog data is an authorization input, never a hint for constructing an ID.
No reader in this module sends a model prompt.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

MAX_BYTES = 256 * 1024
TIMEOUT = 12


def enabled(env: dict[str, str]) -> bool:
    return env.get("JARVISOS_ACCOUNT_MODEL_RESOLUTION") == "1"


class Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Role:
    cli: str
    stem: str
    effort: str = ""
    preview: bool = False


# These identities are roles, not capability or quota rankings. The caller owns those decisions.
ROLES: dict[str, Role] = {
    "coordinator_argon": Role("agy", "gemini-argon", "low"),
    "coordinator_hydrogen": Role("agy", "gemini-hydrogen", "medium"),
    "coordinator_neon": Role("agy", "gemini-neon", "medium"),
    "coordinator_agy_opus": Role("agy", "claude-opus", "medium"),
    "coordinator_codex": Role("codex", "gpt-sol"),
    "coordinator_claude": Role("claude", "claude-opus"),
    "worker_flash": Role("agy", "gemini-flash", "low"),
    "worker_flash-medium": Role("agy", "gemini-flash", "medium"),
    "worker_flash-high": Role("agy", "gemini-flash", "high"),
    "worker_hydrogen-low": Role("agy", "gemini-hydrogen", "low"),
    "worker_hydrogen-medium": Role("agy", "gemini-hydrogen", "medium"),
    "worker_hydrogen-high": Role("agy", "gemini-hydrogen", "high"),
    "worker_argon": Role("agy", "gemini-argon", "low"),
    "worker_argon-medium": Role("agy", "gemini-argon", "medium"),
    "worker_argon-high": Role("agy", "gemini-argon", "high"),
    "worker_agy-sonnet": Role("agy", "claude-sonnet", "high"),
    "worker_agy-opus": Role("agy", "claude-opus", "high"),
    "worker_luna": Role("codex", "gpt-luna"),
    "worker_terra": Role("codex", "gpt-terra"),
    "worker_sol": Role("codex", "gpt-sol"),
    "worker_haiku": Role("claude", "claude-haiku"),
    "worker_sonnet": Role("claude", "claude-sonnet"),
}


def _pattern(role: Role) -> re.Pattern[str]:
    # A role's family is exact: the version is inserted at its configured location.
    vendor, family = role.stem.split("-", 1)
    effort = f"-{re.escape(role.effort)}" if role.effort else ""
    if vendor == "claude":
        body = rf"claude-{re.escape(family)}-(\d+)-(\d+){effort}"
    else:
        body = rf"{re.escape(vendor)}-(\d+)(?:\.(\d+))?-{re.escape(family)}{effort}"
    return re.compile(body + r"(-preview)?\Z")


def parse_catalog(raw: bytes, cli: str) -> frozenset[str]:
    if not raw or len(raw) > MAX_BYTES:
        raise Refused("catalog_malformed")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refused("catalog_malformed") from exc
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        if cli != "agy":
            raise Refused("catalog_malformed") from None
        # agy models emits plain IDs. Do not mine IDs out of prose or errors.
        ids = text.splitlines()
        if not ids or len(ids) > 2048 or any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,100}", line) for line in ids):
            raise Refused("catalog_malformed") from None
    else:
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise Refused("catalog_malformed")
        if "as_of" in data:
            try:
                stamp = datetime.fromisoformat(data["as_of"].replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise Refused("catalog_malformed") from exc
            if stamp.tzinfo is None or not 0 <= (datetime.now(UTC) - stamp).total_seconds() < 60:
                raise Refused("catalog_stale")
        entries = data["models"]
        if not entries or len(entries) > 2048 or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                                                      for item in entries):
            raise Refused("catalog_malformed")
        ids = [item["id"] for item in entries]
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,100}", item) for item in ids) or len(set(ids)) != len(ids):
        raise Refused("catalog_malformed")
    return frozenset(ids)


def catalog(cli: str, binary: str, env: dict[str, str], *, now: float | None = None) -> frozenset[str]:
    _ = now  # A fresh account read is required every time; no cross-account cache identity is assumed.
    if cli == "codex":
        return _codex_catalog(binary, env)
    if cli != "agy":
        # Relay discovery and Claude aliases are not account entitlement evidence.
        raise Refused("catalog_unavailable")
    try:
        proc = subprocess.Popen([binary, "models"], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise Refused("catalog_unreadable") from exc
    output: list[bytes] = []

    def read() -> None:
        assert proc.stdout is not None
        output.append(proc.stdout.read(MAX_BYTES + 1))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(TIMEOUT)
    if reader.is_alive() or not output or len(output[0]) > MAX_BYTES:
        proc.kill()
        proc.wait()
        raise Refused("catalog_unreadable")
    try:
        proc.wait(timeout=max(0.1, TIMEOUT))
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        raise Refused("catalog_unreadable") from exc
    if proc.returncode != 0:
        raise Refused("catalog_unreadable")
    return parse_catalog(output[0], cli)


def _codex_catalog(binary: str, env: dict[str, str]) -> frozenset[str]:
    """Read app-server model/list metadata; no thread or model turn is created."""
    try:
        proc = subprocess.Popen([binary, "app-server"], env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise Refused("catalog_unreadable") from exc
    messages: queue.Queue[bytes | None] = queue.Queue()

    def read() -> None:
        assert proc.stdout is not None
        total = 0
        while total <= MAX_BYTES:
            line = proc.stdout.readline(MAX_BYTES + 1 - total)
            if not line:
                break
            total += len(line)
            messages.put(line)
        messages.put(None)

    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + TIMEOUT

    def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        proc.stdin.flush()

    def response(ident: int) -> dict[str, Any]:
        while True:
            try:
                line = messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise Refused("catalog_unreadable") from exc
            if line is None or len(line) > MAX_BYTES or time.monotonic() > deadline:
                raise Refused("catalog_unreadable")
            try:
                item = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Refused("catalog_malformed") from exc
            if not isinstance(item, dict):
                raise Refused("catalog_malformed")
            if item.get("id") == ident:
                result = item.get("result")
                if "error" in item or not isinstance(result, dict):
                    raise Refused("catalog_unreadable")
                return result

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "account_model_resolution",
                                                                        "version": "1"}}})
        response(1)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "model/list", "params": {}})
        result = response(2)
        entries = result.get("data")
        if not isinstance(entries, list):
            entries = result.get("models")
        return parse_catalog(json.dumps({"models": entries}).encode(), "codex")
    except (OSError, BrokenPipeError) as exc:
        raise Refused("catalog_unreadable") from exc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def resolve(role_name: str, binary: str, env: dict[str, str], *, exact: str | None = None,
            now: float | None = None) -> str:
    role = ROLES.get(role_name)
    if role is None:
        raise Refused("role_not_allowed")
    ids = catalog(role.cli, binary, env, now=now)
    pattern = _pattern(role)
    matches: list[tuple[tuple[int, int, int], str]] = []
    for ident in ids:
        match = pattern.fullmatch(ident)
        if match and (role.preview or not match.group(3)):
            matches.append(((int(match.group(1)), int(match.group(2) or 0), 0 if match.group(3) else 1), ident))
    if exact is not None:
        if exact not in ids or not pattern.fullmatch(exact) or (exact.endswith("-preview") and not role.preview):
            raise Refused("model_not_authorized")
        return exact
    if not matches:
        raise Refused("catalog_no_match")
    return max(matches)[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an exact account model without a model turn")
    parser.add_argument("role")
    parser.add_argument("exact")
    parser.add_argument("binary")
    args = parser.parse_args()
    try:
        resolve(args.role, args.binary, dict(os.environ), exact=args.exact)
    except Refused as exc:
        print(exc.reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
