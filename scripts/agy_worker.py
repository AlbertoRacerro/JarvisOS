#!/usr/bin/env python3
"""Antigravity (`agy`) delegated-worker wrapper (spec 221).

Runs one headless `agy` worker turn inside the certified sandbox, in a linked git worktree, with GitHub and global
Git credentials removed from the child environment, behind an allowlist of models and a no-model-turn quota
preflight (spec 217 probe). Evidence (transcript, diff against the starting HEAD, result.json) is written to an
evidence directory. The wrapper writes no ledger entry and makes no commit: the coordinator reviews the diff.
The worker has full CLI permissions only after the sandbox check passes.
Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ARGON_SPEC = importlib.util.spec_from_file_location("relay_argon", Path(__file__).resolve().parent / "relay_argon.py")
assert _ARGON_SPEC and _ARGON_SPEC.loader
relay_argon = sys.modules.get("relay_argon") or importlib.util.module_from_spec(_ARGON_SPEC)
if "relay_argon" not in sys.modules:
    sys.modules["relay_argon"] = relay_argon
    _ARGON_SPEC.loader.exec_module(relay_argon)

SCHEMA = "agy-worker.v1"
SANDBOX_HOSTNAME = "jarvis-sandbox"
# Confirmed by the signed-in sandbox's model list and the 2026-10-27 Flash proof.
FLASH_MODEL = "gemini-3.8-flash-low"
ARGON_MODEL = "gemini-4-argon-low"
# Spec 255: effort follows the task tier, so every family has an alias per effort the router may pick. The two base
# aliases keep their env overrides; the others are fixed ids. agy Sonnet and Opus run on the 3p buckets.
FIXED_MODELS = {"flash-medium": "gemini-3.8-flash-medium", "flash-high": "gemini-3.8-flash-high",
                "hydrogen-low": "gemini-4-hydrogen-low", "hydrogen-medium": "gemini-4-hydrogen-medium",
                "hydrogen-high": "gemini-4-hydrogen-high",
                "argon-medium": "gemini-4-argon-medium", "argon-high": "gemini-4-argon-high",
                "agy-sonnet": "claude-sonnet-5-5-high", "agy-opus": "claude-opus-5-5-high"}
GEMINI_BUCKETS = ("gemini-5h", "gemini-weekly")
FAMILY_BUCKETS = {"flash": GEMINI_BUCKETS, "hydrogen": GEMINI_BUCKETS,
                  "argon": (*GEMINI_BUCKETS, "gemini-argon-wtu-weekly"),
                  "agy": ("3p-5h", "3p-weekly")}
DEFAULT_TIMEOUT = 1800
MAX_TIMEOUT = 6 * 3600
QUOTA_TIMEOUT = 60
KILL_MARGIN = 60
MAX_TASK_BYTES = 200 * 1024
STRIPPED_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GIT_CONFIG_GLOBAL", "SSH_AUTH_SOCK",
                "GH_ENTERPRISE_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN")
STRIPPED_PREFIXES = ("GH_", "GIT_")
FORBIDDEN_FLAGS = ("--remote-control",)


class Refused(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def in_sandbox() -> bool:
    """The certified jarvis-sandbox boundary (spec 201 D1): its hostname, and no Windows or WSL interop paths."""
    return socket.gethostname() == SANDBOX_HOSTNAME and not Path("/mnt/c").exists() and not Path("/run/WSL").exists()


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def model_ids() -> dict[str, str]:
    return {"flash": os.environ.get("JARVISOS_AGY_FLASH_MODEL", FLASH_MODEL),
            "argon": os.environ.get("JARVISOS_AGY_ARGON_MODEL", ARGON_MODEL), **FIXED_MODELS}


def resolve_model(requested: str, coordinator_fallback: bool = False) -> tuple[str, str]:
    """(alias, model id) for an allowlisted model, else a typed refusal. Nothing outside the list reaches agy.
    `coordinator_fallback` is accepted for old launch lines; spec 255 made Argon a normal worker."""
    ids = model_ids()
    alias = next((a for a, mid in ids.items() if requested in (a, mid)), None)
    if alias is None:
        raise Refused("model_not_allowed", requested)
    return alias, ids[alias]


def child_env(base: dict[str, str] | None = None, gh_config: Path | None = None) -> dict[str, str]:
    """The parent env without GitHub/Git credentials. gh falls back to ~/.config/gh when GH_CONFIG_DIR is unset,
    so the worker gets an empty gh config dir; git loses the global/system credential helpers."""
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if k not in STRIPPED_ENV and not k.startswith(STRIPPED_PREFIXES)}
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if gh_config is not None:
        env["GH_CONFIG_DIR"] = str(gh_config)
    return env


def agy_binary() -> str:
    configured = os.environ.get("JARVISOS_AGY_BIN")
    found = shutil.which(configured) if configured else shutil.which("agy")
    if not found:
        raise Refused("agy_missing", configured or "agy")
    return found


def read_task(path: Path) -> tuple[str, str]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise Refused("task_unreadable", str(error)) from error
    if len(raw) > MAX_TASK_BYTES:
        raise Refused("task_too_large", f"{len(raw)} > {MAX_TASK_BYTES}")
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        raise Refused("task_empty", str(path))
    return text, hashlib.sha256(raw).hexdigest()


def git(worktree: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True,
                          env=child_env(), check=False)
    if check and done.returncode != 0:
        raise Refused("git_failed", f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def check_worktree(worktree: Path) -> tuple[str, Path, Path]:
    """Linked, existing, non-main, clean worktree; returns (starting HEAD, its git dir, the shared common dir)."""
    if not worktree.is_dir():
        raise Refused("worktree_missing", str(worktree))
    if not (worktree / ".git").is_file():
        raise Refused("not_linked_worktree", f"{worktree}/.git is not a file (main checkout or not a worktree)")
    git_dir = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-dir").strip())
    common = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    if git_dir == common:
        raise Refused("main_checkout", str(worktree))
    if git(worktree, "status", "--porcelain", "--untracked-files=all").strip():
        raise Refused("worktree_dirty", str(worktree))
    return git(worktree, "rev-parse", "HEAD").strip(), git_dir, common


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def git_fingerprint(git_dir: Path, common: Path) -> dict[str, Any]:
    """Hashes of the Git metadata a worker could plant to run code in later coordinator git commands."""
    hooks = common / "hooks"
    listing = sorted((p.name, _sha(p)) for p in hooks.iterdir() if p.is_file()) if hooks.is_dir() else []
    return {"common_config": _sha(common / "config"), "worktree_config": _sha(git_dir / "config.worktree"),
            "hooks": [list(item) for item in listing]}


def json_document(text: str) -> dict[str, Any] | None:
    for candidate in [text, *reversed(text.strip().splitlines())]:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def buckets(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 6:
        return []
    if isinstance(node, list):
        return [b for item in node for b in buckets(item, depth + 1)]
    if not isinstance(node, dict):
        return []
    own = node.get("buckets")
    found = [b for b in own if isinstance(b, dict)] if isinstance(own, list) else []
    return found + [b for key, value in node.items() if key != "buckets" for b in buckets(value, depth + 1)]


def target_buckets(alias: str) -> tuple[str, ...]:
    return FAMILY_BUCKETS[alias.split("-")[0]]


USAGE_KEYS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")


def clean_usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, int] = {}
    for k in USAGE_KEYS:
        if k in raw:
            v = raw[k]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                return None
            out[k] = v
    if not out or "total_tokens" not in out:
        return None
    return out


def read_quota_buckets(agy: str, alias: str, env: dict[str, str], allow_zero: bool = False) -> list[dict[str, Any]]:
    try:
        done = subprocess.run([agy, "-p", "/quota", "--output-format", "json"], capture_output=True, text=True,
                              timeout=QUOTA_TIMEOUT, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refused("quota_unreadable", f"{type(error).__name__}: {error}") from error
    output = f"{done.stdout}\n{done.stderr}"
    reading = json_document(done.stdout)
    if "sign in" in output.lower():
        raise Refused("not_signed_in", output.strip()[:300])
    if reading is None:
        raise Refused("quota_unreadable", output.strip()[:300])
    wanted = target_buckets(alias)
    target = [b for b in buckets(reading) if b.get("id") in wanted]
    for bucket in target:
        fraction = bucket.get("remaining_fraction")
        if bucket.get("disabled") is True or isinstance(fraction, bool) or not isinstance(fraction, int | float) \
                or (not allow_zero and fraction <= 0):
            raise Refused("quota_exhausted", str(bucket.get("id")))
    if len(target) != len(wanted) or {b.get("id") for b in target} != set(wanted):
        raise Refused("quota_bucket_missing", "missing " + ", ".join(sorted(set(wanted) - {b.get("id") for b in target})))
    return target


def quota_preflight(agy: str, alias: str, env: dict[str, str]) -> list[dict[str, Any]]:
    """Target buckets of a no-model-turn `/quota` reading, or a typed refusal."""
    return read_quota_buckets(agy, alias, env, allow_zero=False)


def read_quota_after(agy: str, alias: str, env: dict[str, str]) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Target buckets of a no-model-turn `/quota` reading after the run. Never raises."""
    try:
        return read_quota_buckets(agy, alias, env, allow_zero=True), None
    except Refused as exc:
        reason = exc.reason + (f": {exc.detail}" if exc.detail else "")
        return None, reason
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def activity_log_path(evidence: Path) -> Path:
    configured = os.environ.get("JARVISOS_AGY_ACTIVITY_LOG")
    if configured:
        return Path(configured)
    return evidence.resolve().parent / ".worker_runs.jsonl"


