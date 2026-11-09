#!/usr/bin/env python3
"""Native Antigravity coordinator transport (223), within the existing mission owner.

The native stream is retained verbatim. This is neither a Relay plugin nor a worker
credential profile: the coordinator retains the sandboxed mission's existing authority.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import secrets
import shutil
import socket
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Native coordinators run at high effort by default: coordination is design, review and integration (spec 255,
# effort by task). Each agent has its own allowlist, env override and required quota buckets.
MODEL = "gemini-4-argon-high"
MODELS = frozenset(f"gemini-4-argon-{level}" for level in ("low", "medium", "high"))
GEMINI_BUCKETS = frozenset(("gemini-5h", "gemini-weekly"))
REQUIRED_BUCKETS = GEMINI_BUCKETS | {"gemini-argon-wtu-weekly"}
OPUS_MODEL = "claude-opus-5-5-high"
OPUS_MODELS = frozenset(f"claude-opus-5-5-{level}" for level in ("low", "medium", "high"))
OPUS_BUCKETS = frozenset(("3p-5h", "3p-weekly"))
FLASH_MODEL = "gemini-3.8-flash-high"
FLASH_MODELS = frozenset(f"gemini-3.8-flash-{level}" for level in ("medium", "high"))
SONNET_MODEL = "claude-sonnet-5-5-high"
SONNET_MODELS = frozenset(f"claude-sonnet-5-5-{level}" for level in ("low", "medium", "high"))
NATIVE = {  # agent: (env override, default model, allowlist, required buckets)
    "argon": ("JARVISOS_AGY_ARGON_MODEL", MODEL, MODELS, REQUIRED_BUCKETS),
    "agy_opus": ("JARVISOS_AGY_OPUS_MODEL", OPUS_MODEL, OPUS_MODELS, OPUS_BUCKETS),
    "flash": ("JARVISOS_AGY_FLASH_COORDINATOR_MODEL", FLASH_MODEL, FLASH_MODELS, GEMINI_BUCKETS),
    "agy_sonnet": ("JARVISOS_AGY_SONNET_MODEL", SONNET_MODEL, SONNET_MODELS, OPUS_BUCKETS),
}


def models_for(agent: str) -> frozenset[str]:
    if agent not in NATIVE:
        raise Refused("agent_not_allowed")
    return NATIVE[agent][2]


def model_for(agent: str, env: Mapping[str, str]) -> str:
    if agent not in NATIVE:
        raise Refused("agent_not_allowed")
    variable, default, allowed, _ = NATIVE[agent]
    model = env.get(variable, default)
    if model not in allowed:
        raise Refused("model_not_allowed")
    return model


QUOTA_TIMEOUT = 45
# agy supports an hour per print turn; the deadline handling below is unchanged (spec 255).
TURN_TIMEOUT = 3600


class Refused(Exception):
    pass


def sandbox() -> bool:
    return socket.gethostname() == "jarvis-sandbox" and not Path("/mnt/c").exists() and not Path("/run/WSL").exists()


def prepare_login(home: Path | None = None) -> None:
    home = home or Path.home()
    target = home / ".local/share/jarvisos-sandbox/state/agy-home"
    link = home / ".gemini"
    if not target.is_dir():
        raise Refused("login_state_missing")
    if link.is_symlink():
        if link.resolve() != target.resolve():
            raise Refused("login_path_conflict")
    elif link.exists():
        raise Refused("login_path_conflict")
    else:
        link.symlink_to(target, target_is_directory=True)


def subscription_settings(home: Path | None = None) -> None:
    """Refuse paid-credit overage or API/custom-provider configuration; never rewrite account settings."""
    path = (home or Path.home()) / ".gemini/antigravity-cli/settings.json"
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise Refused("settings_unreadable") from exc
    if not isinstance(data, dict) or data.get("useG1Credits", False) is not False:
        raise Refused("credit_overage_enabled_or_invalid")
    if data.get("modelProvider") not in (None, "antigravity"):
        raise Refused("subscription_provider_required")


def buckets(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        return []
    if isinstance(node, list):
        return [b for item in node for b in buckets(item, depth + 1)]
    if not isinstance(node, dict):
        return []
    own = node.get("buckets")
    return ([b for b in own if isinstance(b, dict)] if isinstance(own, list) else []) + [
        b for key, value in node.items() if key != "buckets" for b in buckets(value, depth + 1)]


def quota_document(binary: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """One no-model-turn `agy /quota` document; every native agent selects its own buckets from it."""
    if not sandbox():
        raise Refused("not_sandbox")
    prepare_login()
    subscription_settings()
    try:
        proc = subprocess.run([binary, "-p", "/quota", "--output-format", "json"], env=env,
                              capture_output=True, text=True, timeout=QUOTA_TIMEOUT, check=False)
        data = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise Refused("quota_unreadable") from exc
    if proc.returncode != 0 or not isinstance(data, dict) or data.get("status") != "SUCCESS":
        raise Refused("quota_unreadable")
    return data


def read_quota(binary: str, env: dict[str, str] | None = None, *, agent: str = "argon",
               document: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    model_for(agent, os.environ if env is None else env)
    data = quota_document(binary, env) if document is None else document
    required = NATIVE[agent][3]
    found = [b for b in buckets(data) if b.get("id") in required]
    if len(found) != len(required) or {b.get("id") for b in found} != required:
        raise Refused("quota_bucket_missing")
    for bucket in found:
        fraction = bucket.get("remaining_fraction")
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not math.isfinite(fraction) \
                or not 0 <= fraction <= 1 or not isinstance(bucket.get("disabled", False), bool):
            raise Refused("quota_unreadable")
    return found


def require_room(reading: list[dict[str, Any]]) -> None:
    if any(b.get("disabled") or b["remaining_fraction"] <= 0 for b in reading):
        raise Refused("quota_exhausted")


def new_session(repo: Path, task: str, agent: str = "argon") -> tuple[str, Path]:
    sid = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    turn = repo / ".agent-relay/sessions" / sid / "turns/turn-001"
    turn.mkdir(parents=True)
    rules = ('# Native subscription coordinator\n\n## Rules\n'
             'Execute only the maintainer-authorized task below within the existing sandbox and repository invariants. '
             'A model response never proves delivery; retain real evidence for the independent mission check. '
             'End your final reply with one JSON status line exactly in this format: '
             'RELAY_STATUS: {"status":"propose_done"}. Use status continue for a durable checkpoint, '
             'or blocked only for a real maintainer gate. Do not finish while a command you started is still running: '
             'use command_status until the executor reports its real exit status. '
             'Run long test suites in the foreground with their own timeout, never chained with a commit. Commit and '
             'push as a separate short command early, well before the turn deadline. '
             'Do not invent execution evidence.\n\n## Task\n\n')
    (turn / "prompt.md").write_text(rules + task + "\n", encoding="utf-8")
    (turn / "state.json").write_text(json.dumps({"agent_key": agent, "status": "continue"}) + "\n")
    return sid, turn


def run(repo: Path, session: str, agent: str = "argon") -> int:
    turn = repo / ".agent-relay/sessions" / session / "turns/turn-001"
    metadata: dict[str, Any] = {"schema": "argon-coordinator.v1", "agent": agent, "exit_code": None, "reason": None, "quota": [], "launched": False}
    lease = None
    code = 1
    try:
        if not sandbox() or os.environ.get("JARVISOS_MISSION_PROFILE") != "1":
            raise Refused("not_mission_sandbox")
        model = model_for(agent, os.environ)
        binary = shutil.which("agy")
        if binary is None:
            raise Refused("agy_missing")
        lease_path = repo / ".agent-relay/locks/coordinator.lease"
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        inherited = os.environ.get("JARVISOS_ARGON_LEASE_FD")
        lease = os.fdopen(int(inherited), "a+") if inherited else lease_path.open("a+")
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused("coordinator_busy") from exc
        metadata["quota"] = read_quota(binary, agent=agent)
        require_room(metadata["quota"])
        prompt = (turn / "prompt.md").read_text(encoding="utf-8")
        argv = [binary, "-p", prompt, "--model", model, "--output-format", "stream-json", "--mode", "accept-edits", "--dangerously-skip-permissions",
                "--sandbox", "--print-timeout", f"{TURN_TIMEOUT}s"]
        metadata["model"] = model
        metadata["launched"] = True
        with (turn / "output.jsonl").open("w") as out, (turn / "stderr.log").open("w") as err:
            try:
                proc = subprocess.run(argv, cwd=repo, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                      timeout=TURN_TIMEOUT + 30, check=False)
                code = proc.returncode
                metadata["exit_code"] = code
            except subprocess.TimeoutExpired:
                metadata["reason"] = "timeout"
        # Only genuine native result text is eligible for the mission's existing completion verifier.
        if metadata["exit_code"] == 0:
            events, bad = read_events(turn / "output.jsonl")
            results = [e.get("result") for e in events if e.get("event") == "result"]
            if not bad and len(results) == 1 and isinstance(results[0], dict):
                result = results[0]
                if result.get("status") == "SUCCESS" and not result.get("denied_actions") \
                        and isinstance(result.get("response"), str) and result["response"].strip():
                    (turn / "last-message.txt").write_text(result["response"], encoding="utf-8")
    except (Refused, OSError) as exc:
        metadata["reason"] = str(exc) if isinstance(exc, Refused) else type(exc).__name__
    finally:
        if lease is not None:
            lease.close()
        temp = turn / "native-result.tmp"
        temp.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        temp.replace(turn / "native-result.json")
    print(json.dumps({"session_id": session, "run_error": metadata["reason"]}))
    return code


def read_events(path: Path) -> tuple[list[dict[str, Any]], int]:
    events, bad = [], 0
    if not path.exists():
        return events, bad
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise TypeError("not object")
            events.append(item)
        except (json.JSONDecodeError, TypeError):
            bad += 1
    return events, bad


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--session", required=True)
    parser.add_argument("--agent", choices=tuple(NATIVE), default="argon")
    args = parser.parse_args()
    if not all(c in "0123456789-abcdef" for c in args.session) or len(args.session) != 22:
        raise SystemExit("invalid native session id")
    raise SystemExit(run(args.repo.resolve(), args.session, args.agent))
