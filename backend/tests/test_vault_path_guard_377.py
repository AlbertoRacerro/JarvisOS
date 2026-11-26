"""Spec 377: sandbox mounts, registered jobs, eval runs and walks refuse the private vault."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.modules.relay_gateway import sandbox, service
from app.modules.relay_gateway.service import RelayGatewayError

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import vault_guard  # noqa: E402
from scripts.benchmarks.agent_eval import runner  # noqa: E402

JOBS_SPEC = importlib.util.spec_from_file_location("relay_jobs_377", REPO_ROOT / "scripts" / "relay_jobs.py")
assert JOBS_SPEC and JOBS_SPEC.loader
jobs = importlib.util.module_from_spec(JOBS_SPEC)
JOBS_SPEC.loader.exec_module(jobs)

from test_relay_gateway_157 import _submit, _thread, _wait, gateway  # noqa: E402,F401


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "private" / "vault"
    (path / "jarvis-data").mkdir(parents=True)
    monkeypatch.setenv("JARVISOS_VAULT_PATH", str(path))
    return path


def test_containment_rule_and_sandbox_copy_agree(tmp_path: Path, vault: Path) -> None:
    sibling = vault.parent / "vault-x"
    sibling.mkdir()
    link = tmp_path / "link-into-vault"
    link.symlink_to(vault / "jarvis-data")
    cases = {
        vault: True, vault / "jarvis-data": True, vault / "missing" / "file.db": True, link: True,
        vault.parent: True, tmp_path: True, sibling: False, tmp_path / "fresh-root": False,
    }
    assert vault_guard.vault_root() == sandbox.vault_root() == vault.resolve()
    for path, expected in cases.items():
        assert vault_guard.touches_vault(path, vault_guard.vault_root()) is expected, path
        assert sandbox.touches_vault(str(path), sandbox.vault_root()) is expected, path


def test_unset_or_blank_variable_guards_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("JARVISOS_VAULT_PATH", raising=False)
    assert vault_guard.vault_root() is None and not vault_guard.touches_vault(tmp_path, None)
    monkeypatch.setenv("JARVISOS_VAULT_PATH", "  ")
    assert vault_guard.vault_root() is None and sandbox.vault_root() is None


def test_missing_vault_directory_still_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_VAULT_PATH", str(tmp_path / "not-yet-created"))
    assert vault_guard.touches_vault(tmp_path / "not-yet-created" / "x", vault_guard.vault_root())


def test_cli_refuses_vault_paths_without_printing_them(tmp_path: Path, vault: Path) -> None:
    script = REPO_ROOT / "scripts" / "vault_guard.py"
    refused = subprocess.run([sys.executable, str(script), "check", str(tmp_path / "fresh"), str(vault / "jarvis-data")],
                             capture_output=True, text=True, check=False)
    assert refused.returncode == vault_guard.REFUSED_EXIT
    assert str(vault) not in refused.stdout + refused.stderr and "private vault" in refused.stderr
    passed = subprocess.run([sys.executable, str(script), "check", str(tmp_path / "fresh")],
                            capture_output=True, text=True, check=False)
    assert passed.returncode == 0 and passed.stdout == passed.stderr == ""


def _manifest(gateway) -> dict:  # noqa: F811
    workspace_id, thread_id = _thread()
    run = _wait(workspace_id, thread_id, _submit(workspace_id, thread_id).id)
    path = gateway["runtime"].root / "manifests" / sandbox.manifest_name(run.workspace_path or "")
    return json.loads(path.read_text(encoding="utf-8"))


def _rebuild(gateway, manifest: dict) -> dict:  # noqa: F811
    return service.build_manifest(gateway["runtime"], gateway["config"], "claude", Path(manifest["workspace"]),
                                  Path(manifest["context_dir"]), Path(manifest["home"]), "repository")


def test_manifest_refuses_a_mount_inside_the_vault(gateway, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    manifest = _manifest(gateway)
    assert "deny_roots" not in manifest
    credential = manifest["agents"]["claude"]["credentials"][0]["source"]
    vault = str(Path(credential).parent)
    monkeypatch.setenv("JARVISOS_VAULT_PATH", vault)
    with pytest.raises(RelayGatewayError) as refused:
        _rebuild(gateway, manifest)
    assert str(refused.value) == "relay_mount_vault_refused" and vault not in str(refused.value.__cause__)


def test_manifest_records_deny_roots_and_shim_rechecks(gateway, tmp_path: Path,  # noqa: F811
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest(gateway)
    elsewhere = tmp_path / "elsewhere-vault"
    monkeypatch.setenv("JARVISOS_VAULT_PATH", str(elsewhere))
    rebuilt = _rebuild(gateway, manifest)
    assert rebuilt["deny_roots"] == [str(elsewhere.resolve())]
    runtime_dir = str(gateway["runtime"].root / "sandbox-runs" / "run-x")
    assert sandbox.build_bwrap_args(rebuilt, "claude", runtime_dir, [])
    rebuilt["dependency_mounts"] = [{"source": str(elsewhere / "jarvis-data"), "target": "data"}]
    with pytest.raises(sandbox.ManifestError) as refused:
        sandbox.build_bwrap_args(rebuilt, "claude", runtime_dir, [])
    assert str(elsewhere) not in str(refused.value)
    # The shim also checks its own environment, even when the manifest carries no deny_roots.
    del rebuilt["deny_roots"]
    with pytest.raises(sandbox.ManifestError):
        sandbox.build_bwrap_args(rebuilt, "claude", runtime_dir, [])


def _job_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "control"
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(root))
    monkeypatch.setattr(jobs, "allowed_roots", lambda: (tmp_path,))
    monkeypatch.setattr(jobs, "ledger", lambda *a: None)
    return root


def test_job_start_refuses_vault_paths_and_registers_nothing(tmp_path: Path, vault: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    root = _job_env(tmp_path, monkeypatch)
    (vault / "inputs.json").write_text("{}")
    work = tmp_path / "work"
    work.mkdir()
    ok = {"label": "run", "authority": "spec 377", "cwd": work, "wall_hours": 1.0, "spawn": lambda c: None, "wait": 0.0}
    for argv, extra in ((["true"], {"cwd": vault / "jarvis-data"}),
                        (["true"], {"inputs": [vault / "inputs.json"]}),
                        (["cp", "-r", str(vault / "jarvis-data"), "copy"], {}),
                        (["python3", "run.py", f"--data-root={vault / 'jarvis-data'}"], {})):
        with pytest.raises(ValueError) as refused:
            jobs.start(argv, **{**ok, **extra})
        assert "private vault" in str(refused.value) and str(vault) not in str(refused.value)
    assert not list(root.glob("jobs/*.json"))
    job = jobs.start(["python3", "run.py", "--data-root", str(work / "fresh")], **ok)
    assert job["status"] == "starting"


def test_eval_run_refuses_a_vault_data_root_before_any_request(tmp_path: Path, vault: Path,
                                                               monkeypatch: pytest.MonkeyPatch,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    def no_run(**_kwargs):
        raise AssertionError("run_eval must not start")

    monkeypatch.setattr(runner, "run_eval", no_run)
    base = ["run", "--tasks", str(tmp_path / "t.json"), "--label", "x", "--route-class", "claude:agent"]
    assert runner.main([*base, "--out", str(tmp_path / "out"), "--data-root", str(vault / "jarvis-data")]) == 1
    assert runner.main([*base, "--out", str(vault / "out")]) == 1
    assert str(vault) not in capsys.readouterr().err
