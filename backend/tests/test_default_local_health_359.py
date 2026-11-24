from __future__ import annotations

import errno
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from app.modules.agents.hermes.supervisor import (
    UPSTREAM_REVISION,
    infer_envelope,
    run_governed_inference,
)
from app.modules.ai.agent_contracts import AgentSessionRef
from app.modules.ai.contracts import (
    AIRequest,
    AIResponse,
    AITaskType,
    AIUsage,
)
from app.modules.ai.execution import AiTaskOutcome
from app.modules.ai.providers.local_llamacpp_adapter import LocalLlamaCppAdapter
from app.modules.local_ai.runtime import host_start, llama_cpp, model_registry
from app.modules.local_ai.runtime.host_start import (
    StrataRuntimeController,
    is_loopback_port_silent,
)
from app.modules.local_ai.runtime.llama_cpp import LlamaCppRuntimeConfig
from app.modules.local_ai.runtime.model_registry import (
    ModelEntry,
    get_active_model_entry,
    list_models,
    read_stored_model_choice,
    restore_default,
)

_SESSION = AgentSessionRef(
    workspace_id="ws-main",
    profile_id="isaac",
    hermes_session_id="hermes-session-359",
    jarvis_thread_id="th-main",
    generation=1,
    upstream_revision=UPSTREAM_REVISION,
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


def _mock_adapter(captured_payloads: list[dict[str, Any]]) -> LocalLlamaCppAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_payloads.append(body)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1234567890,
                "model": body.get("model", "test-model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "mocked response"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    return LocalLlamaCppAdapter(client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))


# -----------------------------------------------------------------------------
# Criterion 1: Hermes sampling & reasoning effort preservation
# -----------------------------------------------------------------------------


def test_hermes_preserves_explicit_temperature_from_active_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """When active local model entry defines explicit temperature, Hermes passes temperature=None."""
    entry_with_temp = ModelEntry(
        id="inkling-sq10",
        label="Inkling 2 Mini SQ-10",
        runtime="strata",
        qualification="default",
        unit="inkling.service",
        port=8080,
        served_model="inkling-2-mini-sq10",
        temperature=1.0,
        top_p=0.95,
        top_k=40,
        min_p=0.05,
        reasoning_effort_analysis="high",
        reasoning_effort_final="low",
    )
    monkeypatch.setattr(model_registry, "get_active_model_entry", lambda: entry_with_temp)

    captured_kwargs: list[dict[str, Any]] = []

    def mock_runner(**kwargs: Any) -> AiTaskOutcome:
        captured_kwargs.append(kwargs)
        return AiTaskOutcome(
            flow_id="flow-1",
            status="success",
            ledger_id="l-1",
            selected_route_class="local:fake",
            decision=MagicMock(),
            response=AIResponse(
                provider_id="local_llamacpp",
                model_id="inkling-2-mini-sq10",
                request_id="req-1",
                correlation_id="corr-1",
                text="Generated answer.",
                finish_reason="stop",
                safety_status="allowed",
                usage=AIUsage(provider_id="local_llamacpp", model_id="inkling-2-mini-sq10"),
            ),
        )

    envelope = infer_envelope({
        "id": "frame-1",
        "session_ref": _SESSION.model_dump(mode="json"),
        "task_kind": "general",
        "messages": [{"role": "user", "content": "Synthesize research"}],
    })

    result = run_governed_inference(envelope, runner=mock_runner)
    assert result["status"] == "success"
    assert len(captured_kwargs) == 1
    # Hermes must NOT have overridden temperature with 0.3; passed temperature must be None
    assert captured_kwargs[0]["temperature"] is None

    # Now verify LocalLlamaCppAdapter applies the active entry's temperature, top_p, top_k, min_p
    captured_payloads: list[dict[str, Any]] = []
    adapter = _mock_adapter(captured_payloads)
    adapter.complete(
        AIRequest(
            task_type=AITaskType.synthesis,
            prompt="Synthesize research",
            temperature=captured_kwargs[0]["temperature"],
            metadata=captured_kwargs[0].get("metadata") or {},
        )
    )

    assert len(captured_payloads) == 1
    payload = captured_payloads[0]
    assert payload["temperature"] == 1.0
    assert payload["top_p"] == 0.95
    assert payload["top_k"] == 40
    assert payload["min_p"] == 0.05


def test_hermes_uses_default_0_3_temperature_when_entry_omits_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When active local model entry does NOT specify temperature, Hermes general turns use 0.3."""
    entry_without_temp = ModelEntry(
        id="legacy-model",
        label="Legacy Model",
        runtime="llama_cpp",
        qualification="default",
        model_path="/tmp/model.gguf",
        model_sha256="a" * 64,
        temperature=None,
    )
    monkeypatch.setattr(model_registry, "get_active_model_entry", lambda: entry_without_temp)

    captured_kwargs: list[dict[str, Any]] = []

    def mock_runner(**kwargs: Any) -> AiTaskOutcome:
        captured_kwargs.append(kwargs)
        return AiTaskOutcome(
            flow_id="flow-2",
            status="success",
            ledger_id="l-2",
            selected_route_class="local:fake",
            decision=MagicMock(),
            response=AIResponse(
                provider_id="local_llamacpp",
                model_id="legacy-model",
                request_id="req-2",
                correlation_id="corr-2",
                text="Answer.",
                finish_reason="stop",
                safety_status="allowed",
                usage=AIUsage(provider_id="local_llamacpp", model_id="legacy-model"),
            ),
        )

    envelope = infer_envelope({
        "id": "frame-2",
        "session_ref": _SESSION.model_dump(mode="json"),
        "task_kind": "general",
        "messages": [{"role": "user", "content": "Explain quantum entanglement"}],
    })

    result = run_governed_inference(envelope, runner=mock_runner)
    assert result["status"] == "success"
    assert captured_kwargs[0]["temperature"] == 0.3


def test_hermes_reasoning_effort_final_and_analysis_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify reasoning_phase='final' sends reasoning_effort_final and tool/analysis turns send reasoning_effort_analysis."""
    entry = ModelEntry(
        id="inkling-sq10",
        label="Inkling 2 Mini SQ-10",
        runtime="strata",
        qualification="default",
        unit="inkling.service",
        port=8080,
        served_model="inkling-2-mini-sq10",
        reasoning_effort_analysis="high",
        reasoning_effort_final="low",
    )
    monkeypatch.setattr(model_registry, "get_active_model_entry", lambda: entry)

    captured_kwargs: list[dict[str, Any]] = []

    def mock_runner(**kwargs: Any) -> AiTaskOutcome:
        captured_kwargs.append(kwargs)
        return AiTaskOutcome(
            flow_id="flow-3",
            status="success",
            ledger_id="l-3",
            selected_route_class="local:fake",
            decision=MagicMock(),
            response=AIResponse(
                provider_id="local_llamacpp",
                model_id="inkling-2-mini-sq10",
                request_id="req-3",
                correlation_id="corr-3",
                text="Final answer response",
                finish_reason="stop",
                safety_status="allowed",
                usage=AIUsage(provider_id="local_llamacpp", model_id="inkling-2-mini-sq10"),
            ),
        )

    # 1. Final answer turn passes reasoning_phase="final"
    envelope = infer_envelope({
        "id": "frame-3",
        "session_ref": _SESSION.model_dump(mode="json"),
        "task_kind": "general",
        "messages": [{"role": "user", "content": "Produce final summary"}],
    })
    run_governed_inference(envelope, runner=mock_runner, reasoning_phase="final")
    assert captured_kwargs[-1]["metadata"] == {"reasoning_phase": "final"}

    captured_payloads: list[dict[str, Any]] = []
    adapter = _mock_adapter(captured_payloads)
    adapter.complete(
        AIRequest(
            task_type=AITaskType.synthesis,
            prompt="Produce final summary",
            metadata=captured_kwargs[-1]["metadata"],
        )
    )
    assert captured_payloads[-1]["chat_template_kwargs"]["reasoning_effort"] == "low"

    # 2. Analysis / tool turn without reasoning_phase="final"
    run_governed_inference(envelope, runner=mock_runner, reasoning_phase=None)
    assert "metadata" not in captured_kwargs[-1]
    captured_payloads.clear()
    adapter.complete(
        AIRequest(
            task_type=AITaskType.synthesis,
            prompt="Produce final summary",
        )
    )
    assert captured_payloads[-1]["chat_template_kwargs"]["reasoning_effort"] == "high"


# -----------------------------------------------------------------------------
# Criterion 2: Automatic Strata-to-llama_cpp fallback
# -----------------------------------------------------------------------------


def test_automatic_strata_fallback_on_receipt_failure_without_overwriting_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When default Strata entry fails to start (host-ops receipt failure) and port is silent, falls back to llama_cpp."""
    spool = tmp_path / "spool"
    (spool / "requests").mkdir(parents=True, exist_ok=True)
    (spool / "receipts").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))

    llama_file = tmp_path / "qwen.gguf"
    llama_file.write_bytes(b"qwen-weights")
    models = [
        {
            "id": "sage",
            "label": "Sage",
            "runtime": "strata",
            "qualification": "default",
            "unit": "sage.service",
            "port": 8080,
            "served_model": "sage-model",
        },
        {
            "id": "qwen",
            "label": "Qwen 2.5",
            "runtime": "llama_cpp",
            "qualification": "qualified",
            "model_path": str(llama_file),
            "model_sha256": "b" * 64,
        },
    ]
    _setup_registry(tmp_path, monkeypatch, models)
    # sha peek returns verified
    monkeypatch.setattr(llama_cpp, "peek_model_sha256", lambda *_: ("b" * 64, False))
    monkeypatch.setattr(model_registry, "peek_model_sha256", lambda *_: ("b" * 64, False))

    started_llama = []
    owner = SimpleNamespace(
        active_model_id="sage",
        default_model_id="sage",
        config=LlamaCppRuntimeConfig(),
        status=lambda: {"model_loaded": True, "runtime_reachable": True},
        start=lambda: started_llama.append("started") or {"status": "ok"},
        stop=lambda: None,
    )
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "llama_cpp_runtime_config", lambda: LlamaCppRuntimeConfig(manage=True))

    ctrl = StrataRuntimeController(probe_fn=lambda *_: False)
    # Start sage and record failure receipt
    st = ctrl.start("sage", "sage.service", "127.0.0.1", 8080, "sage-model")
    assert st.state == "loading"
    req_id = st.last_request_id
    receipt_file = spool / "receipts" / f"{req_id}.json"
    receipt_file.write_text(
        json.dumps({
            "schema": host_start.RECEIPT_SCHEMA,
            "request_id": req_id,
            "outcome": "failed",
            "error": "systemd unit failed to start",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)

    # Now get_active_model_entry must automatically fall back to qwen
    active = get_active_model_entry()
    assert active is not None
    assert active.id == "qwen"
    assert active.runtime == "llama_cpp"
    assert "started" in started_llama

    # Crucial invariant: stored model choice on disk was NOT overwritten
    assert read_stored_model_choice() is None


def test_automatic_strata_fallback_on_startup_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When default Strata startup times out and port is silent, automatically falls back to llama_cpp."""
    spool = tmp_path / "spool"
    (spool / "requests").mkdir(parents=True, exist_ok=True)
    (spool / "receipts").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))

    llama_file = tmp_path / "qwen.gguf"
    llama_file.write_bytes(b"qwen-weights")
    models = [
        {
            "id": "sage",
            "label": "Sage",
            "runtime": "strata",
            "qualification": "default",
            "unit": "sage.service",
            "port": 8080,
            "served_model": "sage-model",
        },
        {
            "id": "qwen",
            "label": "Qwen 2.5",
            "runtime": "llama_cpp",
            "qualification": "qualified",
            "model_path": str(llama_file),
            "model_sha256": "b" * 64,
        },
    ]
    _setup_registry(tmp_path, monkeypatch, models)
    monkeypatch.setattr(model_registry, "peek_model_sha256", lambda *_: ("b" * 64, False))

    simulated_time = 1000.0
    ctrl = StrataRuntimeController(
        probe_fn=lambda *_: False,
        clock_fn=lambda: simulated_time,
        timeout_s=10.0,
    )
    ctrl.start("sage", "sage.service", "127.0.0.1", 8080, "sage-model")
    # Advance time past timeout
    simulated_time = 1015.0
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)

    owner = SimpleNamespace(
        active_model_id="sage",
        default_model_id="sage",
        config=LlamaCppRuntimeConfig(),
        status=lambda: {"model_loaded": True, "runtime_reachable": True, "spawned_by_jarvis": True},
        start=lambda: {"status": "ok"},
        stop=lambda: None,
    )
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "llama_cpp_runtime_config", lambda: LlamaCppRuntimeConfig(manage=True))

    active = get_active_model_entry()
    assert active is not None
    assert active.id == "qwen"
    assert read_stored_model_choice() is None


