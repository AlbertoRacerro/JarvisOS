from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

# Spec 302 Hard rule:
# "The backend never runs systemctl, never spawns/signals the Strata process, never imports scripts/.
# Its only host action is writing a jarvisos.hostops.request.v1 JSON file into the requests/ dir of the spool
# named by JARVISOS_HOSTOPS_SPOOL. A test (only the test) imports scripts/host_ops.py to check the written request passes request_problem."
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import host_ops

from app.modules.ai.contracts import AIProviderHealth, AIRequest, AITaskType
from app.modules.ai.providers import local_llamacpp_adapter
from app.modules.local_ai.runtime import host_start, llama_cpp, model_registry
from app.modules.local_ai.runtime.host_start import (
    StrataRuntimeController,
    probe_strata_readiness,
    write_service_control_request,
)
from app.modules.local_ai.runtime.model_registry import (
    activate_model,
    list_models,
    read_registry,
    save_model_choice,
    start_configured_model,
)


def _setup_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, models: list[dict[str, Any]]) -> Path:
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    registry_file = settings_dir / "local_models.json"
    choice_file = settings_dir / "local_model_choice.json"
    registry_file.write_text(json.dumps({"models": models}), encoding="utf-8")
    paths = SimpleNamespace(
        data_root=tmp_path,
        local_models_file=registry_file,
        local_model_choice_file=choice_file,
    )
    monkeypatch.setattr(model_registry, "build_paths", lambda: paths)
    monkeypatch.setattr(llama_cpp, "build_paths", lambda: paths)
    return registry_file


def _valid_strata_default_entries(tmp_path: Path) -> list[dict[str, Any]]:
    return [
        {
            "id": "sage",
            "label": "Sage",
            "runtime": "strata",
            "qualification": "default",
            "unit": "sage.service",
            "port": 8080,
            "served_model": "sage-model-27b",
        },
        {
            "id": "gemma",
            "label": "Gemma",
            "runtime": "llama_cpp",
            "qualification": "qualified",
            "model_path": str(tmp_path / "gemma.gguf"),
            "model_sha256": "a" * 64,
        },
    ]


# -----------------------------------------------------------------------------
# 1. Registry validation cases (Criterion 1)
# -----------------------------------------------------------------------------


def test_registry_strata_default_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entries = _valid_strata_default_entries(tmp_path)
    _setup_registry(tmp_path, monkeypatch, entries)
    models, present = read_registry()
    assert present is True
    assert len(models) == 2
    assert models[0].id == "sage"
    assert models[0].runtime == "strata"
    assert models[0].qualification == "default"
    assert models[0].unit == "sage.service"
    assert models[0].port == 8080
    assert models[0].served_model == "sage-model-27b"
    assert models[1].id == "gemma"
    assert models[1].runtime == "llama_cpp"
    assert models[1].qualification == "qualified"


@pytest.mark.parametrize(
    "bad_entry_mod",
    [
        {"qualification": "default"},  # two defaults when applied to second entry
        {"unit": None},  # missing unit
        {"unit": "invalid-unit"},  # bad unit regex (no .service)
        {"unit": "../escape.service"},  # bad characters in unit
        {"port": None},  # missing port
        {"port": 0},  # port out of range
        {"port": 70000},  # port > 65535
        {"port": "8080"},  # port not an int
        {"served_model": None},  # missing served_model
        {"served_model": ""},  # empty served_model
        {"host": "192.168.1.100"},  # non-loopback host
    ],
)
def test_registry_strata_validation_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_entry_mod: dict[str, Any]
) -> None:
    base = _valid_strata_default_entries(tmp_path)
    if "qualification" in bad_entry_mod:
        base[1]["qualification"] = "default"  # causes 2 defaults
    else:
        for k, v in bad_entry_mod.items():
            if v is None:
                base[0].pop(k, None)
            else:
                base[0][k] = v

    _setup_registry(tmp_path, monkeypatch, base)
    with pytest.raises(HTTPException) as exc:
        read_registry()
    assert exc.value.status_code == 503


def test_registry_zero_defaults_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _valid_strata_default_entries(tmp_path)
    base[0]["qualification"] = "qualified"
    _setup_registry(tmp_path, monkeypatch, base)
    with pytest.raises(HTTPException) as exc:
        read_registry()
    assert exc.value.status_code == 503


