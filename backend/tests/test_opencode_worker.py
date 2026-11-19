from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "opencode_worker.py"
SPEC = importlib.util.spec_from_file_location("opencode_worker_under_test", SCRIPT)
assert SPEC and SPEC.loader
ow = importlib.util.module_from_spec(SPEC)
sys.modules["opencode_worker_under_test"] = ow
SPEC.loader.exec_module(ow)


@pytest.fixture
def temp_pool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    monkeypatch.delenv("JARVISOS_PACK_PATH", raising=False)  # the suite loads a fake pack by default
    key = tmp_path / "pool.key"
    key.write_text("TEST_TEMP_POOL_SECRET", encoding="utf-8")
    pools = tmp_path / "temp-pools.json"
    pools.write_text(json.dumps([{
        "name": "testpool", "provider": "Test Public", "base_url": "https://example.invalid/v1",
        "model": "test-model", "key_file": str(key),
        "expires_at": "2099-01-01T00:00:00Z",
        "rate_limits": "free", "data_policy": "public",
    }]), encoding="utf-8")
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools))
    return pools, key


@pytest.fixture
def linked_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "worktree"
    repo = Path(__file__).resolve().parents[2]
    ref = "origin/master"
    probe = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", ref],
                           capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        ref = "master"
    done = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), ref],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    yield worktree
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
                   capture_output=True, text=True, check=False)


def test_run_refuses_pack_outside_repository_and_strips_environment(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_pool: tuple[Path, Path],
        linked_worktree: Path) -> None:
    capture = tmp_path / "child-env.json"
    fake_bin = tmp_path / "opencode"
    fake_bin.write_text(
        "#!/bin/sh\n"
        f"python3 - <<'PY'\n"
        "import json, os\n"
        f"json.dump(dict(os.environ), open({str(capture)!r}, 'w'))\n"
        "PY\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)
    monkeypatch.setenv("JARVISOS_OPENCODE_BIN", str(fake_bin))
    monkeypatch.setenv("GH_TOKEN", "gh-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    task = tmp_path / "task.md"
    task.write_text("Do no harm.", encoding="utf-8")
    evidence = tmp_path / "evidence"
    args = ow.argparse.Namespace(pool="testpool", task_file=str(task), worktree=str(linked_worktree),
                                 timeout=30, evidence_dir=str(evidence))
    _, result = ow.execute_run(args)
    assert result["status"] == "succeeded"
    child = json.loads(capture.read_text(encoding="utf-8"))
    for forbidden in ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                      "GEMINI_API_KEY", "JARVISOS_PACK_PATH"):
        assert forbidden not in child
    assert not any(key.startswith("GIT_") for key in child)
    assert child["TEMP_POOL_API_KEY"] == "TEST_TEMP_POOL_SECRET"

    evidence_text = "\n".join(path.read_text(encoding="utf-8", errors="replace")
                              for path in evidence.rglob("*") if path.is_file())
    assert "TEST_TEMP_POOL_SECRET" not in evidence_text
    config_text = (evidence / "opencode.json").read_text(encoding="utf-8")
    assert "TEST_TEMP_POOL_SECRET" not in config_text
    assert "{env:TEMP_POOL_API_KEY}" in config_text
    assert '"baseURL": "https://example.invalid/v1"' in config_text
    assert '"npm": "@ai-sdk/openai-compatible"' in config_text
    assert '"external_directory": "deny"' in config_text

    monkeypatch.setenv("JARVISOS_PACK_PATH", "/tmp/private-pack")
    _, packed = ow.execute_run(args)
    assert packed["status"] == "refused"
    assert packed["reason"] == "private_pack_visible"

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_args = ow.argparse.Namespace(pool="testpool", task_file=str(task),
                                         worktree=str(outside), timeout=30,
                                         evidence_dir=str(tmp_path / "outside-evidence"))
    monkeypatch.delenv("JARVISOS_PACK_PATH")
    _, outside_result = ow.execute_run(outside_args)
    assert outside_result["status"] == "refused"
    assert "worktree" in outside_result["reason"] or "linked" in outside_result["reason"]


def test_qualify_records_three_results(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                       temp_pool: tuple[Path, Path]) -> None:
    capture_dir = tmp_path / "qualify-capture"
    capture_dir.mkdir()
    fake_bin = tmp_path / "opencode"
    fake_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"capture = pathlib.Path({str(capture_dir)!r})\n"
        "task = ' '.join(sys.argv)\n"
        "if 'returns the sum' in task:\n"
        "    pathlib.Path('scratch.py').write_text('def add(a, b):\\n    return a + b\\n')\n"
        "elif 'True for even integers' in task:\n"
        "    pathlib.Path('scratch.py').write_text('def is_even(number):\\n    return number % 2 == 0\\n')\n"
        "elif 'tier2-merge' in task:\n"
        "    pass\n",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)
    monkeypatch.setenv("JARVISOS_OPENCODE_BIN", str(fake_bin))
    record_file = tmp_path / "worker-outcomes.jsonl"
    args = ow.argparse.Namespace(pool="testpool", timeout=30, record_file=record_file)
    assert ow.execute_qualification(args) == 0
    records = [json.loads(line) for line in record_file.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 3
    assert sum(record["ci_first_pass"] == "pass" for record in records) == 2
    qualification = next((tmp_path / "continuity").parent.rglob("qualification.json"))
    assert json.loads(qualification.read_text(encoding="utf-8"))["tier"] == 1
