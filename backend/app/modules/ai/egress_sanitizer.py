from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.ai import sensitivity
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.egress_policy import EgressPolicyConfig, load_default_egress_policy
from app.modules.ai.egress_service import (
    EgressContractError,
    canonical_json,
    sanitizer_sample_value,
    sanitizer_should_sample,
    sha256_text,
)
from app.modules.events.service import log_event

_ALLOWED_FINAL_LEVELS = frozenset({"S0", "S1"})
_ALLOWED_SANITIZER_KINDS = frozenset({"deterministic", "model_local"})
_ALLOWED_AUDIT_DISPOSITIONS = frozenset({"accepted", "rejected"})


@dataclass(frozen=True)
class PromptDerivative:
    id: str
    workspace_id: str | None
    raw_prompt_digest: str
    derivative_digest: str
    final_level: str
    transformations: tuple[str, ...]
    sanitizer_kind: str
    sanitizer_version: str
    sanitizer_config_digest: str
    sanitizer_ai_job_id: str | None
    policy_version: str
    status: str
    created_at: str
    revoked_at: str | None
    revocation_reason: str | None
    derivative_content: str = field(repr=False)


@dataclass(frozen=True)
class SanitizerApproval:
    derivative_kind: str
    derivative_id: str
    derivative_digest: str
    workspace_id: str | None
    final_level: str
    sanitizer_kind: str
    sanitizer_version: str
    sanitizer_config_digest: str
    sanitizer_ai_job_id: str | None
    policy_version: str
    audit_item_id: str | None
    reused: bool


@dataclass(frozen=True)
class SanitizerAuditDisposition:
    audit_item_id: str
    derivative_kind: str
    derivative_id: str
    derivative_digest: str
    state: str
    invalidated_packet_count: int
    revoked_ticket_count: int
    released_reservation_count: int


