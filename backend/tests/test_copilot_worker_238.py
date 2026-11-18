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


def test_prepare_copilot_home_copies_config_only(tmp_path: Path) -> None:
    home = tmp_path / "user_home"
    copilot_dir = home / ".copilot"
    copilot_dir.mkdir(parents=True)
    (copilot_dir / "config.json").write_text(json.dumps({"auth": "ok"}), encoding="utf-8")
    (copilot_dir / "logs.txt").write_text("historical logs\n", encoding="utf-8")
    (copilot_dir / "session-state").mkdir()
    (copilot_dir / "session-state" / "old.jsonl").write_text("old\n", encoding="utf-8")

    evidence = tmp_path / "evidence"
    ch = worker.prepare_copilot_home(evidence, home=home)
    assert ch == evidence / "copilot-home"
    assert ch.is_dir()
    assert json.loads((ch / "config.json").read_text(encoding="utf-8")) == {"auth": "ok"}
    assert not (ch / "logs.txt").exists()
    assert not (ch / "session-state").exists()


def test_prepare_copilot_home_copies_from_sandbox_target_when_no_dot_copilot(tmp_path: Path) -> None:
    home = tmp_path / "user_home"
    state = home / ".local/share/jarvisos-sandbox/state/copilot/copilot-home"
    state.mkdir(parents=True)
    (state / "config.json").write_text(json.dumps({"sandbox_config": True}), encoding="utf-8")

    evidence = tmp_path / "evidence"
    ch = worker.prepare_copilot_home(evidence, home=home)
    assert json.loads((ch / "config.json").read_text(encoding="utf-8")) == {"sandbox_config": True}


def test_extract_copilot_session_auto_resolved_and_shutdown_token_details(tmp_path: Path) -> None:
    copilot_home = tmp_path / "copilot-home"
    sess_dir = copilot_home / "session-state" / "sess-123"
    sess_dir.mkdir(parents=True)
    events = [
        {"type": "session.start", "data": {}},
        {"type": "session.auto_mode_resolved", "data": {
            "chosenModel": "mai-code-2-flash",
            "candidateModels": ["mai-code-2-flash", "gpt-4o"],
            "selectionReason": "heuristic",
        }},
        {"type": "session.shutdown", "data": {
            "currentModel": "mai-code-2-flash",
            "totalPremiumRequests": 1,
            "tokenDetails": {
                "input": {"tokenCount": 100},
                "output": {"tokenCount": 50},
                "cache_read": {"tokenCount": 20},
                "cache_write": {"tokenCount": 10},
            },
        }},
    ]
    (sess_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )

    info = worker.extract_copilot_session(copilot_home)
    assert info["actual_model"] == "mai-code-2-flash"
    assert info["auto_routing"] == {
        "chosen_model": "mai-code-2-flash",
        "candidate_models": ["mai-code-2-flash", "gpt-4o"],
        "selection_reason": "heuristic",
    }
    assert info["usage"] == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_tokens": 30,
        "total_tokens": 180,
        "premium_requests": 1,
    }


def test_extract_copilot_session_shutdown_model_metrics(tmp_path: Path) -> None:
    copilot_home = tmp_path / "copilot-home"
    sess_dir = copilot_home / "session-state" / "sess-456"
    sess_dir.mkdir(parents=True)
    events = [
        {"type": "session.shutdown", "data": {
            "currentModel": "claude-sonnet-3.5",
            "totalPremiumRequests": 2,
            "modelMetrics": {
                "claude-sonnet-3.5": {
                    "tokenDetails": {
                        "input": {"tokenCount": 200},
                        "output": {"tokenCount": 80},
                        "cache_read": {"tokenCount": 40},
                        "cache_write": {"tokenCount": 10},
                    },
                },
                "gpt-4o": {
                    "usage": {
                        "inputTokens": 150,
                        "outputTokens": 60,
                        "cacheReadTokens": 50,
                        "cacheWriteTokens": 20,
                    },
                },
            },
        }},
    ]
    (sess_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )

    info = worker.extract_copilot_session(copilot_home)
    assert info["actual_model"] == "claude-sonnet-3.5"
    assert info["auto_routing"] is None
    # model 1: in=200, out=80, cache=50
    # model 2: in=150-50=100, out=60, cache=70
    assert info["usage"] == {
        "input_tokens": 300,
        "output_tokens": 140,
        "cache_tokens": 120,
        "total_tokens": 560,
        "premium_requests": 2,
    }


