from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.ai.contracts import AIPolicyMode
from app.modules.ai.egress_policy import EgressPolicyConfig, load_default_egress_policy
from app.modules.ai.egress_service import (
    EgressContractError,
    EgressPacketMaterial,
    EgressPacketProjection,
    build_packet_projection,
)
from app.modules.ai.provider_registry import ProviderRegistry, load_default_provider_registry
from app.modules.secrets.storage import resolve_secret_ref


class EgressStateError(RuntimeError):
    """Fail-closed state or concurrency error in the 059b persistence boundary."""


@dataclass(frozen=True)
class EgressPreparation:
    decision_id: str
    result: str
    reason_code: str
    packet_id: str
    packet_digest: str
    ticket_id: str | None
    reservation_id: str | None
    trigger_ids: tuple[str, ...]
    confirmation_required: bool
    projected_input_tokens: int
    projected_output_tokens: int
    projected_cost_upper_usd: float
    provider_id: str
    model_id: str
    fallback_index: int
    policy_version: str
    trigger_version: str
    config_digest: str


@dataclass(frozen=True, slots=True)
class EgressContinuationAuthority:
    flow_id: str
    parent_attempt_id: str
    parent_flow_attempt_index: int
    next_continuation_index: int
    protected_segment_index: int
    expected_sensitivity_level: str

    def canonical_json(self) -> str:
        for name, value in (
            ("flow_id", self.flow_id),
            ("parent_attempt_id", self.parent_attempt_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise EgressContractError(f"{name} must be non-empty text")
        for name, value, minimum in (
            ("parent_flow_attempt_index", self.parent_flow_attempt_index, 0),
            ("next_continuation_index", self.next_continuation_index, 1),
            ("protected_segment_index", self.protected_segment_index, 0),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise EgressContractError(f"{name} is outside its valid range")
        if self.expected_sensitivity_level not in {"S0", "S1"}:
            raise EgressContractError(
                "external continuation authority requires S0 or S1 sensitivity"
            )
        return json.dumps(
            {
                "expected_sensitivity_level": self.expected_sensitivity_level,
                "flow_id": self.flow_id.strip(),
                "next_continuation_index": self.next_continuation_index,
                "parent_attempt_id": self.parent_attempt_id.strip(),
                "parent_flow_attempt_index": self.parent_flow_attempt_index,
                "protected_segment_index": self.protected_segment_index,
                "version": "token-flow-confirmation-resume-v0",
            },
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class _BudgetSnapshot:
    global_actual_cost_usd: float
    global_reserved_cost_usd: float
    provider_actual_tokens: int
    provider_actual_cost_usd: float
    provider_reserved_tokens: int
    provider_reserved_cost_usd: float
    today_actual_cost_usd: float
    today_reserved_cost_usd: float


@dataclass(frozen=True)
class EgressAvailabilityProjection:
    available: bool
    blocking_reason: str | None
    global_actual_cost_usd: float
    global_reserved_cost_usd: float
    budget_exhausted: bool


@dataclass(frozen=True)
class _AvailabilityMaterial:
    provider_id: str


@dataclass(frozen=True)
class _AvailabilityProjectionInputs:
    projected_cost_upper_usd: float = 0.0
    projected_input_tokens: int = 0
    projected_output_tokens: int = 0


def _global_budget_exhausted(
    connection: sqlite3.Connection,
    *,
    snapshot: _BudgetSnapshot,
) -> bool:
    """Project the independent global-budget axis without changing deny precedence."""

    settings = connection.execute(
        """
        SELECT monthly_api_budget_usd, api_spend_month_to_date_usd
        FROM ai_settings WHERE id = 'default'
        """
    ).fetchone()
    if settings is None:
        return False
    monthly_budget = float(settings["monthly_api_budget_usd"])
    if monthly_budget <= 0:
        return True
    configured_global_spend = float(settings["api_spend_month_to_date_usd"])
    global_actual = max(configured_global_spend, snapshot.global_actual_cost_usd)
    return global_actual + snapshot.global_reserved_cost_usd >= monthly_budget


def project_egress_availability(
    provider_id: str,
    *,
    registry: ProviderRegistry | None = None,
    now: datetime | None = None,
) -> EgressAvailabilityProjection:
    """Read the canonical next-attempt blocking/accounting truth without mutation.

    This intentionally does not expire or reconcile reservations, persist packets,
    decisions, attempts or reservations, or dispatch a provider. Active/in-flight
    reservations are counted exactly as stored by the execution owner.
    """

    registry = registry or load_default_provider_registry()
    provider = registry.providers.get(provider_id)
    now_dt = _normalized_now(now)
    now_iso = now_dt.isoformat()

    with open_sqlite_connection() as connection:
        snapshot = _budget_snapshot(
            connection,
            provider_id=provider_id,
            now_dt=now_dt,
            now_iso=now_iso,
        )
        global_actual = snapshot.global_actual_cost_usd
        global_budget_exhausted = _global_budget_exhausted(
            connection,
            snapshot=snapshot,
        )

        if provider is None:
            return EgressAvailabilityProjection(
                available=False,
                blocking_reason="provider_unknown",
                global_actual_cost_usd=global_actual,
                global_reserved_cost_usd=snapshot.global_reserved_cost_usd,
                budget_exhausted=global_budget_exhausted,
            )

        if not provider.enabled:
            blocking_reason = "provider_disabled"
        else:
            blocking_reason = _hard_blocking_reason(
                connection,
                material=_AvailabilityMaterial(provider_id=provider_id),
                projection=_AvailabilityProjectionInputs(),
                registry=registry,
                snapshot=snapshot,
            )

    return EgressAvailabilityProjection(
        available=blocking_reason is None,
        blocking_reason=blocking_reason,
        global_actual_cost_usd=global_actual,
        global_reserved_cost_usd=snapshot.global_reserved_cost_usd,
        budget_exhausted=global_budget_exhausted,
    )


def prepare_egress_attempt(
    material: EgressPacketMaterial,
    *,
    policy: EgressPolicyConfig | None = None,
    registry: ProviderRegistry | None = None,
    continuation_authority: EgressContinuationAuthority | None = None,
    now: datetime | None = None,
) -> EgressPreparation:
    """Persist one immutable packet and one policy decision atomically.

    Eligible first-use or operator-gated attempts return a pending ticket and create no
    reservation. Silent allows create exactly one active projected-cost reservation.
    Hard denials create neither. No adapter is called by this service.
    """

    policy = policy or load_default_egress_policy()
    registry = registry or load_default_provider_registry()
    if continuation_authority is not None and not isinstance(
        continuation_authority, EgressContinuationAuthority
    ):
        raise TypeError("continuation_authority must be EgressContinuationAuthority")
    projection = build_packet_projection(material, policy=policy, registry=registry)
    now_dt = _normalized_now(now)
    now_iso = now_dt.isoformat()

    with _immediate_transaction() as connection:
        _expire_stale_active_reservations(connection, now_iso=now_iso)
        packet_id = _persist_packet(
            connection,
            material=material,
            projection=projection,
            now_iso=now_iso,
        )
        snapshot = _budget_snapshot(
            connection,
            provider_id=material.provider_id,
            now_dt=now_dt,
            now_iso=now_iso,
        )
        blocking_reason = _hard_blocking_reason(
            connection,
            material=material,
            projection=projection,
            registry=registry,
            snapshot=snapshot,
        )
        if blocking_reason is not None:
            decision_id = _insert_decision(
                connection,
                material=material,
                projection=projection,
                packet_id=packet_id,
                now_iso=now_iso,
                result="deny",
                reason_code=blocking_reason,
                trigger_ids=(),
                confirmation_required=False,
                reservation_id=None,
                ticket_id=None,
            )
            return _preparation(
                decision_id=decision_id,
                packet_id=packet_id,
                material=material,
                projection=projection,
                result="deny",
                reason_code=blocking_reason,
                trigger_ids=(),
                ticket_id=None,
                reservation_id=None,
            )

        trigger_ids = _trigger_ids(
            connection,
            material=material,
            projection=projection,
            policy=policy,
            snapshot=snapshot,
        )
        unsupported = set(trigger_ids) - set(policy.confirmable_triggers)
        if unsupported:
            raise EgressStateError("runtime produced a non-confirmable confirmation trigger")

        if trigger_ids and _is_bluecad_structural_packet(material):
            decision_id = _insert_decision(
                connection,
                material=material,
                projection=projection,
                packet_id=packet_id,
                now_iso=now_iso,
                result="deny",
                reason_code="bluecad_structural_confirmation_unsupported",
                trigger_ids=trigger_ids,
                confirmation_required=False,
                reservation_id=None,
                ticket_id=None,
            )
            return _preparation(
                decision_id=decision_id,
                packet_id=packet_id,
                material=material,
                projection=projection,
                result="deny",
                reason_code="bluecad_structural_confirmation_unsupported",
                trigger_ids=trigger_ids,
                ticket_id=None,
                reservation_id=None,
            )

        if trigger_ids:
            decision_id = str(uuid4())
            ticket_id = str(uuid4())
            _insert_decision(
                connection,
                material=material,
                projection=projection,
                packet_id=packet_id,
                now_iso=now_iso,
                result="pause",
                reason_code="confirmation_required",
                trigger_ids=trigger_ids,
                confirmation_required=True,
                reservation_id=None,
                ticket_id=ticket_id,
                decision_id=decision_id,
            )
            _insert_ticket(
                connection,
                ticket_id=ticket_id,
                decision_id=decision_id,
                packet_id=packet_id,
                material=material,
                projection=projection,
                trigger_ids=trigger_ids,
                now_dt=now_dt,
                policy=policy,
                continuation_authority=continuation_authority,
            )
            return _preparation(
                decision_id=decision_id,
                packet_id=packet_id,
                material=material,
                projection=projection,
                result="pause",
                reason_code="confirmation_required",
                trigger_ids=trigger_ids,
                ticket_id=ticket_id,
                reservation_id=None,
            )

        decision_id = str(uuid4())
        reservation_id = str(uuid4())
        _insert_decision(
            connection,
            material=material,
            projection=projection,
            packet_id=packet_id,
            now_iso=now_iso,
            result="allow",
            reason_code="silent_allow",
            trigger_ids=(),
            confirmation_required=False,
            reservation_id=reservation_id,
            ticket_id=None,
            decision_id=decision_id,
        )
        _insert_reservation(
            connection,
            reservation_id=reservation_id,
            decision_id=decision_id,
            material=material,
            projection=projection,
            now_dt=now_dt,
            policy=policy,
        )
        return _preparation(
            decision_id=decision_id,
            packet_id=packet_id,
            material=material,
            projection=projection,
            result="allow",
            reason_code="silent_allow",
            trigger_ids=(),
            ticket_id=None,
            reservation_id=reservation_id,
        )


@contextmanager
def _immediate_transaction() -> Iterator[sqlite3.Connection]:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()


def _persist_packet(
    connection: sqlite3.Connection,
    *,
    material: EgressPacketMaterial,
    projection: EgressPacketProjection,
    now_iso: str,
) -> str:
    existing = connection.execute(
        "SELECT * FROM egress_packets WHERE packet_digest = ?",
        (projection.packet_digest,),
    ).fetchone()
    if existing is not None:
        expected = {
            "workspace_id": material.workspace_id,
            "operation": material.operation,
            "task_kind": material.task_kind,
            "route_class": material.route_class,
            "provider_id": material.provider_id,
            "model_id": material.model_id,
            "fallback_index": material.fallback_index,
            "prompt_digest": projection.prompt_digest,
            "prompt_derivative_id": material.prompt_derivative_id,
            "packet_json": projection.packet_json,
            "included_manifest_json": projection.included_manifest_json,
            "withheld_manifest_json": projection.withheld_manifest_json,
            "sanitizer_failed_manifest_json": projection.sanitizer_failed_manifest_json,
            "policy_capped_manifest_json": projection.policy_capped_manifest_json,
            "budget_dropped_manifest_json": projection.budget_dropped_manifest_json,
            "source_digests_json": projection.source_digests_json,
            "final_level": material.final_level,
            "max_output_tokens": material.max_output_tokens,
            "policy_version": projection.policy_version,
            "trigger_version": projection.trigger_version,
            "config_digest": projection.config_digest,
        }
        if any(existing[key] != value for key, value in expected.items()):
            raise EgressStateError("packet digest collision or immutable packet mismatch")
        return str(existing["id"])

    packet_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO egress_packets (
            id, workspace_id, packet_digest, operation, task_kind, route_class,
            provider_id, model_id, fallback_index, prompt_digest,
            prompt_derivative_id, packet_json, included_manifest_json,
            withheld_manifest_json, sanitizer_failed_manifest_json,
            policy_capped_manifest_json, budget_dropped_manifest_json,
            source_digests_json, final_level, max_output_tokens, policy_version,
            trigger_version, config_digest, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            packet_id,
            material.workspace_id,
            projection.packet_digest,
            material.operation,
            material.task_kind,
            material.route_class,
            material.provider_id,
            material.model_id,
            material.fallback_index,
            projection.prompt_digest,
            material.prompt_derivative_id,
            projection.packet_json,
            projection.included_manifest_json,
            projection.withheld_manifest_json,
            projection.sanitizer_failed_manifest_json,
            projection.policy_capped_manifest_json,
            projection.budget_dropped_manifest_json,
            projection.source_digests_json,
            material.final_level,
            material.max_output_tokens,
            projection.policy_version,
            projection.trigger_version,
            projection.config_digest,
            now_iso,
        ),
    )
    return packet_id


def _insert_decision(
    connection: sqlite3.Connection,
    *,
    material: EgressPacketMaterial,
    projection: EgressPacketProjection,
    packet_id: str,
    now_iso: str,
    result: str,
    reason_code: str,
    trigger_ids: tuple[str, ...],
    confirmation_required: bool,
    reservation_id: str | None,
    ticket_id: str | None,
    decision_id: str | None = None,
) -> str:
    decision_id = decision_id or str(uuid4())
    connection.execute(
        """
        INSERT INTO egress_decisions (
            id, workspace_id, created_at, result, reason_code, operation,
            route_class, provider_id, model_id, fallback_index, packet_id,
            packet_digest, safe_input_digest, prompt_level, context_level,
            final_level, source_count, included_count, withheld_count,
            trigger_ids_json, confirmation_required, projected_input_tokens,
            projected_output_tokens, projected_cost_upper_usd, pricing_version,
            pricing_effective_at, reservation_id, ticket_id, policy_version,
            trigger_version, config_digest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            material.workspace_id,
            now_iso,
            result,
            reason_code,
            material.operation,
            material.route_class,
            material.provider_id,
            material.model_id,
            material.fallback_index,
            packet_id,
            projection.packet_digest,
            projection.safe_input_digest,
            material.prompt_level,
            material.context_level,
            material.final_level,
            projection.source_count,
            projection.included_count,
            projection.withheld_count,
            json.dumps(trigger_ids, separators=(",", ":")),
            int(confirmation_required),
            projection.projected_input_tokens,
            projection.projected_output_tokens,
            projection.projected_cost_upper_usd,
            projection.pricing_version,
            projection.pricing_effective_at,
            reservation_id,
            ticket_id,
            projection.policy_version,
            projection.trigger_version,
            projection.config_digest,
        ),
    )
    return decision_id


def _insert_ticket(
    connection: sqlite3.Connection,
    *,
    ticket_id: str,
    decision_id: str,
    packet_id: str,
    material: EgressPacketMaterial,
    projection: EgressPacketProjection,
    trigger_ids: tuple[str, ...],
    now_dt: datetime,
    policy: EgressPolicyConfig,
    continuation_authority: EgressContinuationAuthority | None,
) -> None:
    connection.execute(
        """
        INSERT INTO egress_confirmation_tickets (
            id, decision_id, packet_id, packet_digest, provider_id, model_id,
            trigger_ids_json, source_digests_json, continuation_authority_json,
            policy_version, config_digest, state, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            ticket_id,
            decision_id,
            packet_id,
            projection.packet_digest,
            material.provider_id,
            material.model_id,
            json.dumps(trigger_ids, separators=(",", ":")),
            projection.source_digests_json,
            (
                continuation_authority.canonical_json()
                if continuation_authority is not None
                else None
            ),
            projection.policy_version,
            projection.config_digest,
            now_dt.isoformat(),
            (now_dt + timedelta(seconds=policy.confirmation_ticket_ttl_seconds)).isoformat(),
        ),
    )


def _insert_reservation(
    connection: sqlite3.Connection,
    *,
    reservation_id: str,
    decision_id: str,
    material: EgressPacketMaterial,
    projection: EgressPacketProjection,
    now_dt: datetime,
    policy: EgressPolicyConfig,
) -> None:
    connection.execute(
        """
        INSERT INTO egress_budget_reservations (
            id, decision_id, packet_digest, provider_id, model_id,
            projected_input_tokens, projected_output_tokens,
            projected_cost_upper_usd, state, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        (
            reservation_id,
            decision_id,
            projection.packet_digest,
            material.provider_id,
            material.model_id,
            projection.projected_input_tokens,
            projection.projected_output_tokens,
            projection.projected_cost_upper_usd,
            now_dt.isoformat(),
            (now_dt + timedelta(seconds=policy.reservation_ttl_seconds)).isoformat(),
        ),
    )


def _hard_blocking_reason(
    connection: sqlite3.Connection,
    *,
    material: EgressPacketMaterial | _AvailabilityMaterial,
    projection: EgressPacketProjection | _AvailabilityProjectionInputs,
    registry: ProviderRegistry,
    snapshot: _BudgetSnapshot,
) -> str | None:
    settings = connection.execute(
        """
        SELECT policy_mode, monthly_api_budget_usd,
               api_spend_month_to_date_usd, paid_ai_enabled,
               provider_mode, scaleway_enabled,
               scaleway_monthly_token_cap,
               scaleway_hard_stop_token_cap,
               scaleway_input_tokens_month_to_date,
               scaleway_output_tokens_month_to_date
        FROM ai_settings WHERE id = 'default'
        """
    ).fetchone()
    if settings is None:
        return "missing_ai_settings"
    if settings["policy_mode"] == AIPolicyMode.DISABLED.value:
        return "ai_policy_disabled"

    provider = registry.providers[material.provider_id]
    if not provider.requires_network:
        return None

    if not bool(settings["paid_ai_enabled"]):
        return "paid_ai_disabled"
    monthly_budget = float(settings["monthly_api_budget_usd"])
    if monthly_budget <= 0:
        return "monthly_budget_zero"

    if material.provider_id == "scaleway":
        if settings["provider_mode"] != "scaleway":
            return "scaleway_provider_mode_required"
        if not bool(settings["scaleway_enabled"]):
            return "scaleway_disabled"

    try:
        credential = resolve_secret_ref(provider.api_key_ref)
    except ValueError:
        return "provider_credential_reference_invalid"
    if not credential.key_present:
        return "provider_credentials_missing"

    is_availability_projection = isinstance(material, _AvailabilityMaterial)
    configured_global_spend = float(settings["api_spend_month_to_date_usd"])
    global_actual = max(configured_global_spend, snapshot.global_actual_cost_usd)
    projected_global_cost = (
        global_actual
        + snapshot.global_reserved_cost_usd
        + projection.projected_cost_upper_usd
    )
    if projected_global_cost > monthly_budget or (
        is_availability_projection and projected_global_cost >= monthly_budget
    ):
        return "global_monthly_cost_cap_exceeded"
    projected_provider_tokens = (
        snapshot.provider_actual_tokens
        + snapshot.provider_reserved_tokens
        + projection.projected_input_tokens
        + projection.projected_output_tokens
    )
    if material.provider_id == "scaleway":
        monthly_cap = int(settings["scaleway_monthly_token_cap"])
        hard_stop_cap = int(settings["scaleway_hard_stop_token_cap"])
        if monthly_cap <= 0:
            return "scaleway_monthly_token_cap_zero"
        if hard_stop_cap <= 0:
            return "scaleway_hard_stop_token_cap_zero"
        legacy_tokens = int(
            settings["scaleway_input_tokens_month_to_date"]
        ) + int(settings["scaleway_output_tokens_month_to_date"])
        projected_scaleway_tokens = legacy_tokens + projected_provider_tokens
        if projected_scaleway_tokens > monthly_cap or (
            is_availability_projection and projected_scaleway_tokens >= monthly_cap
        ):
            return "scaleway_monthly_token_cap_exceeded"
        if projected_scaleway_tokens > hard_stop_cap or (
            is_availability_projection and projected_scaleway_tokens >= hard_stop_cap
        ):
            return "scaleway_hard_stop_token_cap_exceeded"
    if provider.monthly_token_cap > 0 and (
        projected_provider_tokens > provider.monthly_token_cap
        or (
            is_availability_projection
            and projected_provider_tokens >= provider.monthly_token_cap
        )
    ):
        return "provider_monthly_token_cap_exceeded"
    projected_provider_cost = (
        snapshot.provider_actual_cost_usd
        + snapshot.provider_reserved_cost_usd
        + projection.projected_cost_upper_usd
    )
    if provider.monthly_cost_cap_usd > 0 and (
        projected_provider_cost > provider.monthly_cost_cap_usd
        or (
            is_availability_projection
            and projected_provider_cost >= provider.monthly_cost_cap_usd
        )
    ):
        return "provider_monthly_cost_cap_exceeded"
    return None


def _trigger_ids(
    connection: sqlite3.Connection,
    *,
    material: EgressPacketMaterial,
    projection: EgressPacketProjection,
    policy: EgressPolicyConfig,
    snapshot: _BudgetSnapshot,
) -> tuple[str, ...]:
    triggers: list[str] = []
    prior_attempt = connection.execute(
        """
        SELECT 1
        FROM egress_attempts AS attempt
        JOIN egress_decisions AS decision ON decision.id = attempt.decision_id
        WHERE attempt.provider_id = ? AND attempt.model_id = ?
          AND attempt.network_attempt = 1 AND decision.trigger_version = ?
        LIMIT 1
        """,
        (material.provider_id, material.model_id, projection.trigger_version),
    ).fetchone()
    if prior_attempt is None:
        triggers.append("t1")

    if policy.daily_soft_spend_usd > 0 and (
        snapshot.today_actual_cost_usd
        + snapshot.today_reserved_cost_usd
        + projection.projected_cost_upper_usd
        > policy.daily_soft_spend_usd
    ):
        triggers.append("t2")

    if material.workspace_id is not None:
        row = connection.execute(
            "SELECT ask_me FROM workspace_egress_policy WHERE workspace_id = ?",
            (material.workspace_id,),
        ).fetchone()
        if row is not None and bool(row["ask_me"]):
            triggers.append("t5")
    return tuple(triggers)


def _is_bluecad_structural_packet(material: EgressPacketMaterial) -> bool:
    if material.task_kind != "bluecad_cad_repair":
        return False
    matches: list[dict[str, object]] = []
    for item in material.included_manifest:
        if not isinstance(item, dict):
            continue
        evidence_lineage = item.get("evidence_lineage")
        if isinstance(evidence_lineage, dict):
            matches.append(evidence_lineage)
    return (
        len(matches) == 1
        and matches[0].get("schema_version") == "bluecad_evidence_lineage_v0_1"
    )


def _budget_snapshot(
    connection: sqlite3.Connection,
    *,
    provider_id: str,
    now_dt: datetime,
    now_iso: str,
) -> _BudgetSnapshot:
    month_start = now_dt.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    day_start = now_dt.replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    global_row = connection.execute(
        """
        SELECT COALESCE(SUM(COALESCE(cost_estimate, 0)), 0) AS cost
        FROM ai_jobs WHERE created_at >= ?
        """,
        (month_start,),
    ).fetchone()
    provider_row = connection.execute(
        """
        SELECT
            COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0) AS tokens,
            COALESCE(SUM(COALESCE(cost_estimate, 0)), 0) AS cost
        FROM ai_jobs WHERE provider_id = ? AND created_at >= ?
        """,
        (provider_id, month_start),
    ).fetchone()
    today_row = connection.execute(
        """
        SELECT COALESCE(SUM(COALESCE(cost_estimate, 0)), 0) AS cost
        FROM ai_jobs WHERE created_at >= ?
        """,
        (day_start,),
    ).fetchone()
    global_reservation = connection.execute(
        """
        SELECT COALESCE(SUM(projected_cost_upper_usd), 0) AS cost
        FROM egress_budget_reservations
        WHERE state = 'in_flight' OR (state = 'active' AND expires_at > ?)
        """,
        (now_iso,),
    ).fetchone()
    provider_reservation = connection.execute(
        """
        SELECT
            COALESCE(SUM(projected_input_tokens + projected_output_tokens), 0) AS tokens,
            COALESCE(SUM(projected_cost_upper_usd), 0) AS cost
        FROM egress_budget_reservations
        WHERE provider_id = ?
          AND (state = 'in_flight' OR (state = 'active' AND expires_at > ?))
        """,
        (provider_id, now_iso),
    ).fetchone()
    today_reservation = connection.execute(
        """
        SELECT COALESCE(SUM(projected_cost_upper_usd), 0) AS cost
        FROM egress_budget_reservations
        WHERE created_at >= ?
          AND (state = 'in_flight' OR (state = 'active' AND expires_at > ?))
        """,
        (day_start, now_iso),
    ).fetchone()
    return _BudgetSnapshot(
        global_actual_cost_usd=float(global_row["cost"]),
        global_reserved_cost_usd=float(global_reservation["cost"]),
        provider_actual_tokens=int(provider_row["tokens"]),
        provider_actual_cost_usd=float(provider_row["cost"]),
        provider_reserved_tokens=int(provider_reservation["tokens"]),
        provider_reserved_cost_usd=float(provider_reservation["cost"]),
        today_actual_cost_usd=float(today_row["cost"]),
        today_reserved_cost_usd=float(today_reservation["cost"]),
    )


def _reconcile_stale_in_flight_reservations(
    connection: sqlite3.Connection, *, now_iso: str
) -> None:
    rows = connection.execute(
        """
        SELECT
            reservation.id AS reservation_id,
            reservation.version AS reservation_version,
            reservation.decision_id,
            reservation.ai_job_id,
            reservation.projected_input_tokens,
            reservation.projected_output_tokens,
            reservation.projected_cost_upper_usd,
            packet.id AS packet_id,
            packet.route_class,
            packet.provider_id,
            packet.model_id,
            packet.fallback_index,
            job.status AS ai_job_status,
            job.input_tokens AS ai_job_input_tokens,
            job.output_tokens AS ai_job_output_tokens,
            job.cost_estimate AS ai_job_cost_estimate,
            job.usage_source AS ai_job_usage_source
        FROM egress_budget_reservations AS reservation
        JOIN egress_packets AS packet
          ON packet.packet_digest = reservation.packet_digest
        JOIN ai_jobs AS job ON job.id = reservation.ai_job_id
        WHERE reservation.state = 'in_flight'
          AND reservation.attempt_started_at IS NOT NULL
          AND julianday(?) >= julianday(reservation.attempt_started_at)
              + (julianday(reservation.expires_at) - julianday(reservation.created_at))
        ORDER BY reservation.created_at, reservation.id
        """,
        (now_iso,),
    ).fetchall()

    for row in rows:
        verified_usage = (
            row["ai_job_status"] != "queued"
            and row["ai_job_input_tokens"] is not None
            and row["ai_job_output_tokens"] is not None
            and row["ai_job_cost_estimate"] is not None
            and row["ai_job_usage_source"] == "actual"
        )
        if verified_usage:
            input_tokens = int(row["ai_job_input_tokens"])
            output_tokens = int(row["ai_job_output_tokens"])
            actual_cost_usd = float(row["ai_job_cost_estimate"])
            reconciliation_status = "actual_recovered_after_timeout"
            usage_source = "actual"
        else:
            input_tokens = int(row["projected_input_tokens"])
            output_tokens = int(row["projected_output_tokens"])
            actual_cost_usd = float(row["projected_cost_upper_usd"])
            reconciliation_status = "conservative_in_flight_timeout"
            usage_source = "estimated"

        attempt_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO egress_attempts (
                id, decision_id, packet_id, ai_job_id, reservation_id,
                route_class, provider_id, model_id, fallback_index,
                network_attempt, reconciliation_status,
                projected_input_tokens, projected_output_tokens,
                projected_cost_upper_usd, actual_input_tokens,
                actual_output_tokens, actual_cost_usd, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                row["decision_id"],
                row["packet_id"],
                row["ai_job_id"],
                row["reservation_id"],
                row["route_class"],
                row["provider_id"],
                row["model_id"],
                row["fallback_index"],
                reconciliation_status,
                row["projected_input_tokens"],
                row["projected_output_tokens"],
                row["projected_cost_upper_usd"],
                input_tokens,
                output_tokens,
                actual_cost_usd,
                now_iso,
            ),
        )
        connection.execute(
            """
            UPDATE ai_jobs
            SET status = CASE
                    WHEN status = 'queued' THEN 'provider_error'
                    ELSE status
                END,
                input_tokens = ?, output_tokens = ?, cost_estimate = ?,
                usage_source = ?, error_type = CASE
                    WHEN status = 'queued' THEN 'EgressInFlightTimeout'
                    ELSE error_type
                END
            WHERE id = ?
            """,
            (
                input_tokens,
                output_tokens,
                actual_cost_usd,
                usage_source,
                row["ai_job_id"],
            ),
        )
        updated = connection.execute(
            """
            UPDATE egress_budget_reservations
            SET state = 'reconciled', version = version + 1,
                reconciled_at = ?, egress_attempt_id = ?,
                actual_input_tokens = ?, actual_output_tokens = ?,
                actual_cost_usd = ?, reconciliation_status = ?
            WHERE id = ? AND state = 'in_flight' AND version = ?
            """,
            (
                now_iso,
                attempt_id,
                input_tokens,
                output_tokens,
                actual_cost_usd,
                reconciliation_status,
                row["reservation_id"],
                int(row["reservation_version"]),
            ),
        )
        if updated.rowcount != 1:
            raise EgressStateError("stale in-flight reservation CAS conflict")


def _expire_stale_active_reservations(
    connection: sqlite3.Connection, *, now_iso: str
) -> None:
    _reconcile_stale_in_flight_reservations(connection, now_iso=now_iso)
    connection.execute(
        """
        UPDATE egress_budget_reservations
        SET state = 'expired', version = version + 1,
            reconciled_at = ?, reconciliation_status = 'expired_before_start'
        WHERE state = 'active' AND expires_at <= ?
        """,
        (now_iso, now_iso),
    )


def _preparation(
    *,
    decision_id: str,
    packet_id: str,
    material: EgressPacketMaterial,
    projection: EgressPacketProjection,
    result: str,
    reason_code: str,
    trigger_ids: tuple[str, ...],
    ticket_id: str | None,
    reservation_id: str | None,
) -> EgressPreparation:
    return EgressPreparation(
        decision_id=decision_id,
        result=result,
        reason_code=reason_code,
        packet_id=packet_id,
        packet_digest=projection.packet_digest,
        ticket_id=ticket_id,
        reservation_id=reservation_id,
        trigger_ids=trigger_ids,
        confirmation_required=ticket_id is not None,
        projected_input_tokens=projection.projected_input_tokens,
        projected_output_tokens=projection.projected_output_tokens,
        projected_cost_upper_usd=projection.projected_cost_upper_usd,
        provider_id=material.provider_id,
        model_id=material.model_id,
        fallback_index=material.fallback_index,
        policy_version=projection.policy_version,
        trigger_version=projection.trigger_version,
        config_digest=projection.config_digest,
    )


def _normalized_now(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None:
        raise EgressContractError("now must include timezone information")
    return result.astimezone(UTC)
