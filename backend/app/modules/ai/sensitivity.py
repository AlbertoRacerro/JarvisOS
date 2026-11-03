"""Spec 059a sensitivity labels, reviewed derivatives, and context filtering.

This module is deliberately provider-free. It prepares external-eligible context
for the 059b execution boundary but cannot authorize or perform an egress call.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.core.packs import sensitivity_terms
from app.modules.ai.context_builder import (
    _CONTEXT_PACK_DROP_PRIORITY,
    CONTEXT_PACK_KINDS,
    MAX_CONTEXT_BLOCKS,
    ContextSelectionSpec,
    _block_for_record,
    _serialize_blocks,
    _statuses_for_selection,
    canonical_digest,
    canonicalize_blocks,
)
from app.modules.ai.sensitivity_models import (
    ALLOWED_SOURCE_KINDS,
    DERIVATIVE_SOURCE_KINDS,
    SanitizedDerivativeCreate,
    SanitizedDerivativeRead,
    SensitivityContextPreviewResponse,
    SensitivityLabelCreate,
    SensitivityLabelRead,
)
from app.modules.bluecad.evidence import EvidenceRecord, select_evidence_records
from app.modules.events.service import log_event, utc_now
from app.modules.modeling.models import AssumptionRead, DecisionRead, ParameterRead, RequirementRead
from app.modules.modeling.service import select_context_records

POLICY_VERSION = "ip-egress-v1"
_LEVEL_RANK = {"S0": 0, "S1": 1, "S2": 2, "S3": 3, "S4": 4}
_EXTERNAL_ELIGIBLE_LEVELS = {"S0", "S1"}
_SOURCE_TABLES = {
    "decision": "decisions",
    "assumption": "assumptions",
    "parameter": "parameters",
    "requirement": "requirements",
    "evidence": "evidence_records",
}
_SOURCE_MODELS = {
    "decision": DecisionRead,
    "assumption": AssumptionRead,
    "parameter": ParameterRead,
    "requirement": RequirementRead,
    "evidence": EvidenceRecord,
}
_PRIVATE_KEY_BOUNDARY = re.escape("-" * 5 + "BE" + "GIN ") + (
    r"(?:RSA |EC |OPENSSH )?"
) + re.escape("PRIVATE " + "KEY" + "-" * 5)
_SECRET_PATTERNS = (
    re.compile(_PRIVATE_KEY_BOUNDARY, re.I),
    re.compile(
        r"\b(?:a[p]i[_-]?k[e]y|passw[o]rd|passw[d]|client[_-]?s[e]cret)"
        r"\s*[:=]\s*\S+",
        re.I,
    ),
    re.compile(r"\bB[e]arer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\bs[k]-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\be[y]J[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)
_IP_PATTERNS = (re.compile(r"\b(?:trade secret|proprietary|patent-pending|unpublished design)\b", re.I),)


@lru_cache(maxsize=8)
def _pack_ip_patterns(terms: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """Project terms from loaded packs, next to an IP noun, are S3 (the egress guard for pack projects)."""
    if not terms:
        return ()
    names = "|".join(re.escape(term) for term in terms)
    return (
        re.compile(
            rf"\b(?:{names})\b.{{0,80}}\b(?:geometry|correlation|process parameter|design decision|IP)\b",
            re.I,
        ),
        re.compile(
            rf"\b(?:geometria|correlazione|parametr[oi] di processo|decisione progettuale)\b.{{0,80}}\b(?:{names})\b",
            re.I,
        ),
    )


def _ip_patterns() -> tuple[re.Pattern[str], ...]:
    return _IP_PATTERNS + _pack_ip_patterns(sensitivity_terms())


_CONFIDENTIAL_PATTERNS = (
    re.compile(r"\b(?:confidential|partner-only|under NDA|private project)\b", re.I),
    re.compile(r"\b(?:confidenziale|riservat[oa]|sotto NDA|progetto privato)\b", re.I),
)


class SensitivityPolicyError(ValueError):
    """A sensitivity or derivative contract was violated."""


class SensitivityNotFoundError(LookupError):
    """A workspace source, label, or derivative does not exist."""


@dataclass(frozen=True)
class SourceSnapshot:
    workspace_id: str
    subject_ref: str
    kind: str
    record_id: str
    block: dict[str, Any]
    content_digest: str


@dataclass(frozen=True)
class _CandidateBlock:
    block: dict[str, Any]
    manifest: dict[str, Any]
    drop_priority: int


def deterministic_floor(content: str) -> str | None:
    """Return only a hard lower bound; never a permission decision."""
    trigger = deterministic_floor_trigger(content)
    return trigger[0] if trigger is not None else None


def deterministic_floor_trigger(content: str) -> tuple[str, str] | None:
    """The floor level plus the first phrase that raised it, for operator explanation."""
    for level, patterns in (
        ("S4", _SECRET_PATTERNS),
        ("S3", _ip_patterns()),
        ("S2", _CONFIDENTIAL_PATTERNS),
    ):
        for pattern in patterns:
            match = pattern.search(content)
            if match is not None:
                return level, match.group(0)
    return None


def _is_external_eligible(level: str) -> bool:
    return level in _EXTERNAL_ELIGIBLE_LEVELS


def resolve_source_snapshot(workspace_id: str, subject_ref: str) -> SourceSnapshot:
    snapshot, _ = _resolve_source_snapshot_and_label(workspace_id, subject_ref)
    return snapshot


def create_sensitivity_label(payload: SensitivityLabelCreate) -> SensitivityLabelRead:
    kind, record_id = _parse_subject_ref(payload.subject_ref)
    label_id = str(uuid4())
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        now = utc_now()
        _require_workspace(connection, payload.workspace_id)
        row = connection.execute(
            f"SELECT * FROM {_SOURCE_TABLES[kind]} WHERE id = ? AND workspace_id = ?",
            (record_id, payload.workspace_id),
        ).fetchone()
        if row is None:
            raise SensitivityNotFoundError(f"Source not found: {payload.subject_ref}")
        model = _SOURCE_MODELS[kind].model_validate(dict(row))
        snapshot = _snapshot_from_block(
            payload.workspace_id,
            _block_for_record(kind, model),
        )
        prior = _latest_label_row_in_connection(
            connection,
            payload.workspace_id,
            snapshot.subject_ref,
        )
        if prior is not None and _LEVEL_RANK[prior["level"]] >= _LEVEL_RANK["S2"]:
            if _LEVEL_RANK[payload.level] < _LEVEL_RANK[prior["level"]]:
                raise SensitivityPolicyError(
                    "S2-S4 sources cannot be downgraded in place; "
                    "create a sanitized derivative."
                )

        floor = deterministic_floor(snapshot.block["content"])
        final_level = _max_level(payload.level, floor)
        classification_source = (
            "deterministic_floor"
            if floor is not None and _LEVEL_RANK[floor] > _LEVEL_RANK[payload.level]
            else "human"
        )
        connection.execute(
            """
            INSERT INTO sensitivity_labels (
                id, workspace_id, subject_ref, content_digest, level,
                classification_source, policy_version, actor, prior_label_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'local-user', ?, ?)
            """,
            (
                label_id,
                payload.workspace_id,
                snapshot.subject_ref,
                snapshot.content_digest,
                final_level,
                classification_source,
                POLICY_VERSION,
                None if prior is None else prior["id"],
                now,
            ),
        )
        log_event(
            connection,
            event_type="SensitivityLabelCreated",
            actor="local-user",
            target_type="SensitivityLabel",
            target_id=label_id,
            workspace_id=payload.workspace_id,
            payload={
                "subject_ref": snapshot.subject_ref,
                "content_digest": snapshot.content_digest,
                "level": final_level,
                "classification_source": classification_source,
                "policy_version": POLICY_VERSION,
            },
        )
        connection.commit()
        inserted = connection.execute(
            "SELECT * FROM sensitivity_labels WHERE id = ?",
            (label_id,),
        ).fetchone()
    if inserted is None:
        raise RuntimeError("Sensitivity label disappeared after creation")
    return _label_read(inserted, current=True, stale_reason=None)


def get_current_sensitivity_label(
    workspace_id: str,
    subject_ref: str,
) -> SensitivityLabelRead | None:
    _, label = _resolve_source_snapshot_and_label(workspace_id, subject_ref)
    return label


def create_sanitized_derivative(
    payload: SanitizedDerivativeCreate,
) -> SanitizedDerivativeRead:
    content_floor = deterministic_floor(payload.content)
    if content_floor == "S4":
        raise SensitivityPolicyError(
            "Sanitized content still contains a deterministic secret marker."
        )
    if (
        content_floor is not None
        and _LEVEL_RANK[content_floor] > _LEVEL_RANK[payload.effective_level]
    ):
        raise SensitivityPolicyError(
            f"Declared derivative level {payload.effective_level} "
            f"is below deterministic floor {content_floor}."
        )

    derivative_id = str(uuid4())
    content_digest = _derivative_content_digest(payload.content)
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        now = utc_now()
        _require_workspace(connection, payload.workspace_id)
        resolved_sources = [
            _resolve_source_snapshot_and_label_in_connection(
                connection,
                payload.workspace_id,
                ref,
            )
            for ref in payload.source_refs
        ]
        source_digests = {
            snapshot.subject_ref: snapshot.content_digest
            for snapshot, _ in resolved_sources
        }
        source_levels = [
            _effective_level_for_bound_snapshot(snapshot, label)
            for snapshot, label in resolved_sources
        ]
        if (
            "S4" in source_levels
            and _LEVEL_RANK[payload.effective_level] > _LEVEL_RANK["S1"]
        ):
            raise SensitivityPolicyError(
                "A derivative of an S4 source may be declared only S0 or S1."
            )

        connection.execute(
            """
            INSERT INTO sanitized_derivatives (
                id, workspace_id, source_refs_json, source_digests_json, content,
                content_digest, effective_level, transformations_json, policy_version,
                status, actor, reviewer, reviewed_at, stale_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 'local-user',
                      NULL, NULL, NULL, ?, ?)
            """,
            (
                derivative_id,
                payload.workspace_id,
                _json(sorted(source_digests)),
                _json(source_digests),
                payload.content,
                content_digest,
                payload.effective_level,
                _json(payload.transformations),
                POLICY_VERSION,
                now,
                now,
            ),
        )
        log_event(
            connection,
            event_type="SanitizedDerivativeDrafted",
            actor="local-user",
            target_type="SanitizedDerivative",
            target_id=derivative_id,
            workspace_id=payload.workspace_id,
            payload={
                "source_refs": sorted(source_digests),
                "source_digests": source_digests,
                "content_digest": content_digest,
                "effective_level": payload.effective_level,
                "policy_version": POLICY_VERSION,
            },
        )
        inserted = connection.execute(
            """
            SELECT * FROM sanitized_derivatives
            WHERE id = ? AND workspace_id = ?
            """,
            (derivative_id, payload.workspace_id),
        ).fetchone()
        connection.commit()
    if inserted is None:
        raise RuntimeError("Sanitized derivative disappeared after creation")
    return _derivative_read(inserted)


def approve_sanitized_derivative(
    workspace_id: str,
    derivative_id: str,
    *,
    reviewer_notes: str | None = None,
) -> SanitizedDerivativeRead:
    stale_error: str | None = None
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        now = utc_now()
        _require_workspace(connection, workspace_id)
        row = connection.execute(
            """
            SELECT * FROM sanitized_derivatives
            WHERE id = ? AND workspace_id = ?
            """,
            (derivative_id, workspace_id),
        ).fetchone()
        if row is None:
            raise SensitivityNotFoundError(
                f"Sanitized derivative not found: {derivative_id}"
            )
        derivative = _derivative_read(row)
        if derivative.status != "draft":
            raise SensitivityPolicyError("Only a draft derivative can be approved.")

        stale_reason = _derivative_stale_reason_in_connection(connection, derivative)
        if stale_reason is not None:
            _mark_derivative_stale_in_connection(
                connection,
                derivative,
                stale_reason,
                allowed_statuses={"draft", "approved"},
            )
            connection.commit()
            stale_error = stale_reason
        else:
            content_floor = deterministic_floor(derivative.content)
            if content_floor in {"S3", "S4"}:
                raise SensitivityPolicyError(
                    "Derivative content remains external-ineligible "
                    f"at deterministic floor {content_floor}."
                )
            if (
                content_floor is not None
                and _LEVEL_RANK[content_floor]
                > _LEVEL_RANK[derivative.effective_level]
            ):
                raise SensitivityPolicyError(
                    f"Derivative level {derivative.effective_level} "
                    f"is below deterministic floor {content_floor}."
                )

            cursor = connection.execute(
                """
                UPDATE sanitized_derivatives
                SET status = 'approved', reviewer = 'local-user', reviewed_at = ?,
                    stale_reason = NULL, updated_at = ?
                WHERE id = ? AND workspace_id = ? AND status = 'draft'
                """,
                (now, now, derivative_id, workspace_id),
            )
            if cursor.rowcount != 1:
                raise SensitivityPolicyError(
                    "Derivative state changed before approval."
                )
            log_event(
                connection,
                event_type="SanitizedDerivativeApproved",
                actor="local-user",
                target_type="SanitizedDerivative",
                target_id=derivative_id,
                workspace_id=workspace_id,
                payload={
                    "content_digest": derivative.content_digest,
                    "effective_level": derivative.effective_level,
                    "policy_version": POLICY_VERSION,
                    "reviewer_notes_present": bool(reviewer_notes),
                    "reviewer_notes_digest": (
                        canonical_digest({"reviewer_notes": reviewer_notes})
                        if reviewer_notes
                        else None
                    ),
                },
            )
            connection.commit()

    if stale_error is not None:
        raise SensitivityPolicyError(
            f"Derivative sources are stale: {stale_error}"
        )
    return get_sanitized_derivative(workspace_id, derivative_id)


def revoke_sanitized_derivative(
    workspace_id: str,
    derivative_id: str,
) -> SanitizedDerivativeRead:
    derivative = get_sanitized_derivative(workspace_id, derivative_id)
    if derivative.status not in {"draft", "approved"}:
        raise SensitivityPolicyError(
            "Only a draft or approved derivative can be revoked."
        )
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        now = utc_now()
        cursor = connection.execute(
            """
            UPDATE sanitized_derivatives
            SET status = 'revoked', stale_reason = 'revoked_by_operator',
                updated_at = ?
            WHERE id = ? AND workspace_id = ? AND status IN ('draft', 'approved')
            """,
            (now, derivative_id, workspace_id),
        )
        if cursor.rowcount != 1:
            raise SensitivityPolicyError(
                "Derivative state changed before revocation."
            )
        log_event(
            connection,
            event_type="SanitizedDerivativeRevoked",
            actor="local-user",
            target_type="SanitizedDerivative",
            target_id=derivative_id,
            workspace_id=workspace_id,
            payload={"content_digest": derivative.content_digest},
        )
        connection.commit()
    return get_sanitized_derivative(workspace_id, derivative_id)


def get_sanitized_derivative(
    workspace_id: str,
    derivative_id: str,
    *,
    refresh: bool = False,
) -> SanitizedDerivativeRead:
    """Read a derivative without mutation unless revalidation is explicit.

    The default is read-only. ``refresh=True`` remains an explicit compatibility
    path to ``revalidate_sanitized_derivative``.
    """
    if refresh:
        return revalidate_sanitized_derivative(workspace_id, derivative_id)
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        row = connection.execute(
            """
            SELECT * FROM sanitized_derivatives
            WHERE id = ? AND workspace_id = ?
            """,
            (derivative_id, workspace_id),
        ).fetchone()
    if row is None:
        raise SensitivityNotFoundError(
            f"Sanitized derivative not found: {derivative_id}"
        )
    return _derivative_read(row)


def revalidate_sanitized_derivative(
    workspace_id: str,
    derivative_id: str,
) -> SanitizedDerivativeRead:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_workspace(connection, workspace_id)
        row = connection.execute(
            """
            SELECT * FROM sanitized_derivatives
            WHERE id = ? AND workspace_id = ?
            """,
            (derivative_id, workspace_id),
        ).fetchone()
        if row is None:
            raise SensitivityNotFoundError(
                f"Sanitized derivative not found: {derivative_id}"
            )
        derivative = _derivative_read(row)
        if derivative.status != "approved":
            connection.commit()
            return derivative
        stale_reason = _derivative_stale_reason_in_connection(connection, derivative)
        if stale_reason is None:
            connection.commit()
            return derivative
        _mark_derivative_stale_in_connection(
            connection,
            derivative,
            stale_reason,
            allowed_statuses={"approved"},
        )
        connection.commit()
        refreshed = connection.execute(
            """
            SELECT * FROM sanitized_derivatives
            WHERE id = ? AND workspace_id = ?
            """,
            (derivative_id, workspace_id),
        ).fetchone()
    if refreshed is None:
        raise RuntimeError("Derivative disappeared after revalidation")
    return _derivative_read(refreshed)


def build_external_context_preview(
    workspace_id: str,
    budget_chars: int,
    selection: ContextSelectionSpec,
) -> SensitivityContextPreviewResponse:
    kinds = [
        kind
        for kind in (selection.kinds or list(CONTEXT_PACK_KINDS))
        if kind in CONTEXT_PACK_KINDS
    ]
    statuses_by_kind = _statuses_for_selection(selection, kinds)
    domain_kinds = [kind for kind in kinds if kind != "evidence"]
    candidates: list[_CandidateBlock] = []
    withheld: list[dict[str, Any]] = []
    included_derivatives: set[str] = set()
    covered_source_refs: set[str] = set()

    with open_sqlite_connection() as connection:
        connection.execute("BEGIN")
        _require_workspace(connection, workspace_id)
        records_by_kind = select_context_records(
            workspace_id,
            kinds=domain_kinds,
            statuses_by_kind=statuses_by_kind,
            ids=selection.ids,
            query=selection.query,
            max_items_per_kind=selection.max_items_per_kind,
            connection=connection,
        )
        if "evidence" in kinds:
            records_by_kind["evidence"] = select_evidence_records(
                workspace_id,
                statuses=statuses_by_kind["evidence"],
                ids=selection.ids,
                query=selection.query,
                max_items=selection.max_items_per_kind,
                connection=connection,
            )
        raw_blocks = [
            _block_for_record(kind, record)
            for kind in kinds
            for record in records_by_kind.get(kind, [])
        ]
        selected_refs = {block["source"] for block in raw_blocks}

        for block in raw_blocks:
            source_ref = block["source"]
            if source_ref in covered_source_refs:
                continue
            snapshot = _snapshot_from_block(workspace_id, block)
            try:
                candidate, exclusion = _candidate_for_snapshot_in_connection(
                    connection,
                    snapshot,
                    allowed_derivative_sources=selected_refs,
                    covered_derivative_sources=covered_source_refs,
                )
            except SensitivityNotFoundError:
                withheld.append(
                    {
                        "source_ref": source_ref,
                        "effective_level": "unknown",
                        "reason": "source_missing_during_preview",
                    }
                )
                continue
            if candidate is None:
                withheld.append(exclusion)
                continue
            derivative_id = candidate.manifest.get("derivative_id")
            if derivative_id is None:
                candidates.append(candidate)
                continue
            if derivative_id in included_derivatives:
                continue
            derivative_sources = set(candidate.manifest["source_refs"])
            candidates = [
                item
                for item in candidates
                if item.manifest.get("derivative_id") is not None
                or item.manifest.get("source_ref") not in derivative_sources
            ]
            included_derivatives.add(derivative_id)
            covered_source_refs.update(derivative_sources)
            candidates.append(candidate)
    return _apply_budget(candidates, withheld, budget_chars)


def preview_manual_context(
    workspace_id: str,
    raw_blocks: list[dict[str, Any]],
    budget_chars: int,
) -> SensitivityContextPreviewResponse:
    blocks = canonicalize_blocks(raw_blocks)
    candidates: list[_CandidateBlock] = []
    withheld: list[dict[str, Any]] = []
    seen_derivatives: set[str] = set()
    covered_source_refs: set[str] = set()
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN")
        _require_workspace(connection, workspace_id)
        for block in blocks:
            derivative_id = block.get("id")
            expected_source = (
                None if derivative_id is None else f"derivative:{derivative_id}"
            )
            if (
                not isinstance(derivative_id, str)
                or block["source"] != expected_source
            ):
                withheld.append(
                    {
                        "source_ref": block["source"],
                        "effective_level": "unknown",
                        "reason": "manual_block_not_server_derivative",
                    }
                )
                continue
            row = connection.execute(
                """
                SELECT * FROM sanitized_derivatives
                WHERE id = ? AND workspace_id = ?
                """,
                (derivative_id, workspace_id),
            ).fetchone()
            if row is None:
                withheld.append(
                    {
                        "source_ref": block["source"],
                        "effective_level": "unknown",
                        "reason": "derivative_not_found",
                    }
                )
                continue
            derivative = _derivative_read(row)
            if derivative.status != "approved":
                withheld.append(
                    {
                        "source_ref": block["source"],
                        "effective_level": derivative.effective_level,
                        "reason": f"derivative_{derivative.status}",
                    }
                )
                continue
            if _derivative_stale_reason_in_connection(connection, derivative) is not None:
                withheld.append(
                    {
                        "source_ref": block["source"],
                        "effective_level": derivative.effective_level,
                        "reason": "derivative_stale",
                    }
                )
                continue
            if not _is_external_eligible(derivative.effective_level):
                withheld.append(
                    {
                        "source_ref": block["source"],
                        "effective_level": derivative.effective_level,
                        "derivative_id": derivative.id,
                        "reason": "derivative_level_not_external_eligible",
                    }
                )
                continue
            if _derivative_content_digest(block["content"]) != derivative.content_digest:
                withheld.append(
                    {
                        "source_ref": block["source"],
                        "effective_level": derivative.effective_level,
                        "reason": "derivative_content_digest_mismatch",
                    }
                )
                continue
            if derivative_id in seen_derivatives:
                continue
            derivative_sources = set(derivative.source_refs)
            if derivative_sources & covered_source_refs:
                withheld.append(
                    {
                        "source_ref": block["source"],
                        "effective_level": derivative.effective_level,
                        "derivative_id": derivative.id,
                        "reason": "derivative_source_overlap",
                    }
                )
                continue
            seen_derivatives.add(derivative_id)
            covered_source_refs.update(derivative_sources)
            candidates.append(_candidate_from_derivative(derivative))
    return _apply_budget(candidates, withheld, budget_chars)


def _candidate_for_snapshot_in_connection(
    connection: sqlite3.Connection,
    snapshot: SourceSnapshot,
    *,
    allowed_derivative_sources: set[str],
    covered_derivative_sources: set[str] | None = None,
) -> tuple[_CandidateBlock | None, dict[str, Any]]:
    current_snapshot, label = _resolve_source_snapshot_and_label_in_connection(
        connection,
        snapshot.workspace_id,
        snapshot.subject_ref,
    )
    if current_snapshot.content_digest != snapshot.content_digest:
        return (
            None,
            {
                "source_ref": snapshot.subject_ref,
                "effective_level": "unknown",
                "reason": "source_changed_during_preview",
            },
        )

    floor = deterministic_floor(snapshot.block["content"])
    label_current = (
        label is not None
        and label.current
        and label.content_digest == snapshot.content_digest
        and label.policy_version == POLICY_VERSION
    )
    effective_level = (
        _max_level(label.level, floor)
        if label_current
        else floor or "unknown"
    )
    if label_current and _is_external_eligible(effective_level):
        return (
            _CandidateBlock(
                block=snapshot.block,
                manifest={
                    "source_ref": snapshot.subject_ref,
                    "content_digest": snapshot.content_digest,
                    "effective_level": effective_level,
                    "label_id": label.id,
                    "derivative_id": None,
                    "inclusion_reason": "current_label",
                },
                drop_priority=_source_kind_priority(snapshot.kind),
            ),
            {},
        )

    derivative, ineligible_derivative = _approved_derivative_for_source_in_connection(
        connection,
        snapshot.workspace_id,
        snapshot.subject_ref,
        allowed_source_refs=allowed_derivative_sources,
        disallowed_source_refs=covered_derivative_sources or set(),
    )
    if derivative is not None:
        return _candidate_from_derivative(derivative), {}
    if ineligible_derivative is not None:
        return (
            None,
            {
                "source_ref": snapshot.subject_ref,
                "effective_level": ineligible_derivative.effective_level,
                "derivative_id": ineligible_derivative.id,
                "reason": "derivative_level_not_external_eligible",
            },
        )
    if label is None:
        reason = "missing_current_label"
    elif not label.current:
        reason = "stale_label"
    else:
        reason = "raw_level_not_external_eligible"
    return (
        None,
        {
            "source_ref": snapshot.subject_ref,
            "effective_level": effective_level,
            "reason": reason,
        },
    )


def _candidate_from_derivative(
    derivative: SanitizedDerivativeRead,
) -> _CandidateBlock:
    if not _is_external_eligible(derivative.effective_level):
        raise SensitivityPolicyError(
            "External preview candidate must be S0 or S1."
        )
    return _CandidateBlock(
        block={
            "source": f"derivative:{derivative.id}",
            "type": "sanitized_derivative",
            "id": derivative.id,
            "content": derivative.content,
        },
        manifest={
            "source_ref": f"derivative:{derivative.id}",
            "source_refs": derivative.source_refs,
            "content_digest": derivative.content_digest,
            "effective_level": derivative.effective_level,
            "label_id": None,
            "derivative_id": derivative.id,
            "inclusion_reason": "approved_derivative",
        },
        drop_priority=max(
            _source_kind_priority(_parse_subject_ref(ref, DERIVATIVE_SOURCE_KINDS)[0])
            for ref in derivative.source_refs
        ),
    )


def _apply_budget(
    candidates: list[_CandidateBlock],
    withheld: list[dict[str, Any]],
    budget_chars: int,
) -> SensitivityContextPreviewResponse:
    kept = list(candidates)
    dropped: list[dict[str, Any]] = []
    while kept and (
        len(kept) > MAX_CONTEXT_BLOCKS
        or len(_serialize_blocks([item.block for item in kept])) > budget_chars
    ):
        drop_index = min(
            range(len(kept)),
            key=lambda index: (
                kept[index].drop_priority,
                -index,
            ),
        )
        dropped.append(kept.pop(drop_index).manifest)
    blocks = [item.block for item in kept]
    manifests = [item.manifest for item in kept]
    char_count = len(_serialize_blocks(blocks)) if blocks else 0
    return SensitivityContextPreviewResponse(
        blocks=blocks,
        context_digest=canonical_digest(blocks) if blocks else None,
        included_sources_manifest=manifests,
        withheld_sources_manifest=withheld,
        dropped_sources_manifest=dropped,
        char_count=char_count,
        estimated_token_count=(char_count + 3) // 4,
        included_count=len(blocks),
        withheld_count=len(withheld),
        dropped_count=len(dropped),
        budget_chars=budget_chars,
    )


def _approved_derivative_for_source_in_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
    subject_ref: str,
    *,
    allowed_source_refs: set[str],
    disallowed_source_refs: set[str],
) -> tuple[SanitizedDerivativeRead | None, SanitizedDerivativeRead | None]:
    _require_workspace(connection, workspace_id)
    rows = connection.execute(
        """
        SELECT * FROM sanitized_derivatives
        WHERE workspace_id = ? AND status = 'approved'
        ORDER BY updated_at DESC, id ASC
        """,
        (workspace_id,),
    ).fetchall()
    ineligible: SanitizedDerivativeRead | None = None
    for row in rows:
        derivative = _derivative_read(row)
        if subject_ref not in derivative.source_refs:
            continue
        derivative_sources = set(derivative.source_refs)
        if not derivative_sources.issubset(allowed_source_refs):
            continue
        if derivative_sources & disallowed_source_refs:
            continue
        if _derivative_stale_reason_in_connection(connection, derivative) is not None:
            continue
        if not _is_external_eligible(derivative.effective_level):
            if ineligible is None:
                ineligible = derivative
            continue
        return derivative, ineligible
    return None, ineligible


def _derivative_stale_reason_in_connection(
    connection: sqlite3.Connection,
    derivative: SanitizedDerivativeRead,
) -> str | None:
    if derivative.policy_version != POLICY_VERSION:
        return "policy_version_mismatch"
    for ref in derivative.source_refs:
        try:
            snapshot, label = _resolve_source_snapshot_and_label_in_connection(
                connection,
                derivative.workspace_id,
                ref,
            )
        except SensitivityNotFoundError:
            return f"source_missing:{ref}"
        if derivative.source_digests.get(ref) != snapshot.content_digest:
            return f"source_digest_mismatch:{ref}"
        source_level = _effective_level_for_bound_snapshot(snapshot, label)
        if (
            source_level == "S4"
            and _LEVEL_RANK[derivative.effective_level] > _LEVEL_RANK["S1"]
        ):
            return f"source_level_incompatible:{ref}:S4"
    return None


def _effective_level_for_bound_snapshot(
    snapshot: SourceSnapshot,
    label: SensitivityLabelRead | None,
) -> str:
    floor = deterministic_floor(snapshot.block["content"])
    if (
        label is None
        or not label.current
        or label.content_digest != snapshot.content_digest
        or label.policy_version != POLICY_VERSION
    ):
        return floor or "unknown"
    return _max_level(label.level, floor)


def _mark_derivative_stale_in_connection(
    connection: sqlite3.Connection,
    derivative: SanitizedDerivativeRead,
    reason: str,
    *,
    allowed_statuses: set[str],
) -> bool:
    statuses = sorted(
        status
        for status in allowed_statuses
        if status in {"draft", "approved"}
    )
    if not statuses or derivative.status not in statuses:
        return False
    placeholders = ", ".join("?" for _ in statuses)
    now = utc_now()
    cursor = connection.execute(
        f"""
        UPDATE sanitized_derivatives
        SET status = 'stale', stale_reason = ?, updated_at = ?
        WHERE id = ? AND workspace_id = ?
          AND status IN ({placeholders})
        """,
        (
            reason,
            now,
            derivative.id,
            derivative.workspace_id,
            *statuses,
        ),
    )
    if cursor.rowcount != 1:
        return False
    log_event(
        connection,
        event_type="SanitizedDerivativeMarkedStale",
        actor="deterministic-policy",
        target_type="SanitizedDerivative",
        target_id=derivative.id,
        workspace_id=derivative.workspace_id,
        payload={
            "content_digest": derivative.content_digest,
            "previous_status": derivative.status,
            "stale_reason": reason,
            "policy_version": POLICY_VERSION,
        },
    )
    return True


def _resolve_source_snapshot_and_label(
    workspace_id: str,
    subject_ref: str,
) -> tuple[SourceSnapshot, SensitivityLabelRead | None]:
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN")
        return _resolve_source_snapshot_and_label_in_connection(
            connection,
            workspace_id,
            subject_ref,
        )


def _resolve_source_snapshot_and_label_in_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
    subject_ref: str,
) -> tuple[SourceSnapshot, SensitivityLabelRead | None]:
    kind, record_id = _parse_subject_ref(subject_ref, DERIVATIVE_SOURCE_KINDS)
    _require_workspace(connection, workspace_id)
    if kind == "interaction":
        return _interaction_snapshot_in_connection(connection, workspace_id, record_id), None
    source_row = connection.execute(
        f"SELECT * FROM {_SOURCE_TABLES[kind]} "
        "WHERE id = ? AND workspace_id = ?",
        (record_id, workspace_id),
    ).fetchone()
    if source_row is None:
        raise SensitivityNotFoundError(f"Source not found: {subject_ref}")
    model = _SOURCE_MODELS[kind].model_validate(dict(source_row))
    snapshot = _snapshot_from_block(
        workspace_id,
        _block_for_record(kind, model),
    )
    label_row = _latest_label_row_in_connection(
        connection,
        workspace_id,
        snapshot.subject_ref,
    )
    if label_row is None:
        return snapshot, None
    digest_matches = label_row["content_digest"] == snapshot.content_digest
    policy_matches = label_row["policy_version"] == POLICY_VERSION
    current = digest_matches and policy_matches
    if current:
        stale_reason = None
    elif not digest_matches:
        stale_reason = "content_digest_mismatch"
    else:
        stale_reason = "policy_version_mismatch"
    return (
        snapshot,
        _label_read(
            label_row,
            current=current,
            stale_reason=stale_reason,
        ),
    )


def _interaction_snapshot_in_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
    interaction_id: str,
) -> SourceSnapshot:
    """The operator's own turn text, bound to its workspace through its thread."""
    row = connection.execute(
        """
        SELECT interaction.id, interaction.user_text
        FROM ai_thread_interactions AS interaction
        JOIN ai_threads AS thread ON thread.id = interaction.thread_id
        WHERE interaction.id = ? AND thread.workspace_id = ?
        """,
        (interaction_id, workspace_id),
    ).fetchone()
    if row is None:
        raise SensitivityNotFoundError(f"Source not found: interaction:{interaction_id}")
    return _snapshot_from_block(
        workspace_id,
        {
            "source": f"interaction:{row['id']}",
            "type": "interaction",
            "id": str(row["id"]),
            "content": str(row["user_text"]),
        },
    )