def create_prompt_derivative(
    *,
    raw_prompt: str,
    derivative_content: str,
    final_level: str,
    transformations: list[str] | tuple[str, ...],
    sanitizer_kind: str,
    sanitizer_version: str,
    sanitizer_config_digest: str,
    sanitizer_ai_job_id: str | None = None,
    workspace_id: str | None = None,
    policy: EgressPolicyConfig | None = None,
    now: datetime | None = None,
) -> SanitizerApproval:
    """Persist one immutable, auto-approved S0/S1 prompt derivative.

    Prompt and derivative digests bind the exact received strings. Validation may
    reject blank or oversized strings but never trims or rewrites them.
    """

    policy = policy or load_default_egress_policy()
    now_dt = _normalized_now(now)
    raw_prompt = _bounded_exact_text(
        raw_prompt,
        "raw_prompt",
        policy.max_prompt_chars,
    )
    derivative_content = _bounded_exact_text(
        derivative_content,
        "derivative_content",
        policy.max_prompt_chars,
    )
    _validate_final_content(derivative_content, final_level=final_level)
    if sensitivity.deterministic_floor(raw_prompt) == "S4":
        raise sensitivity.SensitivityPolicyError(
            "Raw prompt contains a deterministic secret marker and cannot be sanitized."
        )

    transformations_tuple = _transformations(transformations)
    sanitizer_version = _required_text(sanitizer_version, "sanitizer_version")
    sanitizer_config_digest = _validate_config_digest(sanitizer_config_digest)
    if sanitizer_ai_job_id is not None:
        sanitizer_ai_job_id = _required_text(
            sanitizer_ai_job_id,
            "sanitizer_ai_job_id",
        )
    _validate_sanitizer_kind(sanitizer_kind)
    if workspace_id is not None:
        workspace_id = _required_text(workspace_id, "workspace_id")

    raw_digest = sha256_text(raw_prompt)
    derivative_digest = sha256_text(derivative_content)

    with _transaction() as connection:
        _require_workspace_if_present(connection, workspace_id)
        _validate_sanitizer_ai_job(
            connection,
            sanitizer_kind=sanitizer_kind,
            sanitizer_ai_job_id=sanitizer_ai_job_id,
            expected_output_digest=canonical_digest({"text": derivative_content}),
        )
        row = connection.execute(
            """
            SELECT *
            FROM egress_prompt_derivatives
            WHERE workspace_id IS ?
              AND raw_prompt_digest = ?
              AND derivative_digest = ?
              AND policy_version = ?
            """,
            (
                workspace_id,
                raw_digest,
                derivative_digest,
                policy.policy_version,
            ),
        ).fetchone()
        if row is not None:
            _assert_prompt_derivative_match(
                row,
                derivative_content=derivative_content,
                final_level=final_level,
                transformations=transformations_tuple,
                sanitizer_kind=sanitizer_kind,
                sanitizer_version=sanitizer_version,
                sanitizer_config_digest=sanitizer_config_digest,
                sanitizer_ai_job_id=sanitizer_ai_job_id,
            )
            if row["status"] != "approved":
                raise sensitivity.SensitivityPolicyError(
                    "A revoked prompt derivative cannot be reused."
                )
            audit_item_id = _ensure_audit_item(
                connection,
                derivative_kind="prompt",
                derivative_id=str(row["id"]),
                derivative_digest=derivative_digest,
                workspace_id=workspace_id,
                policy=policy,
                now_dt=now_dt,
            )
            return _approval(
                derivative_kind="prompt",
                derivative_id=str(row["id"]),
                derivative_digest=derivative_digest,
                workspace_id=workspace_id,
                final_level=final_level,
                sanitizer_kind=sanitizer_kind,
                sanitizer_version=sanitizer_version,
                sanitizer_config_digest=sanitizer_config_digest,
                sanitizer_ai_job_id=sanitizer_ai_job_id,
                policy=policy,
                audit_item_id=audit_item_id,
                reused=True,
            )

        derivative_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO egress_prompt_derivatives (
                id, workspace_id, raw_prompt_digest, derivative_content,
                derivative_digest, final_level, transformations_json,
                sanitizer_kind, sanitizer_version, sanitizer_config_digest,
                sanitizer_ai_job_id, policy_version, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved', ?)
            """,
            (
                derivative_id,
                workspace_id,
                raw_digest,
                derivative_content,
                derivative_digest,
                final_level,
                canonical_json(list(transformations_tuple)),
                sanitizer_kind,
                sanitizer_version,
                sanitizer_config_digest,
                sanitizer_ai_job_id,
                policy.policy_version,
                now_dt.isoformat(),
            ),
        )
        audit_item_id = _ensure_audit_item(
            connection,
            derivative_kind="prompt",
            derivative_id=derivative_id,
            derivative_digest=derivative_digest,
            workspace_id=workspace_id,
            policy=policy,
            now_dt=now_dt,
        )
        log_event(
            connection,
            event_type="EgressPromptDerivativeApproved",
            actor="deterministic-policy",
            target_type="EgressPromptDerivative",
            target_id=derivative_id,
            workspace_id=workspace_id,
            payload={
                "raw_prompt_digest": raw_digest,
                "derivative_digest": derivative_digest,
                "final_level": final_level,
                "sanitizer_kind": sanitizer_kind,
                "sanitizer_version": sanitizer_version,
                "sanitizer_config_digest": sanitizer_config_digest,
                "sanitizer_ai_job_id": sanitizer_ai_job_id,
                "policy_version": policy.policy_version,
                "audit_item_id": audit_item_id,
            },
        )
        return _approval(
            derivative_kind="prompt",
            derivative_id=derivative_id,
            derivative_digest=derivative_digest,
            workspace_id=workspace_id,
            final_level=final_level,
            sanitizer_kind=sanitizer_kind,
            sanitizer_version=sanitizer_version,
            sanitizer_config_digest=sanitizer_config_digest,
            sanitizer_ai_job_id=sanitizer_ai_job_id,
            policy=policy,
            audit_item_id=audit_item_id,
            reused=False,
        )


def auto_approve_canonical_derivative(
    *,
    workspace_id: str,
    source_refs: list[str] | tuple[str, ...],
    derivative_content: str,
    final_level: str,
    transformations: list[str] | tuple[str, ...],
    sanitizer_kind: str,
    sanitizer_version: str,
    sanitizer_config_digest: str,
    sanitizer_ai_job_id: str | None = None,
    expected_source_digests: dict[str, str] | None = None,
    expected_source_levels: dict[str, str] | None = None,
    approval_source: str = "policy-sanitizer-v1",
    policy: EgressPolicyConfig | None = None,
    now: datetime | None = None,
) -> SanitizerApproval:
    """Auto-approve one provenance-bound canonical derivative atomically."""

    policy = policy or load_default_egress_policy()
    now_dt = _normalized_now(now)
    workspace_id = _required_text(workspace_id, "workspace_id")
    derivative_content = _bounded_exact_text(
        derivative_content,
        "derivative_content",
        policy.max_context_chars,
    )
    _validate_final_content(derivative_content, final_level=final_level)
    source_refs_tuple = _source_refs(source_refs)
    transformations_tuple = _transformations(transformations)
    sanitizer_version = _required_text(sanitizer_version, "sanitizer_version")
    sanitizer_config_digest = _validate_config_digest(sanitizer_config_digest)
    approval_source = _required_text(approval_source, "approval_source")
    if sanitizer_ai_job_id is not None:
        sanitizer_ai_job_id = _required_text(
            sanitizer_ai_job_id,
            "sanitizer_ai_job_id",
        )
    _validate_sanitizer_kind(sanitizer_kind)
    expected_source_digests = _validate_expected_source_digests(
        expected_source_digests,
        source_refs=source_refs_tuple,
        required=sanitizer_kind == "model_local",
    )
    expected_source_levels = _validate_expected_source_levels(
        expected_source_levels,
        source_refs=source_refs_tuple,
    )

    derivative_digest = sensitivity._derivative_content_digest(derivative_content)
    source_refs_json = canonical_json(list(source_refs_tuple))
    transformations_json = canonical_json(list(transformations_tuple))

    with _transaction() as connection:
        sensitivity._require_workspace(connection, workspace_id)
        _validate_sanitizer_ai_job(
            connection,
            sanitizer_kind=sanitizer_kind,
            sanitizer_ai_job_id=sanitizer_ai_job_id,
            expected_output_digest=canonical_digest({"text": derivative_content}),
        )
        resolved_sources = [
            sensitivity._resolve_source_snapshot_and_label_in_connection(
                connection,
                workspace_id,
                source_ref,
            )
            for source_ref in source_refs_tuple
        ]
        source_digests = {
            snapshot.subject_ref: snapshot.content_digest
            for snapshot, _label in resolved_sources
        }
        if expected_source_digests is not None and source_digests != expected_source_digests:
            raise sensitivity.SensitivityPolicyError(
                "Canonical sanitizer source snapshot changed before approval."
            )
        source_levels = {
            snapshot.subject_ref: sensitivity._effective_level_for_bound_snapshot(
                snapshot,
                label,
            )
            for snapshot, label in resolved_sources
        }
        if expected_source_levels is not None and source_levels != expected_source_levels:
            raise sensitivity.SensitivityPolicyError(
                "Canonical sanitizer source level changed before approval."
            )
        source_digests_json = canonical_json(source_digests)

        row = connection.execute(
            """
            SELECT *
            FROM sanitized_derivatives
            WHERE workspace_id = ?
              AND source_refs_json = ?
              AND source_digests_json = ?
              AND content_digest = ?
              AND effective_level = ?
              AND transformations_json = ?
              AND sanitizer_kind = ?
              AND sanitizer_version = ?
              AND sanitizer_config_digest = ?
              AND approval_source = ?
              AND auto_approved = 1
            ORDER BY created_at DESC, id ASC
            LIMIT 1
            """,
            (
                workspace_id,
                source_refs_json,
                source_digests_json,
                derivative_digest,
                final_level,
                transformations_json,
                sanitizer_kind,
                sanitizer_version,
                sanitizer_config_digest,
                approval_source,
            ),
        ).fetchone()
        if row is not None:
            if row["status"] != "approved":
                raise sensitivity.SensitivityPolicyError(
                    "A rejected or stale auto-sanitized derivative cannot be reused."
                )
            if row["content"] != derivative_content:
                raise sensitivity.SensitivityPolicyError(
                    "Canonical derivative digest collision or immutable content mismatch."
                )
            if row["sanitizer_ai_job_id"] != sanitizer_ai_job_id:
                raise sensitivity.SensitivityPolicyError(
                    "Canonical derivative sanitizer ai_job mismatch."
                )
            derivative_id = str(row["id"])
            audit_item_id = _ensure_audit_item(
                connection,
                derivative_kind="canonical",
                derivative_id=derivative_id,
                derivative_digest=derivative_digest,
                workspace_id=workspace_id,
                policy=policy,
                now_dt=now_dt,
            )
            return _approval(
                derivative_kind="canonical",
                derivative_id=derivative_id,
                derivative_digest=derivative_digest,
                workspace_id=workspace_id,
                final_level=final_level,
                sanitizer_kind=sanitizer_kind,
                sanitizer_version=sanitizer_version,
                sanitizer_config_digest=sanitizer_config_digest,
                sanitizer_ai_job_id=sanitizer_ai_job_id,
                policy=policy,
                audit_item_id=audit_item_id,
                reused=True,
            )

        derivative_id = str(uuid4())
        now_iso = now_dt.isoformat()
        connection.execute(
            """
            INSERT INTO sanitized_derivatives (
                id, workspace_id, source_refs_json, source_digests_json,
                content, content_digest, effective_level, transformations_json,
                policy_version, status, actor, reviewer, reviewed_at,
                stale_reason, created_at, updated_at, sanitizer_kind,
                sanitizer_version, sanitizer_config_digest, sanitizer_ai_job_id,
                approval_source, auto_approved
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved',
                      'deterministic-policy', ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                derivative_id,
                workspace_id,
                source_refs_json,
                source_digests_json,
                derivative_content,
                derivative_digest,
                final_level,
                transformations_json,
                sensitivity.POLICY_VERSION,
                approval_source,
                now_iso,
                now_iso,
                now_iso,
                sanitizer_kind,
                sanitizer_version,
                sanitizer_config_digest,
                sanitizer_ai_job_id,
                approval_source,
            ),
        )
        audit_item_id = _ensure_audit_item(
            connection,
            derivative_kind="canonical",
            derivative_id=derivative_id,
            derivative_digest=derivative_digest,
            workspace_id=workspace_id,
            policy=policy,
            now_dt=now_dt,
        )
        log_event(
            connection,
            event_type="SanitizedDerivativeAutoApproved",
            actor="deterministic-policy",
            target_type="SanitizedDerivative",
            target_id=derivative_id,
            workspace_id=workspace_id,
            payload={
                "source_refs": list(source_refs_tuple),
                "source_digests": source_digests,
                "content_digest": derivative_digest,
                "effective_level": final_level,
                "sanitizer_kind": sanitizer_kind,
                "sanitizer_version": sanitizer_version,
                "sanitizer_config_digest": sanitizer_config_digest,
                "sanitizer_ai_job_id": sanitizer_ai_job_id,
                "approval_source": approval_source,
                "sensitivity_policy_version": sensitivity.POLICY_VERSION,
                "egress_policy_version": policy.policy_version,
                "audit_item_id": audit_item_id,
            },
        )
        return _approval(
            derivative_kind="canonical",
            derivative_id=derivative_id,
            derivative_digest=derivative_digest,
            workspace_id=workspace_id,
            final_level=final_level,
            sanitizer_kind=sanitizer_kind,
            sanitizer_version=sanitizer_version,
            sanitizer_config_digest=sanitizer_config_digest,
            sanitizer_ai_job_id=sanitizer_ai_job_id,
            policy=policy,
            audit_item_id=audit_item_id,
            reused=False,
        )


