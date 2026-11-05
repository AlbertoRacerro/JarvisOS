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
from datetime import timedelta
from pathlib import Path

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


@pytest.mark.parametrize("dirty,head", [(" M x", SHA), ("?? x", SHA), ("", "b" * 40)])
def test_bench_clean_head_and_plain_launcher(host, dirty, head):
    paths, ex, _ = host
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, head if "rev-parse" in argv else dirty, "")

    ex.run = run
    assert request(host, "bench.run", {"bench": "relay-observe-tests", "worktree": "x"}, sha=SHA)[1]["outcome"] == "refused"
    assert all("--cwd" not in a and "--" not in a and k["cwd"] == str(paths.home / "src/worktrees/x") for a, k in calls)


def local_eval_policy(paths, *, base_url="http://127.0.0.1:18081/v1", credential=True):
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


def fake_prompt_phase(host, monkeypatch, prompts):
    paths, ex, _ = host
    checkout = paths.home / "src/worktrees/x/backend"
    checkout.mkdir(parents=True, exist_ok=True)
    ex.run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, SHA if "rev-parse" in argv else "", "")
    seen = {}

    class Child:
        returncode = 0

        def __init__(self, argv, **kw):
            seen["argv"] = argv
            seen["env"] = kw["env"]
            (checkout / "prompts.json").write_text(json.dumps(prompts))

        def poll(self):
            return self.returncode

    real_popen = subprocess.Popen
    monkeypatch.setattr(h.subprocess, "Popen", lambda argv, **kw: Child(argv, **kw)
                        if argv[0] == "systemd-run" else real_popen(argv, **kw))
    return seen


def test_local_eval_token_stays_on_host_and_receipt_is_scores_only(host, monkeypatch):
    paths, _, _ = host
    local_eval_policy(paths)
    prompts = [{"name": "case_one", "prompt": "test prompt", "correct": ["good"], "plausible": ["maybe"]}]
    seen = fake_prompt_phase(host, monkeypatch, prompts)
    requests = []

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, _):
            return json.dumps({"choices": [{"message": {"content": "[[point:good]] secret echo"}}]}).encode()

    class Opener:
        def open(self, req, timeout):
            requests.append((req, timeout))
            return Response()

    monkeypatch.setattr(h.urllib.request, "build_opener", lambda *args: Opener())
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval", "worktree": "x"}, sha=SHA)
    assert receipt["outcome"] == "ok"
    assert receipt["detail"] == {"model": "test-model", "base_url": "http://127.0.0.1:18081/v1",
                                 "head": SHA, "ids": "after", "tally": {"correct": 1, "plausible": 0,
                                 "wrong": 0, "none": 0}, "rows": [{"name": "case_one", "first": "good",
                                                                 "verdict": "correct", "seconds": 0.0}]}
    assert "test-local-model-credential" not in json.dumps(receipt)
    assert "test-local-model-credential" not in str(seen)
    assert requests[0][0].get_header("Authorization") == "Bearer test-local-model-credential"
    assert requests[0][1] == 30
    body = json.loads(requests[0][0].data)
    assert body["temperature"] == 0 and body["max_tokens"] == 220
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("url", ["http://example.com/v1", "https://127.0.0.1:18081/v1",
                                  "http://127.0.0.2:18081/v1", "http://localhost.evil:18081/v1"])
def test_local_eval_refuses_non_loopback(host, url):
    paths, _, _ = host
    local_eval_policy(paths, base_url=url)
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval", "worktree": "x"}, sha=SHA)
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
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval", "worktree": "x"}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert "credential" in receipt["error"]


@pytest.mark.parametrize("prompts", [[], [{"name": "x", "prompt": "p" * 16001, "correct": ["a"],
                                              "plausible": ["b"]}], [{"name": "x", "prompt": "p",
                                              "correct": ["a"], "plausible": ["a"]}],
                                     [{"name": "x", "prompt": "p", "correct": ["a"],
                                       "plausible": ["b"]}] * 51])
def test_local_eval_refuses_malformed_or_oversized_prompts(host, monkeypatch, prompts):
    paths, _, _ = host
    local_eval_policy(paths)
    fake_prompt_phase(host, monkeypatch, prompts)
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval", "worktree": "x"}, sha=SHA)
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
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval", "worktree": "x"}, sha=SHA)
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
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval", "worktree": "x"}, sha=SHA)
    assert receipt["outcome"] == "failed"
    assert receipt["error"] == "local model returned HTTP 401"
    assert "test-local-model-credential" not in json.dumps(receipt)


def test_local_eval_unregistered_evaluator_refused(host):
    paths, _, _ = host
    local_eval_policy(paths)
    _, receipt = request(host, "local_eval.run", {"bench": "other", "worktree": "x"}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert "not registered" in receipt["error"]


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


def test_bench_unreachable_commit_refused(host):
    paths, ex, _ = host
    local_eval_policy(paths)
    def run(argv, **kw):
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, SHA, "")
        if "merge-base" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "[]" if "gh" in argv else "", "")
    ex.run = run
    _, receipt = request(host, "local_eval.run", {"bench": "local-eval", "worktree": "x"}, sha=SHA)
    assert receipt["outcome"] == "refused"
    assert "not reachable" in receipt["error"]


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
