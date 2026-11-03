from __future__ import annotations

import sqlite3

from app.modules.ai import sensitivity
from app.modules.ai.egress_service import (
    EgressPacketMaterial,
    EgressPacketProjection,
    canonical_json,
    sha256_text,
)


def validate_ticket_authority_state(
    connection: sqlite3.Connection,
    *,
    material: EgressPacketMaterial,
    projection: EgressPacketProjection,
    allow_continuation_segments: bool = False,
) -> None:
    """Revalidate mutable authority referenced by one pending confirmation ticket.

    Packet and policy digests prove that the persisted envelope is unchanged. They do not
    prove that a previously-approved prompt derivative, canonical derivative, source row,
    or sensitivity label is still current. This check runs inside the same IMMEDIATE
    transaction that performs the pending-to-consumed CAS.
    """

    _validate_prompt_derivative(
        connection,
        material=material,
        projection=projection,
    )
    _validate_context_authority(connection, material=material)
    _validate_source_digests(
        connection,
        material=material,
        allow_continuation_segments=allow_continuation_segments,
    )


def _validate_prompt_derivative(
    connection: sqlite3.Connection,
    *,
    material: EgressPacketMaterial,
    projection: EgressPacketProjection,
) -> None:
    derivative_id = material.prompt_derivative_id
    if derivative_id is None:
        return
    row = connection.execute(
        "SELECT * FROM egress_prompt_derivatives WHERE id = ?",
        (derivative_id,),
    ).fetchone()
    if row is None:
        raise sensitivity.SensitivityPolicyError(
            "ticket prompt derivative no longer exists"
        )
    if row["status"] != "approved":
        raise sensitivity.SensitivityPolicyError(
            "ticket prompt derivative is no longer approved"
        )
    if row["workspace_id"] != material.workspace_id:
        raise sensitivity.SensitivityPolicyError(
            "ticket prompt derivative workspace binding changed"
        )
    if row["policy_version"] != projection.policy_version:
        raise sensitivity.SensitivityPolicyError(
            "ticket prompt derivative policy version changed"
        )
    if row["final_level"] != material.prompt_level:
        raise sensitivity.SensitivityPolicyError(
            "ticket prompt derivative level changed"
        )
    if row["derivative_content"] != material.prompt:
        raise sensitivity.SensitivityPolicyError(
            "ticket prompt derivative content changed"
        )
    if row["derivative_digest"] != sha256_text(material.prompt):
        raise sensitivity.SensitivityPolicyError(
            "ticket prompt derivative digest changed"
        )
    if sensitivity.deterministic_floor(material.prompt) is not None:
        raise sensitivity.SensitivityPolicyError(
            "ticket prompt derivative is no longer external-eligible"
        )


def _validate_context_authority(
    connection: sqlite3.Connection,
    *,
    material: EgressPacketMaterial,
) -> None:
    blocks_by_source: dict[str, dict] = {}
    for block in material.context_blocks:
        source = block.get("source")
        if not isinstance(source, str) or not source:
            raise sensitivity.SensitivityPolicyError(
                "ticket context block has no canonical source"
            )
        if source in blocks_by_source:
            raise sensitivity.SensitivityPolicyError(
                "ticket context contains duplicate sources"
            )
        blocks_by_source[source] = block

    manifests_by_source: dict[str, dict] = {}
    for manifest in material.included_manifest:
        source_ref = manifest.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref:
            raise sensitivity.SensitivityPolicyError(
                "ticket included manifest has no source_ref"
            )
        if source_ref in manifests_by_source:
            raise sensitivity.SensitivityPolicyError(
                "ticket included manifest contains duplicate sources"
            )
        manifests_by_source[source_ref] = manifest
        derivative_id = manifest.get("derivative_id")
        if derivative_id is None:
            _validate_direct_source_manifest(
                connection,
                workspace_id=material.workspace_id,
                manifest=manifest,
                block=blocks_by_source.get(source_ref),
            )
        elif isinstance(derivative_id, str) and derivative_id:
            _validate_canonical_derivative_manifest(
                connection,
                workspace_id=material.workspace_id,
                derivative_id=derivative_id,
                manifest=manifest,
                block=blocks_by_source.get(source_ref),
            )
        else:
            raise sensitivity.SensitivityPolicyError(
                "ticket included manifest has invalid derivative_id"
            )

    if set(blocks_by_source) != set(manifests_by_source):
        raise sensitivity.SensitivityPolicyError(
            "ticket context blocks no longer match included authority manifests"
        )


