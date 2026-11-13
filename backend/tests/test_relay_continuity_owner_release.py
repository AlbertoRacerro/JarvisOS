import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from scripts import relay_continuity as rc


def test_owner_release_sha(tmp_path, monkeypatch):
    sha = "a" * 40
    monkeypatch.setenv("JARVISOS_RELEASE_SHA", sha)
    assert rc.release_sha() == sha

    monkeypatch.delenv("JARVISOS_RELEASE_SHA", raising=False)
    # When __file__ is in a repository root or scripts dir (not releases/<sha>)
    # release_sha() returns None
    assert rc.release_sha() is None or rc.release_sha() == Path(rc.__file__).resolve().parent.parent.name


def test_owner_reports_release_sha_in_serve_state(tmp_path, monkeypatch):
    sha = "b" * 40
    monkeypatch.setenv("JARVISOS_RELEASE_SHA", sha)
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(tmp_path / "continuity"))
    monkeypatch.setattr(rc, "in_sandbox", lambda: True)
    monkeypatch.setattr(rc, "become_subreaper", lambda: True)
    monkeypatch.setattr(rc.shutil, "which", lambda cmd: "/bin/claude" if cmd == "claude" else None)
    monkeypatch.setattr(rc, "link_relay_claude_credentials", lambda *a: None)
    monkeypatch.setattr(rc, "sandbox_provider_env", lambda *a: {"CLAUDE_CONFIG_DIR": str(tmp_path)})

    # Mock serve_step to exit after one step
    monkeypatch.setattr(rc, "serve_step", lambda *a, **kw: "empty")
    monkeypatch.setattr(rc, "roadmap_step", lambda *a, **kw: None)

    args = argparse.Namespace(clear_stop=True, no_notify=True, turns=1, interval=1, once=True)
    res = rc.command_serve(args)
    assert res == 0

    state_file = tmp_path / "continuity" / "serve" / "state.json"
    assert state_file.is_file()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state.get("release_sha") == sha
    assert "release_updated_at" in state


def test_check_and_request_release_update_when_no_owner_sha(tmp_path):
    assert not rc.check_and_request_release_update(tmp_path, tmp_path, current_sha=None)


def test_check_and_request_release_update_when_same_sha(tmp_path, monkeypatch):
    sha = "c" * 40
    def fake_run(args, cwd=None, timeout=20.0):
        if "rev-parse" in args:
            return 0, f"{sha}\n"
        return 0, ""

    assert not rc.check_and_request_release_update(tmp_path, tmp_path, run=fake_run, current_sha=sha)


def test_check_and_request_release_update_when_candidate_not_newer(tmp_path, monkeypatch):
    owner_sha = "c" * 40
    candidate_sha = "d" * 40

    def fake_run(args, cwd=None, timeout=20.0):
        if "rev-parse" in args:
            return 0, f"{candidate_sha}\n"
        if "merge-base" in args:
            return 1, ""  # not ancestor
        return 0, ""

    monkeypatch.setattr(rc.host_ops, "_gh", lambda *a, **kw: "behind")
    assert not rc.check_and_request_release_update(tmp_path, tmp_path, run=fake_run, current_sha=owner_sha)


def test_check_and_request_release_update_when_checks_not_green(tmp_path, monkeypatch):
    owner_sha = "c" * 40
    candidate_sha = "d" * 40

    def fake_run(args, cwd=None, timeout=20.0):
        if "rev-parse" in args:
            return 0, f"{candidate_sha}\n"
        if "merge-base" in args:
            return 0, ""  # ancestor
        return 0, ""

    monkeypatch.setattr(rc.host_ops, "required_checks_problem", lambda sha: "check 'backend' is pending")
    logs = []
    res = rc.check_and_request_release_update(
        tmp_path, tmp_path, run=fake_run, current_sha=owner_sha, log=logs.append
    )
    assert not res
    assert any("checks not green" in m for m in logs)


