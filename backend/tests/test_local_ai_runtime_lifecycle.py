from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any
from unittest.mock import Mock

import httpx
from fastapi.testclient import TestClient

from app.modules.local_ai.runtime.lifecycle import (
    DEFAULT_WARM_MODEL,
    LocalAiRuntimeConfig,
    LocalAiRuntimeLifecycle,
    local_ai_runtime_config_from_env,
)


class _FakeResponse:
    def __init__(self, payload: object | None = None, status_code: int = 200) -> None:
        self.payload = payload if payload is not None else {}
        self.status_code = status_code

    def json(self) -> object:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad status", request=httpx.Request("GET", "http://test"), response=None)


class _FakeClient:
    def __init__(self, routes: dict[tuple[str, str], list[object] | object]) -> None:
        self.routes = {key: value if isinstance(value, list) else [value] for key, value in routes.items()}
        self.calls: list[tuple[str, str, float | None, dict[str, object] | None]] = []
        self.closed = False

    def get(self, url: str, timeout: float | None = None) -> _FakeResponse:
        self.calls.append(("GET", url, timeout, None))
        return self._next("GET", url)

    def post(self, url: str, json: dict[str, object], timeout: float | None = None) -> _FakeResponse:
        self.calls.append(("POST", url, timeout, json))
        return self._next("POST", url)

    def close(self) -> None:
        self.closed = True

    def _next(self, method: str, url: str) -> _FakeResponse:
        path = httpx.URL(url).path
        key = (method, path)
        if key not in self.routes:
            raise AssertionError(f"unexpected {method} {path}")
        actions = self.routes[key]
        action = actions.pop(0) if len(actions) > 1 else actions[0]
        if isinstance(action, Exception):
            raise action
        if isinstance(action, _FakeResponse):
            return action
        return _FakeResponse(action)


class _FakeProcess:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.terminated = False
        self.killed = False
        self.wait_calls: list[float | None] = []

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        return 0


def _config(**overrides: object) -> LocalAiRuntimeConfig:
    values: dict[str, object] = {
        "enabled": True,
        "endpoint_raw": "http://127.0.0.1:11434/api/generate",
        "warm_model": DEFAULT_WARM_MODEL,
        "command": "ollama",
        "startup_timeout_s": 0.01,
        "health_interval_s": 0.001,
        "warm_timeout_s": 0.02,
        "warm_keep_alive": "24h",
    }
    values.update(overrides)
    return LocalAiRuntimeConfig(**values)


def _factory(client: _FakeClient):
    return lambda: client


def test_disabled_lifecycle_does_not_call_http_or_subprocess() -> None:
    popen = Mock(side_effect=AssertionError("subprocess must not start"))
    client_factory = Mock(side_effect=AssertionError("HTTP must not be called"))
    lifecycle = LocalAiRuntimeLifecycle(
        config=_config(enabled=False),
        http_client_factory=client_factory,
        popen=popen,
    )

    state = lifecycle.startup_sync()
    shutdown = lifecycle.shutdown_sync()

    assert state.enabled is False
    assert state.startup_attempted is False
    assert state.warm_scheduled is False
    assert shutdown.shutdown_called is True
    popen.assert_not_called()
    client_factory.assert_not_called()


def test_already_running_schedules_and_warms_qwen_without_spawning() -> None:
    client = _FakeClient(
        {
            ("GET", "/api/version"): {"version": "0.9.6"},
            ("GET", "/api/tags"): {"models": [{"name": DEFAULT_WARM_MODEL}]},
            ("POST", "/api/generate"): {"response": "warm ok"},
        }
    )
    popen = Mock(side_effect=AssertionError("already running must not spawn"))
    lifecycle = LocalAiRuntimeLifecycle(config=_config(), http_client_factory=_factory(client), popen=popen)

    state = lifecycle.startup_sync()
    warm_state = lifecycle.warm_sync()
    shutdown_state = lifecycle.shutdown_sync()

    assert state.already_running is True
    assert state.spawned_by_jarvis is False
    assert state.warm_scheduled is True
    assert warm_state.warm_succeeded is True
    assert shutdown_state.shutdown_called is True
    popen.assert_not_called()
    assert [call[0:2] for call in client.calls] == [
        ("GET", "http://127.0.0.1:11434/api/version"),
        ("GET", "http://127.0.0.1:11434/api/tags"),
        ("POST", "http://127.0.0.1:11434/api/generate"),
    ]
    assert client.calls[-1][3] == {
        "model": DEFAULT_WARM_MODEL,
        "prompt": "warm",
        "stream": False,
        "keep_alive": "24h",
    }


