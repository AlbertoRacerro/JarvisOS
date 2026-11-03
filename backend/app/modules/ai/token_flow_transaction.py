from __future__ import annotations

import sqlite3

from app.modules.ai.token_flow_evidence import (
    AttemptEvidence,
    _normalize_evidence,
    _require_attempt_for_evidence,
    _require_exact_replay,
    _require_unwritten_evidence,
    _validate_binding,
    _validate_parent_and_continuation,
    _validate_usage_and_result,
)
from app.modules.ai.token_flow_evidence import (
    _update_values as _service_update_values,
)
from app.modules.ai.token_flow_service import (
    ID_RE,
    Flow,
    TokenFlowConflictError,
    _decode_flow,
    _refresh_identity,
    _require_flow,
    _safe,
)

_COLUMNS = (
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

_UPDATE = """
UPDATE ai_jobs SET
    flow_id = ?, flow_attempt_index = ?,
    parent_attempt_id = ?, continuation_index = ?, execution_class = ?,
    adapter_invoked = ?, external_dispatch_state = ?, requested_output_ceiling = ?,
    effective_output_ceiling = ?, normalized_finish_reason = ?,
    normalized_usage_source = ?, cache_read_tokens = ?, reasoning_tokens = ?,
    accounting_basis = ?, accounted_provider_spend_usd_decimal = ?,
    outcome_reason = ?, capability_version = ?, pricing_version = ?,
    accounting_version = ?
WHERE id = ?
  AND flow_id IS NULL AND flow_attempt_index IS NULL
  AND parent_attempt_id IS NULL AND continuation_index IS NULL
  AND execution_class IS NULL AND adapter_invoked IS NULL
  AND external_dispatch_state IS NULL AND requested_output_ceiling IS NULL
  AND effective_output_ceiling IS NULL AND normalized_finish_reason IS NULL
  AND normalized_usage_source IS NULL AND cache_read_tokens IS NULL
  AND reasoning_tokens IS NULL AND accounting_basis IS NULL
  AND accounted_provider_spend_usd_decimal IS NULL AND outcome_reason IS NULL
  AND capability_version IS NULL AND pricing_version IS NULL
  AND accounting_version IS NULL
"""


def record_attempt_evidence_in_transaction(
    connection: sqlite3.Connection,
    *,
    flow_id: str,
    attempt_id: str,
    evidence: AttemptEvidence,
) -> Flow:
    """Persist final attempt evidence using the caller's SQLite transaction."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    flow_id = _safe(flow_id, ID_RE, "flow_id")
    attempt_id = _safe(attempt_id, ID_RE, "attempt_id")
    normalized = _normalize_evidence(evidence)

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
        return _decode_flow(_require_flow(connection, flow_id))

    if flow["state"] != "running":
        raise TokenFlowConflictError("only running flows can accept new attempt evidence")
    _require_unwritten_evidence(attempt)
    next_index = connection.execute(
        "SELECT COALESCE(MAX(flow_attempt_index), -1) + 1 AS n "
        "FROM ai_jobs WHERE flow_id = ?",
        (flow_id,),
    ).fetchone()["n"]
    values = _service_update_values(normalized)
    if len(values) != len(_COLUMNS):
        raise TokenFlowConflictError("token-flow evidence column contract changed")
    updated = connection.execute(
        _UPDATE,
        (flow_id, int(next_index), *values, attempt_id),
    )
    if updated.rowcount != 1:
        raise TokenFlowConflictError("ai_job evidence changed concurrently")
    _refresh_identity(connection, flow_id)
    return _decode_flow(_require_flow(connection, flow_id))