def test_check_and_request_release_update_submits_request_when_newer_and_green(tmp_path, monkeypatch):
    owner_sha = "c" * 40
    candidate_sha = "d" * 40
    paths = rc.host_ops.Paths(tmp_path)

    def fake_run(args, cwd=None, timeout=20.0):
        if "rev-parse" in args:
            return 0, f"{candidate_sha}\n"
        if "merge-base" in args:
            return 0, ""  # ancestor
        return 0, ""

    monkeypatch.setattr(rc.host_ops, "required_checks_problem", lambda sha: None)
    logs = []
    res = rc.check_and_request_release_update(
        tmp_path, tmp_path, run=fake_run, current_sha=owner_sha, paths=paths, log=logs.append,
        now=lambda: datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC),
    )
    assert res
    assert any("requested release.install" in m for m in logs)

    # Verify request was written to spool
    requests = list(paths.requests.glob("*.json"))
    assert len(requests) == 1
    req_data = json.loads(requests[0].read_text(encoding="utf-8"))
    assert req_data["op"] == "release.install"
    assert req_data["args"]["sha"] == candidate_sha
    assert req_data["sha"] == candidate_sha
    assert req_data["requester"] == "relay-continuity:owner"


def test_command_serve_exits_at_task_boundary_without_taking_task(tmp_path, monkeypatch):
    sha = "e" * 40
    monkeypatch.setenv("JARVISOS_RELEASE_SHA", sha)
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(tmp_path / "continuity"))
    monkeypatch.setattr(rc, "in_sandbox", lambda: True)
    monkeypatch.setattr(rc, "become_subreaper", lambda: True)
    monkeypatch.setattr(rc.shutil, "which", lambda cmd: "/bin/claude" if cmd == "claude" else None)
    monkeypatch.setattr(rc, "link_relay_claude_credentials", lambda *a: None)
    monkeypatch.setattr(rc, "sandbox_provider_env", lambda *a: {"CLAUDE_CONFIG_DIR": str(tmp_path)})

    # Mock check_and_request_release_update to return True (simulating successful install request at task boundary)
    monkeypatch.setattr(rc, "check_and_request_release_update", lambda *a, **kw: True)

    serve_step_called = False
    def fake_serve_step(*a, **kw):
        nonlocal serve_step_called
        serve_step_called = True
        return "empty"

    monkeypatch.setattr(rc, "serve_step", fake_serve_step)

    args = argparse.Namespace(clear_stop=True, no_notify=True, turns=1, interval=1, once=False)
    res = rc.command_serve(args)

    assert res == 0
    # serve_step was NOT called: exited 0 at task boundary without taking any task
    assert not serve_step_called


def test_owner_does_not_request_install_during_running_turn(tmp_path, monkeypatch):
    check_called_during_turn = False
    turn_active = False

    def check_hook(*a, **kw):
        nonlocal check_called_during_turn
        if turn_active:
            check_called_during_turn = True
        return False

    monkeypatch.setattr(rc, "check_and_request_release_update", check_hook)
    monkeypatch.setattr(rc, "in_sandbox", lambda: True)
    monkeypatch.setattr(rc, "become_subreaper", lambda: True)
    monkeypatch.setattr(rc.shutil, "which", lambda cmd: "/bin/claude" if cmd == "claude" else None)
    monkeypatch.setattr(rc, "link_relay_claude_credentials", lambda *a: None)
    monkeypatch.setattr(rc, "sandbox_provider_env", lambda *a: {"CLAUDE_CONFIG_DIR": str(tmp_path)})

    def fake_serve_step(*a, **kw):
        nonlocal turn_active
        turn_active = True
        turn_active = False
        return "ran:m-1:idle"

    monkeypatch.setattr(rc, "serve_step", fake_serve_step)

    args = argparse.Namespace(clear_stop=True, no_notify=True, turns=1, interval=1, once=True)
    res = rc.command_serve(args)
    assert res == 0
    assert not check_called_during_turn
