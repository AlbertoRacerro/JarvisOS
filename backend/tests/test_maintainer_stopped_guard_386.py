"""Spec 386: MAINTAINER-STOPPED-GUARD-1 tests.

Enforce fail-closed host-ops guard refusing automated restart of maintainer-stopped
local model services. Offline unit tests with fake runners and tmp_path only.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.modules.local_ai.runtime.host_start import StrataRuntimeController

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "host_ops.py"
spec = importlib.util.spec_from_file_location("host_ops", SCRIPT)
assert spec and spec.loader
h = sys.modules.get("host_ops") or importlib.util.module_from_spec(spec)
if "host_ops" not in sys.modules:
    sys.modules["host_ops"] = h
    spec.loader.exec_module(h)


def setup_key(paths: h.Paths) -> None:
    paths.config.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(paths.key)], check=True)
    paths.published.mkdir(parents=True, exist_ok=True)
    pub = Path(f"{paths.key}.pub").read_text()
    paths.allowed_signers.write_text(f'{h.SIGNER} namespaces="{h.SIG_NAMESPACE}" {pub}')


@pytest.fixture
def host(tmp_path: Path):
    paths = h.Paths(tmp_path)
    setup_key(paths)
    policy = copy.deepcopy(h.DEFAULT_POLICY)
    policy["ops"]["service.control"]["units"].extend([
        "jarvisos-strata-sage.service",
        "jarvisos-llama-3b.service",
        "custom-model.service",
    ])
    policy["ops"]["service.control"]["local_model_units"] = ["custom-model.service"]
    paths.policy.write_text(json.dumps(policy))
    paths.policy.chmod(0o600)
    calls: list[tuple[list[str], dict]] = []

    current_states = {
        "jarvisos-strata-sage.service": "active",
        "jarvisos-llama-3b.service": "active",
        "custom-model.service": "active",
        "jarvisos-searxng.service": "active",
    }

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if len(argv) >= 4 and argv[0] == "systemctl" and argv[2] == "show":
            unit = argv[3]
            state = current_states.get(unit, "active")
            return subprocess.CompletedProcess(argv, 0, f"ActiveState={state}\n", "")
        if len(argv) >= 4 and argv[0] == "systemctl":
            action = argv[2]
            unit = argv[3]
            if action == "stop":
                current_states[unit] = "inactive"
            elif action in ("start", "restart"):
                current_states[unit] = "active"
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "ActiveState=active\n", "")

    executor = h.Executor(paths, run=run, post=lambda text, _: 200, log=lambda _: None)
    return paths, executor, calls


def submit_control_request(host, unit: str, action: str, requester: str = "maintainer", **args_extra):
    paths, ex, _ = host
    args = {"unit": unit, "action": action, **args_extra}
    req = h.make_request("service.control", args, None, requester, ex.now())
    h.submit(paths, req)
    ex.step()
    receipt, why = h.load_receipt(paths, req["id"])
    assert receipt is not None, f"Receipt load failed: {why}"
    return req, receipt


# -----------------------------------------------------------------------------
# Helper function tests
# -----------------------------------------------------------------------------


def test_is_local_model_unit() -> None:
    assert h.is_local_model_unit("jarvisos-strata-sage.service") is True
    assert h.is_local_model_unit("jarvisos-strata-deepseek-r1.service") is True
    assert h.is_local_model_unit("jarvisos-llama-3b.service") is True
    assert h.is_local_model_unit("jarvisos-searxng.service") is False
    assert h.is_local_model_unit("jarvisos-mission-owner.service") is False
    assert h.is_local_model_unit("custom-model.service", {"local_model_units": ["custom-model.service"]}) is True
    assert h.is_local_model_unit("custom-model.service", {"local_model_units": []}) is False
    assert h.is_local_model_unit("custom-model.service", None) is False
    assert h.is_local_model_unit(123) is False  # type: ignore[arg-type]
    assert h.is_local_model_unit(None) is False  # type: ignore[arg-type]


def test_is_maintainer_requester() -> None:
    assert h.is_maintainer_requester("maintainer") is True
    assert h.is_maintainer_requester("MAINTAINER") is True
    assert h.is_maintainer_requester("maintainer-chat") is True
    assert h.is_maintainer_requester("Maintainer-Chat") is True
    assert h.is_maintainer_requester("supervisor") is False
    assert h.is_maintainer_requester("agent") is False
    assert h.is_maintainer_requester(None) is False
    assert h.is_maintainer_requester("supervisor", {"maintainer": True}) is True
    assert h.is_maintainer_requester(None, {"maintainer": True}) is True
    assert h.is_maintainer_requester("supervisor", {"maintainer": False}) is False
    assert h.is_maintainer_requester("supervisor", {"maintainer": "true"}) is False


# -----------------------------------------------------------------------------
# Acceptance Criterion 1: Maintainer stop -> automated start/restart refused
# -----------------------------------------------------------------------------


def test_maintainer_stop_refuses_automated_start_and_restart(host) -> None:
    paths, _, calls = host
    unit = "jarvisos-strata-sage.service"

    # Maintainer stops the local model unit
    _, receipt = submit_control_request(host, unit, "stop", requester="maintainer", reason="free GPU")
    assert receipt["outcome"] == "ok"
    assert paths.maintainer_stopped_units.is_file()

    holds = json.loads(paths.maintainer_stopped_units.read_text(encoding="utf-8"))
    assert unit in holds
    assert holds[unit]["requester"] == "maintainer"
    assert holds[unit]["reason"] == "free GPU"
    assert "stopped_at" in holds[unit]

    call_count_after_stop = len(calls)

    # Automated start must be refused WITHOUT invoking systemctl
    _, start_receipt = submit_control_request(host, unit, "start", requester="supervisor")
    assert start_receipt["outcome"] == "refused"
    assert start_receipt["error"] == "unit stopped by maintainer; ask before restarting"
    assert len(calls) == call_count_after_stop, "systemctl was called despite maintainer hold!"

    # Automated restart must also be refused WITHOUT invoking systemctl
    _, restart_receipt = submit_control_request(host, unit, "restart", requester="supervisor")
    assert restart_receipt["outcome"] == "refused"
    assert restart_receipt["error"] == "unit stopped by maintainer; ask before restarting"
    assert len(calls) == call_count_after_stop, "systemctl was called despite maintainer hold!"


def test_maintainer_stop_via_maintainer_chat_requester(host) -> None:
    paths, _, calls = host
    unit = "jarvisos-strata-sage.service"

    _, receipt = submit_control_request(host, unit, "stop", requester="maintainer-chat")
    assert receipt["outcome"] == "ok"
    assert paths.maintainer_stopped_units.is_file()

    call_count = len(calls)
    _, start_receipt = submit_control_request(host, unit, "start", requester="agent")
    assert start_receipt["outcome"] == "refused"
    assert len(calls) == call_count


def test_maintainer_stop_via_args_maintainer_flag(host) -> None:
    paths, _, calls = host
    unit = "jarvisos-llama-3b.service"

    # Requester is "agent" but maintainer: True argument is supplied
    _, receipt = submit_control_request(
        host, unit, "stop", requester="agent", maintainer=True, reason="host gaming"
    )
    assert receipt["outcome"] == "ok"

    holds = json.loads(paths.maintainer_stopped_units.read_text(encoding="utf-8"))
    assert unit in holds
    assert holds[unit]["reason"] == "host gaming"

    call_count = len(calls)
    _, start_receipt = submit_control_request(host, unit, "start", requester="agent")
    assert start_receipt["outcome"] == "refused"
    assert len(calls) == call_count


# -----------------------------------------------------------------------------
# Acceptance Criterion 2: Automated stop -> automated start succeeds
# -----------------------------------------------------------------------------


def test_automated_stop_allows_automated_start(host) -> None:
    paths, _, _ = host
    unit = "jarvisos-strata-sage.service"

    # Automated stop (e.g. normal model swap)
    _, stop_receipt = submit_control_request(host, unit, "stop", requester="supervisor")
    assert stop_receipt["outcome"] == "ok"

    # No hold recorded for automated stop
    if paths.maintainer_stopped_units.exists():
        holds = json.loads(paths.maintainer_stopped_units.read_text(encoding="utf-8"))
        assert unit not in holds

    # Automated start succeeds
    _, start_receipt = submit_control_request(host, unit, "start", requester="supervisor")
    assert start_receipt["outcome"] == "ok"


# -----------------------------------------------------------------------------
# Maintainer start clears hold and allows subsequent automated operations
# -----------------------------------------------------------------------------


def test_maintainer_start_clears_hold(host) -> None:
    paths, _, _ = host
    unit = "jarvisos-strata-sage.service"

    # 1. Maintainer stop records hold
    submit_control_request(host, unit, "stop", requester="maintainer")
    holds = json.loads(paths.maintainer_stopped_units.read_text(encoding="utf-8"))
    assert unit in holds

    # 2. Automated start is refused
    _, start_refused = submit_control_request(host, unit, "start", requester="supervisor")
    assert start_refused["outcome"] == "refused"

    # 3. Maintainer starts unit -> proceeds with systemctl and clears hold
    _, maint_start = submit_control_request(host, unit, "start", requester="maintainer")
    assert maint_start["outcome"] == "ok"

    holds_after = json.loads(paths.maintainer_stopped_units.read_text(encoding="utf-8"))
    assert unit not in holds_after

    # 4. Subsequent automated restart succeeds
    _, auto_restart = submit_control_request(host, unit, "restart", requester="supervisor")
    assert auto_restart["outcome"] == "ok"


def test_maintainer_restart_clears_hold(host) -> None:
    paths, _, _ = host
    unit = "jarvisos-strata-sage.service"

    submit_control_request(host, unit, "stop", requester="maintainer")
    assert unit in json.loads(paths.maintainer_stopped_units.read_text(encoding="utf-8"))

    _, maint_restart = submit_control_request(host, unit, "restart", requester="maintainer")
    assert maint_restart["outcome"] == "ok"

    assert unit not in json.loads(paths.maintainer_stopped_units.read_text(encoding="utf-8"))


# -----------------------------------------------------------------------------
# Acceptance Criterion 3: Corrupt hold file fails closed for automated start/restart
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("corrupt_content", [
    "{not valid json",
    "[]",
    "123",
    '{"jarvisos-strata-sage.service": "not a dict"}',
    "",
])
def test_corrupt_hold_file_fails_closed_on_automated_start_and_restart(host, corrupt_content: str) -> None:
    paths, _, calls = host
    unit = "jarvisos-strata-sage.service"

    paths.maintainer_stopped_units.parent.mkdir(parents=True, exist_ok=True)
    paths.maintainer_stopped_units.write_text(corrupt_content, encoding="utf-8")

    call_count = len(calls)

    # Automated start must fail closed with Refused and zero systemctl calls
    _, start_receipt = submit_control_request(host, unit, "start", requester="supervisor")
    assert start_receipt["outcome"] == "refused"
    assert "maintainer stopped state file" in start_receipt["error"]
    assert len(calls) == call_count

    # Automated restart must fail closed with Refused and zero systemctl calls
    _, restart_receipt = submit_control_request(host, unit, "restart", requester="supervisor")
    assert restart_receipt["outcome"] == "refused"
    assert "maintainer stopped state file" in restart_receipt["error"]
    assert len(calls) == call_count


# -----------------------------------------------------------------------------
# CLI subcommands: hold-unit, release-unit, held-units
# -----------------------------------------------------------------------------


def test_cli_subcommands_lifecycle(host, capsys: pytest.CaptureFixture[str]) -> None:
    paths, _, _ = host
    unit = "jarvisos-strata-sage.service"

    # 1. hold-unit
    rc = h.main(["--home", str(paths.home), "hold-unit", "--unit", unit, "--reason", "cli test hold"])
    assert rc == 0
    captured = capsys.readouterr()
    res = json.loads(captured.out)
    assert res["ok"] is True
    assert res["unit"] == unit

    # Verify held-units lists it
    rc = h.main(["--home", str(paths.home), "held-units"])
    assert rc == 0
    captured = capsys.readouterr()
    held = json.loads(captured.out)
    assert unit in held
    assert held[unit]["reason"] == "cli test hold"
    assert held[unit]["requester"] == "maintainer"

    # Automated start via executor is refused
    _, start_refused = submit_control_request(host, unit, "start", requester="supervisor")
    assert start_refused["outcome"] == "refused"

    # 2. release-unit
    rc = h.main(["--home", str(paths.home), "release-unit", "--unit", unit])
    assert rc == 0
    captured = capsys.readouterr()
    res = json.loads(captured.out)
    assert res["ok"] is True
    assert res["released"] is True

    # Verify held-units is empty
    rc = h.main(["--home", str(paths.home), "held-units"])
    assert rc == 0
    captured = capsys.readouterr()
    held = json.loads(captured.out)
    assert unit not in held

    # Automated start via executor now succeeds
    _, start_ok = submit_control_request(host, unit, "start", requester="supervisor")
    assert start_ok["outcome"] == "ok"


def test_cli_release_corrupt_file_recovery(host, capsys: pytest.CaptureFixture[str]) -> None:
    paths, _, _ = host
    unit = "jarvisos-strata-sage.service"

    paths.maintainer_stopped_units.parent.mkdir(parents=True, exist_ok=True)
    paths.maintainer_stopped_units.write_text("{corrupt", encoding="utf-8")

    # held-units reports error on corrupt file
    rc = h.main(["--home", str(paths.home), "held-units"])
    assert rc == 1
    capsys.readouterr()

    # release-unit recovers by clearing state
    rc = h.main(["--home", str(paths.home), "release-unit", "--unit", unit])
    assert rc == 0
    capsys.readouterr()

    # held-units is now clean and empty
    rc = h.main(["--home", str(paths.home), "held-units"])
    assert rc == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {}


# -----------------------------------------------------------------------------
# Acceptance Criterion 4: StrataRuntimeController refusal propagation
# -----------------------------------------------------------------------------


def test_strata_runtime_controller_handles_refusal_without_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool = tmp_path / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    receipts_dir = spool / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))

    current_time = 1000.0

    ctrl = StrataRuntimeController(
        probe_fn=lambda *_: False,
        clock_fn=lambda: current_time,
        timeout_s=600.0,
    )

    # Initially stopped
    st0 = ctrl.get_state("sage", "127.0.0.1", 8080, "sage-model")
    assert st0.state == "stopped"

    # Start model -> state is loading
    st1 = ctrl.start("sage", "jarvisos-strata-sage.service", "127.0.0.1", 8080, "sage-model")
    assert st1.state == "loading"
    req_id = st1.last_request_id
    assert req_id is not None

    # Host writes a "refused" receipt
    receipt_file = receipts_dir / f"{req_id}.json"
    receipt_file.write_text(
        json.dumps({
            "schema": "jarvisos.hostops.receipt.v1",
            "request_id": req_id,
            "outcome": "refused",
            "error": "unit stopped by maintainer; ask before restarting",
        }),
        encoding="utf-8",
    )

    # get_state surfaces "refused" cleanly as stopped with last_error
    current_time = 1005.0
    st2 = ctrl.get_state("sage", "127.0.0.1", 8080, "sage-model")
    assert st2.state == "stopped"
    assert st2.last_error == "unit stopped by maintainer; ask before restarting"
    assert st2.reason == "unit stopped by maintainer; ask before restarting"

    # Subsequent polling does NOT retry or transition back to loading
    current_time = 1100.0
    st3 = ctrl.get_state("sage", "127.0.0.1", 8080, "sage-model")
    assert st3.state == "stopped"
    assert st3.last_error == "unit stopped by maintainer; ask before restarting"


# -----------------------------------------------------------------------------
# Non-local model units unaffected
# -----------------------------------------------------------------------------


def test_non_local_model_unit_unaffected(host) -> None:
    paths, _, calls = host
    unit = "jarvisos-searxng.service"

    # Maintainer stops SearXNG (non-local-model unit)
    _, stop_rcpt = submit_control_request(host, unit, "stop", requester="maintainer")
    assert stop_rcpt["outcome"] == "ok"

    # Non-local-model unit is NOT recorded in maintainer_stopped_units.json
    if paths.maintainer_stopped_units.exists():
        holds = json.loads(paths.maintainer_stopped_units.read_text(encoding="utf-8"))
        assert unit not in holds

    # Automated start succeeds without refusal
    _, start_rcpt = submit_control_request(host, unit, "start", requester="supervisor")
    assert start_rcpt["outcome"] == "ok"