def _latest_label_row_in_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
    subject_ref: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT label.*
        FROM sensitivity_labels AS label
        WHERE label.workspace_id = ?
          AND label.subject_ref = ?
          AND NOT EXISTS (
              SELECT 1
              FROM sensitivity_labels AS child
              WHERE child.prior_label_id = label.id
          )
        ORDER BY label.rowid DESC
        LIMIT 1
        """,
        (workspace_id, subject_ref),
    ).fetchone()


def _source_kind_priority(kind: str) -> int:
    return _CONTEXT_PACK_DROP_PRIORITY.get(kind, 0)


def _snapshot_from_block(
    workspace_id: str,
    block: dict[str, Any],
) -> SourceSnapshot:
    subject_ref = str(block["source"])
    kind, record_id = _parse_subject_ref(subject_ref, DERIVATIVE_SOURCE_KINDS)
    content = str(block["content"])
    return SourceSnapshot(
        workspace_id=workspace_id,
        subject_ref=subject_ref,
        kind=kind,
        record_id=record_id,
        block=dict(block),
        content_digest=canonical_digest(
            {
                "subject_ref": subject_ref,
                "content": content,
            }
        ),
    )


def _parse_subject_ref(
    subject_ref: str,
    kinds: frozenset[str] = ALLOWED_SOURCE_KINDS,
) -> tuple[str, str]:
    if not isinstance(subject_ref, str) or ":" not in subject_ref:
        raise SensitivityPolicyError("subject_ref must use <kind>:<id>")
    kind, record_id = subject_ref.split(":", 1)
    if kind not in kinds or not record_id.strip():
        raise SensitivityPolicyError("Unsupported or malformed subject_ref")
    return kind, record_id.strip()


def _max_level(*levels: str | None) -> str:
    concrete = [level for level in levels if level is not None]
    if not concrete:
        return "S0"
    return max(concrete, key=lambda level: _LEVEL_RANK[level])


def _derivative_content_digest(content: str) -> str:
    return canonical_digest({"content": content})


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _label_read(
    row: sqlite3.Row,
    *,
    current: bool,
    stale_reason: str | None,
) -> SensitivityLabelRead:
    return SensitivityLabelRead(
        id=row["id"],
        workspace_id=row["workspace_id"],
        subject_ref=row["subject_ref"],
        content_digest=row["content_digest"],
        level=row["level"],
        classification_source=row["classification_source"],
        policy_version=row["policy_version"],
        actor=row["actor"],
        prior_label_id=row["prior_label_id"],
        created_at=row["created_at"],
        current=current,
        stale_reason=stale_reason,
    )


def _derivative_read(row: sqlite3.Row) -> SanitizedDerivativeRead:
    return SanitizedDerivativeRead(
        id=row["id"],
        workspace_id=row["workspace_id"],
        source_refs=json.loads(row["source_refs_json"]),
        source_digests=json.loads(row["source_digests_json"]),
        content=row["content"],
        content_digest=row["content_digest"],
        effective_level=row["effective_level"],
        transformations=json.loads(row["transformations_json"]),
        policy_version=row["policy_version"],
        status=row["status"],
        actor=row["actor"],
        reviewer=row["reviewer"],
        reviewed_at=row["reviewed_at"],
        stale_reason=row["stale_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _require_workspace(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> None:
    row = connection.execute(
        "SELECT id FROM workspaces WHERE id = ?",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise SensitivityNotFoundError("Workspace not found.")
