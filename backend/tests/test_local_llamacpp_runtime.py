from __future__ import annotations

import hashlib
import json
import threading
import time

import httpx
import pytest

from app.modules.ai.contracts import AIRequest, AITaskType
from app.modules.ai.provider_registry import load_default_provider_registry, registry_bindings
from app.modules.ai.providers import local_llamacpp_adapter as adapter_module
from app.modules.ai.providers.local_llamacpp_adapter import LocalLlamaCppAdapter
from app.modules.ai.thread_routes import _llamacpp_route_availability
from app.modules.local_ai.runtime.llama_cpp import (
    LlamaCppRuntimeConfig,
    LlamaCppRuntimeOwner,
    cached_model_sha256,
    request_model_sha256,
    validate_loopback_host,
)


def _config(**overrides: object) -> LlamaCppRuntimeConfig:
    values: dict[str, object] = {
        "binary_path": "/fake/llama-server", "model_path": "/fake/model.gguf",
        "host": "127.0.0.1", "port": 8080, "ctx_size": 4096, "n_gpu_layers": 12,
    }
    values.update(overrides)
    return LlamaCppRuntimeConfig(**values)  # type: ignore[arg-type]


def test_adapter_is_loopback_only_and_sends_route_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, object] = {}
    monkeypatch.setattr(adapter_module, "llama_cpp_runtime_config", lambda: _config())

    def respond(request: httpx.Request) -> httpx.Response:
        sent.update(httpx.QueryParams(request.url.query))
        import json
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "analysis <think>secret reasoning</think> answer",
            "reasoning_content": "private thought", "finish_reason": None,
        }, "finish_reason": "stop"}], "usage": {
            "prompt_tokens": 12, "completion_tokens": 9,
            "completion_tokens_details": {"reasoning_tokens": 4},
        }})

    transport = httpx.MockTransport(respond)
    adapter = LocalLlamaCppAdapter(client_factory=lambda: httpx.Client(transport=transport))
    result = adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="hello", max_output_tokens=77))
    assert sent["max_tokens"] == 77
    assert result.provider_id == "local_llamacpp"
    assert result.finish_reason == "stop"
    assert result.text == "analysis  answer"
    assert result.raw_provider_metadata["reasoning_tokens"] == 4
    assert result.usage.output_tokens == 9

    monkeypatch.setattr(adapter_module, "llama_cpp_runtime_config", lambda: _config(host="192.168.1.2"))
    with pytest.raises(ValueError, match="loopback"):
        adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="hello"))


@pytest.mark.parametrize(("thinking", "expected"), [("on", True), ("off", False)])
def test_adapter_sends_per_request_thinking_control(
    thinking: str, expected: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, object] = {}
    monkeypatch.setattr(adapter_module, "llama_cpp_runtime_config", lambda: _config(thinking=thinking))
    transport = httpx.MockTransport(lambda request: _capture_payload(request, payload))
    LocalLlamaCppAdapter(client_factory=lambda: httpx.Client(transport=transport)).complete(
        AIRequest(task_type=AITaskType.synthesis, prompt="hello")
    )
    assert payload["chat_template_kwargs"] == {"enable_thinking": expected}


def _capture_payload(request: httpx.Request, payload: dict[str, object]) -> httpx.Response:
    payload.update(json.loads(request.content))
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})


def test_adapter_default_thinking_keeps_payload_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    payload: dict[str, object] = {}
    monkeypatch.setattr(adapter_module, "llama_cpp_runtime_config", lambda: _config(thinking="default"))
    transport = httpx.MockTransport(lambda request: _capture_payload(request, payload))
    LocalLlamaCppAdapter(client_factory=lambda: httpx.Client(transport=transport)).complete(
        AIRequest(task_type=AITaskType.synthesis, prompt="hello")
    )
    assert payload == {
        "model": "qwen3.8-27b-q4kxl", "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0, "max_tokens": 2048, "stream": False,
    }