def get_prompt_derivative(
    derivative_id: str,
    *,
    workspace_id: str | None = None,
) -> PromptDerivative:
    derivative_id = _required_text(derivative_id, "derivative_id")
    if workspace_id is not None:
        workspace_id = _required_text(workspace_id, "workspace_id")
    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM egress_prompt_derivatives
            WHERE id = ? AND workspace_id IS ?
            """,
            (derivative_id, workspace_id),
        ).fetchone()
    if row is None:
        raise sensitivity.SensitivityNotFoundError(
            f"Prompt derivative not found: {derivative_id}"
        )
    return _prompt_derivative_read(row)


def resolve_approved_prompt_derivative(
    *,
    raw_prompt: str,
    workspace_id: str | None = None,
    policy: EgressPolicyConfig | None = None,
) -> PromptDerivative | None:
    policy = policy or load_default_egress_policy()
    raw_prompt = _bounded_exact_text(
        raw_prompt,
        "raw_prompt",
        policy.max_prompt_chars,
    )
    if workspace_id is not None:
        workspace_id = _required_text(workspace_id, "workspace_id")
    raw_digest = sha256_text(raw_prompt)
    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM egress_prompt_derivatives
            WHERE workspace_id IS ?
              AND raw_prompt_digest = ?
              AND policy_version = ?
              AND status = 'approved'
            ORDER BY created_at DESC, id ASC
            LIMIT 1
            """,
            (workspace_id, raw_digest, policy.policy_version),
        ).fetchone()
    if row is None:
        return None
    derivative = _prompt_derivative_read(row)
    _validate_final_content(
        derivative.derivative_content,
        final_level=derivative.final_level,
    )
    if sha256_text(derivative.derivative_content) != derivative.derivative_digest:
        raise sensitivity.SensitivityPolicyError(
            "Prompt derivative content digest mismatch."
        )
    return derivative


