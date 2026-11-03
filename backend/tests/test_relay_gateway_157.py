from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.sensitivity import (
    approve_sanitized_derivative,
    create_sanitized_derivative,
    revoke_sanitized_derivative,
)
from app.modules.ai.sensitivity_models import SanitizedDerivativeCreate
from app.modules.events.service import utc_now
from app.modules.relay_gateway import sandbox, service
from app.modules.relay_gateway.service import (
    ContextReleaseCreate,
    RelayGatewayError,
    RelayRunRequest,
    RelayRuntime,
)

CODING_TASK = "Fix this bug in the Jarvis Sidecar and run the tests."
GEOMETRY_TASK = "Analyze our real proprietary photobioreactor geometry and improve the design."


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    source = tmp_path / "source"
    (source / "docs" / "relay-context").mkdir(parents=True)
    (source / "docs" / "relay-context" / "README.md").write_text("stable safe context\n", encoding="utf-8")
    (source / "backend" / ".venv").mkdir(parents=True)
    (source / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git(source, "init", "-q", "-b", "master")
    (source / ".gitignore").write_text("backend/.venv/\n", encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "init")
    binary = tmp_path / "tools" / "claude-code" / "bin" / "claude.exe"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    config = json.loads(service.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    config["enabled"] = True
    config_path = tmp_path / "relay_gateway.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("JARVISOS_RELAY_GATEWAY_CONFIG", str(config_path))
    launches: list[dict] = []
    session_id = "20260928-000000-abc123"
    edits: list[str] = []

    def launch(argv: list[str], cwd: Path, env: dict[str, str], log_path: Path) -> subprocess.Popen[bytes]:
        launches.append({"argv": argv, "cwd": cwd, "env": env})
        # Relay links each --continue to a new session id; the fake mirrors that.
        linked = session_id if len(launches) == 1 else f"{session_id}-{len(launches)}"
        for name in edits:
            (cwd / name).write_text(f"edited {len(launches)}\n", encoding="utf-8")
            _git(cwd, "add", name)
            _git(cwd, "commit", "-qm", f"agent edit {len(launches)}")
        log_path.write_text(json.dumps({
            "session_id": linked, "stop_reason": "done",
            "turns": [{"turn": 1, "exit_code": 0, "text": f"done {len(launches)}"}],
        }), encoding="utf-8")
        return subprocess.Popen(["true"])

    runtime = RelayRuntime(tmp_path / "relay", source, "/opt/relay", lambda _name: str(binary), launch)
    service.set_runtime(runtime)
    initialize_database()
    yield {"config_path": config_path, "config": config, "runtime": runtime, "launches": launches,
           "session_id": session_id, "source": source, "edits": edits}
    service.set_runtime(None)


def _thread(workspace_id: str = "relay-test") -> tuple[str, str]:
    thread_id, now = str(uuid4()), utc_now()
    with open_sqlite_connection() as connection:
        connection.execute("INSERT OR IGNORE INTO workspaces (id, name, slug, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                           (workspace_id, workspace_id, workspace_id, now, now))
        connection.execute("INSERT INTO ai_threads (id, workspace_id, created_at, last_activity_at) VALUES (?, ?, ?, ?)",
                           (thread_id, workspace_id, now, now))
        connection.commit()
    return workspace_id, thread_id


def _derivative(workspace_id: str, content: str = "Synthetic CFD fixture: two runs, fields u,v,p.") -> str:
    record_id, now = str(uuid4()), utc_now()
    with open_sqlite_connection() as connection:
        connection.execute("INSERT INTO decisions (id, workspace_id, title, decision_text, status, created_at, updated_at) VALUES (?, ?, 'Local', 'Private CFD', 'accepted', ?, ?)",
                           (record_id, workspace_id, now, now))
        connection.commit()
    drafted = create_sanitized_derivative(SanitizedDerivativeCreate(
        workspace_id=workspace_id, source_refs=[f"decision:{record_id}"], content=content,
        effective_level="S1", transformations=["replaced real data with a synthetic schema"],
    ))
    return approve_sanitized_derivative(workspace_id, drafted.id).id


def _wait(workspace_id: str, thread_id: str, run_id: str) -> service.RelayRunRead:
    deadline = time.time() + 10
    while time.time() < deadline:
        run = service.get_relay_run(workspace_id, thread_id, run_id)
        if run.state not in {"queued", "running"}:
            return run
        time.sleep(0.05)
    raise AssertionError("relay run did not finish")


def _submit(workspace_id: str, thread_id: str, prompt: str = CODING_TASK, attested: bool = True):
    return service.submit_relay_run(workspace_id, thread_id,
                                    RelayRunRequest(prompt=prompt, agent="claude", cloud_safe_attested=attested))


def test_classification_maps_to_access_mode_and_rejects_unknown_levels(tmp_path: Path) -> None:
    assert [service.access_mode_for(level) for level in service.LEVELS] == [
        "repository", "repository", "derivative", "derivative", "derivative"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"repository": {"level": "CLOUD"}}), encoding="utf-8")
    with pytest.raises(RelayGatewayError):
        service.load_config(bad)
    shipped = service.relay_status(service.load_config(service.DEFAULT_CONFIG_PATH))
    assert shipped.repository_level == "S1" and shipped.access_mode == "repository"


def test_shipped_gateway_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVISOS_RELAY_GATEWAY_ENABLED", raising=False)
    assert service.gateway_enabled(service.load_config(service.DEFAULT_CONFIG_PATH)) is False


def test_admission_is_deterministic_and_fail_closed(gateway) -> None:
    workspace_id, _ = _thread()
    assert service.admit_prompt("key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789", attested=True,
                                workspace_id=workspace_id).reason_code == "prompt_secret_detected"
    assert service.admit_prompt(GEOMETRY_TASK, attested=True,
                                workspace_id=workspace_id).reason_code == "prompt_sanitization_required"
    assert service.admit_prompt(CODING_TASK, attested=False,
                                workspace_id=workspace_id).reason_code == "prompt_classification_required"
    admitted = service.admit_prompt(CODING_TASK, attested=True, workspace_id=workspace_id)
    assert admitted.allowed and admitted.prompt_source == "operator_attested"


def test_denied_task_is_recorded_without_dispatch(gateway) -> None:
    workspace_id, thread_id = _thread()
    run = _submit(workspace_id, thread_id, GEOMETRY_TASK)
    assert run.state == "denied" and run.reason_code == "prompt_sanitization_required"
    assert gateway["launches"] == []


def test_disabled_gateway_and_unknown_agent_are_refused(gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, thread_id = _thread()
    with pytest.raises(RelayGatewayError, match="relay_agent_not_allowed"):
        service.submit_relay_run(workspace_id, thread_id, RelayRunRequest(prompt=CODING_TASK, agent="gemini",
                                                                          cloud_safe_attested=True))
    gateway["config"]["enabled"] = False
    gateway["config_path"].write_text(json.dumps(gateway["config"]), encoding="utf-8")
    monkeypatch.delenv("JARVISOS_RELAY_GATEWAY_ENABLED", raising=False)
    with pytest.raises(RelayGatewayError, match="relay_gateway_disabled"):
        _submit(workspace_id, thread_id)


def test_coding_task_runs_in_repository_clone_and_later_turn_continues_session(gateway) -> None:
    workspace_id, thread_id = _thread()
    first = _wait(workspace_id, thread_id, _submit(workspace_id, thread_id).id)
    assert first.state == "completed" and first.result_text == "done 1"
    assert first.relay_session_id == gateway["session_id"] and first.access_mode == "repository"
    workspace = Path(first.workspace_path or "")
    assert (workspace / "app.py").read_text(encoding="utf-8") == "print('v1')\n"
    assert not os.path.samefile(workspace / "app.py", gateway["source"] / "app.py")
    launch = gateway["launches"][0]
    argv = launch["argv"]
    assert argv[:6] == ["/opt/relay", "--json", "run", "claude", "--yes", "--repo"]
    assert argv[argv.index("--max-turns") + 1] == str(gateway["config"]["max_turns"])
    assert argv[-2] == "--task" and argv[-1].endswith(CODING_TASK)
    assert "--continue" not in argv and first.continued_from_session_id is None
    assert launch["env"]["PATH"].startswith(str(gateway["runtime"].root / "bin") + ":")
    # Relay's own permission settings are untouched; only the sandboxed worker gets its flags.
    assert set(launch["env"]) == {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TERM", "GIT_CEILING_DIRECTORIES",
                                  *service.HOST_GIT_ENV}
    assert launch["env"]["GIT_CONFIG_KEY_0"] == "core.fsmonitor" and launch["env"]["GIT_CONFIG_VALUE_0"] == "false"

    second = _wait(workspace_id, thread_id, _submit(workspace_id, thread_id).id)
    argv = gateway["launches"][1]["argv"]
    assert argv[argv.index("--continue") + 1] == gateway["session_id"]
    assert second.workspace_path == first.workspace_path and second.turn_index == 1
    assert second.continued_from_session_id == gateway["session_id"]
    assert second.relay_session_id == f"{gateway['session_id']}-2"

    third = _wait(workspace_id, thread_id, _submit(workspace_id, thread_id).id)
    argv = gateway["launches"][2]["argv"]
    assert argv[argv.index("--continue") + 1] == f"{gateway['session_id']}-2"
    assert third.continued_from_session_id == second.relay_session_id and third.turn_index == 2


def test_run_returns_commit_and_diff_evidence(gateway) -> None:
    workspace_id, thread_id = _thread()
    gateway["edits"].append("app.py")
    run = _wait(workspace_id, thread_id, _submit(workspace_id, thread_id).id)
    assert run.state == "completed" and run.base_commit and run.head_commit
    assert run.head_commit != run.base_commit
    assert "agent edit 1" in (run.change_summary or "") and "app.py" in (run.change_summary or "")
    gateway["edits"].clear()
    unchanged = _wait(workspace_id, thread_id, _submit(workspace_id, thread_id).id)
    assert unchanged.head_commit == run.head_commit and unchanged.change_summary == "No changes."


def test_private_domain_data_gate_refuses_runs_until_sandbox_accepted(gateway) -> None:
    workspace_id, thread_id = _thread()
    shipped = service.load_config(service.DEFAULT_CONFIG_PATH)["private_domain_data"]
    assert shipped["enabled"] is False and shipped["sandbox_isolation_accepted"] is False
    gateway["config"]["private_domain_data"] = {"enabled": True, "sandbox_isolation_accepted": False}
    gateway["config_path"].write_text(json.dumps(gateway["config"]), encoding="utf-8")
    status = service.relay_status()
    assert status.private_domain_data_enabled and status.blocked_reason == "private_domain_data_sandbox_not_accepted"
    with pytest.raises(RelayGatewayError, match="private_domain_data_sandbox_not_accepted"):
        _submit(workspace_id, thread_id)
    assert gateway["launches"] == []
    gateway["config"]["private_domain_data"]["sandbox_isolation_accepted"] = True
    gateway["config_path"].write_text(json.dumps(gateway["config"]), encoding="utf-8")
    assert _wait(workspace_id, thread_id, _submit(workspace_id, thread_id).id).state == "completed"


def test_sandboxed_claude_gets_non_interactive_permissions_only_inside_bwrap(gateway) -> None:
    workspace_id, thread_id = _thread()
    run = _wait(workspace_id, thread_id, _submit(workspace_id, thread_id).id)
    root = gateway["runtime"].root
    manifest = json.loads((root / "manifests" / sandbox.manifest_name(run.workspace_path or ""))
                          .read_text(encoding="utf-8"))
    args = sandbox.build_bwrap_args(manifest, "claude", str(root / "sandbox-runs" / "run-x"),
                                    ["-p", "hi", "--output-format", "stream-json"])
    assert args[0].endswith("bwrap")
    binary = args.index(manifest["agents"]["claude"]["binary"])
    assert args[binary + 1:] == ["--permission-mode", "bypassPermissions", "-p", "hi", "--output-format", "stream-json"]
    assert "--permission-mode" not in " ".join(gateway["launches"][0]["argv"])


def test_agent_planted_git_metadata_never_executes_on_the_host(gateway, tmp_path: Path) -> None:
    workspace_id, thread_id = _thread()
    run = _wait(workspace_id, thread_id, _submit(workspace_id, thread_id).id)
    workspace = Path(run.workspace_path or "")
    root = gateway["runtime"].root
    manifest = json.loads((root / "manifests" / sandbox.manifest_name(str(workspace))).read_text(encoding="utf-8"))
    args = sandbox.build_bwrap_args(manifest, "claude", str(root / "sandbox-runs" / "run-x"), [])
    joined = " ".join(args)
    assert f"--ro-bind {manifest['git_config']} {workspace}/.git/config" in joined
    assert f"--tmpfs {workspace}/.git/hooks" in joined
    trusted = Path(manifest["git_config"]).read_bytes()

    # What a prompt-injected agent could leave behind if it escaped the read-only bind.
    canary = tmp_path / "PWNED"
    _git(workspace, "config", "core.fsmonitor", f"touch {canary}; false")
    (workspace / ".git" / "hooks" / "post-checkout").write_text(f"#!/bin/sh\ntouch {canary}\n", encoding="utf-8")
    (workspace / ".git" / "commondir").write_text(str(tmp_path), encoding="utf-8")
    nested = workspace / "vendor" / "sub"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    _git(nested, "config", "core.fsmonitor", f"touch {canary}; false")

    sandbox.restore_workspace_git(str(workspace), manifest["git_config"])
    assert (workspace / ".git" / "config").read_bytes() == trusted
    assert list((workspace / ".git" / "hooks").iterdir()) == []
    assert not (workspace / ".git" / "commondir").exists() and not (nested / ".git").exists()
    subprocess.run(["git", "status", "--short"], cwd=workspace, check=True, capture_output=True)
    assert not canary.exists()


def test_host_git_ignores_repository_fsmonitor_and_gitfile_redirects(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    canary = tmp_path / "PWNED"
    _git(repo, "config", "core.fsmonitor", f"touch {canary}; false")
    service._git("status", "--short", "--ignore-submodules=all", cwd=repo)
    assert not canary.exists()
    trusted = tmp_path / "trusted.config"
    trusted.write_text("[core]\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    gitfile = tmp_path / "ws"
    gitfile.mkdir()
    (gitfile / ".git").write_text(f"gitdir: {elsewhere}\n", encoding="utf-8")
    sandbox.restore_workspace_git(str(gitfile), str(trusted))
    assert not (gitfile / ".git").exists()


def test_manifest_and_shim_bind_only_admitted_paths(gateway) -> None:
    workspace_id, thread_id = _thread()
    run = _wait(workspace_id, thread_id, _submit(workspace_id, thread_id).id)
    root = gateway["runtime"].root
    manifest_path = root / "manifests" / sandbox.manifest_name(run.workspace_path or "")
    assert oct(manifest_path.stat().st_mode & 0o777) == "0o600"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    args = sandbox.build_bwrap_args(manifest, "claude", str(root / "sandbox-runs" / "run-x"), ["-p", "hi"])
    joined = " ".join(args)
    for flag in ("--unshare-all", "--clearenv", "--die-with-parent", "--new-session"):
        assert flag in args
    assert "--share-net" not in args and "/mnt" not in joined
    home = str(Path.home())
    bound_sources = [args[i + 1] for i, value in enumerate(args) if value in {"--bind", "--ro-bind"}]
    for source in bound_sources:
        assert source.startswith(("/usr", "/etc", str(root), str(gateway["source"]), os.path.dirname(os.path.dirname(os.path.abspath(sandbox.__file__)))))\
            or source == f"{home}/.claude/.credentials.json" or "claude-code" in source, source
    assert f"--tmpfs {run.workspace_path}/.agent-relay" in joined
    env_keys = {args[i + 1] for i, value in enumerate(args) if value == "--setenv"}
    assert not any(key.startswith(("SCALEWAY", "CREDENTIALS", "JARVISOS_DATA")) for key in env_keys)
    shim = (root / "bin" / "claude").read_text(encoding="utf-8")
    assert "sandbox.py' shim 'claude'" in shim


def test_repository_classification_change_yields_derivative_workspace(gateway) -> None:
    workspace_id, thread_id = _thread()
    _wait(workspace_id, thread_id, _submit(workspace_id, thread_id).id)
    gateway["config"]["repository"]["level"] = "S2"
    gateway["config_path"].write_text(json.dumps(gateway["config"]), encoding="utf-8")
    run = _wait(workspace_id, thread_id, _submit(workspace_id, thread_id).id)
    assert run.access_mode == "derivative" and run.repository_level == "S2"
    workspace = Path(run.workspace_path or "")
    assert sorted(path.name for path in workspace.iterdir()) == [".git", "README.md"]
    assert "--continue" not in gateway["launches"][-1]["argv"]
    manifest = json.loads((gateway["runtime"].root / "manifests" / sandbox.manifest_name(str(workspace)))
                          .read_text(encoding="utf-8"))
    assert manifest["dependency_mounts"] == []


def test_context_releases_follow_current_derivative_authority(gateway) -> None:
    workspace_id, thread_id = _thread()
    _, other_thread = _thread(workspace_id)
    kept = _derivative(workspace_id)
    revoked = _derivative(workspace_id, "Approved fact later withdrawn.")
    expired = _derivative(workspace_id, "Approved fact with an expiry.")
    derivative_revoked = _derivative(workspace_id, "Derivative revoked at the source.")
    foreign = _derivative(workspace_id, "Released only to another thread.")
    release = service.create_context_release(workspace_id, ContextReleaseCreate(
        derivative_id=kept, scope="persistent", purpose="CFD view fixture", released_by="operator"))
    withdrawn = service.create_context_release(workspace_id, ContextReleaseCreate(
        derivative_id=revoked, scope="thread", thread_id=thread_id, purpose="x", released_by="operator"))
    service.revoke_context_release(workspace_id, withdrawn.id, "no longer needed")
    service.create_context_release(workspace_id, ContextReleaseCreate(
        derivative_id=expired, scope="persistent", purpose="x", released_by="operator",
        expires_at=datetime.now(UTC) + timedelta(seconds=1)))
    service.create_context_release(workspace_id, ContextReleaseCreate(
        derivative_id=derivative_revoked, scope="persistent", purpose="x", released_by="operator"))
    revoke_sanitized_derivative(workspace_id, derivative_revoked)
    service.create_context_release(workspace_id, ContextReleaseCreate(
        derivative_id=foreign, scope="thread", thread_id=other_thread, purpose="x", released_by="operator"))
    time.sleep(1.1)
    run = _wait(workspace_id, thread_id, _submit(workspace_id, thread_id).id)
    assert run.released_derivative_ids == [kept]
    context = gateway["runtime"].root / "context" / (run.relay_workspace_id or "")
    manifest = json.loads((context / "MANIFEST.json").read_text(encoding="utf-8"))
    assert [entry["release_id"] for entry in manifest["derivatives"]] == [release.id]
    assert sorted(path.name for path in (context / "derivatives").iterdir()) == [f"{kept}.md"]
    assert (context / "stable" / "README.md").exists()


def test_only_approved_cloud_readable_derivatives_can_be_released(gateway) -> None:
    workspace_id, _ = _thread()
    approved = _derivative(workspace_id)
    record = service.revalidate_sanitized_derivative(workspace_id, approved)
    drafted = create_sanitized_derivative(SanitizedDerivativeCreate(
        workspace_id=workspace_id, source_refs=record.source_refs, content="draft", effective_level="S1",
        transformations=["draft only"]))
    with pytest.raises(RelayGatewayError):
        service.create_context_release(workspace_id, ContextReleaseCreate(
            derivative_id=drafted.id, scope="persistent", purpose="x", released_by="operator"))


def test_one_active_run_per_thread(gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, thread_id = _thread()
    monkeypatch.setattr(service, "_start_run", lambda *args: None)
    _submit(workspace_id, thread_id)
    with pytest.raises(RelayGatewayError, match="relay_run_in_progress"):
        _submit(workspace_id, thread_id)


# ---- sandbox ---------------------------------------------------------------------------


def _manifest_file(directory: Path, workspace: Path, recorded: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"version": 1, "workspace": recorded or os.path.realpath(workspace), "agents": {"claude": {}}}
    path = directory / sandbox.manifest_name(str(workspace))
    path.write_text(json.dumps(manifest), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_shim_fails_closed_without_matching_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    assert sandbox.main(["shim", "claude", str(tmp_path / "manifests"), "--", "-p", "x"]) == sandbox.MANIFEST_MISSING_EXIT
    assert "refused" in capsys.readouterr().err
    path = _manifest_file(tmp_path / "manifests", workspace)
    with pytest.raises(sandbox.ManifestError, match="not admitted"):
        sandbox.load_manifest(str(tmp_path / "manifests"), str(workspace), "codex")
    path.chmod(0o666)
    with pytest.raises(sandbox.ManifestError, match="unsafe"):
        sandbox.load_manifest(str(tmp_path / "manifests"), str(workspace), "claude")
    _manifest_file(tmp_path / "manifests", workspace, recorded="/elsewhere")
    with pytest.raises(sandbox.ManifestError, match="does not match"):
        sandbox.load_manifest(str(tmp_path / "manifests"), str(workspace), "claude")


def test_egress_policy_allows_only_public_listed_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    allow = ["api.anthropic.com", "*.githubusercontent.com"]
    assert sandbox.host_allowed("api.anthropic.com", allow)
    assert sandbox.host_allowed("objects.githubusercontent.com", allow)
    assert not sandbox.host_allowed("evil.com", allow)
    assert not sandbox.host_allowed("githubusercontent.com.evil.com", allow)
    for literal in ("127.0.0.1", "::1", "10.0.0.5"):
        with pytest.raises(PermissionError):
            sandbox.resolve_public(literal, 443)
    for address in ("127.0.0.1", "172.24.118.58", "169.254.1.1", "192.168.1.2"):
        monkeypatch.setattr(sandbox.socket, "getaddrinfo",
                            lambda *_args, value=address, **_kw: [(0, 0, 0, "", (value, 443))])
        with pytest.raises(PermissionError):
            sandbox.resolve_public("api.anthropic.com", 443)
    monkeypatch.setattr(sandbox.socket, "getaddrinfo", lambda *_args, **_kw: [(0, 0, 0, "", ("160.79.104.10", 443))])
    assert sandbox.resolve_public("api.anthropic.com", 443) == ("160.79.104.10", 443)