def test_runtime_config_thinking_file_env_and_invalid(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.local_ai.runtime import llama_cpp as runtime_module

    config_file = tmp_path / "llama_cpp.json"
    config_file.write_text(json.dumps({"thinking": "on"}), encoding="utf-8")
    monkeypatch.setenv("JARVISOS_LLAMACPP_CONFIG", str(config_file))
    monkeypatch.delenv("JARVISOS_LLAMACPP_THINKING", raising=False)
    runtime_module.clear_llama_cpp_config_cache()
    assert runtime_module.llama_cpp_runtime_config().thinking == "on"
    monkeypatch.setenv("JARVISOS_LLAMACPP_THINKING", "off")
    runtime_module.clear_llama_cpp_config_cache()
    assert runtime_module.llama_cpp_runtime_config().thinking == "off"
    monkeypatch.setenv("JARVISOS_LLAMACPP_THINKING", "sometimes")
    runtime_module.clear_llama_cpp_config_cache()
    assert runtime_module.llama_cpp_runtime_config().thinking == "default"
    runtime_module.clear_llama_cpp_config_cache()


@pytest.mark.parametrize(("finish", "expected"), [("stop", "stop"), ("length", "length"), ("error", "error")])
def test_finish_reason_mapping(finish: str, expected: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_module, "llama_cpp_runtime_config", lambda: _config())
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={
        "choices": [{"message": {"content": "answer"}, "finish_reason": finish}], "usage": {},
    }))
    result = LocalLlamaCppAdapter(client_factory=lambda: httpx.Client(transport=transport)).complete(
        AIRequest(task_type=AITaskType.synthesis, prompt="hello")
    )
    assert result.finish_reason == expected
    assert (result.error is not None) == (expected == "error")


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


@pytest.mark.parametrize(("handler", "cause"), [
    (lambda _: httpx.Response(401, json={"error": "secret prompt echo"}), "HTTP 401"),
    (lambda _: httpx.Response(503, json={"error": "Loading model"}), "HTTP 503"),
    (_refuse, "ConnectError"),
])
def test_request_failure_reports_bounded_cause(handler, cause: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_module, "llama_cpp_runtime_config", lambda: _config())
    transport = httpx.MockTransport(handler)
    result = LocalLlamaCppAdapter(client_factory=lambda: httpx.Client(transport=transport)).complete(
        AIRequest(task_type=AITaskType.synthesis, prompt="hello")
    )
    assert result.error is not None
    assert result.error.message == f"Local llama-server request failed ({cause})."
    assert "secret" not in result.error.message


def test_loopback_validation_and_digest_cache(tmp_path) -> None:
    assert validate_loopback_host("127.0.0.1") is None
    assert validate_loopback_host("10.0.0.1") == "LLAMACPP_NON_LOOPBACK_ENDPOINT"
    model = tmp_path / "model.gguf"
    model.write_bytes(b"small deterministic fixture")
    expected = hashlib.sha256(model.read_bytes()).hexdigest()
    assert cached_model_sha256(str(model)) == expected
    assert cached_model_sha256(str(model)) == expected