def log_run_start(path: Path, run_id: str, started_at: datetime, ts: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"run_id": run_id, "event": "start", "started_at": iso(started_at), "ts": ts}
        line = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


def log_run_end(path: Path, run_id: str, finished_at: datetime, ts: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"run_id": run_id, "event": "end", "finished_at": iso(finished_at), "ts": ts}
        line = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


def check_overlap(path: Path, run_id: str, this_start: float, this_end: float) -> bool | None:
    try:
        if not path.is_file():
            return False
        runs: dict[str, dict[str, float]] = {}
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(data, dict):
                    continue
                rid = data.get("run_id")
                if not isinstance(rid, str) or not rid:
                    continue
                ev = data.get("event")
                ts = data.get("ts")
                if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                    continue
                info = runs.setdefault(rid, {})
                if ev == "start":
                    info["start"] = float(ts)
                elif ev == "end":
                    info["end"] = float(ts)
    except OSError:
        return None

    s1 = this_start
    e1 = max(this_end, this_start + 0.001)

    for peer_id, info in runs.items():
        if peer_id == run_id or "start" not in info:
            continue
        s2 = info["start"]
        if "end" in info:
            e2 = max(info["end"], s2 + 0.001)
        else:
            e2 = s2 + MAX_TIMEOUT + KILL_MARGIN
            if s1 >= e2:
                continue
        if max(s1, s2) < min(e1, e2):
            return True

    return False