# -----------------------------------------------------------------------------
# Criterion 3: Silent-port guard (Single hot model invariant)
# -----------------------------------------------------------------------------


def test_fallback_refuses_to_spawn_llama_cpp_if_strata_port_still_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Strata port still answers, fallback refuses to spawn llama_cpp (no double GPU load)."""
    spool = tmp_path / "spool"
    (spool / "requests").mkdir(parents=True, exist_ok=True)
    (spool / "receipts").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))

    llama_file = tmp_path / "qwen.gguf"
    llama_file.write_bytes(b"qwen-weights")
    models = [
        {
            "id": "sage",
            "label": "Sage",
            "runtime": "strata",
            "qualification": "default",
            "unit": "sage.service",
            "port": 8080,
            "served_model": "sage-model",
        },
        {
            "id": "qwen",
            "label": "Qwen 2.5",
            "runtime": "llama_cpp",
            "qualification": "qualified",
            "model_path": str(llama_file),
            "model_sha256": "b" * 64,
        },
    ]
    _setup_registry(tmp_path, monkeypatch, models)
    monkeypatch.setattr(model_registry, "peek_model_sha256", lambda *_: ("b" * 64, False))

    # Controller says state is failed, but probe responds True (still answering!)
    class AnsweringController:
        def get_state(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(state="failed", reason="Simulated failure", elapsed_seconds=0)

        def probe(self, *args: Any, **kwargs: Any) -> bool:
            return True

    ctrl = AnsweringController()
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)

    llama_spawned = False

    def fake_start() -> dict[str, Any]:
        nonlocal llama_spawned
        llama_spawned = True
        return {"status": "ok"}

    owner = SimpleNamespace(
        active_model_id="sage",
        default_model_id="sage",
        config=LlamaCppRuntimeConfig(),
        status=lambda: {"model_loaded": False, "runtime_reachable": False},
        start=fake_start,
        stop=lambda: None,
    )
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "llama_cpp_runtime_config", lambda: LlamaCppRuntimeConfig(manage=True))

    listed = list_models()
    assert listed["fallback_active"] is False
    assert llama_spawned is False
    assert owner.active_model_id == "sage"


def test_is_loopback_port_silent_real_sockets() -> None:
    """Real loopback sockets: an answering port is not silent, a closed one is; other hosts never are."""
    assert is_loopback_port_silent("example.com", 80) is False

    # Bind ephemeral port on 127.0.0.1
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert is_loopback_port_silent("127.0.0.1", port) is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    assert is_loopback_port_silent("127.0.0.1", port) is True


def test_is_loopback_port_silent_fail_closed_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed loopback silence check: only definite ECONNREFUSED on loopback is True."""
    # 1. Unsupported hosts fail closed
    assert is_loopback_port_silent("192.168.1.1", 8080) is False
    assert is_loopback_port_silent("example.com", 80) is False
    assert is_loopback_port_silent("10.0.0.1", 8080) is False
    assert is_loopback_port_silent("", 8080) is False

    # 2. Ephemeral server on 127.0.0.1: open -> not silent (False); stopped -> silent (True)
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert is_loopback_port_silent("127.0.0.1", port) is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    # Definite connection refused on loopback -> True
    assert is_loopback_port_silent("127.0.0.1", port) is True

    # 3. Timeout uncertainty -> False (not proven silent)
    def mock_timeout(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("connect timed out")

    monkeypatch.setattr(socket, "create_connection", mock_timeout)
    assert is_loopback_port_silent("127.0.0.1", 8080) is False

    # 4. DNS uncertainty -> False
    def mock_gaierror(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "create_connection", mock_gaierror)
    assert is_loopback_port_silent("localhost", 8080) is False

    # 5. Generic OS / network errors -> False
    def mock_unreach(*args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.EHOSTUNREACH, "No route to host")

    monkeypatch.setattr(socket, "create_connection", mock_unreach)
    assert is_loopback_port_silent("127.0.0.1", 8080) is False


def test_strata_controller_deterministic_port_silence_injection() -> None:
    """StrataRuntimeController supports deterministic port silence injection without sockets."""
    ctrl_loud = StrataRuntimeController(port_silence_probe_fn=lambda h, p: False)
    assert ctrl_loud.is_port_silent("127.0.0.1", 8080) is False

    ctrl_silent = StrataRuntimeController(port_silence_probe_fn=lambda h, p: True)
    assert ctrl_silent.is_port_silent("127.0.0.1", 8080) is True


def test_fallback_refuses_when_strata_port_alive_with_wrong_model_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Strata fails but its loopback port is still answering (e.g. wrong served_model), fallback refuses.

    Causal proof:
    1. Port open with wrong model id -> probe is False, but port is reachable -> fallback refused (no llama_cpp spawn).
    2. Server stopped -> port silent -> fallback activates and spawns llama_cpp.
    """
    spool = tmp_path / "spool"
    (spool / "requests").mkdir(parents=True, exist_ok=True)
    (spool / "receipts").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))

    class WrongModelHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/v1/models":
                body = json.dumps({"data": [{"id": "wrong-model-id"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), WrongModelHandler)
    server_port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    llama_file = tmp_path / "qwen.gguf"
    llama_file.write_bytes(b"qwen-weights")
    models = [
        {
            "id": "sage",
            "label": "Sage",
            "runtime": "strata",
            "qualification": "default",
            "unit": "sage.service",
            "port": server_port,
            "served_model": "sage-model",
        },
        {
            "id": "qwen",
            "label": "Qwen 2.5",
            "runtime": "llama_cpp",
            "qualification": "qualified",
            "model_path": str(llama_file),
            "model_sha256": "b" * 64,
        },
    ]
    _setup_registry(tmp_path, monkeypatch, models)
    monkeypatch.setattr(model_registry, "peek_model_sha256", lambda *_: ("b" * 64, False))

    ctrl = StrataRuntimeController()
    st = ctrl.start("sage", "sage.service", "127.0.0.1", server_port, "sage-model")
    assert st.state == "loading"
    req_id = st.last_request_id
    (spool / "receipts" / f"{req_id}.json").write_text(
        json.dumps({
            "schema": host_start.RECEIPT_SCHEMA,
            "request_id": req_id,
            "outcome": "failed",
            "error": "host ops crash",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)

    assert ctrl.probe("127.0.0.1", server_port, "sage-model") is False
    assert ctrl.get_state("sage", "127.0.0.1", server_port, "sage-model").state == "failed"

    llama_spawned = False

    def fake_start() -> dict[str, Any]:
        nonlocal llama_spawned
        llama_spawned = True
        return {"status": "ok"}

    owner = SimpleNamespace(
        active_model_id="sage",
        default_model_id="sage",
        config=LlamaCppRuntimeConfig(),
        status=lambda: {"model_loaded": False, "runtime_reachable": False},
        start=fake_start,
        stop=lambda: None,
    )
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "llama_cpp_runtime_config", lambda: LlamaCppRuntimeConfig(manage=True))

    try:
        # Phase 1: Port is reachable (though wrong model ID) -> fallback MUST be refused
        listed = list_models()
        assert listed["fallback_active"] is False
        assert llama_spawned is False
        assert owner.active_model_id == "sage"

        # Direct check on determine_fallback
        entries, _ = model_registry.read_registry()
        active, fallback, _ = model_registry.determine_fallback(entries)
        assert active is False
        assert fallback is None
    finally:
        # Phase 2: Shut down server -> port becomes silent
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2.0)

    # Now port is silent: fallback activates and spawns llama_cpp
    listed_after = list_models()
    assert listed_after["fallback_active"] is True
    assert listed_after["active_id"] == "qwen"
    assert llama_spawned is True
    assert owner.active_model_id == "qwen"


def test_fallback_refuses_when_strata_host_is_unsupported_non_loopback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Strata host is non-loopback / unsupported, fallback fails closed and refuses llama_cpp.

    Causal proof:
    1. Host is non-loopback (192.168.1.99) -> port silence check fails closed -> fallback refused.
    2. Host is allowed loopback host (127.0.0.1) with silent port -> fallback activates and spawns llama_cpp.
    """
    llama_file = tmp_path / "qwen.gguf"
    llama_file.write_bytes(b"qwen-weights")
    monkeypatch.setattr(model_registry, "peek_model_sha256", lambda *_: ("b" * 64, False))

    llama_entry = ModelEntry(
        id="qwen",
        label="Qwen 2.5",
        runtime="llama_cpp",
        qualification="qualified",
        model_path=str(llama_file),
        model_sha256="b" * 64,
    )
    unsupported_strata_entry = ModelEntry(
        id="sage",
        label="Sage",
        runtime="strata",
        qualification="default",
        unit="sage.service",
        host="192.168.1.99",
        port=8080,
        served_model="sage-model",
    )
    supported_strata_entry = ModelEntry(
        id="sage",
        label="Sage",
        runtime="strata",
        qualification="default",
        unit="sage.service",
        host="127.0.0.1",
        port=8080,
        served_model="sage-model",
    )

    class FailedController:
        def get_state(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(state="failed", reason="host ops crash", elapsed_seconds=0)

        def probe(self, *args: Any, **kwargs: Any) -> bool:
            return False

        def is_port_silent(self, host: str, port: int) -> bool:
            return is_loopback_port_silent(host, port)

    ctrl = FailedController()
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)

    llama_spawned = False

    def fake_start() -> dict[str, Any]:
        nonlocal llama_spawned
        llama_spawned = True
        return {"status": "ok"}

    owner = SimpleNamespace(
        active_model_id="sage",
        default_model_id="sage",
        config=LlamaCppRuntimeConfig(),
        status=lambda: {"model_loaded": False, "runtime_reachable": False},
        start=fake_start,
        stop=lambda: None,
    )
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "llama_cpp_runtime_config", lambda: LlamaCppRuntimeConfig(manage=True))

    # Phase 1: Unsupported non-loopback host -> fallback MUST be refused
    monkeypatch.setattr(model_registry, "read_registry", lambda: ([unsupported_strata_entry, llama_entry], True))

    active, fallback, _ = model_registry.determine_fallback([unsupported_strata_entry, llama_entry])
    assert active is False
    assert fallback is None

    listed = list_models()
    assert listed["fallback_active"] is False
    assert llama_spawned is False
    assert owner.active_model_id == "sage"

    # Phase 2: Switch to allowed loopback host on silent port -> fallback activates
    monkeypatch.setattr(model_registry, "read_registry", lambda: ([supported_strata_entry, llama_entry], True))

    active_after, fallback_after, _ = model_registry.determine_fallback([supported_strata_entry, llama_entry])
    assert active_after is True
    assert fallback_after is not None
    assert fallback_after.id == "qwen"

    listed_after = list_models()
    assert listed_after["fallback_active"] is True
    assert listed_after["active_id"] == "qwen"
    assert llama_spawned is True
    assert owner.active_model_id == "qwen"


