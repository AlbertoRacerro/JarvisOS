"""Spec 207: offline host boundary, real Ed25519 receipts, no host mutations."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "host_ops.py"
spec = importlib.util.spec_from_file_location("host_ops", SCRIPT)
assert spec and spec.loader
h = sys.modules.get("host_ops") or importlib.util.module_from_spec(spec)
if "host_ops" not in sys.modules:
    sys.modules["host_ops"] = h
    spec.loader.exec_module(h)
SHA = "a" * 40


def key(paths):
    paths.config.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(paths.key)], check=True)
    paths.published.mkdir(parents=True)
    paths.allowed_signers.write_text(f'{h.SIGNER} namespaces="{h.SIG_NAMESPACE}" ' + Path(f"{paths.key}.pub").read_text())


@pytest.fixture
def host(tmp_path):
    paths = h.Paths(tmp_path)
    key(paths)
    paths.policy.write_text(json.dumps(h.DEFAULT_POLICY))
    paths.policy.chmod(0o600)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "ActiveState=active\n", "")

    executor = h.Executor(paths, run=run, post=lambda text, _: 200, log=lambda _: None)
    return paths, executor, calls


def request(host, op="service.status", args=None, **changes):
    paths, ex, _ = host
    req = h.make_request(op, {"unit": "jarvisos-mission-owner.service"} if args is None else args, None, "test", ex.now())
    req.update(changes)
    h.submit(paths, req)
    ex.step()
    receipt, why = h.load_receipt(paths, req["id"])
    assert receipt, why
    return req, receipt


@pytest.mark.parametrize("change", [
    {"schema": "bad"}, {"extra": 1}, {"id": "bad"}, {"sha": "bad"}, {"args": []},
    {"op": "shell"}, {"requested_at": "invalid"}, {"requester": 1},
])
def test_request_validation(change):
    req = h.make_request("service.status", {}, None, "test", h.now_utc())
    req.update(change)
    assert h.request_problem(req, h.now_utc())


@pytest.mark.parametrize("offset", [-3601, 301])
def test_request_time(offset):
    req = h.make_request("wsl.probe", {}, SHA, "test", h.now_utc() + timedelta(seconds=offset))
    assert h.request_problem(req, h.now_utc())


@pytest.mark.parametrize("malformed", ["size", "symlink", "json", "id"])
def test_spool_refusals(host, malformed):
    paths, ex, _ = host
    req = h.make_request("wsl.probe", {}, None, "test", ex.now())
    rid = h.submit(paths, req)
    file = paths.requests / f"{rid}.json"
    if malformed == "symlink":
        file.unlink()
        file.symlink_to(paths.policy)
    elif malformed == "size":
        file.write_bytes(b"x" * (h.MAX_REQUEST_BYTES + 1))
    elif malformed == "json":
        file.write_text("{")
    else:
        req["id"] = "b" * 32
        file.write_bytes(h.canonical(req))
    ex.step()
    assert h.load_receipt(paths, rid)[0]["outcome"] == "refused"
    assert not file.exists()


def test_replay_republish_and_recovery(host):
    paths, ex, calls = host
    req, receipt = request(host)
    count = len(calls)
    (paths.receipts / f"{req['id']}.json").unlink()
    h.submit(paths, req)
    ex.step()
    assert len(calls) == count
    assert h.load_receipt(paths, req["id"])[0] == receipt
    other = h.make_request("service.status", req["args"], None, "test", ex.now())
    fd = ex._state_fd("inflight")
    try:
        h.write_at(fd, f"{other['id']}.json", h.canonical(other))
    finally:
        os.close(fd)
    ex.recover()
    h.submit(paths, other)
    ex.step()
    assert len(calls) == count
    assert h.load_receipt(paths, other["id"])[0]["outcome"] == "interrupted"


def test_cancel_before_start(host):
    paths, ex, calls = host
    req = h.make_request("wsl.probe", {}, None, "test", ex.now())
    rid = h.submit(paths, req)
    assert h.main(["--home", str(paths.home), "cancel", rid]) == 0
    ex.step()
    assert h.load_receipt(paths, rid)[0]["outcome"] == "cancelled"
    assert not calls


@pytest.mark.parametrize("op", sorted(h.OPERATIONS))
def test_unknown_arguments_and_absent_policy_refused(host, op):
    _, receipt = request(host, op, {"invented": 1})
    assert receipt["outcome"] == "refused"
    paths, _, _ = host
    paths.policy.write_text(json.dumps({"schema": h.POLICY_SCHEMA, "ops": {}}))
    _, receipt = request(host, op, {})
    assert receipt["outcome"] == "refused"


@pytest.mark.parametrize("op,args", [
    ("service.status", {"unit": "evil.service"}),
    ("service.control", {"unit": "evil.service", "action": "start"}),
    ("service.control", {"unit": "jarvisos-mission-owner.service", "action": "kill"}),
    ("service.control", {"unit": "jarvisos-mission-owner.service", "action": "stop"}),
    ("service.control", {"unit": "jarvisos-mission-owner.service", "action": "restart"}),
    ("service.install", {"template": "evil"}),
    ("wsl.handoff", {"file": "bad.exe"}),
    ("wsl.handoff", {"file": "../bad.png"}),
    ("wsl.handoff", {"file": "bad.png", "sha256": "bad"}),
    ("browser.screenshot", {"url": "https://example.com"}),
    ("browser.screenshot", {"url": "http://127.0.0.1:8000", "viewport": "evil"}),
    ("http.smoke", {"target": "evil"}),
    ("bench.run", {"bench": "evil", "worktree": "x"}),
    ("bench.run", {"bench": "relay-observe-tests", "worktree": "../x"}),
    ("bench.run", {"bench": "relay-observe-tests", "worktree": "x"}),
    ("release.install", {"sha": SHA}),
])
def test_operation_refusals(host, op, args):
    assert request(host, op, args)[1]["outcome"] == "refused"


@pytest.mark.parametrize("mode", ["unknown", "writable"])
def test_policy_fail_closed(host, mode):
    paths, ex, calls = host
    if mode == "unknown":
        paths.policy.write_text(json.dumps({"schema": h.POLICY_SCHEMA, "ops": {"shell": {}}}))
    else:
        paths.policy.chmod(0o666)
    assert request(host)[1]["outcome"] == "refused"
    assert not calls
    assert json.loads(paths.heartbeat.read_text())["problem"]
    with pytest.raises(RuntimeError):
        h.load_policy(paths)


@pytest.mark.parametrize("attack", ["edit", "rename", "key"])
def test_receipt_forgery(host, tmp_path, attack):
    paths, _, _ = host
    req, _ = request(host)
    rid = req["id"]
    file = paths.receipts / f"{rid}.json"
    if attack == "edit":
        file.write_text(file.read_text().replace('"outcome":"ok"', '"outcome":"failed"'))
    elif attack == "rename":
        other = "f" * 32
        shutil.copyfile(file, paths.receipts / f"{other}.json")
        shutil.copyfile(str(file) + ".sig", paths.receipts / f"{other}.json.sig")
        rid = other
    else:
        other = h.Paths(tmp_path / "other")
        key(other)
        Path(str(file) + ".sig").write_bytes(h.sign(other.key, file.read_bytes()))
    assert h.load_receipt(paths, rid)[0] is None
    assert h.main(["--home", str(paths.home), "verify", rid]) == 1


@pytest.mark.parametrize("text", ["<!channel>", "https://example.com", "x\x00", "sk-" + "x" * 30, "<https://example.com>"])
def test_slack_filter(text):
    assert h.message_problem(text)


def test_slack_allowed_and_rate_limit(host):
    assert h.message_problem(f"Fix <{h.REPO_URL}/pull/123|PR>\nPronto") is None
    paths, _, _ = host
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["slack.deliver"]["max_per_hour"] = 1
    paths.policy.write_text(json.dumps(policy))
    assert request(host, "slack.deliver", {"text": "Pronto"})[1]["outcome"] == "ok"
    assert request(host, "slack.deliver", {"text": "Pronto"})[1]["outcome"] == "refused"


@pytest.mark.parametrize("dirty,head", [(" M x\0", SHA), ("?? x\0", SHA), ("", "b" * 40)])
def test_bench_clean_head_and_plain_launcher(host, dirty, head):
    paths, ex, _ = host
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, head if "rev-parse" in argv else
                                           dirty if "status" in argv else "H x\n", "")

    ex.run = run
    assert request(host, "bench.run", {"bench": "relay-observe-tests", "worktree": "x"}, sha=SHA)[1]["outcome"] == "refused"
    root = paths.home / "src/worktrees/x"
    launcher = str(paths.home / ".local/bin/jarvis-sandbox")
    git = [launcher, "git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-C", str(root)]
    assert [a for a, _ in calls] == [
        [*git, "rev-parse", "HEAD"],
        [*git, "status", "--porcelain", "-z", "--ignored", "--untracked-files=all"],
        [*git, "ls-files", "-v"],
    ]
    assert all(k["cwd"] == str(root) for _, k in calls)


def test_bench_clean_check_real_linked_worktree(host, monkeypatch):
    paths, ex, _ = host
    repo = paths.home / "repo"
    root = paths.home / "src/worktrees/x"
    root.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "tracked.txt").write_text("original\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "initial"], check=True)
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(root)], check=True)
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["bench.run"]["launcher"] = "/usr/bin/env"
    paths.policy.write_text(json.dumps(policy))
    ex.run = lambda argv, **kw: subprocess.run(argv, capture_output=True, text=True, **kw)
    monkeypatch.setattr(h, "_run_benchmark", lambda *_args, **_kw: {"accepted": True})

    def outcome():
        return request(host, "bench.run", {"bench": "relay-observe-tests", "worktree": "x"}, sha=sha)[1]["outcome"]

    assert outcome() == "ok"
    subprocess.run(["git", "-C", str(root), "update-index", "--assume-unchanged", "tracked.txt"], check=True)
    (root / "tracked.txt").write_text("edited\n")
    assert outcome() == "refused"
    subprocess.run(["git", "-C", str(root), "update-index", "--no-assume-unchanged", "tracked.txt"], check=True)
    assert outcome() == "refused"
    (root / "tracked.txt").write_text("original\n")
    subprocess.run(["git", "-C", str(root), "update-index", "--skip-worktree", "tracked.txt"], check=True)
    assert outcome() == "refused"
    subprocess.run(["git", "-C", str(root), "update-index", "--no-skip-worktree", "tracked.txt"], check=True)
    assert outcome() == "ok"

    common_dir = Path(subprocess.check_output(["git", "-C", str(root), "rev-parse", "--git-common-dir"],
                                              text=True).strip())
    (common_dir / "info/exclude").write_text(
        "conftest.py\n__pycache__/\n.pytest_cache/\n.venv/\n.ruff_cache/\nnode_modules\n.env\n"
    )
    tests = root / "backend/tests"
    tests.mkdir(parents=True)
    conftest = tests / "conftest.py"
    conftest.write_text("raise RuntimeError('unexpected execution')\n")
    assert outcome() == "refused"
    conftest.unlink()

    subprocess.run(["git", "-C", str(repo), "config", "status.showUntrackedFiles", "no"], check=True)
    untracked = tests / "unexpected.txt"
    untracked.write_text("unexpected\n")
    assert outcome() == "refused"
    untracked.unlink()

    pycache = tests / "__pycache__"
    pycache.mkdir()
    (pycache / "a b.pyc").write_bytes(b"cache")
    assert outcome() == "ok"
    (pycache / "unexpected.py").write_text("unexpected\n")
    assert outcome() == "refused"
    (pycache / "unexpected.py").unlink()
    pytest_cache = tests / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / "README.md").write_text("cache\n")
    assert outcome() == "ok"
    venv_python = root / "backend/.venv/bin/python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("python\n")
    ruff_cache = root / "backend/.ruff_cache/x"
    ruff_cache.parent.mkdir(parents=True)
    ruff_cache.write_text("cache\n")
    assert outcome() == "ok"
    shared_modules = paths.home / "shared_modules"
    shared_modules.mkdir()
    (root / "frontend").mkdir()
    (root / "frontend/node_modules").symlink_to(shared_modules, target_is_directory=True)
    assert outcome() == "ok"
    env_file = root / "backend/.env"
    env_file.write_text("ignored\n")
    assert outcome() == "refused"


def local_eval_policy(paths, *, base_url="http://127.0.0.1:18081/v1", credential=True):
    (paths.home / "src/worktrees").mkdir(parents=True, exist_ok=True)
    key_file = paths.config / "local-model-key"
    if credential:
        key_file.write_text("test-local-model-credential\n")
        key_file.chmod(0o600)
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["bench.run"]["benchmarks"]["local-eval"] = {
        "cwd": "backend", "argv": ["python", "pointing_eval.py", "--ids", "new",
                                   "--emit-prompts", "prompts.json"], "result_file": "prompts.json",
    }
    policy["ops"]["local_eval.run"] = {"evaluations": {"local-eval": {
        "credential_file": str(key_file), "base_url": base_url, "model": "test-model",
        "ids": "new", "label": "after", "max_tokens": 220, "timeout_s": 30,
    }}}
    paths.policy.write_text(json.dumps(policy))
    return key_file


def fake_prompt_phase(host, monkeypatch, prompts, change=None):
    _, ex, _ = host
    monkeypatch.setattr(h, "_gh", lambda args, **kw: "behind" if "compare/" in args[0] else "[]")
    def checkout(sha, root, remaining):
        (root / "backend").mkdir()
        data = b"# evaluator\n"
        (root / "backend/pointing_eval.py").write_bytes(data)
        return {Path("backend/pointing_eval.py"): (len(data), h.hashlib.sha256(data).hexdigest())}, {Path("backend")}
    monkeypatch.setattr(h, "_eval_checkout", checkout)
    seen = {}

    class Child:
        returncode = 0

        def __init__(self, argv, **kw):
            seen["argv"] = argv
            seen["env"] = kw["env"]
            seen["cwd"] = kw["cwd"]
            kw["stdout"].write(b"test-local-model-credential")
            (Path(kw["cwd"]) / "prompts.json").write_text(json.dumps(prompts))
            if change == "modified":
                (Path(kw["cwd"]) / "pointing_eval.py").write_text("changed\n")
            elif change == "new":
                (Path(kw["cwd"]) / "extra.py").write_text("new\n")
            elif change == "cache":
                cache = Path(kw["cwd"]) / "__pycache__"
                cache.mkdir()
                (cache / "x.pyc").write_bytes(b"cache")

        def poll(self):
            return self.returncode

    real_popen = subprocess.Popen
    real_run = subprocess.run
    monkeypatch.setattr(h.subprocess, "Popen", lambda argv, **kw: Child(argv, **kw)
                        if argv[0] == "systemd-run" else real_popen(argv, **kw))
    def run(argv, **kw):
        if argv[:3] == ["systemctl", "--user", "stop"]:
            seen["stopped"] = True
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv[:3] == ["systemctl", "--user", "show"]:
            assert seen.get("stopped")
            return subprocess.CompletedProcess(argv, 0, "ActiveState=inactive\nLoadState=not-found\n", "")
        return real_run(argv, **kw)
    monkeypatch.setattr(h.subprocess, "run", run)
    return seen


def test_local_eval_token_stays_on_host_and_receipt_is_scores_only(host, monkeypatch):
    paths, _, _ = host
    local_eval_policy(paths)
    prompts = [{"name": "case_one", "prompt": "test prompt", "correct": ["good"], "plausible": ["maybe"]}]
    seen = fake_prompt_phase(host, monkeypatch, prompts)
    requests = []

    class Response:
        status = 200
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=lambda _: None)))
        chunks = [json.dumps({"choices": [{"message": {"content": "[[point:good]] secret echo"}}]}).encode()]
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read1(self, _):
            return self.chunks.pop(0) if self.chunks else b""

    class Opener:
        def open(self, req, timeout):
            requests.append((req, timeout))
            return Response()

    monkeypatch.setattr(h.urllib.request, "build_opener", lambda *args: Opener())
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval"}, sha=SHA)
    assert receipt["outcome"] == "ok"
    assert receipt["detail"] == {"model": "test-model", "base_url": "http://127.0.0.1:18081/v1",
                                 "head": SHA, "ids": "after", "tally": {"correct": 1, "plausible": 0,
                                 "wrong": 0, "none": 0}, "rows": [{"name": "case_one", "first": "good",
                                                                 "verdict": "correct", "seconds": 0.0}]}
    assert "test-local-model-credential" not in json.dumps(receipt)
    assert "test-local-model-credential" not in str(seen)
    assert Path(seen["cwd"]).parent.parent == paths.home / "src/worktrees"
    launcher = str(paths.home / ".local/bin/jarvis-sandbox")
    index = seen["argv"].index(launcher)
    assert seen["argv"][index:index + 4] == [launcher, "env", "-C", str(seen["cwd"])]
    assert seen["argv"][index + 4] == "PYTHONDONTWRITEBYTECODE=1"
    assert not Path(seen["cwd"]).exists()
    assert seen["stopped"]
    assert requests[0][0].get_header("Authorization") == "Bearer test-local-model-credential"
    assert 0 < requests[0][1] <= 30
    body = json.loads(requests[0][0].data)
    assert body["temperature"] == 0 and body["max_tokens"] == 220
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("change,expected", [("modified", "refused"), ("new", "refused"),
                                              ("cache", "refused")])
def test_local_eval_checkout_integrity_after_prompt_run(host, monkeypatch, change, expected):
    paths, _, _ = host
    local_eval_policy(paths)
    seen = fake_prompt_phase(host, monkeypatch, [], change=change)
    monkeypatch.setattr(h, "_eval_prompts", lambda _: [])
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval"}, sha=SHA)
    assert receipt["outcome"] == expected
    assert not Path(seen["cwd"]).exists()


@pytest.mark.parametrize("url", ["http://example.com/v1", "https://127.0.0.1:18081/v1",
                                  "http://127.0.0.2:18081/v1", "http://localhost:18081/v1",
                                  "http://localhost.evil:18081/v1"])
def test_local_eval_refuses_non_loopback(host, url):
    paths, _, _ = host
    local_eval_policy(paths, base_url=url)
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval"}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert "loopback" in receipt["error"]


@pytest.mark.parametrize("mode", ["missing", "insecure", "empty"])
def test_local_eval_refuses_bad_credential(host, monkeypatch, mode):
    paths, _, _ = host
    credential = local_eval_policy(paths, credential=mode != "missing")
    if mode == "insecure":
        credential.chmod(0o644)
    elif mode == "empty":
        credential.write_text("")
    fake_prompt_phase(host, monkeypatch, [{"name": "case_one", "prompt": "prompt", "correct": ["a"],
                                      "plausible": ["b"]}])
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval"}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert "credential" in receipt["error"]


def test_local_eval_never_reads_credential_while_prompt_scope_survives(host, monkeypatch):
    paths, _, _ = host
    local_eval_policy(paths)
    fake_prompt_phase(host, monkeypatch, [{"name": "case", "prompt": "p", "correct": ["a"], "plausible": []}])
    real_run = h.subprocess.run
    def still_active(argv, **kw):
        if argv[:3] == ["systemctl", "--user", "show"]:
            return subprocess.CompletedProcess(argv, 0, "ActiveState=active\nLoadState=loaded\n", "")
        return real_run(argv, **kw)
    monkeypatch.setattr(h.subprocess, "run", still_active)
    monkeypatch.setattr(h, "_eval_credential", lambda *_: pytest.fail("credential read before scope stopped"))
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval"}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert "prompt phase" in receipt["error"]


def test_local_eval_cancel_stops_scope_before_credential(host, monkeypatch):
    paths, _, _ = host
    local_eval_policy(paths)
    seen = fake_prompt_phase(host, monkeypatch, [{"name": "case", "prompt": "p", "correct": ["a"], "plausible": []}])
    real_popen = h.subprocess.Popen
    class RunningChild:
        returncode = None
        def poll(self):
            return self.returncode
        def kill(self):
            self.returncode = -9
        def wait(self, **_):
            return self.returncode
    monkeypatch.setattr(h.subprocess, "Popen", lambda argv, **kw: RunningChild()
                        if argv[0] == "systemd-run" else real_popen(argv, **kw))
    ctx = h.Context(paths, h.load_policy(paths)[0], {"id": "a" * 32}, lambda *_a, **_k: None,
                    lambda *_a: 200, h.now_utc, lambda: True, [])
    root = paths.home / "checkout"
    (root / "backend").mkdir(parents=True)
    monkeypatch.setattr(h, "_eval_credential", lambda *_: pytest.fail("credential read after cancel"))
    with pytest.raises(h.Cancelled):
        h._run_benchmark(ctx, "local-eval", ctx.conf("bench.run")["benchmarks"]["local-eval"],
                         root, "backend", None, SHA, None, close_scope=True)
    assert seen["stopped"]


@pytest.mark.parametrize("prompts", [[], [{"name": "x", "prompt": "p" * 16001, "correct": ["a"],
                                              "plausible": ["b"]}], [{"name": "x", "prompt": "p",
                                              "correct": ["a"], "plausible": ["a"]}],
                                     [{"name": "x", "prompt": "p", "correct": ["a"],
                                       "plausible": ["b"]}] * 51])
def test_local_eval_refuses_malformed_or_oversized_prompts(host, monkeypatch, prompts):
    paths, _, _ = host
    local_eval_policy(paths)
    fake_prompt_phase(host, monkeypatch, prompts)
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval"}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert receipt["detail"] == {}


def test_local_eval_http_error_does_not_expose_token(host, monkeypatch):
    paths, _, _ = host
    local_eval_policy(paths)
    fake_prompt_phase(host, monkeypatch, [{"name": "case_one", "prompt": "prompt", "correct": ["a"],
                                      "plausible": ["b"]}])

    class Opener:
        def open(self, req, timeout):
            raise h.urllib.error.URLError("test-local-model-credential")

    monkeypatch.setattr(h.urllib.request, "build_opener", lambda *args: Opener())
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert "test-local-model-credential" not in json.dumps(receipt)


def test_local_eval_http_status_reports_code_only(host, monkeypatch):
    paths, _, _ = host
    local_eval_policy(paths)
    fake_prompt_phase(host, monkeypatch, [{"name": "case_one", "prompt": "prompt", "correct": ["a"],
                                      "plausible": []}])

    class Opener:
        def open(self, req, timeout):
            raise h.urllib.error.HTTPError(req.full_url, 401, "test-local-model-credential", {}, None)

    monkeypatch.setattr(h.urllib.request, "build_opener", lambda *args: Opener())
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert receipt["error"] == "local model returned HTTP 401"
    assert "test-local-model-credential" not in json.dumps(receipt)


def test_local_eval_unregistered_evaluator_refused(host):
    paths, _, _ = host
    local_eval_policy(paths)
    _, receipt = request(host, "local_eval.run", {"bench": "other"}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert "not registered" in receipt["error"]


def test_local_eval_rejects_worktree_argument(host):
    paths, _, _ = host
    local_eval_policy(paths)
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval", "worktree": "x"}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert "unknown arguments" in receipt["error"]


def test_local_eval_rejects_credential_outside_host_config(host):
    paths, _, _ = host
    local_eval_policy(paths)
    with pytest.raises(h.Refused, match="credential path"):
        h._eval_credential(str(paths.spool / "key"), paths)


def test_local_eval_score_matches_evaluator():
    eval_path = SCRIPT.parents[1] / "docs/implementation/evidence/bubu3-pointing/pointing_eval.py"
    source = eval_path.read_text()
    import ast
    scope = {"re": h.re}
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DIRECTIVE" for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(eval_path), "exec"), scope)
        if isinstance(node, ast.FunctionDef) and node.name == "score":
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(eval_path), "exec"), scope)
    for reply in ["hello", "[[point:a]]", "[[point:b|reason]]", "[[point:z]] [[point:a]]"]:
        assert h._eval_score(reply, ["a"], ["b"]) == scope["score"](reply, ["a"], ["b"])


def test_bench_request_cannot_supply_secret_env_and_unregistered_refused(host):
    _, receipt = request(host, "bench.run", {"bench": "relay-observe-tests", "worktree": "x",
                                             "secret_env": {"JARVISOS_LLAMACPP_API_KEY": "injected"}}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert "unknown arguments" in receipt["error"]
    _, receipt = request(host, "bench.run", {"bench": "not-registered", "worktree": "x"}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert "not registered" in receipt["error"]


def test_bench_unreachable_commit_refused(host, monkeypatch):
    paths, ex, _ = host
    local_eval_policy(paths)
    # A forged sandbox origin/master is irrelevant; only host GitHub answers count.
    sandbox_calls = []
    def forged_git(argv, **kw):
        sandbox_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, SHA, "")
    ex.run = forged_git
    calls = []
    def gh(args, **kw):
        calls.append(args)
        return "ahead" if "compare/" in args[0] else "[]"
    monkeypatch.setattr(h, "_gh", gh)
    monkeypatch.setattr(h, "_eval_checkout", lambda *args: pytest.fail("unreachable sha fetched"))
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval"}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert "not reachable" in receipt["error"]
    assert calls and not sandbox_calls


def test_local_eval_open_pr_head_is_host_verified(host, monkeypatch):
    paths, _, _ = host
    local_eval_policy(paths)
    head = "b" * 40
    calls = []
    def gh(args, **kw):
        calls.append(args[0])
        if "pulls?" in args[0]:
            return head + "\n"
        return "ahead" if "compare/master" in args[0] else "behind"
    monkeypatch.setattr(h, "_gh", gh)
    h._eval_reachable(SHA, lambda: 30)
    assert any(f"compare/{head}...{SHA}" in call for call in calls)


def test_local_eval_total_deadline(host, monkeypatch):
    paths, _, _ = host
    local_eval_policy(paths)
    fake_prompt_phase(host, monkeypatch, [{"name": "case_one", "prompt": "p", "correct": ["a"], "plausible": []}])
    ticks = iter([0, 0] + [31] * 20)
    monkeypatch.setattr(h.time, "monotonic", lambda: next(ticks))
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert "time limit" in receipt["error"]


def test_local_eval_stalled_launcher_stops_scope_and_reaps_before_credential(host, monkeypatch):
    paths, _, _ = host
    local_eval_policy(paths)
    seen = fake_prompt_phase(host, monkeypatch, [])
    clock = [0.0]
    child = None

    class StalledChild:
        returncode = None
        killed = False
        reaped = False

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, **_):
            self.reaped = True
            return self.returncode

    real_popen = subprocess.Popen

    def popen(argv, **kw):
        nonlocal child
        if argv[0] == "systemd-run":
            child = StalledChild()
            kw["stdout"].write(b"test-local-model-credential")
            return child
        return real_popen(argv, **kw)

    monkeypatch.setattr(h.subprocess, "Popen", popen)
    scope_run = h.subprocess.run

    def run(argv, **kw):
        if argv[:3] == ["systemctl", "--user", "show"]:
            assert child.killed and child.reaped
        return scope_run(argv, **kw)

    monkeypatch.setattr(h.subprocess, "run", run)
    monkeypatch.setattr(h.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(h.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(h, "_eval_credential", lambda *_: pytest.fail("credential read after deadline"))
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert receipt["error"] == "local evaluation exceeded its time limit"
    assert receipt["detail"] == {}
    assert seen["stopped"] and child.killed and child.reaped
    assert "test-local-model-credential" not in json.dumps(receipt)


def test_local_eval_dripping_response_obeys_absolute_deadline(host, monkeypatch):
    paths, _, _ = host
    local_eval_policy(paths)
    fake_prompt_phase(host, monkeypatch, [{"name": "case", "prompt": "p", "correct": ["a"], "plausible": []}])
    clock = [0.0]
    socket_timeouts = []
    reads = []

    class Response:
        status = 200
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(
            settimeout=lambda seconds: socket_timeouts.append(seconds))))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read1(self, size):
            reads.append(size)
            clock[0] += 1
            return b"{"  # One byte arrives within every socket timeout.

    class Opener:
        def open(self, req, timeout):
            return Response()

    monkeypatch.setattr(h.urllib.request, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(h.time, "monotonic", lambda: clock[0])
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert receipt["error"] == "local evaluation exceeded its time limit"
    assert receipt["detail"] == {}
    assert len(reads) == 30 and all(size <= 64 << 10 for size in reads)
    assert socket_timeouts[0] == 30 and socket_timeouts[-1] == 1
    assert "test-local-model-credential" not in json.dumps(receipt)


def test_local_eval_chunked_header_cannot_extend_body_deadline():
    stopped = h.threading.Event()

    class Socket:
        def settimeout(self, _):
            pass

        def shutdown(self, _):
            stopped.set()

    class Response:
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=Socket()))

        def read1(self, _):
            # Chunked header parsing can make repeated reads inside one read1 call.
            assert stopped.wait(timeout=1)
            raise h.http.client.IncompleteRead(b"")

    started = time.monotonic()
    deadline = started + 0.15

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise h.Failed("local evaluation exceeded its time limit", {})
        return left

    with pytest.raises(h.Failed, match="time limit"):
        h._eval_response_body(Response(), remaining)
    assert stopped.is_set()
    assert time.monotonic() - started < 0.8


def test_bench_result_scrubs_nested_strings():
    value = {"outer": [{"secret": "sk-" + "x" * 30}]}
    assert "sk-" not in json.dumps(h.scrub_json(value))


def test_bench_result_receipt_keeps_hash_and_limits(host, monkeypatch):
    paths, ex, _ = host
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["bench.run"]["benchmarks"]["relay-observe-tests"]["result_file"] = "result.json"
    paths.policy.write_text(json.dumps(policy))
    checkout = paths.home / "src/worktrees/x/backend"
    checkout.mkdir(parents=True)
    ex.run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, SHA if "rev-parse" in argv else "", "")
    class Child:
        returncode = 0
        def __init__(self, argv, **kw):
            launcher = str(paths.home / ".local/bin/jarvis-sandbox")
            index = argv.index(launcher)
            assert argv[index:index + 4] == [launcher, "env", "-C", str(checkout)]
            (Path(kw["cwd"]) / "result.json").write_text(json.dumps({"nested": ["sk-" + "x" * 30]}))
        def poll(self):
            return self.returncode
    real_popen = subprocess.Popen
    monkeypatch.setattr(h.subprocess, "Popen", lambda argv, **kw: Child(argv, **kw)
                        if argv[0] == "systemd-run" else real_popen(argv, **kw))
    _, receipt = request(host, "bench.run", {"bench": "relay-observe-tests", "worktree": "x"}, sha=SHA)
    assert receipt["outcome"] == "ok"
    assert "sk-" not in json.dumps(receipt)
    assert receipt["detail"]["result"]["nested"] == ["[redacted]"]
    assert len(receipt["detail"]["output_sha256"]) == 64
    assert receipt["detail"]["limits"]["timeout_s"] == 900


def test_eval_checkout_extracts_only_safe_sha_archive(tmp_path, monkeypatch):
    content = archive([("repo", tarfile.DIRTYPE), ("repo/backend/pointing_eval.py", tarfile.REGTYPE)])
    fake_archive_download(tmp_path, monkeypatch, content)
    h._eval_checkout(SHA, tmp_path, lambda: 30)
    assert (tmp_path / "backend/pointing_eval.py").read_bytes() == b"# release\n"


@pytest.mark.parametrize("members", [
    [("repo", tarfile.REGTYPE)],
    [("repo", tarfile.DIRTYPE), ("other/file", tarfile.REGTYPE)],
    [("repo", tarfile.DIRTYPE), ("repo/link", tarfile.SYMTYPE)],
    [("repo", tarfile.DIRTYPE), ("repo/hardlink", tarfile.LNKTYPE)],
    [("repo", tarfile.DIRTYPE), ("repo/device", tarfile.CHRTYPE)],
    [("repo", tarfile.DIRTYPE), ("/absolute", tarfile.REGTYPE)],
    [("repo", tarfile.DIRTYPE), ("repo/../escape", tarfile.REGTYPE)],
])
def test_eval_checkout_refuses_unsafe_archive_member(tmp_path, monkeypatch, members):
    bad = archive(members)
    fake_archive_download(tmp_path, monkeypatch, bad)
    with pytest.raises(h.Refused, match="unsafe"):
        h._eval_checkout(SHA, tmp_path, lambda: 30)


def archive(members):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            data = b"# release\n"
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            tar.addfile(info, io.BytesIO(data) if info.size else None)
    return out.getvalue()


def fake_archive_download(tmp_path, monkeypatch, content):
    source = tmp_path / "archive.gz"
    source.write_bytes(content)
    class Download:
        def __init__(self):
            self.stdout = source.open("rb")
        def poll(self):
            return 0
        def wait(self, **_):
            return 0
    def popen(argv, **_):
        assert argv == ["gh", "api", f"repos/{h.REPO}/tarball/{SHA}"]
        return Download()
    monkeypatch.setattr(h.subprocess, "Popen", popen)


@pytest.mark.parametrize("limit,setting", [(12, "download"), (5, "extracted")])
def test_eval_checkout_refuses_archive_size_limits(tmp_path, monkeypatch, limit, setting):
    content = archive([("repo", tarfile.DIRTYPE), ("repo/backend/file", tarfile.REGTYPE)])
    fake_archive_download(tmp_path, monkeypatch, content)
    if setting == "download":
        monkeypatch.setattr(h, "MAX_EVAL_ARCHIVE_BYTES", limit)
    else:
        monkeypatch.setattr(h, "MAX_EVAL_EXTRACTED_BYTES", limit)
    with pytest.raises(h.Refused, match="size limit"):
        h._eval_checkout(SHA, tmp_path / "checkout", lambda: 30)


def test_eval_checkout_refuses_member_count_and_deadline(tmp_path, monkeypatch):
    content = archive([("repo", tarfile.DIRTYPE), ("repo/a", tarfile.REGTYPE), ("repo/b", tarfile.REGTYPE)])
    fake_archive_download(tmp_path, monkeypatch, content)
    monkeypatch.setattr(h, "MAX_EVAL_ARCHIVE_MEMBERS", 2)
    with pytest.raises(h.Refused, match="too many members"):
        h._eval_checkout(SHA, tmp_path / "checkout", lambda: 30)
    monkeypatch.setattr(h, "MAX_EVAL_ARCHIVE_MEMBERS", 20)
    calls = 0
    def expired():
        nonlocal calls
        calls += 1
        if calls >= 4:
            raise h.Failed("local evaluation exceeded its time limit", {})
        return 30
    with pytest.raises(h.Failed, match="time limit"):
        h._eval_checkout(SHA, tmp_path / "checkout", expired)
    assert calls >= 4


def test_eval_tempdir_restores_permission_for_cleanup(tmp_path):
    with h._eval_tempdir(tmp_path) as root:
        assert root.parent == tmp_path
        assert root.name.startswith(".hostops-eval-")
        child = root / "child"
        child.mkdir()
        (child / "file").write_text("x")
        child.chmod(0)
    assert not root.exists()


@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_eval_tempdir_refuses_unsafe_parent(tmp_path, kind):
    parent = tmp_path / "worktrees"
    if kind == "file":
        parent.write_text("x")
    elif kind == "symlink":
        parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(h.Refused, match="checkout parent"):
        with h._eval_tempdir(parent):
            pytest.fail("unsafe parent accepted")


def release_archive(extra=()):
    return archive([(f"repo/scripts/{name}", tarfile.REGTYPE) for name in
                    ("host_ops.py", "relay_continuity.py", "relay_argon.py", "relay_jobs.py", "relay_observe.py", "devctx.py", "worker_routing.py")]
                   + [("repo/README.md", tarfile.REGTYPE), *extra])


def mock_install(monkeypatch, data, status="ahead"):
    monkeypatch.delenv("JARVIS_SANDBOX", raising=False)
    monkeypatch.setattr(h.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(h, "_gh", lambda args: status)
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, data, b"")

    monkeypatch.setattr(h.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("name,kind", [("/absolute", tarfile.REGTYPE), ("repo/scripts/../evil", tarfile.REGTYPE),
                                       ("repo/scripts/link", tarfile.SYMTYPE), ("repo/scripts/hard", tarfile.LNKTYPE),
                                       ("repo/scripts/device", tarfile.CHRTYPE)])
def test_release_safe_extract(tmp_path, monkeypatch, name, kind):
    mock_install(monkeypatch, release_archive([(name, kind)]))
    paths = h.Paths(tmp_path)
    with pytest.raises(h.Refused):
        h.install_release(paths, SHA)
    assert not (paths.install / "current").exists()


def test_install_master_only_and_sandbox(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    calls = mock_install(monkeypatch, release_archive(), status="diverged")
    assert h.install(paths, SHA) == 2
    assert not calls
    monkeypatch.setattr(h.socket, "gethostname", lambda: "jarvis-sandbox")
    assert h.install(paths, SHA) == 2
    assert not calls


def test_install_release_and_units(host, monkeypatch):
    paths, _, _ = host
    calls = mock_install(monkeypatch, release_archive())
    assert h.install(paths, SHA) == 0
    current = paths.install / "current"
    assert current.resolve() == paths.install / "releases" / SHA
    assert sorted(p.name for p in current.iterdir()) == ["scripts"]
    assert not any("jarvisos-relay-watch.service" in a for a in calls)
    assert h.install(paths, SHA, with_watch=True) == 0
    assert any(a[-2:] == ["--now", "jarvisos-relay-watch.service"] for a in calls)
    for name in (h.UNIT, "jarvisos-relay-watch.service"):
        body = (paths.units / name).read_text()
        assert "python3 -I %h/.local/share/jarvisos-hostops/current/scripts/" in body
    for setting in ("NoNewPrivileges=yes", "MemoryMax=2G", "TasksMax=256", "CPUQuota=200%", "RestartForceExitStatus=75"):
        assert setting in h.SERVICE_UNIT


def test_release_operation_receipts_before_restart(host, monkeypatch):
    paths, ex, _ = host
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["release.install"]["trust_master"] = True
    paths.policy.write_text(json.dumps(policy))
    installed = []
    monkeypatch.setattr(h, "install_release", lambda paths, sha: installed.append(sha))
    req, receipt = request(host, "release.install", {"sha": SHA})
    assert installed == [SHA] and receipt["outcome"] == "ok"
    assert ex.restart_requested
    assert h.load_receipt(paths, req["id"])[0]


def test_journal_does_not_leak_descriptors(host):
    _, ex, _ = host
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(20):
        ex._append_journal({"op": "test"})
    assert len(os.listdir("/proc/self/fd")) == before


def test_isolated_copied_scripts(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("host_ops.py", "relay_continuity.py", "relay_argon.py", "relay_jobs.py", "relay_observe.py", "devctx.py", "worker_routing.py"):
        shutil.copyfile(SCRIPT.parent / name, scripts / name)
    for name in ("relay_continuity.py", "relay_observe.py", "devctx.py"):
        proc = subprocess.run([sys.executable, "-I", str(scripts / name), "--help"],
                              cwd=SCRIPT.parents[1], capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("attack", ["sha", "symlink"])
def test_handoff_file_refusals(host, monkeypatch, attack):
    paths, _, _ = host
    paths.outbox.mkdir(parents=True)
    file = paths.outbox / "model.png"
    if attack == "symlink":
        file.symlink_to(paths.policy)
    else:
        file.write_bytes(b"png")
    target = paths.home / "windows"
    monkeypatch.setattr(h, "_handoff_dir", lambda ctx, conf: target)
    # A symlink must be refused as a policy violation, rather than followed.
    receipt = request(host, "wsl.handoff", {"file": "model.png", "sha256": "0" * 64})[1]
    assert receipt["outcome"] == "refused"
    assert not list(target.glob("*.png")) if target.exists() else True


def test_screenshot_uses_sandbox_and_plain_argv(host):
    paths, ex, calls = host
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    chrome = paths.home / "chromium"
    chrome.touch()
    policy["ops"]["browser.screenshot"]["chrome"] = str(chrome)
    paths.policy.write_text(json.dumps(policy))

    def run(argv, **kw):
        calls.append((argv, kw))
        shot = next(a.split("=", 1)[1] for a in argv if a.startswith("--screenshot="))
        Path(shot).write_bytes(b"\x89PNG\r\n\x1a\nimage")
        return subprocess.CompletedProcess(argv, 0, "", "")

    ex.run = run
    req, receipt = request(host, "browser.screenshot", {"url": "http://127.0.0.1:8000/"})
    assert receipt["outcome"] == "ok"
    argv, kw = calls[0]
    assert argv[0] == str(paths.home / ".local/bin/jarvis-sandbox")
    assert argv[1] == str(chrome) and "--cwd" not in argv
    assert kw["cwd"] == str(paths.home / "src/JarvisOS")
    assert (paths.receipts / f"{req['id']}.png").read_bytes().startswith(b"\x89PNG")


def test_command_timeout_receipt(host):
    _, ex, _ = host

    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    ex.run = run
    assert request(host)[1]["outcome"] == "timeout"


def test_recovery_preserves_already_done_receipt(host):
    paths, ex, _ = host
    req, receipt = request(host)
    fd = ex._state_fd("inflight")
    try:
        h.write_at(fd, f"{req['id']}.json", h.canonical(req))
    finally:
        os.close(fd)
    ex.recover()
    assert h.load_receipt(paths, req["id"])[0] == receipt


def test_policy_reloaded_between_requests(host):
    paths, ex, calls = host

    def run(argv, **kwargs):
        calls.append(argv)
        paths.policy.write_text(json.dumps({"schema": h.POLICY_SCHEMA, "ops": {}}))
        return subprocess.CompletedProcess(argv, 0, "ActiveState=active", "")

    ex.run = run
    ids = [h.submit(paths, h.make_request("service.status", {"unit": "jarvisos-mission-owner.service"},
                                         None, "test", ex.now())) for _ in range(2)]
    ex.step()
    outcomes = [h.load_receipt(paths, rid)[0]["outcome"] for rid in ids]
    assert sorted(outcomes) == ["ok", "refused"] and len(calls) == 1


def test_release_restart_even_if_watcher_restart_fails(host, monkeypatch):
    paths, ex, _ = host
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["release.install"]["trust_master"] = True
    paths.policy.write_text(json.dumps(policy))
    monkeypatch.setattr(h, "install_release", lambda *args: None)
    ex.run = lambda argv, **kw: subprocess.CompletedProcess(argv, int("restart" in argv), "ActiveState=active", "")
    assert request(host, "release.install", {"sha": SHA})[1]["outcome"] == "failed"
    assert ex.restart_requested


def test_serve_restart_code_and_lock_descriptor_closed(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    monkeypatch.delenv("JARVIS_SANDBOX", raising=False)
    monkeypatch.setattr(h.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(h.signal, "signal", lambda *args: None)

    class Executor:
        restart_requested = True

        def __init__(self, paths):
            pass

        def recover(self):
            pass

        def log(self, message):
            pass

        def step(self):
            pass

    monkeypatch.setattr(h, "Executor", Executor)
    before = len(os.listdir("/proc/self/fd"))
    assert h.serve(paths, 0.1) == 75
    assert len(os.listdir("/proc/self/fd")) == before


def test_copied_release_discovers_repo_from_cwd(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    scripts = tmp_path / "release/scripts"
    scripts.mkdir(parents=True)
    for name in ("host_ops.py", "relay_continuity.py", "relay_argon.py", "relay_jobs.py", "relay_observe.py", "devctx.py", "worker_routing.py"):
        shutil.copyfile(SCRIPT.parent / name, scripts / name)
    code = ("import runpy,sys; "
            "r=runpy.run_path(sys.argv[1]); "
            "rc=r['rc']; "
            "assert rc.repo_root() == rc.Path.cwd(); "
            "assert rc.devctx.default_env().repo == rc.Path.cwd()")
    proc = subprocess.run([sys.executable, "-I", "-c", code, str(scripts / "relay_observe.py")],
                          cwd=repo, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr


def test_find_ok_receipt_filters_operation_sha_and_outcome(host):
    paths, _, _ = host
    req, receipt = request(host, sha=SHA)
    assert h.find_ok_receipt(paths, "service.status", SHA) == receipt
    assert h.find_ok_receipt(paths, "service.status", "b" * 40) is None
    assert h.find_ok_receipt(paths, "wsl.probe", SHA) is None
    refused = request(host, "service.status", {"unit": "evil.service"}, sha=SHA)[0]
    assert h.find_ok_receipt(paths, "service.status", SHA)["request_id"] == req["id"]
    assert h.load_receipt(paths, refused["id"])[0]["outcome"] == "refused"


def test_named_http_target_cannot_be_external(host):
    paths, _, _ = host
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["http.smoke"]["targets"]["bad"] = {"url": "https://example.com", "expect": 200}
    paths.policy.write_text(json.dumps(policy))
    assert request(host, "http.smoke", {"target": "bad"})[1]["outcome"] == "refused"


def test_mission_owner_start_is_allowed_and_receipted(host):
    _, _, calls = host
    _, receipt = request(host, "service.control", {"unit": "jarvisos-mission-owner.service", "action": "start"})
    assert receipt["outcome"] == "ok"
    assert ["systemctl", "--user", "start", "jarvisos-mission-owner.service"] in [argv for argv, _ in calls]


def semantic_policy(paths, **changes):
    (paths.home / "src/worktrees").mkdir(parents=True, exist_ok=True)
    credential = paths.config / "semantic-key"
    credential.write_text("semantic-private-token\n")
    credential.chmod(0o600)
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["bench.run"]["benchmarks"]["semantic"] = {
        "cwd": "backend", "argv": ["python", "semantic_eval.py", "--emit-prompts", "prompts.json"],
        "grade_argv": ["python", "semantic_eval.py", "--grade", "{replies_file}"],
        "result_file": "prompts.json",
    }
    policy["ops"]["service.control"]["units"].append("candidate.service")
    setting = {"kind": "semantic_judgment", "credential_file": str(credential),
               "base_url": "http://127.0.0.1:18081/v1", "model": "candidate",
               "thinking": "on", "verification": True, "max_tokens": 8192,
               "per_request_timeout_s": 30, "timeout_s": 120, "attempts": 3,
               "temperature": 0.5, "stop_default": True, "backend_port": 18080,
               "server_unit": "candidate.service"}
    setting.update(changes)
    policy["ops"]["local_eval.run"] = {"evaluations": {"semantic": setting}}
    paths.policy.write_text(json.dumps(policy))
    return setting


def fake_semantic(host, monkeypatch, *, failure=None, restore_failure=False, replies_override=None):
    paths, ex, calls = host
    case = json.loads((SCRIPT.parents[1] / "eval/local/semantic_judgment/cases.json").read_text())["cases"][0]
    row = {"name": case["id"], "prompt": "secret semantic prompt", "category": case["category"],
           "answer": case["answer"]}
    monkeypatch.setattr(h, "_eval_reachable", lambda *_: None)
    def checkout(_sha, root, _remaining):
        (root / "backend").mkdir()
        data = b"# evaluator\n"
        (root / "backend/semantic_eval.py").write_bytes(data)
        return {Path("backend/semantic_eval.py"): (len(data), h.hashlib.sha256(data).hexdigest())}, {Path("backend")}
    monkeypatch.setattr(h, "_eval_checkout", checkout)
    import importlib.util
    grader_path = SCRIPT.parents[1] / "eval/local/semantic_judgment/semantic_eval.py"
    grader_spec = importlib.util.spec_from_file_location("test_semantic_grader", grader_path)
    grader = importlib.util.module_from_spec(grader_spec)
    grader_spec.loader.exec_module(grader)
    events = []
    answer = json.dumps(case["answer"])
    replies = replies_override or [answer, json.dumps({**case["answer"], "verdict": "no_issue"}), answer]
    def bench(_ctx, _name, bench, root, sub, result, *_args, **_kwargs):
        calls.append((bench, _kwargs))
        if "--emit-prompts" in bench["argv"]:
            (root / sub / "prompts.json").write_text(json.dumps([row]))
            return {"result": [row]}
        recorded = json.loads((root / sub / "semantic-replies.json").read_text())
        assert recorded == {case["id"]: replies[:len(recorded[case["id"]])]}
        events.append("grade")
        return {"result": grader.grade_replies([case], recorded)}
    monkeypatch.setattr(h, "_run_benchmark", bench)
    state = {"active": False}
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[2] in ("start", "stop"):
            state["active"] = argv[2] == "start"
            events.append(f"unit_{argv[2]}")
        return subprocess.CompletedProcess(argv, 0, f"ActiveState={'active' if state['active'] else 'inactive'}\n", "")
    ex.run = run
    default = {"reachable": True}
    def http(_opener, url, _remaining, *, data=None, token=None):
        if url.endswith("/stop"):
            events.append("default_stop")
            default["reachable"] = False
            return {"runtime_reachable": False}
        if url.endswith("/start"):
            events.append("default_start")
            if restore_failure:
                raise h.Failed("restore failed")
            default["reachable"] = True
            return {"runtime_reachable": True}
        if url.endswith("/local-ai/runtime/llama-cpp"):
            return {"runtime_reachable": default["reachable"]}
        if url.endswith("/models"):
            events.append("ready")
            return {}
        events.append("model")
        if failure == "error":
            raise h.Failed("local model failed")
        if failure == "timeout":
            raise h.Failed("local model request exceeded its time limit")
        if failure == "cancel":
            raise h.Cancelled({})
        body = json.loads(data)
        assert body["messages"] == [{"role": "user", "content": row["prompt"]}]
        assert body["seed"] == len([item for item in events if item == "model"]) - 1
        assert token == "semantic-private-token"
        return {"choices": [{"message": {"content": replies[body["seed"]],
                                         "reasoning_content": "reason"}}],
                "usage": {"completion_tokens": 10}}
    monkeypatch.setattr(h, "_semantic_http", http)
    monkeypatch.setattr(h, "_semantic_memory", lambda _stop, peaks: peaks.update(
        {"ram_bytes": 100, "vram_mib": 200}))
    return events, case


def test_semantic_eval_grades_and_restores_without_text(host, monkeypatch):
    paths, _, calls = host
    semantic_policy(paths)
    events, case = fake_semantic(host, monkeypatch)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "ok", receipt["error"]
    assert events == ["default_stop", "unit_start", "ready", "model", "model", "model",
                      "unit_stop", "default_start", "grade"]
    benchmark_calls = [(bench, kwargs) for bench, kwargs in calls if isinstance(bench, dict)]
    assert len(benchmark_calls) == 2
    prompt_bench, prompt_kwargs = benchmark_calls[0]
    grade_bench, grade_kwargs = benchmark_calls[1]
    assert prompt_bench["argv"] == ["python", "semantic_eval.py", "--emit-prompts", "prompts.json"]
    assert prompt_kwargs["close_scope"] is True
    assert grade_bench["argv"] == ["python", "semantic_eval.py", "--grade", "semantic-replies.json"]
    assert grade_kwargs["close_scope"] is True
    assert grade_kwargs["json_stdout"] is True
    detail = receipt["detail"]
    result = detail["rows"][0]
    assert result["name"] == case["id"] and result["score"] == 1
    assert result["attempts_scored"] == 2 and result["disagreement"] is True
    assert result["majority"]["verdict"] == case["answer"]["verdict"]
    assert [item["completion_tokens"] for item in result["attempts"]] == [10, 10, 10]
    assert all(item["reasoning_present"] for item in result["attempts"])
    assert detail["categories"][case["category"]] == {"score": 1, "count": 1}
    assert detail["peak_ram_bytes"] == 100 and detail["peak_vram_mib"] == 200
    assert detail["errors"] == 0 and detail["restarts"] == 0
    encoded = json.dumps(receipt)
    assert "secret semantic prompt" not in encoded
    assert "semantic-private-token" not in encoded
    assert json.dumps(case["answer"]) not in encoded


@pytest.mark.parametrize("failure,outcome", [
    ("error", "failed"), ("timeout", "failed"), ("cancel", "cancelled"), (None, "ok"),
])
def test_semantic_restores_on_every_model_exit(host, monkeypatch, failure, outcome):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch, failure=failure)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == outcome
    assert events.index("default_stop") < events.index("unit_start")
    assert "unit_stop" in events and "default_start" in events


def test_semantic_failed_restore_is_failed(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    fake_semantic(host, monkeypatch, restore_failure=True)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert "restore" in receipt["error"]
    assert receipt["detail"]["restore"]["default"] == "failed"


def test_semantic_start_200_without_runtime_fails_restore(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = h._semantic_http
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/start"):
            events.append("default_start")
            return {"runtime_reachable": False, "reason_code": "LLAMACPP_FILE_MISSING"}
        return original(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert receipt["detail"]["restore"]["default"] == "failed"
    assert events[-1] == "default_start"


def test_semantic_restore_waits_for_loading_default(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = h._semantic_http
    statuses = iter([True, False, True])
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/start"):
            events.append("default_start")
            return {"runtime_reachable": False, "reason_code": "LLAMACPP_LOADING"}
        if url.endswith("/local-ai/runtime/llama-cpp"):
            return {"runtime_reachable": next(statuses)}
        return original(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "ok", receipt["error"]
    assert receipt["detail"]["restore"]["default"] == "ok"
    assert events[-2:] == ["default_start", "grade"]


def test_semantic_refuses_reachable_default_after_stop(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = h._semantic_http
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/stop"):
            events.append("default_stop")
            return {"runtime_reachable": True}
        return original(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert events == ["default_stop"]
    assert receipt["detail"]["restore"]["default"] == "not_needed"


def test_semantic_already_unreachable_default_is_not_stopped_or_started(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = h._semantic_http
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/local-ai/runtime/llama-cpp"):
            return {"runtime_reachable": False}
        return original(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "ok", receipt["error"]
    assert "default_stop" not in events and "default_start" not in events
    assert receipt["detail"]["restore"]["default"] == "not_needed"


def test_semantic_failed_stop_request_still_restores_default(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = h._semantic_http
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/stop"):
            events.append("default_stop")
            raise OSError("connection reset")
        return original(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert events == ["default_stop", "default_start"]
    assert receipt["detail"]["restore"]["default"] == "ok"


def test_semantic_invalid_initial_default_status_fails_without_restore(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = h._semantic_http
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/local-ai/runtime/llama-cpp"):
            return {"runtime_reachable": None}
        return original(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert events == []
    assert receipt["detail"]["restore"]["default"] == "not_needed"


def test_semantic_restore_has_own_budget_after_timeout(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths, timeout_s=1)
    events, _ = fake_semantic(host, monkeypatch, failure="timeout")
    original = h._semantic_http
    remaining_at_restore = []
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/chat/completions"):
            h.time.sleep(1.05)
        if url.endswith("/start"):
            remaining_at_restore.append(remaining())
        return original(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert events[-2:] == ["unit_stop", "default_start"]
    assert remaining_at_restore[0] > h.SEMANTIC_RESTORE_S - 10
    assert receipt["detail"]["restore"] == {"default": "ok", "server_unit": "ok"}


def test_semantic_unit_failed_during_stop_still_restores_default(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = h._unit_state
    def state(ctx, unit, *, timeout=30):
        if "unit_stop" in events:
            return {"ActiveState": "failed"}
        return original(ctx, unit, timeout=timeout)
    monkeypatch.setattr(h, "_unit_state", state)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "ok", receipt["error"]
    assert events[-3:] == ["unit_stop", "default_start", "grade"]
    assert receipt["detail"]["restore"] == {"default": "ok", "server_unit": "ok"}


def test_semantic_unit_failed_during_start_fails_immediately(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = h._unit_state
    def state(ctx, unit, *, timeout=30):
        if "unit_start" in events and "unit_stop" not in events:
            return {"ActiveState": "failed"}
        return original(ctx, unit, timeout=timeout)
    monkeypatch.setattr(h, "_unit_state", state)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert events == ["default_stop", "unit_start", "unit_stop", "default_start"]
    assert receipt["detail"]["restore"] == {"default": "ok", "server_unit": "ok"}


def test_semantic_short_reply_does_not_trigger_leak_check(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths, model="no_issue", attempts=1, temperature=0)
    fake_semantic(host, monkeypatch, replies_override=["no_issue"])
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "ok", receipt["error"]
    assert receipt["detail"]["rows"][0]["score"] == 0


def test_semantic_restores_after_candidate_start_failure(host, monkeypatch):
    paths, ex, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = ex.run
    def run(argv, **kwargs):
        if argv[2:4] == ["start", "candidate.service"]:
            events.append("unit_start_failed")
            return subprocess.CompletedProcess(argv, 1, "", "")
        return original(argv, **kwargs)
    ex.run = run
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert events == ["default_stop", "unit_start_failed", "unit_stop", "default_start"]
    assert receipt["detail"]["restore"] == {"default": "ok", "server_unit": "ok"}


def test_semantic_sampler_start_failure_still_stops_and_restores(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    class UnstartedSampler:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("sampler start failed")

        def join(self, **kwargs):
            pytest.fail("unstarted sampler was joined")

    monkeypatch.setattr(h.threading, "Thread", UnstartedSampler)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert events == ["default_stop", "unit_start", "ready", "unit_stop", "default_start"]
    assert receipt["detail"]["restore"] == {"default": "ok", "server_unit": "ok"}


def test_semantic_sampler_join_failure_still_stops_and_restores(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    class BrokenJoin:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def join(self, **kwargs):
            raise RuntimeError("join failed")

    monkeypatch.setattr(h.threading, "Thread", BrokenJoin)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert events[-2:] == ["unit_stop", "default_start"]
    assert receipt["detail"]["restore"] == {"default": "ok", "server_unit": "ok"}


def test_semantic_readiness_uses_token_and_counts_errors_and_restarts(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    fake_semantic(host, monkeypatch)
    original_http = h._semantic_http
    original_state = h._unit_state
    seen = {"readiness": 0}
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/models"):
            assert kwargs["token"] == "semantic-private-token"
            seen["readiness"] += 1
            if seen["readiness"] == 1:
                raise h.Failed("temporary readiness error", {})
        return original_http(opener, url, remaining, **kwargs)
    def state(ctx, unit, *, timeout=30):
        result = original_state(ctx, unit, timeout=timeout)
        result["NRestarts"] = "3" if seen["readiness"] else "1"
        return result
    monkeypatch.setattr(h, "_semantic_http", http)
    monkeypatch.setattr(h, "_unit_state", state)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "ok", receipt["error"]
    assert receipt["detail"]["errors"] == 1
    assert receipt["detail"]["restarts"] == 2


@pytest.mark.parametrize("initial,current,expected", [
    ("1000001", "1000003", 2), ("2", "1000005", 1_000_000),
    ("5", "3", 0), ("invalid", "7", 0),
])
def test_semantic_restarts_uses_raw_initial_count(host, monkeypatch, initial, current, expected):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = h._unit_state
    def state(ctx, unit, *, timeout=30):
        result = original(ctx, unit, timeout=timeout)
        result["NRestarts"] = current if "unit_start" in events else initial
        return result
    monkeypatch.setattr(h, "_unit_state", state)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "ok", receipt["error"]
    assert receipt["detail"]["restarts"] == expected


@pytest.mark.parametrize("replies,expected_score", [
    (["A", "A", "B", "C"], 0), (["A", "A", "A", "B"], 1),
])
def test_semantic_four_attempts_require_strict_majority(host, monkeypatch, replies, expected_score):
    paths, _, _ = host
    semantic_policy(paths, attempts=4)
    case = json.loads((SCRIPT.parents[1] / "eval/local/semantic_judgment/cases.json").read_text())["cases"][0]
    other = [v for v in ("no_issue", "invalid_assumption", "cannot_conclude", "contradiction")
             if v != case["answer"]["verdict"]]
    verdicts = {"A": case["answer"]["verdict"], "B": other[0], "C": other[1]}
    encoded = [json.dumps({**case["answer"], "verdict": verdicts[item]}) for item in replies]
    fake_semantic(host, monkeypatch, replies_override=encoded)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "ok", receipt["error"]
    row = receipt["detail"]["rows"][0]
    assert row["majority"]["verdict"] == (verdicts["A"] if expected_score else None)
    assert row["majority"]["score"] == row["score"] == expected_score


def test_semantic_tied_attempts_have_no_majority(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths, attempts=2)
    _, case = fake_semantic(host, monkeypatch)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "ok", receipt["error"]
    row = receipt["detail"]["rows"][0]
    assert row["name"] == case["id"]
    assert row["attempts_scored"] == 1
    assert row["majority"] == {"verdict": None, "options": None, "missing": None, "score": 0}
    assert row["score"] == receipt["detail"]["categories"][case["category"]]["score"] == 0


def test_semantic_cancels_during_readiness_and_restores(host, monkeypatch):
    paths, ex, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original_http = h._semantic_http
    req = h.make_request("local_eval.run", {"bench": "semantic"}, SHA, "test", ex.now())
    h.submit(paths, req)
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/models"):
            (paths.requests / f"{req['id']}.cancel").write_bytes(b"")
            raise h.Failed("temporary readiness error", {})
        return original_http(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    ex.step()
    receipt, why = h.load_receipt(paths, req["id"])
    assert receipt, why
    assert receipt["outcome"] == "cancelled"
    assert events[-2:] == ["unit_stop", "default_start"]


def test_semantic_unit_wait_checks_cancellation():
    cancelled = iter([False, False, True])
    ctx = SimpleNamespace(cancelled=lambda: next(cancelled),
                          run=lambda *args, **kwargs: subprocess.CompletedProcess(
                              args, 0, "ActiveState=activating\n", ""))
    with pytest.raises(h.Cancelled):
        h._semantic_unit(ctx, "start", "candidate.service", lambda: 10)


def test_semantic_grader_failure_keeps_replies_out_of_receipt(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    events, case = fake_semantic(host, monkeypatch)
    original = h._run_benchmark
    def bench(*args, **kwargs):
        if "--grade" in args[2]["argv"]:
            raise h.Failed("grader failed", {"output_tail": json.dumps(case["answer"])})
        return original(*args, **kwargs)
    monkeypatch.setattr(h, "_run_benchmark", bench)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert "grading" in receipt["error"]
    assert json.dumps(case["answer"]) not in json.dumps(receipt)
    assert events[-2:] == ["unit_stop", "default_start"]


@pytest.mark.parametrize("change", [
    {"max_tokens": 8193}, {"per_request_timeout_s": 1801}, {"timeout_s": 28801},
    {"attempts": 9}, {"temperature": 2.1}, {"thinking": "maybe"}, {"verification": 1},
    {"server_unit": "unregistered.service"}, {"base_url": "http://example.com/v1"},
    {"stop_default": 1}, {"attempts": 1, "temperature": 0.5},
])
def test_semantic_refuses_bad_policy(host, change):
    paths, _, _ = host
    semantic_policy(paths, **change)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "refused"


def test_semantic_refuses_unregistered_and_concurrent(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    assert request(host, "local_eval.run", {"bench": "other"}, sha=SHA)[1]["outcome"] == "refused"
    with h._semantic_lock(paths):
        assert request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)[1]["outcome"] == "refused"


@pytest.mark.parametrize("thinking,verification,expected", [
    ("on", True, {"enable_thinking": True, "verification": True}),
    ("off", False, {"enable_thinking": False, "verification": False}),
    ("off", None, {"enable_thinking": False}),
])
def test_semantic_request_settings(host, monkeypatch, thinking, verification, expected):
    paths, _, _ = host
    setting = semantic_policy(paths, thinking=thinking, attempts=1, temperature=0)
    if verification is None:
        policy = json.loads(paths.policy.read_text())
        del policy["ops"]["local_eval.run"]["evaluations"]["semantic"]["verification"]
        paths.policy.write_text(json.dumps(policy))
    else:
        setting["verification"] = verification
        policy = json.loads(paths.policy.read_text())
        policy["ops"]["local_eval.run"]["evaluations"]["semantic"] = setting
        paths.policy.write_text(json.dumps(policy))
    fake_semantic(host, monkeypatch)
    original = h._semantic_http
    bodies = []
    def capture(*args, **kwargs):
        if args[1].endswith("/chat/completions"):
            bodies.append(json.loads(kwargs["data"]))
        return original(*args, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", capture)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "ok", receipt["error"]
    assert len(bodies) == 1
    assert bodies[0]["chat_template_kwargs"] == expected
    assert bodies[0]["max_tokens"] == 8192
    assert bodies[0]["seed"] == 0 and bodies[0]["temperature"] == 0


@pytest.mark.parametrize("change", [
    {"name": "bad name"}, {"category": "other"}, {"prompt": ""}, {"answer": {}},
    {"extra": "field"},
])
def test_semantic_refuses_bad_prompt_rows(host, monkeypatch, change):
    paths, _, _ = host
    semantic_policy(paths)
    fake_semantic(host, monkeypatch)
    original = h._run_benchmark
    def bench(*args, **kwargs):
        result = original(*args, **kwargs)
        if "--emit-prompts" in args[2]["argv"]:
            result["result"][0].update(change)
        return result
    monkeypatch.setattr(h, "_run_benchmark", bench)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert receipt["detail"] == {}


@pytest.mark.parametrize("grade_argv", [
    ["python", "semantic_eval.py", "--grade", "some-file.json"],
    ["python", "semantic_eval.py", "--grade", "{replies_file}", "--grade", "again"],
    ["python", "semantic_eval.py", "--emit-prompts", "prompts.json"],
])
def test_semantic_requires_registered_grade_argv(host, grade_argv):
    paths, _, _ = host
    semantic_policy(paths)
    policy = json.loads(paths.policy.read_text())
    policy["ops"]["bench.run"]["benchmarks"]["semantic"]["grade_argv"] = grade_argv
    paths.policy.write_text(json.dumps(policy))
    assert request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)[1]["outcome"] == "refused"


def test_semantic_memory_samples_peak_and_missing_gpu(monkeypatch):
    samples = iter([
        ("MemTotal: 1000 kB\nMemAvailable: 600 kB\n", "100\n50\n"),
        ("MemTotal: 1000 kB\nMemAvailable: 400 kB\n", "missing"),
    ])
    current = [None]
    real_read = h.Path.read_text
    def read(path, *args, **kwargs):
        if str(path) == "/proc/meminfo":
            current[0] = next(samples)
            return current[0][0]
        return real_read(path, *args, **kwargs)
    def run(argv, **kwargs):
        assert argv == ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
        if current[0][1] == "missing":
            raise FileNotFoundError
        return subprocess.CompletedProcess(argv, 0, current[0][1], "")
    class Stop:
        count = 0
        def is_set(self):
            return self.count >= 2
        def wait(self, _):
            self.count += 1
    monkeypatch.setattr(h.Path, "read_text", read)
    monkeypatch.setattr(h.subprocess, "run", run)
    peaks = {"ram_bytes": None, "vram_mib": None}
    h._semantic_memory(Stop(), peaks)
    assert peaks == {"ram_bytes": 600 * 1024, "vram_mib": 150}


def test_semantic_http_uses_loopback_post_and_no_redirect_or_proxy(monkeypatch):
    seen = []
    class Response:
        status = 200
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=lambda _: None)))
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read1(self, _):
            return b""
    class Opener:
        def open(self, req, timeout):
            seen.append((req.full_url, req.get_method(), timeout))
            return Response()
    handlers = []
    def build(*args):
        handlers.extend(args)
        return Opener()
    monkeypatch.setattr(h.urllib.request, "build_opener", build)
    opener = h.urllib.request.build_opener(h.urllib.request.ProxyHandler({}), h._RejectRedirects())
    h._semantic_http(opener, "http://127.0.0.1:18080/local-ai/runtime/llama-cpp/stop",
                     lambda: 5, data=b"{}")
    assert seen == [("http://127.0.0.1:18080/local-ai/runtime/llama-cpp/stop", "POST", 5)]
    assert isinstance(handlers[0], h.urllib.request.ProxyHandler)
    assert handlers[0].proxies == {}
    assert isinstance(handlers[1], h._RejectRedirects)