def run_argv(agy: str, task: str, model: str, timeout: int, resume: str | None = None) -> list[str]:
    argv = [agy, "-p", task, "--model", model, "--output-format", "json", "--mode", "accept-edits",
            "--dangerously-skip-permissions", "--sandbox", "--print-timeout", f"{timeout}s"]
    if resume is not None:
        # Retry the same model in its own interrupted conversation (full context and scratch artifacts).
        if not relay_argon.CONVERSATION_RE.fullmatch(resume):
            raise Refused("invalid_conversation_id")
        argv += ["--conversation", resume]
    if any(flag in argv[3:] for flag in FORBIDDEN_FLAGS):
        raise Refused("forbidden_flag")
    return argv


def collect_diff(worktree: Path, start_head: str) -> str:
    parts = [git(worktree, "diff", "--no-ext-diff", "--no-textconv", start_head, check=False)]
    for name in git(worktree, "ls-files", "--others", "--exclude-standard", "-z", check=False).split("\0"):
        if name:
            parts.append(git(worktree, "diff", "--no-ext-diff", "--no-textconv", "--no-index", "--", "/dev/null", name, check=False))
    return "".join(parts)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def default_evidence_dir() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path.home() / "jarvis-control" / "work" / "evidence" / f"agy-worker-{stamp}-{secrets.token_hex(3)}"


