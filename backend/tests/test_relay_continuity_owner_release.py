import argparse
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "relay_continuity.py"
rc = sys.modules.get("relay_continuity")
if rc is None:
    SPEC = importlib.util.spec_from_file_location("relay_continuity", SCRIPT)
    assert SPEC and SPEC.loader
    rc = importlib.util.module_from_spec(SPEC)
    sys.modules["relay_continuity"] = rc
    SPEC.loader.exec_module(rc)


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


def test_check_and_request_release_update_consults_refused_history_and_retries_next_newer(tmp_path, monkeypatch):
    owner_sha = "c" * 40
    refused_sha = "d" * 40
    newer_sha = "e" * 40
    paths = rc.host_ops.Paths(tmp_path)

    # Record refused_sha in release history
    refused_entry = {
        "candidate_sha": refused_sha,
        "previous_sha": owner_sha,
        "status": "refused",
        "reason": "owner timeout",
        "returned_at": "2026-11-13T10:00:00Z",
        "notified": True,
    }
    paths.state.mkdir(parents=True, exist_ok=True)
    with open(paths.release_history, "a", encoding="utf-8") as f:
        f.write(json.dumps(refused_entry) + "\n")

    current_candidate = [refused_sha]

    def fake_run(args, cwd=None, timeout=20.0):
        if "rev-parse" in args:
            return 0, f"{current_candidate[0]}\n"
        if "merge-base" in args:
            return 0, ""  # ancestor
        return 0, ""

    monkeypatch.setattr(rc.host_ops, "required_checks_problem", lambda sha: None)
    logs = []

    # Attempt 1: origin/master is refused_sha -> must be refused per criterion 7!
    res1 = rc.check_and_request_release_update(
        tmp_path, tmp_path, run=fake_run, current_sha=owner_sha, paths=paths, log=logs.append,
        now=lambda: datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC),
    )
    assert not res1
    assert any("previously refused" in m for m in logs)
    assert len(list(paths.requests.glob("*.json"))) == 0

    # Attempt 2: next newer commit lands on master -> must be requested per criterion 7!
    current_candidate[0] = newer_sha
    logs.clear()
    res2 = rc.check_and_request_release_update(
        tmp_path, tmp_path, run=fake_run, current_sha=owner_sha, paths=paths, log=logs.append,
        now=lambda: datetime(2026, 11, 13, 12, 5, 0, tzinfo=UTC),
    )
    assert res2
    assert any("requested release.install" in m for m in logs)
    requests = list(paths.requests.glob("*.json"))
    assert len(requests) == 1
    req_data = json.loads(requests[0].read_text(encoding="utf-8"))
    assert req_data["sha"] == newer_sha


def test_refused_install_restarts_owner_and_owner_does_not_loop(tmp_path, monkeypatch):
    import copy
    import subprocess
    paths = rc.host_ops.Paths(tmp_path)
    owner_sha = "1" * 40
    candidate_sha = "2" * 40

    policy = copy.deepcopy(rc.host_ops.DEFAULT_POLICY)
    policy["ops"]["release.install"]["trust_master"] = False
    paths.policy.parent.mkdir(parents=True, exist_ok=True)
    paths.policy.write_text(json.dumps(policy))
    paths.config.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(paths.key)], check=True)
    paths.published.mkdir(parents=True, exist_ok=True)
    paths.allowed_signers.write_text(
        f'{rc.host_ops.SIGNER} namespaces="{rc.host_ops.SIG_NAMESPACE}" ' + Path(f"{paths.key}.pub").read_text()
    )

    calls = []
    ex = rc.host_ops.Executor(paths, run=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "ActiveState=active\n", ""),
                              post=lambda t, b: 200, log=lambda m: None)

    # 1. Owner requests release.install
    req = rc.host_ops.make_request("release.install", {"sha": candidate_sha}, candidate_sha, "relay-continuity:owner", ex.now())
    rc.host_ops.submit(paths, req)

    # 2. Executor executes step -> refuses install and restarts owner
    ex.step()
    receipt, _ = rc.host_ops.load_receipt(paths, req["id"])
    assert receipt["outcome"] == "refused"
    assert any("start" in c and "jarvisos-mission-owner.service" in c for c in calls)

    # Release history records refusal
    history = rc.host_ops.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["candidate_sha"] == candidate_sha
    assert history[0]["status"] == "refused"

    # 3. Owner wakes up and runs check_and_request_release_update at boundary
    def fake_run(args, cwd=None, timeout=20.0):
        if "rev-parse" in args:
            return 0, f"{candidate_sha}\n"
        if "merge-base" in args:
            return 0, ""
        return 0, ""

    monkeypatch.setattr(rc.host_ops, "required_checks_problem", lambda sha: None)
    logs = []
    # Spool requests directory should be empty now that executor finished req
    assert len(list(paths.requests.glob("*.json"))) == 0

    res = rc.check_and_request_release_update(
        tmp_path, tmp_path, run=fake_run, current_sha=owner_sha, paths=paths, log=logs.append,
    )
    # Owner does NOT request the same candidate again!
    assert res is False
    assert any("previously refused" in m for m in logs)
    # No new request spooled -> loop is broken!
    assert len(list(paths.requests.glob("*.json"))) == 0