# -----------------------------------------------------------------------------
# 2. Host-start writer: request_problem, spool, readiness states
# -----------------------------------------------------------------------------


def test_host_start_writer_request_shape_and_no_spool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool_dir = tmp_path / "spool"
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool_dir))

    # Without spool directory existing, get_spool_path is None
    assert host_start.get_spool_path() is None

    # Now create the spool dir
    spool_dir.mkdir(parents=True, exist_ok=True)
    assert host_start.get_spool_path() == spool_dir

    now = datetime.now(UTC)
    req_id = write_service_control_request(spool_dir, "sage.service", "start", now=now)
    req_file = spool_dir / "requests" / f"{req_id}.json"
    assert req_file.is_file()

    req_data = json.loads(req_file.read_text(encoding="utf-8"))
    assert req_data["op"] == "service.control"
    assert req_data["args"] == {"unit": "sage.service", "action": "start"}

    # Pinned by scripts/host_ops.py::request_problem
    problem = host_ops.request_problem(req_data, now)
    assert problem is None, f"request_problem returned: {problem}"


def test_strata_readiness_states_driven_by_fake_probe_and_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool = tmp_path / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))

    clock_time = 1000.0

    def fake_clock() -> float:
        return clock_time

    probe_result = False

    def fake_probe(host: str, port: int, served_model: str) -> bool:
        return probe_result

    ctrl = StrataRuntimeController(
        probe_fn=fake_probe,
        clock_fn=fake_clock,
        timeout_s=600.0,
    )

    # Initially stopped
    st = ctrl.get_state("sage", "127.0.0.1", 8080, "sage-model")
    assert st.state == "stopped"

    # Start -> loading
    st_start = ctrl.start("sage", "sage.service", "127.0.0.1", 8080, "sage-model")
    assert st_start.state == "loading"
    assert (spool / "requests" / f"{st_start.last_request_id}.json").is_file()

    # Still loading at 100 seconds
    clock_time = 1100.0
    st_loading = ctrl.get_state("sage", "127.0.0.1", 8080, "sage-model")
    assert st_loading.state == "loading"
    assert st_loading.elapsed_seconds == 100

    # Past timeout without probe succeeding -> failed
    clock_time = 1700.0  # elapsed 700s > 600s timeout
    st_timeout = ctrl.get_state("sage", "127.0.0.1", 8080, "sage-model")
    assert st_timeout.state == "failed"
    assert "timed out" in (st_timeout.reason or "").lower()

    # Restart and probe succeeds -> ready
    clock_time = 1800.0
    st_start_again = ctrl.start("sage", "sage.service", "127.0.0.1", 8080, "sage-model")
    assert st_start_again.state == "loading"
    probe_result = True
    st_ready = ctrl.get_state("sage", "127.0.0.1", 8080, "sage-model")
    assert st_ready.state == "ready"
    assert st_ready.reason is None

    # Probe turns off after ready -> stopped (not loading)
    probe_result = False
    st_after_ready = ctrl.get_state("sage", "127.0.0.1", 8080, "sage-model")
    assert st_after_ready.state == "stopped"

    # Failed receipt outcome
    clock_time = 2000.0
    st_start2 = ctrl.start("sage", "sage.service", "127.0.0.1", 8080, "sage-model")
    receipts_dir = spool / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt_file = receipts_dir / f"{st_start2.last_request_id}.json"
    receipt_file.write_text(
        json.dumps({
            "schema": "jarvisos.hostops.receipt.v1",
            "request_id": st_start2.last_request_id,
            "outcome": "failed",
            "error": "unit failed to start",
        }),
        encoding="utf-8",
    )
    st_failed_rcpt = ctrl.get_state("sage", "127.0.0.1", 8080, "sage-model")
    assert st_failed_rcpt.state == "failed"
    assert "unit failed to start" in (st_failed_rcpt.reason or "")


def test_probe_strata_readiness_real_http_mock() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "sage-27b"}, {"id": "other"}]})
        return httpx.Response(404)

    def client_factory(**kw: Any) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), **kw)
    assert probe_strata_readiness("127.0.0.1", 8080, "sage-27b", client_factory=client_factory) is True
    assert probe_strata_readiness("127.0.0.1", 8080, "gemma", client_factory=client_factory) is False


