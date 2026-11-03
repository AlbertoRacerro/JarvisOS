from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from app.modules.ai import egress_persistence as persistence
from app.modules.ai.egress_policy import EgressPolicyConfig, load_default_egress_policy
from app.modules.ai.egress_revalidation import validate_ticket_authority_state
from app.modules.ai.egress_service import (
    EgressContractError,
    EgressPacketMaterial,
    EgressPacketProjection,
    build_packet_projection,
)
from app.modules.ai.provider_registry import (
    ProviderRegistry,
    load_default_provider_registry,
    resolve_model_pricing,
)
from app.modules.ai.token_flow_confirmation_resume import (
    activate_consumed_continuation_confirmation_in_transaction,
    terminalize_rejected_continuation_confirmation_in_transaction,
    validate_pending_continuation_confirmation_in_transaction,
)


@dataclass(frozen=True)
class EgressTicketConsumption:
    authorized: bool
    reason_code: str
    ticket_id: str
    decision_id: str
    packet_id: str
    reservation_id: str | None
    packet_digest: str
    provider_id: str
    model_id: str
    route_class: str
    fallback_index: int
    max_output_tokens: int
    continuation_authority_json: str | None = field(default=None, repr=False)
    continuation_pause_attempt_id: str | None = None
    packet_json: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class EgressStartedAttempt:
    reservation_id: str
    decision_id: str
    packet_id: str
    packet_digest: str
    ai_job_id: str
    provider_id: str
    model_id: str
    route_class: str
    fallback_index: int
    max_output_tokens: int
    packet_json: str = field(repr=False)


@dataclass(frozen=True)
class EgressReconciliation:
    egress_attempt_id: str
    reservation_id: str
    decision_id: str
    packet_id: str
    ai_job_id: str
    network_attempt: bool
    reservation_state: str
    reconciliation_status: str
    actual_input_tokens: int
    actual_output_tokens: int
    actual_cost_usd: float


_VerifiedUsage = tuple[int, int, float, str]


