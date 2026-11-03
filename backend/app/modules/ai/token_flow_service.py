from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.ai.context_builder import canonical_digest
from app.modules.events.service import utc_now

FlowState = Literal[
    "running",
    "confirmation_required",
    "complete",
    "partial_terminal",
    "failed_terminal",
    "cancelled_terminal",
]
Flow = dict[str, object]

TERMINAL = frozenset(
    {"complete", "partial_terminal", "failed_terminal", "cancelled_terminal"}
)
TRANSITIONS = {
    "running": frozenset({"confirmation_required", *TERMINAL}),
    "confirmation_required": TERMINAL,
}
EXECUTION_CLASSES = frozenset(
    {"none", "synthetic", "local_compute", "external_provider", "legacy_unknown"}
)
DISPATCH_STATES = frozenset({"not_applicable", "not_started", "started", "unknown"})
ACCOUNTING_BASES = frozenset(
    {
        "no_execution",
        "synthetic_not_economic",
        "local_compute_unpriced",
        "external_not_sent",
        "provider_exact",
        "conservative_standard_input",
        "conservative_estimated_usage",
        "legacy_unknown",
    }
)
USAGE_SOURCES = frozenset({"actual", "mixed", "estimated", "none"})
TASK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
ROUTE_RE = re.compile(r"^(?:auto|[a-z][a-z0-9_]*:[a-z][a-z0-9_]*)$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
SPEND_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
OUTPUT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class TokenFlowError(ValueError):
    pass


class TokenFlowNotFoundError(TokenFlowError):
    pass


class TokenFlowConflictError(TokenFlowError):
    pass


def create_flow_in_transaction(
    connection: sqlite3.Connection,
    *,
    task_kind: str,
    requested_route_class: str | None = None,
    workspace_id: str | None = None,
) -> str:
    """Create one canonical running flow inside the caller-owned transaction."""

    task_kind = _safe(task_kind, TASK_RE, "task_kind")
    if requested_route_class is not None:
        requested_route_class = _safe(requested_route_class, ROUTE_RE, "route")
    if workspace_id is not None:
        workspace_id = _safe(workspace_id, ID_RE, "workspace_id")
    row = connection.execute(
        "SELECT max_direct_continuations FROM ai_settings WHERE id = 'default'"
    ).fetchone()
    if row is None:
        raise TokenFlowConflictError("default ai_settings row is required")
    snapshot = _nonnegative_int(row["max_direct_continuations"], "snapshot")
    if snapshot > 16:
        raise TokenFlowError("continuation snapshot must be between 0 and 16")
    flow_id, now = str(uuid4()), utc_now()
    connection.execute(
        """
        INSERT INTO ai_flows (
            id, workspace_id, task_kind, requested_route_class, state,
            max_direct_continuations_snapshot, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
        """,
        (flow_id, workspace_id, task_kind, requested_route_class, snapshot, now, now),
    )
    return flow_id


def create_flow(
    *,
    task_kind: str,
    requested_route_class: str | None = None,
    workspace_id: str | None = None,
) -> Flow:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            flow_id = create_flow_in_transaction(
                connection,
                task_kind=task_kind,
                requested_route_class=requested_route_class,
                workspace_id=workspace_id,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return get_flow(flow_id)


def validate_existing_flow_for_execution(
    flow_id: str,
    *,
    task_kind: str,
    requested_route_class: str | None = None,
    workspace_id: str | None = None,
) -> Flow:
    """Fail closed unless a pre-created flow is pristine and exactly request-bound."""

    flow_id = _safe(flow_id, ID_RE, "flow_id")
    task_kind = _safe(task_kind, TASK_RE, "task_kind")
    if requested_route_class is not None:
        requested_route_class = _safe(requested_route_class, ROUTE_RE, "route")
    if workspace_id is not None:
        workspace_id = _safe(workspace_id, ID_RE, "workspace_id")
    with open_sqlite_connection() as connection:
        row = _require_flow(connection, flow_id)
        if row["state"] != "running":
            raise TokenFlowConflictError("pre-created flow must still be running")
        if row["task_kind"] != task_kind:
            raise TokenFlowConflictError("pre-created flow task kind does not match request")
        if row["workspace_id"] != workspace_id:
            raise TokenFlowConflictError("pre-created flow workspace does not match request")
        if row["requested_route_class"] != requested_route_class:
            raise TokenFlowConflictError("pre-created flow requested route does not match request")
        if _nonnegative_int(row["attempt_count"], "attempt_count") != 0:
            raise TokenFlowConflictError("pre-created flow already owns an attempt")
        attempts = connection.execute(
            "SELECT COUNT(*) AS n FROM ai_jobs WHERE flow_id = ?", (flow_id,)
        ).fetchone()
        if attempts is None or int(attempts["n"]) != 0:
            raise TokenFlowConflictError("pre-created flow already owns an ai_job")
        if any(
            row[field] is not None
            for field in (
                "terminal_reason",
                "terminal_attempt_id",
                "completed_at",
                "cancelled_at",
                "final_accounting_digest",
                "final_output_digest",
            )
        ):
            raise TokenFlowConflictError("pre-created flow already carries terminal metadata")
        owners = connection.execute(
            "SELECT COUNT(*) AS n FROM ai_thread_interactions WHERE flow_id = ?", (flow_id,)
        ).fetchone()
        if owners is not None and int(owners["n"]) > 1:
            raise TokenFlowConflictError("pre-created flow has conflicting thread ownership")
        return _decode_flow(row)


def get_flow(flow_id: str) -> Flow:
    flow_id = _safe(flow_id, ID_RE, "flow_id")
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT * FROM ai_flows WHERE id = ?", (flow_id,)).fetchone()
    if row is None:
        raise TokenFlowNotFoundError(f"flow {flow_id} does not exist")
    return _decode_flow(row)


def get_confirmation_flow_for_ticket(ticket_id: str) -> Flow:
    """Recover the one canonical paused flow bound to a pending 059b ticket.

    This lookup deliberately accepts a ticket that has expired by wall clock while its
    persisted state is still ``pending``. ``consume_confirmation_ticket`` remains the
    authority that atomically marks it expired or revoked on access.
    """

    ticket_id = _safe(ticket_id, ID_RE, "ticket_id")
    with open_sqlite_connection() as connection:
        rows = connection.execute(
            """
            SELECT flow.id AS flow_id
            FROM ai_flows AS flow
            JOIN ai_jobs AS attempt ON attempt.flow_id = flow.id
            WHERE flow.state = 'confirmation_required'
              AND attempt.flow_attempt_index = (
                  SELECT MAX(candidate.flow_attempt_index)
                  FROM ai_jobs AS candidate
                  WHERE candidate.flow_id = flow.id
              )
              AND json_valid(attempt.route_reason_json)
              AND json_extract(attempt.route_reason_json, '$.egress_ticket_id') = ?
            ORDER BY flow.id
            """,
            (ticket_id,),
        ).fetchall()
        matches: list[Flow] = []
        for row in rows:
            flow_id = str(row["flow_id"])
            _require_confirmation_ticket_binding(
                connection,
                flow_id,
                ticket_id=ticket_id,
                allowed_ticket_states=frozenset({"pending"}),
                require_unexpired=False,
            )
            matches.append(_decode_flow(_require_flow(connection, flow_id)))
    if len(matches) != 1:
        raise TokenFlowConflictError(
            "confirmation ticket must resolve to exactly one canonical paused flow"
        )
    return matches[0]


def activate_confirmation_flow(*, flow_id: str, ticket_id: str) -> Flow:
    """Open one paused flow only after its exact ticket leaves ``pending``.

    This is a bounded 061a bridge for the single confirmed attempt. It does not expose
    a generic confirmation-required -> running transition and does not implement 061b
    continuation, segment, assembly, or restart recovery semantics.
    """

    flow_id = _safe(flow_id, ID_RE, "flow_id")
    ticket_id = _safe(ticket_id, ID_RE, "ticket_id")
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            flow = _require_flow(connection, flow_id)
            if flow["state"] != "confirmation_required":
                raise TokenFlowConflictError(
                    "only confirmation_required flows can accept a confirmed attempt"
                )
            _require_confirmation_ticket_binding(
                connection,
                flow_id,
                ticket_id=ticket_id,
                allowed_ticket_states=frozenset({"consumed", "expired", "revoked"}),
                require_unexpired=False,
            )
            updated = connection.execute(
                """
                UPDATE ai_flows
                SET state = 'running', updated_at = ?
                WHERE id = ? AND state = 'confirmation_required'
                """,
                (utc_now(), flow_id),
            )
            if updated.rowcount != 1:
                raise TokenFlowConflictError(
                    "confirmation flow activation changed concurrently"
                )
            result = _recompute(connection, flow_id)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise


def link_attempt_to_flow(*, flow_id: str, attempt_id: str) -> Flow:
    flow_id = _safe(flow_id, ID_RE, "flow_id")
    attempt_id = _safe(attempt_id, ID_RE, "attempt_id")
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            flow = _require_flow(connection, flow_id)
            if flow["state"] != "running":
                raise TokenFlowConflictError("only running flows can accept attempts")
            attempt = connection.execute(
                "SELECT flow_id, flow_attempt_index, task_kind FROM ai_jobs WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise TokenFlowNotFoundError(f"ai_job {attempt_id} does not exist")
            if attempt["task_kind"] != flow["task_kind"]:
                raise TokenFlowConflictError("ai_job task kind does not match flow")
            if attempt["flow_id"] == flow_id:
                if attempt["flow_attempt_index"] is None:
                    raise TokenFlowConflictError("linked ai_job has no attempt index")
            elif attempt["flow_id"] is not None or attempt["flow_attempt_index"] is not None:
                raise TokenFlowConflictError("ai_job is already linked to another flow")
            else:
                row = connection.execute(
                    "SELECT COALESCE(MAX(flow_attempt_index), -1) + 1 n "
                    "FROM ai_jobs WHERE flow_id = ?",
                    (flow_id,),
                ).fetchone()
                updated = connection.execute(
                    "UPDATE ai_jobs SET flow_id = ?, flow_attempt_index = ? "
                    "WHERE id = ? AND flow_id IS NULL AND flow_attempt_index IS NULL",
                    (flow_id, int(row["n"]), attempt_id),
                )
                if updated.rowcount != 1:
                    raise TokenFlowConflictError("ai_job link changed concurrently")
            _refresh_identity(connection, flow_id)
            connection.commit()
            return _decode_flow(_require_flow(connection, flow_id))
        except Exception:
            connection.rollback()
            raise


def transition_flow_state(
    *,
    flow_id: str,
    new_state: FlowState,
    terminal_reason: str | None = None,
    terminal_attempt_id: str | None = None,
    terminal_response_text: str | None = None,
) -> Flow:
    flow_id = _safe(flow_id, ID_RE, "flow_id")
    if new_state not in {*TRANSITIONS, *TERMINAL}:
        raise TokenFlowError(f"unknown flow state {new_state}")
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            flow = _require_flow(connection, flow_id)
            current = str(flow["state"])
            if current in TERMINAL:
                raise TokenFlowConflictError("terminal flow state is immutable")
            if new_state not in TRANSITIONS.get(current, frozenset()):
                raise TokenFlowConflictError(f"transition {current} -> {new_state} is not allowed")
            is_terminal = new_state in TERMINAL
            if is_terminal:
                terminal_reason = _safe(terminal_reason, REASON_RE, "terminal_reason")
                if new_state != "cancelled_terminal" and terminal_attempt_id is None:
                    raise TokenFlowError(f"{new_state} flow requires terminal_attempt_id")
                if terminal_attempt_id is not None:
                    terminal_attempt_id = _safe(terminal_attempt_id, ID_RE, "terminal_attempt_id")
                    attempt = _require_latest_attempt(connection, flow_id, terminal_attempt_id)
                    _validate_terminal_status(new_state, attempt["status"])
                    if attempt["output_digest"] is not None:
                        _output_digest(attempt["output_digest"])
                    if new_state in {"complete", "partial_terminal"}:
                        _output_digest(attempt["output_digest"])
            else:
                if (
                    terminal_reason is not None
                    or terminal_attempt_id is not None
                    or terminal_response_text is not None
                ):
                    raise TokenFlowError("nonterminal transition cannot set terminal metadata")
                if new_state == "confirmation_required":
                    _require_confirmation_attempt(connection, flow_id)
            now = utc_now()
            connection.execute(
                """
                UPDATE ai_flows
                SET state = ?, terminal_reason = ?, terminal_attempt_id = ?, updated_at = ?,
                    completed_at = ?, cancelled_at = ?
                WHERE id = ?
                """,
                (
                    new_state,
                    terminal_reason,
                    terminal_attempt_id,
                    now,
                    now if is_terminal and new_state != "cancelled_terminal" else None,
                    now if new_state == "cancelled_terminal" else None,
                    flow_id,
                ),
            )
            recomputed = _recompute(connection, flow_id)
            if terminal_response_text is not None:
                if new_state != "complete" or terminal_attempt_id is None:
                    raise TokenFlowError(
                        "terminal response capture requires a complete flow"
                    )
                from app.modules.ai.flow_record_capture import (
                    capture_final_flow_records_in_transaction,
                )

                capture_final_flow_records_in_transaction(
                    connection,
                    flow_id=flow_id,
                    task_kind=str(flow["task_kind"]),
                    response_text=terminal_response_text,
                    terminal_attempt_id=terminal_attempt_id,
                    workspace_id=flow["workspace_id"],
                )
            connection.commit()
            return recomputed
        except Exception:
            connection.rollback()
            raise


def recompute_flow_aggregates(flow_id: str) -> Flow:
    flow_id = _safe(flow_id, ID_RE, "flow_id")
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = _recompute(connection, flow_id)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise


def _attempt_rows(connection: sqlite3.Connection, flow_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT id, task_kind, flow_attempt_index, continuation_index, execution_class,
               adapter_invoked, external_dispatch_state, input_tokens, output_tokens,
               cache_read_tokens, reasoning_tokens, normalized_usage_source, latency_ms,
               accounting_basis, accounted_provider_spend_usd_decimal, output_digest,
               status, selected_route_class, provider_id, model_id, fallback_index,
               route_reason_json
        FROM ai_jobs WHERE flow_id = ? ORDER BY flow_attempt_index
        """,
        (flow_id,),
    ).fetchall()


def _identity(attempts: list[sqlite3.Row]) -> tuple[list[str], int]:
    if [row["flow_attempt_index"] for row in attempts] != list(range(len(attempts))):
        raise TokenFlowConflictError("flow attempt indexes must be contiguous")
    continuation_count = 0
    for row in attempts:
        if row["continuation_index"] is not None:
            continuation_count = max(
                continuation_count,
                _nonnegative_int(row["continuation_index"], "continuation_index"),
            )
    return [str(row["id"]) for row in attempts], continuation_count


def _refresh_identity(connection: sqlite3.Connection, flow_id: str) -> None:
    attempts = _attempt_rows(connection, flow_id)
    ordered_ids, continuation_count = _identity(attempts)
    connection.execute(
        "UPDATE ai_flows SET continuation_count = ?, attempt_count = ?, "
        "ordered_attempt_ids_json = ?, updated_at = ? WHERE id = ?",
        (continuation_count, len(attempts), _json(ordered_ids), utc_now(), flow_id),
    )


def _recompute(connection: sqlite3.Connection, flow_id: str) -> Flow:
    flow = _require_flow(connection, flow_id)
    attempts = _attempt_rows(connection, flow_id)
    ordered_ids, continuation_count = _identity(attempts)
    execution: dict[str, int] = {}
    dispatch: dict[str, int] = {}
    accounting: dict[str, int] = {}
    usage_sources: dict[str, int] = {}
    totals = {key: 0 for key in (
        "input_tokens", "output_tokens", "cache_read_tokens",
        "reasoning_tokens", "total_tokens", "latency_ms",
    )}
    spend = Decimal("0")
    for row in attempts:
        execution_class = _required_enum(row["execution_class"], EXECUTION_CLASSES, "execution_class")
        dispatch_state = _required_enum(
            row["external_dispatch_state"], DISPATCH_STATES, "external_dispatch_state"
        )
        accounting_basis = _required_enum(row["accounting_basis"], ACCOUNTING_BASES, "accounting_basis")
        usage_source = _required_enum(
            row["normalized_usage_source"], USAGE_SOURCES, "normalized_usage_source"
        )
        if row["adapter_invoked"] not in (0, 1):
            raise TokenFlowError("adapter_invoked must be 0 or 1")
        if usage_source != "none" and (
            row["input_tokens"] is None or row["output_tokens"] is None
        ):
            raise TokenFlowError("consumed usage requires normalized token totals")
        for bucket, key in (
            (execution, execution_class), (dispatch, dispatch_state),
            (accounting, accounting_basis), (usage_sources, usage_source),
        ):
            bucket[key] = bucket.get(key, 0) + 1
        values = {
            key: _optional_count(row[key], key)
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens", "latency_ms")
        }
        if values["cache_read_tokens"] > values["input_tokens"]:
            raise TokenFlowError("cache_read_tokens cannot exceed input_tokens")
        if values["reasoning_tokens"] > values["output_tokens"]:
            raise TokenFlowError("reasoning_tokens cannot exceed output_tokens")
        if usage_source == "none" and any(
            values[key]
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "reasoning_tokens")
        ):
            raise TokenFlowError("usage source none cannot carry token consumption")
        for key, value in values.items():
            totals[key] += value
        totals["total_tokens"] += values["input_tokens"] + values["output_tokens"]
        row_spend = _spend(row["accounted_provider_spend_usd_decimal"])
        output_digest = row["output_digest"]
        if output_digest is not None:
            output_digest = _output_digest(output_digest)
        _validate_evidence(
            execution_class, int(row["adapter_invoked"]), dispatch_state,
            usage_source, accounting_basis, row_spend, output_digest,
        )
        if row_spend and execution_class != "external_provider":
            raise TokenFlowError("non-external execution cannot have provider spend")
        if execution_class == "external_provider":
            spend += row_spend
    usage_totals: dict[str, object] = {**totals, "usage_source_counts": usage_sources}
    spend_text = _decimal_text(spend)
    now = utc_now()
    connection.execute(
        """
        UPDATE ai_flows SET continuation_count = ?, attempt_count = ?,
            ordered_attempt_ids_json = ?, execution_class_counts_json = ?,
            external_dispatch_counts_json = ?, usage_totals_json = ?,
            accounting_basis_counts_json = ?, external_provider_spend_usd_decimal = ?,
            local_compute_cost_unpriced = ?, synthetic_evidence_present = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            continuation_count, len(attempts), _json(ordered_ids), _json(execution),
            _json(dispatch), _json(usage_totals), _json(accounting), spend_text,
            int("local_compute" in execution), int("synthetic" in execution), now, flow_id,
        ),
    )
    if flow["state"] in TERMINAL:
        flow = _require_flow(connection, flow_id)
        terminal_attempt = None
        if flow["terminal_attempt_id"] is not None:
            terminal_attempt = _require_latest_attempt(
                connection, flow_id, str(flow["terminal_attempt_id"])
            )
            _validate_terminal_status(str(flow["state"]), terminal_attempt["status"])
        output_digest = _terminal_output_digest(
            connection,
            flow_id=flow_id,
            flow=flow,
            terminal_attempt=terminal_attempt,
        )
        accounting_payload = {
            "accounting_basis_counts": accounting,
            "execution_class_counts": execution,
            "external_dispatch_counts": dispatch,
            "external_provider_spend_usd_decimal": spend_text,
            "flow_id": flow_id,
            "local_compute_cost_unpriced": "local_compute" in execution,
            "ordered_attempt_ids": ordered_ids,
            "schema": "token-flow-v0",
            "synthetic_evidence_present": "synthetic" in execution,
            "usage_totals": usage_totals,
        }
        accounting_digest = canonical_digest(accounting_payload)
        if flow["final_accounting_digest"] is not None and (
            flow["final_accounting_digest"] != accounting_digest
            or flow["final_output_digest"] != output_digest
        ):
            raise TokenFlowConflictError("terminal flow digest evidence changed")
        connection.execute(
            "UPDATE ai_flows SET final_accounting_digest = ?, final_output_digest = ? WHERE id = ?",
            (accounting_digest, output_digest, flow_id),
        )
    return _decode_flow(_require_flow(connection, flow_id))


def _terminal_output_digest(
    connection: sqlite3.Connection,
    *,
    flow_id: str,
    flow: sqlite3.Row,
    terminal_attempt: sqlite3.Row | None,
) -> str | None:
    segments = connection.execute(
        """
        SELECT segment_index, originating_attempt_id, body_text, body_digest
        FROM ai_flow_segments
        WHERE flow_id = ?
        ORDER BY segment_index
        """,
        (flow_id,),
    ).fetchall()
    if segments:
        indexes = [int(row["segment_index"]) for row in segments]
        if indexes != list(range(len(segments))):
            raise TokenFlowConflictError(
                "terminal protected segment indexes changed"
            )
        bodies: list[str] = []
        originating_attempt_ids: list[str] = []
        for segment in segments:
            body = segment["body_text"]
            if not isinstance(body, str) or not body:
                raise TokenFlowConflictError(
                    "terminal protected segment body is malformed"
                )
            body_digest = canonical_digest({"text": body})
            if segment["body_digest"] != body_digest:
                raise TokenFlowConflictError(
                    "terminal protected segment digest evidence changed"
                )
            origin_id = str(segment["originating_attempt_id"])
            origin = connection.execute(
                "SELECT output_digest FROM ai_jobs WHERE id = ? AND flow_id = ?",
                (origin_id, flow_id),
            ).fetchone()
            if origin is None or origin["output_digest"] != body_digest:
                raise TokenFlowConflictError(
                    "terminal protected segment origin evidence changed"
                )
            bodies.append(body)
            originating_attempt_ids.append(origin_id)
        if len(set(originating_attempt_ids)) != len(originating_attempt_ids):
            raise TokenFlowConflictError(
                "terminal protected segment origins are duplicated"
            )
        if (
            terminal_attempt is not None
            and terminal_attempt["output_digest"] is not None
            and originating_attempt_ids[-1] != terminal_attempt["id"]
        ):
            raise TokenFlowConflictError(
                "terminal output attempt no longer owns the final segment"
            )
        output_digest: str | None = canonical_digest(
            {"text": "".join(bodies)}
        )
    else:
        output_digest = (
            terminal_attempt["output_digest"]
            if terminal_attempt is not None
            else None
        )
        if output_digest is not None:
            output_digest = _output_digest(output_digest)

    if flow["state"] in {"complete", "partial_terminal"}:
        output_digest = _output_digest(output_digest)
    persisted = flow["final_output_digest"]
    if persisted is not None and persisted != output_digest:
        raise TokenFlowConflictError(
            "terminal flow digest evidence changed"
        )
    return output_digest


def _validate_evidence(
    execution: str,
    invoked: int,
    dispatch: str,
    usage: str,
    accounting: str,
    spend: Decimal,
    output_digest: str | None,
) -> None:
    if execution != "external_provider" and dispatch != "not_applicable":
        raise TokenFlowError("non-external execution requires not_applicable dispatch")
    if execution == "external_provider" and dispatch == "not_applicable":
        raise TokenFlowError("external execution requires dispatch evidence")
    if dispatch in {"started", "unknown"} and not invoked:
        raise TokenFlowError("started or unknown dispatch requires adapter invocation")
    if not invoked and usage != "none":
        raise TokenFlowError("non-invoked attempt requires usage source none")
    expected = {
        "none": "no_execution", "synthetic": "synthetic_not_economic",
        "local_compute": "local_compute_unpriced", "legacy_unknown": "legacy_unknown",
    }
    if execution in expected and accounting != expected[execution]:
        raise TokenFlowError(f"{execution} execution has invalid accounting basis")
    if execution == "synthetic" and usage != "estimated":
        raise TokenFlowError("synthetic execution requires estimated usage")
    if execution == "local_compute" and usage not in {"actual", "estimated"}:
        raise TokenFlowError("local_compute usage must be actual or estimated")
    if execution in {"synthetic", "local_compute"} and not invoked:
        raise TokenFlowError(f"{execution} execution requires adapter invocation")
    if execution == "none" and invoked:
        raise TokenFlowError("none execution cannot invoke an adapter")
    if execution == "none" and output_digest is not None:
        raise TokenFlowError("none execution cannot carry output_digest")
    if execution != "external_provider":
        return
    if (not invoked or dispatch == "not_started") and output_digest is not None:
        raise TokenFlowError("non-invoked external execution cannot carry output_digest")
    if dispatch == "not_started":
        if usage != "none" or accounting != "external_not_sent" or spend:
            raise TokenFlowError("external not_started evidence is inconsistent")
    elif dispatch == "unknown":
        if usage != "estimated" or accounting != "conservative_estimated_usage":
            raise TokenFlowError("external unknown dispatch requires estimated conservative evidence")
        if spend <= 0:
            raise TokenFlowError("external unknown dispatch requires positive provider spend")
    elif dispatch == "started":
        if usage not in {"actual", "mixed", "estimated"}:
            raise TokenFlowError("external started usage cannot be none")
        if accounting not in {
            "provider_exact", "conservative_standard_input", "conservative_estimated_usage"
        }:
            raise TokenFlowError("external started accounting basis is invalid")
        if accounting in {"provider_exact", "conservative_standard_input"} and usage != "actual":
            raise TokenFlowError("exact or standard-input accounting requires actual usage")
        if accounting in {
            "conservative_standard_input", "conservative_estimated_usage"
        } and spend <= 0:
            raise TokenFlowError("conservative started accounting requires positive spend")


def _require_confirmation_attempt(connection: sqlite3.Connection, flow_id: str) -> None:
    _require_confirmation_ticket_binding(
        connection,
        flow_id,
        ticket_id=None,
        allowed_ticket_states=frozenset({"pending"}),
        require_unexpired=True,
    )


def _require_confirmation_ticket_binding(
    connection: sqlite3.Connection,
    flow_id: str,
    *,
    ticket_id: str | None,
    allowed_ticket_states: frozenset[str],
    require_unexpired: bool,
) -> None:
    attempts = _attempt_rows(connection, flow_id)
    if not attempts:
        raise TokenFlowConflictError("confirmation_required needs a canonical pause attempt")
    attempt = attempts[-1]
    if (
        attempt["execution_class"] != "external_provider"
        or attempt["adapter_invoked"] != 0
        or attempt["external_dispatch_state"] != "not_started"
        or attempt["normalized_usage_source"] != "none"
        or attempt["accounting_basis"] != "external_not_sent"
        or _spend(attempt["accounted_provider_spend_usd_decimal"]) != 0
        or attempt["output_digest"] is not None
    ):
        raise TokenFlowConflictError("latest attempt is not a canonical confirmation pause")
    metadata = _route_reason(attempt["route_reason_json"])
    persisted_ticket_id = _metadata_text(metadata, "egress_ticket_id")
    if ticket_id is not None and persisted_ticket_id != ticket_id:
        raise TokenFlowConflictError("confirmation ticket does not match paused flow")
    decision_id = _metadata_text(metadata, "egress_decision_id")
    packet_digest = _metadata_text(metadata, "egress_packet_digest")
    trigger_ids = _metadata_text_list(metadata, "egress_trigger_ids")
    if metadata.get("egress_reason_code") != "confirmation_required":
        raise TokenFlowConflictError("confirmation attempt reason is not canonical")
    ticket = connection.execute(
        """
        SELECT
            ticket.state AS ticket_state, ticket.expires_at,
            ticket.packet_digest AS ticket_packet_digest,
            ticket.provider_id AS ticket_provider_id,
            ticket.model_id AS ticket_model_id,
            ticket.trigger_ids_json AS ticket_trigger_ids_json,
            ticket.policy_version AS ticket_policy_version,
            ticket.config_digest AS ticket_config_digest,
            decision.id AS decision_id, decision.result AS decision_result,
            decision.reason_code AS decision_reason_code,
            decision.packet_id AS decision_packet_id,
            decision.packet_digest AS decision_packet_digest,
            decision.ticket_id AS decision_ticket_id,
            decision.trigger_ids_json AS decision_trigger_ids_json,
            decision.confirmation_required, decision.reservation_id,
            decision.policy_version AS decision_policy_version,
            decision.trigger_version AS decision_trigger_version,
            decision.config_digest AS decision_config_digest,
            packet.id AS packet_id, packet.packet_digest,
            packet.task_kind AS packet_task_kind,
            packet.workspace_id AS packet_workspace_id,
            packet.route_class, packet.provider_id, packet.model_id,
            packet.fallback_index, packet.policy_version,
            packet.trigger_version, packet.config_digest
        FROM egress_confirmation_tickets AS ticket
        JOIN egress_decisions AS decision ON decision.id = ticket.decision_id
        JOIN egress_packets AS packet ON packet.id = ticket.packet_id
        WHERE ticket.id = ?
        """,
        (persisted_ticket_id,),
    ).fetchone()
    if ticket is None:
        raise TokenFlowConflictError("confirmation ticket does not exist")
    if ticket["ticket_state"] not in allowed_ticket_states:
        raise TokenFlowConflictError(
            "confirmation ticket state is not valid for this flow operation"
        )
    if require_unexpired and not _unexpired(ticket["expires_at"]):
        raise TokenFlowConflictError("confirmation ticket is not pending and unexpired")
    flow = _require_flow(connection, flow_id)
    if (
        ticket["packet_task_kind"] != attempt["task_kind"]
        or ticket["packet_task_kind"] != flow["task_kind"]
        or ticket["packet_workspace_id"] != flow["workspace_id"]
    ):
        raise TokenFlowConflictError("confirmation packet task or workspace does not match flow")
    expected_binding = (
        attempt["provider_id"],
        attempt["model_id"],
        attempt["selected_route_class"],
        attempt["fallback_index"],
    )
    persisted_binding = (
        ticket["provider_id"],
        ticket["model_id"],
        ticket["route_class"],
        ticket["fallback_index"],
    )
    metadata_binding = (
        metadata.get("fallback_provider_id"),
        metadata.get("fallback_model_id"),
        metadata.get("fallback_chain_route"),
        metadata.get("fallback_attempt_index"),
    )
    if expected_binding != persisted_binding or metadata_binding != persisted_binding:
        raise TokenFlowConflictError("confirmation ticket binding does not match attempt")
    ticket_triggers = _stored_text_list(ticket["ticket_trigger_ids_json"])
    decision_triggers = _stored_text_list(ticket["decision_trigger_ids_json"])
    if trigger_ids != ticket_triggers or trigger_ids != decision_triggers:
        raise TokenFlowConflictError("confirmation ticket trigger binding changed")
    if (
        decision_id != ticket["decision_id"]
        or packet_digest != ticket["packet_digest"]
        or persisted_ticket_id != ticket["decision_ticket_id"]
        or ticket["decision_result"] != "pause"
        or ticket["decision_reason_code"] != "confirmation_required"
        or ticket["confirmation_required"] != 1
        or ticket["reservation_id"] is not None
        or ticket["decision_packet_id"] != ticket["packet_id"]
        or ticket["decision_packet_digest"] != ticket["packet_digest"]
        or ticket["ticket_packet_digest"] != ticket["packet_digest"]
        or ticket["ticket_provider_id"] != ticket["provider_id"]
        or ticket["ticket_model_id"] != ticket["model_id"]
        or ticket["ticket_policy_version"] != ticket["policy_version"]
        or ticket["decision_policy_version"] != ticket["policy_version"]
        or ticket["ticket_config_digest"] != ticket["config_digest"]
        or ticket["decision_config_digest"] != ticket["config_digest"]
        or ticket["decision_trigger_version"] != ticket["trigger_version"]
    ):
        raise TokenFlowConflictError("confirmation ticket authority binding changed")


def _require_flow(connection: sqlite3.Connection, flow_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM ai_flows WHERE id = ?", (flow_id,)).fetchone()
    if row is None:
        raise TokenFlowNotFoundError(f"flow {flow_id} does not exist")
    return row


def _require_attempt(
    connection: sqlite3.Connection, flow_id: str, attempt_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT id, status, output_digest FROM ai_jobs WHERE id = ? AND flow_id = ?",
        (attempt_id, flow_id),
    ).fetchone()
    if row is None:
        raise TokenFlowConflictError("terminal_attempt_id must belong to the flow")
    return row


def _validate_terminal_status(state: str, value: object) -> None:
    if not isinstance(value, str) or not TASK_RE.fullmatch(value):
        raise TokenFlowError("terminal ai_job status is malformed")
    if value == "queued":
        raise TokenFlowConflictError("queued ai_job cannot terminalize a flow")
    if state == "complete" and value != "success":
        raise TokenFlowConflictError("complete flow requires a successful ai_job")
    if state == "failed_terminal" and value == "success":
        raise TokenFlowConflictError("failed_terminal flow requires a non-success ai_job")


def _route_reason(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        raise TokenFlowConflictError("confirmation attempt route metadata is malformed")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise TokenFlowConflictError(
            "confirmation attempt route metadata is malformed"
        ) from exc
    if not isinstance(parsed, dict):
        raise TokenFlowConflictError("confirmation attempt route metadata is malformed")
    return parsed


def _metadata_text(metadata: dict[str, object], field: str) -> str:
    value = metadata.get(field)
    if not isinstance(value, str) or not value:
        raise TokenFlowConflictError(f"confirmation attempt {field} is malformed")
    return value


def _metadata_text_list(metadata: dict[str, object], field: str) -> list[str]:
    return _text_list(metadata.get(field), f"confirmation attempt {field}")


def _stored_text_list(value: object) -> list[str]:
    if not isinstance(value, str):
        raise TokenFlowConflictError("confirmation ticket trigger evidence is malformed")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise TokenFlowConflictError(
            "confirmation ticket trigger evidence is malformed"
        ) from exc
    return _text_list(parsed, "confirmation ticket trigger evidence")


def _text_list(value: object, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise TokenFlowConflictError(f"{field} is malformed")
    return value


def _unexpired(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        expires_at = datetime.fromisoformat(value)
    except ValueError:
        return False
    return expires_at.tzinfo is not None and expires_at > datetime.now(UTC)


def _require_latest_attempt(
    connection: sqlite3.Connection, flow_id: str, attempt_id: str
) -> sqlite3.Row:
    row = _require_attempt(connection, flow_id, attempt_id)
    latest = connection.execute(
        "SELECT id FROM ai_jobs WHERE flow_id = ? ORDER BY flow_attempt_index DESC LIMIT 1",
        (flow_id,),
    ).fetchone()
    if latest is None or latest["id"] != attempt_id:
        raise TokenFlowConflictError("terminal_attempt_id must be the final ordered attempt")
    return row


def _decode_flow(row: sqlite3.Row) -> Flow:
    result: Flow = dict(row)
    for stored, public in (
        ("ordered_attempt_ids_json", "ordered_attempt_ids"),
        ("execution_class_counts_json", "execution_class_counts"),
        ("external_dispatch_counts_json", "external_dispatch_counts"),
        ("usage_totals_json", "usage_totals"),
        ("accounting_basis_counts_json", "accounting_basis_counts"),
    ):
        result[public] = json.loads(str(result.pop(stored)))
    result["local_compute_cost_unpriced"] = bool(result["local_compute_cost_unpriced"])
    result["synthetic_evidence_present"] = bool(result["synthetic_evidence_present"])
    return result


def _safe(value: object, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise TokenFlowError(f"{field} is malformed")
    return value


def _required_enum(value: object, allowed: frozenset[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise TokenFlowError(f"{field} is missing or unsupported")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TokenFlowError(f"{field} must be a non-negative integer")
    return value


def _optional_count(value: object, field: str) -> int:
    return 0 if value is None else _nonnegative_int(value, field)


def _spend(value: object) -> Decimal:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or not SPEND_RE.fullmatch(value)
    ):
        raise TokenFlowError("accounted provider spend is missing or not decimal text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise TokenFlowError("accounted provider spend is invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise TokenFlowError("accounted provider spend must be finite and non-negative")
    return parsed


def _decimal_text(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def _output_digest(value: object) -> str:
    if not isinstance(value, str) or not OUTPUT_DIGEST_RE.fullmatch(value):
        raise TokenFlowError("terminal output_digest must be canonical sha256")
    return value


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