def test_extract_copilot_session_fallback_to_usage_records(tmp_path: Path) -> None:
    copilot_home = tmp_path / "copilot-home"
    sess_dir = copilot_home / "session-state" / "sess-789"
    sess_dir.mkdir(parents=True)
    events = [
        {"type": "session.usage_record", "data": {
            "usage": {
                "responseModel": "gpt-4o",
                "inputTokens": 120,
                "outputTokens": 30,
                "cacheReadTokens": 20,
                "cacheWriteTokens": 10,
            },
        }},
        {"type": "session.usage_checkpoint", "data": {
            "totalPremiumRequests": 3,
        }},
        {"type": "session.usage_record", "data": {
            "usage": {
                "model": "gpt-4o",
                "inputTokens": 150,
                "outputTokens": 40,
                "cacheReadTokens": 50,
                "cacheWriteTokens": 20,
            },
        }},
    ]
    (sess_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )

    info = worker.extract_copilot_session(copilot_home)
    assert info["actual_model"] == "gpt-4o"
    assert info["auto_routing"] is None
    # r1: in=100, out=30, cache=30
    # r2: in=100, out=40, cache=70
    assert info["usage"] == {
        "input_tokens": 200,
        "output_tokens": 70,
        "cache_tokens": 100,
        "total_tokens": 370,
        "premium_requests": 3,
    }


def test_extract_copilot_session_fallback_directory_and_time_filter(tmp_path: Path) -> None:
    copilot_home = tmp_path / "empty-copilot-home"
    copilot_home.mkdir()
    fallback_home = tmp_path / "fallback"
    sess_old = fallback_home / "session-state" / "sess-old"
    sess_old.mkdir(parents=True)
    sess_new = fallback_home / "session-state" / "sess-new"
    sess_new.mkdir(parents=True)

    (sess_old / "events.jsonl").write_text(
        json.dumps({"type": "session.shutdown", "data": {"currentModel": "old-model"}}) + "\n",
        encoding="utf-8",
    )
    (sess_new / "events.jsonl").write_text(
        json.dumps({"type": "session.shutdown", "data": {"currentModel": "new-model"}}) + "\n",
        encoding="utf-8",
    )

    # Set mtime
    now = worker.now_utc()
    old_time = now.timestamp() - 100.0
    new_time = now.timestamp()
    os.utime(sess_old / "events.jsonl", (old_time, old_time))
    os.utime(sess_new / "events.jsonl", (new_time, new_time))

    info = worker.extract_copilot_session(copilot_home, fallback_home=fallback_home, started_at=now)
    assert info["actual_model"] == "new-model"


def test_extract_copilot_session_missing_or_corrupt(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    assert worker.extract_copilot_session(empty) == {"actual_model": None, "auto_routing": None, "usage": None}

    corrupt = tmp_path / "corrupt"
    sess_dir = corrupt / "session-state" / "sess-x"
    sess_dir.mkdir(parents=True)
    (sess_dir / "events.jsonl").write_text("not json\n{malformed\n", encoding="utf-8")
    assert worker.extract_copilot_session(corrupt) == {"actual_model": None, "auto_routing": None, "usage": None}


def test_copilot_worker_execution_populates_actual_model_and_usage(env: Env) -> None:
    # Update fake copilot script in env to write real events.jsonl to COPILOT_HOME
    copilot_bin = Path(env.root / "copilot")
    copilot_bin.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "ch = Path(os.environ.get('COPILOT_HOME', ''))\n"
        "if ch.is_dir():\n"
        "    sdir = ch / 'session-state' / 'sess-live'\n"
        "    sdir.mkdir(parents=True, exist_ok=True)\n"
        "    events = [\n"
        "        {'type': 'session.auto_mode_resolved', 'data': {'chosenModel': 'mai-code-2-flash', 'candidateModels': ['mai-code-2-flash']}},\n"
        "        {'type': 'session.shutdown', 'data': {'currentModel': 'mai-code-2-flash', 'tokenDetails': {'input': {'tokenCount': 50}, 'output': {'tokenCount': 25}}}},\n"
        "    ]\n"
        "    (sdir / 'events.jsonl').write_text('\\n'.join(json.dumps(e) for e in events) + '\\n')\n"
        "Path('tracked.txt').write_text('after\\n')\n"
        "print('worker ok')\n"
    )
    copilot_bin.chmod(0o755)

    result = env.run()
    assert result["status"] == "succeeded"
    assert result["actual_model"] == "mai-code-2-flash"
    assert result["auto_routing"]["chosen_model"] == "mai-code-2-flash"
    assert result["usage"]["input_tokens"] == 50
    assert result["usage"]["output_tokens"] == 25
    assert result["usage"]["total_tokens"] == 75