def consume_confirmation_ticket(
    ticket_id: str,
    *,
    policy: EgressPolicyConfig | None = None,
    registry: ProviderRegistry | None = None,
    continuation_flow_id: str | None = None,
    now: datetime | None = None,
) -> EgressTicketConsumption:
    """Consume one pending ticket and create one active reservation atomically.

    Continuation tickets additionally revalidate and reactivate their paused flow in
    the same SQLite transaction. Expired or revoked continuation tickets terminalize
    the canonical no-dispatch pause attempt without invoking an adapter.
    """

    if not isinstance(ticket_id, str) or not ticket_id.strip():
        raise EgressContractError("ticket_id must be non-empty text")
    if continuation_flow_id is not None and (
        not isinstance(continuation_flow_id, str) or not continuation_flow_id.strip()
    ):
        raise EgressContractError("continuation_flow_id must be non-empty text")
    policy = policy or load_default_egress_policy()
    registry = registry or load_default_provider_registry()
    now_dt = persistence._normalized_now(now)
    now_iso = now_dt.isoformat()

    with persistence._immediate_transaction() as connection:
        persistence._expire_stale_active_reservations(connection, now_iso=now_iso)
        row = _ticket_row(connection, ticket_id)
        if row is None:
            raise persistence.EgressStateError("confirmation ticket was not found")
        if row["ticket_state"] != "pending":
            raise persistence.EgressStateError(
                f"confirmation ticket is not pending: {row['ticket_state']}"
            )
        authority_json = row["continuation_authority_json"]
        if authority_json is not None and continuation_flow_id is None:
            raise persistence.EgressStateError(
                "continuation confirmation ticket requires flow authority"
            )
        if authority_json is None and continuation_flow_id is not None:
            raise persistence.EgressStateError(
                "ordinary confirmation ticket cannot claim continuation authority"
            )

        if row["expires_at"] <= now_iso:
            _transition_ticket(
                connection,
                ticket_id=ticket_id,
                expected_version=int(row["ticket_version"]),
                new_state="expired",
                now_iso=now_iso,
                reason="ticket_expired",
            )
            pause_id = (
                terminalize_rejected_continuation_confirmation_in_transaction(
                    connection,
                    flow_id=continuation_flow_id,
                    ticket_id=ticket_id,
                    terminal_reason="ticket_expired",
                    now=now_dt,
                )
                if authority_json is not None and continuation_flow_id is not None
                else None
            )
            return _ticket_consumption(
                row,
                authorized=False,
                reason_code="ticket_expired",
                continuation_pause_attempt_id=pause_id,
            )

        if authority_json is not None and continuation_flow_id is not None:
            try:
                validate_pending_continuation_confirmation_in_transaction(
                    connection,
                    flow_id=continuation_flow_id,
                    ticket_id=ticket_id,
                    authority_json=str(authority_json),
                    now=now_dt,
                )
            except (EgressContractError, persistence.EgressStateError, ValueError):
                _transition_ticket(
                    connection,
                    ticket_id=ticket_id,
                    expected_version=int(row["ticket_version"]),
                    new_state="revoked",
                    now_iso=now_iso,
                    reason="ticket_continuation_authority_invalid",
                )
                pause_id = terminalize_rejected_continuation_confirmation_in_transaction(
                    connection,
                    flow_id=continuation_flow_id,
                    ticket_id=ticket_id,
                    terminal_reason="ticket_continuation_authority_invalid",
                    now=now_dt,
                )
                return _ticket_consumption(
                    row,
                    authorized=False,
                    reason_code="ticket_continuation_authority_invalid",
                    continuation_pause_attempt_id=pause_id,
                )

        try:
            material, projection = _rebuild_ticket_projection(
                row,
                policy=policy,
                registry=registry,
            )
            validate_ticket_authority_state(
                connection,
                material=material,
                projection=projection,
                allow_continuation_segments=authority_json is not None,
            )
        except (EgressContractError, persistence.EgressStateError, ValueError):
            _transition_ticket(
                connection,
                ticket_id=ticket_id,
                expected_version=int(row["ticket_version"]),
                new_state="revoked",
                now_iso=now_iso,
                reason="ticket_binding_or_policy_drift",
            )
            pause_id = (
                terminalize_rejected_continuation_confirmation_in_transaction(
                    connection,
                    flow_id=continuation_flow_id,
                    ticket_id=ticket_id,
                    terminal_reason="ticket_binding_or_policy_drift",
                    now=now_dt,
                )
                if authority_json is not None and continuation_flow_id is not None
                else None
            )
            return _ticket_consumption(
                row,
                authorized=False,
                reason_code="ticket_binding_or_policy_drift",
                continuation_pause_attempt_id=pause_id,
            )

        snapshot = persistence._budget_snapshot(
            connection,
            provider_id=material.provider_id,
            now_dt=now_dt,
            now_iso=now_iso,
        )
        blocking_reason = persistence._hard_blocking_reason(
            connection,
            material=material,
            projection=projection,
            registry=registry,
            snapshot=snapshot,
        )
        if blocking_reason is not None:
            _transition_ticket(
                connection,
                ticket_id=ticket_id,
                expected_version=int(row["ticket_version"]),
                new_state="revoked",
                now_iso=now_iso,
                reason=blocking_reason,
            )
            pause_id = (
                terminalize_rejected_continuation_confirmation_in_transaction(
                    connection,
                    flow_id=continuation_flow_id,
                    ticket_id=ticket_id,
                    terminal_reason=blocking_reason,
                    now=now_dt,
                )
                if authority_json is not None and continuation_flow_id is not None
                else None
            )
            return _ticket_consumption(
                row,
                authorized=False,
                reason_code=blocking_reason,
                continuation_pause_attempt_id=pause_id,
            )

        reservation_id = str(uuid4())
        updated = connection.execute(
            """
            UPDATE egress_confirmation_tickets
            SET state = 'consumed', version = version + 1, consumed_at = ?
            WHERE id = ? AND state = 'pending' AND version = ?
            """,
            (now_iso, ticket_id, int(row["ticket_version"])),
        )
        if updated.rowcount != 1:
            raise persistence.EgressStateError("confirmation ticket CAS conflict")
        persistence._insert_reservation(
            connection,
            reservation_id=reservation_id,
            decision_id=str(row["decision_id"]),
            material=material,
            projection=projection,
            now_dt=now_dt,
            policy=policy,
        )
        pause_id = None
        if authority_json is not None and continuation_flow_id is not None:
            resolved = activate_consumed_continuation_confirmation_in_transaction(
                connection,
                flow_id=continuation_flow_id,
                ticket_id=ticket_id,
                authority_json=str(authority_json),
                now=now_dt,
            )
            pause_id = resolved.pause_attempt_id
        return _ticket_consumption(
            row,
            authorized=True,
            reason_code="ticket_consumed",
            reservation_id=reservation_id,
            packet_json=projection.packet_json,
            continuation_pause_attempt_id=pause_id,
        )


