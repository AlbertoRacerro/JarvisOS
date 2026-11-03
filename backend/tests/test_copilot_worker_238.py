"""Spec 238: Copilot worker qualification with fake local executables only."""
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

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "copilot_worker.py"
SPEC = importlib.util.spec_from_file_location("copilot_worker_238", SCRIPT)
assert SPEC and SPEC.loader
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)

GOOD = {"cli_enabled": True, "quota_snapshots": {"premium_interactions": {
    "has_quota": True, "entitlement": 100, "remaining": 75, "percent_remaining": 75,
    "overage_permitted": False}}}


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True, env=env).stdout


class Env:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch):
        self.root = root
        monkeypatch.setenv("HOME", str(root))
        monkeypatch.setenv("JARVISOS_COPILOT_GH_TOKEN", "dedicated-test-token")
        self.main = root / "main"
        self.main.mkdir()
        git(self.main, "init", "-q", "-b", "main")
        (self.main / "tracked.txt").write_text("before\n")
        git(self.main, "add", ".")
        git(self.main, "commit", "-q", "-m", "initial")
        self.wt = root / "wt"
        git(self.main, "worktree", "add", "-q", "-b", "worker", str(self.wt))
        self.task = root / "task.txt"
        self.task.write_text("edit a file\n")
        self.evidence = root / "evidence"
        self.config = root / "config.json"
        self.log = root / "log.json"
        self.set(quota=GOOD)
        gh = root / "gh"
        gh.write_text(f"#!{sys.executable}\nimport json, os\nfrom pathlib import Path\n"
                      f"cfg=json.loads(Path({str(self.config)!r}).read_text())\n"
                      f"assert os.environ.get('GH_TOKEN') == 'dedicated-test-token'\n"
                      f"print(json.dumps(cfg['quota']) if not isinstance(cfg['quota'], str) else cfg['quota'])\n")
        gh.chmod(0o755)
        monkeypatch.setenv("JARVISOS_GH_BIN", str(gh))
        copilot = root / "copilot"
        copilot.write_text(f"#!{sys.executable}\nimport json, os, sys, subprocess, time\nfrom pathlib import Path\n"
                           f"cfg=json.loads(Path({str(self.config)!r}).read_text())\n"
                           f"Path({str(self.log)!r}).write_text(json.dumps({{'argv': sys.argv[1:], 'env': {{k:os.getenv(k) for k in ('GH_TOKEN','GITHUB_TOKEN','GH_FOO','GIT_FOO','SSH_AUTH_SOCK','GH_CONFIG_DIR','GIT_CONFIG_GLOBAL','GIT_CONFIG_NOSYSTEM','GIT_TERMINAL_PROMPT','COPILOT_GITHUB_TOKEN')}}}}))\n"
                           "if cfg.get('sleep'):\n subprocess.Popen(['sleep','300']); time.sleep(300)\n"
                           "if cfg.get('plant'):\n"
                           " common=subprocess.check_output(['git','rev-parse','--path-format=absolute','--git-common-dir'],text=True).strip()\n"
                           " with open(Path(common)/'config','a') as f: f.write('\\n[core]\\n')\n"
                           "Path('tracked.txt').write_text('after\\n')\n"
                           "print('worker output'); print('worker stderr',file=sys.stderr)\n")
        copilot.chmod(0o755)
        monkeypatch.setenv("JARVISOS_COPILOT_BIN", str(copilot))

    def set(self, **changes: Any) -> None:
        data = json.loads(self.config.read_text()) if self.config.exists() else {}
        data.update(changes)
        self.config.write_text(json.dumps(data))

    def run(self, *, model: str = "copilot-auto", sandbox: bool = True, worktree: Path | None = None,
            timeout: int = 10) -> dict[str, Any]:
        args = argparse.Namespace(model=model, task_file=str(self.task), worktree=str(worktree or self.wt),
                                  timeout=timeout, evidence_dir=str(self.evidence))
        path, result = worker.execute(args, sandbox_check=lambda: sandbox)
        assert path == self.evidence / "result.json"
        assert json.loads(path.read_text()) == result
        return result


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def reason(result: dict[str, Any]) -> str:
    assert result["status"] == "refused"
    return result["reason"].split(":")[0]


