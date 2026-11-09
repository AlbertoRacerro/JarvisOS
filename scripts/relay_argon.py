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
import re
import secrets
import shutil
import socket
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MODEL = "gemini-4-argon-low"
MODELS = frozenset(f"gemini-4-argon-{level}" for level in ("low", "medium", "high"))
REQUIRED_BUCKETS = frozenset(("gemini-5h", "gemini-weekly", "gemini-argon-wtu-weekly"))
OPUS_MODEL = "claude-opus-5-5-medium"
OPUS_MODELS = frozenset(f"claude-opus-5-5-{level}" for level in ("low", "medium", "high"))
OPUS_BUCKETS = frozenset(("3p-5h", "3p-weekly"))


def model_for(agent: str, env: Mapping[str, str]) -> str:
    if agent not in ("argon", "agy_opus"):
        raise Refused("agent_not_allowed")
    model = env.get("JARVISOS_AGY_OPUS_MODEL", OPUS_MODEL) if agent == "agy_opus" else env.get("JARVISOS_AGY_ARGON_MODEL", MODEL)
    if model not in (OPUS_MODELS if agent == "agy_opus" else MODELS):
        raise Refused("model_not_allowed")
    return model


QUOTA_TIMEOUT = 45
# Our own turn budget, not a model limit. 30 minutes cut most long implementation turns mid-way (most of
# the agy turns that ended unsettled on 2026-11-07); one hour keeps the deadline handling with room for real work.
TURN_TIMEOUT = 3600

AGY_ERROR_PREFIX = "AGY_ERROR: "

# Resuming an interrupted agy conversation (maintainer directive L20261109091909-a4bb): the CLI keeps the whole
# conversation locally (full tool results, scratch artifacts) and `--conversation <id>` resumes it in print mode
# (verified live 2026-11-09: same id, memory kept, step indices continue, a missing id starts a new one).
# A large conversation is compacted first (`/compact`, verified live: memory kept); a huge one is not resumed.
CONVERSATION_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
RESUME_COMPACT_BYTES = 6 * 2**20  # about one and a half long implementation turns of conversation store
RESUME_MAX_BYTES = 48 * 2**20
COMPACT_TIMEOUT = 600


class Refused(Exception):
    pass


def conversation_of(turn: Path) -> str | None:
    """The agy conversation id a retained turn ran in (its stream's init event), or None."""
    try:
        with (turn / "output.jsonl").open(encoding="utf-8", errors="replace") as handle:
            first = json.loads(handle.readline() or "null")
    except (OSError, json.JSONDecodeError):
        return None
    cid = first.get("conversation_id") if isinstance(first, dict) and first.get("event") == "init" else None
    return cid if isinstance(cid, str) and CONVERSATION_RE.fullmatch(cid) else None