def _setup_git_repos(tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)

    repo = tmp_path / "main_checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "master", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

    (repo / "file.txt").write_text("v1\n")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-u", "origin", "master"], cwd=repo, check=True)

    clone = tmp_path / "other_clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=clone, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=clone, check=True)

    return repo, origin, clone


def test_fast_forward_clean_master(tmp_path):
    repo, origin, clone = _setup_git_repos(tmp_path)

    # Push commit 2 from other clone to origin/master
    (clone / "file.txt").write_text("v2\n")
    subprocess.run(["git", "commit", "-am", "commit 2"], cwd=clone, check=True)
    subprocess.run(["git", "push", "origin", "master"], cwd=clone, check=True)
    target_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, check=True, capture_output=True, text=True).stdout.strip()

    # Also have an untracked file in main repo (untracked files must not block fast-forward)
    (repo / "untracked.txt").write_text("untracked\n")

    logs = []
    tracker = {"sha": None}
    res = rc.fast_forward_main_checkout(repo, log=logs.append, last_logged=tracker)
    assert res is True
    assert (repo / "file.txt").read_text() == "v2\n"
    assert (repo / "untracked.txt").read_text() == "untracked\n"
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    assert head_sha == target_sha
    assert any("fast-forwarded main checkout to origin/master" in m for m in logs)


def test_fast_forward_skipped_on_dirty_tracked_file(tmp_path):
    repo, origin, clone = _setup_git_repos(tmp_path)

    # Push commit 2 to origin/master
    (clone / "file.txt").write_text("v2\n")
    subprocess.run(["git", "commit", "-am", "commit 2"], cwd=clone, check=True)
    subprocess.run(["git", "push", "origin", "master"], cwd=clone, check=True)
    target_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, check=True, capture_output=True, text=True).stdout.strip()

    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    # Make tracked file dirty in repo
    (repo / "file.txt").write_text("dirty\n")
    (repo / "untracked.txt").write_text("untracked\n")

    logs = []
    tracker = {"sha": None}
    res = rc.fast_forward_main_checkout(repo, log=logs.append, last_logged=tracker)
    assert res is False
    assert (repo / "file.txt").read_text() == "dirty\n"
    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    assert head_after == head_before
    assert tracker["sha"] == target_sha
    assert any("checkout is dirty" in m for m in logs)

    # Call a second time: must NOT log again for the same origin/master sha
    logs_second = []
    res2 = rc.fast_forward_main_checkout(repo, log=logs_second.append, last_logged=tracker)
    assert res2 is False
    assert logs_second == []


def test_fast_forward_skipped_when_tree_becomes_dirty_between_checks(tmp_path):
    repo, origin, clone = _setup_git_repos(tmp_path)

    # Push commit 2 to origin/master
    (clone / "file.txt").write_text("v2\n")
    subprocess.run(["git", "commit", "-am", "commit 2"], cwd=clone, check=True)
    subprocess.run(["git", "push", "origin", "master"], cwd=clone, check=True)
    target_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, check=True, capture_output=True, text=True).stdout.strip()

    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    status_calls = []
    merge_called = []
    real_run = rc.devctx.run

    def wrapped_run(argv, **kw):
        if argv == ["git", "status", "--porcelain", "--untracked-files=no"]:
            status_calls.append(len(status_calls) + 1)
        if "merge" in argv:
            merge_called.append(list(argv))
        res = real_run(argv, **kw)
        if argv == ["git", "rev-parse", "HEAD"]:
            # Dirty the tracked file after initial status check passed
            (repo / "file.txt").write_text("dirty-between-checks\n")
        return res

    logs = []
    tracker = {"sha": None}
    res = rc.fast_forward_main_checkout(repo, run=wrapped_run, log=logs.append, last_logged=tracker)
    assert res is False
    assert len(status_calls) == 2
    assert not merge_called
    assert (repo / "file.txt").read_text() == "dirty-between-checks\n"
    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    assert head_after == head_before
    assert tracker["sha"] == target_sha
    assert any("checkout is dirty" in m for m in logs)



