from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.token_flow_segments import (
    list_protected_segment_metadata,
    read_protected_segment,
)
from app.modules.ai.token_flow_service import (
    ID_RE,
    TokenFlowConflictError,
    _safe,
)


@dataclass(frozen=True, slots=True)
class AssembledOutput:
    flow_id: str
    body_digest: str
    byte_count: int
    token_count: int
    segment_count: int
    segment_digests: tuple[str, ...]
    originating_attempt_ids: tuple[str, ...]
    body_text: str = field(repr=False)


def assemble_protected_output(
    *,
    flow_id: str,
    workspace_id: str | None,
    expected_sensitivity_level: str,
    now: datetime | None = None,
) -> AssembledOutput:
    """Validate and deterministically assemble every protected segment for one flow."""

    flow_id = _safe(flow_id, ID_RE, "flow_id")
    metadata = list_protected_segment_metadata(
        flow_id=flow_id,
        workspace_id=workspace_id,
        now=now,
    )
    if not metadata:
        raise TokenFlowConflictError(
            "protected output assembly requires at least one segment"
        )
    if any(item.expired for item in metadata):
        raise TokenFlowConflictError(
            "protected output assembly cannot use expired segments"
        )

    validated = [
        read_protected_segment(
            flow_id=flow_id,
            segment_index=item.segment_index,
            workspace_id=workspace_id,
            expected_sensitivity_level=expected_sensitivity_level,
            now=now,
        )
        for item in metadata
    ]
    bodies = [item.body_text for item in validated]
    body_text = "".join(bodies)
    if not body_text:
        raise TokenFlowConflictError(
            "protected output assembly produced an empty body"
        )
    segment_digests = tuple(item.metadata.body_digest for item in validated)
    attempt_ids = tuple(
        item.metadata.originating_attempt_id for item in validated
    )
    if len(set(attempt_ids)) != len(attempt_ids):
        raise TokenFlowConflictError(
            "protected output assembly contains duplicate originating attempts"
        )
    return AssembledOutput(
        flow_id=flow_id,
        body_digest=canonical_digest({"text": body_text}),
        byte_count=len(body_text.encode("utf-8")),
        token_count=sum(item.metadata.token_count for item in validated),
        segment_count=len(validated),
        segment_digests=segment_digests,
        originating_attempt_ids=attempt_ids,
        body_text=body_text,
    )
