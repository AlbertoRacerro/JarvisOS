from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.core.database import open_sqlite_connection
from app.modules.ai.token_flow_service import (
    ACCOUNTING_BASES,
    DISPATCH_STATES,
    EXECUTION_CLASSES,
    ID_RE,
    REASON_RE,
    USAGE_SOURCES,
    Flow,
    TokenFlowConflictError,
    TokenFlowError,
    TokenFlowNotFoundError,
    _decode_flow,
    _output_digest,
    _refresh_identity,
    _require_flow,
    _required_enum,
    _safe,
    _spend,
    _validate_evidence,
)

FinishReason = Literal["stop", "length", "content_filter", "tool_call", "error", "unknown"]
FINISH_REASONS = frozenset({"stop", "length", "content_filter", "tool_call", "error", "unknown"})


@dataclass(frozen=True, slots=True)
class AttemptEvidence:
    execution_class: str
    adapter_invoked: bool
    external_dispatch_state: str
    normalized_usage_source: str
    accounting_basis: str
    accounted_provider_spend_usd_decimal: str
    outcome_reason: str
    accounting_version: str
    provider_id: str | None = None
    model_id: str | None = None
    selected_route_class: str | None = None
    fallback_index: int | None = None
    parent_attempt_id: str | None = None
    continuation_index: int | None = None
    requested_output_ceiling: int | None = None
    effective_output_ceiling: int | None = None
    normalized_finish_reason: FinishReason | None = None
    cache_read_tokens: int | None = None
    reasoning_tokens: int | None = None
    capability_version: str | None = None
    pricing_version: str | None = None


_EVIDENCE_COLUMNS = (
    "parent_attempt_id",
    "continuation_index",
    "execution_class",
    "adapter_invoked",
    "external_dispatch_state",
    "requested_output_ceiling",
    "effective_output_ceiling",
    "normalized_finish_reason",
    "normalized_usage_source",
    "cache_read_tokens",
    "reasoning_tokens",
    "accounting_basis",
    "accounted_provider_spend_usd_decimal",
    "outcome_reason",
    "capability_version",
    "pricing_version",
    "accounting_version",
)


def record_attempt_evidence(
    *,
    flow_id: str,
    attempt_id: str,
    evidence: AttemptEvidence,
) -> Flow:
    """Persist canonical 061 evidence and flow linkage in one transaction.

    The function owns only additive 061 columns on an already-created ``ai_jobs`` row.
    Existing execution writers remain authoritative for status, binding, output, usage totals,
    latency, and 059b records. Exact replay is idempotent; partial or conflicting evidence
    fails closed without changing either the attempt or the flow.
    """

    flow_id = _safe(flow_id, ID_RE, "flow_id")
    attempt_id = _safe(attempt_id, ID_RE, "attempt_id")
    normalized = _normalize_evidence(evidence)

    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            flow = _require_flow(connection, flow_id)
            attempt = _require_attempt_for_evidence(connection, attempt_id)
            if attempt["task_kind"] != flow["task_kind"]:
                raise TokenFlowConflictError("ai_job task kind does not match flow")
            _validate_binding(attempt, normalized)
            _validate_usage_and_result(attempt, normalized)
            _validate_parent_and_continuation(connection, flow, attempt_id, normalized)

            if attempt["flow_id"] is not None or attempt["flow_attempt_index"] is not None:
                _require_exact_replay(flow_id, attempt, normalized)
                _refresh_identity(connection, flow_id)
                connection.commit()
                return _decode_flow(_require_flow(connection, flow_id))

            if flow["state"] != "running":
                raise TokenFlowConflictError("only running flows can accept attempt evidence")
            _require_unwritten_evidence(attempt)
            next_index = connection.execute(
                "SELECT COALESCE(MAX(flow_attempt_index), -1) + 1 AS n "
                "FROM ai_jobs WHERE flow_id = ?",
                (flow_id,),
            ).fetchone()["n"]

            values = _update_values(normalized)
            assignments = ", ".join(
                ["flow_id = ?", "flow_attempt_index = ?"]
                + [f"{column} = ?" for column in _EVIDENCE_COLUMNS]
            )
            null_guards = " AND ".join(
                ["flow_id IS NULL", "flow_attempt_index IS NULL"]
                + [f"{column} IS NULL" for column in _EVIDENCE_COLUMNS]
            )
            updated = connection.execute(
                f"UPDATE ai_jobs SET {assignments} WHERE id = ? AND {null_guards}",
                (flow_id, int(next_index), *values, attempt_id),
            )
            if updated.rowcount != 1:
                raise TokenFlowConflictError("ai_job evidence changed concurrently")

            _refresh_identity(connection, flow_id)
            connection.commit()
            return _decode_flow(_require_flow(connection, flow_id))
        except Exception:
            connection.rollback()
            raise


