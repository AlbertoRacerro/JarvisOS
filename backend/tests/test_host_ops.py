"""Spec 207: offline host boundary, real Ed25519 receipts, no host mutations."""
from __future__ import annotations

import copy
import http.server
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from datetime import UTC, datetime, timedelta
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
            self.stdout = io.BytesIO(b"test-local-model-credential")
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


def test_local_eval_real_loopback_content_length(host, monkeypatch):
    paths, _, _ = host
    prompts = [{"name": "case_one", "prompt": "test prompt", "correct": ["good"], "plausible": []}]

    class Model(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            assert self.headers["Authorization"] == "Bearer test-local-model-credential"
            request_body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert request_body["messages"] == [{"role": "user", "content": "test prompt"}]
            body = json.dumps({"choices": [{"message": {"content": "[[point:good]]"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        local_eval_policy(paths, base_url=f"http://127.0.0.1:{server.server_port}/v1")
        fake_prompt_phase(host, monkeypatch, prompts)
        _, receipt = request(host, "local_eval.run", {"bench": "local-eval"}, sha=SHA)
        assert receipt["outcome"] == "ok", receipt["error"]
        assert receipt["detail"]["tally"]["correct"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
        stdout = io.BytesIO()
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
            child.stdout = io.BytesIO(b"test-local-model-credential")
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


def test_eval_response_body_stops_after_content_length_closes_socket():
    class Socket:
        closed = False

        def settimeout(self, _):
            if self.closed:
                raise OSError(9, "Bad file descriptor")

    sock = Socket()

    class Response:
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=sock))

        def read1(self, _):
            sock.closed = True
            return b'{"ok": true}'

        def isclosed(self):
            return sock.closed

    assert h._eval_response_body(Response(), lambda: 1) == b'{"ok": true}'


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
            self.stdout = io.BytesIO()
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


def test_bench_oversized_output_kills_child_and_stops_scope(host, monkeypatch):
    paths, _, _ = host
    ctx = h.Context(paths, h.load_policy(paths)[0], {"id": "a" * 32}, lambda *_a, **_k: None,
                    lambda *_a: 200, h.now_utc, lambda: False, [])
    root = paths.home / "checkout"
    (root / "backend").mkdir(parents=True)
    seen = {}

    class TrackedOutput(io.BytesIO):
        def close(self):
            seen["retained"] = self.tell()
            super().close()

    class Child:
        returncode = None

        def __init__(self, argv, **kw):
            assert kw["stdout"] == subprocess.PIPE
            assert kw["bufsize"] == 0
            self.stdout = TrackedOutput(b"x" * (h._BENCH_OUTPUT_MAX * 20))
            seen["child"] = self

        def poll(self):
            return self.returncode

        def kill(self):
            seen["killed_while_running"] = self.returncode is None
            self.returncode = -9

        def wait(self, **_):
            return self.returncode

    def stop(argv, **_):
        seen["stop"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(h.subprocess, "Popen", Child)
    monkeypatch.setattr(h.subprocess, "run", stop)
    with pytest.raises(h.Failed, match="benchmark output exceeded its size limit") as error:
        h._run_benchmark(ctx, "sample", {"argv": ["python", "bench.py"]}, root, "backend", None,
                         SHA, "x", json_stdout=True)
    assert seen["killed_while_running"]
    assert seen["retained"] == h._BENCH_OUTPUT_MAX + 1
    assert seen["stop"] == ["systemctl", "--user", "stop", "hostops-bench-aaaaaaaaaaaa.scope"]
    assert error.value.detail["bench"] == "sample"
    assert "output_tail" not in error.value.detail
    assert "output_sha256" not in error.value.detail


def test_bench_exited_child_stops_scope_to_close_output(host, monkeypatch):
    paths, _, _ = host
    ctx = h.Context(paths, h.load_policy(paths)[0], {"id": "a" * 32}, lambda *_a, **_k: None,
                    lambda *_a: 200, h.now_utc, lambda: False, [])
    root = paths.home / "checkout"
    (root / "backend").mkdir(parents=True)
    reading = threading.Event()
    stopped = threading.Event()
    seen = {}

    class BlockingOutput:
        def __init__(self):
            self.reads = 0

        def read(self, size):
            reading.set()
            assert stopped.wait(3), "benchmark scope was not stopped"
            self.reads += 1
            return b'{"score": 1}' if self.reads == 1 else b""

        def close(self):
            seen["closed"] = True

    class Child:
        returncode = 0

        def __init__(self, argv, **kw):
            assert kw["stdout"] == subprocess.PIPE
            assert kw["bufsize"] == 0
            self.stdout = BlockingOutput()

        def poll(self):
            return self.returncode

    def stop(argv, **_):
        assert reading.is_set()
        seen["stop"] = argv
        stopped.set()
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(h.subprocess, "Popen", Child)
    monkeypatch.setattr(h.subprocess, "run", stop)
    started = time.monotonic()
    result = h._run_benchmark(ctx, "sample", {"argv": ["python", "bench.py"]}, root, "backend",
                              None, SHA, "x", json_stdout=True)
    assert time.monotonic() - started < 3
    assert seen["stop"] == ["systemctl", "--user", "stop", "hostops-bench-aaaaaaaaaaaa.scope"]
    assert seen["closed"]
    assert result["result"] == {"score": 1}


def test_bench_json_stdout_within_limit_succeeds(host, monkeypatch):
    paths, _, _ = host
    ctx = h.Context(paths, h.load_policy(paths)[0], {"id": "a" * 32}, lambda *_a, **_k: None,
                    lambda *_a: 200, h.now_utc, lambda: False, [])
    root = paths.home / "checkout"
    (root / "backend").mkdir(parents=True)

    class Child:
        returncode = 0

        def __init__(self, argv, **kw):
            self.stdout = io.BytesIO(b'{"score": 1}')

        def poll(self):
            return self.returncode

    monkeypatch.setattr(h.subprocess, "Popen", Child)
    result = h._run_benchmark(ctx, "sample", {"argv": ["python", "bench.py"]}, root, "backend",
                              None, SHA, "x", json_stdout=True)
    assert result["result"] == {"score": 1}
    assert result["returncode"] == 0


def test_bench_plain_log_over_json_cap_keeps_tail(host, monkeypatch):
    paths, _, _ = host
    ctx = h.Context(paths, h.load_policy(paths)[0], {"id": "a" * 32}, lambda *_a, **_k: None,
                    lambda *_a: 200, h.now_utc, lambda: False, [])
    root = paths.home / "checkout"
    (root / "backend").mkdir(parents=True)

    class Child:
        returncode = 0

        def __init__(self, argv, **kw):
            self.stdout = io.BytesIO(b"log line\n" * (h._BENCH_OUTPUT_MAX // 9 + 1) + b"done")

        def poll(self):
            return self.returncode

    monkeypatch.setattr(h.subprocess, "Popen", Child)
    result = h._run_benchmark(ctx, "sample", {"argv": ["python", "bench.py"]}, root, "backend",
                              None, SHA, "x")
    assert result["output_tail"].endswith("done")


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


def mock_install(monkeypatch, data, status="ahead", rules=None, checks=None, statuses=None):
    monkeypatch.delenv("JARVIS_SANDBOX", raising=False)
    monkeypatch.setattr(h.socket, "gethostname", lambda: "host")
    if rules is None:
        rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "backend"}]}}]
    if checks is None:
        checks = [{"name": "backend", "status": "completed", "conclusion": "success", "id": 1, "completed_at": "2026-01-01T00:00:00Z"}]
    if statuses is None:
        statuses = []

    def gh(args, **kw):
        endpoint = args[0]
        if "rules/branches/" in endpoint:
            if isinstance(rules, Exception):
                raise rules
            return json.dumps(rules) if isinstance(rules, (list, dict)) else str(rules)
        if "check-runs" in endpoint:
            if isinstance(checks, Exception):
                raise checks
            return json.dumps({"check_runs": checks}) if isinstance(checks, (list, dict)) else str(checks)
        if "statuses" in endpoint:
            if isinstance(statuses, Exception):
                raise statuses
            return json.dumps(statuses) if isinstance(statuses, (list, dict)) else str(statuses)
        return status

    monkeypatch.setattr(h, "_gh", gh)
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


def test_required_checks_problem_ruleset_and_check_runs(monkeypatch):
    # Unreadable ruleset
    monkeypatch.setattr(h, "_gh", lambda args: (_ for _ in ()).throw(RuntimeError("api error")))
    assert "unreadable ruleset" in h.required_checks_problem(SHA)

    monkeypatch.setattr(h, "_gh", lambda args: "not-json{")
    assert "unreadable ruleset" in h.required_checks_problem(SHA)

    monkeypatch.setattr(h, "_gh", lambda args: json.dumps({"not": "a list"}))
    assert "unreadable ruleset" in h.required_checks_problem(SHA)

    # Empty ruleset
    monkeypatch.setattr(h, "_gh", lambda args: "[]")
    assert "empty ruleset" in h.required_checks_problem(SHA)

    monkeypatch.setattr(h, "_gh", lambda args: json.dumps([{"type": "other_rule"}]))
    assert "empty ruleset" in h.required_checks_problem(SHA)

    rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "backend"}]}}]

    # Check runs query fails
    def gh_runs_fail(args):
        if "rules/branches/" in args[0]:
            return json.dumps(rules)
        raise RuntimeError("check-runs failed")

    monkeypatch.setattr(h, "_gh", gh_runs_fail)
    assert "unreadable check runs" in h.required_checks_problem(SHA)

    # Missing check run
    def gh_missing(args):
        if "rules/branches/" in args[0]:
            return json.dumps(rules)
        return json.dumps({"check_runs": [{"name": "other_check", "status": "completed", "conclusion": "success"}]})

    monkeypatch.setattr(h, "_gh", gh_missing)
    prob = h.required_checks_problem(SHA)
    assert prob and "required check 'backend' is missing" in prob

    # Pending check run
    def gh_pending(args):
        if "rules/branches/" in args[0]:
            return json.dumps(rules)
        return json.dumps({"check_runs": [{"name": "backend", "status": "in_progress", "conclusion": None, "id": 1}]})

    monkeypatch.setattr(h, "_gh", gh_pending)
    prob = h.required_checks_problem(SHA)
    assert prob and "required check 'backend' is not completed (in_progress)" in prob

    # Failure conclusion
    def gh_failed(args):
        if "rules/branches/" in args[0]:
            return json.dumps(rules)
        return json.dumps({"check_runs": [{"name": "backend", "status": "completed", "conclusion": "failure", "id": 1}]})

    monkeypatch.setattr(h, "_gh", gh_failed)
    prob = h.required_checks_problem(SHA)
    assert prob and "required check 'backend' did not succeed (failure)" in prob

    # Multiple runs: latest decides
    # Case A: earlier failure, latest success -> None
    def gh_latest_success(args):
        if "rules/branches/" in args[0]:
            return json.dumps(rules)
        return json.dumps({"check_runs": [
            {"name": "backend", "status": "completed", "conclusion": "failure", "id": 1, "completed_at": "2026-01-01T00:00:00Z"},
            {"name": "backend", "status": "completed", "conclusion": "success", "id": 2, "completed_at": "2026-01-01T00:05:00Z"},
        ]})

    monkeypatch.setattr(h, "_gh", gh_latest_success)
    assert h.required_checks_problem(SHA) is None

    # Case B: earlier success, latest failure -> problem
    def gh_latest_failure(args):
        if "rules/branches/" in args[0]:
            return json.dumps(rules)
        return json.dumps({"check_runs": [
            {"name": "backend", "status": "completed", "conclusion": "success", "id": 1, "completed_at": "2026-01-01T00:00:00Z"},
            {"name": "backend", "status": "completed", "conclusion": "failure", "id": 2, "completed_at": "2026-01-01T00:05:00Z"},
        ]})

    monkeypatch.setattr(h, "_gh", gh_latest_failure)
    prob = h.required_checks_problem(SHA)
    assert prob and "required check 'backend' did not succeed (failure)" in prob