def execute(args: argparse.Namespace, sandbox_check: Callable[[], bool] | None = None) -> tuple[Path | None, dict[str, Any]]:
    """Run the wrapper; returns (result.json path or None, result). Never raises for refusals."""
    started = now_utc()
    started_ts = time.time()
    worktree = Path(args.worktree).expanduser()
    evidence = Path(args.evidence_dir).expanduser() if args.evidence_dir else default_evidence_dir()
    result: dict[str, Any] = {
        "schema": SCHEMA, "status": "refused", "reason": None, "model": None, "alias": None,
        "worktree": str(worktree), "start_head": None, "exit_code": None, "started_at": iso(started),
        "finished_at": None, "quota": [], "quota_after": None, "usage": None, "overlap": None, "argv": None}
    try:
        evidence.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        result["reason"] = f"evidence_dir_uncreatable: {error}"
        return None, result
    run_id = secrets.token_hex(8)
    log_path = activity_log_path(evidence)
    logged = False
    try:
        if not (sandbox_check or in_sandbox)():
            raise Refused("not_sandbox")
        alias, model = resolve_model(args.model, args.coordinator_fallback)
        result.update(alias=alias, model=model)
        task, digest = read_task(Path(args.task_file))
        result["start_head"], git_dir, common = check_worktree(worktree)
        if not 0 < args.timeout <= MAX_TIMEOUT:
            raise Refused("timeout_out_of_range", f"{args.timeout} not in 1..{MAX_TIMEOUT}")
        log_run_start(log_path, run_id, started, started_ts)
        logged = True
        _run(args, result, evidence, sandbox_check or in_sandbox, alias=alias, model=model,
             task=task, digest=digest, git_dir=git_dir, common=common)
    except Refused as refusal:
        result.update(status="refused", reason=refusal.reason + (f": {refusal.detail}" if refusal.detail else ""))
    finished = now_utc()
    finished_ts = time.time()
    result["finished_at"] = iso(finished)
    if logged:
        log_run_end(log_path, run_id, finished, finished_ts)
        result["overlap"] = check_overlap(log_path, run_id, started_ts, finished_ts)
    path = evidence / "result.json"
    write_json(path, result)
    return path, result


