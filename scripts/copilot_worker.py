#!/usr/bin/env python3
"""Spec 238 GitHub Copilot CLI development-time delegated-worker wrapper.

Runs one bounded turn on public repository code in a linked sandbox worktree. A no-model-turn GitHub entitlement
preflight refuses absent or exhausted quota. The child receives only Copilot-specific authentication and cannot
inherit coordinator GitHub or Git credentials. Writes evidence; never commits. Stdlib only.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "copilot-worker.v1"
SANDBOX_HOSTNAME = "jarvis-sandbox"
AUTO_MODEL = "auto"
DEFAULT_TIMEOUT = 1800
MAX_TIMEOUT = 6 * 3600
QUOTA_TIMEOUT = 30
KILL_MARGIN = 60
MAX_TASK_BYTES = 200 * 1024
STRIPPED_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GIT_CONFIG_GLOBAL", "SSH_AUTH_SOCK",
                "GH_ENTERPRISE_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN", "COPILOT_GITHUB_TOKEN",
                "JARVISOS_COPILOT_GH_TOKEN", "COPILOT_HOME")
STRIPPED_PREFIXES = ("GH_", "GIT_")
FORBIDDEN_FLAGS = ("--yolo", "--allow-all", "--remote-control")


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
    return {"copilot-auto": os.environ.get("JARVISOS_COPILOT_AUTO_MODEL", AUTO_MODEL)}


def resolve_model(requested: str) -> tuple[str, str]:
    ids = model_ids()
    alias = next((key for key, model in ids.items() if requested in (key, model)), None)
    if alias is None:
        raise Refused("model_not_allowed", requested)
    return alias, ids[alias]


def prepare_copilot_auth(home: Path | None = None, base_env: Mapping[str, str] | None = None) -> str | None:
    home = home or Path.home()
    env = os.environ if base_env is None else base_env
    state = home / ".local/share/jarvisos-sandbox/state/copilot"
    token = env.get("JARVISOS_COPILOT_GH_TOKEN") or env.get("COPILOT_GITHUB_TOKEN")
    if not token:
        try:
            token = (state / "token").read_text(encoding="utf-8").strip()
        except OSError:
            pass
    target = state / "copilot-home"
    if target.is_dir():
        link = home / ".copilot"
        if link.is_symlink():
            if link.resolve() != target.resolve():
                raise Refused("login_path_conflict", str(link))
        elif link.exists():
            raise Refused("login_path_conflict", str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    if not token and not target.is_dir():
        raise Refused("auth_missing")
    return token or None


def child_env(base: dict[str, str] | None = None, gh_config: Path | None = None,
              copilot_token: str | None = None, copilot_home: Path | None = None) -> dict[str, str]:
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if k not in STRIPPED_ENV and not k.startswith(STRIPPED_PREFIXES)}
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if gh_config is not None:
        env["GH_CONFIG_DIR"] = str(gh_config)
    if copilot_token is not None:
        env["COPILOT_GITHUB_TOKEN"] = copilot_token
    if copilot_home is not None:
        env["COPILOT_HOME"] = str(copilot_home)
    return env


def prepare_copilot_home(evidence: Path, home: Path | None = None) -> Path:
    copilot_home = evidence / "copilot-home"
    copilot_home.mkdir(parents=True, exist_ok=True)
    try:
        copilot_home.chmod(0o700)
    except OSError:
        pass
    base_home = home or Path.home()
    candidates = [
        base_home / ".copilot" / "config.json",
        base_home / ".local/share/jarvisos-sandbox/state/copilot/copilot-home/config.json",
    ]
    for src in candidates:
        if src.is_file():
            try:
                raw_cfg = json.loads(src.read_text(encoding="utf-8"))
                if isinstance(raw_cfg, dict):
                    safe_cfg = {
                        k: v for k, v in raw_cfg.items()
                        if not any(s in k.lower() for s in ("token", "secret", "password", "key", "credential"))
                    }
                    (copilot_home / "config.json").write_text(json.dumps(safe_cfg, indent=2) + "\n", encoding="utf-8")
                break
            except (OSError, json.JSONDecodeError):
                pass
    return copilot_home


def _host_which(command: str) -> str | None:
    entries = [str(Path.home() / ".local" / "bin")]
    entries += sorted(glob.glob(str(Path.home() / ".nvm" / "versions" / "node" / "*" / "bin")), reverse=True)
    entries.append(os.environ.get("PATH", "/usr/bin:/bin"))
    return shutil.which(command, path=os.pathsep.join(entries))


def copilot_binary() -> str:
    configured = os.environ.get("JARVISOS_COPILOT_BIN")
    found = shutil.which(configured) if configured else _host_which("copilot")
    if not found:
        raise Refused("copilot_missing")
    return found


def gh_binary() -> str:
    configured = os.environ.get("JARVISOS_GH_BIN")
    found = shutil.which(configured) if configured else _host_which("gh")
    if not found:
        raise Refused("gh_missing")
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


def quota_preflight(gh: str, token: str | None, base_env: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = dict(os.environ if base_env is None else base_env)
    if token:
        env["GH_TOKEN"] = token  # This process alone; never passed to the worker child.
    try:
        done = subprocess.run([gh, "api", "copilot_internal/user"], capture_output=True, text=True,
                              timeout=QUOTA_TIMEOUT, env=env, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refused("quota_unreadable", type(error).__name__) from error
    output = f"{done.stdout}\n{done.stderr}".lower()
    if any(marker in output for marker in ("notsignedin", "401", "bad credentials", "gh auth login", "not logged into")):
        raise Refused("not_signed_in")
    reading = json_document(done.stdout)
    if reading is None or done.returncode != 0:
        raise Refused("quota_unreadable")
    snapshots = reading.get("quota_snapshots")
    snap = snapshots.get("premium_interactions") if isinstance(snapshots, dict) else None
    if not isinstance(snap, dict):
        raise Refused("quota_bucket_missing")
    if reading.get("cli_enabled") is False:
        raise Refused("cli_disabled")
    if snap.get("has_quota") is False and snap.get("unlimited") is not True:
        if snap.get("entitlement") in (0, 0.0):
            raise Refused("no_entitlement", str(reading.get("access_type_sku") or "free_limited_copilot"))
        raise Refused("quota_exhausted")
    if snap.get("unlimited") is True:
        return {"id": "premium_interactions", "remaining_fraction": 1.0, "unlimited": True}
    pct = snap.get("percent_remaining")
    if isinstance(pct, bool) or not isinstance(pct, int | float) or not 0 <= pct <= 100:
        raise Refused("quota_unreadable")
    fraction = float(pct) / 100.0
    remaining = snap.get("remaining")
    if remaining is not None and (isinstance(remaining, bool) or not isinstance(remaining, int | float)
                                  or not math.isfinite(remaining)):
        raise Refused("quota_unreadable")
    if fraction <= 0 or (remaining is not None and remaining <= 0):
        raise Refused("quota_exhausted")
    return {"id": "premium_interactions", "remaining_fraction": round(fraction, 4),
            "entitlement": snap.get("entitlement"), "remaining": remaining}


def run_argv(copilot: str, task: str, model: str) -> list[str]:
    argv = [copilot, "-p", task, "--model", model, "--allow-all-tools", "--no-ask-user",
            "--deny-tool", "shell(gh:*)", "--deny-tool", "shell(git push*)",
            "--deny-tool", "shell(git remote*)", "--deny-tool", "shell(git config*)"]
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
    return Path.home() / "jarvis-control" / "work" / "evidence" / f"copilot-worker-{stamp}"


def extract_copilot_session(copilot_home: Path, fallback_home: Path | None = None,
                            started_at: datetime | None = None) -> dict[str, Any]:
    candidate_files: list[Path] = []
    try:
        candidate_files = [f for f in (copilot_home / "session-state").glob("*/events.jsonl") if f.is_file()]
    except OSError:
        candidate_files = []

    if not candidate_files:
        if fallback_home is not None:
            try:
                if fallback_home.is_dir():
                    fb_files = [f for f in (fallback_home / "session-state").glob("*/events.jsonl") if f.is_file()]
                    if started_at is not None:
                        threshold = started_at.timestamp() - 2.0
                        fb_files = [f for f in fb_files if f.stat().st_mtime >= threshold]
                    candidate_files = fb_files
            except OSError:
                candidate_files = []
        elif started_at is not None:
            fb = Path.home() / ".copilot"
            try:
                if fb.is_dir():
                    threshold = started_at.timestamp() - 2.0
                    candidate_files = [
                        f for f in (fb / "session-state").glob("*/events.jsonl")
                        if f.is_file() and f.stat().st_mtime >= threshold
                    ]
            except OSError:
                candidate_files = []

    if not candidate_files:
        return {"actual_model": None, "auto_routing": None, "usage": None}

    try:
        newest_file = max(candidate_files, key=lambda f: f.stat().st_mtime)
        lines = newest_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"actual_model": None, "auto_routing": None, "usage": None}

    auto_mode_resolved_events: list[dict[str, Any]] = []
    shutdown_events: list[dict[str, Any]] = []
    usage_record_events: list[dict[str, Any]] = []
    usage_checkpoint_events: list[dict[str, Any]] = []

    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue
        try:
            event = json.loads(line_str)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "session.auto_mode_resolved" and isinstance(event.get("data"), dict):
            auto_mode_resolved_events.append(event)
        elif etype == "session.shutdown" and isinstance(event.get("data"), dict):
            shutdown_events.append(event)
        elif etype == "session.usage_record" and isinstance(event.get("data"), dict):
            usage_record_events.append(event)
        elif etype == "session.usage_checkpoint" and isinstance(event.get("data"), dict):
            usage_checkpoint_events.append(event)

    auto_routing: dict[str, Any] | None = None
    resolved_models: list[str] = []
    if auto_mode_resolved_events:
        for ev in auto_mode_resolved_events:
            c_m = ev["data"].get("chosenModel")
            if isinstance(c_m, str) and c_m.strip():
                cleaned_m = c_m.strip()
                if cleaned_m not in resolved_models:
                    resolved_models.append(cleaned_m)
        data = auto_mode_resolved_events[-1]["data"]
        if resolved_models:
            c_raw = data.get("candidateModels")
            candidates = [c for c in c_raw if isinstance(c, str) and c] if isinstance(c_raw, list) else []
            r_raw = data.get("selectionReason")
            reason = r_raw if isinstance(r_raw, str) else None
            auto_routing = {
                "chosen_model": ",".join(resolved_models),
                "candidate_models": candidates,
                "selection_reason": reason,
            }

    actual_model: str | None = None
    if auto_routing is not None and auto_routing.get("chosen_model"):
        actual_model = auto_routing["chosen_model"]
    elif shutdown_events:
        data = shutdown_events[-1]["data"]
        cm = data.get("currentModel")
        if isinstance(cm, str) and cm.strip():
            actual_model = cm.strip()
        elif isinstance(data.get("modelMetrics"), dict) and data["modelMetrics"]:
            keys = [str(k).strip() for k in data["modelMetrics"].keys() if str(k).strip()]
            if keys:
                actual_model = ",".join(keys)
    if actual_model is None and usage_record_events:
        for ev in reversed(usage_record_events):
            d = ev.get("data")
            if isinstance(d, dict):
                u = d.get("usage")
                if isinstance(u, dict):
                    rm = u.get("responseModel") or u.get("model")
                    if isinstance(rm, str) and rm.strip():
                        actual_model = rm.strip()
                        break

    def _non_neg_int(val: Any) -> int | None:
        if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            return val
        return None

    usage: dict[str, int] | None = None
    if shutdown_events:
        data = shutdown_events[-1]["data"]
        td = data.get("tokenDetails")
        if isinstance(td, dict):
            inp = _non_neg_int(td.get("input", {}).get("tokenCount") if isinstance(td.get("input"), dict) else None)
            out = _non_neg_int(td.get("output", {}).get("tokenCount") if isinstance(td.get("output"), dict) else None)
            cr_val = td.get("cache_read", {}).get("tokenCount") if isinstance(td.get("cache_read"), dict) else 0
            cw_val = td.get("cache_write", {}).get("tokenCount") if isinstance(td.get("cache_write"), dict) else 0
            cr = _non_neg_int(cr_val) if cr_val is not None else 0
            if cr is None:
                cr = 0
            cw = _non_neg_int(cw_val) if cw_val is not None else 0
            if cw is None:
                cw = 0
            prem_raw = data.get("totalPremiumRequests", 0)
            prem = prem_raw if (isinstance(prem_raw, int) and not isinstance(prem_raw, bool) and prem_raw >= 0) else 0
            if inp is not None and out is not None and cr is not None and cw is not None:
                cache_tokens = cr + cw
                usage = {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_tokens": cache_tokens,
                    "total_tokens": inp + out + cache_tokens,
                    "premium_requests": prem,
                }
        if usage is None and isinstance(data.get("modelMetrics"), dict) and data["modelMetrics"]:
            tot_in = 0
            tot_out = 0
            tot_cache = 0
            have_metric = False
            for metric in data["modelMetrics"].values():
                if not isinstance(metric, dict):
                    continue
                m_td = metric.get("tokenDetails")
                m_u = metric.get("usage")
                if isinstance(m_td, dict):
                    m_inp = _non_neg_int(m_td.get("input", {}).get("tokenCount") if isinstance(m_td.get("input"), dict) else None)
                    m_out = _non_neg_int(m_td.get("output", {}).get("tokenCount") if isinstance(m_td.get("output"), dict) else None)
                    m_cr = _non_neg_int(m_td.get("cache_read", {}).get("tokenCount") if isinstance(m_td.get("cache_read"), dict) else 0) or 0
                    m_cw = _non_neg_int(m_td.get("cache_write", {}).get("tokenCount") if isinstance(m_td.get("cache_write"), dict) else 0) or 0
                    if m_inp is not None and m_out is not None:
                        tot_in += m_inp
                        tot_out += m_out
                        tot_cache += (m_cr + m_cw)
                        have_metric = True
                elif isinstance(m_u, dict):
                    m_in_tok = _non_neg_int(m_u.get("inputTokens")) or 0
                    m_out_tok = _non_neg_int(m_u.get("outputTokens")) or 0
                    m_cr_tok = _non_neg_int(m_u.get("cacheReadTokens")) or 0
                    m_cw_tok = _non_neg_int(m_u.get("cacheWriteTokens")) or 0
                    tot_in += max(0, m_in_tok - m_cr_tok)
                    tot_out += m_out_tok
                    tot_cache += (m_cr_tok + m_cw_tok)
                    have_metric = True
            if have_metric:
                prem_raw = data.get("totalPremiumRequests", 0)
                prem = prem_raw if (isinstance(prem_raw, int) and not isinstance(prem_raw, bool) and prem_raw >= 0) else 0
                usage = {
                    "input_tokens": tot_in,
                    "output_tokens": tot_out,
                    "cache_tokens": tot_cache,
                    "total_tokens": tot_in + tot_out + tot_cache,
                    "premium_requests": prem,
                }

    if usage is None and usage_record_events:
        tot_in = 0
        tot_out = 0
        tot_cache = 0
        have_record = False
        for ev in usage_record_events:
            d = ev.get("data")
            if not isinstance(d, dict):
                continue
            u = d.get("usage")
            if not isinstance(u, dict):
                continue
            in_tok = _non_neg_int(u.get("inputTokens")) or 0
            out_tok = _non_neg_int(u.get("outputTokens")) or 0
            cr_tok = _non_neg_int(u.get("cacheReadTokens")) or 0
            cw_tok = _non_neg_int(u.get("cacheWriteTokens")) or 0
            tot_in += max(0, in_tok - cr_tok)
            tot_out += out_tok
            tot_cache += (cr_tok + cw_tok)
            have_record = True
        if have_record:
            prem = 0
            if usage_checkpoint_events:
                last_chk_data = usage_checkpoint_events[-1].get("data")
                if isinstance(last_chk_data, dict):
                    prem_raw = last_chk_data.get("totalPremiumRequests", 0)
                    prem = prem_raw if (isinstance(prem_raw, int) and not isinstance(prem_raw, bool) and prem_raw >= 0) else 0
            usage = {
                "input_tokens": tot_in,
                "output_tokens": tot_out,
                "cache_tokens": tot_cache,
                "total_tokens": tot_in + tot_out + tot_cache,
                "premium_requests": prem,
            }

    return {"actual_model": actual_model, "auto_routing": auto_routing, "usage": usage}


def execute(args: argparse.Namespace, sandbox_check: Callable[[], bool] | None = None) -> tuple[Path | None, dict[str, Any]]:
    """Run the wrapper; returns (result.json path or None, result). Never raises for refusals."""
    started = now_utc()
    worktree = Path(args.worktree).expanduser()
    evidence = Path(args.evidence_dir).expanduser() if args.evidence_dir else default_evidence_dir()
    result: dict[str, Any] = {
        "schema": SCHEMA, "status": "refused", "reason": None, "model": None, "alias": None,
        "actual_model": None, "auto_routing": None, "usage": None,
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
    alias, model = resolve_model(args.model)
    result.update(alias=alias, model=model)
    task, digest = read_task(Path(args.task_file))
    worktree = Path(args.worktree).expanduser()
    result["start_head"], git_dir, common = check_worktree(worktree)
    if not 0 < args.timeout <= MAX_TIMEOUT:
        raise Refused("timeout_out_of_range", f"{args.timeout} not in 1..{MAX_TIMEOUT}")
    copilot = copilot_binary()
    gh = gh_binary()
    token = prepare_copilot_auth()
    gh_config = evidence / "gh-config-empty"
    gh_config.mkdir(exist_ok=True)
    copilot_home = prepare_copilot_home(evidence)
    env = child_env(gh_config=gh_config, copilot_token=token, copilot_home=copilot_home)
    result["quota"] = quota_preflight(gh, token)
    argv = run_argv(copilot, task, model)
    result["argv"] = [f"<task-file:{args.task_file} {digest}>" if item == task else item for item in argv]
    before = git_fingerprint(git_dir, common)
    status, reason = "succeeded", None
    started = now_utc()
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
    if reason != "git_metadata_modified":  # never run git over planted config or hooks
        (evidence / "diff.patch").write_text(collect_diff(worktree, result["start_head"]), encoding="utf-8")
    try:
        (copilot_home / "config.json").unlink(missing_ok=True)
    except OSError:
        pass
    try:
        session_info = extract_copilot_session(copilot_home, started_at=started)
        result["actual_model"] = session_info["actual_model"]
        result["auto_routing"] = session_info["auto_routing"]
        result["usage"] = session_info["usage"]
    except Exception:  # noqa: BLE001 - session extraction must never fail a worker run
        pass
    result.update(status=status, reason=reason)


def cmd_run(args: argparse.Namespace) -> int:
    path, result = execute(args)
    print(f"result: {path}")
    print(f"status: {result['status']}" + (f" ({result['reason']})" if result["reason"] else ""))
    return {"succeeded": 0, "refused": 2}.get(result["status"], 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one headless Copilot CLI worker turn")
    run.add_argument("--task-file", required=True)
    run.add_argument("--worktree", required=True)
    run.add_argument("--model", default="copilot-auto")
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    run.add_argument("--evidence-dir")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