def test_install_release_green_installs(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    paths.config.mkdir(parents=True, exist_ok=True)
    paths.policy.write_text(json.dumps(h.DEFAULT_POLICY))
    paths.policy.chmod(0o600)
    mock_install(monkeypatch, release_archive())
    h.install_release(paths, SHA)
    current = paths.install / "current"
    assert current.resolve() == paths.install / "releases" / SHA
    assert (paths.install / "releases" / SHA / "scripts" / "host_ops.py").is_file()
    # Re-install candidate again (identical sha) succeeds
    h.install_release(paths, SHA)
    assert current.resolve() == paths.install / "releases" / SHA


@pytest.mark.parametrize("rules,checks,match", [
    (None, [{"name": "other", "status": "completed", "conclusion": "success"}], "missing"),
    (None, [{"name": "backend", "status": "in_progress", "conclusion": None, "id": 1}], "in_progress"),
    (None, [{"name": "backend", "status": "completed", "conclusion": "failure", "id": 1}], "failure"),
    (RuntimeError("rules error"), None, "unreadable ruleset"),
    ([], None, "empty ruleset"),
])
def test_install_release_refuses_checks_problems(tmp_path, monkeypatch, rules, checks, match):
    paths = h.Paths(tmp_path)
    old_sha = "1" * 40
    (paths.install / "releases" / old_sha / "scripts").mkdir(parents=True)
    (paths.install / "current").symlink_to(paths.install / "releases" / old_sha)

    mock_install(monkeypatch, release_archive(), status="ahead", rules=rules, checks=checks)
    with pytest.raises(h.Refused, match=match):
        h.install_release(paths, SHA)
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha


def test_install_release_candidate_behind_installed_refused(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    old_sha = "1" * 40
    candidate_sha = "2" * 40
    (paths.install / "releases" / old_sha / "scripts").mkdir(parents=True)
    (paths.install / "current").symlink_to(paths.install / "releases" / old_sha)

    def gh(args, **kw):
        endpoint = args[0]
        if f"compare/{candidate_sha}...master" in endpoint:
            return "ahead"
        if f"compare/{old_sha}...{candidate_sha}" in endpoint:
            return "behind"
        if "rules/branches/" in endpoint:
            return json.dumps([{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "backend"}]}}])
        if "check-runs" in endpoint:
            return json.dumps({"check_runs": [{"name": "backend", "status": "completed", "conclusion": "success", "id": 1, "completed_at": "2026-01-01T00:00:00Z"}]})
        if "statuses" in endpoint:
            return "[]"
        return "behind"

    monkeypatch.delenv("JARVIS_SANDBOX", raising=False)
    monkeypatch.setattr(h.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(h, "_gh", gh)
    monkeypatch.setattr(h.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, release_archive(), b""))

    with pytest.raises(h.Refused, match="not ahead or identical to installed"):
        h.install_release(paths, candidate_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha


def test_install_release_preflight_failure_refused(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    old_sha = "1" * 40
    candidate_sha = "2" * 40
    (paths.install / "releases" / old_sha / "scripts").mkdir(parents=True)
    (paths.install / "current").symlink_to(paths.install / "releases" / old_sha)

    mock_install(monkeypatch, release_archive(), status="ahead")
    monkeypatch.setattr(h, "_run_preflight", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "policy enables unknown operations ['bad_op']\nextra info"))

    with pytest.raises(h.Refused, match=r"candidate preflight failed: policy enables unknown operations \['bad_op'\]"):
        h.install_release(paths, candidate_sha)

    # Release directory not created and current unchanged
    assert not (paths.install / "releases" / candidate_sha).exists()
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha


def test_preflight_cli_ok_and_unknown_op(tmp_path, capsys):
    paths = h.Paths(tmp_path)
    paths.config.mkdir(parents=True, exist_ok=True)
    paths.policy.write_text(json.dumps(h.DEFAULT_POLICY))
    paths.policy.chmod(0o600)

    # Valid policy
    before = sorted(tmp_path.rglob("*"))
    rc = h.main(["--home", str(tmp_path), "preflight"])
    out, err = capsys.readouterr()
    assert rc == 0
    data = json.loads(out)
    assert data == {"ok": True, "version": h.executor_version()}
    assert not err
    after = sorted(tmp_path.rglob("*"))
    assert before == after  # Preflight must not write anything

    # Policy enabling unknown operation
    bad_policy = copy.deepcopy(h.DEFAULT_POLICY)
    bad_policy["ops"]["nonexistent.op"] = {}
    paths.policy.write_text(json.dumps(bad_policy))
    rc = h.main(["--home", str(tmp_path), "preflight"])
    out, err = capsys.readouterr()
    assert rc == 1
    assert "policy enables unknown operations" in err
    assert "nonexistent.op" in err
    assert not out


def test_install_release_reinstall_damaged_existing_release_refused(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    old_sha = "1" * 40
    candidate_sha = "2" * 40
    (paths.install / "releases" / old_sha / "scripts").mkdir(parents=True)
    for name in ("host_ops.py", "relay_continuity.py", "relay_jobs.py", "relay_observe.py", "devctx.py", "worker_routing.py"):
        (paths.install / "releases" / old_sha / "scripts" / name).write_text("# old")
    (paths.install / "current").symlink_to(paths.install / "releases" / old_sha)

    # Existing candidate release directory already on disk, but damaged (missing worker_routing.py)
    cand_dir = paths.install / "releases" / candidate_sha / "scripts"
    cand_dir.mkdir(parents=True)
    for name in ("host_ops.py", "relay_continuity.py", "relay_jobs.py", "relay_observe.py", "devctx.py"):
        (cand_dir / name).write_text("# candidate")

    mock_install(monkeypatch, release_archive(), status="ahead")
    monkeypatch.setattr(h, "_run_preflight", lambda argv, timeout=60: subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}), ""))

    preflighted = []

    def record_preflight(argv, timeout=60):
        preflighted.append(argv[2])
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}), "")

    monkeypatch.setattr(h, "_run_preflight", record_preflight)

    # The damaged existing release is refused by verification before any of its code runs
    with pytest.raises(h.Refused, match="existing release"):
        h.install_release(paths, candidate_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha
    assert not any(str(paths.install / "releases" / candidate_sha) in arg for arg in preflighted)

    # Same manifest but different content: still refused before preflight
    (cand_dir / "worker_routing.py").write_text("# candidate")
    (cand_dir / "relay_argon.py").write_text("# candidate")
    with pytest.raises(h.Refused, match="existing release"):
        h.install_release(paths, candidate_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha
    assert not any(str(paths.install / "releases" / candidate_sha) in arg for arg in preflighted)

    # Existing release matches the candidate archive
    for name in ("host_ops.py", "relay_continuity.py", "relay_argon.py", "relay_jobs.py", "relay_observe.py", "devctx.py", "worker_routing.py"):
        target = cand_dir / name
        target.write_bytes(b"# release\n")
        target.chmod(0o644)

    # A matching existing release still has to pass its own preflight
    def run_preflight_fail_on_candidate(argv, timeout=60):
        if str(paths.install / "releases" / candidate_sha) in argv[2]:
            return subprocess.CompletedProcess(argv, 1, "", "candidate preflight failed: broken release\n")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}), "")

    monkeypatch.setattr(h, "_run_preflight", run_preflight_fail_on_candidate)
    with pytest.raises(h.Refused, match="candidate preflight failed: broken release"):
        h.install_release(paths, candidate_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha

    monkeypatch.setattr(h, "_run_preflight", lambda argv, timeout=60: subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}), ""))
    h.install_release(paths, candidate_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / candidate_sha


def test_install_release_reinstall_modified_content_refused(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    old_sha = "1" * 40
    candidate_sha = "2" * 40
    (paths.install / "releases" / old_sha / "scripts").mkdir(parents=True)
    (paths.install / "current").symlink_to(paths.install / "releases" / old_sha)

    mock_install(monkeypatch, release_archive(), status="ahead")
    monkeypatch.setattr(h, "_run_preflight", lambda argv, timeout=60: subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}), ""))

    # First install creates valid releases/candidate_sha
    h.install_release(paths, candidate_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / candidate_sha

    # Tamper with content of one script in the existing release
    target = paths.install / "releases" / candidate_sha / "scripts" / "relay_jobs.py"
    target.write_bytes(b"# tampered content\n")

    # Point current back to old_sha to prove it fails closed without switching current
    (paths.install / "current").unlink()
    (paths.install / "current").symlink_to(paths.install / "releases" / old_sha)

    with pytest.raises(h.Refused, match=r"existing release content mismatch for scripts/relay_jobs\.py"):
        h.install_release(paths, candidate_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha


def test_install_release_reinstall_extra_file_refused(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    old_sha = "1" * 40
    candidate_sha = "2" * 40
    (paths.install / "releases" / old_sha / "scripts").mkdir(parents=True)
    (paths.install / "current").symlink_to(paths.install / "releases" / old_sha)

    mock_install(monkeypatch, release_archive(), status="ahead")
    monkeypatch.setattr(h, "_run_preflight", lambda argv, timeout=60: subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}), ""))

    # First install
    h.install_release(paths, candidate_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / candidate_sha

    # Add extra script file in existing release
    extra_script = paths.install / "releases" / candidate_sha / "scripts" / "backdoor.py"
    extra_script.write_bytes(b"# backdoor\n")

    (paths.install / "current").unlink()
    (paths.install / "current").symlink_to(paths.install / "releases" / old_sha)

    with pytest.raises(h.Refused, match=r"existing release has extra file: scripts/backdoor\.py"):
        h.install_release(paths, candidate_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha

    # Remove extra script, add extra file at release root
    extra_script.unlink()
    extra_root = paths.install / "releases" / candidate_sha / "evil.sh"
    extra_root.write_bytes(b"#!/bin/sh\n")

    with pytest.raises(h.Refused, match=r"existing release has extra file: evil\.sh"):
        h.install_release(paths, candidate_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha


def test_install_release_reinstall_mode_mismatch_refused(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    old_sha = "1" * 40
    candidate_sha = "2" * 40
    (paths.install / "releases" / old_sha / "scripts").mkdir(parents=True)
    (paths.install / "current").symlink_to(paths.install / "releases" / old_sha)

    mock_install(monkeypatch, release_archive(), status="ahead")
    monkeypatch.setattr(h, "_run_preflight", lambda argv, timeout=60: subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}), ""))

    # First install
    h.install_release(paths, candidate_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / candidate_sha

    # Mode mismatch on script: chmod 0o755 when candidate extracted as 0o644
    target = paths.install / "releases" / candidate_sha / "scripts" / "host_ops.py"
    target.chmod(0o755)

    (paths.install / "current").unlink()
    (paths.install / "current").symlink_to(paths.install / "releases" / old_sha)

    with pytest.raises(h.Refused, match=r"existing release file mode mismatch for scripts/host_ops\.py"):
        h.install_release(paths, candidate_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha


def test_install_release_reinstall_identical_release_succeeds(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    old_sha = "1" * 40
    candidate_sha = "2" * 40
    (paths.install / "releases" / old_sha / "scripts").mkdir(parents=True)
    (paths.install / "current").symlink_to(paths.install / "releases" / old_sha)

    mock_install(monkeypatch, release_archive(), status="ahead")
    monkeypatch.setattr(h, "_run_preflight", lambda argv, timeout=60: subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}), ""))

    # First install
    h.install_release(paths, candidate_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / candidate_sha

    # Symlink current back to old_sha
    (paths.install / "current").unlink()
    (paths.install / "current").symlink_to(paths.install / "releases" / old_sha)

    # Identical reinstall succeeds and updates current
    h.install_release(paths, candidate_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / candidate_sha


def test_required_checks_problem_integration_id(monkeypatch):
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": "backend", "integration_id": 15368},
                ]
            }
        }
    ]

    def gh_factory(runs, statuses=None):
        def gh(args, **kw):
            endpoint = args[0]
            if "rules/branches/" in endpoint:
                return json.dumps(rules)
            if "check-runs" in endpoint:
                return json.dumps({"check_runs": runs})
            if "statuses" in endpoint:
                return json.dumps(statuses or [])
            return "ahead"
        return gh

    # Check run from wrong app ID (e.g. 99999 instead of 15368) -> missing
    runs_wrong_app = [
        {"name": "backend", "status": "completed", "conclusion": "success", "id": 1, "app": {"id": 99999}}
    ]
    monkeypatch.setattr(h, "_gh", gh_factory(runs_wrong_app))
    prob = h.required_checks_problem(SHA)
    assert prob and "required check 'backend' is missing" in prob

    # Check run with no app dict -> missing
    runs_no_app = [
        {"name": "backend", "status": "completed", "conclusion": "success", "id": 1}
    ]
    monkeypatch.setattr(h, "_gh", gh_factory(runs_no_app))
    prob = h.required_checks_problem(SHA)
    assert prob and "required check 'backend' is missing" in prob

    # Successful run from app 99999 but failed run from required app 15368 -> failure
    runs_mixed_apps = [
        {"name": "backend", "status": "completed", "conclusion": "success", "id": 2, "app": {"id": 99999}},
        {"name": "backend", "status": "completed", "conclusion": "failure", "id": 1, "app": {"id": 15368}},
    ]
    monkeypatch.setattr(h, "_gh", gh_factory(runs_mixed_apps))
    prob = h.required_checks_problem(SHA)
    assert prob and "required check 'backend' did not succeed (failure)" in prob

    # Successful run from required app 15368 -> OK
    runs_correct_app = [
        {"name": "backend", "status": "completed", "conclusion": "success", "id": 3, "app": {"id": 15368}}
    ]
    monkeypatch.setattr(h, "_gh", gh_factory(runs_correct_app))
    assert h.required_checks_problem(SHA) is None


def test_required_checks_problem_pending_rerun_and_monotonic_id(monkeypatch):
    rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "backend"}]}}]

    def gh_factory(runs):
        def gh(args, **kw):
            endpoint = args[0]
            if "rules/branches/" in endpoint:
                return json.dumps(rules)
            if "check-runs" in endpoint:
                return json.dumps({"check_runs": runs})
            if "statuses" in endpoint:
                return "[]"
            return "ahead"
        return gh

    # Older success (with timestamps), newer queued rerun (no timestamps) -> refuse (not completed)
    runs_queued_rerun = [
        {"name": "backend", "status": "completed", "conclusion": "success", "id": 1, "completed_at": "2026-01-01T12:05:00Z"},
        {"name": "backend", "status": "queued", "conclusion": None, "id": 2},
    ]
    monkeypatch.setattr(h, "_gh", gh_factory(runs_queued_rerun))
    prob = h.required_checks_problem(SHA)
    assert prob and "required check 'backend' is not completed (queued)" in prob

    # Older success, newer in_progress rerun -> refuse
    runs_in_progress_rerun = [
        {"name": "backend", "status": "completed", "conclusion": "success", "id": 1, "completed_at": "2026-01-01T12:05:00Z"},
        {"name": "backend", "status": "in_progress", "conclusion": None, "id": 2, "started_at": "2026-01-01T12:10:00Z"},
    ]
    monkeypatch.setattr(h, "_gh", gh_factory(runs_in_progress_rerun))
    prob = h.required_checks_problem(SHA)
    assert prob and "required check 'backend' is not completed (in_progress)" in prob

    # Monotonic ID selection: run id 2 completed with success wins over run id 1 with failure even if timestamps are inverted
    runs_id_success = [
        {"name": "backend", "status": "completed", "conclusion": "failure", "id": 1, "completed_at": "2026-01-01T12:10:00Z"},
        {"name": "backend", "status": "completed", "conclusion": "success", "id": 2, "completed_at": "2026-01-01T12:00:00Z"},
    ]
    monkeypatch.setattr(h, "_gh", gh_factory(runs_id_success))
    assert h.required_checks_problem(SHA) is None

    # Monotonic ID selection: run id 2 completed with failure wins over run id 1 with success
    runs_id_failure = [
        {"name": "backend", "status": "completed", "conclusion": "success", "id": 1, "completed_at": "2026-01-01T12:10:00Z"},
        {"name": "backend", "status": "completed", "conclusion": "failure", "id": 2, "completed_at": "2026-01-01T12:00:00Z"},
    ]
    monkeypatch.setattr(h, "_gh", gh_factory(runs_id_failure))
    prob = h.required_checks_problem(SHA)
    assert prob and "required check 'backend' did not succeed (failure)" in prob


def test_required_checks_problem_commit_statuses(monkeypatch):
    rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "backend"}]}}]
    check_runs = [{"name": "backend", "status": "completed", "conclusion": "success", "id": 1}]

    def gh_factory(statuses):
        def gh(args, **kw):
            endpoint = args[0]
            if "rules/branches/" in endpoint:
                return json.dumps(rules)
            if "check-runs" in endpoint:
                return json.dumps({"check_runs": check_runs})
            if "statuses" in endpoint:
                if isinstance(statuses, Exception):
                    raise statuses
                return json.dumps(statuses)
            return "ahead"
        return gh

    # Check run succeeded, but commit status failed -> refuse
    statuses_failed = [{"id": 1, "context": "backend", "state": "failure"}]
    monkeypatch.setattr(h, "_gh", gh_factory(statuses_failed))
    prob = h.required_checks_problem(SHA)
    assert prob and "required check 'backend' commit status did not succeed (failure)" in prob

    # Check run succeeded, but commit status pending -> refuse
    statuses_pending = [{"id": 1, "context": "backend", "state": "pending"}]
    monkeypatch.setattr(h, "_gh", gh_factory(statuses_pending))
    prob = h.required_checks_problem(SHA)
    assert prob and "required check 'backend' commit status did not succeed (pending)" in prob

    # Older commit status pending (id 1), latest commit status success (id 2) -> OK
    statuses_resolved = [
        {"id": 1, "context": "backend", "state": "pending"},
        {"id": 2, "context": "backend", "state": "success"},
    ]
    monkeypatch.setattr(h, "_gh", gh_factory(statuses_resolved))
    assert h.required_checks_problem(SHA) is None

    # Older commit status success (id 1), latest commit status pending rerun (id 2) -> refuse
    statuses_rerun_pending = [
        {"id": 1, "context": "backend", "state": "success"},
        {"id": 2, "context": "backend", "state": "pending"},
    ]
    monkeypatch.setattr(h, "_gh", gh_factory(statuses_rerun_pending))
    prob = h.required_checks_problem(SHA)
    assert prob and "required check 'backend' commit status did not succeed (pending)" in prob

    # Commit statuses query fails -> unreadable commit statuses
    monkeypatch.setattr(h, "_gh", gh_factory(RuntimeError("API error")))
    prob = h.required_checks_problem(SHA)
    assert prob and "unreadable commit statuses" in prob


