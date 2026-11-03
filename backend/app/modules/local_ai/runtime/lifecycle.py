from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import httpx

from app.modules.local_ai.runtime.ollama import (
    OllamaRuntimeEndpointError,
    configured_ollama_endpoint_raw,
    resolve_ollama_runtime_urls,
)

MANAGE_OLLAMA_ENV = "JARVISOS_MANAGE_OLLAMA"
WARM_MODEL_ENV = "JARVISOS_OLLAMA_WARM_MODEL"
COMMAND_ENV = "JARVISOS_OLLAMA_COMMAND"
STARTUP_TIMEOUT_ENV = "JARVISOS_OLLAMA_STARTUP_TIMEOUT_S"
HEALTH_INTERVAL_ENV = "JARVISOS_OLLAMA_HEALTH_INTERVAL_S"
WARM_TIMEOUT_ENV = "JARVISOS_OLLAMA_WARM_TIMEOUT_S"
WARM_KEEP_ALIVE_ENV = "JARVISOS_OLLAMA_WARM_KEEP_ALIVE"
MAX_LOADED_MODELS_ENV = "OLLAMA_MAX_LOADED_MODELS"

DEFAULT_WARM_MODEL = "qwen3:8b"
DEFAULT_COMMAND = "ollama"
DEFAULT_STARTUP_TIMEOUT_S = 10.0
DEFAULT_HEALTH_INTERVAL_S = 0.25
DEFAULT_WARM_TIMEOUT_S = 10.0
DEFAULT_WARM_KEEP_ALIVE = "24h"
_SHUTDOWN_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class LocalAiRuntimeConfig:
    enabled: bool
    endpoint_raw: str
    warm_model: str = DEFAULT_WARM_MODEL
    command: str = DEFAULT_COMMAND
    startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S
    health_interval_s: float = DEFAULT_HEALTH_INTERVAL_S
    warm_timeout_s: float = DEFAULT_WARM_TIMEOUT_S
    warm_keep_alive: str = DEFAULT_WARM_KEEP_ALIVE


@dataclass
class LocalAiRuntimeState:
    enabled: bool = False
    endpoint: str | None = None
    generate_endpoint: str | None = None
    already_running: bool = False
    spawned_by_jarvis: bool = False
    process_pid: int | None = None
    startup_attempted: bool = False
    startup_succeeded: bool = False
    startup_error_type: str | None = None
    startup_error_message: str | None = None
    warm_model: str = DEFAULT_WARM_MODEL
    warm_scheduled: bool = False
    warm_started: bool = False
    warm_succeeded: bool = False
    warm_error_type: str | None = None
    warm_error_message: str | None = None
    missing_warm_model: bool = False
    shutdown_called: bool = False


def create_local_ai_runtime_lifecycle_from_env() -> LocalAiRuntimeLifecycle:
    return LocalAiRuntimeLifecycle(config=local_ai_runtime_config_from_env())


def local_ai_runtime_config_from_env() -> LocalAiRuntimeConfig:
    return LocalAiRuntimeConfig(
        enabled=_truthy(os.getenv(MANAGE_OLLAMA_ENV)),
        endpoint_raw=configured_ollama_endpoint_raw(),
        warm_model=_env_str(WARM_MODEL_ENV, DEFAULT_WARM_MODEL),
        command=_env_str(COMMAND_ENV, DEFAULT_COMMAND),
        startup_timeout_s=_env_float(STARTUP_TIMEOUT_ENV, DEFAULT_STARTUP_TIMEOUT_S),
        health_interval_s=_env_float(HEALTH_INTERVAL_ENV, DEFAULT_HEALTH_INTERVAL_S),
        warm_timeout_s=_env_float(WARM_TIMEOUT_ENV, DEFAULT_WARM_TIMEOUT_S),
        warm_keep_alive=_env_str(WARM_KEEP_ALIVE_ENV, DEFAULT_WARM_KEEP_ALIVE),
    )