# -----------------------------------------------------------------------------
# 3. Startup & Spool behaviour (Criteria 2 and 3)
# -----------------------------------------------------------------------------


def test_startup_with_spool_writes_start_and_spawns_no_llama(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool = tmp_path / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))

    entries = _valid_strata_default_entries(tmp_path)
    (tmp_path / "gemma.gguf").write_bytes(b"fixture")
    _setup_registry(tmp_path, monkeypatch, entries)

    events: list[str] = []
    owner = SimpleNamespace(
        active_model_id="sage",
        default_model_id="sage",
        start=lambda: events.append("llama_start"),
        stop=lambda: events.append("llama_stop"),
        status=lambda: {"runtime_reachable": False, "model_loaded": False},
        _lock=None,
    )
    monkeypatch.setattr(llama_cpp, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "llama_cpp_runtime_config", lambda: SimpleNamespace(manage=True))

    ctrl = StrataRuntimeController(probe_fn=lambda *_: False)
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)

    start_configured_model()

    assert "llama_start" not in events  # Spawns NO llama-server!
    req_files = list((spool / "requests").glob("*.json"))
    assert len(req_files) == 1
    req = json.loads(req_files[0].read_text(encoding="utf-8"))
    assert req["op"] == "service.control"
    assert req["args"] == {"unit": "sage.service", "action": "start"}

    # Status reports loading
    st = ctrl.get_state("sage", "127.0.0.1", 8080, "sage-model-27b")
    assert st.state == "loading"


def test_startup_without_spool_starts_llama_and_reports_strata_needs_host_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JARVISOS_HOSTOPS_SPOOL", raising=False)

    entries = _valid_strata_default_entries(tmp_path)
    (tmp_path / "gemma.gguf").write_bytes(b"fixture")
    _setup_registry(tmp_path, monkeypatch, entries)

    events: list[str] = []
    owner = SimpleNamespace(
        active_model_id="gemma",
        default_model_id="sage",
        start=lambda: events.append("llama_start"),
        status=lambda: {"runtime_reachable": True, "model_loaded": True, "reason_code": None},
        _lock=None,
    )
    monkeypatch.setattr(llama_cpp, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "llama_cpp_runtime_config", lambda: SimpleNamespace(manage=True))

    ctrl = StrataRuntimeController(probe_fn=lambda *_: False)
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)

    start_configured_model()

    assert events == ["llama_start"]

    # list_models reports strata as needing host start
    listed = list_models()
    sage_entry = next(m for m in listed["models"] if m["id"] == "sage")
    assert sage_entry["state"] == "requires host start"


# -----------------------------------------------------------------------------
# 4. Owner switch order (Criterion 4) & stored choice persistence
# -----------------------------------------------------------------------------