def test_install_release_unresolvable_current_refused(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    mock_install(monkeypatch, release_archive(), status="ahead")

    # Case A: current is a broken symlink -> refuse
    current = paths.install / "current"
    paths.install.mkdir(parents=True, exist_ok=True)
    current.symlink_to(paths.install / "releases" / ("9" * 40))
    with pytest.raises(h.Refused, match="cannot resolve current release"):
        h.install_release(paths, SHA)

    # Case B: current resolves to a directory whose name is not a 40-char SHA -> refuse
    current.unlink()
    bad_dir = paths.install / "releases" / "not-a-sha"
    bad_dir.mkdir(parents=True, exist_ok=True)
    current.symlink_to(bad_dir)
    with pytest.raises(h.Refused, match="current release is not a valid commit sha directory"):
        h.install_release(paths, SHA)

    # Case C: current is a regular file, not a directory -> refuse
    current.unlink()
    bad_file = paths.install / "releases" / ("f" * 40)
    bad_file.write_text("file")
    current.symlink_to(bad_file)
    with pytest.raises(h.Refused, match="current release is not a valid commit sha directory"):
        h.install_release(paths, SHA)

    # Case D: Absence of current (first install) stays allowed
    current.unlink()
    assert not current.exists() and not current.is_symlink()
    h.install_release(paths, SHA)
    assert current.resolve() == paths.install / "releases" / SHA


def test_install_refused_leaves_no_new_files(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    # Refused because commit is diverged from master
    mock_install(monkeypatch, release_archive(), status="diverged")

    rc = h.install(paths, SHA)
    assert rc == 2
    # Ensure config and policy directories/files were not created
    assert not paths.config.exists()
    assert not paths.policy.exists()
    assert not paths.install.exists()
    assert not (paths.home / ".local").exists()
    assert not (paths.home / ".config").exists()


def test_release_operation_receipts_before_restart(host, monkeypatch):
    paths, ex, _ = host
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["release.install"]["trust_master"] = True
    paths.policy.write_text(json.dumps(policy))
    installed = []
    monkeypatch.setattr(h, "install_release", lambda paths, sha, **kw: installed.append(sha))
    req, receipt = request(host, "release.install", {"sha": SHA})
    assert installed == [SHA] and receipt["outcome"] == "ok"
    assert ex.restart_requested
    assert h.load_receipt(paths, req["id"])[0]


def test_parse_systemd_ts():
    assert h.parse_systemd_ts("Thu 2026-11-13 07:10:00 UTC") == datetime(2026, 11, 13, 7, 10, 0, tzinfo=UTC)
    assert h.parse_systemd_ts("2026-11-13 07:10:00 UTC") == datetime(2026, 11, 13, 7, 10, 0, tzinfo=UTC)
    assert h.parse_systemd_ts("2026-11-13 07:10:00") == datetime(2026, 11, 13, 7, 10, 0, tzinfo=UTC)
    assert h.parse_systemd_ts("Fri 2026-11-13 08:10:00 CET") == datetime(2026, 11, 13, 7, 10, 0, tzinfo=UTC)
    assert h.parse_systemd_ts("Fri 2026-11-13 09:10:00 CEST") == datetime(2026, 11, 13, 7, 10, 0, tzinfo=UTC)
    assert h.parse_systemd_ts("Fri 2026-11-13 02:10:00 EST") == datetime(2026, 11, 13, 7, 10, 0, tzinfo=UTC)
    assert h.parse_systemd_ts("Fri 2026-11-13 08:10:00 +0100") == datetime(2026, 11, 13, 7, 10, 0, tzinfo=UTC)
    assert h.parse_systemd_ts("1731481800") == datetime.fromtimestamp(1731481800, tz=UTC)
    assert h.parse_systemd_ts("") is None
    assert h.parse_systemd_ts("not a timestamp") is None
    assert h.parse_systemd_ts("2026-11-13 07:10:00 UNKNOWN_TZ") is None


def test_install_release_persists_pending_marker(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    mock_install(monkeypatch, release_archive())
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    h.install_release(paths, SHA, now=t0)

    assert paths.release_pending.is_file()
    marker = h.load_release_pending(paths)
    assert marker is not None
    assert marker["previous_sha"] is None
    assert marker["candidate_sha"] == SHA
    assert marker["request_id"] is None
    assert marker["attempts"] == 0
    assert marker["watcher_active"] is False
    assert marker["created_at"] == "2026-11-13T10:00:00Z"
    assert marker["deadline"] == "2026-11-13T10:10:00Z"

    # Installing candidate B while candidate A is still pending is refused (Finding 4)
    candidate_b = "b" * 40
    mock_install(monkeypatch, release_archive(), status="ahead")
    t1 = datetime(2026, 11, 13, 11, 0, 0, tzinfo=UTC)
    with pytest.raises(h.Refused, match="another release is already pending"):
        h.install_release(paths, candidate_b, request_id="1" * 32, watcher_active=True, now=t1)

    # Candidate A's pending marker remains intact
    marker_a = h.load_release_pending(paths)
    assert marker_a is not None
    assert marker_a["candidate_sha"] == SHA
    assert marker_a["created_at"] == "2026-11-13T10:00:00Z"
    assert marker_a["deadline"] == "2026-11-13T10:10:00Z"

    # Once candidate A is confirmed / resolved, upgrading to candidate B succeeds
    paths.release_pending.unlink()
    h.install_release(paths, candidate_b, request_id="1" * 32, watcher_active=True, now=t1)

    marker_b = h.load_release_pending(paths)
    assert marker_b is not None
    assert marker_b["previous_sha"] == SHA
    assert marker_b["candidate_sha"] == candidate_b
    assert marker_b["request_id"] == "1" * 32
    assert marker_b["attempts"] == 0
    assert marker_b["watcher_active"] is True
    assert marker_b["created_at"] == "2026-11-13T11:00:00Z"
    assert marker_b["deadline"] == "2026-11-13T11:10:00Z"


def test_install_release_failure_does_not_persist_pending_marker(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "backend"}]}}]
    mock_install(monkeypatch, release_archive(), rules=rules, checks=[])

    with pytest.raises(h.Refused, match="required check 'backend' is missing"):
        h.install_release(paths, SHA)
    assert not paths.release_pending.exists()


def test_op_release_install_passes_request_id_and_watcher_state(host, monkeypatch):
    paths, ex, _ = host
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["release.install"]["trust_master"] = True
    paths.policy.write_text(json.dumps(policy))

    installed_info = {}

    def fake_install(p, sha, *, request_id=None, watcher_active=False, now=None):
        installed_info.update({"sha": sha, "request_id": request_id, "watcher_active": watcher_active})

    monkeypatch.setattr(h, "install_release", fake_install)
    ex.run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "ActiveState=active\n", "")

    req, receipt = request(host, "release.install", {"sha": SHA})
    assert receipt["outcome"] == "ok"
    assert installed_info["sha"] == SHA
    assert installed_info["request_id"] == req["id"]
    assert installed_info["watcher_active"] is True


def test_op_release_install_no_retry_on_type_error(host, monkeypatch):
    paths, ex, _ = host
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["release.install"]["trust_master"] = True
    paths.policy.write_text(json.dumps(policy))

    calls = 0

    def fail_install(*args, **kw):
        nonlocal calls
        calls += 1
        raise TypeError("intentional TypeError inside install_release")

    monkeypatch.setattr(h, "install_release", fail_install)
    ex.run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "ActiveState=active\n", "")

    req, receipt = request(host, "release.install", {"sha": SHA})
    assert receipt["outcome"] == "failed"
    assert "TypeError" in str(receipt.get("error"))
    assert calls == 1
    assert not ex.restart_requested


def test_confirm_release_without_watcher(host):
    paths, ex, _ = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    ex.now = lambda: t0
    ex._version = SHA
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False, now=t0)
    assert paths.release_pending.is_file()

    # One complete healthy step confirms the release
    assert ex.step() == 0
    assert not paths.release_pending.exists()
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["candidate_sha"] == SHA
    assert history[0]["status"] == "confirmed"
    assert history[0]["confirmed_at"] == "2026-11-13T10:00:00Z"


def test_confirm_release_with_watcher_requires_60s_healthy(host):
    paths, ex, _ = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=True, now=t0)

    # Step 1 at t=0: watcher active, but 0s elapsed -> not confirmed yet
    ex.step()
    assert paths.release_pending.is_file()
    assert len(h.read_release_history(paths)) == 0

    # Step 2 at t=30s: 30s elapsed -> not confirmed yet
    current_time[0] = t0 + timedelta(seconds=30)
    ex.step()
    assert paths.release_pending.is_file()
    assert len(h.read_release_history(paths)) == 0

    # Step 3 at t=65s: >= 60s elapsed with no restarts -> confirmed!
    current_time[0] = t0 + timedelta(seconds=65)
    ex.step()
    assert not paths.release_pending.exists()
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["candidate_sha"] == SHA
    assert history[0]["status"] == "confirmed"
    assert history[0]["confirmed_at"] == "2026-11-13T10:01:05Z"


def test_confirm_release_watcher_restarts_resets_60s_timer(host):
    paths, ex, _ = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=True, now=t0)

    watcher_restarts = [0]

    def run(argv, **kw):
        if "jarvisos-relay-watch.service" in argv:
            return subprocess.CompletedProcess(argv, 0, f"ActiveState=active\nNRestarts={watcher_restarts[0]}\n", "")
        return subprocess.CompletedProcess(argv, 0, "ActiveState=active\n", "")

    ex.run = run

    # Step 1 at t=0
    ex.step()
    assert paths.release_pending.is_file()

    # Watcher restarts at t=40s
    current_time[0] = t0 + timedelta(seconds=40)
    watcher_restarts[0] = 1
    ex.step()
    assert paths.release_pending.is_file()

    # Step at t=65s (only 25s since restart at t=40) -> not confirmed
    current_time[0] = t0 + timedelta(seconds=65)
    ex.step()
    assert paths.release_pending.is_file()
    assert len(h.read_release_history(paths)) == 0

    # Step at t=105s (65s since restart at t=40) -> confirmed!
    current_time[0] = t0 + timedelta(seconds=105)
    ex.step()
    assert not paths.release_pending.exists()
    assert len(h.read_release_history(paths)) == 1


def test_confirm_release_watcher_inactive_fails_confirmation(host):
    paths, ex, _ = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=True, now=t0)

    ex.run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "ActiveState=failed\n", "")

    current_time[0] = t0 + timedelta(seconds=100)
    ex.step()
    # Candidate records failure and requests restart only; current and history untouched
    assert paths.release_pending.is_file()
    marker = h.load_release_pending(paths)
    assert "watcher" in marker.get("unhealthy_reason", "")
    assert ex.restart_requested is True
    assert len(h.read_release_history(paths)) == 0

    # Next start's guard() performs return
    assert h.guard(paths, now=current_time[0]) == 0
    assert not paths.release_pending.exists()
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["status"] == "refused"
    assert "watcher" in history[0]["reason"]


def test_confirm_release_heartbeat_problem_fails_confirmation(host):
    paths, ex, _ = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    ex.now = lambda: t0
    ex._version = SHA
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False, now=t0)

    # Missing signing key -> heartbeat problem
    paths.key.unlink()
    ex.step()
    assert paths.release_pending.is_file()
    marker = h.load_release_pending(paths)
    assert "signing key missing" in marker.get("unhealthy_reason", "")
    assert ex.restart_requested is True
    assert len(h.read_release_history(paths)) == 0

    assert h.guard(paths, now=t0) == 0
    assert not paths.release_pending.exists()
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["status"] == "refused"
    assert "signing key missing" in history[0]["reason"]


def test_confirm_release_version_mismatch_fails_confirmation(host):
    paths, ex, _ = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    ex.now = lambda: t0
    candidate_b = "b" * 40
    ex._version = SHA  # Running SHA, but candidate is SHA_B
    h.write_release_pending(paths, previous_sha=None, candidate_sha=candidate_b,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False, now=t0)

    ex.step()
    assert paths.release_pending.is_file()
    marker = h.load_release_pending(paths)
    assert "version mismatch" in marker.get("unhealthy_reason", "")
    assert ex.restart_requested is True
    assert len(h.read_release_history(paths)) == 0

    assert h.guard(paths, now=t0) == 0
    assert not paths.release_pending.exists()
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["status"] == "refused"
    assert "version mismatch" in history[0]["reason"]


def test_confirm_release_deadline_expired_fails_confirmation(host):
    paths, ex, _ = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    ex.now = lambda: t0
    ex._version = SHA
    # Deadline in the past
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 - timedelta(seconds=1)),
                            request_id="req1", attempts=0, watcher_active=False, now=t0 - timedelta(minutes=10))

    ex.step()
    assert paths.release_pending.is_file()
    marker = h.load_release_pending(paths)
    assert "deadline expired" in marker.get("unhealthy_reason", "")
    assert ex.restart_requested is True
    assert len(h.read_release_history(paths)) == 0

    assert h.guard(paths, now=t0) == 0
    assert not paths.release_pending.exists()
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["status"] == "refused"
    assert "deadline expired" in history[0]["reason"]


def test_confirm_release_systemd_active_enter_timestamp(host):
    paths, ex, _ = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=True, now=t0)

    def run(argv, **kw):
        if "jarvisos-relay-watch.service" in argv:
            return subprocess.CompletedProcess(argv, 0, "ActiveState=active\nActiveEnterTimestamp=Fri 2026-11-13 10:00:00 UTC\n", "")
        return subprocess.CompletedProcess(argv, 0, "ActiveState=active\n", "")

    ex.run = run

    # At 10:00:30 UTC: only 30s since ActiveEnterTimestamp -> not confirmed
    current_time[0] = t0 + timedelta(seconds=30)
    ex.step()
    assert paths.release_pending.is_file()

    # At 10:01:05 UTC: 65s since ActiveEnterTimestamp -> confirmed!
    current_time[0] = t0 + timedelta(seconds=65)
    ex.step()
    assert not paths.release_pending.exists()
    assert len(h.read_release_history(paths)) == 1


def test_confirm_release_systemd_active_enter_timestamp_non_utc(host):
    paths, ex, _ = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=True, now=t0)

    # 10:00:00 UTC is 11:00:00 CET (UTC+1)
    def run(argv, **kw):
        if "jarvisos-relay-watch.service" in argv:
            return subprocess.CompletedProcess(argv, 0, "ActiveState=active\nActiveEnterTimestamp=Fri 2026-11-13 11:00:00 CET\n", "")
        return subprocess.CompletedProcess(argv, 0, "ActiveState=active\n", "")

    ex.run = run

    # At 10:00:10 UTC (only 10s elapsed): must not confirm early
    current_time[0] = t0 + timedelta(seconds=10)
    ex.step()
    assert paths.release_pending.is_file()
    assert len(h.read_release_history(paths)) == 0

    # At 10:00:30 UTC: only 30s elapsed -> not confirmed
    current_time[0] = t0 + timedelta(seconds=30)
    ex.step()
    assert paths.release_pending.is_file()
    assert len(h.read_release_history(paths)) == 0

    # At 10:01:05 UTC: 65s since start -> confirmed!
    current_time[0] = t0 + timedelta(seconds=65)
    ex.step()
    assert not paths.release_pending.exists()
    assert len(h.read_release_history(paths)) == 1


