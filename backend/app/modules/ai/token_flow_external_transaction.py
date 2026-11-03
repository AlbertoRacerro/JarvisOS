from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from app.modules.ai import egress_persistence as persistence
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.contracts import AIExternalDispatchState, AIResponse
from app.modules.ai.egress_lifecycle import (
    EgressReconciliation,
    _actual_usage,
    _reservation_row,
    _validate_ai_job_binding,
    _verified_ai_job_usage,
)
from app.modules.ai.egress_service import EgressContractError
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.provider_registry import (
    ProviderRegistry,
    load_default_provider_registry,
    resolve_model_pricing,
)
from app.modules.ai.token_flow_continuation import (
    ContinuationDecision,
    apply_continuation_lineage,
)
from app.modules.ai.token_flow_continuation_transaction import (
    record_continuation_attempt_evidence_in_transaction,
)
from app.modules.ai.token_flow_external import (
    external_not_started_evidence,
    external_reconciled_evidence,
)
from app.modules.ai.token_flow_transaction import record_attempt_evidence_in_transaction

_TERMINAL_JOB_STATUSES = frozenset(
    {
        "config_error",
        "provider_error",
        "route_unavailable",
        "success",
        "validation_error",
    }
)


@dataclass(frozen=True, slots=True)
class ExternalAttemptFinalization:
    flow_id: str
    ai_job_id: str
    status: str
    dispatch_state: str
    reconciliation_status: str
    input_tokens: int | None
    output_tokens: int | None
    accounted_provider_spend_usd_decimal: str