def review_sanitizer_audit_item(
    audit_item_id: str,
    *,
    disposition: str,
    reviewer: str = "local-user",
    notes: str | None = None,
    now: datetime | None = None,
) -> SanitizerAuditDisposition:
    """Review one sampled derivative and invalidate dependent unstarted work."""

    audit_item_id = _required_text(audit_item_id, "audit_item_id")
    reviewer = _required_text(reviewer, "reviewer")
    if disposition not in _ALLOWED_AUDIT_DISPOSITIONS:
        raise EgressContractError("unsupported sanitizer audit disposition")
    if notes is not None:
        if not isinstance(notes, str):
            raise EgressContractError("audit notes must be text")
        if len(notes) > 2000:
            raise EgressContractError("audit notes exceed 2000 characters")
    now_dt = _normalized_now(now)
    now_iso = now_dt.isoformat()

    with _transaction() as connection:
        audit = connection.execute(
            "SELECT * FROM sanitizer_audit_items WHERE id = ?",
            (audit_item_id,),
        ).fetchone()
        if audit is None:
            raise sensitivity.SensitivityNotFoundError(
                f"Sanitizer audit item not found: {audit_item_id}"
            )
        if audit["state"] != "pending":
            raise sensitivity.SensitivityPolicyError(
                f"Sanitizer audit item is not pending: {audit['state']}"
            )

        packet_ids: set[str] = set()
        revoked_ticket_count = 0
        released_reservation_count = 0
        if disposition == "rejected":
            if audit["derivative_kind"] == "prompt":
                changed = connection.execute(
                    """
                    UPDATE egress_prompt_derivatives
                    SET status = 'revoked', revoked_at = ?,
                        revocation_reason = 'sanitizer_audit_rejected'
                    WHERE id = ? AND derivative_digest = ? AND status = 'approved'
                    """,
                    (
                        now_iso,
                        audit["derivative_id"],
                        audit["derivative_digest"],
                    ),
                )
                if changed.rowcount != 1:
                    raise sensitivity.SensitivityPolicyError(
                        "Prompt derivative changed before audit rejection."
                    )
                packet_ids = {
                    str(row["id"])
                    for row in connection.execute(
                        "SELECT id FROM egress_packets WHERE prompt_derivative_id = ?",
                        (audit["derivative_id"],),
                    ).fetchall()
                }
            elif audit["derivative_kind"] == "canonical":
                changed = connection.execute(
                    """
                    UPDATE sanitized_derivatives
                    SET status = 'revoked',
                        stale_reason = 'sanitizer_audit_rejected',
                        updated_at = ?
                    WHERE id = ? AND content_digest = ?
                      AND status = 'approved' AND auto_approved = 1
                    """,
                    (
                        now_iso,
                        audit["derivative_id"],
                        audit["derivative_digest"],
                    ),
                )
                if changed.rowcount != 1:
                    raise sensitivity.SensitivityPolicyError(
                        "Canonical derivative changed before audit rejection."
                    )
                packet_ids = _canonical_derivative_packet_ids(
                    connection,
                    derivative_id=str(audit["derivative_id"]),
                )
            else:
                raise sensitivity.SensitivityPolicyError(
                    "Unsupported derivative kind in audit row."
                )

            if packet_ids:
                placeholders = ",".join("?" for _ in packet_ids)
                values = tuple(sorted(packet_ids))
                tickets = connection.execute(
                    f"""
                    UPDATE egress_confirmation_tickets
                    SET state = 'revoked', version = version + 1,
                        revoked_at = ?,
                        revocation_reason = 'sanitizer_audit_rejected'
                    WHERE state = 'pending'
                      AND packet_id IN ({placeholders})
                    """,
                    (now_iso, *values),
                )
                revoked_ticket_count = tickets.rowcount
                reservations = connection.execute(
                    f"""
                    UPDATE egress_budget_reservations
                    SET state = 'released', version = version + 1,
                        reconciled_at = ?,
                        reconciliation_status =
                            'sanitizer_audit_rejected_before_start'
                    WHERE state = 'active'
                      AND decision_id IN (
                          SELECT id FROM egress_decisions
                          WHERE packet_id IN ({placeholders})
                      )
                    """,
                    (now_iso, *values),
                )
                released_reservation_count = reservations.rowcount

        changed = connection.execute(
            """
            UPDATE sanitizer_audit_items
            SET state = ?, reviewed_at = ?, reviewer = ?, notes = ?
            WHERE id = ? AND state = 'pending'
            """,
            (disposition, now_iso, reviewer, notes, audit_item_id),
        )
        if changed.rowcount != 1:
            raise sensitivity.SensitivityPolicyError(
                "Sanitizer audit item changed before review."
            )
        log_event(
            connection,
            event_type="SanitizerAuditReviewed",
            actor=reviewer,
            target_type="SanitizerAuditItem",
            target_id=audit_item_id,
            workspace_id=audit["workspace_id"],
            payload={
                "derivative_kind": audit["derivative_kind"],
                "derivative_id": audit["derivative_id"],
                "derivative_digest": audit["derivative_digest"],
                "disposition": disposition,
                "notes_present": notes is not None,
                "notes_digest": sha256_text(notes) if notes is not None else None,
                "invalidated_packet_count": len(packet_ids),
                "revoked_ticket_count": revoked_ticket_count,
                "released_reservation_count": released_reservation_count,
            },
        )
        return SanitizerAuditDisposition(
            audit_item_id=audit_item_id,
            derivative_kind=str(audit["derivative_kind"]),
            derivative_id=str(audit["derivative_id"]),
            derivative_digest=str(audit["derivative_digest"]),
            state=disposition,
            invalidated_packet_count=len(packet_ids),
            revoked_ticket_count=revoked_ticket_count,
            released_reservation_count=released_reservation_count,
        )