def test_confirm_release_watcher_inactive_then_active_resets_timer(host):
    paths, ex, _ = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=True, now=t0)

    watcher_state = ["active"]

    def run(argv, **kw):
        if "jarvisos-relay-watch.service" in argv:
            return subprocess.CompletedProcess(argv, 0, f"ActiveState={watcher_state[0]}\n", "")
        return subprocess.CompletedProcess(argv, 0, "ActiveState=active\n", "")

    ex.run = run

    # Step 1 at t=0: active
    ex.step()
    assert paths.release_pending.is_file()
    marker = h.load_release_pending(paths)
    assert marker.get("watcher_seen_at") == "2026-11-13T10:00:00Z"

    # Step 2 at t=30s: watcher becomes inactive -> resets in-memory and durable marker
    current_time[0] = t0 + timedelta(seconds=30)
    watcher_state[0] = "inactive"
    ex.step()
    assert paths.release_pending.is_file()
    assert ex._watcher_seen_at is None
    marker = h.load_release_pending(paths)
    assert "watcher_seen_at" not in marker

    # Step 3 at t=40s: watcher becomes active again -> new timer starts from t=40s
    current_time[0] = t0 + timedelta(seconds=40)
    watcher_state[0] = "active"
    ex.step()
    assert paths.release_pending.is_file()
    marker = h.load_release_pending(paths)
    assert marker.get("watcher_seen_at") == "2026-11-13T10:00:40Z"

    # Step 4 at t=65s: 65s since t=0, but only 25s since reactivation at t=40s -> must not confirm!
    current_time[0] = t0 + timedelta(seconds=65)
    ex.step()
    assert paths.release_pending.is_file()
    assert len(h.read_release_history(paths)) == 0

    # Step 5 at t=105s: 65s since reactivation -> confirmed!
    current_time[0] = t0 + timedelta(seconds=105)
    ex.step()
    assert not paths.release_pending.exists()
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["candidate_sha"] == SHA


def test_confirm_release_watcher_restart_after_60s_resets_timer(host):
    paths, ex, _ = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=True, now=t0)

    restarts = [0]

    # ActiveEnterTimestamp left at t0 to ensure stale enter_ts does not bypass the reset
    def run(argv, **kw):
        if "jarvisos-relay-watch.service" in argv:
            return subprocess.CompletedProcess(
                argv, 0,
                f"ActiveState=active\nNRestarts={restarts[0]}\nActiveEnterTimestamp=Fri 2026-11-13 10:00:00 UTC\n",
                ""
            )
        return subprocess.CompletedProcess(argv, 0, "ActiveState=active\n", "")

    ex.run = run

    # Step 1 at t=0
    ex.step()
    assert paths.release_pending.is_file()

    # At t=65s, the 60s window elapsed, but watcher restarts right at t=65s!
    current_time[0] = t0 + timedelta(seconds=65)
    restarts[0] = 1
    ex.step()
    assert paths.release_pending.is_file()
    assert len(h.read_release_history(paths)) == 0
    marker = h.load_release_pending(paths)
    assert marker.get("watcher_seen_at") == "2026-11-13T10:01:05Z"
    assert marker.get("watcher_initial_restarts") == 1

    # At t=70s (only 5s after restart): must not confirm early even with stale enter_ts
    current_time[0] = t0 + timedelta(seconds=70)
    ex.step()
    assert paths.release_pending.is_file()
    assert len(h.read_release_history(paths)) == 0

    # Simulate executor process restart: fresh Executor instance
    ex2 = h.Executor(paths, run=run, now=lambda: current_time[0], version=SHA)
    current_time[0] = t0 + timedelta(seconds=75)
    ex2.step()
    assert paths.release_pending.is_file()
    assert len(h.read_release_history(paths)) == 0

    # At t=126s (61s after restart at t=65s): confirmed!
    current_time[0] = t0 + timedelta(seconds=126)
    ex2.step()
    assert not paths.release_pending.exists()
    assert len(h.read_release_history(paths)) == 1