def finalize_external_attempt(
    *,
    flow_id: str,
    ai_job_id: str,
    binding: ProviderBinding,
    fallback_index: int,
    status: str,
    response: AIResponse | None,
    latency_ms: int,
    error_type: str | None,
    adapter_invoked: bool,
    dispatch_state: AIExternalDispatchState,
    requested_output_ceiling: int | None,
    effective_output_ceiling: int | None,
    outcome_reason: str,
    reservation_id: str | None = None,
    registry: ProviderRegistry | None = None,
    use_confirmation_pricing_snapshot: bool = False,
    continuation_decision: ContinuationDecision | None = None,
    now: datetime | None = None,
) -> ExternalAttemptFinalization:
    """Finalize one external attempt, 059b accounting, and 061 evidence atomically."""

    if not isinstance(adapter_invoked, bool):
        raise EgressContractError("adapter_invoked must be boolean")
    if not isinstance(dispatch_state, AIExternalDispatchState):
        raise EgressContractError("dispatch_state must be AIExternalDispatchState")
    if dispatch_state is not AIExternalDispatchState.not_started and not adapter_invoked:
        raise EgressContractError("started or unknown dispatch requires adapter invocation")
    if response is not None:
        if not adapter_invoked:
            raise EgressContractError("non-invoked attempt cannot carry a response")
        if response.external_dispatch_state is not dispatch_state:
            raise EgressContractError("response dispatch evidence does not match finalization")
    if reservation_id is None and dispatch_state is not AIExternalDispatchState.not_started:
        raise EgressContractError("dispatched attempt requires a 059b reservation")
    if reservation_id is None and adapter_invoked:
        raise EgressContractError("adapter invocation requires a 059b reservation")

    if not isinstance(use_confirmation_pricing_snapshot, bool):
        raise EgressContractError(
            "use_confirmation_pricing_snapshot must be boolean"
        )
    if use_confirmation_pricing_snapshot and (
        reservation_id is not None
        or adapter_invoked
        or dispatch_state is not AIExternalDispatchState.not_started
        or response is not None
    ):
        raise EgressContractError(
            "confirmation pricing snapshot is only valid for a non-dispatched "
            "attempt without a reservation"
        )

    registry = registry or load_default_provider_registry()
    with persistence._immediate_transaction() as connection:
        _bind_attempt_identity(
            connection,
            ai_job_id=ai_job_id,
            binding=binding,
            fallback_index=fallback_index,
        )
        pricing_version = (
            _confirmation_pricing_version(
                connection,
                ai_job_id=ai_job_id,
                binding=binding,
                fallback_index=fallback_index,
            )
            if use_confirmation_pricing_snapshot
            else resolve_model_pricing(
                registry, binding.provider_id, binding.model_id
            ).pricing_version
        )
        _finalize_ai_job_in_transaction(
            connection,
            ai_job_id=ai_job_id,
            status=status,
            response=response,
            latency_ms=latency_ms,
            error_type=error_type,
        )

        reconciliation: EgressReconciliation | None = None
        if reservation_id is not None:
            network_attempt = dispatch_state in {
                AIExternalDispatchState.started,
                AIExternalDispatchState.unknown,
            }
            reconciliation = _reconcile_reserved_attempt_in_transaction(
                connection,
                reservation_id=reservation_id,
                ai_job_id=ai_job_id,
                network_attempt=network_attempt,
                actual_input_tokens=(
                    response.usage.input_tokens if response is not None and network_attempt else None
                ),
                actual_output_tokens=(
                    response.usage.output_tokens if response is not None and network_attempt else None
                ),
                usage_source=(
                    response.usage.usage_source.value
                    if response is not None and network_attempt
                    else "estimated"
                ),
                registry=registry,
                now=now,
            )

        if reconciliation is None:
            evidence = external_not_started_evidence(
                binding=binding,
                pricing_version=pricing_version,
                outcome_reason=outcome_reason,
                requested_output_ceiling=requested_output_ceiling,
                effective_output_ceiling=effective_output_ceiling,
                fallback_index=fallback_index,
                adapter_invoked=False,
                response=None,
            )
            reconciliation_status = "not_sent"
            input_tokens = None
            output_tokens = None
        elif dispatch_state is AIExternalDispatchState.not_started:
            evidence = external_not_started_evidence(
                binding=binding,
                pricing_version=pricing_version,
                outcome_reason=outcome_reason,
                requested_output_ceiling=requested_output_ceiling,
                effective_output_ceiling=effective_output_ceiling,
                fallback_index=fallback_index,
                adapter_invoked=adapter_invoked,
                response=response,
            )
            reconciliation_status = reconciliation.reconciliation_status
            input_tokens = reconciliation.actual_input_tokens
            output_tokens = reconciliation.actual_output_tokens
            _apply_reconciled_usage(connection, ai_job_id=ai_job_id, reconciliation=reconciliation, usage_source="estimated")
        else:
            evidence = external_reconciled_evidence(
                binding=binding,
                pricing_version=pricing_version,
                dispatch_state=dispatch_state,
                reconciliation_status=reconciliation.reconciliation_status,
                reconciled_cost_usd=reconciliation.actual_cost_usd,
                response=response,
                outcome_reason=outcome_reason,
                requested_output_ceiling=requested_output_ceiling,
                effective_output_ceiling=effective_output_ceiling,
                fallback_index=fallback_index,
            )
            reconciliation_status = reconciliation.reconciliation_status
            input_tokens = reconciliation.actual_input_tokens
            output_tokens = reconciliation.actual_output_tokens
            legacy_usage_source = (
                "actual" if evidence.normalized_usage_source == "actual" else "estimated"
            )
            _apply_reconciled_usage(
                connection,
                ai_job_id=ai_job_id,
                reconciliation=reconciliation,
                usage_source=legacy_usage_source,
            )

        if continuation_decision is None:
            flow = record_attempt_evidence_in_transaction(
                connection,
                flow_id=flow_id,
                attempt_id=ai_job_id,
                evidence=evidence,
            )
        else:
            continuation_evidence = apply_continuation_lineage(
                evidence,
                continuation_decision,
            )
            flow = record_continuation_attempt_evidence_in_transaction(
                connection,
                flow_id=flow_id,
                attempt_id=ai_job_id,
                evidence=continuation_evidence,
                decision=continuation_decision,
                now=now,
            )
            evidence = continuation_evidence
        return ExternalAttemptFinalization(
            flow_id=str(flow["id"]),
            ai_job_id=ai_job_id,
            status=status,
            dispatch_state=dispatch_state.value,
            reconciliation_status=reconciliation_status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            accounted_provider_spend_usd_decimal=evidence.accounted_provider_spend_usd_decimal,
        )


