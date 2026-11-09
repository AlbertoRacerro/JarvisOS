"""Spec 221: agy worker wrapper, exercised against a fake agy executable."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "agy_worker.py"
SPEC = importlib.util.spec_from_file_location("agy_worker_221", SCRIPT)
assert SPEC and SPEC.loader
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)

GOOD_QUOTA = {"command": {"data": {"groups": [{"buckets": [
    {"id": "gemini-5h", "remaining_fraction": 0.8}, {"id": "gemini-weekly", "remaining_fraction": 0.5},
    {"id": "argon-5h", "remaining_fraction": 0.4}, {"id": "3p-5h", "remaining_fraction": 0.0}]}]}}}

FAKE = """#!{python}
import json, os, sys
from pathlib import Path
cfg = json.loads(Path({cfg!r}).read_text())
args = sys.argv[1:]
if args[:2] == ["-p", "/quota"]:
    print(cfg["quota_out"]); sys.exit(0)
Path(cfg["log"]).write_text(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(),
    "env": {{k: os.environ.get(k) for k in ("GH_TOKEN", "GITHUB_TOKEN", "GH_FOO", "GIT_FOO",
    "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT", "SSH_AUTH_SOCK", "GH_CONFIG_DIR", "GIT_CONFIG_NOSYSTEM")}}}}))
if cfg.get("sleep_grandchild"):
    import subprocess
    subprocess.Popen(["sleep", "300"]); import time; time.sleep(300)
if cfg.get("plant"):
    common = os.path.join(os.popen("git rev-parse --path-format=absolute --git-common-dir").read().strip())
    if cfg["plant"] == "config":
        open(os.path.join(common, "config"), "a").write("[core]\\n\\thooksPath = /tmp/x\\n")
    else:
        os.makedirs(os.path.join(common, "hooks"), exist_ok=True)
        open(os.path.join(common, "hooks", "post-checkout"), "w").write("#!/bin/sh\\n")
