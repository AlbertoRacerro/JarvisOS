"""Fail-closed, server-owned IP egress boundary for Hermes chat read tools."""
from __future__ import annotations

import os
import re
from typing import Any

from app.core import packs
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.agent_contracts import CapabilityGrantRef
from app.modules.ai.sensitivity import (
    _SECRET_PATTERNS,
    _pack_ip_patterns,
    deterministic_floor,
)
from app.modules.events.service import log_event

CLOUD_IP_MODE_NO_CLOUD = "no_cloud"
CLOUD_IP_MODE_SANITIZED = "sanitized"
CLOUD_IP_MODES = frozenset({
    CLOUD_IP_MODE_NO_CLOUD,
    CLOUD_IP_MODE_SANITIZED,
})
DEFAULT_CLOUD_IP_MODE = CLOUD_IP_MODE_NO_CLOUD

LOCAL_PROVIDER_MODES = frozenset({
    "fake",
    "local",
    "local_llamacpp",
    "local_ollama",
    "ollama",
    "llamacpp",
})

_KNOWN_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}|Bearer\s+[A-Za-z0-9_.-]+)"
)
_KV_SECRET_RE = re.compile(
    r'(?i)(["\']?)([a-z0-9_.-]{0,64}(?:password|passwd|secret|token|api_key|api-key|apikey)[a-z0-9_.-]*+)\1\s*([:=])\s*([^\s,;\"\'<>(){}\[\]]++|"[^"\r\n]+"|\'[^\'\r\n]+\')'
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.I,
)


def normalize_cloud_ip_mode(value: Any) -> str:
    """Validate and normalize cloud IP mode. Unknown or malformed values fail-closed to no_cloud."""
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in CLOUD_IP_MODES:
            return candidate
    return DEFAULT_CLOUD_IP_MODE


def resolve_cloud_ip_mode() -> str:
    """Resolve server-owned cloud IP mode; unknown values fail closed."""
    env_val = os.getenv("JARVIS_CLOUD_IP_MODE") or os.getenv("JARVISOS_CLOUD_IP_MODE")
    if env_val:
        return normalize_cloud_ip_mode(env_val)

    return DEFAULT_CLOUD_IP_MODE


def is_local_route(
    route_class: str | None = None,
    grant: CapabilityGrantRef | None = None,
    role_id: str | None = None,
) -> bool:
    """Strictly server-owned route classification: local routes bypass cloud egress restrictions."""
    if grant is not None and grant.constraints:
        is_local_constraint = grant.constraints.get("is_local")
        if isinstance(is_local_constraint, bool):
            return is_local_constraint
        grant_route = grant.constraints.get("route_class")
        if grant_route is not None:
            route_class = str(grant_route)

    if route_class is not None:
        if isinstance(route_class, str):
            candidate = route_class.strip()
            if candidate.startswith("local:") or candidate == "fake":
                return True
            if candidate == "hermes:agent":
                try:
                    from app.modules.ai.settings import get_ai_settings

                    settings = get_ai_settings()
                    role_routes = getattr(settings, "hermes_role_routes", {}) or {}
                    role_key = role_id.strip() if isinstance(role_id, str) and role_id.strip() else "isaac"
                    role_route = role_routes.get(role_key)
                    if role_route and isinstance(role_route, str):
                        return role_route.startswith("local:") or role_route == "fake"
                    if role_key != "isaac":
                        isaac_route = role_routes.get("isaac")
                        if isaac_route and isinstance(isaac_route, str):
                            return isaac_route.startswith("local:") or isaac_route == "fake"
                    provider_mode = str(getattr(settings, "provider_mode", "") or "").lower()
                    if provider_mode:
                        return provider_mode in LOCAL_PROVIDER_MODES
                except Exception:
                    pass
                return False
            return False
        return False

    try:
        from app.modules.ai.settings import get_ai_settings

        settings = get_ai_settings()
        provider_mode = str(getattr(settings, "provider_mode", "") or "").lower()
        return provider_mode in LOCAL_PROVIDER_MODES
    except Exception:
        return False