def test_spawn_when_down_sets_max_loaded_models_and_shutdown_kills_spawned_process() -> None:
    process = _FakeProcess(pid=9876)
    popen_calls: list[dict[str, object]] = []

    def popen(*args: object, **kwargs: object) -> _FakeProcess:
        popen_calls.append({"args": args, "kwargs": kwargs})
        return process

    client = _FakeClient(
        {
            ("GET", "/api/version"): [
                httpx.ConnectError("down"),
                {"version": "0.9.6"},
            ],
            ("GET", "/api/tags"): {"models": [{"name": DEFAULT_WARM_MODEL}]},
            ("POST", "/api/generate"): {"response": "warm ok"},
        }
    )
    lifecycle = LocalAiRuntimeLifecycle(
        config=_config(),
        http_client_factory=_factory(client),
        popen=popen,
        environ={},
        sleep=lambda _seconds: None,
        platform_system=lambda: "Linux",
    )

    state = lifecycle.startup_sync()
    lifecycle.warm_sync()
    lifecycle.shutdown_sync()

    assert state.spawned_by_jarvis is True
    assert state.process_pid == 9876
    assert popen_calls[0]["args"] == (["ollama", "serve"],)
    assert popen_calls[0]["kwargs"]["shell"] is False
    assert popen_calls[0]["kwargs"]["cwd"] is None
    assert popen_calls[0]["kwargs"]["env"]["OLLAMA_MAX_LOADED_MODELS"] == "1"
    assert process.terminated is True
    assert process.wait_calls


def test_spawn_preserves_user_max_loaded_models() -> None:
    process = _FakeProcess()
    popen_kwargs: dict[str, object] = {}

    def popen(*args: object, **kwargs: object) -> _FakeProcess:
        popen_kwargs.update(kwargs)
        return process

    client = _FakeClient(
        {
            ("GET", "/api/version"): [httpx.ConnectError("down"), {"version": "0.9.6"}],
            ("GET", "/api/tags"): {"models": [{"name": DEFAULT_WARM_MODEL}]},
        }
    )
    lifecycle = LocalAiRuntimeLifecycle(
        config=_config(),
        http_client_factory=_factory(client),
        popen=popen,
        environ={"OLLAMA_MAX_LOADED_MODELS": "2"},
        sleep=lambda _seconds: None,
    )

    lifecycle.startup_sync()

    assert popen_kwargs["env"]["OLLAMA_MAX_LOADED_MODELS"] == "2"


def test_startup_timeout_terminates_spawned_process_and_does_not_warm() -> None:
    process = _FakeProcess()
    client = _FakeClient(
        {
            ("GET", "/api/version"): httpx.ConnectError("down"),
        }
    )
    lifecycle = LocalAiRuntimeLifecycle(
        config=_config(startup_timeout_s=0.0),
        http_client_factory=_factory(client),
        popen=Mock(return_value=process),
        sleep=lambda _seconds: None,
        platform_system=lambda: "Linux",
    )

    state = lifecycle.startup_sync()

    assert state.startup_error_type == "startup_timeout"
    assert state.warm_scheduled is False
    assert process.terminated is True


def test_missing_warm_model_does_not_generate_or_auto_pull() -> None:
    client = _FakeClient(
        {
            ("GET", "/api/version"): {"version": "0.9.6"},
            ("GET", "/api/tags"): {"models": [{"name": "gemma4:12b-it-qat"}]},
        }
    )
    lifecycle = LocalAiRuntimeLifecycle(config=_config(), http_client_factory=_factory(client))

    state = lifecycle.startup_sync()

    assert state.missing_warm_model is True
    assert state.startup_error_type == "missing_warm_model"
    assert state.warm_scheduled is False
    assert all(call[0] != "POST" for call in client.calls)


