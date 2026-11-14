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
    {"id": "gemini-argon-wtu-weekly", "remaining_fraction": 0.4}, {"id": "3p-5h", "remaining_fraction": 0.6},
    {"id": "3p-weekly", "remaining_fraction": 0.3}]}]}}}

FAKE = """#!{python}
import json, os, sys
from pathlib import Path
cfg = json.loads(Path({cfg!r}).read_text())
args = sys.argv[1:]
if args[:2] == ["-p", "/quota"]:
    calls = cfg.get("quota_calls", 0) + 1
    cfg["quota_calls"] = calls
    Path({cfg!r}).write_text(json.dumps(cfg))
    out = cfg.get("quota_after_out") if calls > 1 and "quota_after_out" in cfg else cfg["quota_out"]
    exit_code = cfg.get("quota_after_exit", 0) if calls > 1 and "quota_after_exit" in cfg else 0
    print(out); sys.exit(exit_code)
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
if cfg.get("write", True):
    Path("tracked.txt").write_text("changed\\n")
    Path("new.txt").write_text("brand new\\n")
print(cfg["run_out"]); sys.stderr.write(cfg.get("stderr", "worker stderr\\n")); sys.exit(cfg["exit"])
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
        self.set(quota_out=json.dumps(GOOD_QUOTA), run_out=json.dumps({"status": "SUCCESS", "response": "done"}), exit=0)
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

    def clean(self) -> None:
        git(self.wt, "reset", "--hard", "HEAD")
        git(self.wt, "clean", "-fd")

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
                           "--mode", "accept-edits", "--dangerously-skip-permissions", "--sandbox", "--print-timeout", "30s"]
    assert Path(log["cwd"]).resolve() == env.wt.resolve()
    assert "--remote-control" not in log["argv"]
    assert result["argv"][2].startswith("<task-file:") and "do the thing" not in json.dumps(result["argv"])
    assert {b["id"] for b in result["quota"]} == {"gemini-5h", "gemini-weekly"}
    assert result["start_head"] == git(env.wt, "rev-parse", "HEAD").strip()
    assert json.loads((env.evidence / "transcript.json").read_text()) == {"status": "SUCCESS", "response": "done"}
    assert "worker stderr" in (env.evidence / "stderr.txt").read_text()
    diff = (env.evidence / "diff.patch").read_text()
    assert "+changed" in diff and "-original" in diff and "+brand new" in diff and "new.txt" in diff
    assert git(env.wt, "log", "--oneline").count("\n") == 1  # no commit made


def test_non_json_transcript_and_failure_evidence(env: Env) -> None:
    env.set(run_out="plain text", exit=1)
    result = env.run()
    assert result["status"] == "failed" and result["exit_code"] == 1 and result["reason"] == "exit_code_1"
    assert result["failure_class"] == "model_or_unknown"
    assert (env.evidence / "transcript.txt").read_text().strip() == "plain text"
    assert (env.evidence / "stderr.txt").exists() and "+brand new" in (env.evidence / "diff.patch").read_text()


def test_sandboxed_full_permissions_and_stripped_credentials(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_FOO", "GIT_FOO", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(key, "secret")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/home/x/.gitconfig")
    monkeypatch.setenv("GH_CONFIG_DIR", "/home/x/.config/gh")
    result = env.run()
    assert result["status"] == "succeeded"
    seen = json.loads(env.log.read_text())
    argv = seen["argv"]
    assert "--dangerously-skip-permissions" in argv
    assert "--sandbox" in argv
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "accept-edits"
    env_vars = seen["env"]
    gh_config = Path(env_vars["GH_CONFIG_DIR"])
    assert gh_config.parent == env.evidence and gh_config.is_dir() and not any(gh_config.iterdir())
    assert env_vars["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env_vars["GH_TOKEN"] is env_vars["GITHUB_TOKEN"] is env_vars["GH_FOO"] is env_vars["GIT_FOO"] is env_vars["SSH_AUTH_SOCK"] is None
    assert env_vars["GIT_CONFIG_GLOBAL"] == "/dev/null" and env_vars["GIT_TERMINAL_PROMPT"] == "0"


def test_not_sandbox_refusal(env: Env) -> None:
    refused(env.run(sandbox=False), "not_sandbox")
    assert not env.invoked()


@pytest.mark.parametrize("write", [False, True])
def test_permission_denials_classified_as_harness(env: Env, write: bool) -> None:
    env.set(run_out=json.dumps({"status": "SUCCESS", "response": "", "denied_actions": [{"action": "command"}]}),
            write=write)
    result = env.run()
    assert result["status"] == "failed" and result["failure_class"] == "harness"
    assert result["reason"] == "permission_denied: command"


@pytest.mark.parametrize("status_val", ["ERROR", "FAILED"])
def test_stream_error_status_classified_as_harness(env: Env, status_val: str) -> None:
    env.set(run_out=json.dumps({"status": status_val, "response": "done"}))
    result = env.run()
    assert result["status"] == "failed" and result["failure_class"] == "harness"
    assert result["reason"] == f"stream_status_{status_val}"


@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("response_val", ["", "  \n", "\t"])
def test_empty_model_response_classified_as_harness(env: Env, write: bool, response_val: str) -> None:
    env.set(run_out=json.dumps({"status": "SUCCESS", "response": response_val}), write=write)
    result = env.run()
    assert result["status"] == "failed" and result["failure_class"] == "harness"
    assert result["reason"] == "empty_result"
    if write:
        assert result["diff_bytes"] > 0
    else:
        assert result["diff_bytes"] == 0


def test_review_task_without_diff_succeeds(env: Env) -> None:
    env.set(run_out=json.dumps({"status": "SUCCESS", "response": "Code review passed: no changes required."}), write=False)
    result = env.run()
    assert result["status"] == "succeeded" and result["reason"] is None and result["failure_class"] is None
    assert result["diff_bytes"] == 0
    assert (env.evidence / "diff.patch").read_text() == ""
    assert json.loads((env.evidence / "transcript.json").read_text())["response"] == "Code review passed: no changes required."


def test_unparsed_plain_text_output_not_empty_result(env: Env) -> None:
    # Zero-exit non-JSON plain text succeeds without diff
    env.set(run_out="Plain text code review: LGTM.\n", exit=0, write=False)
    result = env.run()
    assert result["status"] == "succeeded" and result["exit_code"] == 0
    assert result["reason"] is None and result["failure_class"] is None
    assert (env.evidence / "transcript.txt").read_text().strip() == "Plain text code review: LGTM."
    assert result["diff_bytes"] == 0

    # Zero-exit non-JSON plain text succeeds with diff
    env.clean()
    env.set(run_out="Applied plain text edits successfully.\n", exit=0, write=True)
    result = env.run()
    assert result["status"] == "succeeded" and result["exit_code"] == 0
    assert result["reason"] is None and result["failure_class"] is None
    assert (env.evidence / "transcript.txt").read_text().strip() == "Applied plain text edits successfully."
    assert result["diff_bytes"] > 0

    # Nonzero exit fails without being overridden by empty_result or stream_status_missing
    env.clean()
    env.set(run_out="Plain text output from a failing command.\n", exit=1, write=False)
    result = env.run()
    assert result["status"] == "failed" and result["exit_code"] == 1
    assert result["reason"] == "exit_code_1"
    assert result["failure_class"] == "model_or_unknown"

    # Zero-exit empty plain text fails as empty_result regardless of diff
    env.clean()
    env.set(run_out="   \n", exit=0, write=False)
    result = env.run()
    assert result["status"] == "failed" and result["exit_code"] == 0
    assert result["reason"] == "empty_result" and result["failure_class"] == "harness"

    env.clean()
    env.set(run_out="   \n", exit=0, write=True)
    result = env.run()
    assert result["status"] == "failed" and result["exit_code"] == 0
    assert result["reason"] == "empty_result" and result["failure_class"] == "harness"
    assert result["diff_bytes"] > 0

    # Zero-exit plain text with runner error in stderr fails
    env.clean()
    env.set(run_out="Some plain text\n", exit=0, write=False,
            stderr='log\nAGY_ERROR: {"status": "RESOURCE_EXHAUSTED", "error_code": 429}\n')
    result = env.run()
    assert result["status"] == "failed"
    assert result["reason"] == "runner_provider_quota"
    assert result["failure_class"] == "provider_quota"


def test_json_success_with_runner_error_fails(env: Env) -> None:
    env.set(run_out=json.dumps({"status": "SUCCESS", "response": "Review completed: all checks passed."}),
            exit=0,
            write=False,
            stderr='worker stderr\nAGY_ERROR: {"status": "RESOURCE_EXHAUSTED", "error_code": 429}\n')
    result = env.run()
    assert result["status"] == "failed" and result["exit_code"] == 0
    assert result["reason"] == "runner_provider_quota"
    assert result["failure_class"] == "provider_quota"


def test_default_evidence_dirs_are_unique_in_same_second(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDatetime(worker.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> worker.datetime:
            return cls(2026, 11, 13, tzinfo=tz)

    monkeypatch.setattr(worker, "datetime", FixedDatetime)
    monkeypatch.setenv("HOME", str(tmp_path))
    first = worker.default_evidence_dir()
    second = worker.default_evidence_dir()
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    assert "20261113T000000Z" in first.name and "20261113T000000Z" in second.name
    assert first != second and first.is_dir() and second.is_dir()


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


@pytest.mark.parametrize("model", ["3p-claude-opus", "claude-sonnet", "claude-sonnet-5-5-low", "gpt-5", "gemini-9-unknown", "3pthing",
                                   "gemini-4-argon-max"])
def test_disallowed_model(env: Env, model: str) -> None:
    refused(env.run(model=model), "model_not_allowed")
    assert not env.invoked()


def test_argon_is_a_normal_worker(env: Env) -> None:
    result = env.run(model="argon")
    assert result["status"] == "succeeded"
    assert json.loads(env.log.read_text())["argv"][3] == worker.ARGON_MODEL
    assert {b["id"] for b in result["quota"]} == {"gemini-5h", "gemini-weekly", "gemini-argon-wtu-weekly"}
    assert worker.resolve_model("argon", True) == ("argon", worker.ARGON_MODEL)  # old launch lines still work


@pytest.mark.parametrize(("alias", "model_id", "pool"), [
    ("flash-medium", "gemini-3.8-flash-medium", {"gemini-5h", "gemini-weekly"}),
    ("flash-high", "gemini-3.8-flash-high", {"gemini-5h", "gemini-weekly"}),
    ("argon-high", "gemini-4-argon-high", {"gemini-5h", "gemini-weekly", "gemini-argon-wtu-weekly"}),
    ("agy-sonnet", "claude-sonnet-5-5-high", {"3p-5h", "3p-weekly"}),
    ("agy-opus", "claude-opus-5-5-high", {"3p-5h", "3p-weekly"}),
])
def test_effort_and_3p_aliases(env: Env, alias: str, model_id: str, pool: set[str]) -> None:
    result = env.run(model=alias)
    assert result["status"] == "succeeded" and result["alias"] == alias
    assert json.loads(env.log.read_text())["argv"][3] == model_id
    assert {b["id"] for b in result["quota"]} == pool


def test_hydrogen_buckets_and_fixed_models_resolve_to_family_buckets() -> None:
    assert worker.target_buckets("hydrogen-high") == ("gemini-5h", "gemini-weekly")
    for alias in worker.FIXED_MODELS:
        family = alias.split("-")[0]
        assert family in worker.FAMILY_BUCKETS, f"alias {alias!r} family {family!r} not in FAMILY_BUCKETS"
        assert worker.target_buckets(alias) == worker.FAMILY_BUCKETS[family]


def test_argon_own_bucket_exhausted(env: Env) -> None:
    quota = json.loads(json.dumps(GOOD_QUOTA))
    quota["command"]["data"]["groups"][0]["buckets"][2]["remaining_fraction"] = 0
    env.set(quota_out=json.dumps(quota))
    refused(env.run(model="argon-high"), "quota_exhausted")
    assert env.run(model="flash-high")["status"] == "succeeded"


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
    env.set(quota_out=json.dumps({"buckets": [{"id": "gemini-5h", "remaining_fraction": 0.5}]}))
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
    ("failed", "empty_result", "", "harness"),
    ("failed", "permission_denied: command", "", "harness"),
    ("failed", "permission_denied: edit_file, bash", "", "harness"),
    ("failed", "stream_status_ERROR", "", "harness"),
    ("failed", "stream_status_FAILED", "", "harness"),
    ("failed", "stream_status_missing", "", "harness"),
    ("failed", "timeout", "", "harness"),
    ("failed", "spawn_failed: no such file", "", "harness"),
    ("failed", "git_metadata_modified", "", "policy"),
    ("failed", "exit_code_1", "plain failure", "model_or_unknown"),
    ("failed", "exit_code_3", 'AGY_ERROR: {"status": "RESOURCE_EXHAUSTED", "error_code": 429}', "provider_quota"),
    ("failed", "exit_code_3", 'AGY_ERROR: {"status": "DEADLINE_EXCEEDED", "reason": "print_timeout", '
                              '"short_error": "print timeout after 30m0s"}', "harness"),
    ("failed", "exit_code_3", 'AGY_ERROR: {"status": "UNAUTHENTICATED", "error_code": 401}', "harness"),
    ("failed", "runner_provider_quota", "", "provider_quota"),
    ("failed", "runner_turn_time_limit", "", "harness"),
])
def test_failure_class_mapping_all_guards(status: str, reason: str | None, stderr: str, expected: str | None) -> None:
    assert worker.failure_class(status, reason, stderr) == expected


# --- Spec 272 (WORKER-COST-1): spend, usage, quota_after, overlap ---------------------


def test_usage_and_quota_after_and_overlap_false_written_to_result(env: Env) -> None:
    usage_data = {
        "input_tokens": 150,
        "output_tokens": 40,
        "thinking_tokens": 10,
        "cache_read_tokens": 0,
        "total_tokens": 200,
        "extra_field": "dropped",
    }
    quota_after_reading = {
        "command": {"data": {"groups": [{"buckets": [
            {"id": "gemini-5h", "remaining_fraction": 0.78},
            {"id": "gemini-weekly", "remaining_fraction": 0.47},
        ]}]}}
    }
    env.set(
        run_out=json.dumps({"status": "SUCCESS", "response": "done", "usage": usage_data}),
        quota_after_out=json.dumps(quota_after_reading),
    )
    result = env.run()
    assert result["status"] == "succeeded"
    assert result["usage"] == {
        "input_tokens": 150,
        "output_tokens": 40,
        "thinking_tokens": 10,
        "cache_read_tokens": 0,
        "total_tokens": 200,
    }
    assert result["quota_after"] == [
        {"id": "gemini-5h", "remaining_fraction": 0.78},
        {"id": "gemini-weekly", "remaining_fraction": 0.47},
    ]
    assert result["overlap"] is False
    assert "quota_after_error" not in result


def test_quota_after_failure_still_succeeds_with_null_and_error(env: Env) -> None:
    env.set(
        run_out=json.dumps({"status": "SUCCESS", "response": "done"}),
        quota_after_out="Something went wrong",
        quota_after_exit=1,
    )
    result = env.run()
    assert result["status"] == "succeeded"
    assert result["quota_after"] is None
    assert "quota_after_error" in result
    assert result["quota_after_error"] is not None
    assert result["overlap"] is False


def test_active_intervals_intersect_both_report_overlap_true(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    log_file = env.tmp / ".worker_runs.jsonl"
    monkeypatch.setenv("JARVISOS_AGY_ACTIVITY_LOG", str(log_file))

    now = time.time()
    peer_id = "peer123"
    worker.log_run_start(log_file, peer_id, worker.now_utc(), now - 5)

    res = env.run()
    assert res["overlap"] is True

    worker.log_run_end(log_file, peer_id, worker.now_utc(), now + 10)
    peer1_overlap = worker.check_overlap(log_file, peer_id, now - 5, now + 10)
    assert peer1_overlap is True


def test_crashed_peer_older_than_max_timeout_plus_kill_margin_not_overlapping(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_file = env.tmp / ".worker_runs.jsonl"
    monkeypatch.setenv("JARVISOS_AGY_ACTIVITY_LOG", str(log_file))

    crashed_time = time.time() - (worker.MAX_TIMEOUT + worker.KILL_MARGIN + 3600)
    worker.log_run_start(log_file, "crashed_run", worker.now_utc(), crashed_time)

    res = env.run()
    assert res["overlap"] is False


def test_different_evidence_parents_share_default_activity_log(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISOS_AGY_ACTIVITY_LOG", raising=False)
    default_log = worker.activity_log_path()
    now = time.time()
    worker.log_run_start(default_log, "peer_other_dir", worker.now_utc(), now - 5)

    custom_ev = env.tmp / "custom_sub" / "nested" / "ev1"
    args = argparse.Namespace(
        task_file=str(env.task), worktree=str(env.wt), model="flash",
        timeout=30, evidence_dir=str(custom_ev), coordinator_fallback=False,
    )
    _, res = worker.execute(args, sandbox_check=lambda: True)
    assert res["overlap"] is True


def test_activity_log_write_failure_reports_overlap_null(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point activity log at an existing directory so open() fails with EISDIR (OSError)
    bad_log = env.tmp / "is_a_dir"
    bad_log.mkdir()
    monkeypatch.setenv("JARVISOS_AGY_ACTIVITY_LOG", str(bad_log))
    res = env.run()
    assert res["status"] == "succeeded"
    assert res["overlap"] is None


def test_sequential_subsecond_runs_do_not_report_overlap(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    log_file = env.tmp / ".worker_runs.jsonl"
    monkeypatch.setenv("JARVISOS_AGY_ACTIVITY_LOG", str(log_file))
    ev1 = env.tmp / "ev_seq_1"
    ev2 = env.tmp / "ev_seq_2"
    args1 = argparse.Namespace(
        task_file=str(env.task), worktree=str(env.wt), model="flash",
        timeout=30, evidence_dir=str(ev1), coordinator_fallback=False,
    )
    args2 = argparse.Namespace(
        task_file=str(env.task), worktree=str(env.wt), model="flash",
        timeout=30, evidence_dir=str(ev2), coordinator_fallback=False,
    )
    _, res1 = worker.execute(args1, sandbox_check=lambda: True)
    env.clean()
    _, res2 = worker.execute(args2, sandbox_check=lambda: True)
    assert res1["overlap"] is False
    assert res2["overlap"] is False
