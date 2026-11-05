from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import secrets
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from app.core.paths import build_paths

_CONFIG_ENV = "JARVISOS_LLAMACPP_CONFIG"
_API_KEY_ENV = "JARVISOS_LLAMACPP_API_KEY"
_API_KEY_FILE_ENV = "JARVISOS_LLAMACPP_API_KEY_FILE"
_LOGGER = logging.getLogger(__name__)
_HASH_CACHE: dict[tuple[str, int, int], str] = {}
_HASH_LOCK = threading.Lock()


def _write_api_key_file(api_key: str) -> None:
    configured = os.getenv(_API_KEY_FILE_ENV)
    if configured is None:
        return
    temporary: Path | None = None
    try:
        target = Path(configured)
        if not target.is_absolute():
            raise ValueError("relative API key file")
        temporary = target.parent / f".{target.name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            data = api_key.encode("utf-8")
            while data:
                written = os.write(fd, data)
                if not written:
                    raise OSError("short API key file write")
                data = data[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, target)
    except (OSError, ValueError):
        _LOGGER.warning("Could not write llama-server API key file.")
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@dataclass(frozen=True)
class LlamaCppRuntimeConfig:
    binary_path: str = ""
    model_path: str = ""
    model_sha256: str = ""
    model_id: str = "qwen3.8-27b-q4kxl"
    host: str = "127.0.0.1"
    port: int = 8080
    ctx_size: int = 8192
    # None leaves llama-server's --fit to size GPU offload from measured free memory.
    n_gpu_layers: int | None = None
    extra_args: tuple[str, ...] = ()
    library_dirs: tuple[str, ...] = ()
    pinned_build_id: str = ""
    manage: bool = False
    timeout_s: float = 2.0
    request_timeout_s: float = 900.0
    startup_wait_s: float = 10.0
    log_path: str = ""
    thinking: str = "default"
    # True only when llama-server was started with a multimodal projector (--mmproj in extra_args).
    vision: bool = False

    @property
    def origin(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def base_url(self) -> str:
        return f"{self.origin}/v1"


@lru_cache(maxsize=1)
def llama_cpp_runtime_config() -> LlamaCppRuntimeConfig:
    path = Path(os.getenv(_CONFIG_ENV, build_paths().data_root / "settings" / "llama_cpp.json"))
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        raw = {}
    def get(name: str, env: str, default: Any) -> Any:
        value = os.getenv(env)
        return value if value is not None else raw.get(name, default)
    args = get("extra_args", "JARVISOS_LLAMACPP_EXTRA_ARGS", [])
    if isinstance(args, str):
        args = args.split()
    library_dirs = get("library_dirs", "JARVISOS_LLAMACPP_LIBRARY_DIRS", [])
    if isinstance(library_dirs, str):
        library_dirs = library_dirs.split(os.pathsep)
    n_gpu_layers = get("n_gpu_layers", "JARVISOS_LLAMACPP_N_GPU_LAYERS", None)
    thinking = str(get("thinking", "JARVISOS_LLAMACPP_THINKING", "default"))
    if thinking not in {"default", "on", "off"}:
        thinking = "default"
    return LlamaCppRuntimeConfig(
        binary_path=str(get("binary_path", "JARVISOS_LLAMACPP_BINARY", "")),
        model_path=str(get("model_path", "JARVISOS_LLAMACPP_MODEL", "")),
        model_sha256=str(get("model_sha256", "JARVISOS_LLAMACPP_MODEL_SHA256", "")),
        model_id=str(get("model_id", "JARVISOS_LLAMACPP_MODEL_ID", "qwen3.8-27b-q4kxl")),
        host=str(get("host", "JARVISOS_LLAMACPP_HOST", "127.0.0.1")),
        port=int(get("port", "JARVISOS_LLAMACPP_PORT", 8080)),
        ctx_size=int(get("ctx_size", "JARVISOS_LLAMACPP_CTX_SIZE", 8192)),
        n_gpu_layers=int(n_gpu_layers) if n_gpu_layers not in (None, "") else None,
        extra_args=tuple(str(arg) for arg in args) if isinstance(args, list) else (),
        library_dirs=tuple(str(item) for item in library_dirs if item) if isinstance(library_dirs, list) else (),
        pinned_build_id=str(get("pinned_build_id", "JARVISOS_LLAMACPP_BUILD_ID", "")),
        manage=str(get("manage", "JARVISOS_MANAGE_LLAMACPP", "false")).lower() in {"1", "true", "yes"},
        timeout_s=float(get("timeout_s", "JARVISOS_LLAMACPP_TIMEOUT_S", 2.0)),
        request_timeout_s=float(get("request_timeout_s", "JARVISOS_LLAMACPP_REQUEST_TIMEOUT_S", 900.0)),
        startup_wait_s=float(get("startup_wait_s", "JARVISOS_LLAMACPP_STARTUP_WAIT_S", 10.0)),
        log_path=str(get("log_path", "JARVISOS_LLAMACPP_LOG", build_paths().data_root / "logs" / "llama-server.log")),
        thinking=thinking,
        vision=str(get("vision", "JARVISOS_LLAMACPP_VISION", "false")).lower() in {"1", "true", "yes"},
    )


def clear_llama_cpp_config_cache() -> None:
    llama_cpp_runtime_config.cache_clear()


def validate_loopback_host(host: str) -> str | None:
    if os.getenv("WSL_DISTRO_NAME") and host not in {"127.0.0.1", "localhost", "::1"}:
        return "LLAMACPP_UNSUPPORTED_WINDOWS_WSL_TOPOLOGY"
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return "LLAMACPP_NON_LOOPBACK_ENDPOINT"
    if platform.system() == "Windows" and os.getenv("WSL_DISTRO_NAME"):
        return "LLAMACPP_UNSUPPORTED_WINDOWS_WSL_TOPOLOGY"
    return None


def cached_model_sha256(path: str) -> str | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    key = (str(Path(path).resolve()), stat.st_size, stat.st_mtime_ns)
    with _HASH_LOCK:
        if key in _HASH_CACHE:
            return _HASH_CACHE[key]
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    result = digest.hexdigest()
    with _HASH_LOCK:
        _HASH_CACHE.clear()
        _HASH_CACHE[key] = result
    return result


def _digest_key(path: str) -> tuple[str, int, int] | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return (str(Path(path).resolve()), stat.st_size, stat.st_mtime_ns)


def peek_model_sha256(path: str) -> tuple[str | None, bool]:
    """Return (cached digest, hashing pending) without reading the model file."""
    key = _digest_key(path)
    with _HASH_LOCK:
        return (_HASH_CACHE.get(key) if key else None, key in _HASH_PENDING if key else False)


def request_model_sha256(path: str) -> str | None:
    """Return a cached digest or schedule one background hash of the model file."""
    key = _digest_key(path)
    if key is None:
        return None
    with _HASH_LOCK:
        cached = _HASH_CACHE.get(key)
        if cached is not None:
            return cached
        if key in _HASH_PENDING:
            return None
        _HASH_PENDING.add(key)
    thread = threading.Thread(target=_hash_model, args=(path, key), daemon=True)
    thread.start()
    return None


_HASH_PENDING: set[tuple[str, int, int]] = set()


def _hash_model(path: str, key: tuple[str, int, int]) -> None:
    try:
        cached_model_sha256(path)
    except OSError:
        pass
    finally:
        with _HASH_LOCK:
            _HASH_PENDING.discard(key)


class LlamaCppRuntimeOwner:
    """One process owner for a configured local llama-server."""

    def __init__(self, config: LlamaCppRuntimeConfig | None = None, *, popen: Any = subprocess.Popen,
                 client_factory: Any = httpx.Client, sleep: Any = time.sleep,
                 kill_group: Any = os.killpg) -> None:
        self.config = config or llama_cpp_runtime_config()
        self._popen = popen
        self._client_factory = client_factory
        self._sleep = sleep
        self._kill_group = kill_group
        self._process: Any | None = None
        self._api_key: str | None = None
        self._exit_code: int | None = None
        self._lock = threading.RLock()
        self._reap_lock = threading.Lock()
        self._stop_requested = False
        self._crash_count = 0
        self._last_crash_at: str | None = None
        self._crashed = False
        self._recovery_in_flight = False
        self._automatic_restart_times: deque[float] = deque()
        self.active_model_id: str | None = None
        self.default_model_id: str | None = None

    @property
    def non_default_active(self) -> bool:
        return self.active_model_id is not None and self.active_model_id != self.default_model_id

    def switch_model(self, config: LlamaCppRuntimeConfig, model_id: str, default_id: str) -> dict[str, Any]:
        with self._lock:
            # Keep product requests blocked throughout a switch away from the default.
            self.active_model_id = model_id if model_id != default_id else self.active_model_id
            self.default_model_id = default_id
            self.stop()
            self.config = config
            result = self.start()
            self.active_model_id = model_id
            return result

    @property
    def api_key(self) -> str | None:
        """Per-launch loopback token for a spawned server, or the operator's token for an adopted one."""
        return self._api_key or os.getenv(_API_KEY_ENV) or None

    def auth_headers(self) -> dict[str, str]:
        key = self.api_key
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _reap(self) -> None:
        with self._reap_lock:
            self._reap_unlocked()

    def _reap_unlocked(self) -> None:
        if self._process is not None and self._process.poll() is not None:
            self._exit_code = self._process.returncode
            if not self._stop_requested:
                self._crash_count += 1
                self._last_crash_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._crashed = True
            self._process, self._api_key = None, None
            self._stop_requested = False

    def status(self) -> dict[str, Any]:
        # Status polls must not wait behind a model load in start(). They are also what notices
        # an unexpected exit when no request is in flight (the UI stops sending to an
        # unavailable route), so they schedule the bounded recovery whenever the lock is free.
        row = self._snapshot()
        if self._crashed and self.config.manage and self._lock.acquire(blocking=False):
            try:
                return self.recover_if_crashed()
            finally:
                self._lock.release()
        return row

    def _snapshot(self, *, include_recovery: bool = True) -> dict[str, Any]:
        config = self.config
        self._reap()
        reason = validate_loopback_host(config.host)
        row: dict[str, Any] = {
            "backend": "llama.cpp", "configured": bool(config.binary_path and config.model_path),
            "runtime_reachable": False, "model_installed": bool(config.model_path and Path(config.model_path).is_file()),
            "model_loaded": False, "reason_code": reason,
            "message": "Configure a llama-server binary and GGUF model in data-root/settings/llama_cpp.json.",
            "build_id": config.pinned_build_id or None, "model_file": config.model_path or None,
            "configured_sha256": config.model_sha256 or None,
            "model_sha256": None, "pid": None, "ctx_size": config.ctx_size,
            "n_gpu_layers": config.n_gpu_layers, "vram_bytes": None, "ram_bytes": None,
            "request_slots": None, "spawned_by_jarvis": self._process is not None,
            "digest_state": None, "last_exit_code": self._exit_code,
            "thinking": config.thinking, "crash_count": self._crash_count,
            "model_id": self.active_model_id or config.model_id,
            "last_crash_at": self._last_crash_at,
        }
        if include_recovery and self._recovery_in_flight:
            row["reason_code"] = "LLAMACPP_RECOVERING"
            row["message"] = "Local llama-server crashed; automatic recovery is loading the configured model."
            return row
        if reason:
            row["message"] = (
                "Windows-to-WSL llama-server endpoints are unsupported; run Jarvis and llama-server in the same network namespace."
                if reason == "LLAMACPP_UNSUPPORTED_WINDOWS_WSL_TOPOLOGY"
                else "llama-server must use an HTTP loopback address (127.0.0.1, localhost, or ::1)."
            )
            return row
        if config.model_path:
            # Never read a multi-gigabyte model on the status path; verification is explicit.
            digest, pending = peek_model_sha256(config.model_path)
            row["model_sha256"] = digest
            row["digest_state"] = ("hashing" if pending else "not_verified" if digest is None
                                   else "verified" if not config.model_sha256 or digest == config.model_sha256
                                   else "mismatch")
        if self._process is not None:
            row["pid"] = getattr(self._process, "pid", None)
        if not config.binary_path or not config.model_path:
            row["reason_code"] = row["reason_code"] or "LLAMACPP_NOT_CONFIGURED"
            return row
        try:
            with self._client_factory(timeout=config.timeout_s, headers=self.auth_headers()) as client:
                health = client.get(f"{config.origin}/health")
                if health.status_code == 503 and self._process is not None:
                    row["reason_code"] = "LLAMACPP_LOADING"
                    row["message"] = "llama-server is loading the configured model."
                    return row
                row["runtime_reachable"] = health.is_success
                props = client.get(f"{config.origin}/props")
                if props.status_code == 401:
                    row["reason_code"] = "LLAMACPP_AUTH_REQUIRED"
                    row["message"] = f"llama-server requires an API key; set {_API_KEY_ENV} for an externally started server."
                    return row
                props.raise_for_status()
                data = props.json()
                row["build_id"] = data.get("build_id") or row["build_id"]
                row["ctx_size"] = data.get("default_generation_settings", {}).get("n_ctx") or row["ctx_size"]
                models_response = client.get(f"{config.origin}/v1/models")
                models_response.raise_for_status()
                model_rows = models_response.json().get("data", [])
                expected = config.model_id
                model_ids = {item.get("id") for item in model_rows if isinstance(item, dict)}
                row["model_loaded"] = bool(model_ids.intersection({expected, Path(config.model_path).name}))
                slots = client.get(f"{config.origin}/slots")
                if slots.is_success and isinstance(slots.json(), list):
                    row["request_slots"] = len(slots.json())
                row["reason_code"] = None if row["model_loaded"] else "LLAMACPP_MODEL_MISMATCH"
                row["message"] = "llama-server is reachable and serving its configured model." if row["model_loaded"] else "llama-server is reachable, but its loaded model does not match the configured model."
        except Exception:
            row["reason_code"] = "LLAMACPP_RUNTIME_UNREACHABLE"
            row["message"] = "llama-server is not reachable at the configured loopback address."
        return row

    def start(self) -> dict[str, Any]:
        with self._lock:
            existing = self._snapshot(include_recovery=False)
            if existing["runtime_reachable"]:
                return existing
            reason = validate_loopback_host(self.config.host)
            if reason:
                existing["reason_code"] = reason
                return existing
            if not self.config.binary_path or not self.config.model_path:
                existing["reason_code"] = "LLAMACPP_NOT_CONFIGURED"
                return existing
            if not Path(self.config.binary_path).is_file() or not Path(self.config.model_path).is_file():
                existing["reason_code"] = "LLAMACPP_FILE_MISSING"
                existing["message"] = "Configured llama-server binary or GGUF file does not exist."
                return existing
            config = self.config
            args = [config.binary_path, "--model", config.model_path, "--alias", config.model_id,
                    "--host", config.host, "--port", str(config.port), "--ctx-size", str(config.ctx_size),
                    "--jinja", "--no-webui", *config.extra_args]
            if config.n_gpu_layers is not None:
                args += ["--n-gpu-layers", str(config.n_gpu_layers)]
            # The loopback port is reachable from any local browser page; a per-launch
            # token passed via the environment (not argv) keeps it Jarvis-only.
            api_key = secrets.token_urlsafe(32)
            env = {**os.environ, "LLAMA_API_KEY": api_key}
            if config.library_dirs:
                env["LD_LIBRARY_PATH"] = os.pathsep.join(
                    [*config.library_dirs, *filter(None, [os.environ.get("LD_LIBRARY_PATH")])])
            log_path = Path(config.log_path) if config.log_path else None
            if log_path is not None:
                log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("ab") if log_path is not None else subprocess.DEVNULL
            try:
                self._process = self._popen(args, env=env, stdout=log, stderr=subprocess.STDOUT,
                                            start_new_session=(os.name != "nt"))
            finally:
                if log_path is not None:
                    log.close()  # type: ignore[union-attr]
            _write_api_key_file(api_key)
            self._api_key, self._exit_code = api_key, None
            self._stop_requested = False
            self._crashed = False
            # A cold GGUF load can take minutes; status reports LLAMACPP_LOADING meanwhile.
            deadline = time.monotonic() + config.startup_wait_s
            while time.monotonic() < deadline:
                snapshot = self._snapshot(include_recovery=False)
                if snapshot["runtime_reachable"]:
                    return snapshot
                self._sleep(0.1)
            return self._snapshot(include_recovery=False)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._process is not None:
                self._stop_requested = True
                process, self._process = self._process, None
                try:
                    if os.name != "nt":
                        self._kill_group(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                    process.wait(timeout=30)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                self._api_key = None
            return self.status()

    def recover_if_crashed(self) -> dict[str, Any]:
        """Schedule one bounded restart after an unexpected exit of our managed process."""
        with self._lock:
            self._reap()
            now = time.monotonic()
            while self._automatic_restart_times and now - self._automatic_restart_times[0] >= 600:
                self._automatic_restart_times.popleft()
            if self._recovery_in_flight:
                row = self._snapshot()
                row["reason_code"] = "LLAMACPP_RECOVERING"
                row["message"] = "Local llama-server crashed; automatic recovery is loading the configured model."
                return row
            if not self._crashed or not self.config.manage or self._process is not None:
                return self._snapshot()
            if len(self._automatic_restart_times) >= 3:
                row = self._snapshot()
                row["reason_code"] = "LLAMACPP_CRASH_LOOP"
                row["message"] = "Local model crashed repeatedly; restart it from Settings / AI or the launcher"
                return row
            self._automatic_restart_times.append(now)
            self._crashed = False
            self._recovery_in_flight = True
            thread = threading.Thread(target=self._recover, daemon=True, name="llamacpp-recovery")
            thread.start()
            row = self._snapshot()
            row["reason_code"] = "LLAMACPP_RECOVERING"
            row["message"] = "Local llama-server crashed; automatic recovery is loading the configured model."
            return row

    def _recover(self) -> None:
        try:
            self.start()
        finally:
            with self._lock:
                self._recovery_in_flight = False

    def verify_digest(self) -> dict[str, Any]:
        """Schedule one background SHA-256 of the configured model; poll status for the result."""
        if self.config.model_path:
            request_model_sha256(self.config.model_path)
        return self.status()

    def restart(self) -> dict[str, Any]:
        self.stop()
        return self.start()


_OWNER: LlamaCppRuntimeOwner | None = None
_OWNER_LOCK = threading.Lock()


def get_llama_cpp_runtime_owner() -> LlamaCppRuntimeOwner:
    global _OWNER
    with _OWNER_LOCK:
        if _OWNER is None:
            from fastapi import HTTPException

            from app.modules.local_ai.runtime.model_registry import config_for_entry, read_registry

            try:
                entries, present = read_registry()
                default = next(entry for entry in entries if entry.qualification == "default") if present else None
            except HTTPException:
                default = None
            _OWNER = LlamaCppRuntimeOwner(config_for_entry(default) if default else None)
            if default:
                _OWNER.default_model_id = default.id
                _OWNER.active_model_id = default.id
        return _OWNER