def test_switch_order_and_stored_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool = tmp_path / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))

    entries = _valid_strata_default_entries(tmp_path)
    (tmp_path / "gemma.gguf").write_bytes(b"fixture")
    _setup_registry(tmp_path, monkeypatch, entries)

    events: list[Any] = []
    sage_answering = True

    def fake_probe(host: str, port: int, served_model: str) -> bool:
        return sage_answering

    ctrl = StrataRuntimeController(
        probe_fn=fake_probe,
        sleep_fn=lambda _: events.append("sleep"),
    )
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)

    class FakeOwner:
        def __init__(self) -> None:
            self.active_model_id = "sage"
            self.default_model_id = "sage"
            self._lock = None

        def status(self) -> dict[str, Any]:
            return {"runtime_reachable": True, "model_loaded": True, "spawned_by_jarvis": True}

        def stop(self) -> None:
            events.append("llama_stop")

        def switch_model(self, config: Any, model_id: str, default_id: str) -> dict[str, Any]:
            events.append(("llama_switch", model_id))
            self.active_model_id = model_id
            return {"active_id": model_id}

    owner = FakeOwner()
    monkeypatch.setattr(llama_cpp, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "peek_model_sha256", lambda _: ("a" * 64, False))

    # Sage is ready. Activate Gemma!
    # Order must be: write stop request -> wait until Sage port stops answering -> spawn llama-server -> store choice
    # We simulate Sage stopping when sleep is called
    def fake_sleep_stops_sage(_: float) -> None:
        nonlocal sage_answering
        sage_answering = False
        _write_receipt(spool, "stop", "ok")
        events.append("sage_port_stopped")

    ctrl._sleep_fn = fake_sleep_stops_sage

    activate_model("gemma")

    # Verify stop request was written
    requests = [json.loads(p.read_text()) for p in (spool / "requests").glob("*.json")]
    stop_req = next(r for r in requests if r["args"]["action"] == "stop")
    assert stop_req["args"]["unit"] == "sage.service"
    assert events == ["sage_port_stopped", ("llama_switch", "gemma")]
    assert owner.active_model_id == "gemma"

    # Verify choice is stored
    choice_file = tmp_path / "settings" / "local_model_choice.json"
    assert choice_file.is_file()
    assert json.loads(choice_file.read_text())["active_model_id"] == "gemma"

    # Next: Activating Sage while Gemma runs
    # Order: stop llama-server -> write start request -> active_model_id = sage -> store choice
    events.clear()
    activate_model("sage")
    assert events == ["llama_stop"]
    assert json.loads(choice_file.read_text())["active_model_id"] == "sage"
    start_req = next(r for r in [json.loads(p.read_text()) for p in (spool / "requests").glob("*.json")] if r["args"]["action"] == "start")
    assert start_req["args"]["unit"] == "sage.service"


def _write_receipt(spool: Path, action: str, outcome: str) -> None:
    for path in (spool / "requests").glob("*.json"):
        request = json.loads(path.read_text())
        if request["args"]["action"] == action:
            (spool / "receipts").mkdir(exist_ok=True)
            receipt = {"schema": host_start.RECEIPT_SCHEMA, "request_id": request["id"], "outcome": outcome, "error": None}
            (spool / "receipts" / f"{request['id']}.json").write_text(json.dumps(receipt))


def test_stop_waits_for_receipt_while_loading_model_is_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A loading model does not answer the probe; without the host's receipt it is not stopped.
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))
    clock = [0.0]

    def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    ctrl = StrataRuntimeController(probe_fn=lambda *_: False, clock_fn=lambda: clock[0], sleep_fn=fake_sleep)
    assert ctrl.stop("sage", "sage.service", "127.0.0.1", 8090, "sage-model").state == "failed"
    assert clock[0] >= 120.0

    clock[0] = 0.0
    ctrl._sleep_fn = lambda _: _write_receipt(spool, "stop", "ok")
    assert ctrl.stop("sage", "sage.service", "127.0.0.1", 8090, "sage-model").state == "stopped"


def test_stop_fails_at_once_on_refused_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))
    slept: list[float] = []
    ctrl = StrataRuntimeController(probe_fn=lambda *_: True, sleep_fn=slept.append)
    original_write = host_start.write_service_control_request

    def write_and_refuse(spool_path: Path, unit: str, action: str) -> str:
        request_id = original_write(spool_path, unit, action)
        _write_receipt(spool_path, action, "refused")
        return request_id

    monkeypatch.setattr(host_start, "write_service_control_request", write_and_refuse)
    assert ctrl.stop("sage", "sage.service", "127.0.0.1", 8090, "sage-model").state == "failed"
    assert slept == []


def test_owner_never_spawns_llama_while_strata_is_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # start, restart, crash recovery and the adapter's recovery all go through start()/recover_if_crashed().
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))
    entries = _valid_strata_default_entries(tmp_path)
    _setup_registry(tmp_path, monkeypatch, entries)
    ctrl = StrataRuntimeController(probe_fn=lambda *_: False)
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)
    popen_calls: list[Any] = []
    owner = llama_cpp.LlamaCppRuntimeOwner(
        llama_cpp.LlamaCppRuntimeConfig(binary_path=str(tmp_path / "llama-server"), model_path=str(tmp_path / "gemma.gguf"), manage=True),
        popen=lambda *a, **k: popen_calls.append(a),
    )
    (tmp_path / "llama-server").write_bytes(b"x")
    (tmp_path / "gemma.gguf").write_bytes(b"fixture")
    owner.active_model_id = "sage"
    monkeypatch.setattr(llama_cpp, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)

    assert owner.start()["backend"] == "strata"
    owner._crashed = True
    assert owner.status()["backend"] == "strata"
    assert owner._crashed is False
    assert owner.restart()["backend"] == "strata"
    assert popen_calls == []