def test_status_digest_hashing_runs_off_request_thread(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = tmp_path / "large.gguf"
    model.write_bytes(b"deferred")
    completed = threading.Event()
    caller = threading.get_ident()
    called_from: list[int] = []

    def slow_hash(path: str) -> str:
        called_from.append(threading.get_ident())
        completed.set()
        return "digest"

    monkeypatch.setattr("app.modules.local_ai.runtime.llama_cpp.cached_model_sha256", slow_hash)
    assert request_model_sha256(str(model)) is None
    assert completed.wait(1)
    assert called_from[0] != caller


@pytest.mark.parametrize(
    ("state", "reachable", "installed", "loaded", "reason"),
    [
        ({"configured": True, "runtime_reachable": False, "model_installed": False,
          "model_loaded": False, "reason_code": "LLAMACPP_RUNTIME_UNREACHABLE", "message": "down"},
         False, False, False, "LLAMACPP_RUNTIME_UNREACHABLE"),
        ({"configured": True, "runtime_reachable": True, "model_installed": True,
          "model_loaded": False, "reason_code": "LLAMACPP_MODEL_MISMATCH", "message": "mismatch"},
         True, True, False, "LLAMACPP_MODEL_MISMATCH"),
        ({"configured": True, "runtime_reachable": True, "model_installed": True,
          "model_loaded": True, "reason_code": None, "message": "ready"},
         True, True, True, None),
    ],
)
def test_availability_matrix(monkeypatch: pytest.MonkeyPatch, state, reachable, installed, loaded, reason) -> None:
    class Owner:
        def status(self):
            return state

    monkeypatch.setattr(
        "app.modules.local_ai.runtime.llama_cpp.get_llama_cpp_runtime_owner", lambda: Owner()
    )
    status = _llamacpp_route_availability("local:llamacpp", "qwen3.8-27b-q4kxl")
    assert status.configured is True
    assert status.runtime_reachable is reachable
    assert status.model_installed is installed
    assert status.model_loaded is loaded
    assert status.reason_code == reason


def test_registry_classifies_llamacpp_as_local_compute() -> None:
    registry = load_default_provider_registry()
    provider = registry.providers["local_llamacpp"]
    binding = registry.bindings["local:llamacpp"]
    assert provider.execution_class == "local_compute"
    assert provider.requires_network is False
    assert provider.api_key_ref is None
    assert binding.provider_id == "local_llamacpp"
    assert binding.max_output_tokens == 2048


def test_registry_binding_reports_configured_llamacpp_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_model_id = "qwen3.8-27b-iq2m"
    monkeypatch.setattr(
        "app.modules.local_ai.runtime.llama_cpp.llama_cpp_runtime_config",
        lambda: _config(model_id=configured_model_id),
    )

    binding = registry_bindings()["local:llamacpp"]

    assert binding.model_id == configured_model_id
    assert binding.provider_id == "local_llamacpp"
    assert binding.max_output_tokens == 2048
    assert binding.context_window_tokens == 8192


class _FakeProcess:
    pid = 332211
    returncode: int | None = None
    def poll(self) -> int | None:
        return self.returncode
    def wait(self, timeout: float) -> int:
        return 0
    def kill(self) -> None:
        pass


def test_lifecycle_start_stop_restart_and_existing_instance(tmp_path) -> None:
    binary = tmp_path / "llama-server"
    model = tmp_path / "model.gguf"
    binary.write_text("fake executable")
    model.write_bytes(b"fake model")
    online = False
    spawn_calls: list[list[str]] = []
    killed: list[tuple[int, int]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(200 if online else 503)
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json={"build_id": "fake-build", "default_generation_settings": {"n_ctx": 4096}})
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.8-27b-q4kxl"}]})
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(respond)
    spawn_env: list[dict[str, str]] = []
    def popen(args: list[str], **kwargs: object) -> _FakeProcess:
        nonlocal online
        online = True
        spawn_calls.append(args)
        spawn_env.append(kwargs["env"])  # type: ignore[arg-type]
        return _FakeProcess()
    owner = LlamaCppRuntimeOwner(
        _config(binary_path=str(binary), model_path=str(model)), popen=popen,
        client_factory=lambda **kwargs: httpx.Client(transport=transport, **kwargs),
        sleep=lambda _: None, kill_group=lambda pid, sig: killed.append((pid, sig)),
    )
    assert owner.start()["model_loaded"] is True
    assert len(spawn_calls) == 1
    key = spawn_env[0]["LLAMA_API_KEY"]
    assert key and key not in spawn_calls[0]
    assert "--no-webui" in spawn_calls[0] and "--n-gpu-layers" in spawn_calls[0]
    assert owner.auth_headers() == {"Authorization": f"Bearer {key}"}
    assert owner.start()["runtime_reachable"] is True
    assert len(spawn_calls) == 1
    owner.stop()
    assert killed
    online = False
    owner.restart()
    assert len(spawn_calls) == 2

    existing = LlamaCppRuntimeOwner(
        _config(binary_path=str(binary), model_path=str(model)), popen=lambda *_args, **_kwargs: pytest.fail("duplicate spawn"),
        client_factory=lambda **kwargs: httpx.Client(transport=transport, **kwargs),
    )
    online = True
    assert existing.start()["runtime_reachable"] is True


def test_spawn_defaults_fit_offload_and_reports_loading_auth_and_exit(tmp_path) -> None:
    binary = tmp_path / "llama-server"
    model = tmp_path / "model.gguf"
    binary.write_text("fake executable")
    model.write_bytes(b"fake model")
    health = 503
    seen_auth: list[str | None] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization"))
        if request.url.path.endswith("/health"):
            return httpx.Response(health)
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json={"build_id": "b11178"})
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.8-27b-q4kxl"}]})
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(respond)
    process = _FakeProcess()
    spawned: list[tuple[list[str], dict[str, object]]] = []

    def popen(args: list[str], **kwargs: object) -> _FakeProcess:
        spawned.append((args, kwargs))
        return process

    log = tmp_path / "logs" / "llama-server.log"
    owner = LlamaCppRuntimeOwner(
        _config(binary_path=str(binary), model_path=str(model), n_gpu_layers=None,
                library_dirs=("/opt/cudart",), log_path=str(log), startup_wait_s=0.0),
        popen=popen, client_factory=lambda **kwargs: httpx.Client(transport=transport, **kwargs),
        sleep=lambda _: None,
    )
    loading = owner.start()
    args, kwargs = spawned[0]
    assert "--n-gpu-layers" not in args
    assert str(kwargs["env"]["LD_LIBRARY_PATH"]).startswith("/opt/cudart")  # type: ignore[index]
    assert log.exists()
    assert loading["reason_code"] == "LLAMACPP_LOADING"
    assert loading["digest_state"] == "not_verified"
    health = 200
    ready = owner.status()
    assert ready["model_loaded"] is True
    assert seen_auth[-1] == f"Bearer {kwargs['env']['LLAMA_API_KEY']}"  # type: ignore[index]
    process.returncode = 1
    exited = owner.status()
    assert exited["spawned_by_jarvis"] is False and exited["last_exit_code"] == 1
    assert exited["crash_count"] == 1 and exited["last_crash_at"]
    assert owner.auth_headers() == {}


