from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.token_flow_service import (
    ID_RE,
    TokenFlowConflictError,
    TokenFlowError,
    TokenFlowNotFoundError,
    _require_flow,
    _safe,
)

SEGMENT_AUTHORITY_VERSION = "token-flow-segment-v0"
SEGMENT_GUARD_VERSION = "token-flow-continuation-guard-v0"
SEGMENT_POLICY_VERSION = "token-flow-segment-policy-v0"
SEGMENT_TTL = timedelta(hours=24)
MAX_SEGMENT_BYTES = 1_048_576
SENSITIVITY_LEVELS = frozenset({"S0", "S1", "S2", "S3", "S4"})


@dataclass(frozen=True, slots=True)
class SegmentMetadata:
    id: str
    flow_id: str
    segment_index: int
    originating_attempt_id: str
    body_digest: str
    byte_count: int
    token_count: int
    sensitivity_level: str
    policy_binding_digest: str
    continuation_guard_digest: str
    created_at: str
    expires_at: str
    expired: bool


@dataclass(frozen=True, slots=True)
class ValidatedSegment:
    metadata: SegmentMetadata
    body_text: str = field(repr=False)


def store_protected_segment(
    *,
    flow_id: str,
    originating_attempt_id: str,
    body_text: str,
    effective_sensitivity_level: str,
    workspace_id: str | None,
    now: datetime | None = None,
) -> SegmentMetadata:
    """Persist one protected output segment from canonical server-owned attempt state.

    The caller supplies only the exact response body, its already-resolved effective
    sensitivity, and the current workspace authority. Flow identity, ordering, token
    count, policy binding, continuation guard, expiry, and digest evidence are derived
    inside the immediate transaction. Exact replay is idempotent.
    """

    flow_id = _safe(flow_id, ID_RE, "flow_id")
    originating_attempt_id = _safe(
        originating_attempt_id, ID_RE, "originating_attempt_id"
    )
    workspace_id = _workspace_id(workspace_id)
    sensitivity = _sensitivity_level(effective_sensitivity_level)
    body, body_bytes, body_digest = _body_evidence(body_text)
    current = _utc_datetime(now)

    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            flow = _require_flow(connection, flow_id)
            _require_workspace(flow, workspace_id)
            attempt = _require_attempt(connection, originating_attempt_id)
            _require_attempt_identity(flow, attempt)

            existing = connection.execute(
                """
                SELECT * FROM ai_flow_segments
                WHERE flow_id = ? AND originating_attempt_id = ?
                ORDER BY segment_index
                """,
                (flow_id, originating_attempt_id),
            ).fetchall()
            if len(existing) > 1:
                raise TokenFlowConflictError(
                    "originating attempt has multiple protected segments"
                )

            policy_digest = _policy_binding_digest(flow, attempt)
            guard_digest = _continuation_guard_digest(flow)
            if existing:
                validated = _validate_segment_row(
                    connection,
                    existing[0],
                    flow=flow,
                    workspace_id=workspace_id,
                    expected_sensitivity=sensitivity,
                    current=current,
                )
                if (
                    validated.body_text != body
                    or validated.metadata.body_digest != body_digest
                    or validated.metadata.byte_count != len(body_bytes)
                    or validated.metadata.policy_binding_digest != policy_digest
                    or validated.metadata.continuation_guard_digest != guard_digest
                ):
                    raise TokenFlowConflictError(
                        "protected segment replay does not match persisted evidence"
                    )
                connection.commit()
                return validated.metadata

            if flow["state"] != "running":
                raise TokenFlowConflictError(
                    "only running flows can accept protected segments"
                )
            _require_latest_successful_output_attempt(
                connection,
                flow=flow,
                attempt=attempt,
                body_digest=body_digest,
            )
            indexes = [
                int(row["segment_index"])
                for row in connection.execute(
                    "SELECT segment_index FROM ai_flow_segments "
                    "WHERE flow_id = ? ORDER BY segment_index",
                    (flow_id,),
                ).fetchall()
            ]
            if indexes != list(range(len(indexes))):
                raise TokenFlowConflictError(
                    "protected segment indexes must be contiguous"
                )
            next_index = len(indexes)
            if next_index > int(flow["max_direct_continuations_snapshot"]):
                raise TokenFlowConflictError(
                    "protected segment count exceeds continuation snapshot"
                )

            token_count = _positive_int(attempt["output_tokens"], "output_tokens")
            segment_id = str(uuid4())
            created_at = current.isoformat()
            expires_at = (current + SEGMENT_TTL).isoformat()
            connection.execute(
                """
                INSERT INTO ai_flow_segments (
                    id, flow_id, segment_index, originating_attempt_id, body_text,
                    body_digest, byte_count, token_count, sensitivity_level,
                    policy_binding_digest, continuation_guard_digest,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment_id,
                    flow_id,
                    next_index,
                    originating_attempt_id,
                    body,
                    body_digest,
                    len(body_bytes),
                    token_count,
                    sensitivity,
                    policy_digest,
                    guard_digest,
                    created_at,
                    expires_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM ai_flow_segments WHERE id = ?", (segment_id,)
            ).fetchone()
            if row is None:
                raise TokenFlowConflictError("protected segment insert was lost")
            metadata = _metadata(row, current=current)
            connection.commit()
            return metadata
        except Exception:
            connection.rollback()
            raise


def read_protected_segment(
    *,
    flow_id: str,
    segment_index: int,
    workspace_id: str | None,
    expected_sensitivity_level: str,
    now: datetime | None = None,
) -> ValidatedSegment:
    """Read one protected body only after complete integrity and authority validation."""

    flow_id = _safe(flow_id, ID_RE, "flow_id")
    index = _nonnegative_int(segment_index, "segment_index")
    workspace_id = _workspace_id(workspace_id)
    sensitivity = _sensitivity_level(expected_sensitivity_level)
    current = _utc_datetime(now)
    with open_sqlite_connection() as connection:
        flow = _require_flow(connection, flow_id)
        _require_workspace(flow, workspace_id)
        row = connection.execute(
            "SELECT * FROM ai_flow_segments WHERE flow_id = ? AND segment_index = ?",
            (flow_id, index),
        ).fetchone()
        if row is None:
            raise TokenFlowNotFoundError(
                f"protected segment {flow_id}:{index} does not exist"
            )
        return _validate_segment_row(
            connection,
            row,
            flow=flow,
            workspace_id=workspace_id,
            expected_sensitivity=sensitivity,
            current=current,
        )


def list_protected_segment_metadata(
    *,
    flow_id: str,
    workspace_id: str | None,
    now: datetime | None = None,
) -> tuple[SegmentMetadata, ...]:
    """Return safe validated metadata without exposing protected segment bodies."""

    flow_id = _safe(flow_id, ID_RE, "flow_id")
    workspace_id = _workspace_id(workspace_id)
    current = _utc_datetime(now)
    with open_sqlite_connection() as connection:
        flow = _require_flow(connection, flow_id)
        _require_workspace(flow, workspace_id)
        rows = connection.execute(
            "SELECT * FROM ai_flow_segments WHERE flow_id = ? ORDER BY segment_index",
            (flow_id,),
        ).fetchall()
        indexes = [int(row["segment_index"]) for row in rows]
        if indexes != list(range(len(rows))):
            raise TokenFlowConflictError(
                "protected segment indexes must be contiguous"
            )
        metadata: list[SegmentMetadata] = []
        for row in rows:
            validated = _validate_segment_row(
                connection,
                row,
                flow=flow,
                workspace_id=workspace_id,
                expected_sensitivity=str(row["sensitivity_level"]),
                current=current,
                allow_expired=True,
            )
            metadata.append(validated.metadata)
        return tuple(metadata)


def _validate_segment_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    flow: sqlite3.Row,
    workspace_id: str | None,
    expected_sensitivity: str,
    current: datetime,
    allow_expired: bool = False,
) -> ValidatedSegment:
    if row["flow_id"] != flow["id"]:
        raise TokenFlowConflictError("protected segment flow identity changed")
    _require_workspace(flow, workspace_id)
    if row["sensitivity_level"] != expected_sensitivity:
        raise TokenFlowConflictError(
            "protected segment sensitivity does not match current authority"
        )

    attempt = _require_attempt(connection, str(row["originating_attempt_id"]))
    _require_attempt_identity(flow, attempt)
    if attempt["status"] != "success" or attempt["output_digest"] is None:
        raise TokenFlowConflictError(
            "protected segment origin is not a successful output attempt"
        )

    body = row["body_text"]
    if not isinstance(body, str):
        raise TokenFlowConflictError("protected segment body is malformed")
    body_bytes = body.encode("utf-8")
    digest = canonical_digest({"text": body})
    if row["body_digest"] != digest or attempt["output_digest"] != digest:
        raise TokenFlowConflictError("protected segment digest evidence changed")
    if int(row["byte_count"]) != len(body_bytes):
        raise TokenFlowConflictError("protected segment byte count changed")
    token_count = _positive_int(row["token_count"], "token_count")
    if token_count != _positive_int(attempt["output_tokens"], "output_tokens"):
        raise TokenFlowConflictError("protected segment token count changed")
    if row["policy_binding_digest"] != _policy_binding_digest(flow, attempt):
        raise TokenFlowConflictError("protected segment policy binding changed")
    if row["continuation_guard_digest"] != _continuation_guard_digest(flow):
        raise TokenFlowConflictError("protected segment continuation guard changed")

    expires_at = _parse_datetime(row["expires_at"], "expires_at")
    expired = expires_at <= current
    if expired and not allow_expired:
        raise TokenFlowConflictError("protected segment has expired")
    metadata = _metadata(row, current=current)
    return ValidatedSegment(metadata=metadata, body_text=body)


def _require_attempt(
    connection: sqlite3.Connection, attempt_id: str
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT id, flow_id, flow_attempt_index, task_kind, status,
               requested_route_class, selected_route_class, provider_id, model_id,
               fallback_index, route_reason_json, output_digest, output_tokens,
               normalized_finish_reason
        FROM ai_jobs WHERE id = ?
        """,
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise TokenFlowNotFoundError(f"ai_job {attempt_id} does not exist")
    return row


def _require_attempt_identity(flow: sqlite3.Row, attempt: sqlite3.Row) -> None:
    if attempt["flow_id"] != flow["id"] or attempt["flow_attempt_index"] is None:
        raise TokenFlowConflictError(
            "protected segment attempt must belong to the flow"
        )
    if attempt["task_kind"] != flow["task_kind"]:
        raise TokenFlowConflictError(
            "protected segment attempt task kind does not match flow"
        )


def _require_latest_successful_output_attempt(
    connection: sqlite3.Connection,
    *,
    flow: sqlite3.Row,
    attempt: sqlite3.Row,
    body_digest: str,
) -> None:
    latest = connection.execute(
        "SELECT MAX(flow_attempt_index) AS n FROM ai_jobs WHERE flow_id = ?",
        (flow["id"],),
    ).fetchone()["n"]
    if latest is None or int(attempt["flow_attempt_index"]) != int(latest):
        raise TokenFlowConflictError(
            "protected segment must originate from the latest flow attempt"
        )
    if attempt["status"] != "success":
        raise TokenFlowConflictError(
            "protected segment requires a successful originating attempt"
        )
    if attempt["output_digest"] != body_digest:
        raise TokenFlowConflictError(
            "protected segment body does not match originating output"
        )
    if attempt["normalized_finish_reason"] is None:
        raise TokenFlowConflictError(
            "protected segment origin requires normalized finish evidence"
        )


def _policy_binding_digest(flow: sqlite3.Row, attempt: sqlite3.Row) -> str:
    try:
        route_reason = json.loads(attempt["route_reason_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise TokenFlowConflictError(
            "protected segment route metadata is malformed"
        ) from exc
    if not isinstance(route_reason, dict):
        raise TokenFlowConflictError(
            "protected segment route metadata is malformed"
        )
    return canonical_digest(
        {
            "schema": SEGMENT_POLICY_VERSION,
            "flow_id": flow["id"],
            "workspace_id": flow["workspace_id"],
            "task_kind": flow["task_kind"],
            "requested_route_class": flow["requested_route_class"],
            "attempt_id": attempt["id"],
            "flow_attempt_index": attempt["flow_attempt_index"],
            "selected_route_class": attempt["selected_route_class"],
            "provider_id": attempt["provider_id"],
            "model_id": attempt["model_id"],
            "fallback_index": attempt["fallback_index"],
            "route_reason": route_reason,
        }
    )


def _continuation_guard_digest(flow: sqlite3.Row) -> str:
    return canonical_digest(
        {
            "schema": SEGMENT_GUARD_VERSION,
            "flow_id": flow["id"],
            "workspace_id": flow["workspace_id"],
            "task_kind": flow["task_kind"],
            "requested_route_class": flow["requested_route_class"],
            "max_direct_continuations_snapshot": flow[
                "max_direct_continuations_snapshot"
            ],
            "config_version": flow["config_version"],
        }
    )


def _metadata(row: sqlite3.Row, *, current: datetime) -> SegmentMetadata:
    expires_at = _parse_datetime(row["expires_at"], "expires_at")
    return SegmentMetadata(
        id=str(row["id"]),
        flow_id=str(row["flow_id"]),
        segment_index=int(row["segment_index"]),
        originating_attempt_id=str(row["originating_attempt_id"]),
        body_digest=str(row["body_digest"]),
        byte_count=int(row["byte_count"]),
        token_count=_positive_int(row["token_count"], "token_count"),
        sensitivity_level=_sensitivity_level(row["sensitivity_level"]),
        policy_binding_digest=str(row["policy_binding_digest"]),
        continuation_guard_digest=str(row["continuation_guard_digest"]),
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]),
        expired=expires_at <= current,
    )