def _normalize_evidence(evidence: AttemptEvidence) -> dict[str, object]:
    if not isinstance(evidence, AttemptEvidence):
        raise TokenFlowError("evidence must be AttemptEvidence")
    if not isinstance(evidence.adapter_invoked, bool):
        raise TokenFlowError("adapter_invoked must be boolean")

    execution_class = _required_enum(
        evidence.execution_class, EXECUTION_CLASSES, "execution_class"
    )
    dispatch = _required_enum(
        evidence.external_dispatch_state, DISPATCH_STATES, "external_dispatch_state"
    )
    usage_source = _required_enum(
        evidence.normalized_usage_source, USAGE_SOURCES, "normalized_usage_source"
    )
    accounting_basis = _required_enum(
        evidence.accounting_basis, ACCOUNTING_BASES, "accounting_basis"
    )
    outcome_reason = _safe(evidence.outcome_reason, REASON_RE, "outcome_reason")
    accounting_version = _safe(
        evidence.accounting_version, ID_RE, "accounting_version"
    )
    capability_version = _optional_safe(
        evidence.capability_version, ID_RE, "capability_version"
    )
    pricing_version = _optional_safe(evidence.pricing_version, ID_RE, "pricing_version")
    parent_attempt_id = _optional_safe(
        evidence.parent_attempt_id, ID_RE, "parent_attempt_id"
    )

    provider_id = _optional_safe(evidence.provider_id, ID_RE, "provider_id")
    model_id = _optional_safe(evidence.model_id, ID_RE, "model_id")
    selected_route_class = evidence.selected_route_class
    if selected_route_class is not None:
        selected_route_class = _safe(selected_route_class, ID_RE, "selected_route_class")
    fallback_index = _optional_nonnegative_int(evidence.fallback_index, "fallback_index")
    continuation_index = _optional_nonnegative_int(
        evidence.continuation_index, "continuation_index"
    )
    requested_ceiling = _optional_positive_int(
        evidence.requested_output_ceiling, "requested_output_ceiling"
    )
    effective_ceiling = _optional_positive_int(
        evidence.effective_output_ceiling, "effective_output_ceiling"
    )
    if (
        requested_ceiling is not None
        and effective_ceiling is not None
        and effective_ceiling > requested_ceiling
    ):
        raise TokenFlowError("effective output ceiling cannot exceed requested ceiling")

    finish_reason = evidence.normalized_finish_reason
    if finish_reason is not None and finish_reason not in FINISH_REASONS:
        raise TokenFlowError("normalized_finish_reason is unsupported")
    cache_read_tokens = _optional_nonnegative_int(
        evidence.cache_read_tokens, "cache_read_tokens"
    )
    reasoning_tokens = _optional_nonnegative_int(
        evidence.reasoning_tokens, "reasoning_tokens"
    )

    spend = _spend(evidence.accounted_provider_spend_usd_decimal)
    spend_text = _decimal_text(spend)
    invoked = int(evidence.adapter_invoked)
    _validate_evidence(
        execution_class,
        invoked,
        dispatch,
        usage_source,
        accounting_basis,
        spend,
        None,
    )

    if execution_class == "none":
        if (provider_id is None) != (model_id is None):
            raise TokenFlowError("none execution binding requires both provider_id and model_id")
        if provider_id is not None and selected_route_class is None:
            raise TokenFlowError("none execution concrete binding requires selected_route_class")
        if capability_version is not None or pricing_version is not None:
            raise TokenFlowError("none execution cannot carry capability or pricing version")
    else:
        if provider_id is None or model_id is None or selected_route_class is None:
            raise TokenFlowError("classified execution requires concrete binding identity")
        if capability_version is None:
            raise TokenFlowError("classified execution requires capability_version")
        if execution_class == "external_provider":
            if pricing_version is None:
                raise TokenFlowError("external execution requires pricing_version")
        elif pricing_version is not None:
            raise TokenFlowError("non-external execution cannot carry pricing_version")

    if not invoked and finish_reason is not None:
        raise TokenFlowError("non-invoked attempt cannot carry finish reason")
    if continuation_index is not None and parent_attempt_id is None:
        raise TokenFlowError("continuation attempt requires parent_attempt_id")

    return {
        "execution_class": execution_class,
        "adapter_invoked": invoked,
        "external_dispatch_state": dispatch,
        "normalized_usage_source": usage_source,
        "accounting_basis": accounting_basis,
        "accounted_provider_spend_usd_decimal": spend_text,
        "outcome_reason": outcome_reason,
        "accounting_version": accounting_version,
        "provider_id": provider_id,
        "model_id": model_id,
        "selected_route_class": selected_route_class,
        "fallback_index": fallback_index,
        "parent_attempt_id": parent_attempt_id,
        "continuation_index": continuation_index,
        "requested_output_ceiling": requested_ceiling,
        "effective_output_ceiling": effective_ceiling,
        "normalized_finish_reason": finish_reason,
        "cache_read_tokens": cache_read_tokens,
        "reasoning_tokens": reasoning_tokens,
        "capability_version": capability_version,
        "pricing_version": pricing_version,
    }