class LocalAiRuntimeLifecycle:
    def __init__(
        self,
        *,
        config: LocalAiRuntimeConfig,
        http_client_factory: Callable[[], Any] = httpx.Client,
        popen: Callable[..., Any] = subprocess.Popen,
        process_run: Callable[..., Any] = subprocess.run,
        environ: dict[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        platform_system: Callable[[], str] = platform.system,
        to_thread: Callable[..., Coroutine[Any, Any, Any]] = asyncio.to_thread,
        create_task: Callable[[Coroutine[Any, Any, Any]], Any] = asyncio.create_task,
    ) -> None:
        self.config = config
        self.state = LocalAiRuntimeState(enabled=config.enabled, warm_model=config.warm_model)
        self._http_client_factory = http_client_factory
        self._popen = popen
        self._process_run = process_run
        self._environ = environ if environ is not None else os.environ
        self._sleep = sleep
        self._monotonic = monotonic
        self._platform_system = platform_system
        self._to_thread = to_thread
        self._create_task = create_task
        self._process: Any | None = None
        self._warm_task: Any | None = None

    async def startup(self) -> LocalAiRuntimeState:
        state = await self._to_thread(self.startup_sync)
        if state.warm_scheduled:
            self._warm_task = self._create_task(self.warm())
        return state

    def startup_sync(self) -> LocalAiRuntimeState:
        if not self.config.enabled:
            return self.state

        self.state.startup_attempted = True
        try:
            urls = resolve_ollama_runtime_urls(self.config.endpoint_raw)
        except OllamaRuntimeEndpointError as exc:
            self._record_startup_error("invalid_endpoint", str(exc))
            return self.state

        self.state.endpoint = urls.base_url
        self.state.generate_endpoint = urls.generate_endpoint

        client = self._http_client_factory()
        try:
            if self._version_reachable(client, urls.base_url):
                self.state.already_running = True
                self.state.startup_succeeded = True
            else:
                self._spawn_ollama()
                if not self._wait_until_reachable(client, urls.base_url):
                    self._record_startup_error("startup_timeout", "Ollama did not become reachable before timeout")
                    self._terminate_spawned_process()
                    return self.state
                self.state.startup_succeeded = True

            installed_models = self._fetch_installed_models(client, urls.base_url)
            if self.config.warm_model not in installed_models:
                self.state.missing_warm_model = True
                self._record_startup_error(
                    "missing_warm_model",
                    f"Configured warm model is not installed: {self.config.warm_model}",
                )
                return self.state

            self.state.warm_scheduled = True
            return self.state
        except Exception as exc:
            self._record_startup_error(type(exc).__name__, str(exc))
            return self.state
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    async def warm(self) -> LocalAiRuntimeState:
        return await self._to_thread(self.warm_sync)

    def warm_sync(self) -> LocalAiRuntimeState:
        if not self.config.enabled:
            return self.state

        try:
            urls = resolve_ollama_runtime_urls(self.config.endpoint_raw)
        except OllamaRuntimeEndpointError as exc:
            self._record_warm_error("invalid_endpoint", str(exc))
            return self.state

        self.state.generate_endpoint = urls.generate_endpoint
        self.state.warm_started = True
        client = self._http_client_factory()
        try:
            response = client.post(
                urls.generate_endpoint,
                json={
                    "model": self.config.warm_model,
                    "prompt": "warm",
                    "stream": False,
                    "keep_alive": self.config.warm_keep_alive,
                },
                timeout=self.config.warm_timeout_s,
            )
            response.raise_for_status()
            self.state.warm_succeeded = True
            return self.state
        except Exception as exc:
            self._record_warm_error(type(exc).__name__, str(exc))
            return self.state
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    async def shutdown(self) -> LocalAiRuntimeState:
        return await self._to_thread(self.shutdown_sync)

    def shutdown_sync(self) -> LocalAiRuntimeState:
        if self.state.shutdown_called:
            return self.state
        self.state.shutdown_called = True
        self._terminate_spawned_process()
        return self.state

    def _spawn_ollama(self) -> None:
        env = dict(self._environ)
        env.setdefault(MAX_LOADED_MODELS_ENV, "1")
        self._process = self._popen([self.config.command, "serve"], shell=False, cwd=None, env=env)
        self.state.spawned_by_jarvis = True
        self.state.process_pid = getattr(self._process, "pid", None)

    def _wait_until_reachable(self, client: Any, base_url: str) -> bool:
        deadline = self._monotonic() + self.config.startup_timeout_s
        while self._monotonic() < deadline:
            if self._version_reachable(client, base_url):
                return True
            self._sleep(self.config.health_interval_s)
        return self._version_reachable(client, base_url)

    def _version_reachable(self, client: Any, base_url: str) -> bool:
        try:
            response = client.get(_api_url(base_url, "/api/version"), timeout=self.config.health_interval_s)
            response.raise_for_status()
            return True
        except Exception:
            return False

    def _fetch_installed_models(self, client: Any, base_url: str) -> set[str]:
        response = client.get(_api_url(base_url, "/api/tags"), timeout=self.config.health_interval_s)
        response.raise_for_status()
        payload = response.json()
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise ValueError("tags payload missing models list")
        installed: set[str] = set()
        for item in models:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str):
                name = item.get("model")
            if isinstance(name, str):
                installed.add(name)
        return installed

    def _terminate_spawned_process(self) -> None:
        if not self.state.spawned_by_jarvis or self._process is None:
            return
        pid = self.state.process_pid
        if pid is not None and self._platform_system().lower() == "windows":
            try:
                self._process_run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    shell=False,
                    check=True,
                    timeout=_SHUTDOWN_TIMEOUT_S,
                )
                return
            except Exception:
                pass
        self._terminate_process_fallback()

    def _terminate_process_fallback(self) -> None:
        if self._process is None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=_SHUTDOWN_TIMEOUT_S)
        except Exception:
            try:
                self._process.kill()
            except Exception:
                pass

    def _record_startup_error(self, error_type: str, message: str) -> None:
        self.state.startup_error_type = error_type
        self.state.startup_error_message = message

    def _record_warm_error(self, error_type: str, message: str) -> None:
        self.state.warm_error_type = error_type
        self.state.warm_error_message = message


def _api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def _truthy(value: str | None) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"} if value is not None else False


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return raw if raw is not None and raw.strip() else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = float(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default
