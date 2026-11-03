from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.modules.ai.token_flow_continuation import ContinuationDecision
from app.modules.ai.token_flow_segments import (
    _continuation_guard_digest,
    _parse_datetime,
    _policy_binding_digest,
)
from app.modules.ai.token_flow_service import (
    TokenFlowConflictError,
    _recompute,
    _require_confirmation_ticket_binding,
    _require_flow,
)

_AUTHORITY_KEYS = frozenset(
    {
        "expected_sensitivity_level",
        "flow_id",
        "next_continuation_index",
        "parent_attempt_id",
        "parent_flow_attempt_index",
        "protected_segment_index",
        "version",
    }
)
_AUTHORITY_VERSION = "token-flow-confirmation-resume-v0"


@dataclass(frozen=True, slots=True)
class ContinuationConfirmationAuthority:
    flow_id: str
    parent_attempt_id: str
    parent_flow_attempt_index: int
    next_continuation_index: int
    protected_segment_index: int
    expected_sensitivity_level: str

    def decision(self) -> ContinuationDecision:
        return ContinuationDecision(
            eligible=True,
            reason="eligible",
            flow_id=self.flow_id,
            parent_attempt_id=self.parent_attempt_id,
            parent_flow_attempt_index=self.parent_flow_attempt_index,
            next_continuation_index=self.next_continuation_index,
            protected_segment_index=self.protected_segment_index,
        )


@dataclass(frozen=True, slots=True)
class ContinuationConfirmationResolution:
    authority: ContinuationConfirmationAuthority
    pause_attempt_id: str