def _redact_kv_secret(match: re.Match[str]) -> str:
    quote = match.group(1)
    key = match.group(2)
    delim = match.group(3)
    if delim == "=":
        return f"{quote}{key}{quote}=[REDACTED_SECRET]"
    return f"{quote}{key}{quote}: [REDACTED_SECRET]"


def sanitize_text(text: str) -> str:
    """Apply deterministic redaction of secrets and pack IP patterns."""
    text = _PRIVATE_KEY_RE.sub("[REDACTED_SECRET]", text)
    text = _KNOWN_SECRET_RE.sub("[REDACTED_SECRET]", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED_SECRET]", text)
    text = _KV_SECRET_RE.sub(_redact_kv_secret, text)

    terms = packs.sensitivity_terms()
    if terms:
        for pat in _pack_ip_patterns(terms):
            text = pat.sub("[REDACTED_IP]", text)

    return text


def check_sensitivity_floor(text: str) -> str | None:
    """Return deterministic sensitivity floor (S4, S3, S2, or None for S0/S1)."""
    return deterministic_floor(text)


def filter_or_sanitize_text(text: str, mode: str) -> tuple[bool, str, str | None]:
    """Filter or sanitize a text snippet according to cloud IP mode.

    Returns (allowed, output_text, reason_code).
    """
    mode = normalize_cloud_ip_mode(mode)
    if mode == CLOUD_IP_MODE_NO_CLOUD:
        floor = check_sensitivity_floor(text)
        if floor in {"S2", "S3", "S4"}:
            return False, "", "egress_pack_blocked"
        return True, text, None

    if mode == CLOUD_IP_MODE_SANITIZED:
        redacted = sanitize_text(text)
        post_floor = check_sensitivity_floor(redacted)
        if post_floor in {"S2", "S3", "S4"}:
            return False, "", "egress_sanitization_required"
        return True, redacted, None



def _record_text_recursive(value: Any) -> str:
    parts: list[str] = []
    if isinstance(value, str):
        parts.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            parts.append(_record_text_recursive(v))
    elif isinstance(value, (list, tuple)):
        for item in value:
            parts.append(_record_text_recursive(item))
    return " ".join(parts)


def _sanitize_value_recursive(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {k: _sanitize_value_recursive(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value_recursive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_value_recursive(item) for item in value)
    return value


def sanitize_record(record: dict[str, Any], mode: str) -> tuple[bool, dict[str, Any], str | None]:
    """Check and optionally sanitize a structured knowledge record.

    Returns (allowed, output_record, reason_code).
    """
    mode = normalize_cloud_ip_mode(mode)
    raw_text = _record_text_recursive(record)

    if mode == CLOUD_IP_MODE_NO_CLOUD:
        floor = check_sensitivity_floor(raw_text)
        if floor in {"S2", "S3", "S4"}:
            return False, {}, "egress_pack_blocked"
        return True, record, None

    if mode == CLOUD_IP_MODE_SANITIZED:
        sanitized = _sanitize_value_recursive(record)
        post_text = _record_text_recursive(sanitized)
        post_floor = check_sensitivity_floor(post_text)
        if post_floor in {"S2", "S3", "S4"}:
            return False, {}, "egress_sanitization_required"
        if isinstance(sanitized, dict):
            sanitized["sanitized"] = True
        return True, sanitized, None



def log_cloud_boundary_event(
    tool_name: str,
    op: str,
    egress_mode: str,
    withheld_count: int,
    reason_code: str,
    workspace_id: str | None = None,
) -> None:
    """Persist metadata only for a cloud-route refusal or withholding."""
    payload = {
        "tool": tool_name,
        "op": op,
        "mode": normalize_cloud_ip_mode(egress_mode),
        "withheld_count": withheld_count,
        "reason_code": reason_code,
    }
    with open_sqlite_connection() as connection:
        has_events_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
    if not has_events_table:
        initialize_database()
    with open_sqlite_connection() as connection:
        log_event(
            connection,
            event_type="hermes.cloud_ip_boundary",
            actor="jarvis",
            target_type="cloud_ip_boundary",
            workspace_id=None,
            payload=payload,
        )
        connection.commit()