def start_reserved_attempt(
    reservation_id: str,
    *,
    ai_job_id: str,
    now: datetime | None = None,
) -> EgressStartedAttempt:
    """Bind an existing ai_jobs row and move one active reservation in-flight."""

    _required_identifier(reservation_id, "reservation_id")
    _required_identifier(ai_job_id, "ai_job_id")
    now_dt = persistence._normalized_now(now)
    now_iso = now_dt.isoformat()
    expired = False
    started: EgressStartedAttempt | None = None
    with persistence._immediate_transaction() as connection:
        row = _reservation_row(connection, reservation_id)
        if row is None:
            raise persistence.EgressStateError("reservation was not found")
        if row["reservation_state"] != "active":
            raise persistence.EgressStateError(
                f"reservation is not active: {row['reservation_state']}"
            )
        if row["expires_at"] <= now_iso:
            updated = connection.execute(
                """
                UPDATE egress_budget_reservations
                SET state = 'expired', version = version + 1,
                    reconciled_at = ?, reconciliation_status = 'expired_before_start'
                WHERE id = ? AND state = 'active' AND version = ?
                """,
                (now_iso, reservation_id, int(row["reservation_version"])),
            )
            if updated.rowcount != 1:
                raise persistence.EgressStateError("reservation expiry CAS conflict")
            expired = True
        else:
            _validate_ai_job_binding(connection, ai_job_id=ai_job_id, row=row)
            updated = connection.execute(
                """
                UPDATE egress_budget_reservations
                SET state = 'in_flight', version = version + 1,
                    attempt_started_at = ?, ai_job_id = ?
                WHERE id = ? AND state = 'active' AND version = ?
                """,
                (
                    now_iso,
                    ai_job_id,
                    reservation_id,
                    int(row["reservation_version"]),
                ),
            )
            if updated.rowcount != 1:
                raise persistence.EgressStateError("reservation start CAS conflict")
            started = EgressStartedAttempt(
                reservation_id=reservation_id,
                decision_id=str(row["decision_id"]),
                packet_id=str(row["packet_id"]),
                packet_digest=str(row["packet_digest"]),
                ai_job_id=ai_job_id,
                provider_id=str(row["provider_id"]),
                model_id=str(row["model_id"]),
                route_class=str(row["route_class"]),
                fallback_index=int(row["fallback_index"]),
                max_output_tokens=int(row["max_output_tokens"]),
                packet_json=str(row["packet_json"]),
            )
    if expired:
        raise persistence.EgressStateError("reservation expired before start")
    if started is None:
        raise persistence.EgressStateError("reservation start produced no result")
    return started


