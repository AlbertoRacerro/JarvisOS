#!/usr/bin/env python3
"""Antigravity (`agy`) delegated-worker wrapper (spec 221).

Runs one headless `agy` worker turn inside the certified sandbox, in a linked git worktree, with GitHub and global
Git credentials removed from the child environment, behind an allowlist of models and a no-model-turn quota
preflight (spec 217 probe). Evidence (transcript, diff against the starting HEAD, result.json) is written to an
evidence directory. The wrapper writes no ledger entry and makes no commit: the coordinator reviews the diff.
Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
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
DEFAULT_TIMEOUT = 1800
MAX_TIMEOUT = 6 * 3600
QUOTA_TIMEOUT = 60
KILL_MARGIN = 60
MAX_TASK_BYTES = 200 * 1024
STRIPPED_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GIT_CONFIG_GLOBAL", "SSH_AUTH_SOCK",
                "GH_ENTERPRISE_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN")
STRIPPED_PREFIXES = ("GH_", "GIT_")
FORBIDDEN_FLAGS = ("--dangerously-skip-permissions", "--remote-control")


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
            "argon": os.environ.get("JARVISOS_AGY_ARGON_MODEL", ARGON_MODEL)}


def resolve_model(requested: str, coordinator_fallback: bool) -> tuple[str, str]:
    """(alias, model id) for an allowlisted model, else a typed refusal. Nothing outside the list reaches agy."""
    ids = model_ids()
    lowered = requested.lower()
    if lowered.startswith("3p") or "claude" in lowered or "gpt" in lowered:
        raise Refused("model_not_allowed", requested)
    alias = next((a for a, mid in ids.items() if requested in (a, mid)), None)
    if alias is None:
        raise Refused("model_not_allowed", requested)
    if alias == "argon" and not coordinator_fallback:
        raise Refused("argon_requires_coordinator_fallback", requested)
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


def target_prefix(alias: str) -> str:
    return "gemini-" if alias == "flash" else "argon-"


def quota_preflight(agy: str, alias: str, env: dict[str, str]) -> list[dict[str, Any]]:
    """Target buckets of a no-model-turn `/quota` reading, or a typed refusal."""
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
    prefix = target_prefix(alias)
    target = [b for b in buckets(reading) if str(b.get("id", "")).startswith(prefix)]
    if not target:
        raise Refused("quota_bucket_missing", f"no bucket id starting {prefix!r}")
    for bucket in target:
        fraction = bucket.get("remaining_fraction")
        if bucket.get("disabled") is True or not isinstance(fraction, int | float) or fraction <= 0:
            raise Refused("quota_exhausted", str(bucket.get("id")))
    return target


def run_argv(agy: str, task: str, model: str, timeout: int, resume: str | None = None) -> list[str]:
    argv = [agy, "-p", task, "--model", model, "--output-format", "json", "--mode", "accept-edits", "--sandbox",
            "--print-timeout", f"{timeout}s"]
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
    return Path.home() / "jarvis-control" / "work" / "evidence" / f"agy-worker-{stamp}"


def execute(args: argparse.Namespace, sandbox_check: Callable[[], bool] | None = None) -> tuple[Path | None, dict[str, Any]]:
    """Run the wrapper; returns (result.json path or None, result). Never raises for refusals."""
    started = now_utc()
    worktree = Path(args.worktree).expanduser()
    evidence = Path(args.evidence_dir).expanduser() if args.evidence_dir else default_evidence_dir()
    result: dict[str, Any] = {
        "schema": SCHEMA, "status": "refused", "reason": None, "model": None, "alias": None,
        "worktree": str(worktree), "start_head": None, "exit_code": None, "started_at": iso(started),
        "finished_at": None, "quota": [], "argv": None}
    try:
        evidence.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        result["reason"] = f"evidence_dir_uncreatable: {error}"
        return None, result
    try:
        _run(args, result, evidence, sandbox_check or in_sandbox)
    except Refused as refusal:
        result.update(status="refused", reason=refusal.reason + (f": {refusal.detail}" if refusal.detail else ""))
    result["finished_at"] = iso(now_utc())
    path = evidence / "result.json"
    write_json(path, result)
    return path, result


def _run(args: argparse.Namespace, result: dict[str, Any], evidence: Path, sandbox_check: Callable[[], bool]) -> None:
    if not sandbox_check():
        raise Refused("not_sandbox")
    alias, model = resolve_model(args.model, args.coordinator_fallback)
    result.update(alias=alias, model=model)
    task, digest = read_task(Path(args.task_file))
    worktree = Path(args.worktree).expanduser()
    result["start_head"], git_dir, common = check_worktree(worktree)
    if not 0 < args.timeout <= MAX_TIMEOUT:
        raise Refused("timeout_out_of_range", f"{args.timeout} not in 1..{MAX_TIMEOUT}")
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
    if json_document(stdout) is not None:
        (evidence / "transcript.json").write_text(stdout, encoding="utf-8")
    else:
        (evidence / "transcript.txt").write_text(stdout, encoding="utf-8")
    (evidence / "stderr.txt").write_text(stderr, encoding="utf-8")
    diff = ""
    if reason != "git_metadata_modified":  # never run git over planted config or hooks
        diff = collect_diff(worktree, result["start_head"])
        (evidence / "diff.patch").write_text(diff, encoding="utf-8")
    document = json_document(stdout)
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
    if status == "timeout" or (reason or "").startswith("spawn_failed"):
        return "harness"
    cause = relay_argon.harness_cause(relay_argon.native_error(stderr))
    if cause is not None:
        return "provider_quota" if cause[0] == "quota" else "harness"
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