def _offline_server(**kwargs: object) -> httpx.Client:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(503)
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json={"build_id": "fake"})
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json=[])

    return httpx.Client(transport=httpx.MockTransport(respond), **kwargs)


def _wait_until(predicate) -> None:
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def test_crashed_managed_process_recovers_once_in_background(tmp_path) -> None:
    binary = tmp_path / "llama-server"
    model = tmp_path / "model.gguf"
    binary.write_text("fake executable")
    model.write_bytes(b"fake model")
    processes: list[_FakeProcess] = []

    def popen(*_args: object, **_kwargs: object) -> _FakeProcess:
        process = _FakeProcess()
        processes.append(process)
        return process

    owner = LlamaCppRuntimeOwner(
        _config(binary_path=str(binary), model_path=str(model), manage=True, startup_wait_s=0),
        popen=popen, client_factory=_offline_server,
    )
    owner.start()
    processes[0].returncode = 7
    recovery = owner.recover_if_crashed()
    assert recovery["reason_code"] == "LLAMACPP_RECOVERING"
    _wait_until(lambda: len(processes) == 2 and not owner._recovery_in_flight)
    assert len(processes) == 2
    assert owner.status()["crash_count"] == 1
    assert owner.recover_if_crashed()["reason_code"] != "LLAMACPP_RECOVERING"


def test_status_poll_alone_recovers_a_crashed_managed_process(tmp_path) -> None:
    binary = tmp_path / "llama-server"
    model = tmp_path / "model.gguf"
    binary.write_text("fake executable")
    model.write_bytes(b"fake model")
    processes: list[_FakeProcess] = []

    def popen(*_args: object, **_kwargs: object) -> _FakeProcess:
        processes.append(_FakeProcess())
        return processes[-1]

    owner = LlamaCppRuntimeOwner(
        _config(binary_path=str(binary), model_path=str(model), manage=True, startup_wait_s=0),
        popen=popen, client_factory=_offline_server,
    )
    owner.start()
    processes[0].returncode = -9
    assert owner.status()["reason_code"] == "LLAMACPP_RECOVERING"
    _wait_until(lambda: len(processes) == 2 and not owner._recovery_in_flight)
    assert owner.status()["crash_count"] == 1 and len(processes) == 2


def test_stop_is_not_counted_as_crash(tmp_path) -> None:
    binary = tmp_path / "llama-server"
    model = tmp_path / "model.gguf"
    binary.write_text("fake executable")
    model.write_bytes(b"fake model")
    process = _FakeProcess()
    owner = LlamaCppRuntimeOwner(
        _config(binary_path=str(binary), model_path=str(model), manage=True, startup_wait_s=0),
        popen=lambda *_args, **_kwargs: process, client_factory=_offline_server,
        kill_group=lambda *_args: None,
    )
    owner.start()
    owner.stop()
    row = owner.status()
    assert row["crash_count"] == 0
    assert row["last_crash_at"] is None


def test_recovery_budget_exhaustion_reports_crash_loop(tmp_path) -> None:
    binary = tmp_path / "llama-server"
    model = tmp_path / "model.gguf"
    binary.write_text("fake executable")
    model.write_bytes(b"fake model")
    processes: list[_FakeProcess] = []

    def popen(*_args: object, **_kwargs: object) -> _FakeProcess:
        process = _FakeProcess()
        processes.append(process)
        return process

    owner = LlamaCppRuntimeOwner(
        _config(binary_path=str(binary), model_path=str(model), manage=True, startup_wait_s=0),
        popen=popen, client_factory=_offline_server,
    )
    owner.start()
    for expected_process_count in (2, 3, 4):
        processes[-1].returncode = 3
        assert owner.recover_if_crashed()["reason_code"] == "LLAMACPP_RECOVERING"
        _wait_until(lambda count=expected_process_count: len(processes) == count and not owner._recovery_in_flight)
    processes[-1].returncode = 3
    row = owner.recover_if_crashed()
    assert row["reason_code"] == "LLAMACPP_CRASH_LOOP"
    assert row["message"] == "Local model crashed repeatedly; restart it from Settings / AI or the launcher"
    assert len(processes) == 4