def test_success_evidence_argv_and_credential_boundary(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_FOO", "GIT_FOO", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(key, "coordinator-secret")
    result = env.run()
    assert result["status"] == "succeeded" and result["quota"]["remaining_fraction"] == 0.75
    log = json.loads(env.log.read_text())
    argv, child = log["argv"], log["env"]
    assert argv[:4] == ["-p", "edit a file\n", "--model", worker.AUTO_MODEL]
    assert argv[4:] == ["--allow-all-tools", "--no-ask-user", "--deny-tool", "shell(gh:*)",
                        "--deny-tool", "shell(git push*)", "--deny-tool", "shell(git remote*)",
                        "--deny-tool", "shell(git config*)"]
    assert all(child[key] is None for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_FOO", "GIT_FOO", "SSH_AUTH_SOCK"))
    assert child["COPILOT_GITHUB_TOKEN"] == "dedicated-test-token"
    assert child["GIT_CONFIG_GLOBAL"] == "/dev/null" and child["GIT_CONFIG_NOSYSTEM"] == "1"
    assert child["GIT_TERMINAL_PROMPT"] == "0" and not list(Path(child["GH_CONFIG_DIR"]).iterdir())
    assert "edit a file" not in json.dumps(result["argv"])
    assert "worker output" in (env.evidence / "transcript.txt").read_text()
    assert "worker stderr" in (env.evidence / "stderr.txt").read_text()
    assert "+after" in (env.evidence / "diff.patch").read_text()


@pytest.mark.parametrize("model", ["claude-opus", "gpt-6", "unknown", "copilot-haiku", "copilot-sonnet"])
def test_model_refusal(env: Env, model: str) -> None:
    assert reason(env.run(model=model)) == "model_not_allowed"


def test_worktree_and_host_refusals(env: Env) -> None:
    assert reason(env.run(sandbox=False)) == "not_sandbox"
    assert reason(env.run(worktree=env.main)) == "not_linked_worktree"
    (env.wt / "untracked").write_text("dirty")
    assert reason(env.run()) == "worktree_dirty"


def test_binary_and_auth_refusals(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_COPILOT_BIN", str(env.root / "missing"))
    assert reason(env.run()) == "copilot_missing"
    monkeypatch.setenv("JARVISOS_COPILOT_BIN", str(env.root / "copilot"))
    monkeypatch.delenv("JARVISOS_COPILOT_GH_TOKEN")
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    assert reason(env.run()) == "auth_missing"


@pytest.mark.parametrize(("snapshot", "expected"), [
    ({"has_quota": False, "entitlement": 0}, "no_entitlement"),
    ({"has_quota": False, "entitlement": 100}, "quota_exhausted"),
    ({"has_quota": True, "percent_remaining": 0}, "quota_exhausted"),
    ({"has_quota": True, "percent_remaining": 0, "overage_permitted": True}, "quota_exhausted"),
    ({"has_quota": True, "percent_remaining": 50, "remaining": 0, "overage_permitted": True}, "quota_exhausted"),
    ({"has_quota": True, "percent_remaining": "bad"}, "quota_unreadable"),
])
def test_quota_refusals(env: Env, snapshot: dict[str, Any], expected: str) -> None:
    env.set(quota={"cli_enabled": True, "quota_snapshots": {"premium_interactions": snapshot}})
    assert reason(env.run()) == expected
    assert not env.log.exists()


def test_disabled_unreadable_and_auth_quota(env: Env) -> None:
    env.set(quota={"cli_enabled": False, "quota_snapshots": {"premium_interactions": GOOD["quota_snapshots"]["premium_interactions"]}})
    assert reason(env.run()) == "cli_disabled"
    env.set(quota="not json")
    assert reason(env.run()) == "quota_unreadable"
    env.set(quota={"cli_enabled": True})
    assert reason(env.run()) == "quota_bucket_missing"
    env.set(quota="Bad credentials 401")
    assert reason(env.run()) == "not_signed_in"


def test_timeout_kills_process_group(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "KILL_MARGIN", 0)
    env.set(sleep=True)
    started = time.monotonic()
    result = env.run(timeout=1)
    assert result["status"] == "timeout" and time.monotonic() - started < 10


def test_git_metadata_modified(env: Env) -> None:
    env.set(plant=True)
    result = env.run()
    assert result["status"] == "failed" and result["reason"] == "git_metadata_modified"
    assert not (env.evidence / "diff.patch").exists()


def test_auth_file_link_and_conflict(tmp_path: Path) -> None:
    state = tmp_path / ".local/share/jarvisos-sandbox/state/copilot"
    state.mkdir(parents=True)
    (state / "token").write_text("file-token\n")
    target = state / "copilot-home"
    target.mkdir()
    assert worker.prepare_copilot_auth(tmp_path, {}) == "file-token"
    assert (tmp_path / ".copilot").is_symlink()
    assert worker.prepare_copilot_auth(tmp_path, {}) == "file-token"
    (tmp_path / ".copilot").unlink()
    (tmp_path / ".copilot").mkdir()
    with pytest.raises(worker.Refused) as caught:
        worker.prepare_copilot_auth(tmp_path, {})
    assert caught.value.reason == "login_path_conflict"


def test_all_allowlisted_model_ids_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    assert worker.resolve_model("copilot-auto") == ("copilot-auto", worker.AUTO_MODEL)
    assert worker.resolve_model("auto") == ("copilot-auto", worker.AUTO_MODEL)
    monkeypatch.setenv("JARVISOS_COPILOT_AUTO_MODEL", "custom-auto")
    assert worker.resolve_model("copilot-auto") == ("copilot-auto", "custom-auto")
    for model in ("gpt-6", "copilot-sonnet"):
        with pytest.raises(worker.Refused) as caught:
            worker.resolve_model(model)
        assert caught.value.reason == "model_not_allowed"