def test_missing_warm_model_after_spawn_keeps_process_until_shutdown() -> None:
    process = _FakeProcess()
    client = _FakeClient(
        {
            ("GET", "/api/version"): [httpx.ConnectError("down"), {"version": "0.9.6"}],
            ("GET", "/api/tags"): {"models": [{"name": "gemma4:12b-it-qat"}]},
        }
    )
    lifecycle = LocalAiRuntimeLifecycle(
        config=_config(),
        http_client_factory=_factory(client),
        popen=Mock(return_value=process),
        sleep=lambda _seconds: None,
        platform_system=lambda: "Linux",
    )

    state = lifecycle.startup_sync()

    assert state.spawned_by_jarvis is True
    assert state.missing_warm_model is True
    assert process.terminated is False

    lifecycle.shutdown_sync()

    assert process.terminated is True


def test_invalid_endpoint_fails_closed_before_http_or_subprocess() -> None:
    client_factory = Mock(side_effect=AssertionError("HTTP must not be called"))
    popen = Mock(side_effect=AssertionError("subprocess must not start"))
    lifecycle = LocalAiRuntimeLifecycle(
        config=_config(endpoint_raw="http://example.com:11434/api/generate"),
        http_client_factory=client_factory,
        popen=popen,
    )

    state = lifecycle.startup_sync()

    assert state.startup_error_type == "invalid_endpoint"
    assert state.warm_scheduled is False
    client_factory.assert_not_called()
    popen.assert_not_called()


def test_warm_model_override_does_not_change_route_bindings() -> None:
    client = _FakeClient(
        {
            ("GET", "/api/version"): {"version": "0.9.6"},
            ("GET", "/api/tags"): {"models": [{"name": "test:warm"}]},
            ("POST", "/api/generate"): {"response": "warm ok"},
        }
    )
    lifecycle = LocalAiRuntimeLifecycle(
        config=_config(warm_model="test:warm"),
        http_client_factory=_factory(client),
    )

    state = lifecycle.startup_sync()
    lifecycle.warm_sync()

    from app.modules.ai.execution import _default_bindings

    bindings = _default_bindings()
    assert state.warm_model == "test:warm"
    assert client.calls[-1][3]["model"] == "test:warm"
    assert bindings["local:fast"].model_id == "qwen3:8b"
    assert bindings["local:general"].model_id == "gemma4:12b-it-qat"
    assert bindings["local:gemma"].model_id == "gemma4:12b-it-qat"


def test_create_app_import_and_construction_do_not_start_lifecycle(monkeypatch) -> None:
    import app.main as main

    factory = Mock(side_effect=AssertionError("lifespan factory must not run during create_app"))
    monkeypatch.setattr(main, "create_local_ai_runtime_lifecycle_from_env", factory)

    main.create_app()

    factory.assert_not_called()


def test_fastapi_lifespan_invokes_lifecycle_without_real_process_or_network(monkeypatch) -> None:
    import app.main as main

    class _FakeLifecycle:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        async def startup(self) -> None:
            self.started = True

        async def shutdown(self) -> None:
            self.stopped = True

    lifecycle = _FakeLifecycle()
    monkeypatch.setattr(main, "create_local_ai_runtime_lifecycle_from_env", Mock(return_value=lifecycle))

    with TestClient(main.create_app()) as client:
        assert client.get("/health").status_code == 200
        assert lifecycle.started is True

    assert lifecycle.stopped is True


def test_shutdown_is_idempotent() -> None:
    process = _FakeProcess()
    lifecycle = LocalAiRuntimeLifecycle(config=_config(), popen=Mock(return_value=process), platform_system=lambda: "Linux")
    lifecycle._process = process
    lifecycle.state.spawned_by_jarvis = True
    lifecycle.state.process_pid = process.pid

    lifecycle.shutdown_sync()
    lifecycle.shutdown_sync()

    assert process.terminated is True
    assert len(process.wait_calls) == 1