def test_switch_refuses_llama_spawn_while_sage_still_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool = tmp_path / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))
    entries = _valid_strata_default_entries(tmp_path)
    (tmp_path / "gemma.gguf").write_bytes(b"fixture")
    _setup_registry(tmp_path, monkeypatch, entries)

    clock = [0.0]

    def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    ctrl = StrataRuntimeController(probe_fn=lambda *_: True, clock_fn=lambda: clock[0], sleep_fn=fake_sleep)
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)
    spawned: list[str] = []

    class FakeOwner:
        active_model_id = "sage"
        default_model_id = "sage"
        _lock = None

        def status(self) -> dict[str, Any]:
            return {}

        def switch_model(self, config: Any, model_id: str, default_id: str) -> dict[str, Any]:
            spawned.append(model_id)
            return {}

    owner = FakeOwner()
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "peek_model_sha256", lambda _: ("a" * 64, False))

    with pytest.raises(HTTPException) as exc:
        activate_model("gemma")
    assert exc.value.status_code == 409
    assert spawned == []
    assert not (tmp_path / "settings" / "local_model_choice.json").exists()


def test_stored_choice_survives_new_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entries = _valid_strata_default_entries(tmp_path)
    _setup_registry(tmp_path, monkeypatch, entries)
    spool = tmp_path / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))

    # Save Gemma as stored choice
    save_model_choice("gemma")

    # Reset owner
    monkeypatch.setattr(llama_cpp, "_OWNER", None)
    fresh_owner = llama_cpp.get_llama_cpp_runtime_owner()
    assert fresh_owner.active_model_id == "gemma"
    assert fresh_owner.default_model_id == "sage"


# -----------------------------------------------------------------------------
# 5. Adapter routing, model id, and refusals (Criterion 5)
# -----------------------------------------------------------------------------


def test_adapter_routing_when_strata_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entries = _valid_strata_default_entries(tmp_path)
    _setup_registry(tmp_path, monkeypatch, entries)

    # Active model is Sage
    owner = SimpleNamespace(active_model_id="sage", default_model_id="sage", auth_headers=lambda: {"Authorization": "Bearer BAD"})
    monkeypatch.setattr(llama_cpp, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(local_llamacpp_adapter, "get_llama_cpp_runtime_owner", lambda: owner)

    ctrl = StrataRuntimeController(probe_fn=lambda *_: True)
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)

    captured_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Hello from Sage"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    adapter = local_llamacpp_adapter.LocalLlamaCppAdapter(
        client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(mock_handler), **kw)
    )

    assert adapter.health() == AIProviderHealth.healthy

    resp = adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="test prompt"))
    assert resp.error is None
    assert resp.model_id == "sage-model-27b"  # stored as served_model_id on the ai_jobs row
    assert resp.text == "Hello from Sage"

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.url == "http://127.0.0.1:8080/v1/chat/completions"
    assert "Authorization" not in req.headers  # No auth header!
    payload = json.loads(req.content)
    assert payload["model"] == "sage-model-27b"  # Served name!


def test_adapter_refusal_when_loading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entries = _valid_strata_default_entries(tmp_path)
    _setup_registry(tmp_path, monkeypatch, entries)

    owner = SimpleNamespace(active_model_id="sage", default_model_id="sage")
    monkeypatch.setattr(llama_cpp, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(local_llamacpp_adapter, "get_llama_cpp_runtime_owner", lambda: owner)

    # Controller reports loading
    ctrl = SimpleNamespace(
        get_state=lambda *_: SimpleNamespace(state="loading", reason="loading model", elapsed_seconds=42)
    )
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)

    adapter = local_llamacpp_adapter.LocalLlamaCppAdapter(
        client_factory=lambda: pytest.fail("HTTP client should not be called")
    )
    resp = adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="test prompt"))
    assert resp.error is not None
    assert resp.error.retryable is True
    assert resp.error.message == "Local model is still loading."