def _require_attempt_for_evidence(
    connection: sqlite3.Connection, attempt_id: str
) -> sqlite3.Row:
    columns = ", ".join(
        (
            "id",
            "task_kind",
            "selected_route_class",
            "provider_id",
            "model_id",
            "fallback_index",
            "flow_id",
            "flow_attempt_index",
            "input_tokens",
            "output_tokens",
            "output_digest",
            *_EVIDENCE_COLUMNS,
        )
    )
    row = connection.execute(
        f"SELECT {columns} FROM ai_jobs WHERE id = ?", (attempt_id,)
    ).fetchone()
    if row is None:
        raise TokenFlowNotFoundError(f"ai_job {attempt_id} does not exist")
    return row


def _validate_binding(attempt: sqlite3.Row, evidence: dict[str, object]) -> None:
    expected = (
        evidence["provider_id"],
        evidence["model_id"],
        evidence["selected_route_class"],
        evidence["fallback_index"],
    )
    persisted = (
        attempt["provider_id"],
        attempt["model_id"],
        attempt["selected_route_class"],
        attempt["fallback_index"],
    )
    if expected != persisted:
        raise TokenFlowConflictError("attempt binding identity does not match evidence")


def _validate_usage_and_result(
    attempt: sqlite3.Row, evidence: dict[str, object]
) -> None:
    input_tokens = _optional_nonnegative_int(attempt["input_tokens"], "input_tokens")
    output_tokens = _optional_nonnegative_int(attempt["output_tokens"], "output_tokens")
    cache_read = evidence["cache_read_tokens"]
    reasoning = evidence["reasoning_tokens"]
    usage_source = str(evidence["normalized_usage_source"])

    if usage_source != "none" and (input_tokens is None or output_tokens is None):
        raise TokenFlowError("consumed usage requires input and output token totals")
    if usage_source == "none" and any(
        value not in (None, 0) for value in (input_tokens, output_tokens, cache_read, reasoning)
    ):
        raise TokenFlowError("usage source none cannot carry token consumption")
    if cache_read is not None and input_tokens is not None and cache_read > input_tokens:
        raise TokenFlowError("cache_read_tokens cannot exceed input_tokens")
    if reasoning is not None and output_tokens is not None and reasoning > output_tokens:
        raise TokenFlowError("reasoning_tokens cannot exceed output_tokens")

    output_digest = attempt["output_digest"]
    if output_digest is not None:
        _output_digest(output_digest)
        if evidence["normalized_finish_reason"] is None:
            raise TokenFlowError("output-bearing attempt requires normalized finish reason")
    _validate_evidence(
        str(evidence["execution_class"]),
        int(evidence["adapter_invoked"]),
        str(evidence["external_dispatch_state"]),
        usage_source,
        str(evidence["accounting_basis"]),
        _spend(evidence["accounted_provider_spend_usd_decimal"]),
        output_digest,
    )


def _validate_parent_and_continuation(
    connection: sqlite3.Connection,
    flow: sqlite3.Row,
    attempt_id: str,
    evidence: dict[str, object],
) -> None:
    parent_id = evidence["parent_attempt_id"]
    continuation_index = evidence["continuation_index"]
    if continuation_index is not None and int(continuation_index) > int(
        flow["max_direct_continuations_snapshot"]
    ):
        raise TokenFlowConflictError("continuation index exceeds flow snapshot")
    if parent_id is None:
        return
    if parent_id == attempt_id:
        raise TokenFlowConflictError("attempt cannot parent itself")
    parent = connection.execute(
        "SELECT flow_id, flow_attempt_index FROM ai_jobs WHERE id = ?", (parent_id,)
    ).fetchone()
    if (
        parent is None
        or parent["flow_id"] != flow["id"]
        or parent["flow_attempt_index"] is None
    ):
        raise TokenFlowConflictError("parent attempt must already belong to the flow")


def _require_unwritten_evidence(attempt: sqlite3.Row) -> None:
    if attempt["flow_id"] is not None or attempt["flow_attempt_index"] is not None:
        raise TokenFlowConflictError("ai_job has partial flow linkage")
    if any(attempt[column] is not None for column in _EVIDENCE_COLUMNS):
        raise TokenFlowConflictError("ai_job has partial or pre-existing token-flow evidence")


def _require_exact_replay(
    flow_id: str, attempt: sqlite3.Row, evidence: dict[str, object]
) -> None:
    if attempt["flow_id"] != flow_id or attempt["flow_attempt_index"] is None:
        raise TokenFlowConflictError("ai_job is already linked to another flow")
    expected = dict(zip(_EVIDENCE_COLUMNS, _update_values(evidence), strict=True))
    mismatched = [column for column, value in expected.items() if attempt[column] != value]
    if mismatched:
        raise TokenFlowConflictError("attempt evidence replay does not match persisted evidence")


def _update_values(evidence: dict[str, object]) -> tuple[object, ...]:
    return tuple(evidence[column] for column in _EVIDENCE_COLUMNS)


def _optional_safe(value: object, pattern, field: str) -> str | None:
    return None if value is None else _safe(value, pattern, field)


def _optional_nonnegative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TokenFlowError(f"{field} must be a non-negative integer")
    return value


def _optional_positive_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TokenFlowError(f"{field} must be a positive integer")
    return value


def _decimal_text(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")
