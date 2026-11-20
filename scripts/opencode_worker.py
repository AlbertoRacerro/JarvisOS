#!/usr/bin/env python3
"""Run bounded temporary OpenAI-compatible pools through the OpenCode CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "opencode-worker.v1"
DEFAULT_TIMEOUT = 1800
MAX_TIMEOUT = 6 * 3600
KILL_MARGIN = 60
MAX_TASK_BYTES = 200 * 1024
KEY_ENV = "TEMP_POOL_API_KEY"
ZEN = "opencode_zen"
ZEN_PROVIDER = "opencode"  # OpenCode's built-in Zen provider id


class Refused(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def iso(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def control_dir() -> Path:
    return Path(os.environ.get("RELAY_CONTINUITY_DIR",
                               Path.home() / "jarvis-control" / "work" / "continuity")).expanduser()


def temp_pools_file() -> Path:
    override = os.environ.get("TEMP_POOLS_FILE")
    if override:
        return Path(override).expanduser()
    return control_dir().parent / "temp-pools.json"


def load_pools() -> list[dict[str, Any]]:
    try:
        data = json.loads(temp_pools_file().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    entries = data if isinstance(data, list) else data.get("pools") if isinstance(data, dict) else None
    return [entry for entry in entries or [] if isinstance(entry, dict)]


def find_pool(name: str) -> dict[str, Any]:
    for entry in load_pools():
        if entry.get("name") == name:
            return entry
    raise Refused("pool_not_found", name)


def parse_expiry(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def validate_pool(entry: dict[str, Any]) -> tuple[Path | None, str | None, str, str, str]:
    """(key path, base URL, model, provider, free list). OpenCode's built-in Zen provider needs neither key nor URL."""
    if entry.get("expires_at") is not None:
        expires = parse_expiry(entry.get("expires_at"))
        if expires is None:
            raise Refused("invalid_pool")
        if datetime.now(UTC) >= expires:
            raise Refused("pool_expired")
    free_list = entry.get("free_list")
    if free_list not in ("openai_models", "openrouter_public", "opencode_zen"):
        raise Refused("invalid_free_list")
    model = entry.get("model")
    provider = entry.get("provider")
    if not isinstance(model, str) or not model.strip():
        raise Refused("invalid_model")
    if not isinstance(provider, str) or not provider.strip():
        raise Refused("invalid_provider")
    if free_list == ZEN:
        return None, None, model.strip(), provider.strip(), free_list
    base_url = entry.get("base_url")
    key_file = entry.get("key_file")
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise Refused("invalid_base_url")
    if not isinstance(key_file, str) or not Path(key_file).is_absolute():
        raise Refused("invalid_key_file")
    key_path = Path(key_file).expanduser()
    if key_path.is_symlink():
        raise Refused("invalid_key_file", "symlink")
    if not key_path.is_file():
        raise Refused("key_missing")
    if key_path.resolve().is_relative_to(repo_root().resolve()):
        raise Refused("invalid_key_file", "inside repository")
    return key_path, base_url, model.strip(), provider.strip(), free_list


def git(worktree: Path, *args: str, check: bool = True) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True,
                              text=True, check=False)
    except OSError as error:
        raise Refused("git_failed", str(error)) from error
    if check and proc.returncode != 0:
        raise Refused("git_failed", proc.stderr.strip())
    return proc.stdout