def test_adapter_refusal_when_under_qualification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [
        {
            "id": "sage",
            "label": "Sage",
            "runtime": "strata",
            "qualification": "default",
            "unit": "sage.service",
            "port": 8080,
            "served_model": "sage-model-27b",
        },
        {
            "id": "experimental",
            "label": "Experimental",
            "runtime": "llama_cpp",
            "qualification": "under_qualification",
            "model_path": str(tmp_path / "exp.gguf"),
            "model_sha256": "b" * 64,
        },
    ]
    _setup_registry(tmp_path, monkeypatch, entries)

    owner = SimpleNamespace(active_model_id="experimental", default_model_id="sage")
    monkeypatch.setattr(llama_cpp, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(local_llamacpp_adapter, "get_llama_cpp_runtime_owner", lambda: owner)

    adapter = local_llamacpp_adapter.LocalLlamaCppAdapter(
        client_factory=lambda: pytest.fail("HTTP client should not be called")
    )
    assert adapter.health() == AIProviderHealth.unavailable
    resp = adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="test prompt"))
    assert resp.error is not None
    assert resp.error.message == "Default local model is not loaded."


def test_ai_job_records_served_model_beside_binding_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "root"))
    from app.core.bootstrap import initialize_storage
    from app.core.config import get_settings
    from app.core.database import open_sqlite_connection
    from app.modules.ai.contracts import AIResponse, AIUsage, RoutingDecision
    from app.modules.ai.execution import _write_ai_job

    get_settings.cache_clear()
    initialize_storage(seed_default=True)
    decision = RoutingDecision(provider_id="local_llamacpp", model_id="qwen3.8-27b-q4kxl", decision_reason="bound")
    response = AIResponse(
        provider_id="local_llamacpp", model_id="sage-model-27b", request_id="r1", text="ok",
        finish_reason="stop", safety_status="allowed",
        usage=AIUsage(provider_id="local_llamacpp", model_id="sage-model-27b"),
    )
    job_id = _write_ai_job(
        status="succeeded", task_kind="synthesis", requested_route_class="local:llamacpp",
        selected_route_class="local:llamacpp", decision=decision, prompt_digest=None, context_digest=None,
        context_sources=None, response=response, latency_ms=1, error_type=None,
    )
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT model_id, route_reason_json FROM ai_jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["model_id"] == "qwen3.8-27b-q4kxl"
    assert json.loads(row["route_reason_json"])["served_model_id"] == "sage-model-27b"


def test_regression_finding_1_qualified_active_leaves_local_routes_and_hermes_agent_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.ai import thread_routes
    from app.modules.ai.thread_models import HermesRoleOption

    entries = _valid_strata_default_entries(tmp_path)
    _setup_registry(tmp_path, monkeypatch, entries)

    # When Gemma (qualified) is active while Sage (strata) is default:
    owner = SimpleNamespace(
        active_model_id="gemma",
        default_model_id="sage",
        status=lambda: {
            "configured": True,
            "runtime_reachable": True,
            "model_installed": True,
            "model_loaded": True,
            "reason_code": None,
            "message": "llama-server is reachable and serving its configured model.",
        },
    )
    monkeypatch.setattr(llama_cpp, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)

    avail = thread_routes._llamacpp_route_availability("local:llamacpp", "gemma")
    assert avail.model_loaded is True
    assert avail.reason_code is None
    assert avail.runtime_reachable is True

    # Hermes agent availability with this route availability must be ready
    roles = [
        HermesRoleOption(
            role_id="coder",
            display_name="Coder",
            purpose="Code writing",
            route="local:llamacpp",
            model_id="gemma",
            available=True,
        )
    ]
    agent_avail = thread_routes._hermes_agent_availability(None, roles=roles)
    assert agent_avail.runtime_reachable is True
    assert agent_avail.model_installed is True
    assert agent_avail.reason_code is None

    # When an under_qualification entry is active, local routes and Hermes agent are blocked:
    uq_entries = [
        *entries,
        {
            "id": "unqualified",
            "label": "Unqualified",
            "runtime": "llama_cpp",
            "qualification": "under_qualification",
            "model_path": str(tmp_path / "uq.gguf"),
            "model_sha256": "d" * 64,
        },
    ]
    _setup_registry(tmp_path, monkeypatch, uq_entries)
    owner.active_model_id = "unqualified"
    uq_avail = thread_routes._llamacpp_route_availability("local:llamacpp", "unqualified")
    assert uq_avail.model_loaded is False
    assert uq_avail.reason_code == "LLAMACPP_DEFAULT_NOT_LOADED"
    assert uq_avail.message == "Default local model is not loaded."