def _body_evidence(value: object) -> tuple[str, bytes, str]:
    if not isinstance(value, str) or not value:
        raise TokenFlowError("protected segment body must be non-empty text")
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_SEGMENT_BYTES:
        raise TokenFlowError("protected segment body exceeds the byte limit")
    return value, encoded, canonical_digest({"text": value})


def _workspace_id(value: object) -> str | None:
    return None if value is None else _safe(value, ID_RE, "workspace_id")


def _require_workspace(flow: sqlite3.Row, workspace_id: str | None) -> None:
    if flow["workspace_id"] != workspace_id:
        raise TokenFlowConflictError(
            "protected segment workspace does not match flow"
        )


def _sensitivity_level(value: object) -> str:
    if not isinstance(value, str) or value not in SENSITIVITY_LEVELS:
        raise TokenFlowError("protected segment sensitivity is unsupported")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TokenFlowConflictError(f"{field_name} must be a positive integer")
    return value


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TokenFlowError(f"{field_name} must be a non-negative integer")
    return value


def _utc_datetime(value: datetime | None) -> datetime:
    current = value if value is not None else datetime.now(UTC)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise TokenFlowError("now must be a timezone-aware datetime")
    return current.astimezone(UTC)


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TokenFlowConflictError(f"{field_name} is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TokenFlowConflictError(f"{field_name} is malformed") from exc
    if parsed.tzinfo is None:
        raise TokenFlowConflictError(f"{field_name} is malformed")
    return parsed.astimezone(UTC)