def reconcile_reserved_attempt(
    reservation_id: str,
    *,
    ai_job_id: str,
    network_attempt: bool,
    actual_input_tokens: int | None = None,
    actual_output_tokens: int | None = None,
    usage_source: str = "actual",
    registry: ProviderRegistry | None = None,
    now: datetime | None = None,
) -> EgressReconciliation:
    """Create one immutable egress_attempt and finalize the reservation.

    A failed-before-network path records zero provider consumption and releases the
    reservation. Network attempts use current matching pricing when possible and fall
    back to the reserved upper bound on missing usage or pricing drift.
    """

    _required_identifier(reservation_id, "reservation_id")
    _required_identifier(ai_job_id, "ai_job_id")
    if not isinstance(network_attempt, bool):
        raise EgressContractError("network_attempt must be boolean")
    if usage_source not in {"actual", "mixed", "estimated"}:
        raise EgressContractError("unsupported usage_source")
    registry = registry or load_default_provider_registry()
    now_dt = persistence._normalized_now(now)
    now_iso = now_dt.isoformat()

    with persistence._immediate_transaction() as connection:
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
                actual_output_tokens = ?, actual_cost_usd = ?,
                reconciliation_status = ?
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
        persisted = connection.execute(
            """
            SELECT reconciliation_status, actual_input_tokens,
                   actual_output_tokens, actual_cost_usd
            FROM egress_attempts WHERE id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if persisted is None:
            raise persistence.EgressStateError("reconciled egress attempt was not found")
        return EgressReconciliation(
            egress_attempt_id=attempt_id,
            reservation_id=reservation_id,
            decision_id=str(row["decision_id"]),
            packet_id=str(row["packet_id"]),
            ai_job_id=ai_job_id,
            network_attempt=network_attempt,
            reservation_state=terminal_state,
            reconciliation_status=str(persisted["reconciliation_status"]),
            actual_input_tokens=int(persisted["actual_input_tokens"]),
            actual_output_tokens=int(persisted["actual_output_tokens"]),
            actual_cost_usd=float(persisted["actual_cost_usd"]),
        )


def _ticket_row(connection: sqlite3.Connection, ticket_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT
            ticket.id AS ticket_id, ticket.state AS ticket_state,
            ticket.version AS ticket_version, ticket.expires_at,
            ticket.packet_digest AS ticket_packet_digest,
            ticket.policy_version AS ticket_policy_version,
            ticket.config_digest AS ticket_config_digest,
            ticket.source_digests_json, ticket.continuation_authority_json,
            decision.id AS decision_id, decision.safe_input_digest,
            decision.prompt_level, decision.context_level, decision.final_level,
            decision.projected_input_tokens, decision.projected_output_tokens,
            decision.projected_cost_upper_usd, decision.pricing_version,
            decision.pricing_effective_at, decision.trigger_version,
            packet.id AS packet_id, packet.workspace_id, packet.packet_digest,
            packet.operation, packet.task_kind, packet.route_class,
            packet.provider_id, packet.model_id, packet.fallback_index,
            packet.prompt_digest, packet.prompt_derivative_id,
            packet.packet_json, packet.included_manifest_json,
            packet.withheld_manifest_json, packet.sanitizer_failed_manifest_json,
            packet.policy_capped_manifest_json,
            packet.budget_dropped_manifest_json, packet.max_output_tokens,
            packet.policy_version, packet.trigger_version AS packet_trigger_version,
            packet.config_digest
        FROM egress_confirmation_tickets AS ticket
        JOIN egress_decisions AS decision ON decision.id = ticket.decision_id
        JOIN egress_packets AS packet ON packet.id = ticket.packet_id
        WHERE ticket.id = ?
        """,
        (ticket_id,),
    ).fetchone()


