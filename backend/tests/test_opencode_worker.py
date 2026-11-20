from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from shutil import which

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
    pools.write_text(json.dumps({"reference": "luna", "scores": {"luna": 28}, "pools": [{
        "name": "testpool", "provider": "Test Public", "base_url": "https://example.invalid/v1",
        "model": "test-model", "key_file": str(key), "free_list": "openai_models", "index_score": 30,
        "rate_limits": "free", "data_policy": "public",
    }, {
        "name": "zenpool", "provider": "OpenCode Zen", "model": "zen-model-free", "free_list": "opencode_zen",
        "index_score": 33, "rate_limits": "free", "data_policy": "public",
    }]}), encoding="utf-8")
    monkeypatch.setenv("TEMP_POOLS_FILE", str(pools))
    return pools, key


@pytest.fixture
def no_sandbox(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path, Path]]:
    """CI runners may forbid user namespaces; the bubblewrap argv is tested on its own below."""
    calls: list[tuple[str, Path, Path]] = []

    def fake(binary: str, worktree: Path, scratch: Path) -> list[str]:
        calls.append((binary, worktree, scratch))
        return []

    monkeypatch.setattr(ow, "sandbox_argv", fake)
    return calls


@pytest.fixture
def linked_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "worktree"
    repo = Path(__file__).resolve().parents[2]
    ref = "origin/master"
    probe = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", ref],
                           capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        ref = "HEAD"
    done = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), ref],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    yield worktree
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
                   capture_output=True, text=True, check=False)


def test_run_refuses_pack_outside_repository_and_strips_environment(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_pool: tuple[Path, Path],
        linked_worktree: Path, no_sandbox: list[tuple[str, Path, Path]]) -> None:
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


def test_zen_pool_runs_built_in_provider_without_key(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_pool: tuple[Path, Path],
        linked_worktree: Path, no_sandbox: list[tuple[str, Path, Path]]) -> None:
    capture = tmp_path / "child.json"
    fake_bin = tmp_path / "opencode"
    fake_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"json.dump({{'env': dict(os.environ), 'argv': sys.argv}}, open({str(capture)!r}, 'w'))\n",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)
    monkeypatch.setenv("JARVISOS_OPENCODE_BIN", str(fake_bin))
    task = tmp_path / "task.md"
    task.write_text("Do no harm.", encoding="utf-8")
    evidence = tmp_path / "evidence"
    args = ow.argparse.Namespace(pool="zenpool", task_file=str(task), worktree=str(linked_worktree),
                                 timeout=30, evidence_dir=str(evidence))
    _, result = ow.execute_run(args)
    assert result["status"] == "succeeded"
    child = json.loads(capture.read_text(encoding="utf-8"))
    assert "TEMP_POOL_API_KEY" not in child["env"]
    assert child["argv"][child["argv"].index("--model") + 1] == "opencode/zen-model-free"
    config = json.loads((evidence / "opencode.json").read_text(encoding="utf-8"))
    assert "provider" not in config and config["permission"]["external_directory"] == "deny"


def _smoke_bin(tmp_path: Path, exit_code: int) -> Path:
    fake_bin = tmp_path / f"opencode-{exit_code}"
    fake_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "task = ' '.join(sys.argv)\n"
        "if 'returns the sum' in task:\n"
        "    pathlib.Path('scratch.py').write_text('def add(a, b):\\n    return a + b\\n')\n"
        "elif 'True for even integers' in task:\n"
        "    pathlib.Path('scratch.py').write_text('def is_even(number):\\n    return number % 2 == 0\\n')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)
    return fake_bin