Path("tracked.txt").write_text("changed\\n")
Path("new.txt").write_text("brand new\\n")
print(cfg["run_out"]); sys.stderr.write("worker stderr\\n"); sys.exit(cfg["exit"])
"""


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env=env).stdout


class Env:
    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp
        monkeypatch.setenv("HOME", str(tmp))
        (tmp / ".local/share/jarvisos-sandbox/state/agy-home").mkdir(parents=True)
        self.main = tmp / "main"
        self.main.mkdir()
        git(self.main, "init", "-q", "-b", "main")
        (self.main / "tracked.txt").write_text("original\n")
        git(self.main, "add", ".")
        git(self.main, "commit", "-q", "-m", "init")
        self.wt = tmp / "wt"
        git(self.main, "worktree", "add", "-q", "-b", "work", str(self.wt))
        self.task = tmp / "task.md"
        self.task.write_text("do the thing\n")
        self.cfg = tmp / "cfg.json"
        self.log = tmp / "log.json"
        self.evidence = tmp / "evidence"
        self.set(quota_out=json.dumps(GOOD_QUOTA), run_out=json.dumps({"ok": True}), exit=0)
        fake = tmp / "agy"
        fake.write_text(FAKE.format(python=sys.executable, cfg=str(self.cfg)))
        fake.chmod(0o755)
        monkeypatch.setenv("JARVISOS_AGY_BIN", str(fake))
        monkeypatch.delenv("JARVISOS_AGY_FLASH_MODEL", raising=False)
        monkeypatch.delenv("JARVISOS_AGY_ARGON_MODEL", raising=False)

    def set(self, **values: Any) -> None:
        cfg = json.loads(self.cfg.read_text()) if self.cfg.exists() else {"log": str(self.log)}
        cfg.update(values)
        self.cfg.write_text(json.dumps(cfg))

    def run(self, *, model: str = "flash", fallback: bool = False, sandbox: bool = True, worktree: Path | None = None,
            task: Path | None = None, timeout: int = 30) -> dict[str, Any]:
        args = argparse.Namespace(task_file=str(task or self.task), worktree=str(worktree or self.wt), model=model,
                                  timeout=timeout, evidence_dir=str(self.evidence), coordinator_fallback=fallback)
        path, result = worker.execute(args, sandbox_check=lambda: sandbox)
        assert path == self.evidence / "result.json"
        assert json.loads(path.read_text()) == result
        return result  # type: ignore[no-any-return]

    def invoked(self) -> bool:
        return self.log.exists()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def refused(result: dict[str, Any], reason: str) -> None:
    assert result["status"] == "refused"
    assert result["reason"].split(":")[0] == reason


def test_success_argv_evidence_and_diff(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    result = env.run()
    assert result["status"] == "succeeded" and result["exit_code"] == 0 and result["alias"] == "flash"
    log = json.loads(env.log.read_text())
    assert log["argv"] == ["-p", "do the thing\n", "--model", worker.FLASH_MODEL, "--output-format", "json",
                           "--mode", "accept-edits", "--sandbox", "--print-timeout", "30s"]
    assert Path(log["cwd"]).resolve() == env.wt.resolve()
    assert "--dangerously-skip-permissions" not in log["argv"] and "--remote-control" not in log["argv"]
    assert result["argv"][2].startswith("<task-file:") and "do the thing" not in json.dumps(result["argv"])
    assert {b["id"] for b in result["quota"]} == {"gemini-5h", "gemini-weekly"}
    assert result["start_head"] == git(env.wt, "rev-parse", "HEAD").strip()
    assert json.loads((env.evidence / "transcript.json").read_text()) == {"ok": True}
    assert "worker stderr" in (env.evidence / "stderr.txt").read_text()
    diff = (env.evidence / "diff.patch").read_text()
    assert "+changed" in diff and "-original" in diff and "+brand new" in diff and "new.txt" in diff
    assert git(env.wt, "log", "--oneline").count("\n") == 1  # no commit made


def test_non_json_transcript_and_failure_evidence(env: Env) -> None:
    env.set(run_out="plain text", exit=1)
    result = env.run()
    assert result["status"] == "failed" and result["exit_code"] == 1 and result["reason"] == "exit_code_1"
    assert (env.evidence / "transcript.txt").read_text().strip() == "plain text"
    assert (env.evidence / "stderr.txt").exists() and "+brand new" in (env.evidence / "diff.patch").read_text()


def test_credentials_are_stripped(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_FOO", "GIT_FOO", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(key, "secret")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/home/x/.gitconfig")
    monkeypatch.setenv("GH_CONFIG_DIR", "/home/x/.config/gh")
    assert env.run()["status"] == "succeeded"
    seen = json.loads(env.log.read_text())["env"]
    gh_config = Path(seen["GH_CONFIG_DIR"])
    assert gh_config.parent == env.evidence and gh_config.is_dir() and not any(gh_config.iterdir())
    assert seen["GIT_CONFIG_NOSYSTEM"] == "1"
    assert seen["GH_TOKEN"] is seen["GITHUB_TOKEN"] is seen["GH_FOO"] is seen["GIT_FOO"] is seen["SSH_AUTH_SOCK"] is None
    assert seen["GIT_CONFIG_GLOBAL"] == "/dev/null" and seen["GIT_TERMINAL_PROMPT"] == "0"


def test_not_sandbox(env: Env) -> None:
    refused(env.run(sandbox=False), "not_sandbox")
    assert not env.invoked()


def test_main_checkout_and_non_worktree(env: Env) -> None:
    refused(env.run(worktree=env.main), "not_linked_worktree")
    plain = env.tmp / "plain"
    plain.mkdir()
    refused(env.run(worktree=plain), "not_linked_worktree")
    refused(env.run(worktree=env.tmp / "absent"), "worktree_missing")
    assert not env.invoked()


def test_dirty_worktree(env: Env) -> None:
    (env.wt / "stray.txt").write_text("x")
    refused(env.run(), "worktree_dirty")


@pytest.mark.parametrize("model", ["3p-claude-opus", "claude-sonnet", "gpt-5", "gemini-9-unknown", "3pthing"])
def test_disallowed_model(env: Env, model: str) -> None:
    refused(env.run(model=model), "model_not_allowed")
    assert not env.invoked()


def test_argon_needs_fallback(env: Env) -> None:
    refused(env.run(model="argon"), "argon_requires_coordinator_fallback")
    assert not env.invoked()
    result = env.run(model="argon", fallback=True)
    assert result["status"] == "succeeded"
    assert json.loads(env.log.read_text())["argv"][3] == worker.ARGON_MODEL
    assert {b["id"] for b in result["quota"]} == {"argon-5h"}


def test_model_id_env_override(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_AGY_FLASH_MODEL", "gemini-custom")
    env.run()
    assert json.loads(env.log.read_text())["argv"][3] == "gemini-custom"


def test_agy_missing(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_AGY_BIN", str(env.tmp / "nope"))
    refused(env.run(), "agy_missing")


def test_quota_refusals(env: Env) -> None:
    env.set(quota_out="Please sign in to continue")
    refused(env.run(), "not_signed_in")
    env.set(quota_out='{"error": "Sign In required"}')
    refused(env.run(), "not_signed_in")
    env.set(quota_out="banner\n" + json.dumps({"buckets": [{"id": "gemini-5h", "remaining_fraction": 0}]}))
    refused(env.run(), "quota_exhausted")
    env.set(quota_out=json.dumps({"buckets": [{"id": "gemini-5h", "remaining_fraction": 0.5, "disabled": True}]}))
    refused(env.run(), "quota_exhausted")
    env.set(quota_out=json.dumps({"buckets": [{"id": "argon-5h", "remaining_fraction": 0.5}]}))
    refused(env.run(), "quota_bucket_missing")
    assert not env.invoked()


def test_quota_unreadable(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "QUOTA_TIMEOUT", 1)
    env.set(quota_out="")
    refused(env.run(), "quota_unreadable")
    env.set(quota_out="garbage without json")
    refused(env.run(), "quota_unreadable")
    slow = env.tmp / "slow"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    monkeypatch.setenv("JARVISOS_AGY_BIN", str(slow))
    refused(env.run(), "quota_unreadable")


def test_task_file_refusals(env: Env) -> None:
    env.task.write_text("  \n")
    refused(env.run(), "task_empty")
    env.task.write_text("x" * (worker.MAX_TASK_BYTES + 1))
    refused(env.run(), "task_too_large")
    refused(env.run(task=env.tmp / "missing.md"), "task_unreadable")


def test_cli_exit_codes(env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(worker, "in_sandbox", lambda: True)
    base = ["run", "--task-file", str(env.task), "--worktree", str(env.wt), "--evidence-dir", str(env.evidence)]
    assert worker.main(base) == 0
    assert "status: succeeded" in capsys.readouterr().out
    assert worker.main([*base, "--model", "argon"]) == 2
    assert not (env.evidence / "x").exists()


@pytest.mark.parametrize("timeout", [0, -5, worker.MAX_TIMEOUT + 1])
def test_timeout_out_of_range(env: Env, timeout: int) -> None:
    refused(env.run(timeout=timeout), "timeout_out_of_range")
    assert not env.invoked()


def test_task_text_naming_a_forbidden_flag_is_only_prompt_text(env: Env) -> None:
    env.task.write_text("never pass --remote-control\n")
    assert env.run()["status"] == "succeeded"
    assert "--remote-control" not in json.loads(env.log.read_text())["argv"][3:]


def test_timeout_kills_process_group(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "KILL_MARGIN", 0)
    env.set(sleep_grandchild=True)
    started = time.monotonic()
    result = env.run(timeout=1)
    assert result["status"] == "timeout" and result["reason"] == "exceeded 1s"
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("plant", ["config", "hook"])
def test_git_metadata_modification_fails_run(env: Env, plant: str) -> None:
    env.set(plant=plant)
    result = env.run()
    assert result["status"] == "failed" and result["reason"] == "git_metadata_modified"
    assert result["git_metadata"]["before"] != result["git_metadata"]["after"]
    assert not (env.evidence / "diff.patch").exists()


def test_git_metadata_unchanged_on_success(env: Env) -> None:
    assert "git_metadata" not in env.run()


def test_prepare_login_links_gemini_and_refuses_missing_or_conflicting_home(env: Env) -> None:
    target = env.tmp / ".local/share/jarvisos-sandbox/state/agy-home"
    # Removing agy-home refuses with login_state_missing before touching the worktree
    target.rmdir()
    refused(env.run(), "login_state_missing")
    assert not env.invoked()

    # Re-creating agy-home and replacing .gemini with a directory refuses with login_path_conflict
    target.mkdir(parents=True)
    (env.tmp / ".gemini").mkdir()
    refused(env.run(), "login_path_conflict")
    assert not env.invoked()

    # Removing the conflicting directory lets prepare_login link .gemini -> agy-home and succeed
    (env.tmp / ".gemini").rmdir()
    assert env.run()["status"] == "succeeded"
    assert (env.tmp / ".gemini").is_symlink() and (env.tmp / ".gemini").resolve() == target.resolve()


# --- maintainer directive L20261109090114-5e79: a worker's harness failure is never a capability verdict -------

@pytest.mark.parametrize(("status", "reason", "stderr", "expected"), [
    ("succeeded", None, "", None),
    ("timeout", "exceeded 930s", "", "harness"),
    ("failed", "spawn_failed: no such file", "", "harness"),
    ("failed", "exit_code_3", 'AGY_ERROR: {"status": "DEADLINE_EXCEEDED", "reason": "print_timeout", '
                              '"short_error": "print timeout after 30m0s"}', "harness"),
    ("failed", "exit_code_3", 'AGY_ERROR: {"status": "RESOURCE_EXHAUSTED", "error_code": 429}', "provider_quota"),
    ("failed", "exit_code_3", 'AGY_ERROR: {"status": "UNAUTHENTICATED", "error_code": 401}', "harness"),
    ("failed", "exit_code_1", "plain failure", "model_or_unknown"),
    ("failed", "git_metadata_modified", "", "policy"),
])
def test_failure_class_separates_harness_from_model(status, reason, stderr, expected):
    assert worker.failure_class(status, reason, stderr) == expected