def test_async_startup_uses_to_thread_and_schedules_warm_without_awaiting() -> None:
    client = _FakeClient(
        {
            ("GET", "/api/version"): {"version": "0.9.6"},
            ("GET", "/api/tags"): {"models": [{"name": DEFAULT_WARM_MODEL}]},
            ("POST", "/api/generate"): {"response": "warm ok"},
        }
    )
    to_thread_calls: list[str] = []
    scheduled: list[Coroutine[Any, Any, Any]] = []

    async def fake_to_thread(func, *args, **kwargs):
        to_thread_calls.append(func.__name__)
        return func(*args, **kwargs)

    def fake_create_task(coro):
        scheduled.append(coro)
        return coro

    lifecycle = LocalAiRuntimeLifecycle(
        config=_config(),
        http_client_factory=_factory(client),
        to_thread=fake_to_thread,
        create_task=fake_create_task,
    )

    async def run() -> None:
        state = await lifecycle.startup()
        assert state.warm_scheduled is True
        assert state.warm_started is False
        assert to_thread_calls == ["startup_sync"]
        assert len(scheduled) == 1
        await scheduled[0]

    asyncio.run(run())

    assert to_thread_calls == ["startup_sync", "warm_sync"]
    assert lifecycle.state.warm_succeeded is True


def test_windows_tree_kill_targets_only_spawned_pid_with_taskkill() -> None:
    process = _FakeProcess(pid=2468)
    run = Mock()
    lifecycle = LocalAiRuntimeLifecycle(
        config=_config(),
        process_run=run,
        platform_system=lambda: "Windows",
    )
    lifecycle._process = process
    lifecycle.state.spawned_by_jarvis = True
    lifecycle.state.process_pid = process.pid

    lifecycle.shutdown_sync()

    run.assert_called_once_with(["taskkill", "/F", "/T", "/PID", "2468"], shell=False, check=True, timeout=5.0)
    assert process.terminated is False


def test_windows_taskkill_failure_falls_back_to_process_terminate() -> None:
    process = _FakeProcess(pid=2468)
    run = Mock(side_effect=subprocess_error())
    lifecycle = LocalAiRuntimeLifecycle(
        config=_config(),
        process_run=run,
        platform_system=lambda: "Windows",
    )
    lifecycle._process = process
    lifecycle.state.spawned_by_jarvis = True
    lifecycle.state.process_pid = process.pid

    lifecycle.shutdown_sync()

    assert process.terminated is True


def test_warm_uses_shared_generate_endpoint_and_rejects_invalid_endpoint_before_http() -> None:
    client = _FakeClient({("POST", "/api/generate"): {"response": "warm ok"}})
    lifecycle = LocalAiRuntimeLifecycle(
        config=_config(endpoint_raw="http://127.0.0.1:11434"),
        http_client_factory=_factory(client),
    )

    lifecycle.warm_sync()

    assert client.calls[0][1] == "http://127.0.0.1:11434/api/generate"

    bad_client_factory = Mock(side_effect=AssertionError("HTTP must not be called"))
    bad_lifecycle = LocalAiRuntimeLifecycle(
        config=_config(endpoint_raw="http://127.0.0.1:11434/api/tags"),
        http_client_factory=bad_client_factory,
    )

    state = bad_lifecycle.warm_sync()

    assert state.warm_error_type == "invalid_endpoint"
    bad_client_factory.assert_not_called()


def test_config_from_env_defaults_disabled_and_reads_overrides(monkeypatch) -> None:
    monkeypatch.delenv("JARVISOS_MANAGE_OLLAMA", raising=False)
    assert local_ai_runtime_config_from_env().enabled is False

    monkeypatch.setenv("JARVISOS_MANAGE_OLLAMA", "yes")
    monkeypatch.setenv("JARVISOS_OLLAMA_WARM_MODEL", "test:warm")
    monkeypatch.setenv("JARVISOS_OLLAMA_COMMAND", "ollama-test")
    monkeypatch.setenv("JARVISOS_OLLAMA_STARTUP_TIMEOUT_S", "3.5")
    monkeypatch.setenv("JARVISOS_OLLAMA_HEALTH_INTERVAL_S", "0.5")
    monkeypatch.setenv("JARVISOS_OLLAMA_WARM_TIMEOUT_S", "4.5")
    monkeypatch.setenv("JARVISOS_OLLAMA_WARM_KEEP_ALIVE", "12h")

    config = local_ai_runtime_config_from_env()

    assert config.enabled is True
    assert config.warm_model == "test:warm"
    assert config.command == "ollama-test"
    assert config.startup_timeout_s == 3.5
    assert config.health_interval_s == 0.5
    assert config.warm_timeout_s == 4.5
    assert config.warm_keep_alive == "12h"


def subprocess_error() -> Exception:
    return RuntimeError("taskkill failed")
