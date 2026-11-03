from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

LOCAL_OLLAMA_ENDPOINT_ENV = "JARVISOS_DEV_MESSAGE_ROUTE_ENDPOINT"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
_GENERATE_PATH = "/api/generate"
_ALLOWED_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class OllamaRuntimeURLs:
    base_url: str
    generate_endpoint: str


class OllamaRuntimeEndpointError(ValueError):
    """Raised when the configured local Ollama endpoint is invalid."""


def configured_ollama_endpoint_raw() -> str:
    return os.getenv(LOCAL_OLLAMA_ENDPOINT_ENV, f"{DEFAULT_OLLAMA_BASE_URL}{_GENERATE_PATH}")


def resolve_ollama_runtime_urls(raw_endpoint: str | None = None) -> OllamaRuntimeURLs:
    normalized_endpoint = _normalize_generate_endpoint(configured_ollama_endpoint_raw() if raw_endpoint is None else raw_endpoint)
    return OllamaRuntimeURLs(
        base_url=_derive_base_url(normalized_endpoint),
        generate_endpoint=normalized_endpoint,
    )


def _normalize_generate_endpoint(raw_endpoint: str) -> str:
    if not isinstance(raw_endpoint, str):
        raise OllamaRuntimeEndpointError("endpoint must be a string")
    if not raw_endpoint.strip():
        raise OllamaRuntimeEndpointError("endpoint must be a non-empty URL")
    parsed = urlparse(raw_endpoint)
    if parsed.scheme != "http":
        raise OllamaRuntimeEndpointError("endpoint must use http")
    if parsed.hostname not in _ALLOWED_LOCAL_HOSTS:
        raise OllamaRuntimeEndpointError("endpoint host must be localhost-only")
    if parsed.username is not None or parsed.password is not None:
        raise OllamaRuntimeEndpointError("endpoint must not include credentials")
    if parsed.query or parsed.fragment:
        raise OllamaRuntimeEndpointError("endpoint must not include query or fragment")

    path = parsed.path or ""
    if path in ("", "/"):
        return urlunparse((parsed.scheme, parsed.netloc, _GENERATE_PATH, "", "", ""))
    if path != _GENERATE_PATH:
        raise OllamaRuntimeEndpointError("endpoint path must be /api/generate")
    return urlunparse((parsed.scheme, parsed.netloc, _GENERATE_PATH, "", "", ""))


def _derive_base_url(generate_endpoint: str) -> str:
    parsed = urlparse(generate_endpoint)
    path = parsed.path or ""
    if path.endswith(_GENERATE_PATH):
        path = path[: -len(_GENERATE_PATH)]
    else:
        path = path.rstrip("/")
    if path == "/":
        path = ""
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