def _approval(
    *,
    derivative_kind: str,
    derivative_id: str,
    derivative_digest: str,
    workspace_id: str | None,
    final_level: str,
    sanitizer_kind: str,
    sanitizer_version: str,
    sanitizer_config_digest: str,
    sanitizer_ai_job_id: str | None,
    policy: EgressPolicyConfig,
    audit_item_id: str | None,
    reused: bool,
) -> SanitizerApproval:
    return SanitizerApproval(
        derivative_kind=derivative_kind,
        derivative_id=derivative_id,
        derivative_digest=derivative_digest,
        workspace_id=workspace_id,
        final_level=final_level,
        sanitizer_kind=sanitizer_kind,
        sanitizer_version=sanitizer_version,
        sanitizer_config_digest=sanitizer_config_digest,
        sanitizer_ai_job_id=sanitizer_ai_job_id,
        policy_version=policy.policy_version,
        audit_item_id=audit_item_id,
        reused=reused,
    )


def _ensure_audit_item(
    connection: sqlite3.Connection,
    *,
    derivative_kind: str,
    derivative_id: str,
    derivative_digest: str,
    workspace_id: str | None,
    policy: EgressPolicyConfig,
    now_dt: datetime,
) -> str | None:
    iso_calendar = now_dt.isocalendar()
    iso_week = f"{iso_calendar.year:04d}-W{iso_calendar.week:02d}"
    if not sanitizer_should_sample(
        derivative_kind=derivative_kind,
        derivative_id=derivative_id,
        derivative_digest=derivative_digest,
        iso_week=iso_week,
        policy_version=policy.policy_version,
        sample_rate_bps=policy.sample_rate_bps,
    ):
        return None
    selection_value = sanitizer_sample_value(
        derivative_kind=derivative_kind,
        derivative_id=derivative_id,
        derivative_digest=derivative_digest,
        iso_week=iso_week,
        policy_version=policy.policy_version,
    )
    row = connection.execute(
        """
        SELECT id
        FROM sanitizer_audit_items
        WHERE derivative_kind = ?
          AND derivative_id = ?
          AND derivative_digest = ?
          AND iso_week = ?
          AND policy_version = ?
        """,
        (
            derivative_kind,
            derivative_id,
            derivative_digest,
            iso_week,
            policy.policy_version,
        ),
    ).fetchone()
    if row is not None:
        return str(row["id"])
    audit_item_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO sanitizer_audit_items (
            id, workspace_id, derivative_kind, derivative_id,
            derivative_digest, iso_week, policy_version,
            selection_value, sample_rate_bps, state, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            audit_item_id,
            workspace_id,
            derivative_kind,
            derivative_id,
            derivative_digest,
            iso_week,
            policy.policy_version,
            selection_value,
            policy.sample_rate_bps,
            now_dt.isoformat(),
        ),
    )
    return audit_item_id