def _reservation_row(
    connection: sqlite3.Connection, reservation_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT
            reservation.id AS reservation_id,
            reservation.state AS reservation_state,
            reservation.version AS reservation_version,
            reservation.expires_at, reservation.ai_job_id,
            reservation.projected_input_tokens,
            reservation.projected_output_tokens,
            reservation.projected_cost_upper_usd,
            decision.id AS decision_id, decision.pricing_version,
            decision.pricing_effective_at,
            packet.id AS packet_id, packet.packet_digest, packet.packet_json,
            packet.route_class, packet.provider_id, packet.model_id,
            packet.fallback_index, packet.max_output_tokens
        FROM egress_budget_reservations AS reservation
        JOIN egress_decisions AS decision ON decision.id = reservation.decision_id
        JOIN egress_packets AS packet ON packet.packet_digest = reservation.packet_digest
        WHERE reservation.id = ?
        """,
        (reservation_id,),
    ).fetchone()


def _rebuild_ticket_projection(
    row: sqlite3.Row,
    *,
    policy: EgressPolicyConfig,
    registry: ProviderRegistry,
) -> tuple[EgressPacketMaterial, EgressPacketProjection]:
    packet = json.loads(row["packet_json"])
    source_digests = json.loads(row["source_digests_json"])
    material = EgressPacketMaterial(
        operation=str(row["operation"]),
        task_kind=str(row["task_kind"]),
        route_class=str(row["route_class"]),
        provider_id=str(row["provider_id"]),
        model_id=str(row["model_id"]),
        fallback_index=int(row["fallback_index"]),
        prompt=str(packet["prompt"]),
        context_blocks=tuple(packet["context_blocks"]),
        prompt_level=str(row["prompt_level"]),
        context_level=str(row["context_level"]),
        final_level=str(row["final_level"]),
        max_output_tokens=int(row["max_output_tokens"]),
        workspace_id=row["workspace_id"],
        prompt_derivative_id=row["prompt_derivative_id"],
        included_manifest=tuple(json.loads(row["included_manifest_json"])),
        withheld_manifest=tuple(json.loads(row["withheld_manifest_json"])),
        sanitizer_failed_manifest=tuple(
            json.loads(row["sanitizer_failed_manifest_json"])
        ),
        policy_capped_manifest=tuple(json.loads(row["policy_capped_manifest_json"])),
        budget_dropped_manifest=tuple(json.loads(row["budget_dropped_manifest_json"])),
        source_digests=tuple(sorted(source_digests.items())),
    )
    projection = build_packet_projection(material, policy=policy, registry=registry)
    expected = {
        "packet_digest": row["packet_digest"],
        "prompt_digest": row["prompt_digest"],
        "safe_input_digest": row["safe_input_digest"],
        "projected_input_tokens": row["projected_input_tokens"],
        "projected_output_tokens": row["projected_output_tokens"],
        "projected_cost_upper_usd": row["projected_cost_upper_usd"],
        "pricing_version": row["pricing_version"],
        "pricing_effective_at": row["pricing_effective_at"],
        "policy_version": row["policy_version"],
        "trigger_version": row["packet_trigger_version"],
        "config_digest": row["config_digest"],
    }
    actual = {
        "packet_digest": projection.packet_digest,
        "prompt_digest": projection.prompt_digest,
        "safe_input_digest": projection.safe_input_digest,
        "projected_input_tokens": projection.projected_input_tokens,
        "projected_output_tokens": projection.projected_output_tokens,
        "projected_cost_upper_usd": projection.projected_cost_upper_usd,
        "pricing_version": projection.pricing_version,
        "pricing_effective_at": projection.pricing_effective_at,
        "policy_version": projection.policy_version,
        "trigger_version": projection.trigger_version,
        "config_digest": projection.config_digest,
    }
    if actual != expected:
        raise persistence.EgressStateError("ticket packet or policy binding drifted")
    if row["ticket_packet_digest"] != projection.packet_digest:
        raise persistence.EgressStateError("ticket packet digest mismatch")
    if row["ticket_policy_version"] != projection.policy_version:
        raise persistence.EgressStateError("ticket policy version mismatch")
    if row["ticket_config_digest"] != projection.config_digest:
        raise persistence.EgressStateError("ticket config digest mismatch")
    return material, projection


def _transition_ticket(
    connection: sqlite3.Connection,
    *,
    ticket_id: str,
    expected_version: int,
    new_state: str,
    now_iso: str,
    reason: str,
) -> None:
    if new_state == "expired":
        values = (new_state, expected_version + 1, ticket_id, expected_version)
        sql = """
            UPDATE egress_confirmation_tickets
            SET state = ?, version = ?
            WHERE id = ? AND state = 'pending' AND version = ?
        """
    elif new_state == "revoked":
        values = (
            new_state,
            expected_version + 1,
            now_iso,
            reason,
            ticket_id,
            expected_version,
        )
        sql = """
            UPDATE egress_confirmation_tickets
            SET state = ?, version = ?, revoked_at = ?, revocation_reason = ?
            WHERE id = ? AND state = 'pending' AND version = ?
        """
    else:
        raise EgressContractError("unsupported ticket transition")
    updated = connection.execute(sql, values)
    if updated.rowcount != 1:
        raise persistence.EgressStateError("confirmation ticket transition CAS conflict")


def _ticket_consumption(
    row: sqlite3.Row,
    *,
    authorized: bool,
    reason_code: str,
    reservation_id: str | None = None,
    packet_json: str | None = None,
    continuation_pause_attempt_id: str | None = None,
) -> EgressTicketConsumption:
    return EgressTicketConsumption(
        authorized=authorized,
        reason_code=reason_code,
        ticket_id=str(row["ticket_id"]),
        decision_id=str(row["decision_id"]),
        packet_id=str(row["packet_id"]),
        reservation_id=reservation_id,
        packet_digest=str(row["packet_digest"]),
        provider_id=str(row["provider_id"]),
        model_id=str(row["model_id"]),
        route_class=str(row["route_class"]),
        fallback_index=int(row["fallback_index"]),
        max_output_tokens=int(row["max_output_tokens"]),
        continuation_authority_json=row["continuation_authority_json"],
        continuation_pause_attempt_id=continuation_pause_attempt_id,
        packet_json=packet_json,
    )


def _validate_ai_job_binding(
    connection: sqlite3.Connection, *, ai_job_id: str, row: sqlite3.Row
) -> None:
    ai_job = connection.execute(
        "SELECT provider_id, model_id, selected_route_class FROM ai_jobs WHERE id = ?",
        (ai_job_id,),
    ).fetchone()
    if ai_job is None:
        raise persistence.EgressStateError("ai_job must exist before reservation transition")
    if (
        ai_job["provider_id"],
        ai_job["model_id"],
        ai_job["selected_route_class"],
    ) != (row["provider_id"], row["model_id"], row["route_class"]):
        raise persistence.EgressStateError("ai_job binding does not match reservation packet")


def _verified_ai_job_usage(
    connection: sqlite3.Connection, *, ai_job_id: str
) -> _VerifiedUsage | None:
    row = connection.execute(
        """
        SELECT status, input_tokens, output_tokens, cost_estimate, usage_source
        FROM ai_jobs WHERE id = ?
        """,
        (ai_job_id,),
    ).fetchone()
    if row is None:
        raise persistence.EgressStateError("ai_job was not found during reconciliation")
    if (
        row["status"] == "queued"
        or row["input_tokens"] is None
        or row["output_tokens"] is None
        or row["cost_estimate"] is None
        or row["usage_source"] not in {"actual", "estimated", "mixed"}
    ):
        return None
    return (
        int(row["input_tokens"]),
        int(row["output_tokens"]),
        float(row["cost_estimate"]),
        str(row["usage_source"]),
    )


def _conservative_usage(
    row: sqlite3.Row,
    *,
    status: str,
    verified_usage: _VerifiedUsage | None = None,
    reported_input_tokens: int | None = None,
    reported_output_tokens: int | None = None,
    expected_cost: float | None = None,
) -> tuple[int, int, float, str]:
    input_candidates = [int(row["projected_input_tokens"])]
    output_candidates = [int(row["projected_output_tokens"])]
    cost_candidates = [float(row["projected_cost_upper_usd"])]
    if verified_usage is not None:
        input_candidates.append(verified_usage[0])
        output_candidates.append(verified_usage[1])
        cost_candidates.append(verified_usage[2])
    if reported_input_tokens is not None:
        input_candidates.append(reported_input_tokens)
    if reported_output_tokens is not None:
        output_candidates.append(reported_output_tokens)
    if expected_cost is not None:
        cost_candidates.append(expected_cost)
    return max(input_candidates), max(output_candidates), max(cost_candidates), status


def _conservative_cost_only(
    row: sqlite3.Row,
    *,
    status: str,
    verified_usage: _VerifiedUsage,
    expected_cost: float | None = None,
) -> tuple[int, int, float, str]:
    verified_input, verified_output, verified_cost, _ = verified_usage
    cost_candidates = [float(row["projected_cost_upper_usd"]), verified_cost]
    if expected_cost is not None:
        cost_candidates.append(expected_cost)
    return verified_input, verified_output, max(cost_candidates), status


def _actual_usage(
    row: sqlite3.Row,
    *,
    network_attempt: bool,
    actual_input_tokens: int | None,
    actual_output_tokens: int | None,
    usage_source: str,
    verified_usage: _VerifiedUsage | None,
    registry: ProviderRegistry,
) -> tuple[int, int, float, str]:
    if not network_attempt:
        if actual_input_tokens not in {None, 0} or actual_output_tokens not in {None, 0}:
            raise EgressContractError("pre-network reconciliation cannot report provider usage")
        return 0, 0, 0.0, "not_sent"

    if actual_input_tokens is None or actual_output_tokens is None:
        return _conservative_usage(
            row,
            status="conservative_missing_usage",
            verified_usage=verified_usage,
        )
    for value, field_name in (
        (actual_input_tokens, "actual_input_tokens"),
        (actual_output_tokens, "actual_output_tokens"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EgressContractError(f"{field_name} must be a non-negative integer")

    if (
        usage_source != "actual"
        or verified_usage is None
        or verified_usage[3] != "actual"
    ):
        return _conservative_usage(
            row,
            status="conservative_unverified_usage",
            verified_usage=verified_usage,
            reported_input_tokens=actual_input_tokens,
            reported_output_tokens=actual_output_tokens,
        )

    verified_input, verified_output, verified_cost, _ = verified_usage
    if (actual_input_tokens, actual_output_tokens) != (verified_input, verified_output):
        return _conservative_usage(
            row,
            status="conservative_usage_binding_mismatch",
            verified_usage=verified_usage,
            reported_input_tokens=actual_input_tokens,
            reported_output_tokens=actual_output_tokens,
        )

    try:
        pricing = resolve_model_pricing(
            registry,
            str(row["provider_id"]),
            str(row["model_id"]),
        )
    except ValueError:
        pricing = None
    pricing_matches = pricing is not None and (
        pricing.pricing_version,
        pricing.pricing_effective_at,
    ) == (row["pricing_version"], row["pricing_effective_at"])
    if not pricing_matches:
        current_pricing_cost = None
        if pricing is not None:
            current_pricing_cost = (
                actual_input_tokens * pricing.input_usd_per_1m_tokens
                + actual_output_tokens * pricing.output_usd_per_1m_tokens
            ) / 1_000_000
        return _conservative_cost_only(
            row,
            status="conservative_pricing_drift",
            verified_usage=verified_usage,
            expected_cost=current_pricing_cost,
        )

    expected_cost = (
        actual_input_tokens * pricing.input_usd_per_1m_tokens
        + actual_output_tokens * pricing.output_usd_per_1m_tokens
    ) / 1_000_000
    tolerance = max(1e-12, expected_cost * 1e-9)
    if abs(verified_cost - expected_cost) > tolerance:
        return _conservative_cost_only(
            row,
            status="conservative_cost_binding_mismatch",
            verified_usage=verified_usage,
            expected_cost=expected_cost,
        )
    return actual_input_tokens, actual_output_tokens, verified_cost, "actual"


def _required_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise EgressContractError(f"{field_name} must be non-empty text")
