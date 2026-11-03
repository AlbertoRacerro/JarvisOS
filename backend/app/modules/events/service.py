import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

REDACTED = "[REDACTED]"

SENSITIVE_EXACT_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "bearer_token",
    "access_token",
    "refresh_token",
    "password",
    "passwd",
    "secret",
    "secret_key",
    "private_key",
    "raw_prompt",
    "prompt",
    "raw_stdout",
    "raw_stderr",
}

SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "private_key",
    "secret_key",
    "authorization",
    "bearer",
)

PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_ -]?key|password|token|secret[_ -]?key|private[_ -]?key|bearer)\b\s*(=|:|is)\s*([^\s,;]+)"
)
AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s,;]+)")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def redact_event_payload(payload: object) -> object:
    return _redact_value(payload)


def log_event(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    actor: str,
    target_type: str,
    target_id: str | None = None,
    workspace_id: str | None = None,
    payload: Mapping[str, object] | None = None,
) -> str:
    event_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO events (
            id, workspace_id, event_type, actor, target_type, target_id, payload, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            workspace_id,
            event_type,
            actor,
            target_type,
            target_id,
            json.dumps(redact_event_payload(payload or {})),
            utc_now(),
        ),
    )
    return event_id


def count_events_by_type(connection: sqlite3.Connection, event_type: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS count FROM events WHERE event_type = ?",
        (event_type,),
    ).fetchone()
    return int(row["count"])


def list_events_by_type(connection: sqlite3.Connection, event_type: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM events WHERE event_type = ? ORDER BY created_at ASC",
        (event_type,),
    ).fetchall()


def _redact_value(value: object, key: str | None = None) -> object:
    if key and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(child_key): _redact_value(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower()
    if normalized in SENSITIVE_EXACT_KEYS:
        return True
    return any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)


def _redact_string(value: str) -> str:
    redacted = PRIVATE_KEY_BLOCK_RE.sub(REDACTED, value)
    redacted = AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", redacted)
    redacted = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", redacted)
    redacted = re.sub(r"(?i)\.env", "[REDACTED_ENV_REF]", redacted)
    return redacted