def test_confirm_release_crash_recovery_window(host):
    paths, ex, _ = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    ex.now = lambda: t0
    ex._version = SHA
    marker = h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                                    deadline=h.iso(t0 + timedelta(minutes=10)),
                                    request_id="req1", attempts=0, watcher_active=False, now=t0)
    assert paths.release_pending.is_file()

    # Simulate crash after release-history.jsonl was appended and fsynced,
    # but before release-pending.json was unlinked
    entry = dict(marker)
    entry["status"] = "confirmed"
    entry["confirmed_at"] = h.iso(t0)
    with open(paths.release_history, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")

    assert len(h.read_release_history(paths)) == 1
    assert paths.release_pending.is_file()

    # On next step, maybe_confirm_release detects the pending release is already in history,
    # cleans up release-pending.json without duplicating the history record
    ex.step()
    assert not paths.release_pending.exists()
    history = h.read_release_history(paths)
    assert len(history) == 1


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
    monkeypatch.setattr(h, "install_release", lambda *args, **kw: None)
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


def fake_semantic(host, monkeypatch, *, failure=None, restore_failure=False, replies_override=None,
                  row_override=None):
    paths, ex, calls = host
    case = json.loads((SCRIPT.parents[1] / "eval/local/semantic_judgment/cases.json").read_text())["cases"][0]
    row = {"name": case["id"], "prompt": "secret semantic prompt " * 6, "category": case["category"],
           "answer": case["answer"]}
    if row_override:
        row.update(row_override)
        case = {**case, "id": row["name"]}
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
    def http(_opener, url, _remaining, *, data=None, token=None, cancelled=None):
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
            model = json.loads(paths.policy.read_text())["ops"]["local_eval.run"]["evaluations"]["semantic"]["model"]
            return {"data": [{"id": model}]}
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


def test_semantic_real_loopback_and_grader_stdout(host, monkeypatch):
    paths, ex, _ = host
    evaluator = SCRIPT.parents[1] / "eval/local/semantic_judgment"
    grader_spec = importlib.util.spec_from_file_location("semantic_e2e_grader", evaluator / "semantic_eval.py")
    grader = importlib.util.module_from_spec(grader_spec)
    assert grader_spec and grader_spec.loader
    grader_spec.loader.exec_module(grader)
    cases = grader.load_cases()
    entries = grader.export_entries(cases)
    answers = {entry["prompt"]: entry["answer"] for entry in entries}
    replies = {entry["name"]: [json.dumps(entry["answer"]) + " private-reply-marker"]
               for entry in entries}
    events = []
    state = {"default": True, "unit": False, "seen": {}}

    class Backend(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self):
            body = json.dumps({"runtime_reachable": state["default"],
                               "spawned_by_jarvis": state["default"],
                               "pid": 123 if state["default"] else None}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            assert self.path == "/local-ai/runtime/llama-cpp"
            self.respond()

        def do_POST(self):
            assert self.path in ("/local-ai/runtime/llama-cpp/stop", "/local-ai/runtime/llama-cpp/start")
            self.rfile.read(int(self.headers["Content-Length"]))
            state["default"] = self.path.endswith("/start")
            events.append("default_start" if state["default"] else "default_stop")
            self.respond()

    class Model(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            assert self.path == "/v1/models" and state["unit"] and not state["default"]
            assert self.headers["Authorization"] == "Bearer semantic-private-token"
            body = json.dumps({"data": [{"id": "candidate"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            assert self.path == "/v1/chat/completions" and state["unit"] and not state["default"]
            assert self.headers["Authorization"] == "Bearer semantic-private-token"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = body["messages"][0]["content"]
            assert prompt in answers
            state["seen"][prompt] = state["seen"].get(prompt, 0) + 1
            reply = json.dumps(answers[prompt]) + " private-reply-marker"
            payload = json.dumps({"choices": [{"message": {"content": reply}}],
                                  "usage": {"completion_tokens": 7}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    model = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (backend, model)]
    for thread in threads:
        thread.start()
    try:
        semantic_policy(paths, backend_port=backend.server_port,
                        base_url=f"http://127.0.0.1:{model.server_port}/v1",
                        attempts=1, temperature=0)
        monkeypatch.setattr(h, "_eval_reachable", lambda *_: None)
        def checkout(_sha, root, _remaining):
            target = root / "backend"
            target.mkdir()
            files = {}
            for name in ("semantic_eval.py", "cases.json"):
                data = (evaluator / name).read_bytes()
                (target / name).write_bytes(data)
                files[Path("backend") / name] = (len(data), h.hashlib.sha256(data).hexdigest())
            return files, {Path("backend")}
        monkeypatch.setattr(h, "_eval_checkout", checkout)
        real_popen = subprocess.Popen
        def popen(argv, **kwargs):
            if argv[0] == "systemd-run":
                command = argv[argv.index("--") + 1:]
                assert command[:3] == ["timeout", "-k", "15"]
                command = command[4:]
                assert command[1:4] == ["env", "-C", str(kwargs["cwd"])]
                script_args = command[5:] if command[4] == "PYTHONDONTWRITEBYTECODE=1" else command[4:]
                assert script_args[:2] == ["python", "semantic_eval.py"]
                events.append("grade" if "--grade" in script_args else "prompt")
                return real_popen([sys.executable, *script_args[1:]], **kwargs)
            return real_popen(argv, **kwargs)
        monkeypatch.setattr(h.subprocess, "Popen", popen)
        real_run = subprocess.run
        def run(argv, **kwargs):
            if argv[:3] == ["systemctl", "--user", "stop"] and argv[3].endswith(".scope"):
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            if argv[:3] == ["systemctl", "--user", "show"] and argv[3].endswith(".scope"):
                return subprocess.CompletedProcess(argv, 0, "ActiveState=inactive\nLoadState=not-found\n", "")
            return real_run(argv, **kwargs)
        monkeypatch.setattr(h.subprocess, "run", run)
        def unit_run(argv, **kwargs):
            if argv[2] in ("start", "stop"):
                state["unit"] = argv[2] == "start"
                events.append("unit_start" if state["unit"] else "unit_stop")
            return subprocess.CompletedProcess(argv, 0,
                                               f"ActiveState={'active' if state['unit'] else 'inactive'}\n", "")
        ex.run = unit_run
        _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
        assert receipt["outcome"] == "ok", receipt["error"]
        assert events[0:3] == ["prompt", "default_stop", "unit_start"]
        assert events[-3:] == ["unit_stop", "default_start", "grade"]
        assert len(state["seen"]) == len(entries) and all(count == 1 for count in state["seen"].values())
        expected = json.loads(real_run([sys.executable, str(evaluator / "semantic_eval.py"), "--grade",
                                        str(_write_semantic_replies(paths, replies))],
                                       check=True, capture_output=True, text=True).stdout)
        assert receipt["detail"]["categories"] == expected["categories"]
        for index, row in enumerate(receipt["detail"]["rows"]):
            assert row["case"] == f"case-{index + 1:02d}"
            grade = expected["cases"][entries[index]["name"]]
            assert row["score"] == grade["score"]
            assert row["attempts"][0]["score"] == grade["attempts"][0]["score"]
            assert {key: row["attempts"][0][key] for key in ("verdict", "options", "missing", "parse_error")} == {
                key: grade["attempts"][0][key] for key in ("verdict", "options", "missing", "parse_error")}
        encoded = json.dumps(receipt)
        assert "private-reply-marker" not in encoded
        assert "semantic-private-token" not in encoded
        assert all(entry["prompt"] not in encoded for entry in entries)
    finally:
        for server in (backend, model):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


def _write_semantic_replies(paths, replies):
    path = paths.home / "expected-semantic-replies.json"
    path.write_text(json.dumps(replies))
    return path


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
    assert result["case"] == "case-01" and result["score"] == 1
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


def test_semantic_reply_total_limit_restores_without_text(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    case = json.loads((SCRIPT.parents[1] / "eval/local/semantic_judgment/cases.json").read_text())["cases"][0]
    reply = json.dumps(case["answer"])
    events, _ = fake_semantic(host, monkeypatch, replies_override=[reply] * 3)
    monkeypatch.setattr(h, "SEMANTIC_REPLIES_MAX_BYTES", len(reply.encode("utf-8")) * 2 - 1)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert receipt["error"] == "local model replies exceeded their size limit"
    assert receipt["detail"]["restore"] == {"default": "ok", "server_unit": "ok"}
    assert events.count("model") == 2
    assert "grade" not in events
    assert reply not in json.dumps(receipt)


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
    assert events == ["default_stop", "default_start"]
    assert receipt["detail"]["restore"]["default"] == "ok"


@pytest.mark.parametrize("occupied", [
    {"spawned_by_jarvis": True, "pid": 123, "reason_code": "LLAMACPP_LOADING"},
    {"spawned_by_jarvis": True},
    {"pid": 123},
    {"reason_code": "LLAMACPP_LOADING"},
])
def test_semantic_unreachable_occupied_default_is_stopped_and_restored(host, monkeypatch, occupied):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = h._semantic_http
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/local-ai/runtime/llama-cpp"):
            if "default_start" in events:
                return {"runtime_reachable": True}
            return {"runtime_reachable": False, **occupied}
        return original(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "ok", receipt["error"]
    assert events.index("default_stop") < events.index("unit_start")
    assert events[-2:] == ["default_start", "grade"]


def test_semantic_recovering_default_is_refused_before_stop(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = h._semantic_http
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/local-ai/runtime/llama-cpp"):
            return {"runtime_reachable": False, "spawned_by_jarvis": True,
                    "pid": None, "reason_code": "LLAMACPP_RECOVERING"}
        return original(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert receipt["error"] == "default runtime is recovering"
    assert events == []


def test_semantic_stop_with_managed_process_still_present_restores(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = h._semantic_http
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/stop"):
            events.append("default_stop")
            return {"runtime_reachable": False, "spawned_by_jarvis": True, "pid": 123}
        return original(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert receipt["error"] == "default runtime stop was not confirmed"
    assert events == ["default_stop", "default_start"]
    assert receipt["detail"]["restore"]["default"] == "ok"


@pytest.mark.parametrize("field,value", [("spawned_by_jarvis", 1), ("pid", True), ("pid", "123")])
def test_semantic_invalid_default_status_types_fail_closed(host, monkeypatch, field, value):
    paths, _, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = h._semantic_http
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/local-ai/runtime/llama-cpp"):
            return {"runtime_reachable": False, field: value}
        return original(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert receipt["error"] == "default runtime status is invalid"
    assert events == []


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


def test_semantic_restore_ignores_cancellation(host, monkeypatch):
    paths, ex, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = h._semantic_http
    req = h.make_request("local_eval.run", {"bench": "semantic"}, SHA, "test", ex.now())
    h.submit(paths, req)
    restoring = False

    def http(opener, url, remaining, **kwargs):
        nonlocal restoring
        if url.endswith("/models"):
            (paths.requests / f"{req['id']}.cancel").write_bytes(b"")
            raise h.Cancelled({})
        if url.endswith("/start"):
            restoring = True
            assert "cancelled" not in kwargs
        if restoring and url.endswith("/local-ai/runtime/llama-cpp"):
            assert "cancelled" not in kwargs
        return original(opener, url, remaining, **kwargs)

    monkeypatch.setattr(h, "_semantic_http", http)
    ex.step()
    receipt, why = h.load_receipt(paths, req["id"])
    assert receipt, why
    assert receipt["outcome"] == "cancelled"
    assert receipt["detail"]["restore"]["default"] == "ok"
    assert events[-2:] == ["unit_stop", "default_start"]
    assert restoring


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


def test_semantic_default_stays_stopped_after_unit_stop_exhausts_budget(host, monkeypatch):
    paths, ex, calls = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    monkeypatch.setattr(h, "SEMANTIC_RESTORE_S", 0.01)
    original = ex.run
    def run(argv, **kwargs):
        if argv[:3] == ["systemctl", "--user", "stop"] and argv[3] == "candidate.service":
            events.append("unit_stop")
            time.sleep(0.02)
            raise h.Failed("semantic restore exceeded its time limit", {})
        return original(argv, **kwargs)
    ex.run = run
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert receipt["error"] == "semantic evaluation restore failed"
    assert events[-1] == "unit_stop"
    assert "default_start" not in events
    assert receipt["detail"]["restore"] == {"default": "failed", "server_unit": "failed"}


def test_semantic_failed_unit_stop_does_not_start_default(host, monkeypatch):
    paths, ex, _ = host
    semantic_policy(paths)
    events, _ = fake_semantic(host, monkeypatch)
    original = ex.run
    def run(argv, **kwargs):
        if argv[:4] == ["systemctl", "--user", "stop", "candidate.service"]:
            events.append("unit_stop_failed")
            return subprocess.CompletedProcess(argv, 1, "", "")
        return original(argv, **kwargs)
    ex.run = run
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert receipt["error"] == "semantic evaluation restore failed"
    assert "unit_stop_failed" in events
    assert "default_start" not in events
    assert receipt["detail"]["restore"] == {"default": "failed", "server_unit": "failed"}


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


def test_semantic_without_unit_waits_for_model(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths, server_unit=None)
    events, _ = fake_semantic(host, monkeypatch)
    original_http = h._semantic_http
    seen = {"readiness": 0}
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/models"):
            seen["readiness"] += 1
            assert kwargs["token"] == "semantic-private-token"
            if seen["readiness"] == 1:
                raise h.Failed("local HTTP returned status 503", {})
            if seen["readiness"] == 2:
                return {"data": [{"id": "other-model"}]}
        return original_http(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    monkeypatch.setattr(h.time, "sleep", lambda _: None)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "ok", receipt["error"]
    assert seen["readiness"] == 3
    assert receipt["detail"]["errors"] == 2
    assert events == ["default_stop", "ready", "model", "model", "model", "default_start", "grade"]
    assert receipt["detail"]["restore"] == {"default": "ok", "server_unit": "not_needed"}


def test_semantic_without_unit_never_ready_restores_without_prompt(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths, server_unit=None, timeout_s=1)
    events, _ = fake_semantic(host, monkeypatch)
    original_http = h._semantic_http
    original_sleep = time.sleep
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/models"):
            events.append("ready")
            return {"data": [{"id": "other-model"}]}
        return original_http(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    monkeypatch.setattr(h.time, "sleep", lambda _: original_sleep(1.05))
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert "model" not in events
    assert events[0:2] == ["default_stop", "ready"]
    assert events[-1] == "default_start"
    assert receipt["detail"]["restore"] == {"default": "ok", "server_unit": "not_needed"}


def test_semantic_empty_models_response_never_sends_prompt(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths, timeout_s=1)
    events, _ = fake_semantic(host, monkeypatch)
    original_http = h._semantic_http
    original_sleep = time.sleep
    def http(opener, url, remaining, **kwargs):
        if url.endswith("/models"):
            events.append("ready")
            return {}
        return original_http(opener, url, remaining, **kwargs)
    monkeypatch.setattr(h, "_semantic_http", http)
    monkeypatch.setattr(h.time, "sleep", lambda _: original_sleep(1.05))
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert "model" not in events
    assert events[-2:] == ["unit_stop", "default_start"]
    assert receipt["detail"]["restore"] == {"default": "ok", "server_unit": "ok"}


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
    assert row["case"] == "case-01"
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


@pytest.mark.parametrize("target", ["attempts", "majority"])
def test_semantic_rejects_prompt_excerpt_in_grader_option(host, monkeypatch, target):
    paths, _, _ = host
    semantic_policy(paths)
    prompt = "A" * 100
    excerpt = prompt[:31]
    events, case = fake_semantic(host, monkeypatch, row_override={"prompt": prompt})
    original = h._run_benchmark
    def bench(*args, **kwargs):
        result = original(*args, **kwargs)
        if "--grade" in args[2]["argv"]:
            grade = result["result"]["cases"][case["id"]]
            (grade["attempts"][0] if target == "attempts" else grade["majority"])["options"] = [excerpt]
        return result
    monkeypatch.setattr(h, "_run_benchmark", bench)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert receipt["error"] == "semantic grading failed"
    assert excerpt not in json.dumps(receipt)
    assert receipt["detail"]["restore"] == {"default": "ok", "server_unit": "ok"}
    assert events.index("unit_stop") < events.index("default_start") < events.index("grade")


@pytest.mark.parametrize("field", ["options", "missing"])
def test_semantic_rejects_duplicate_grader_letter(host, monkeypatch, field):
    paths, _, _ = host
    semantic_policy(paths)
    events, case = fake_semantic(host, monkeypatch)
    original = h._run_benchmark
    def bench(*args, **kwargs):
        result = original(*args, **kwargs)
        if "--grade" in args[2]["argv"]:
            result["result"]["cases"][case["id"]]["attempts"][0][field] = ["A", "A"]
        return result
    monkeypatch.setattr(h, "_run_benchmark", bench)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert receipt["error"] == "semantic grading failed"
    assert receipt["detail"]["restore"] == {"default": "ok", "server_unit": "ok"}
    assert events.index("unit_stop") < events.index("default_start") < events.index("grade")


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


def test_semantic_refuses_prompt_as_name(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    fake_semantic(host, monkeypatch)
    original = h._run_benchmark
    def bench(*args, **kwargs):
        result = original(*args, **kwargs)
        if "--emit-prompts" in args[2]["argv"]:
            result["result"][0].update(name="short_prompt", prompt="short_prompt")
        return result
    monkeypatch.setattr(h, "_run_benchmark", bench)
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert receipt["detail"] == {}


def test_semantic_receipt_omits_exporter_name_and_prompt_slice(host, monkeypatch):
    paths, _, _ = host
    semantic_policy(paths)
    prompt = "A" * 100
    fake_semantic(host, monkeypatch, row_override={"name": prompt[:80], "prompt": prompt})
    _, receipt = request(host, "local_eval.run", {"bench": "semantic"}, sha=SHA)
    assert receipt["outcome"] == "ok", receipt["error"]
    assert receipt["detail"]["rows"][0]["case"] == "case-01"
    encoded = json.dumps(receipt, ensure_ascii=False)
    assert all(prompt[index:index + 32] not in encoded for index in range(len(prompt) - 31))


def test_semantic_result_check_refuses_prompt_slice():
    prompt = "A" * 100
    with pytest.raises(h.Failed, match="semantic evaluation result is invalid"):
        h._semantic_check_result({"case": prompt[:32]}, "secret-token", [{"prompt": prompt}], {},
                                 {"default": "not_needed", "server_unit": "not_needed"})


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
    opener = h.urllib.request.build_opener(h.urllib.request.ProxyHandler({}), h._RejectRedirects(),
                                           h._SemanticHTTPHandler())
    h._semantic_http(opener, "http://127.0.0.1:18080/local-ai/runtime/llama-cpp/stop",
                     lambda: 5, data=b"{}")
    assert seen == [("http://127.0.0.1:18080/local-ai/runtime/llama-cpp/stop", "POST", 5)]
    assert isinstance(handlers[0], h.urllib.request.ProxyHandler)
    assert handlers[0].proxies == {}
    assert isinstance(handlers[1], h._RejectRedirects)
    assert isinstance(handlers[2], h._SemanticHTTPHandler)


@pytest.mark.parametrize("cancel", [False, True])
def test_semantic_http_connect_stall_is_bounded(monkeypatch, cancel):
    monkeypatch.setattr(h, "SEMANTIC_CONNECT_S", 0.05)
    timeouts = []
    def connect(_address, timeout, source_address=None):
        timeouts.append(timeout)
        time.sleep(timeout)
        raise h.socket.timeout("connect timed out")
    monkeypatch.setattr(h.socket, "create_connection", connect)
    opener = h.urllib.request.build_opener(h.urllib.request.ProxyHandler({}), h._RejectRedirects(),
                                           h._SemanticHTTPHandler())
    started = time.monotonic()
    with pytest.raises(h.Cancelled if cancel else h.Failed,
                       match=None if cancel else "local HTTP endpoint is unavailable"):
        h._semantic_http(opener, "http://127.0.0.1:18080/models", lambda: 2,
                         cancelled=(lambda: time.monotonic() - started >= 0.02) if cancel else None)
    assert time.monotonic() - started < 0.5
    assert timeouts == [pytest.approx(h.SEMANTIC_CONNECT_S)]


def test_semantic_http_connect_restores_socket_timeout(monkeypatch):
    timeouts = []
    class Socket:
        def settimeout(self, value):
            timeouts.append(value)
    def connect(self):
        timeouts.append(self.timeout)
        self.sock = Socket()
    monkeypatch.setattr(h.http.client.HTTPConnection, "connect", connect)
    handler = h._SemanticHTTPHandler()
    def open_connection(connection, req):
        instance = connection("127.0.0.1", timeout=30)
        instance.connect()
        assert instance.timeout == 30
        assert req._semantic_socket is instance.sock
    monkeypatch.setattr(handler, "do_open", open_connection)
    handler.http_open(h.urllib.request.Request("http://127.0.0.1:18080/models"))
    assert timeouts == [h.SEMANTIC_CONNECT_S, 30]


def test_semantic_http_deadline_covers_dribbling_headers():
    stopped = threading.Event()

    class Model(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            while not stopped.is_set():
                try:
                    self.connection.sendall(b"x")
                except OSError:
                    return
                stopped.wait(0.2)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        opener = h.urllib.request.build_opener(h.urllib.request.ProxyHandler({}), h._RejectRedirects(),
                                               h._SemanticHTTPHandler())
        end = time.monotonic() + 1

        def deadline():
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise h.Failed("local evaluation exceeded its time limit", {})
            return remaining

        started = time.monotonic()
        with pytest.raises(h.Failed, match="local evaluation exceeded its time limit"):
            h._semantic_http(opener, f"http://127.0.0.1:{server.server_port}/models", deadline)
        assert time.monotonic() - started < 2
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_semantic_http_deadline_closes_connection_and_joins_watchdog():
    closed = threading.Event()

    class Model(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            if self.connection.recv(1) == b"":
                closed.set()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        opener = h.urllib.request.build_opener(h.urllib.request.ProxyHandler({}), h._RejectRedirects(),
                                               h._SemanticHTTPHandler())
        baseline = threading.active_count()
        end = time.monotonic() + 0.4

        def deadline():
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise h.Failed("local evaluation exceeded its time limit", {})
            return remaining

        with pytest.raises(h.Failed, match="local evaluation exceeded its time limit"):
            h._semantic_http(opener, f"http://127.0.0.1:{server.server_port}/models", deadline)
        assert closed.wait(1)
        until = time.monotonic() + 1
        while threading.active_count() != baseline and time.monotonic() < until:
            time.sleep(0.01)
        assert threading.active_count() == baseline
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_semantic_http_cancels_stalled_request():
    closed = threading.Event()

    class Model(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            if self.connection.recv(1) == b"":
                closed.set()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        opener = h.urllib.request.build_opener(h.urllib.request.ProxyHandler({}), h._RejectRedirects(),
                                               h._SemanticHTTPHandler())
        started = time.monotonic()
        with pytest.raises(h.Cancelled):
            h._semantic_http(opener, f"http://127.0.0.1:{server.server_port}/models", lambda: 2,
                             cancelled=lambda: time.monotonic() - started >= 0.3)
        assert time.monotonic() - started < 1.5
        assert closed.wait(1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_semantic_http_deadline_covers_blocked_open():
    class Opener:
        def open(self, _req, *, timeout):
            assert timeout > 0
            time.sleep(timeout)
            raise OSError("timed out")

    end = time.monotonic() + 0.1

    def deadline():
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise h.Failed("local evaluation exceeded its time limit", {})
        return remaining

    with pytest.raises(h.Failed, match="local evaluation exceeded its time limit"):
        h._semantic_http(Opener(), "http://127.0.0.1:18080/models", deadline)


# ---------------------------------------------------------------------------
# Scope Item 3 and Findings 4 & 5 (Spec 262): Guard, Automatic Return, Refused Prevention
# ---------------------------------------------------------------------------


def test_service_unit_has_exec_start_pre_guard():
    assert "ExecStartPre=/usr/bin/python3 -I %h/.local/share/jarvisos-hostops/guard/scripts/host_ops.py guard" in h.SERVICE_UNIT


def test_load_release_pending_malformed_fails_closed(tmp_path):
    paths = h.Paths(tmp_path)
    assert h.load_release_pending(paths) is None

    # Unparseable JSON
    paths.state.mkdir(parents=True, exist_ok=True)
    paths.release_pending.write_bytes(b"{not-valid-json")
    with pytest.raises(h.MalformedPendingRelease, match="unparseable JSON"):
        h.load_release_pending(paths)

    # JSON not an object
    paths.release_pending.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(h.MalformedPendingRelease, match="not a JSON object"):
        h.load_release_pending(paths)

    # Missing candidate_sha
    paths.release_pending.write_text(json.dumps({"previous_sha": "a" * 40}))
    with pytest.raises(h.MalformedPendingRelease, match="missing valid candidate_sha"):
        h.load_release_pending(paths)

    # Invalid candidate_sha
    paths.release_pending.write_text(json.dumps({"candidate_sha": "not-a-full-sha"}))
    with pytest.raises(h.MalformedPendingRelease, match="missing valid candidate_sha"):
        h.load_release_pending(paths)


def test_install_release_refuses_when_pending_malformed(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    mock_install(monkeypatch, release_archive())
    paths.state.mkdir(parents=True, exist_ok=True)
    paths.release_pending.write_bytes(b"corrupt-json{")

    with pytest.raises(h.Refused, match="malformed pending release marker exists"):
        h.install_release(paths, SHA)


def test_maybe_confirm_release_malformed_pending_quarantines_and_returns(host):
    paths, ex, _ = host
    paths.state.mkdir(parents=True, exist_ok=True)
    paths.release_pending.write_bytes(b"corrupt-json{")
    (paths.install / "releases" / SHA / "scripts").mkdir(parents=True, exist_ok=True)
    (paths.install / "current").symlink_to(paths.install / "releases" / SHA)

    notices = []
    ex.post = lambda text, _: notices.append(text) or 200

    confirmed = ex.maybe_confirm_release()
    assert confirmed is False
    # Candidate requests restart only; does not quarantine or write history
    assert paths.release_pending.exists()
    assert ex.restart_requested is True
    assert len(notices) == 0
    assert len(h.read_release_history(paths)) == 0

    # Guard on next start handles corrupt marker
    rc = h.guard(paths, post=lambda text, _: notices.append(text) or 200)
    assert rc == 0
    assert not paths.release_pending.exists()

    # Corrupt marker was quarantined
    quarantined = list(paths.state.glob("release-pending.corrupt.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"corrupt-json{"

    # Refusal recorded in history
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["status"] == "refused"
    assert "corrupt pending marker" in history[0]["reason"]
    assert history[0]["notified"] is True


def test_guard_malformed_pending_quarantines_and_returns(tmp_path):
    paths = h.Paths(tmp_path)
    paths.state.mkdir(parents=True, exist_ok=True)
    paths.release_pending.write_bytes(b"corrupt-data")

    notices = []
    rc = h.guard(paths, post=lambda text, _: notices.append(text) or 200)
    assert rc == 0
    assert not paths.release_pending.exists()
    quarantined = list(paths.state.glob("release-pending.corrupt.*"))
    assert len(quarantined) == 1
    assert len(h.read_release_history(paths)) == 1


def test_guard_start_count_exceeded_returns_and_restores(tmp_path):
    paths = h.Paths(tmp_path)
    old_sha = "1" * 40
    candidate_sha = "2" * 40

    old_scripts = paths.install / "releases" / old_sha / "scripts"
    old_scripts.mkdir(parents=True)
    (old_scripts / "host_ops.py").write_text("# old release\n")
    cand_scripts = paths.install / "releases" / candidate_sha / "scripts"
    cand_scripts.mkdir(parents=True)
    (cand_scripts / "host_ops.py").write_text("# candidate release\n")

    # Current currently points to candidate
    (paths.install / "current").symlink_to(paths.install / "releases" / candidate_sha)

    # Pinned guard copy points to old_sha
    h._pin_guard(paths, paths.install / "releases" / old_sha, old_sha)
    assert (paths.guard / "RELEASE").read_text().strip() == old_sha

    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    h.write_release_pending(paths, previous_sha=old_sha, candidate_sha=candidate_sha,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=True, now=t0)

    systemctl_calls = []
    notices = []

    def mock_run(argv, timeout=60):
        systemctl_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def mock_post(text, _):
        notices.append(text)
        return 200

    # Starts 1, 2, 3: attempts increment, no return yet
    assert h.guard(paths, run=mock_run, post=mock_post, now=t0) == 0
    assert h.load_release_pending(paths)["attempts"] == 1
    assert (paths.install / "current").resolve() == paths.install / "releases" / candidate_sha
    assert len(systemctl_calls) == 0
    assert len(notices) == 0

    assert h.guard(paths, run=mock_run, post=mock_post, now=t0) == 0
    assert h.load_release_pending(paths)["attempts"] == 2

    assert h.guard(paths, run=mock_run, post=mock_post, now=t0) == 0
    assert h.load_release_pending(paths)["attempts"] == 3

    # Start 4: attempts become 4 > 3 -> Automatic Return triggers!
    assert h.guard(paths, run=mock_run, post=mock_post, now=t0) == 0

    # 1. current symlink moved back to previous release
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha

    # 2. watcher service restarted
    assert ["systemctl", "--user", "restart", "jarvisos-relay-watch.service"] in systemctl_calls

    # 3. Exactly one notice sent
    assert len(notices) == 1
    assert candidate_sha[:8] in notices[0]
    assert "start count exceeded" in notices[0]

    # 4. Refused candidate recorded in release history
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["candidate_sha"] == candidate_sha
    assert history[0]["status"] == "refused"
    assert "start count exceeded (4)" in history[0]["reason"]
    assert history[0]["notified"] is True

    # 5. Pending marker written for old_sha so old_sha confirms itself on start
    marker_restored = h.load_release_pending(paths)
    assert marker_restored is not None
    assert marker_restored["candidate_sha"] == old_sha

    # Restored previous release runs step and confirms itself
    ex_old = h.Executor(paths, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "ActiveState=active\nActiveEnterTimestamp=2026-11-13 10:00:05 UTC\nNRestarts=0\n", ""),
                        post=mock_post, now=lambda: t0 + timedelta(seconds=70), version=old_sha)
    paths.config.mkdir(parents=True, exist_ok=True)
    paths.policy.write_text(json.dumps(h.DEFAULT_POLICY))
    paths.policy.chmod(0o600)
    paths.key.write_bytes(b"mock-key")
    paths.key.chmod(0o600)
    (paths.spool).mkdir(parents=True, exist_ok=True)
    # Write healthy heartbeat for old_sha
    fd = h.open_dir(paths.spool)
    try:
        h.write_at(fd, "heartbeat.json", h.canonical({"at": h.iso(t0 + timedelta(seconds=70)), "version": old_sha, "problem": None}))
    finally:
        os.close(fd)
    assert ex_old.maybe_confirm_release() is True

    # Marker is now unlinked and confirmed in history
    assert not paths.release_pending.exists()
    final_history = h.read_release_history(paths)
    non_pruned = [e for e in final_history if e.get("status") != "pruned"]
    assert len(non_pruned) == 2
    assert non_pruned[0]["candidate_sha"] == candidate_sha
    assert non_pruned[0]["status"] == "refused"
    assert non_pruned[1]["candidate_sha"] == old_sha
    assert non_pruned[1]["status"] == "confirmed"

    # Exactly one notification was sent throughout
    assert len(notices) == 1


def test_guard_deadline_expired_returns(tmp_path):
    paths = h.Paths(tmp_path)
    old_sha = "1" * 40
    candidate_sha = "2" * 40
    (paths.install / "releases" / old_sha / "scripts").mkdir(parents=True)
    (paths.install / "releases" / candidate_sha / "scripts").mkdir(parents=True)
    (paths.install / "current").symlink_to(paths.install / "releases" / candidate_sha)
    h._pin_guard(paths, paths.install / "releases" / old_sha, old_sha)

    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    # Deadline 10 minutes from t0
    h.write_release_pending(paths, previous_sha=old_sha, candidate_sha=candidate_sha,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False, now=t0)

    notices = []
    # 11 minutes later: deadline expired
    rc = h.guard(paths, post=lambda text, _: notices.append(text) or 200, now=t0 + timedelta(minutes=11))
    assert rc == 0
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["status"] == "refused"
    assert history[0]["reason"] == "deadline expired"
    assert len(notices) == 1


def test_notify_return_sent_only_once_per_candidate(tmp_path):
    paths = h.Paths(tmp_path)
    candidate_sha = "3" * 40

    sent = []

    def poster(text, _):
        sent.append(text)
        return 200

    # First notification succeeds
    assert h.notify_return(paths, candidate_sha, "unhealthy", post=poster) is True
    assert len(sent) == 1

    # Record in history that it was notified
    state_fd = h.open_dir(paths.state, create=True)
    try:
        with open(paths.release_history, "a", encoding="utf-8") as f:
            f.write(json.dumps({"candidate_sha": candidate_sha, "notified": True, "status": "refused"}) + "\n")
    finally:
        os.close(state_fd)

    # Second notification is suppressed
    assert h.notify_return(paths, candidate_sha, "unhealthy", post=poster) is False
    assert len(sent) == 1


def test_refused_candidate_cannot_be_retried_automatically(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    refused_sha = "4" * 40
    newer_sha = "5" * 40

    mock_install(monkeypatch, release_archive())
    state_fd = h.open_dir(paths.state, create=True)
    try:
        with open(paths.release_history, "a", encoding="utf-8") as f:
            f.write(json.dumps({"candidate_sha": refused_sha, "status": "refused", "reason": "deadline expired"}) + "\n")
    finally:
        os.close(state_fd)

    # Attempting to install refused_sha is refused
    with pytest.raises(h.Refused, match=rf"candidate {refused_sha} was previously refused \(deadline expired\)"):
        h.install_release(paths, refused_sha)

    # Attempting to install newer_sha proceeds past refusal check
    h.install_release(paths, newer_sha)
    assert (paths.install / "current").resolve() == paths.install / "releases" / newer_sha


def test_install_release_pins_guard_copy(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    sha_a = "a" * 40
    sha_b = "b" * 40

    mock_install(monkeypatch, release_archive())
    h.install_release(paths, sha_a)

    # First install pins candidate sha_a
    assert paths.guard.is_dir()
    assert (paths.guard / "RELEASE").read_text().strip() == sha_a
    assert (paths.guard / "scripts" / "host_ops.py").is_file()

    # Confirm sha_a
    paths.release_pending.unlink()

    # Second install of sha_b pins outgoing release sha_a to guard
    mock_install(monkeypatch, release_archive(), status="ahead")
    h.install_release(paths, sha_b)
    assert (paths.guard / "RELEASE").read_text().strip() == sha_a


def test_guard_cli_dispatch(tmp_path, capsys):
    paths = h.Paths(tmp_path)
    assert h.main(["--home", str(tmp_path), "guard"]) == 0

    # Force return via CLI
    old_sha = "1" * 40
    candidate_sha = "2" * 40
    (paths.install / "releases" / old_sha / "scripts").mkdir(parents=True)
    (paths.install / "releases" / candidate_sha / "scripts").mkdir(parents=True)
    (paths.install / "current").symlink_to(paths.install / "releases" / candidate_sha)
    h._pin_guard(paths, paths.install / "releases" / old_sha, old_sha)

    # Force return with no pending marker is refused and returns 2
    rc = h.main(["--home", str(tmp_path), "guard", "--return", "--reason", "cli manual return"])
    assert rc == 2
    assert "refus" in capsys.readouterr().err.lower()
    assert len(h.read_release_history(paths)) == 0

    # With pending marker, force return succeeds
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    h.write_release_pending(paths, previous_sha=old_sha, candidate_sha=candidate_sha,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False, now=t0)
    rc = h.main(["--home", str(tmp_path), "guard", "--return", "--reason", "cli manual return"])
    assert rc == 0
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["status"] == "refused"
    assert history[0]["reason"] == "cli manual return"


def test_candidate_unhealthy_records_reason_guard_returns(tmp_path):
    paths = h.Paths(tmp_path)
    old_sha = "1" * 40
    candidate_sha = "2" * 40

    old_scripts = paths.install / "releases" / old_sha / "scripts"
    old_scripts.mkdir(parents=True)
    (old_scripts / "host_ops.py").write_text("# old release\n")
    cand_scripts = paths.install / "releases" / candidate_sha / "scripts"
    cand_scripts.mkdir(parents=True)
    (cand_scripts / "host_ops.py").write_text("# candidate release\n")

    (paths.install / "current").symlink_to(paths.install / "releases" / candidate_sha)
    h._pin_guard(paths, paths.install / "releases" / old_sha, old_sha)

    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    h.write_release_pending(paths, previous_sha=old_sha, candidate_sha=candidate_sha,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False, now=t0)

    # Candidate executor runs and finds itself unhealthy
    ex = h.Executor(paths, now=lambda: t0)
    ex._version = candidate_sha
    paths.key.unlink(missing_ok=True)
    ex.step()

    # Candidate only recorded failure: marker carries reason, current unchanged
    assert paths.release_pending.is_file()
    marker = h.load_release_pending(paths)
    assert marker["unhealthy_reason"] == "unhealthy heartbeat: signing key missing"
    assert (paths.install / "current").resolve() == paths.install / "releases" / candidate_sha
    assert ex.restart_requested is True
    assert len(h.read_release_history(paths)) == 0

    # Then next start's guard() runs from previous copy
    notices = []
    rc = h.guard(paths, post=lambda text, _: notices.append(text) or 200, now=t0)
    assert rc == 0
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha
    assert len(notices) == 1
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["status"] == "refused"
    assert history[0]["candidate_sha"] == candidate_sha
    assert history[0]["previous_sha"] == old_sha
    assert "signing key missing" in history[0]["reason"]
    assert history[0]["notified"] is True


def test_return_confirmation_failure_never_marks_previous_refused(tmp_path):
    paths = h.Paths(tmp_path)
    old_sha = "1" * 40
    old_scripts = paths.install / "releases" / old_sha / "scripts"
    old_scripts.mkdir(parents=True)
    (old_scripts / "host_ops.py").write_text("# old release\n")
    (paths.install / "current").symlink_to(paths.install / "releases" / old_sha)

    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    # Return pending marker with kind: "return"
    h.write_release_pending(paths, previous_sha=None, candidate_sha=old_sha,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id=None, attempts=0, watcher_active=False, now=t0, kind="return")

    # If restored release fails confirmation
    ex = h.Executor(paths, now=lambda: t0)
    ex._version = old_sha
    paths.key.unlink(missing_ok=True)
    ex.step()

    assert ex.restart_requested is True
    assert paths.release_pending.is_file()
    marker = h.load_release_pending(paths)
    assert marker.get("kind") == "return"
    assert "unhealthy_reason" in marker

    notices = []
    rc = h.guard(paths, post=lambda text, _: notices.append(text) or 200, now=t0)
    assert rc == 0
    assert not paths.release_pending.exists()  # marker cleared!
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha  # current kept where it is!
    assert len(notices) == 1  # notify once!
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["status"] == "return_unconfirmed"  # status return_unconfirmed, NEVER refused!
    assert history[0]["candidate_sha"] == old_sha

    # Verify history entry does not block candidate from being installed
    for h_entry in h.read_release_history(paths):
        assert h_entry.get("status") not in ("refused", "returned")


def test_guard_never_fails_unit_start_on_injected_exception(tmp_path, monkeypatch, capsys):
    paths = h.Paths(tmp_path)
    some_sha = "3" * 40
    (paths.install / "releases" / some_sha / "scripts").mkdir(parents=True)
    (paths.install / "current").symlink_to(paths.install / "releases" / some_sha)

    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    h.write_release_pending(paths, previous_sha=None, candidate_sha=some_sha,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id=None, attempts=0, watcher_active=False, now=t0)

    # Injected exception inside guard
    def fail(*_):
        raise RuntimeError("simulated catastrophe")

    monkeypatch.setattr(h, "load_release_pending", fail)

    rc = h.guard(paths)
    assert rc == 0  # must NEVER fail unit start
    assert (paths.install / "current").resolve() == paths.install / "releases" / some_sha  # current stays as is
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "simulated catastrophe" in err


def test_pin_guard_atomic_and_contains_previous_release_without_candidate_code(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    sha_a = "a" * 40
    sha_b = "b" * 40

    def mock_archive_with_text(text: str) -> bytes:
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            for name in ("host_ops.py", "relay_continuity.py", "relay_argon.py", "relay_jobs.py", "relay_observe.py", "devctx.py", "worker_routing.py"):
                data = f'# {name}\nPAYLOAD = {text!r}\n'.encode() if name == "host_ops.py" else b"# script\n"
                info = tarfile.TarInfo(f"repo/scripts/{name}")
                info.type = tarfile.REGTYPE
                info.size = len(data)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(data))
        return out.getvalue()

    # Install release A
    mock_install(monkeypatch, mock_archive_with_text("CODE_A"))
    h.install_release(paths, sha_a)
    assert (paths.guard / "RELEASE").read_text().strip() == sha_a
    assert "CODE_A" in (paths.guard / "scripts" / "host_ops.py").read_text()

    # Confirm release A
    paths.release_pending.unlink(missing_ok=True)

    # Install release B (candidate)
    mock_install(monkeypatch, mock_archive_with_text("CODE_B"), status="ahead")
    h.install_release(paths, sha_b)

    # guard/RELEASE must be previous sha (sha_a)
    assert (paths.guard / "RELEASE").read_text().strip() == sha_a
    # Guard copy has previous release code and NO candidate code
    guard_code = (paths.guard / "scripts" / "host_ops.py").read_text()
    assert "CODE_A" in guard_code
    assert "CODE_B" not in guard_code

    # Test atomic replacement of existing guard directory
    tmp_source = tmp_path / "new_source"
    tmp_source.mkdir()
    (tmp_source / "marker.txt").write_text("hello\n")
    h._pin_guard(paths, tmp_source, "c" * 40)
    assert (paths.guard / "RELEASE").read_text().strip() == "c" * 40
    assert (paths.guard / "marker.txt").read_text() == "hello\n"
    assert len(list(paths.install.glob(".guard*"))) == 0


def test_guard_return_without_pending_marker_refused(tmp_path, capsys):
    paths = h.Paths(tmp_path)
    confirmed_sha = "5" * 40
    (paths.install / "releases" / confirmed_sha / "scripts").mkdir(parents=True)
    (paths.install / "current").symlink_to(paths.install / "releases" / confirmed_sha)

    assert not paths.release_pending.exists()

    # guard --return without pending marker must print refusal and return 2
    rc = h.guard(paths, force_return=True)
    assert rc == 2
    err = capsys.readouterr().err
    assert "refus" in err.lower()
    # Confirmed release is NOT marked refused in history
    assert len(h.read_release_history(paths)) == 0
    # Current is untouched
    assert (paths.install / "current").resolve() == paths.install / "releases" / confirmed_sha


def test_guard_force_return_successful_exit_status_and_cli(tmp_path, capsys):
    paths = h.Paths(tmp_path)
    old_sha = "1" * 40
    candidate_sha = "2" * 40
    (paths.install / "releases" / old_sha / "scripts").mkdir(parents=True)
    (paths.install / "releases" / candidate_sha / "scripts").mkdir(parents=True)
    (paths.install / "current").symlink_to(paths.install / "releases" / candidate_sha)
    h._pin_guard(paths, paths.install / "releases" / old_sha, old_sha)

    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    h.write_release_pending(paths, previous_sha=old_sha, candidate_sha=candidate_sha,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False, now=t0)

    # Direct guard call with force_return=True must return int 0 on success (never bool True / exit 1)
    rc = h.guard(paths, force_return=True, reason="manual test return", now=t0)
    assert type(rc) is int
    assert rc == 0
    # Current moved back to old release
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["status"] == "refused"
    assert history[0]["reason"] == "manual test return"

    # Subsequent force_return without pending marker returns 2 (refusal)
    # Clear any return confirmation marker written for old_sha
    paths.release_pending.unlink(missing_ok=True)
    rc_refused = h.guard(paths, force_return=True)
    assert type(rc_refused) is int
    assert rc_refused == 2
    assert "refus" in capsys.readouterr().err.lower()

    # CLI test: with pending marker, CLI status must be exit 0
    candidate_b = "3" * 40
    (paths.install / "releases" / candidate_b / "scripts").mkdir(parents=True)
    (paths.install / "current").unlink(missing_ok=True)
    (paths.install / "current").symlink_to(paths.install / "releases" / candidate_b)
    h.write_release_pending(paths, previous_sha=old_sha, candidate_sha=candidate_b,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req2", attempts=0, watcher_active=False, now=t0)

    rc_cli = h.main(["--home", str(tmp_path), "guard", "--return", "--reason", "cli manual return"])
    assert type(rc_cli) is int
    assert rc_cli == 0
    assert (paths.install / "current").resolve() == paths.install / "releases" / old_sha

    # CLI test: without pending marker, CLI status must be exit 2
    paths.release_pending.unlink(missing_ok=True)
    rc_cli_refused = h.main(["--home", str(tmp_path), "guard", "--return"])
    assert type(rc_cli_refused) is int
    assert rc_cli_refused == 2
    assert "refus" in capsys.readouterr().err.lower()


def test_pin_guard_replacement_failure_restores_usable_old_guard(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    old_sha = "a" * 40
    candidate_sha = "b" * 40

    old_source = tmp_path / "old_release"
    old_source.mkdir()
    (old_source / "RELEASE").write_text(f"{old_sha}\n")
    (old_source / "host_ops.py").write_text("# old guard code\n")

    # Initial pin of old guard
    h._pin_guard(paths, old_source, old_sha)
    assert paths.guard.is_dir()
    assert (paths.guard / "RELEASE").read_text().strip() == old_sha
    assert (paths.guard / "host_ops.py").read_text() == "# old guard code\n"

    candidate_source = tmp_path / "cand_release"
    candidate_source.mkdir()
    (candidate_source / "RELEASE").write_text(f"{candidate_sha}\n")
    (candidate_source / "host_ops.py").write_text("# candidate code\n")

    real_rename = os.rename

    def faulty_rename(src, dst):
        # Fail only when replacing guard_dir with the candidate's tmp_guard
        s_src = str(src)
        s_dst = str(dst)
        if s_dst == str(paths.guard) and ".guard-" in s_src and ".guard-old-" not in s_src:
            raise OSError("injected replacement rename failure")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", faulty_rename)

    with pytest.raises(OSError, match="injected replacement rename failure"):
        h._pin_guard(paths, candidate_source, candidate_sha)

    # Safe previous-release guard is restored and usable
    assert paths.guard.is_dir()
    assert not paths.guard.is_symlink()
    assert (paths.guard / "RELEASE").read_text().strip() == old_sha
    assert (paths.guard / "host_ops.py").read_text() == "# old guard code\n"

    # Temporary directories cleaned up; no leaked .guard-*
    leftover = list(paths.install.glob(".guard*"))
    assert len(leftover) == 0


def test_pin_guard_restore_failure_preserves_old_aside(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    old_sha = "a" * 40
    candidate_sha = "b" * 40

    old_source = tmp_path / "old_release"
    old_source.mkdir()
    (old_source / "RELEASE").write_text(f"{old_sha}\n")
    (old_source / "host_ops.py").write_text("# safe previous code\n")

    h._pin_guard(paths, old_source, old_sha)

    candidate_source = tmp_path / "cand_release"
    candidate_source.mkdir()
    (candidate_source / "RELEASE").write_text(f"{candidate_sha}\n")
    (candidate_source / "host_ops.py").write_text("# candidate code\n")

    real_rename = os.rename

    def always_fail_to_guard(src, dst):
        if str(dst) == str(paths.guard):
            raise OSError("cannot rename to guard")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", always_fail_to_guard)

    with pytest.raises(OSError, match="cannot rename to guard"):
        h._pin_guard(paths, candidate_source, candidate_sha)

    # guard_dir could not be renamed back: it points at the kept old copy, which cleanup must not delete
    old_asides = list(paths.install.glob(".guard-old-*"))
    assert len(old_asides) == 1
    assert paths.guard.is_symlink() and paths.guard.resolve() == old_asides[0].resolve()
    assert (old_asides[0] / "RELEASE").read_text().strip() == old_sha
    assert (old_asides[0] / "host_ops.py").read_text() == "# safe previous code\n"
    # Candidate tmp_guard was cleaned up
    cand_tmps = [p for p in paths.install.glob(".guard-*") if ".guard-old-" not in p.name]
    assert len(cand_tmps) == 0


# ---------------------------------------------------------------------------
# Spec 262 Scope Item 4: Owner from release
# ---------------------------------------------------------------------------


def test_unit_templates_contains_mission_owner():
    assert "jarvisos-mission-owner" in h.UNIT_TEMPLATES
    template = h.UNIT_TEMPLATES["jarvisos-mission-owner"]
    assert "WorkingDirectory=%h/src/JarvisOS" in template
    assert "%h/.local/share/jarvisos/hostops/current/scripts/relay_continuity.py serve --interval 60" in template
    assert "jarvisos-mission-owner" in h.DEFAULT_POLICY["ops"]["service.install"]["templates"]
    assert h.DEFAULT_POLICY["ops"]["service.control"]["actions"] == {"jarvisos-mission-owner.service": ["start"]}


def test_publish_release_creates_readonly_scripts_and_symlink(tmp_path):
    paths = h.Paths(tmp_path)
    sha = "b" * 40
    scripts = paths.install / "releases" / sha / "scripts"
    scripts.mkdir(parents=True)
    exe_file = scripts / "script_exec.py"
    exe_file.write_text("#!/usr/bin/env python3\nprint('hello')\n")
    exe_file.chmod(0o755)
    doc_file = scripts / "script_doc.py"
    doc_file.write_text("# just docs\n")
    doc_file.chmod(0o644)

    published_release = h.publish_release(paths, sha)
    assert published_release == paths.published / "releases" / sha
    assert (paths.published / "releases" / sha / "scripts" / "script_exec.py").is_file()
    assert (paths.published / "releases" / sha / "scripts" / "script_doc.py").is_file()

    # Files are read-only: no write bits
    assert not (paths.published / "releases" / sha / "scripts" / "script_exec.py").stat().st_mode & 0o222
    assert not (paths.published / "releases" / sha / "scripts" / "script_doc.py").stat().st_mode & 0o222
    # Executable bit preserved
    assert (paths.published / "releases" / sha / "scripts" / "script_exec.py").stat().st_mode & 0o111
    assert not (paths.published / "releases" / sha / "scripts" / "script_doc.py").stat().st_mode & 0o111

    # Current symlink points to releases/sha
    current = paths.published / "current"
    assert current.is_symlink()
    assert current.resolve() == (paths.published / "releases" / sha).resolve()


def test_return_published_release_switches_symlink(tmp_path):
    paths = h.Paths(tmp_path)
    sha1 = "1" * 40
    sha2 = "2" * 40
    for s in (sha1, sha2):
        d = paths.install / "releases" / s / "scripts"
        d.mkdir(parents=True)
        (d / "host_ops.py").write_text("# code\n")
        h.publish_release(paths, s)

    assert paths.published / "current"
    assert (paths.published / "current").resolve().name == sha2

    assert h.return_published_release(paths, sha1)
    assert (paths.published / "current").resolve().name == sha1


def test_confirm_release_starts_owner_and_confirms_when_owner_reports_sha(host):
    paths, ex, calls = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False,
                            owner_active=True, now=t0)
    assert paths.release_pending.is_file()

    # Step 1: executor healthy, starts owner
    ex.step()
    # Marker was updated with owner_started=True, but pending marker remains because owner hasn't reported yet
    assert paths.release_pending.is_file()
    marker = h.load_release_pending(paths)
    assert marker.get("owner_started") is True
    # Verify systemctl start jarvisos-mission-owner.service was called
    start_calls = [c for c in calls if "start" in c[0] and "jarvisos-mission-owner.service" in c[0]]
    assert len(start_calls) >= 1

    # Owner reports release SHA in its serve state
    paths.owner_serve_state.parent.mkdir(parents=True, exist_ok=True)
    paths.owner_serve_state.write_text(json.dumps({"release_sha": SHA, "release_updated_at": h.iso(t0)}))

    # Step 2: executor checks owner serve state and confirms
    ex.step()
    assert not paths.release_pending.exists()
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["candidate_sha"] == SHA
    assert history[0]["status"] == "confirmed"
    assert "owner_reported_at" in history[0]


def test_confirm_release_owner_timeout_returns_published_link_and_restarts_owner(host):
    paths, ex, calls = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    prev_sha = "9" * 40
    # Set up published releases for prev_sha and SHA
    for s in (prev_sha, SHA):
        d = paths.install / "releases" / s / "scripts"
        d.mkdir(parents=True)
        (d / "host_ops.py").write_text("# code\n")
        h.publish_release(paths, s)

    assert (paths.published / "current").resolve().name == SHA

    h.write_release_pending(paths, previous_sha=prev_sha, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False,
                            owner_active=True, now=t0)

    # Step 1 at t=0: starts owner
    ex.step()
    assert paths.release_pending.is_file()

    # Advance past deadline without owner reporting SHA
    current_time[0] = t0 + timedelta(minutes=11)
    calls.clear()

    # Step 2 past deadline: timeout return!
    ex.step()
    assert not paths.release_pending.exists()
    # Published current returned to previous release
    assert (paths.published / "current").resolve().name == prev_sha
    assert (paths.install / "current").resolve().name == prev_sha
    assert ex.restart_requested is True
    # Owner was started again on previous release
    restart_calls = [c for c in calls if "start" in c[0] and "jarvisos-mission-owner.service" in c[0]]
    assert len(restart_calls) >= 1
    # History records refusal
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["candidate_sha"] == SHA
    assert history[0]["status"] == "refused"
    assert "owner release sha not reported within deadline" in history[0]["reason"]
    assert history[0]["notified"] is True


def test_owner_timeout_returns_executor_and_watcher_to_previous_release(host):
    paths, ex, calls = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    prev_sha = "9" * 40
    for s in (prev_sha, SHA):
        d = paths.install / "releases" / s / "scripts"
        d.mkdir(parents=True)
        (d / "host_ops.py").write_text("# code\n")
        h.publish_release(paths, s)

    h.write_release_pending(paths, previous_sha=prev_sha, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=True,
                            owner_active=True, now=t0)

    # Step 1 at t=0
    ex.step()
    assert paths.release_pending.is_file()

    # Step 2 past deadline
    current_time[0] = t0 + timedelta(minutes=11)
    calls.clear()
    ex.step()

    # Check recovery: published, install, watcher, owner, executor restart
    assert not paths.release_pending.exists()
    assert (paths.published / "current").resolve().name == prev_sha
    assert (paths.install / "current").resolve().name == prev_sha
    assert ex.restart_requested is True

    watcher_restarts = [c for c in calls if "restart" in c[0] and "jarvisos-relay-watch.service" in c[0]]
    assert len(watcher_restarts) >= 1

    owner_starts = [c for c in calls if "start" in c[0] and "jarvisos-mission-owner.service" in c[0]]
    assert len(owner_starts) >= 1

    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["candidate_sha"] == SHA
    assert history[0]["status"] == "refused"
    assert history[0]["notified"] is True


def test_owner_timeout_guards_against_unsafe_restart_when_previous_missing(host):
    paths, ex, calls = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    prev_sha = "9" * 40

    # Only create candidate release, but NOT previous_sha directory
    d = paths.install / "releases" / SHA / "scripts"
    d.mkdir(parents=True)
    (d / "host_ops.py").write_text("# code\n")
    h.publish_release(paths, SHA)

    h.write_release_pending(paths, previous_sha=prev_sha, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False,
                            owner_active=True, now=t0)

    ex.step()
    current_time[0] = t0 + timedelta(minutes=11)
    calls.clear()
    ex.step()

    # Previous release cannot be restored, so install/current is unlinked
    assert not (paths.install / "current").exists()
    # Executor MUST NOT request restart unsafely into missing release
    assert ex.restart_requested is False


def test_owner_timeout_guards_against_repeated_notification(host):
    paths, ex, calls = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    prev_sha = "9" * 40

    notices = []
    ex.post = lambda text, _: notices.append(text) or 200

    for s in (prev_sha, SHA):
        d = paths.install / "releases" / s / "scripts"
        d.mkdir(parents=True)
        (d / "host_ops.py").write_text("# code\n")
        h.publish_release(paths, s)

    h.write_release_pending(paths, previous_sha=prev_sha, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False,
                            owner_active=True, now=t0)

    ex.step()
    current_time[0] = t0 + timedelta(minutes=11)
    ex.step()

    assert len(notices) == 1

    # Simulate another return or step attempting notification for same candidate
    notified_again = h.notify_return(paths, SHA, "another return attempt", post=ex.post)
    assert notified_again is False
    assert len(notices) == 1


def test_release_install_refusal_restarts_owner(host):
    paths, ex, calls = host
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["release.install"]["trust_master"] = False
    paths.policy.write_text(json.dumps(policy))

    # Request submitted by owner
    req = h.make_request("release.install", {"sha": SHA}, None, "relay-continuity:owner", ex.now())
    h.submit(paths, req)
    calls.clear()
    ex.step()

    receipt, _ = h.load_receipt(paths, req["id"])
    assert receipt["outcome"] == "refused"
    # Executor started owner on current release
    start_calls = [c for c in calls if "start" in c[0] and "jarvisos-mission-owner.service" in c[0]]
    assert len(start_calls) >= 1


def test_release_install_failure_restarts_owner(host, monkeypatch):
    paths, ex, calls = host
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["release.install"]["trust_master"] = True
    paths.policy.write_text(json.dumps(policy))

    def fail_install(*args, **kw):
        raise TypeError("simulated install error")

    monkeypatch.setattr(h, "install_release", fail_install)

    req = h.make_request("release.install", {"sha": SHA}, None, "relay-continuity:owner", ex.now())
    h.submit(paths, req)
    calls.clear()
    ex.step()

    receipt, _ = h.load_receipt(paths, req["id"])
    assert receipt["outcome"] == "failed"
    # Executor started owner on current release
    start_calls = [c for c in calls if "start" in c[0] and "jarvisos-mission-owner.service" in c[0]]
    assert len(start_calls) >= 1


def test_return_release_returns_published_link_and_starts_owner(tmp_path):
    paths = h.Paths(tmp_path)
    prev_sha = "8" * 40
    cand_sha = "9" * 40
    for s in (prev_sha, cand_sha):
        d = paths.install / "releases" / s / "scripts"
        d.mkdir(parents=True)
        (d / "host_ops.py").write_text("# code\n")
        h.publish_release(paths, s)

    assert (paths.published / "current").resolve().name == cand_sha

    marker = {
        "candidate_sha": cand_sha,
        "previous_sha": prev_sha,
        "deadline": h.iso(datetime.now(UTC) + timedelta(minutes=10)),
        "owner_active": True,
        "watcher_active": False,
    }

    calls = []
    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert h.return_release(paths, marker=marker, reason="crash loop", run=run)
    assert (paths.published / "current").resolve().name == prev_sha
    start_calls = [c for c in calls if "start" in c[0] and "jarvisos-mission-owner.service" in c[0]]
    assert len(start_calls) >= 1


def test_install_release_failure_after_publication_restores_links(tmp_path, monkeypatch):
    paths = h.Paths(tmp_path)
    sha_prev = "8" * 40
    sha_cand = "9" * 40

    # Set up previous installed release
    d_prev = paths.install / "releases" / sha_prev / "scripts"
    d_prev.mkdir(parents=True)
    (d_prev / "host_ops.py").write_text("# code\n")
    h.publish_release(paths, sha_prev)
    tmp_link = paths.install / ".current.prev"
    os.symlink(paths.install / "releases" / sha_prev, tmp_link)
    os.replace(tmp_link, paths.install / "current")
    (paths.guard / "RELEASE").parent.mkdir(parents=True, exist_ok=True)
    (paths.guard / "RELEASE").write_text(f"{sha_prev}\n", encoding="utf-8")

    assert (paths.published / "current").resolve().name == sha_prev
    assert (paths.install / "current").resolve().name == sha_prev

    rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "backend"}]}}]
    checks = [{"name": "backend", "status": "completed", "conclusion": "success", "id": 1, "completed_at": "2026-01-01T00:00:00Z"}]
    mock_install(monkeypatch, release_archive(), rules=rules, checks=checks)

    # Simulate failure during install/current switch (after publication stage has populated files)
    def fail_switch(p, sha):
        raise OSError("simulated disk error during link switch")

    monkeypatch.setattr(h, "switch_install_release", fail_switch)

    with pytest.raises(OSError, match="simulated disk error"):
        h.install_release(paths, sha_cand)

    # Links must be restored to sha_prev; neither may point to sha_cand
    assert (paths.published / "current").resolve().name == sha_prev
    assert (paths.install / "current").resolve().name == sha_prev
    assert not paths.release_pending.exists()


def test_op_release_install_watcher_restart_failure_restores_published_link_and_restarts_owner(host, monkeypatch):
    paths, ex, calls = host
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["release.install"]["trust_master"] = True
    paths.policy.write_text(json.dumps(policy))

    sha_prev = "8" * 40
    sha_cand = "9" * 40

    # Set up previous release
    d_prev = paths.install / "releases" / sha_prev / "scripts"
    d_prev.mkdir(parents=True)
    (d_prev / "host_ops.py").write_text("# code\n")
    h.publish_release(paths, sha_prev)
    tmp_link = paths.install / ".current.prev"
    os.symlink(paths.install / "releases" / sha_prev, tmp_link)
    os.replace(tmp_link, paths.install / "current")

    # Mock install_release to simulate successful install (switching published & install to candidate)
    def mock_install_fn(p, sha, **kw):
        d_cand = p.install / "releases" / sha / "scripts"
        d_cand.mkdir(parents=True, exist_ok=True)
        (d_cand / "host_ops.py").write_text("# cand\n")
        h.publish_release(p, sha)
        t_link = p.install / ".current.cand"
        os.symlink(d_cand.parent, t_link)
        os.replace(t_link, p.install / "current")

    monkeypatch.setattr(h, "install_release", mock_install_fn)

    # Systemctl returns 1 on watcher restart, 0 on other commands
    def custom_run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(
            argv, 1 if "restart" in argv and "jarvisos-relay-watch.service" in argv else 0,
            "ActiveState=active\n", ""
        )

    ex.run = custom_run

    req = h.make_request("release.install", {"sha": sha_cand}, None, "relay-continuity:owner", ex.now())
    h.submit(paths, req)
    calls.clear()
    ex.step()

    receipt, _ = h.load_receipt(paths, req["id"])
    assert receipt["outcome"] == "failed"
    assert "watcher restart failed" in str(receipt.get("error"))

    # Published current was restored to previous release (owner never runs candidate on failed install)
    assert (paths.published / "current").resolve().name == sha_prev
    # Executor restart requested so candidate can fail health check under guard
    assert ex.restart_requested

    # Owner was restarted on previous release
    start_calls = [c for c in calls if "start" in c[0] and "jarvisos-mission-owner.service" in c[0]]
    assert len(start_calls) >= 1


def test_maybe_confirm_release_stale_matching_sha_does_not_confirm(host):
    paths, ex, calls = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False,
                            owner_active=True, now=t0)

    # Old state file from previous attempt has updated_at before owner_started_at
    paths.owner_serve_state.parent.mkdir(parents=True, exist_ok=True)
    paths.owner_serve_state.write_text(json.dumps({
        "release_sha": SHA,
        "release_updated_at": h.iso(t0 - timedelta(seconds=10)),
    }))

    # Step: starts owner, but stale state must not confirm
    ex.step()
    assert paths.release_pending.is_file()
    marker = h.load_release_pending(paths)
    assert marker.get("owner_started") is True
    assert "owner_reported_at" not in marker
    assert len(h.read_release_history(paths)) == 0


def test_maybe_confirm_release_fresh_report_confirms(host):
    paths, ex, calls = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False,
                            owner_active=True, now=t0)

    # Step 1: starts owner
    ex.step()
    assert paths.release_pending.is_file()

    # Owner reports after owner_started_at
    paths.owner_serve_state.parent.mkdir(parents=True, exist_ok=True)
    paths.owner_serve_state.write_text(json.dumps({
        "release_sha": SHA,
        "release_updated_at": h.iso(t0 + timedelta(seconds=2)),
    }))

    # Step 2: fresh report confirms release
    ex.step()
    assert not paths.release_pending.exists()
    history = h.read_release_history(paths)
    assert len(history) == 1
    assert history[0]["candidate_sha"] == SHA
    assert history[0]["status"] == "confirmed"


def test_maybe_confirm_release_malformed_owner_state_does_not_confirm(host):
    paths, ex, calls = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False,
                            owner_active=True, now=t0)
    paths.owner_serve_state.parent.mkdir(parents=True, exist_ok=True)

    # Case 1: unparseable JSON
    paths.owner_serve_state.write_text("{bad json: not parsed")
    ex.step()
    assert paths.release_pending.is_file()

    # Case 2: missing release_updated_at
    paths.owner_serve_state.write_text(json.dumps({"release_sha": SHA}))
    ex.step()
    assert paths.release_pending.is_file()

    # Case 3: malformed / unparseable timestamp
    paths.owner_serve_state.write_text(json.dumps({
        "release_sha": SHA,
        "release_updated_at": "not-a-valid-iso-timestamp",
    }))
    ex.step()
    assert paths.release_pending.is_file()
    assert len(h.read_release_history(paths)) == 0


def test_maybe_confirm_release_owner_start_failure_does_not_mark_started_or_confirm(host):
    paths, ex, calls = host
    t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)
    current_time = [t0]
    ex.now = lambda: current_time[0]
    ex._version = SHA
    h.write_release_pending(paths, previous_sha=None, candidate_sha=SHA,
                            deadline=h.iso(t0 + timedelta(minutes=10)),
                            request_id="req1", attempts=0, watcher_active=False,
                            owner_active=True, now=t0)

    # Owner start fails (returncode != 0)
    ex.run = lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1 if "start" in argv and "jarvisos-mission-owner.service" in argv else 0, "", ""
    )

    # Even if owner_serve_state has a matching fresh timestamp
    paths.owner_serve_state.parent.mkdir(parents=True, exist_ok=True)
    paths.owner_serve_state.write_text(json.dumps({
        "release_sha": SHA,
        "release_updated_at": h.iso(t0 + timedelta(seconds=2)),
    }))

    ex.step()
    assert paths.release_pending.is_file()
    marker = h.load_release_pending(paths)
    # Must NOT assume start success: owner_started is not marked True
    assert marker.get("owner_started") is not True
    assert "owner_started_at" not in marker
    assert len(h.read_release_history(paths)) == 0


def test_install_self_install_writes_owner_template_and_enables_trust_master(host, monkeypatch):
    paths, _, _ = host
    # Pre-populate custom policy
    custom_policy = json.loads(json.dumps(h.DEFAULT_POLICY))
    custom_policy["ops"]["slack.deliver"]["max_per_hour"] = 42
    custom_policy["custom_preserved_key"] = "preserved"
    paths.policy.write_text(json.dumps(custom_policy, indent=1, sort_keys=True) + "\n")
    paths.policy.chmod(0o600)

    calls = mock_install(monkeypatch, release_archive())
    res = h.install(paths, SHA, self_install=True)
    assert res == 0

    # Executor service unit carries ExecStartPre guard
    unit_content = (paths.units / h.UNIT).read_text()
    assert "ExecStartPre=/usr/bin/python3 -I %h/.local/share/jarvisos-hostops/guard/scripts/host_ops.py guard" in unit_content

    # Owner unit template written
    owner_unit = paths.units / "jarvisos-mission-owner.service"
    assert owner_unit.is_file()
    assert owner_unit.read_text() == h.UNIT_TEMPLATES["jarvisos-mission-owner"]

    # Policy updated atomically with mode 0600
    assert paths.policy.stat().st_mode & 0o777 == 0o600
    updated_policy = json.loads(paths.policy.read_text())
    assert updated_policy["ops"]["release.install"]["trust_master"] is True
    # Preserves other keys
    assert updated_policy["ops"]["slack.deliver"]["max_per_hour"] == 42
    assert updated_policy["custom_preserved_key"] == "preserved"

    # Must never stop or restart the mission owner
    assert not any("jarvisos-mission-owner.service" in " ".join(call) for call in calls)


def test_install_plain_leaves_trust_master_false(host, monkeypatch):
    paths, _, _ = host
    mock_install(monkeypatch, release_archive())
    res = h.install(paths, SHA, self_install=False)
    assert res == 0

    # Owner unit template is NOT written
    assert not (paths.units / "jarvisos-mission-owner.service").exists()

    # Policy has trust_master: False
    updated_policy = json.loads(paths.policy.read_text())
    assert updated_policy["ops"]["release.install"]["trust_master"] is False


def test_prune_releases_keeps_current_previous_and_guard_source(tmp_path):
    paths = h.Paths(tmp_path)
    sha_guard = "1" * 40
    sha_prev = "2" * 40
    sha_curr = "3" * 40
    sha_symlink = "4" * 40
    sha_old = "5" * 40

    # Setup directories in install and published
    for base in (paths.install / "releases", paths.published / "releases"):
        for s in (sha_guard, sha_prev, sha_curr, sha_symlink, sha_old):
            (base / s / "scripts").mkdir(parents=True, exist_ok=True)

    # Current symlinks point to sha_curr
    (paths.install / "current").symlink_to(paths.install / "releases" / sha_curr)
    (paths.published / "current").symlink_to(paths.published / "releases" / sha_curr)

    # Pinned guard copy points to sha_guard
    paths.guard.mkdir(parents=True, exist_ok=True)
    (paths.guard / "RELEASE").write_text(f"{sha_guard}\n")

    # Extra symlinks point to sha_symlink in both install and published
    (paths.install / "pinned-extra").symlink_to(paths.install / "releases" / sha_symlink)
    (paths.published / "pinned-extra").symlink_to(paths.published / "releases" / sha_symlink)

    # History records sha_curr with previous_sha = sha_prev
    paths.state.mkdir(parents=True, exist_ok=True)
    paths.release_history.write_text(
        json.dumps({"candidate_sha": sha_prev, "status": "confirmed", "created_at": "2026-01-01T00:00:00Z"}) + "\n"
        + json.dumps({"candidate_sha": sha_curr, "previous_sha": sha_prev, "status": "confirmed", "created_at": "2026-01-02T00:00:00Z"}) + "\n"
    )

    pruned = h.prune_releases(paths)
    pruned_shas = [p["sha"] for p in pruned]
    assert sha_old in pruned_shas
    assert sha_guard not in pruned_shas
    assert sha_prev not in pruned_shas
    assert sha_curr not in pruned_shas
    assert sha_symlink not in pruned_shas

    # Verify directory states
    assert not (paths.install / "releases" / sha_old).exists()
    assert not (paths.published / "releases" / sha_old).exists()
    assert (paths.install / "releases" / sha_guard).is_dir()
    assert (paths.install / "releases" / sha_prev).is_dir()
    assert (paths.install / "releases" / sha_curr).is_dir()
    assert (paths.install / "releases" / sha_symlink).is_dir()
    assert (paths.published / "releases" / sha_symlink).is_dir()

    # History has pruned records
    history = h.read_release_history(paths)
    pruned_records = [rec for rec in history if rec.get("status") == "pruned"]
    assert len(pruned_records) == 2  # one for install, one for published
    assert all(rec.get("sha") == sha_old for rec in pruned_records)
    # Synced to published
    pub_history = [json.loads(line) for line in (paths.published / "release-history.jsonl").read_text().splitlines()]
    assert any(rec.get("status") == "pruned" and rec.get("sha") == sha_old for rec in pub_history)


def test_prune_releases_does_nothing_while_marker_pending(tmp_path):
    paths = h.Paths(tmp_path)
    sha_old = "9" * 40
    (paths.install / "releases" / sha_old).mkdir(parents=True, exist_ok=True)
    paths.state.mkdir(parents=True, exist_ok=True)
    paths.release_pending.write_text(json.dumps({"candidate_sha": "a" * 40, "created_at": "2026-01-01T00:00:00Z"}))

    pruned = h.prune_releases(paths)
    assert pruned == []
    assert (paths.install / "releases" / sha_old).is_dir()
    assert not paths.release_history.exists()

