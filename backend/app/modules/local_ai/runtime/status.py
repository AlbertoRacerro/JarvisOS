from __future__ import annotations

import copy
import threading
import time
from typing import Any

import httpx

from app.modules.ai.execution import _default_bindings
from app.modules.ai.providers.local_ollama_adapter import LOCAL_OLLAMA_PROVIDER_ID
from app.modules.local_ai.runtime.ollama import (
    OllamaRuntimeEndpointError,
    configured_ollama_endpoint_raw,
    resolve_ollama_runtime_urls,
)

LOCAL_RUNTIME_STATUS_TIMEOUT_S = 1.5
_STATUS_CACHE_TTL_S = 3.0
_STATUS_CACHE_LOCK = threading.Lock()
_STATUS_CACHE: tuple[float, dict[str, Any]] | None = None


def get_local_ai_runtime_status(*, client: Any | None = None) -> dict[str, Any]:
    """Return a short-lived status projection; injected clients always probe fresh."""
    global _STATUS_CACHE
    if client is None:
        now = time.monotonic()
        with _STATUS_CACHE_LOCK:
            if _STATUS_CACHE is not None and now - _STATUS_CACHE[0] < _STATUS_CACHE_TTL_S:
                return copy.deepcopy(_STATUS_CACHE[1])
            status = _probe_local_ai_runtime_status()
            _STATUS_CACHE = (time.monotonic(), status)
            return copy.deepcopy(status)
    return _probe_local_ai_runtime_status(client=client)


def _probe_local_ai_runtime_status(*, client: Any | None = None) -> dict[str, Any]:
    configured_route_models = _configured_local_route_models()
    configured_endpoint = configured_ollama_endpoint_raw()
    status: dict[str, Any] = {
        "ollama_reachable": False,
        "ollama_version": None,
        "installed_models": [],
        "installed_model_sizes": {},
        "loaded_models": [],
        "configured_route_models": configured_route_models,
        "missing_models": dict(configured_route_models),
        "endpoint": configured_endpoint,
        "error_type": None,
        "error_message": None,
        "tags_error_type": None,
        "ps_error_type": None,
    }

    try:
        urls = resolve_ollama_runtime_urls(configured_endpoint)
    except OllamaRuntimeEndpointError as exc:
        status["error_type"] = "invalid_endpoint"
        status["error_message"] = str(exc)
        return status
    status["endpoint"] = urls.base_url

    client_obj = client or httpx.Client()
    should_close = client is None
    try:
        try:
            version_response = client_obj.get(
                _api_url(urls.base_url, "/api/version"),
                timeout=LOCAL_RUNTIME_STATUS_TIMEOUT_S,
            )
            version_response.raise_for_status()
            version_payload = version_response.json()
            status["ollama_reachable"] = True
            status["ollama_version"] = (
                version_payload.get("version") if isinstance(version_payload.get("version"), str) else None
            )
        except Exception as exc:
            status["error_type"] = type(exc).__name__
            status["error_message"] = str(exc)
            return status

        try:
            tags_response = client_obj.get(_api_url(urls.base_url, "/api/tags"), timeout=LOCAL_RUNTIME_STATUS_TIMEOUT_S)
            tags_response.raise_for_status()
            tags_payload = tags_response.json()
            status["installed_models"] = _parse_installed_models(tags_payload)
            status["installed_model_sizes"] = _parse_installed_model_sizes(tags_payload)
            installed = set(status["installed_models"])
            status["missing_models"] = {
                route_class: model_id
                for route_class, model_id in configured_route_models.items()
                if model_id not in installed
            }
        except Exception as exc:
            status["tags_error_type"] = type(exc).__name__

        try:
            ps_response = client_obj.get(_api_url(urls.base_url, "/api/ps"), timeout=LOCAL_RUNTIME_STATUS_TIMEOUT_S)
            ps_response.raise_for_status()
            status["loaded_models"] = _parse_loaded_models(ps_response.json())
        except Exception as exc:
            status["ps_error_type"] = type(exc).__name__

        return status
    finally:
        if should_close:
            client_obj.close()


def clear_local_ai_runtime_status_cache() -> None:
    """Clear cached status for deterministic tests and operator reconfiguration."""
    global _STATUS_CACHE
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE = None


def _configured_local_route_models() -> dict[str, str]:
    configured: dict[str, str] = {}
    for route_class, binding in _default_bindings().items():
        if binding.provider_id == LOCAL_OLLAMA_PROVIDER_ID:
            configured[route_class] = binding.model_id
    return configured


def _api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def _parse_installed_models(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        raise ValueError("tags payload must be an object")
    models = payload.get("models")
    if not isinstance(models, list):
        raise ValueError("tags payload missing models list")
    installed: list[str] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        value = item.get("name")
        if not isinstance(value, str):
            value = item.get("model")
        if isinstance(value, str) and value not in installed:
            installed.append(value)
    return installed


def _parse_installed_model_sizes(payload: object) -> dict[str, int]:
    """On-disk size per installed model; the lower bound a cold load must fit."""
    sizes: dict[str, int] = {}
    models = payload.get("models") if isinstance(payload, dict) else None
    for item in models if isinstance(models, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") if isinstance(item.get("name"), str) else item.get("model")
        size = _optional_int(item.get("size"))
        if isinstance(name, str) and size is not None and size > 0:
            sizes.setdefault(name, size)
    return sizes


def _parse_loaded_models(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("ps payload must be an object")
    models = payload.get("models")
    if not isinstance(models, list):
        raise ValueError("ps payload missing models list")
    loaded: list[dict[str, Any]] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        model = item.get("model")
        if not isinstance(name, str):
            name = model if isinstance(model, str) else None
        if not isinstance(name, str):
            continue
        size = _optional_int(item.get("size"))
        size_vram = _optional_int(item.get("size_vram"))
        spilled = None
        if size is not None and size_vram is not None:
            spilled = size_vram < size
        loaded.append(
            {
                "name": name,
                "model": model if isinstance(model, str) else None,
                "size": size,
                "size_vram": size_vram,
                "processor": item.get("processor") if isinstance(item.get("processor"), str) else None,
                "until": item.get("until") if isinstance(item.get("until"), str) else None,
                "spilled": spilled,
            }
        )
    return loaded


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