def test_fallback_refuses_when_strata_port_check_times_out_uncertainly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Strata port connect times out (uncertainty), fallback fails closed and refuses llama_cpp.

    Causal proof:
    1. Connection times out -> silence not proven -> fallback refused (no llama_cpp spawn).
    2. Connection refused (proven silent) -> fallback activates and spawns llama_cpp.
    """
    spool = tmp_path / "spool"
    (spool / "requests").mkdir(parents=True, exist_ok=True)
    (spool / "receipts").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))

    llama_file = tmp_path / "qwen.gguf"
    llama_file.write_bytes(b"qwen-weights")
    models = [
        {
            "id": "sage",
            "label": "Sage",
            "runtime": "strata",
            "qualification": "default",
            "unit": "sage.service",
            "host": "127.0.0.1",
            "port": 8080,
            "served_model": "sage-model",
        },
        {
            "id": "qwen",
            "label": "Qwen 2.5",
            "runtime": "llama_cpp",
            "qualification": "qualified",
            "model_path": str(llama_file),
            "model_sha256": "b" * 64,
        },
    ]
    _setup_registry(tmp_path, monkeypatch, models)
    monkeypatch.setattr(model_registry, "peek_model_sha256", lambda *_: ("b" * 64, False))

    ctrl = StrataRuntimeController(probe_fn=lambda *_: False)
    st = ctrl.start("sage", "sage.service", "127.0.0.1", 8080, "sage-model")
    req_id = st.last_request_id
    (spool / "receipts" / f"{req_id}.json").write_text(
        json.dumps({
            "schema": host_start.RECEIPT_SCHEMA,
            "request_id": req_id,
            "outcome": "failed",
            "error": "host ops crash",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)

    llama_spawned = False

    def fake_start() -> dict[str, Any]:
        nonlocal llama_spawned
        llama_spawned = True
        return {"status": "ok"}

    owner = SimpleNamespace(
        active_model_id="sage",
        default_model_id="sage",
        config=LlamaCppRuntimeConfig(),
        status=lambda: {"model_loaded": False, "runtime_reachable": False},
        start=fake_start,
        stop=lambda: None,
    )
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "llama_cpp_runtime_config", lambda: LlamaCppRuntimeConfig(manage=True))

    # Phase 1: Port probe times out -> fail closed, fallback refused
    def mock_timeout(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(socket, "create_connection", mock_timeout)

    entries, _ = model_registry.read_registry()
    active, fallback, _ = model_registry.determine_fallback(entries)
    assert active is False
    assert fallback is None

    listed = list_models()
    assert listed["fallback_active"] is False
    assert llama_spawned is False
    assert owner.active_model_id == "sage"

    # Phase 2: Definite connection refused on loopback -> proven silent, fallback activates
    def mock_refused(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

    monkeypatch.setattr(socket, "create_connection", mock_refused)

    entries_after, _ = model_registry.read_registry()
    active_after, fallback_after, _ = model_registry.determine_fallback(entries_after)
    assert active_after is True
    assert fallback_after is not None
    assert fallback_after.id == "qwen"

    listed_after = list_models()
    assert listed_after["fallback_active"] is True
    assert listed_after["active_id"] == "qwen"
    assert llama_spawned is True
    assert owner.active_model_id == "qwen"


# -----------------------------------------------------------------------------
# Criterion 4: Fallback visibility in API & Settings/AI
# -----------------------------------------------------------------------------


def test_list_models_exposes_fallback_metadata_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /local-ai/models exposes fallback_active, fallback_from_id, fallback_reason, and retry is possible."""
    spool = tmp_path / "spool"
    (spool / "requests").mkdir(parents=True, exist_ok=True)
    (spool / "receipts").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JARVISOS_HOSTOPS_SPOOL", str(spool))

    llama_file = tmp_path / "qwen.gguf"
    llama_file.write_bytes(b"qwen-weights")
    models = [
        {
            "id": "sage",
            "label": "Sage",
            "runtime": "strata",
            "qualification": "default",
            "unit": "sage.service",
            "port": 8080,
            "served_model": "sage-model",
        },
        {
            "id": "qwen",
            "label": "Qwen 2.5",
            "runtime": "llama_cpp",
            "qualification": "qualified",
            "model_path": str(llama_file),
            "model_sha256": "b" * 64,
        },
    ]
    _setup_registry(tmp_path, monkeypatch, models)
    monkeypatch.setattr(model_registry, "peek_model_sha256", lambda *_: ("b" * 64, False))

    ctrl = StrataRuntimeController(probe_fn=lambda *_: False)
    # Failed receipt
    st = ctrl.start("sage", "sage.service", "127.0.0.1", 8080, "sage-model")
    (spool / "receipts" / f"{st.last_request_id}.json").write_text(
        json.dumps({
            "schema": host_start.RECEIPT_SCHEMA,
            "request_id": st.last_request_id,
            "outcome": "failed",
            "error": "host ops crash",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(host_start, "get_strata_controller", lambda: ctrl)

    owner = SimpleNamespace(
        active_model_id="sage",
        default_model_id="sage",
        config=LlamaCppRuntimeConfig(),
        status=lambda: {"model_loaded": True, "runtime_reachable": True, "spawned_by_jarvis": True},
        start=lambda: {"status": "ok"},
        stop=lambda: None,
    )
    monkeypatch.setattr(model_registry, "get_llama_cpp_runtime_owner", lambda: owner)
    monkeypatch.setattr(model_registry, "llama_cpp_runtime_config", lambda: LlamaCppRuntimeConfig(manage=True))

    listed = list_models()
    assert listed["registry_present"] is True
    assert listed["fallback_active"] is True
    assert listed["fallback_from_id"] == "sage"
    assert "host ops crash" in (listed["fallback_reason"] or "")
    assert listed["active_id"] == "qwen"

    # Verify Sage entry reports failed state in models list
    sage_entry = next(m for m in listed["models"] if m["id"] == "sage")
    assert sage_entry["state"] == "failed"
    assert "host ops crash" in (sage_entry["state_reason"] or "")

    # Retry with restore_default(): stops llama and starts strata again
    stop_called = False
    start_called = False

    def mock_owner_stop() -> None:
        nonlocal stop_called
        stop_called = True

    owner.stop = mock_owner_stop
    orig_start = ctrl.start

    def mock_ctrl_start(*args: Any, **kwargs: Any) -> Any:
        nonlocal start_called
        start_called = True
        return orig_start(*args, **kwargs)

    ctrl.start = mock_ctrl_start

    restore_default()
    assert stop_called is True
    assert start_called is True
    assert owner.active_model_id == "sage"