def test_fast_forward_skipped_on_another_branch(tmp_path):
    repo, origin, clone = _setup_git_repos(tmp_path)

    # Push commit 2 to origin/master
    (clone / "file.txt").write_text("v2\n")
    subprocess.run(["git", "commit", "-am", "commit 2"], cwd=clone, check=True)
    subprocess.run(["git", "push", "origin", "master"], cwd=clone, check=True)

    # Checkout a feature branch in repo
    subprocess.run(["git", "checkout", "-q", "-b", "feature-x"], cwd=repo, check=True)
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    logs = []
    tracker = {"sha": None}
    res = rc.fast_forward_main_checkout(repo, log=logs.append, last_logged=tracker)
    assert res is False
    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    assert head_after == head_before
    assert any("on branch 'feature-x', not 'master'" in m for m in logs)

    # Call again: logs once per sha
    logs_second = []
    rc.fast_forward_main_checkout(repo, log=logs_second.append, last_logged=tracker)
    assert logs_second == []


def test_fast_forward_skipped_on_diverged_history(tmp_path):
    repo, origin, clone = _setup_git_repos(tmp_path)

    # Push commit 2 to origin/master from clone
    (clone / "file.txt").write_text("from clone\n")
    subprocess.run(["git", "commit", "-am", "clone commit"], cwd=clone, check=True)
    subprocess.run(["git", "push", "origin", "master"], cwd=clone, check=True)

    # Commit local change to master in repo without pulling clone commit
    (repo / "local.txt").write_text("local\n")
    subprocess.run(["git", "add", "local.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "local commit"], cwd=repo, check=True)
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    logs = []
    tracker = {"sha": None}
    res = rc.fast_forward_main_checkout(repo, log=logs.append, last_logged=tracker)
    assert res is False
    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    assert head_after == head_before
    assert (repo / "local.txt").read_text() == "local\n"
    assert any("merge --ff-only failed" in m for m in logs)

    # Call again: logs once per sha
    logs_second = []
    rc.fast_forward_main_checkout(repo, log=logs_second.append, last_logged=tracker)
    assert logs_second == []


def test_command_serve_does_not_call_fast_forward_when_release_requested(tmp_path, monkeypatch):
    sha = "e" * 40
    monkeypatch.setenv("JARVISOS_RELEASE_SHA", sha)
    monkeypatch.setenv("RELAY_CONTINUITY_DIR", str(tmp_path / "continuity"))
    monkeypatch.setattr(rc, "in_sandbox", lambda: True)
    monkeypatch.setattr(rc, "become_subreaper", lambda: True)
    monkeypatch.setattr(rc.shutil, "which", lambda cmd: "/bin/claude" if cmd == "claude" else None)
    monkeypatch.setattr(rc, "link_relay_claude_credentials", lambda *a: None)
    monkeypatch.setattr(rc, "sandbox_provider_env", lambda *a: {"CLAUDE_CONFIG_DIR": str(tmp_path)})

    monkeypatch.setattr(rc, "check_and_request_release_update", lambda *a, **kw: True)

    ff_called = False

    def spy_ff(*a, **kw):
        nonlocal ff_called
        ff_called = True
        return True

    monkeypatch.setattr(rc, "fast_forward_main_checkout", spy_ff)

    args = argparse.Namespace(
        action="serve",
        repo=str(tmp_path / "repo"),
        clear_stop=True,
        no_notify=True,
        interval=1,
        turns=1,
        once=True,
    )
    res = rc.command_serve(args)
    assert res == 0
    assert not ff_called