def test_adopted_server_is_never_restarted(tmp_path) -> None:
    binary = tmp_path / "llama-server"
    model = tmp_path / "model.gguf"
    binary.write_text("fake executable")
    model.write_bytes(b"fake model")
    spawn_calls = 0

    def popen(*_args: object, **_kwargs: object) -> _FakeProcess:
        nonlocal spawn_calls
        spawn_calls += 1
        return _FakeProcess()

    def healthy_server(**kwargs: object) -> httpx.Client:
        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/health"):
                return httpx.Response(200)
            if request.url.path.endswith("/props"):
                return httpx.Response(200, json={"build_id": "fake"})
            if request.url.path.endswith("/v1/models"):
                return httpx.Response(200, json={"data": [{"id": "qwen3.8-27b-q4kxl"}]})
            return httpx.Response(200, json=[])
        return httpx.Client(transport=httpx.MockTransport(respond), **kwargs)

    owner = LlamaCppRuntimeOwner(
        _config(binary_path=str(binary), model_path=str(model), manage=True),
        popen=popen, client_factory=healthy_server,
    )
    assert owner.start()["runtime_reachable"] is True
    assert owner.recover_if_crashed()["runtime_reachable"] is True
    assert spawn_calls == 0


def test_adapter_transport_failure_recovers_but_http_500_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_module, "llama_cpp_runtime_config", lambda: _config())
    calls: list[int] = []

    class Owner:
        def auth_headers(self) -> dict[str, str]:
            return {}

        def recover_if_crashed(self) -> dict[str, str]:
            calls.append(1)
            return {"reason_code": "LLAMACPP_RECOVERING"}

    monkeypatch.setattr(adapter_module, "get_llama_cpp_runtime_owner", lambda: Owner())
    transport = httpx.MockTransport(_refuse)
    failed = LocalLlamaCppAdapter(client_factory=lambda: httpx.Client(transport=transport)).complete(
        AIRequest(task_type=AITaskType.synthesis, prompt="hello")
    )
    assert len(calls) == 1 and failed.error is not None
    assert failed.error.retryable is True and "recovery is in progress" in failed.error.message
    transport = httpx.MockTransport(lambda _: httpx.Response(500))
    failed = LocalLlamaCppAdapter(client_factory=lambda: httpx.Client(transport=transport)).complete(
        AIRequest(task_type=AITaskType.synthesis, prompt="hello")
    )
    assert len(calls) == 1 and failed.error is not None


def test_adapter_sends_assembled_prompt_with_auth_and_route_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(adapter_module, "llama_cpp_runtime_config", lambda: _config(request_timeout_s=900.0))

    class Owner:
        def auth_headers(self) -> dict[str, str]:
            return {"Authorization": "Bearer local-token"}

    monkeypatch.setattr(adapter_module, "get_llama_cpp_runtime_owner", lambda: Owner())

    def respond(request: httpx.Request) -> httpx.Response:
        import json
        captured["auth"] = request.headers.get("authorization")
        captured["timeout"] = request.extensions["timeout"]["read"]
        captured["messages"] = json.loads(request.content)["messages"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    adapter = LocalLlamaCppAdapter(client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    adapter.complete(AIRequest(task_type=AITaskType.synthesis, prompt="SYSTEM: envelope\nUSER: ciao"))
    assert captured["auth"] == "Bearer local-token"
    assert captured["timeout"] == 900.0
    assert captured["messages"] == [{"role": "user", "content": "SYSTEM: envelope\nUSER: ciao"}]


def test_status_does_not_wait_for_a_loading_start(tmp_path) -> None:
    binary = tmp_path / "llama-server"
    model = tmp_path / "model.gguf"
    binary.write_text("fake executable")
    model.write_bytes(b"fake model")
    release = threading.Event()
    owner = LlamaCppRuntimeOwner(
        _config(binary_path=str(binary), model_path=str(model), manage=True, startup_wait_s=2.0),
        popen=lambda *_a, **_k: _FakeProcess(), client_factory=_offline_server,
        sleep=lambda _s: release.wait(2),
    )
    starter = threading.Thread(target=owner.start)
    starter.start()
    _wait_until(lambda: owner._process is not None)
    began = time.monotonic()
    assert owner.status()["reason_code"] in {"LLAMACPP_LOADING", "LLAMACPP_RUNTIME_UNREACHABLE"}
    assert time.monotonic() - began < 1.0
    release.set()
    starter.join(10)
    owner.stop()