def test_smoke_marks_the_bridge_and_never_a_tier(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                 temp_pool: tuple[Path, Path],
                                                 no_sandbox: list[tuple[str, Path, Path]]) -> None:
    sys.path.insert(0, str(SCRIPT.parent))
    import worker_routing as routing

    record_file = tmp_path / "worker-outcomes.jsonl"
    args = ow.argparse.Namespace(pool="testpool", timeout=30, record_file=record_file)
    monkeypatch.setenv("JARVISOS_OPENCODE_BIN", str(_smoke_bin(tmp_path, 1)))
    assert ow.execute_smoke(args) == 0
    records = [json.loads(line) for line in record_file.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 3
    assert all(r["failure"] == "harness" and r["task"].startswith("tempsmoke-") for r in records)
    assert routing.bridge_state("temp-testpool") == "suspect"
    entry = next(e for e in routing.load_temp_pools() if e["name"] == "testpool")
    assert routing.temp_capability(entry, records) == 1
    assert len(no_sandbox) == 3
    assert not any(worktree.exists() for _, worktree, _ in no_sandbox)

    monkeypatch.setenv("JARVISOS_OPENCODE_BIN", str(_smoke_bin(tmp_path, 0)))
    assert ow.execute_smoke(args) == 0
    records = [json.loads(line) for line in record_file.read_text(encoding="utf-8").splitlines()]
    assert [r["failure"] for r in records[3:]] == ["none", "none", "model"]  # the merge task is left undone
    assert routing.bridge_state("temp-testpool") == "suspect"
    two = ow.smoke_tasks()[:2]
    monkeypatch.setattr(ow, "smoke_tasks", lambda: two)
    assert ow.execute_smoke(args) == 0
    assert routing.bridge_state("temp-testpool") == "ok"
    records = [json.loads(line) for line in record_file.read_text(encoding="utf-8").splitlines()]
    assert routing.temp_capability(entry, records) == 1


def test_key_file_inside_repository_or_symlinked_is_refused(tmp_path: Path) -> None:
    inside = ow.repo_root() / "scripts" / "opencode_worker.py"
    entry = {"name": "p", "provider": "x", "base_url": "https://example.invalid/v1", "model": "m",
             "free_list": "openai_models", "key_file": str(inside)}
    with pytest.raises(ow.Refused) as inside_refusal:
        ow.validate_pool(entry)
    assert inside_refusal.value.reason == "invalid_key_file"
    link = tmp_path / "key-link"
    link.symlink_to(inside)
    with pytest.raises(ow.Refused) as link_refusal:
        ow.validate_pool({**entry, "key_file": str(link)})
    assert link_refusal.value.reason == "invalid_key_file"


def test_unreadable_key_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                     temp_pool: tuple[Path, Path], linked_worktree: Path,
                                     no_sandbox: list[tuple[str, Path, Path]]) -> None:
    _, key = temp_pool
    key.write_bytes(b"\xff\xfe not utf-8")
    task = tmp_path / "task.md"
    task.write_text("Do no harm.", encoding="utf-8")
    args = ow.argparse.Namespace(pool="testpool", task_file=str(task), worktree=str(linked_worktree),
                                 timeout=30, evidence_dir=str(tmp_path / "evidence"))
    _, result = ow.execute_run(args)
    assert result["status"] == "refused" and result["reason"] == "key_unreadable"


def test_sandbox_hides_home_tmp_and_windows_drives(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                   linked_worktree: Path) -> None:
    monkeypatch.setattr(ow.shutil, "which", lambda name: None)
    with pytest.raises(ow.Refused) as missing:
        ow.sandbox_argv("/usr/bin/true", linked_worktree, tmp_path)
    assert missing.value.reason == "sandbox_missing"
    monkeypatch.setattr(ow.shutil, "which", lambda name: "/usr/bin/bwrap")
    argv = ow.sandbox_argv("/usr/bin/true", linked_worktree, tmp_path)
    pairs = list(zip(argv, argv[1:], strict=False))
    for hidden in (str(Path.home()), "/tmp", "/mnt"):
        assert ("--tmpfs", hidden) in pairs
    assert ("--bind", str(linked_worktree.resolve())) in pairs
    git_dir = subprocess.run(["git", "-C", str(linked_worktree), "rev-parse", "--path-format=absolute", "--git-dir"],
                             capture_output=True, text=True, check=True).stdout.strip()
    assert ("--ro-bind", str(Path(git_dir).resolve())) in pairs  # a worker cannot rewrite HEAD or refs
    assert ("--bind", str(Path(git_dir).resolve())) not in pairs
    # a loose binary in the home would bind back a private subtree of it
    loose = Path.home() / "some-dir" / "bin" / "opencode"
    with pytest.raises(ow.Refused) as loose_refusal:
        ow.sandbox_argv(str(loose), linked_worktree, tmp_path)
    assert loose_refusal.value.reason == "package_unrecognised"
    assert argv.index("--tmpfs") < argv.index("--bind")  # writable binds are mounted over the hidden trees


def _bwrap_runs() -> bool:
    if not which("bwrap"):
        return False
    probe = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "true"], capture_output=True, check=False)
    return probe.returncode == 0


@pytest.mark.skipif(not _bwrap_runs(), reason="bubblewrap cannot create namespaces on this host")
def test_sandbox_child_cannot_see_home(tmp_path: Path, linked_worktree: Path) -> None:
    scratch = Path(ow.tempfile.mkdtemp(prefix="opencode-worker-"))
    try:
        argv = ow.sandbox_argv("/usr/bin/true", linked_worktree, scratch)
        bound = [str(p) for p in (linked_worktree.resolve(), ow.repo_root().resolve(), Path("/usr/bin"))]
        home = Path.home()
        private = [p for p in home.iterdir() if not any(b == str(p) or b.startswith(str(p) + "/") for b in bound)]
        listed = subprocess.run([*argv, "ls", "-a", str(home)], capture_output=True, text=True, check=False)
        assert listed.returncode == 0
        assert not set(listed.stdout.split()) & {p.name for p in private}
    finally:
        ow.shutil.rmtree(scratch, ignore_errors=True)