def test_regression_finding_2_owner_refuses_spawn_while_registered_strata_port_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.local_ai.runtime.routes import control_llama_cpp

    entries = _valid_strata_default_entries(tmp_path)
    (tmp_path / "gemma.gguf").write_bytes(b"fixture")
    _setup_registry(tmp_path, monkeypatch, entries)

    # Registered Strata entry (Sage) answers its loopback probe
    ctrl = StrataRuntimeController(probe_fn=lambda *_: True)
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)

    popen_calls: list[Any] = []
    config = llama_cpp.LlamaCppRuntimeConfig(
        binary_path=str(tmp_path / "llama-server"),
        model_path=str(tmp_path / "gemma.gguf"),
        manage=True,
    )
    (tmp_path / "llama-server").write_bytes(b"fake-binary")
    owner = llama_cpp.LlamaCppRuntimeOwner(
        config,
        popen=lambda *a, **k: popen_calls.append(a),
    )
    owner.active_model_id = "gemma"
    monkeypatch.setattr(llama_cpp, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)

    # 1. start() must not spawn
    res = owner.start()
    assert popen_calls == []
    assert res["reason_code"] == "STRATA_ACTIVE"

    # 2. recover_if_crashed() must not spawn
    owner._crashed = True
    rec_res = owner.recover_if_crashed()
    assert popen_calls == []
    assert rec_res["reason_code"] == "STRATA_ACTIVE"

    # 3. Explicit start route must not spawn
    route_res = control_llama_cpp("start")
    assert popen_calls == []
    assert route_res["reason_code"] == "STRATA_ACTIVE"


def test_regression_finding_3_host_start_validations_and_post_ready_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))

    # 1. write_service_control_request validates unit name against SYSTEMD_UNIT_RE shared constant
    from app.modules.local_ai.runtime.host_start import (
        SYSTEMD_UNIT_RE,
        check_receipt,
        write_service_control_request,
    )
    from app.modules.local_ai.runtime.model_registry import _UNIT

    assert _UNIT is SYSTEMD_UNIT_RE

    valid_req_id = write_service_control_request(spool, "sage.service", "start")
    assert len(valid_req_id) == 32

    for bad_unit in ("bad;rm -rf /", "sage", "../sage.service", "service", "", "sage@.service.bak"):
        with pytest.raises(ValueError, match="Invalid unit name"):
            write_service_control_request(spool, bad_unit, "start")

    # 2. check_receipt validates request_id format and matching receipt request_id
    assert check_receipt(spool, "short_id") is None
    assert check_receipt(spool, "not_hex_0123456789abcdef012345678!") is None

    # Receipt with mismatched request_id or legacy 'id' field only
    receipts_dir = spool / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    mismatched_file = receipts_dir / f"{valid_req_id}.json"
    mismatched_file.write_text(json.dumps({
        "schema": host_start.RECEIPT_SCHEMA,
        "request_id": "0" * 32,
        "outcome": "ok",
        "error": None,
    }))
    assert check_receipt(spool, valid_req_id) is None

    # Matching receipt
    mismatched_file.write_text(json.dumps({
        "schema": host_start.RECEIPT_SCHEMA,
        "request_id": valid_req_id,
        "outcome": "ok",
        "error": None,
    }))
    assert check_receipt(spool, valid_req_id) == ("ok", None)

    # 3. StrataRuntimeController.get_state: after ready is seen, pending start is cleared so subsequent stop/crash reports stopped
    probe_ready = False
    ctrl = StrataRuntimeController(probe_fn=lambda *_: probe_ready)
    st = ctrl.start("sage", "sage.service", "127.0.0.1", 8080, "sage-model")
    assert st.state == "loading"

    # Now probe sees model is ready
    probe_ready = True
    st_ready = ctrl.get_state("sage", "127.0.0.1", 8080, "sage-model")
    assert st_ready.state == "ready"
    assert st_ready.last_action is None

    # Model stops / crashes later (within timeout)
    probe_ready = False
    st_stopped = ctrl.get_state("sage", "127.0.0.1", 8080, "sage-model")
    assert st_stopped.state == "stopped"