def check_worktree(worktree: Path) -> tuple[str, list[str]]:
    if not worktree.is_dir():
        raise Refused("worktree_missing")
    if not (worktree / ".git").is_file():
        raise Refused("not_linked_worktree")
    common = git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    git_dir = git(worktree, "rev-parse", "--path-format=absolute", "--git-dir").strip()
    if not common or common == git_dir:
        raise Refused("main_checkout")
    try:
        common_resolved = Path(common).resolve()
        source_common = git(repo_root(), "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        if not source_common or common_resolved != Path(source_common).resolve():
            raise Refused("outside_repository") from None
    except OSError:
        raise Refused("outside_repository") from None
    head = git(worktree, "rev-parse", "HEAD").strip()
    status = git(worktree, "status", "--porcelain", "--untracked-files=all").strip()
    changed = [line[3:] for line in status.splitlines() if len(line) >= 4]
    return head, changed


def read_task(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise Refused("task_unreadable", str(error)) from error
    if len(raw) > MAX_TASK_BYTES:
        raise Refused("task_too_large")
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        raise Refused("task_empty")
    return text


def evidence_dir() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return control_dir().parent / "evidence" / f"opencode-worker-{stamp}"


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def child_env(scratch: Path, key: str | None) -> dict[str, str]:
    """Home and config live in a scratch directory outside the control directory, so the child sees no evidence."""
    home = scratch / "home"
    config = scratch / "xdg-config"
    data = scratch / "xdg-data"
    for path in (home, config, data):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    path = os.environ.get("PATH", "/usr/bin:/bin")
    return {
        "PATH": path,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(data),
        "LANG": "C.UTF-8",
        **({KEY_ENV: key} if key is not None else {}),
    }


def sandbox_argv(binary: str, worktree: Path, scratch: Path) -> list[str]:
    """bubblewrap around the child: the user's home, /tmp and /mnt (Windows drives) are empty tmpfs, so the control
    directory, private repositories, packs, keys and credentials are not on the child's filesystem at all. Only the
    worktree and the scratch home are writable; the OpenCode package and this repository's git dir are read-only."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise Refused("sandbox_missing", "bubblewrap (bwrap) is required")
    common = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve()
    own_git_dir = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-dir").strip()).resolve()
    package = Path(binary).resolve().parent.parent
    home = Path.home().resolve()
    if package == home or home.is_relative_to(package) or (
            package.is_relative_to(home) and not _is_opencode_package(package)):
        # A loose binary under the home would bind back a whole private subtree.
        raise Refused("package_unrecognised", str(package))
    argv = [bwrap, "--die-with-parent", "--unshare-pid", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", str(Path.home()), "--tmpfs", "/tmp", "--tmpfs", "/mnt"]
    if Path("/mnt/wsl").is_dir():  # WSL keeps resolv.conf here
        argv += ["--ro-bind", "/mnt/wsl", "/mnt/wsl"]
    argv += ["--ro-bind", str(package), str(package), "--ro-bind", str(common), str(common),
             "--ro-bind", str(own_git_dir), str(own_git_dir), "--bind", str(scratch), str(scratch), "--bind", str(worktree.resolve()), str(worktree.resolve()),
             "--chdir", str(worktree.resolve()), "--"]
    return argv


def _is_opencode_package(package: Path) -> bool:
    try:
        name = json.loads((package / "package.json").read_text(encoding="utf-8")).get("name")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return False
    return isinstance(name, str) and name.startswith("@opencode/")


def opencode_binary() -> str:
    configured = os.environ.get("JARVISOS_OPENCODE_BIN")
    found = shutil.which(configured) if configured else shutil.which("opencode")
    if not found:
        raise Refused("opencode_missing")
    return str(Path(found).resolve())  # the sandbox binds the package, not the launcher symlink


def build_config(scratch: Path, name: str, provider: str, base_url: str | None, model: str) -> Path:
    """OpenCode 2.x config with only this provider (none for Zen: the built-in provider, no key). `--auto` approves
    every permission not explicitly denied, so files outside the worktree, web fetches and session sharing are
    denied here."""
    config_dir = scratch / "xdg-config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "permission": {"external_directory": "deny", "webfetch": "deny"},
    }
    if base_url is not None:
        document["provider"] = {
            re_safe(name): {
                "npm": "@ai-sdk/openai-compatible",
                "name": provider,
                "options": {"baseURL": base_url, "apiKey": "{env:" + KEY_ENV + "}"},
                "models": {model: {"name": model}},
            }
        }
    path = config_dir / "opencode.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def redact(text: str, key: str | None) -> str:
    return text.replace(key, "[REDACTED]") if key else text


def re_safe(value: str) -> str:
    result = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "-" for ch in value)
    return result.strip("-") or "temp"


def changed_files(worktree: Path) -> list[str]:
    try:
        status = git(worktree, "status", "--porcelain", "--untracked-files=all").strip()
    except Refused:
        return []
    return [line[3:] for line in status.splitlines() if len(line) >= 4]


def smoke_tasks() -> list[dict[str, str]]:
    base = repo_root() / "scripts" / "temp_pool_qualification"
    return [
        {"id": "tempsmoke-tier1-add", "task": str(base / "tier1-add" / "task.md"),
         "check": str(base / "tier1-add" / "check.py"), "tier": "1"},
        {"id": "tempsmoke-tier1-even", "task": str(base / "tier1-even" / "task.md"),
         "check": str(base / "tier1-even" / "check.py"), "tier": "1"},
        {"id": "tempsmoke-tier2-merge", "task": str(base / "tier2-merge" / "task.md"),
         "check": str(base / "tier2-merge" / "check.py"), "tier": "2"},
    ]


def execute_smoke(args: argparse.Namespace) -> int:
    try:
        entry = find_pool(args.pool)
        validate_pool(entry)
    except Refused as refusal:
        sys.stderr.write(f"smoke refused: {refusal.reason}\n")
        return 2

    sys.path.insert(0, str(repo_root() / "scripts"))
    import worker_routing as routing

    evidence_root = evidence_dir() / "smoke"
    evidence_root.mkdir(parents=True, exist_ok=True)
    base = repo_root()
    outcomes: list[dict[str, Any]] = []
    for task in smoke_tasks():
        tempdir = Path(tempfile.mkdtemp(prefix="temp-pool-", dir=evidence_root))
        worktree = tempdir / "worktree"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        try:
            ref = "origin/master"
            probe = subprocess.run(["git", "-C", str(base), "rev-parse", "--verify", ref],
                                   capture_output=True, text=True, check=False)
            if probe.returncode != 0:
                ref = "HEAD"
            add = subprocess.run(["git", "-C", str(base), "worktree", "add", "--detach",
                                  str(worktree), ref], capture_output=True, text=True, check=False)
            if add.returncode != 0:
                raise Refused("worktree_create_failed", add.stderr.strip())
            task_args = argparse.Namespace(pool=args.pool, task_file=task["task"], worktree=str(worktree),
                                           timeout=args.timeout,
                                           evidence_dir=str(tempdir / "evidence"))
            _, run_result = execute_run(task_args)
            check = subprocess.run([sys.executable, "-B", task["check"]], cwd=worktree,
                                   env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(worktree)},
                                   capture_output=True, text=True, timeout=args.timeout, check=False)
            outcome = {"task": task["id"], "passed": run_result["status"] == "succeeded" and check.returncode == 0,
                       "run_status": run_result["status"], "check_exit": check.returncode}
        except (Refused, OSError, subprocess.TimeoutExpired) as error:
            outcome = {"task": task["id"], "passed": False, "run_status": "failed",
                       "check_exit": getattr(error, "returncode", None)}
        finally:
            subprocess.run(["git", "-C", str(base), "worktree", "remove", "--force", str(worktree)],
                           capture_output=True, text=True, check=False)
        outcomes.append(outcome)
        result_file = tempdir / "evidence" / "result.json"
        # A run that did not finish is the bridge's failure (harness), never the model's; nothing here sets a tier.
        failure = "harness" if outcome["run_status"] != "succeeded" else "none" if outcome["passed"] else "model"
        routing.record(task=task["id"], kind="tests", worker=f"temp-{args.pool}", tier=int(task["tier"]),
                       ci_first_pass="pass" if outcome["passed"] else "fail", failure=failure,
                       evidence=result_file if result_file.is_file() else None,
                       record_file=getattr(args, "record_file", None))

    bridge = "ok" if all(item["passed"] for item in outcomes) else "suspect"
    routing.set_bridge_state(f"temp-{args.pool}", bridge)
    write_json(evidence_root / "smoke.json", {"pool": args.pool, "bridge": bridge, "outcomes": outcomes})
    print(f"pool: temp-{args.pool}")
    print(f"bridge: {bridge}")
    return 0


def execute_run(args: argparse.Namespace) -> tuple[Path | None, dict[str, Any]]:
    started = datetime.now(UTC)
    worktree = Path(args.worktree).expanduser()
    evidence = Path(args.evidence_dir).expanduser() if args.evidence_dir else evidence_dir()
    result: dict[str, Any] = {
        "schema": SCHEMA, "pool": args.pool, "model": None, "status": "refused",
        "reason": None, "exit_code": None, "started_at": iso(started),
        "finished_at": None, "duration_seconds": 0, "changed_files": [],
    }
    try:
        evidence.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        result["reason"] = f"evidence_dir_uncreatable: {error}"
        return None, result
    try:
        if os.environ.get("JARVISOS_PACK_PATH"):
            raise Refused("private_pack_visible")
        entry = find_pool(args.pool)
        key_path, base_url, model, provider, free_list = validate_pool(entry)
        key: str | None = None
        if key_path is not None:
            try:
                key = key_path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as error:
                raise Refused("key_unreadable") from error
            if not key:
                raise Refused("key_missing")
        task = read_task(Path(args.task_file))
        start_head, _ = check_worktree(worktree)
        if not 0 < args.timeout <= MAX_TIMEOUT:
            raise Refused("timeout_out_of_range")
        binary = opencode_binary()
        scratch = Path(tempfile.mkdtemp(prefix="opencode-worker-"))
        try:
            config = build_config(scratch, args.pool, provider, base_url, model)
            shutil.copyfile(config, evidence / "opencode.json")
            env = child_env(scratch, key)
            sandbox = sandbox_argv(binary, worktree, scratch)
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        # --standalone: a private server in this environment, never the user's background service.
        model_ref = f"{ZEN_PROVIDER}/{model}" if free_list == ZEN else f"{re_safe(args.pool)}/{model}"
        argv = sandbox + [binary, "run", "--standalone", "--model", model_ref,
                "--format", "json", "--auto", task]
        result["model"] = model
        proc_started = datetime.now(UTC)
        try:
            proc = subprocess.Popen(argv, cwd=worktree, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True)
        except OSError as error:
            result.update(status="failed", reason=f"spawn_failed: {error}", exit_code=None)
        else:
            try:
                stdout, stderr = proc.communicate(timeout=args.timeout + KILL_MARGIN)
                result["exit_code"] = proc.returncode
                if proc.returncode == 0:
                    result["status"] = "succeeded"
                else:
                    result.update(status="failed", reason=f"exit_code_{proc.returncode}")
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = proc.communicate()
                result.update(status="timeout", reason=f"exceeded {args.timeout + KILL_MARGIN}s")
                result["exit_code"] = None
            (evidence / "transcript.json").write_text(redact(stdout or "", key), encoding="utf-8")
            (evidence / "stderr.txt").write_text(redact(stderr or "", key), encoding="utf-8")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        result["duration_seconds"] = round((datetime.now(UTC) - proc_started).total_seconds(), 3)
        try:
            end_head = git(worktree, "rev-parse", "HEAD").strip()
            if end_head != start_head:
                result.update(status="failed", reason="worker_commit_detected")
        except Refused:
            result.update(status="failed", reason="git_verification_failed")
        result["changed_files"] = changed_files(worktree)
    except Refused as refusal:
        result.update(status="refused",
                      reason=refusal.reason + (f": {refusal.detail}" if refusal.detail else ""))
    finished = datetime.now(UTC)
    result["finished_at"] = iso(finished)
    result["duration_seconds"] = result.get("duration_seconds") or round((finished - started).total_seconds(), 3)
    path = evidence / "result.json"
    write_json(path, result)
    return path, result


def cmd_run(args: argparse.Namespace) -> int:
    path, result = execute_run(args)
    print(f"result: {path}")
    print(f"status: {result['status']}" + (f" ({result['reason']})" if result["reason"] else ""))
    return 0 if result["status"] == "succeeded" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--pool", required=True)
    run.add_argument("--task-file", required=True)
    run.add_argument("--worktree", required=True)
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    run.add_argument("--evidence-dir")
    run.set_defaults(func=cmd_run)
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--pool", required=True)
    smoke.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    smoke.set_defaults(func=execute_smoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    value = args.func(args)
    return int(value)


if __name__ == "__main__":
    sys.exit(main())