def _confirmation_pricing_version(
    connection: sqlite3.Connection,
    *,
    ai_job_id: str,
    binding: ProviderBinding,
    fallback_index: int,
) -> str:
    job = connection.execute(
        "SELECT task_kind, route_reason_json "
        "FROM ai_jobs WHERE id = ?",
        (ai_job_id,),
    ).fetchone()
    if job is None:
        raise persistence.EgressStateError("ai_job was not found")
    try:
        route_metadata = json.loads(job["route_reason_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise EgressContractError(
            "confirmation pricing snapshot metadata is malformed"
        ) from exc
    if not isinstance(route_metadata, dict):
        raise EgressContractError(
            "confirmation pricing snapshot metadata is malformed"
        )
    ticket_id = route_metadata.get("egress_confirmation_ticket_id")
    if not isinstance(ticket_id, str) or not ticket_id.strip():
        raise EgressContractError(
            "confirmation pricing snapshot ticket is missing"
        )
    ticket_id = ticket_id.strip()

    snapshot = connection.execute(
        """
        SELECT ticket.state AS ticket_state,
               ticket.decision_id AS ticket_decision_id,
               ticket.packet_id AS ticket_packet_id,
               ticket.packet_digest AS ticket_packet_digest,
               ticket.provider_id AS ticket_provider_id,
               ticket.model_id AS ticket_model_id,
               decision.packet_id AS decision_packet_id,
               decision.packet_digest AS decision_packet_digest,
               decision.pricing_version AS pricing_version,
               decision.reservation_id AS decision_reservation_id,
               packet.packet_digest AS packet_digest,
               packet.task_kind AS packet_task_kind,
               packet.route_class AS packet_route_class,
               packet.provider_id AS packet_provider_id,
               packet.model_id AS packet_model_id,
               packet.fallback_index AS packet_fallback_index
        FROM egress_confirmation_tickets AS ticket
        JOIN egress_decisions AS decision ON decision.id = ticket.decision_id
        JOIN egress_packets AS packet ON packet.id = ticket.packet_id
        WHERE ticket.id = ?
        """,
        (ticket_id,),
    ).fetchone()
    if snapshot is None:
        raise EgressContractError(
            "confirmation pricing snapshot ticket was not found"
        )
    if snapshot["ticket_state"] not in {"expired", "revoked"}:
        raise EgressContractError(
            "confirmation pricing snapshot requires an expired or revoked ticket"
        )
    if snapshot["decision_reservation_id"] is not None:
        raise EgressContractError(
            "confirmation pricing snapshot cannot reference a reserved decision"
        )

    expected_metadata = {
        "egress_confirmation_ticket_id": ticket_id,
        "egress_decision_id": snapshot["ticket_decision_id"],
        "egress_packet_digest": snapshot["ticket_packet_digest"],
        "fallback_attempt_index": fallback_index,
        "fallback_chain_route": binding.route_class,
        "fallback_model_id": binding.model_id,
        "fallback_provider_id": binding.provider_id,
    }
    for key, expected in expected_metadata.items():
        if route_metadata.get(key) != expected:
            raise EgressContractError(
                f"confirmation pricing snapshot metadata mismatch: {key}"
            )
    if route_metadata.get("decision_reason") != f"confirmed_ticket:{ticket_id}":
        raise EgressContractError(
            "confirmation pricing snapshot job is not ticket-bound"
        )

    packet_identity = (
        snapshot["packet_route_class"],
        snapshot["packet_provider_id"],
        snapshot["packet_model_id"],
        int(snapshot["packet_fallback_index"]),
    )
    binding_identity = (
        binding.route_class,
        binding.provider_id,
        binding.model_id,
        fallback_index,
    )
    if packet_identity != binding_identity:
        raise EgressContractError(
            "confirmation pricing snapshot binding does not match the packet"
        )
    if (
        snapshot["ticket_provider_id"],
        snapshot["ticket_model_id"],
    ) != (binding.provider_id, binding.model_id):
        raise EgressContractError(
            "confirmation pricing snapshot binding does not match the ticket"
        )
    if (
        snapshot["ticket_packet_id"] != snapshot["decision_packet_id"]
        or snapshot["ticket_packet_digest"] != snapshot["decision_packet_digest"]
        or snapshot["ticket_packet_digest"] != snapshot["packet_digest"]
    ):
        raise EgressContractError(
            "confirmation pricing snapshot packet identity is inconsistent"
        )
    if job["task_kind"] != snapshot["packet_task_kind"]:
        raise EgressContractError(
            "confirmation pricing snapshot task kind does not match"
        )
    pricing_version = snapshot["pricing_version"]
    if not isinstance(pricing_version, str) or not pricing_version.strip():
        raise EgressContractError(
            "confirmation pricing snapshot version is missing"
        )
    return pricing_version.strip()


def _bind_attempt_identity(
    connection: sqlite3.Connection,
    *,
    ai_job_id: str,
    binding: ProviderBinding,
    fallback_index: int,
) -> None:
    row = connection.execute(
        """
        SELECT status, selected_route_class, provider_id, model_id, fallback_index
        FROM ai_jobs WHERE id = ?
        """,
        (ai_job_id,),
    ).fetchone()
    if row is None:
        raise persistence.EgressStateError("ai_job was not found")
    if row["status"] != "queued":
        raise persistence.EgressStateError("ai_job is not queued")
    expected = (binding.route_class, binding.provider_id, binding.model_id)
    persisted = (row["selected_route_class"], row["provider_id"], row["model_id"])
    if persisted != expected:
        raise persistence.EgressStateError("ai_job binding does not match token-flow binding")
    if row["fallback_index"] is None:
        updated = connection.execute(
            "UPDATE ai_jobs SET fallback_index = ? WHERE id = ? AND fallback_index IS NULL",
            (fallback_index, ai_job_id),
        )
        if updated.rowcount != 1:
            raise persistence.EgressStateError("ai_job fallback binding changed concurrently")
    elif int(row["fallback_index"]) != fallback_index:
        raise persistence.EgressStateError("ai_job fallback index does not match")


def _finalize_ai_job_in_transaction(
    connection: sqlite3.Connection,
    *,
    ai_job_id: str,
    status: str,
    response: AIResponse | None,
    latency_ms: int,
    error_type: str | None,
) -> None:
    if status not in _TERMINAL_JOB_STATUSES:
        raise EgressContractError("unsupported terminal ai_job status")
    if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0:
        raise EgressContractError("latency_ms must be a non-negative integer")
    if status == "success" and (response is None or response.text is None):
        raise EgressContractError("successful ai_job requires a text response")

    row = connection.execute(
        "SELECT status, provider_id, model_id FROM ai_jobs WHERE id = ?",
        (ai_job_id,),
    ).fetchone()
    if row is None or row["status"] != "queued":
        raise persistence.EgressStateError("ai_job is not queued or was already finalized")
    expected_binding = (row["provider_id"], row["model_id"])
    if response is not None and (response.provider_id, response.model_id) != expected_binding:
        raise persistence.EgressStateError("ai_job response binding does not match queued attempt")
    if response is not None and (
        response.usage.provider_id,
        response.usage.model_id,
    ) != expected_binding:
        raise persistence.EgressStateError("ai_job usage binding does not match queued attempt")

    output_digest = (
        canonical_digest({"text": response.text})
        if response is not None and response.text is not None
        else None
    )
    input_tokens = response.usage.input_tokens if response is not None else None
    output_tokens = response.usage.output_tokens if response is not None else None
    cost_estimate = response.usage.provider_cost_estimate if response is not None else None
    usage_source = response.usage.usage_source.value if response is not None else None
    updated = connection.execute(
        """
        UPDATE ai_jobs
        SET status = ?, output_digest = ?, input_tokens = ?, output_tokens = ?,
            cost_estimate = ?, usage_source = ?, latency_ms = ?, error_type = ?
        WHERE id = ? AND status = 'queued'
        """,
        (
            status,
            output_digest,
            input_tokens,
            output_tokens,
            cost_estimate,
            usage_source,
            latency_ms,
            error_type,
            ai_job_id,
        ),
    )
    if updated.rowcount != 1:
        raise persistence.EgressStateError("ai_job finalization CAS conflict")


def _reconcile_reserved_attempt_in_transaction(
    connection: sqlite3.Connection,
    *,
    reservation_id: str,
    ai_job_id: str,
    network_attempt: bool,
    actual_input_tokens: int | None,
    actual_output_tokens: int | None,
    usage_source: str,
    registry: ProviderRegistry,
    now: datetime | None,
) -> EgressReconciliation:
    now_iso = persistence._normalized_now(now).isoformat()
    row = _reservation_row(connection, reservation_id)
    if row is None:
        raise persistence.EgressStateError("reservation was not found")
    allowed_states = {"in_flight"} if network_attempt else {"active", "in_flight"}
    if row["reservation_state"] not in allowed_states:
        raise persistence.EgressStateError(
            f"reservation cannot be reconciled from state {row['reservation_state']}"
        )
    if row["ai_job_id"] is not None and row["ai_job_id"] != ai_job_id:
        raise persistence.EgressStateError("reservation is bound to a different ai_job")
    _validate_ai_job_binding(connection, ai_job_id=ai_job_id, row=row)
    verified_usage = _verified_ai_job_usage(connection, ai_job_id=ai_job_id)
    input_tokens, output_tokens, actual_cost, reconciliation_status = _actual_usage(
        row,
        network_attempt=network_attempt,
        actual_input_tokens=actual_input_tokens,
        actual_output_tokens=actual_output_tokens,
        usage_source=usage_source,
        verified_usage=verified_usage,
        registry=registry,
    )
    attempt_id = str(uuid4())
    terminal_state = "reconciled" if network_attempt else "released"
    connection.execute(
        """
        INSERT INTO egress_attempts (
            id, decision_id, packet_id, ai_job_id, reservation_id,
            route_class, provider_id, model_id, fallback_index,
            network_attempt, reconciliation_status,
            projected_input_tokens, projected_output_tokens,
            projected_cost_upper_usd, actual_input_tokens,
            actual_output_tokens, actual_cost_usd, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attempt_id,
            row["decision_id"],
            row["packet_id"],
            ai_job_id,
            reservation_id,
            row["route_class"],
            row["provider_id"],
            row["model_id"],
            row["fallback_index"],
            int(network_attempt),
            reconciliation_status,
            row["projected_input_tokens"],
            row["projected_output_tokens"],
            row["projected_cost_upper_usd"],
            input_tokens,
            output_tokens,
            actual_cost,
            now_iso,
        ),
    )
    updated = connection.execute(
        """
        UPDATE egress_budget_reservations
        SET state = ?, version = version + 1, reconciled_at = ?,
            egress_attempt_id = ?, ai_job_id = ?, actual_input_tokens = ?,
            actual_output_tokens = ?, actual_cost_usd = ?, reconciliation_status = ?
        WHERE id = ? AND state = ? AND version = ?
        """,
        (
            terminal_state,
            now_iso,
            attempt_id,
            ai_job_id,
            input_tokens,
            output_tokens,
            actual_cost,
            reconciliation_status,
            reservation_id,
            row["reservation_state"],
            int(row["reservation_version"]),
        ),
    )
    if updated.rowcount != 1:
        raise persistence.EgressStateError("reservation reconcile CAS conflict")
    return EgressReconciliation(
        egress_attempt_id=attempt_id,
        reservation_id=reservation_id,
        decision_id=str(row["decision_id"]),
        packet_id=str(row["packet_id"]),
        ai_job_id=ai_job_id,
        network_attempt=network_attempt,
        reservation_state=terminal_state,
        reconciliation_status=reconciliation_status,
        actual_input_tokens=int(input_tokens),
        actual_output_tokens=int(output_tokens),
        actual_cost_usd=float(actual_cost),
    )


def _apply_reconciled_usage(
    connection: sqlite3.Connection,
    *,
    ai_job_id: str,
    reconciliation: EgressReconciliation,
    usage_source: str,
) -> None:
    if usage_source not in {"actual", "estimated"}:
        raise EgressContractError("legacy usage_source must be actual or estimated")
    updated = connection.execute(
        """
        UPDATE ai_jobs
        SET input_tokens = ?, output_tokens = ?, cost_estimate = ?, usage_source = ?
        WHERE id = ?
        """,
        (
            reconciliation.actual_input_tokens,
            reconciliation.actual_output_tokens,
            reconciliation.actual_cost_usd,
            usage_source,
            ai_job_id,
        ),
    )
    if updated.rowcount != 1:
        raise persistence.EgressStateError("reconciled ai_job usage update failed")