def conversation_store(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".local/share/jarvisos-sandbox/state/agy-home/antigravity-cli/conversations"


def native_error(stderr: str) -> dict[str, Any] | None:
    """The CLI's own structured error line (`AGY_ERROR: {...}`), the last one if several; None when absent.
    Model prose never reaches stderr, so this is runner evidence, not a claim by the model."""
    found = None
    for line in stderr.splitlines():
        if line.startswith(AGY_ERROR_PREFIX):
            try:
                item = json.loads(line[len(AGY_ERROR_PREFIX):])
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                found = item
    return found


def harness_cause(error: dict[str, Any] | None) -> tuple[str, str] | None:
    """(outcome, cause) for a native error that is the harness or the provider, never a capability verdict.
    outcome uses the continuity vocabulary: quota (provider limit, cool down), transient (back off, retry),
    unknown (retry the same agent after reconciling effects). None: no such evidence."""
    if not error:
        return None
    status = str(error.get("status") or "")
    reason = str(error.get("reason") or "")
    code = error.get("error_code")
    text = str(error.get("short_error") or "")
    if status == "RESOURCE_EXHAUSTED" or code == 429:
        return "quota", "provider_quota"
    if reason == "print_timeout" or (status == "DEADLINE_EXCEEDED" and "print timeout" in text):
        return "unknown", "turn_time_limit"
    if status == "UNAUTHENTICATED" or code == 401:
        # The login was proven by the preflight quota read moments before; a mid-turn 401 is a token blip.
        return "transient", "provider_auth_blip"
    if status in ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED") or (isinstance(code, int) and 500 <= code <= 599) \
            or "request failed" in text:
        return "transient", "provider_network"
    return None


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


def read_quota(binary: str, env: dict[str, str] | None = None, *, agent: str = "argon") -> list[dict[str, Any]]:
    if not sandbox():
        raise Refused("not_sandbox")
    model_for(agent, os.environ if env is None else env)
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
    required = OPUS_BUCKETS if agent == "agy_opus" else REQUIRED_BUCKETS
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


def new_session(repo: Path, task: str, agent: str = "argon", resume: str | None = None) -> tuple[str, Path]:
    sid = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    turn = repo / ".agent-relay/sessions" / sid / "turns/turn-001"
    turn.mkdir(parents=True)
    if resume is not None and CONVERSATION_RE.fullmatch(resume):
        (turn / "resume.json").write_text(json.dumps({"conversation": resume}) + "\n", encoding="utf-8")
        task = ("## Resumed\nThis continues your previous conversation on this work, which was interrupted (the "
                "turn time limit, a network or provider error, or a failed attempt). Your earlier context and "
                "scratch files are still available. First check what is already done (git status, git log, the PR, "
                "your scratch notes), keep what is good, then finish the work.\n\n" + task)
    rules = ('# Native subscription coordinator\n\n## Rules\n'
             'Execute only the maintainer-authorized task below within the existing sandbox and repository invariants. '
             'A model response never proves delivery; retain real evidence for the independent mission check. '
             'End your final reply with one JSON status line exactly in this format: '
             'RELAY_STATUS: {"status":"propose_done"}. Use status continue for a durable checkpoint, '
             'or blocked only for a real maintainer gate. Do not finish while a command you started is still running: '
             'use command_status until the executor reports its real exit status. '
             'Do not invent execution evidence.\n\n'
             '## Working in this harness\n'
             f'- Your turn has a hard limit of {TURN_TIMEOUT // 60} minutes. Plan for it: after each finished unit of '
             'work, commit and push it as its own short command, well before the limit, then continue.\n'
             '- Run tests, builds and walks in the foreground with an explicit timeout, for example '
             '`timeout 900 <command>`. Do not start them as background tasks and then wait for them.\n'
             '- File tools need absolute paths.\n'
             '- A tool call or command that fails is normal: read the error, adjust and continue. '
             'It is not a reason to stop or to report the task blocked.\n\n## Task\n\n')
    (turn / "prompt.md").write_text(rules + task + "\n", encoding="utf-8")
    (turn / "state.json").write_text(json.dumps({"agent_key": agent, "status": "continue"}) + "\n")
    return sid, turn


def prepare_resume(binary: str, model: str, repo: Path, turn: Path, metadata: dict[str, Any],
                   store: Path | None = None) -> str | None:
    """The conversation to resume for this turn, compacted first when large; None starts a new conversation.
    Every choice is recorded in metadata["resume"]."""
    try:
        wanted = json.loads((turn / "resume.json").read_text(encoding="utf-8")).get("conversation")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(wanted, str) or not CONVERSATION_RE.fullmatch(wanted):
        return None
    record: dict[str, Any] = {"conversation": wanted, "used": False}
    metadata["resume"] = record
    try:
        size = ((store or conversation_store()) / f"{wanted}.db").stat().st_size
    except OSError:
        record["why"] = "missing"
        return None
    record["bytes"] = size
    if size > RESUME_MAX_BYTES:
        record["why"] = "too_large"
        return None
    if size > RESUME_COMPACT_BYTES:
        try:
            with (turn / "compact.jsonl").open("w") as out, (turn / "compact-stderr.log").open("w") as err:
                done = subprocess.run([binary, "-p", "/compact", "--model", model, "--conversation", wanted,
                                       "--output-format", "stream-json", "--sandbox", "--print-timeout",
                                       f"{COMPACT_TIMEOUT}s"], cwd=repo, stdout=out, stderr=err,
                                      stdin=subprocess.DEVNULL, timeout=COMPACT_TIMEOUT + 30, check=False)
            record["compacted"] = done.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            record["compacted"] = False
    record["used"] = True
    return wanted


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
        resume = prepare_resume(binary, model, repo, turn, metadata)
        argv = [binary, "-p", prompt, "--model", model, "--output-format", "stream-json", "--mode", "accept-edits", "--dangerously-skip-permissions",
                "--sandbox", "--print-timeout", f"{TURN_TIMEOUT}s", *(["--conversation", resume] if resume else [])]
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
    parser.add_argument("--agent", choices=("argon", "agy_opus"), default="argon")
    args = parser.parse_args()
    if not all(c in "0123456789-abcdef" for c in args.session) or len(args.session) != 22:
        raise SystemExit("invalid native session id")
    raise SystemExit(run(args.repo.resolve(), args.session, args.agent))