def _canonical_derivative_packet_ids(
    connection: sqlite3.Connection,
    *,
    derivative_id: str,
) -> set[str]:
    packet_ids: set[str] = set()
    rows = connection.execute(
        "SELECT id, included_manifest_json FROM egress_packets"
    ).fetchall()
    for row in rows:
        try:
            manifests = json.loads(row["included_manifest_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise sensitivity.SensitivityPolicyError(
                "Stored egress manifest is malformed."
            ) from exc
        if not isinstance(manifests, list):
            raise sensitivity.SensitivityPolicyError(
                "Stored egress manifest is not a list."
            )
        if any(
            isinstance(item, dict) and item.get("derivative_id") == derivative_id
            for item in manifests
        ):
            packet_ids.add(str(row["id"]))
    return packet_ids


def _validate_sanitizer_ai_job(
    connection: sqlite3.Connection,
    *,
    sanitizer_kind: str,
    sanitizer_ai_job_id: str | None,
    expected_output_digest: str,
) -> None:
    if sanitizer_kind == "deterministic":
        if sanitizer_ai_job_id is not None:
            raise sensitivity.SensitivityPolicyError(
                "Deterministic sanitizer must not claim an ai_job."
            )
        return
    if sanitizer_ai_job_id is None:
        raise sensitivity.SensitivityPolicyError(
            "Model-backed sanitizer requires an ai_job."
        )
    row = connection.execute(
        """
        SELECT status, task_kind, selected_route_class, output_digest
        FROM ai_jobs
        WHERE id = ?
        """,
        (sanitizer_ai_job_id,),
    ).fetchone()
    if row is None:
        raise sensitivity.SensitivityPolicyError("Sanitizer ai_job was not found.")
    route_class = str(row["selected_route_class"] or "")
    if (
        row["status"] != "success"
        or row["task_kind"] != "sanitizer"
        or not route_class.startswith("local:")
        or row["output_digest"] != expected_output_digest
    ):
        raise sensitivity.SensitivityPolicyError(
            "Sanitizer ai_job must be a successful dedicated local-route attempt "
            "with matching output provenance."
        )


def _validate_expected_source_digests(
    values: dict[str, str] | None,
    *,
    source_refs: tuple[str, ...],
    required: bool,
) -> dict[str, str] | None:
    if values is None:
        if required:
            raise sensitivity.SensitivityPolicyError(
                "Model-backed canonical sanitizer requires pre-call source digests."
            )
        return None
    if not isinstance(values, dict):
        raise EgressContractError("expected_source_digests must be a mapping")
    cleaned: dict[str, str] = {}
    for raw_ref, raw_digest in values.items():
        source_ref = _required_text(raw_ref, "expected_source_digest source_ref")
        sensitivity._parse_subject_ref(source_ref)
        digest = _required_text(raw_digest, "expected_source_digest")
        if (
            len(digest) != 71
            or not digest.startswith("sha256:")
            or any(char not in "0123456789abcdef" for char in digest[7:])
        ):
            raise EgressContractError(
                "expected source digests must use canonical sha256:<64 lowercase hex>"
            )
        if source_ref in cleaned:
            raise EgressContractError("expected_source_digests contains duplicates")
        cleaned[source_ref] = digest
    if set(cleaned) != set(source_refs):
        raise sensitivity.SensitivityPolicyError(
            "Expected canonical source digest set does not match source_refs."
        )
    return dict(sorted(cleaned.items()))


def _validate_expected_source_levels(
    values: dict[str, str] | None,
    *,
    source_refs: tuple[str, ...],
) -> dict[str, str] | None:
    if values is None:
        return None
    if not isinstance(values, dict):
        raise EgressContractError("expected_source_levels must be a mapping")
    allowed_levels = {"S0", "S1", "S2", "S3", "S4", "unknown"}
    cleaned: dict[str, str] = {}
    for raw_ref, raw_level in values.items():
        source_ref = _required_text(raw_ref, "expected_source_level source_ref")
        sensitivity._parse_subject_ref(source_ref)
        level = _required_text(raw_level, "expected_source_level")
        if level not in allowed_levels:
            raise EgressContractError("expected_source_levels contains an invalid level")
        if source_ref in cleaned:
            raise EgressContractError("expected_source_levels contains duplicates")
        cleaned[source_ref] = level
    if set(cleaned) != set(source_refs):
        raise sensitivity.SensitivityPolicyError(
            "Expected canonical source level set does not match source_refs."
        )
    return dict(sorted(cleaned.items()))


def _validate_final_content(content: str, *, final_level: str) -> None:
    if final_level not in _ALLOWED_FINAL_LEVELS:
        raise sensitivity.SensitivityPolicyError(
            "Auto-approved sanitizer output must be effective S0 or S1."
        )
    floor = sensitivity.deterministic_floor(content)
    if floor is not None:
        raise sensitivity.SensitivityPolicyError(
            f"Sanitizer output remains external-ineligible at deterministic floor {floor}."
        )


def _validate_sanitizer_kind(sanitizer_kind: str) -> None:
    if sanitizer_kind not in _ALLOWED_SANITIZER_KINDS:
        raise EgressContractError("unsupported sanitizer_kind")


def _validate_config_digest(value: str) -> str:
    value = _required_text(value, "sanitizer_config_digest")
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise EgressContractError(
            "sanitizer_config_digest must be a lowercase SHA-256 digest"
        )
    return value


def _assert_prompt_derivative_match(
    row: sqlite3.Row,
    *,
    derivative_content: str,
    final_level: str,
    transformations: tuple[str, ...],
    sanitizer_kind: str,
    sanitizer_version: str,
    sanitizer_config_digest: str,
    sanitizer_ai_job_id: str | None,
) -> None:
    expected = {
        "derivative_content": derivative_content,
        "final_level": final_level,
        "transformations_json": canonical_json(list(transformations)),
        "sanitizer_kind": sanitizer_kind,
        "sanitizer_version": sanitizer_version,
        "sanitizer_config_digest": sanitizer_config_digest,
        "sanitizer_ai_job_id": sanitizer_ai_job_id,
    }
    if any(row[key] != value for key, value in expected.items()):
        raise sensitivity.SensitivityPolicyError(
            "Prompt derivative digest collision or immutable provenance mismatch."
        )


def _prompt_derivative_read(row: sqlite3.Row) -> PromptDerivative:
    return PromptDerivative(
        id=str(row["id"]),
        workspace_id=row["workspace_id"],
        raw_prompt_digest=str(row["raw_prompt_digest"]),
        derivative_digest=str(row["derivative_digest"]),
        final_level=str(row["final_level"]),
        transformations=tuple(json.loads(row["transformations_json"])),
        sanitizer_kind=str(row["sanitizer_kind"]),
        sanitizer_version=str(row["sanitizer_version"]),
        sanitizer_config_digest=str(row["sanitizer_config_digest"]),
        sanitizer_ai_job_id=row["sanitizer_ai_job_id"],
        policy_version=str(row["policy_version"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        revoked_at=row["revoked_at"],
        revocation_reason=row["revocation_reason"],
        derivative_content=str(row["derivative_content"]),
    )


def _source_refs(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise EgressContractError("source_refs must be a list or tuple")
    cleaned = tuple(_required_text(value, "source_ref") for value in values)
    if not cleaned:
        raise EgressContractError("source_refs must not be empty")
    if len(cleaned) > 20:
        raise EgressContractError("source_refs exceeds 20 entries")
    if len(set(cleaned)) != len(cleaned):
        raise EgressContractError("source_refs must not contain duplicates")
    for value in cleaned:
        sensitivity._parse_subject_ref(value)
    return tuple(sorted(cleaned))


def _transformations(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise EgressContractError("transformations must be a list or tuple")
    cleaned = tuple(_required_text(value, "transformation") for value in values)
    if not cleaned:
        raise EgressContractError("transformations must not be empty")
    if len(cleaned) > 50:
        raise EgressContractError("transformations exceeds 50 entries")
    return cleaned


def _bounded_exact_text(value: str, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EgressContractError(f"{field_name} must be non-empty text")
    if len(value) > maximum:
        raise EgressContractError(f"{field_name} exceeds configured character cap")
    return value


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EgressContractError(f"{field_name} must be non-empty text")
    return value.strip()


def _require_workspace_if_present(
    connection: sqlite3.Connection,
    workspace_id: str | None,
) -> None:
    if workspace_id is not None:
        sensitivity._require_workspace(connection, workspace_id)


def _normalized_now(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None:
        raise EgressContractError("now must include timezone information")
    return result.astimezone(UTC)


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