def _validate_direct_source_manifest(
    connection: sqlite3.Connection,
    *,
    workspace_id: str | None,
    manifest: dict,
    block: dict | None,
) -> None:
    if workspace_id is None:
        raise sensitivity.SensitivityPolicyError(
            "ticket direct source has no workspace binding"
        )
    source_ref = str(manifest["source_ref"])
    snapshot, label = _resolve_current_source(
        connection,
        workspace_id=workspace_id,
        source_ref=source_ref,
    )
    if block is None or canonical_json(block) != canonical_json(snapshot.block):
        raise sensitivity.SensitivityPolicyError(
            "ticket direct source block changed"
        )
    if manifest.get("content_digest") != snapshot.content_digest:
        raise sensitivity.SensitivityPolicyError(
            "ticket direct source digest changed"
        )
    if label is None or not label.current:
        raise sensitivity.SensitivityPolicyError(
            "ticket direct source label is no longer current"
        )
    if manifest.get("label_id") != label.id:
        raise sensitivity.SensitivityPolicyError(
            "ticket direct source label changed"
        )
    current_level = sensitivity._effective_level_for_bound_snapshot(snapshot, label)
    if current_level not in {"S0", "S1"}:
        raise sensitivity.SensitivityPolicyError(
            "ticket direct source is no longer external-eligible"
        )
    if manifest.get("effective_level") != current_level:
        raise sensitivity.SensitivityPolicyError(
            "ticket direct source effective level changed"
        )


def _validate_canonical_derivative_manifest(
    connection: sqlite3.Connection,
    *,
    workspace_id: str | None,
    derivative_id: str,
    manifest: dict,
    block: dict | None,
) -> None:
    if workspace_id is None:
        raise sensitivity.SensitivityPolicyError(
            "ticket canonical derivative has no workspace binding"
        )
    row = connection.execute(
        "SELECT * FROM sanitized_derivatives WHERE id = ? AND workspace_id = ?",
        (derivative_id, workspace_id),
    ).fetchone()
    if row is None:
        raise sensitivity.SensitivityPolicyError(
            "ticket canonical derivative no longer exists"
        )
    derivative = sensitivity._derivative_read(row)
    if derivative.status != "approved":
        raise sensitivity.SensitivityPolicyError(
            "ticket canonical derivative is no longer approved"
        )
    stale_reason = sensitivity._derivative_stale_reason_in_connection(
        connection,
        derivative,
    )
    if stale_reason is not None:
        raise sensitivity.SensitivityPolicyError(
            f"ticket canonical derivative is stale: {stale_reason}"
        )
    if derivative.policy_version != sensitivity.POLICY_VERSION:
        raise sensitivity.SensitivityPolicyError(
            "ticket canonical derivative policy version changed"
        )
    if derivative.effective_level not in {"S0", "S1"}:
        raise sensitivity.SensitivityPolicyError(
            "ticket canonical derivative is no longer external-eligible"
        )
    if manifest.get("source_ref") != f"derivative:{derivative.id}":
        raise sensitivity.SensitivityPolicyError(
            "ticket canonical derivative source binding changed"
        )
    if manifest.get("source_refs") != derivative.source_refs:
        raise sensitivity.SensitivityPolicyError(
            "ticket canonical derivative source set changed"
        )
    if manifest.get("content_digest") != derivative.content_digest:
        raise sensitivity.SensitivityPolicyError(
            "ticket canonical derivative digest changed"
        )
    if manifest.get("effective_level") != derivative.effective_level:
        raise sensitivity.SensitivityPolicyError(
            "ticket canonical derivative level changed"
        )
    if sensitivity._derivative_content_digest(derivative.content) != derivative.content_digest:
        raise sensitivity.SensitivityPolicyError(
            "ticket canonical derivative content digest is invalid"
        )
    expected_block = {
        "source": f"derivative:{derivative.id}",
        "type": "sanitized_derivative",
        "id": derivative.id,
        "content": derivative.content,
    }
    if block is None or canonical_json(block) != canonical_json(expected_block):
        raise sensitivity.SensitivityPolicyError(
            "ticket canonical derivative block changed"
        )


def _validate_source_digests(
    connection: sqlite3.Connection,
    *,
    material: EgressPacketMaterial,
    allow_continuation_segments: bool,
) -> None:
    expected = dict(material.source_digests)
    if not expected:
        return
    if material.workspace_id is None:
        raise sensitivity.SensitivityPolicyError(
            "ticket source digests have no workspace binding"
        )
    for source_ref, expected_digest in sorted(expected.items()):
        if source_ref.startswith("segment:"):
            if allow_continuation_segments:
                continue
            raise sensitivity.SensitivityPolicyError(
                "ordinary confirmation ticket cannot reference protected segments"
            )
        snapshot, _label = _resolve_current_source(
            connection,
            workspace_id=material.workspace_id,
            source_ref=source_ref,
        )
        if snapshot.content_digest != expected_digest:
            raise sensitivity.SensitivityPolicyError(
                f"ticket source digest changed: {source_ref}"
            )


def _resolve_current_source(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    source_ref: str,
):
    try:
        return sensitivity._resolve_source_snapshot_and_label_in_connection(
            connection,
            workspace_id,
            source_ref,
        )
    except sensitivity.SensitivityNotFoundError as exc:
        raise sensitivity.SensitivityPolicyError(
            f"ticket source no longer exists: {source_ref}"
        ) from exc