def _run(args: argparse.Namespace, result: dict[str, Any], evidence: Path, sandbox_check: Callable[[], bool],
         alias: str | None = None, model: str | None = None, task: str | None = None,
         digest: str | None = None, git_dir: Path | None = None, common: Path | None = None) -> None:
    worktree = Path(args.worktree).expanduser()
    if alias is None:
        if not sandbox_check():
            raise Refused("not_sandbox")
        alias, model = resolve_model(args.model, args.coordinator_fallback)
        result.update(alias=alias, model=model)
        task, digest = read_task(Path(args.task_file))
        result["start_head"], git_dir, common = check_worktree(worktree)
        if not 0 < args.timeout <= MAX_TIMEOUT:
            raise Refused("timeout_out_of_range", f"{args.timeout} not in 1..{MAX_TIMEOUT}")
    assert model is not None and task is not None and digest is not None and git_dir is not None and common is not None
    agy = agy_binary()
    gh_config = evidence / "gh-config-empty"
    gh_config.mkdir(exist_ok=True)
    env = child_env(gh_config=gh_config)
    try:
        relay_argon.prepare_login()
    except relay_argon.Refused as error:
        raise Refused(str(error)) from error
    result["quota"] = quota_preflight(agy, alias, env)
    argv = run_argv(agy, task, model, args.timeout, getattr(args, "resume_conversation", None))
    result["argv"] = [f"<task-file:{args.task_file} {digest}>" if item == task else item for item in argv]
    before = git_fingerprint(git_dir, common)
    status, reason = "succeeded", None
    try:
        proc = subprocess.Popen(argv, cwd=worktree, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
    except OSError as error:
        stdout, stderr, status, reason = "", str(error), "failed", f"spawn_failed: {error}"
    else:
        try:
            stdout, stderr = proc.communicate(timeout=args.timeout + KILL_MARGIN)
            result["exit_code"] = proc.returncode
            if proc.returncode != 0:
                status, reason = "failed", f"exit_code_{proc.returncode}"
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
            status, reason = "timeout", f"exceeded {args.timeout + KILL_MARGIN}s"
    after = git_fingerprint(git_dir, common)
    if after != before:
        status, reason = "failed", "git_metadata_modified"
        result["git_metadata"] = {"before": before, "after": after}
    document = json_document(stdout)
    if document is not None:
        (evidence / "transcript.json").write_text(stdout, encoding="utf-8")
    else:
        (evidence / "transcript.txt").write_text(stdout, encoding="utf-8")
    (evidence / "stderr.txt").write_text(stderr, encoding="utf-8")
    diff = ""
    if reason != "git_metadata_modified":  # never run git over planted config or hooks
        diff = collect_diff(worktree, result["start_head"])
        (evidence / "diff.patch").write_text(diff, encoding="utf-8")
    if not (reason or "").startswith("spawn_failed"):
        quota_after, quota_after_error = read_quota_after(agy, alias, env)
        result["quota_after"] = quota_after
        if quota_after_error is not None:
            result["quota_after_error"] = quota_after_error
    result["usage"] = clean_usage(document.get("usage")) if isinstance(document, dict) else None
    if reason != "git_metadata_modified" and status != "timeout" and not (reason or "").startswith("spawn_failed"):
        runner_cause = relay_argon.harness_cause(relay_argon.native_error(stderr))
        if document is not None:
            denied = document.get("denied_actions")
            if isinstance(denied, list) and denied:
                actions = ", ".join(str(item.get("action", item)) if isinstance(item, dict) else str(item)
                                    for item in denied)
                status, reason = "failed", f"permission_denied: {actions}"
            elif document.get("status") != "SUCCESS":
                status, reason = "failed", f"stream_status_{document.get('status') or 'missing'}"
            elif status != "failed" and runner_cause is not None:
                status, reason = "failed", f"runner_{runner_cause[1]}"
            elif status != "failed" and not str(document.get("response") or "").strip():
                status, reason = "failed", "empty_result"
        else:
            if status != "failed" and runner_cause is not None:
                status, reason = "failed", f"runner_{runner_cause[1]}"
            elif status != "failed" and not stdout.strip():
                status, reason = "failed", "empty_result"
    cid = document.get("conversation_id") if isinstance(document, dict) else None
    result.update(status=status, reason=reason, failure_class=failure_class(status, reason, stderr),
                  diff_bytes=len(diff.encode("utf-8")),
                  conversation_id=cid if isinstance(cid, str) and relay_argon.CONVERSATION_RE.fullmatch(cid) else None)


def failure_class(status: str, reason: str | None, stderr: str) -> str | None:
    """Why a worker run did not succeed, so the coordinator never mistakes the harness for the model.
    harness / provider_quota: retry the same model (after the reset for quota); this is no capability verdict.
    policy: the run touched git metadata. model_or_unknown: judge the delivered diff before escalating."""
    if status == "succeeded":
        return None
    if reason == "git_metadata_modified":
        return "policy"
    if reason == "empty_result" or (reason or "").startswith(("permission_denied:", "stream_status_")):
        return "harness"
    if status == "timeout" or reason == "timeout" or (reason or "").startswith("spawn_failed"):
        return "harness"
    cause = relay_argon.harness_cause(relay_argon.native_error(stderr))
    if cause is not None:
        return "provider_quota" if cause[0] == "quota" else "harness"
    if (reason or "").startswith("runner_"):
        return "provider_quota" if "quota" in reason else "harness"
    return "model_or_unknown"


def cmd_run(args: argparse.Namespace) -> int:
    path, result = execute(args)
    print(f"result: {path}")
    print(f"status: {result['status']}" + (f" ({result['reason']})" if result["reason"] else ""))
    if result.get("failure_class"):
        advice = {"harness": "retry the same model, never another provider for this; not a capability verdict",
                  "provider_quota": "retry the same model after its reset; not a capability verdict",
                  "policy": "the run touched git metadata",
                  "model_or_unknown": "judge the delivered diff; a one-off slip gets one more try on the same model "
                                      "from the salvaged diff, and only a repeat escalates (salvaging again)"}
        print(f"failure_class: {result['failure_class']} ({advice[result['failure_class']]}); "
              f"diff_bytes: {result.get('diff_bytes', 0)}")
        if result.get("conversation_id") and result["failure_class"] != "policy":
            print(f"retry in the same conversation: --resume-conversation {result['conversation_id']}")
    return {"succeeded": 0, "refused": 2}.get(result["status"], 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one headless agy worker turn")
    run.add_argument("--task-file", required=True)
    run.add_argument("--worktree", required=True)
    run.add_argument("--model", default="flash")
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    run.add_argument("--evidence-dir")
    run.add_argument("--coordinator-fallback", action="store_true")
    run.add_argument("--resume-conversation", help="retry in this earlier run's agy conversation (its result's "
                                                   "conversation_id) instead of a new one")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