def parse_continuation_authority(
    payload_json: str | None,
) -> ContinuationConfirmationAuthority | None:
    if payload_json is None:
        return None
    if not isinstance(payload_json, str) or not payload_json.strip():
        raise TokenFlowConflictError("continuation confirmation authority is malformed")
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise TokenFlowConflictError(
            "continuation confirmation authority is malformed"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != _AUTHORITY_KEYS:
        raise TokenFlowConflictError("continuation confirmation authority shape changed")
    if payload.get("version") != _AUTHORITY_VERSION:
        raise TokenFlowConflictError("continuation confirmation authority version changed")
    flow_id = _text(payload.get("flow_id"), "flow_id")
    parent_attempt_id = _text(payload.get("parent_attempt_id"), "parent_attempt_id")
    parent_index = _integer(
        payload.get("parent_flow_attempt_index"),
        "parent_flow_attempt_index",
        minimum=0,
    )
    continuation_index = _integer(
        payload.get("next_continuation_index"),
        "next_continuation_index",
        minimum=1,
    )
    segment_index = _integer(
        payload.get("protected_segment_index"),
        "protected_segment_index",
        minimum=0,
    )
    sensitivity = payload.get("expected_sensitivity_level")
    if sensitivity not in {"S0", "S1"}:
        raise TokenFlowConflictError(
            "continuation confirmation sensitivity is not externally eligible"
        )
    return ContinuationConfirmationAuthority(
        flow_id=flow_id,
        parent_attempt_id=parent_attempt_id,
        parent_flow_attempt_index=parent_index,
        next_continuation_index=continuation_index,
        protected_segment_index=segment_index,
        expected_sensitivity_level=str(sensitivity),
    )


def validate_pending_continuation_confirmation_in_transaction(
    connection: sqlite3.Connection,
    *,
    flow_id: str,
    ticket_id: str,
    authority_json: str,
    now: datetime | None = None,
) -> ContinuationConfirmationResolution:
    return _validate_context(
        connection,
        flow_id=flow_id,
        ticket_id=ticket_id,
        authority_json=authority_json,
        allowed_ticket_states=frozenset({"pending"}),
        require_live_segment=True,
        now=now,
    )


def activate_consumed_continuation_confirmation_in_transaction(
    connection: sqlite3.Connection,
    *,
    flow_id: str,
    ticket_id: str,
    authority_json: str,
    now: datetime | None = None,
) -> ContinuationConfirmationResolution:
    context = _validate_context(
        connection,
        flow_id=flow_id,
        ticket_id=ticket_id,
        authority_json=authority_json,
        allowed_ticket_states=frozenset({"consumed"}),
        require_live_segment=True,
        now=now,
    )
    authority = context.authority
    detached = connection.execute(
        """
        UPDATE ai_jobs
        SET flow_id = NULL, flow_attempt_index = NULL,
            parent_attempt_id = NULL, continuation_index = NULL
        WHERE id = ? AND flow_id = ? AND flow_attempt_index = ?
          AND parent_attempt_id = ? AND continuation_index = ?
          AND status = 'validation_error' AND adapter_invoked = 0
          AND external_dispatch_state = 'not_started'
        """,
        (
            context.pause_attempt_id,
            flow_id,
            authority.parent_flow_attempt_index + 1,
            authority.parent_attempt_id,
            authority.next_continuation_index,
        ),
    )
    if detached.rowcount != 1:
        raise TokenFlowConflictError(
            "continuation confirmation pause changed before authorization"
        )
    current = _utc_datetime(now).isoformat()
    activated = connection.execute(
        """
        UPDATE ai_flows
        SET state = 'running', updated_at = ?
        WHERE id = ? AND state = 'confirmation_required'
        """,
        (current, flow_id),
    )
    if activated.rowcount != 1:
        raise TokenFlowConflictError(
            "continuation confirmation flow changed before authorization"
        )
    _recompute(connection, flow_id)
    return context


def terminalize_rejected_continuation_confirmation_in_transaction(
    connection: sqlite3.Connection,
    *,
    flow_id: str,
    ticket_id: str,
    terminal_reason: str,
    now: datetime | None = None,
) -> str:
    _require_confirmation_ticket_binding(
        connection,
        flow_id,
        ticket_id=ticket_id,
        allowed_ticket_states=frozenset({"expired", "revoked"}),
        require_unexpired=False,
    )
    flow = _require_flow(connection, flow_id)
    if flow["state"] != "confirmation_required":
        raise TokenFlowConflictError(
            "rejected continuation confirmation flow is not paused"
        )
    pause = connection.execute(
        """
        SELECT id FROM ai_jobs
        WHERE flow_id = ?
        ORDER BY flow_attempt_index DESC
        LIMIT 1
        """,
        (flow_id,),
    ).fetchone()
    if pause is None:
        raise TokenFlowConflictError(
            "rejected continuation confirmation has no pause attempt"
        )
    current = _utc_datetime(now).isoformat()
    updated = connection.execute(
        """
        UPDATE ai_flows
        SET state = 'failed_terminal', terminal_reason = ?,
            terminal_attempt_id = ?, completed_at = ?, updated_at = ?
        WHERE id = ? AND state = 'confirmation_required'
        """,
        (
            terminal_reason,
            str(pause["id"]),
            current,
            current,
            flow_id,
        ),
    )
    if updated.rowcount != 1:
        raise TokenFlowConflictError(
            "rejected continuation confirmation terminalization changed concurrently"
        )
    _recompute(connection, flow_id)
    return str(pause["id"])


def _validate_context(
    connection: sqlite3.Connection,
    *,
    flow_id: str,
    ticket_id: str,
    authority_json: str,
    allowed_ticket_states: frozenset[str],
    require_live_segment: bool,
    now: datetime | None,
) -> ContinuationConfirmationResolution:
    authority = parse_continuation_authority(authority_json)
    if authority is None or authority.flow_id != flow_id:
        raise TokenFlowConflictError(
            "continuation confirmation authority does not match the flow"
        )
    _require_confirmation_ticket_binding(
        connection,
        flow_id,
        ticket_id=ticket_id,
        allowed_ticket_states=allowed_ticket_states,
        require_unexpired=False,
    )
    flow = _require_flow(connection, flow_id)
    if flow["state"] != "confirmation_required":
        raise TokenFlowConflictError("continuation confirmation flow is not paused")
    pause = connection.execute(
        """
        SELECT id, flow_attempt_index, parent_attempt_id, continuation_index
        FROM ai_jobs
        WHERE flow_id = ?
        ORDER BY flow_attempt_index DESC
        LIMIT 1
        """,
        (flow_id,),
    ).fetchone()
    if pause is None:
        raise TokenFlowConflictError("continuation confirmation pause is missing")
    if (
        int(pause["flow_attempt_index"])
        != authority.parent_flow_attempt_index + 1
        or pause["parent_attempt_id"] != authority.parent_attempt_id
        or pause["continuation_index"] != authority.next_continuation_index
    ):
        raise TokenFlowConflictError(
            "continuation confirmation pause lineage changed"
        )
    parent = connection.execute(
        """
        SELECT id, flow_attempt_index, continuation_index, status,
               adapter_invoked, normalized_finish_reason, output_digest,
               output_tokens, requested_route_class, selected_route_class,
               provider_id, model_id, fallback_index, route_reason_json
        FROM ai_jobs WHERE id = ? AND flow_id = ?
        """,
        (authority.parent_attempt_id, flow_id),
    ).fetchone()
    if parent is None:
        raise TokenFlowConflictError("continuation confirmation parent disappeared")
    if int(parent["flow_attempt_index"]) != authority.parent_flow_attempt_index:
        raise TokenFlowConflictError("continuation confirmation parent ordering changed")
    parent_continuation = parent["continuation_index"]
    expected_next = 1 if parent_continuation is None else int(parent_continuation) + 1
    if expected_next != authority.next_continuation_index:
        raise TokenFlowConflictError("continuation confirmation index changed")
    if (
        parent["status"] != "success"
        or parent["adapter_invoked"] != 1
        or parent["normalized_finish_reason"] != "length"
    ):
        raise TokenFlowConflictError(
            "continuation confirmation parent is no longer an exact length stop"
        )
    segment = connection.execute(
        """
        SELECT * FROM ai_flow_segments
        WHERE flow_id = ? AND segment_index = ?
        """,
        (flow_id, authority.protected_segment_index),
    ).fetchone()
    if segment is None:
        raise TokenFlowConflictError(
            "continuation confirmation protected segment disappeared"
        )
    if (
        segment["originating_attempt_id"] != authority.parent_attempt_id
        or segment["body_digest"] != parent["output_digest"]
        or segment["token_count"] != parent["output_tokens"]
        or segment["sensitivity_level"] != authority.expected_sensitivity_level
        or segment["policy_binding_digest"] != _policy_binding_digest(flow, parent)
        or segment["continuation_guard_digest"] != _continuation_guard_digest(flow)
    ):
        raise TokenFlowConflictError(
            "continuation confirmation protected evidence changed"
        )
    if require_live_segment and _parse_datetime(
        segment["expires_at"], "expires_at"
    ) <= _utc_datetime(now):
        raise TokenFlowConflictError(
            "continuation confirmation protected segment expired"
        )
    ticket = connection.execute(
        """
        SELECT source_digests_json FROM egress_confirmation_tickets
        WHERE id = ?
        """,
        (ticket_id,),
    ).fetchone()
    if ticket is None:
        raise TokenFlowConflictError("continuation confirmation ticket disappeared")
    try:
        source_digests = json.loads(ticket["source_digests_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise TokenFlowConflictError(
            "continuation confirmation source digests are malformed"
        ) from exc
    segment_key = f"segment:{authority.protected_segment_index}"
    segment_keys = (
        {
            key
            for key in source_digests
            if isinstance(key, str) and key.startswith("segment:")
        }
        if isinstance(source_digests, dict)
        else set()
    )
    if (
        not isinstance(source_digests, dict)
        or segment_keys != {segment_key}
        or source_digests.get(segment_key) != segment["body_digest"]
    ):
        raise TokenFlowConflictError(
            "continuation confirmation source digest changed"
        )
    return ContinuationConfirmationResolution(
        authority=authority,
        pause_attempt_id=str(pause["id"]),
    )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TokenFlowConflictError(f"continuation confirmation {name} is malformed")
    return value.strip()


def _integer(value: object, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise TokenFlowConflictError(f"continuation confirmation {name} is malformed")
    return value


def _utc_datetime(value: datetime | None) -> datetime:
    current = value if value is not None else datetime.now(UTC)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise TypeError("now must be timezone-aware")
    return current.astimezone(UTC)