def test_owner_refuses_spawn_when_strata_state_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An unreadable registry must not let llama-server start beside a Strata model that may be running.
    _setup_registry(tmp_path, monkeypatch, [])
    (tmp_path / "settings" / "local_models.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "llama-server").write_bytes(b"fake-binary")
    (tmp_path / "gemma.gguf").write_bytes(b"fixture")
    popen_calls: list[Any] = []
    owner = llama_cpp.LlamaCppRuntimeOwner(
        llama_cpp.LlamaCppRuntimeConfig(
            binary_path=str(tmp_path / "llama-server"), model_path=str(tmp_path / "gemma.gguf"), manage=True,
        ),
        popen=lambda *a, **k: popen_calls.append(a),
    )
    assert owner.start()["reason_code"] == "STRATA_ACTIVE"
    owner._crashed = True
    assert owner.recover_if_crashed()["reason_code"] == "STRATA_ACTIVE"
    assert popen_calls == []


def test_llamacpp_loading_503_reads_as_still_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_registry, "get_active_model_entry", lambda: None)
    adapter = local_llamacpp_adapter.LocalLlamaCppAdapter(
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(503, json={"error": {"message": "Loading model"}})
        ))
    )
    result = adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="hello"))
    assert result.error is not None
    assert result.error.message.endswith("Local model is still loading.")
    assert result.error.retryable is True


def test_registry_written_before_host_start_keeps_working(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The shape master accepted: a Strata model listed by file only. After upgrading, before the host steps,
    # the registry stays valid, the Strata entry "requires host start", and llama-server still starts.
    (tmp_path / "llama-server").write_bytes(b"fake-binary")
    (tmp_path / "gemma.gguf").write_bytes(b"fixture")
    legacy = {"id": "sage", "label": "Sage", "runtime": "strata", "qualification": "qualified",
              "model_path": str(tmp_path / "sage.gguf"), "model_sha256": "c" * 64}
    gemma = {"id": "gemma", "label": "Gemma", "runtime": "llama_cpp", "qualification": "default",
             "model_path": str(tmp_path / "gemma.gguf"), "model_sha256": "a" * 64}
    _setup_registry(tmp_path, monkeypatch, [gemma, legacy])
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(tmp_path / "spool"))
    (tmp_path / "spool" / "requests").mkdir(parents=True)
    probes: list[Any] = []
    monkeypatch.setattr(host_start, "get_strata_controller",
                        lambda: StrataRuntimeController(probe_fn=lambda *a: probes.append(a) or False))
    model_registry.save_model_choice("sage")  # a stale choice naming the old-shape entry is ignored
    popen_calls: list[Any] = []
    owner = llama_cpp.LlamaCppRuntimeOwner(
        llama_cpp.LlamaCppRuntimeConfig(
            binary_path=str(tmp_path / "llama-server"), model_path=str(tmp_path / "gemma.gguf"), manage=True,
        ),
        popen=lambda *a, **k: popen_calls.append(a) or SimpleNamespace(pid=1, poll=lambda: None),
    )
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(llama_cpp, "get_llama_cpp_runtime_owner", lambda: owner)

    entries, present = read_registry()
    assert present and [e.id for e in entries] == ["gemma", "sage"]
    assert model_registry.determine_initial_model(entries).id == "gemma"
    rows = {row["id"]: row for row in list_models()["models"]}
    assert rows["sage"]["state"] == "requires host start"
    with pytest.raises(HTTPException) as exc:
        activate_model("sage")
    assert exc.value.status_code == 409
    assert list((tmp_path / "spool" / "requests").iterdir()) == []
    assert not owner._any_strata_answering() and probes == []
    owner.start()
    assert len(popen_calls) == 1

    # A Strata entry with only some host-start fields, or an old-shape default, is still refused.
    for bad in ({**legacy, "unit": "sage.service"}, {**legacy, "qualification": "default"}):
        rows_bad = [bad] if bad["qualification"] == "default" else [gemma, bad]
        _setup_registry(tmp_path, monkeypatch, rows_bad)
        with pytest.raises(HTTPException):
            read_registry()
