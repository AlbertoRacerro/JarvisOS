"""Deterministic, attempt-scoped evidence rendering for structural repair."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from app.core.database import open_sqlite_connection
from app.core.repository import rows_to_models
from app.modules.bluecad.evidence import EvidenceRecord, evidence_pack_line

_ALLOWED_KINDS = ("validation_v0", "mesh_quality_v0", "fem_static_v0")
_BLOCK_LABEL = "EVIDENCE_SIGHT_V0"
EVIDENCE_SIGHT_RENDERER_ID = "evidence_sight_v0"
EVIDENCE_SIGHT_RENDERER_VERSION = "evidence_sight_v0"
EVIDENCE_SIGHT_MAX_LINES = 6
EVIDENCE_SIGHT_MAX_CHARS = 2000


@dataclass(frozen=True)
class EvidenceSight:
    text: str
    digest: str
    record_ids: tuple[str, ...]


def render_evidence_sight(
    workspace_id: str,
    candidate_id: str,
    attempt_id: str,
    *,
    max_lines: int = EVIDENCE_SIGHT_MAX_LINES,
    max_chars: int = EVIDENCE_SIGHT_MAX_CHARS,
) -> EvidenceSight | None:
    """Render bounded evidence for one exact BLUECAD attempt.

    No workspace-, candidate-, or latest-record fallback is permitted. The
    returned digest covers exactly the text visible to the model.
    """

    with open_sqlite_connection() as connection:
        return _render_evidence_sight_in_connection(
            connection,
            workspace_id,
            candidate_id,
            attempt_id,
            max_lines=max_lines,
            max_chars=max_chars,
        )


def _render_evidence_sight_in_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
    candidate_id: str,
    attempt_id: str,
    *,
    max_lines: int = EVIDENCE_SIGHT_MAX_LINES,
    max_chars: int = EVIDENCE_SIGHT_MAX_CHARS,
) -> EvidenceSight | None:
    """Render through an existing coherent read snapshot.

    This internal seam is used by EVIDENCE-EGRESS-0 so source ownership,
    renderer bytes, source digests, and derivative authority are resolved from
    the same snapshot. Selection and byte semantics remain identical to the
    public renderer.
    """

    if not workspace_id or not candidate_id or not attempt_id:
        raise ValueError("workspace_id, candidate_id, and attempt_id are required")
    if isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines <= 0:
        raise ValueError("max_lines must be a positive integer")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")

    placeholders = ", ".join("?" for _ in _ALLOWED_KINDS)
    rows = connection.execute(
        f"""
        SELECT *
        FROM evidence_records
        WHERE workspace_id = ?
          AND candidate_id = ?
          AND attempt_id = ?
          AND kind IN ({placeholders})
        ORDER BY
            CASE kind
                WHEN 'validation_v0' THEN 0
                WHEN 'mesh_quality_v0' THEN 1
                WHEN 'fem_static_v0' THEN 2
                ELSE 3
            END,
            created_at ASC,
            id ASC
        """,
        (workspace_id, candidate_id, attempt_id, *_ALLOWED_KINDS),
    ).fetchall()
    records = rows_to_models(rows, EvidenceRecord)
    if not records:
        return None

    rendered = [(record, evidence_pack_line(record)) for record in records]
    selected: list[tuple[EvidenceRecord, str]] = []
    omitted = 0

    for index, item in enumerate(rendered):
        if len(selected) >= max_lines:
            omitted = len(rendered) - index
            break
        candidate_lines = [_BLOCK_LABEL, *(line for _, line in selected), item[1]]
        candidate_text = "\n".join(candidate_lines)
        if len(candidate_text) > max_chars:
            omitted = len(rendered) - index
            break
        selected.append(item)

    if not selected:
        return None

    if omitted:
        marker = f"evidence:omitted count={omitted}"
        if len(selected) < max_lines:
            with_marker = "\n".join([_BLOCK_LABEL, *(line for _, line in selected), marker])
            if len(with_marker) <= max_chars:
                selected_lines = [line for _, line in selected] + [marker]
            else:
                selected_lines = [line for _, line in selected]
        else:
            replacement = selected[:-1]
            omitted_with_replacement = omitted + 1
            replacement_marker = f"evidence:omitted count={omitted_with_replacement}"
            with_marker = "\n".join(
                [_BLOCK_LABEL, *(line for _, line in replacement), replacement_marker]
            )
            if replacement and len(with_marker) <= max_chars:
                selected = replacement
                selected_lines = [line for _, line in selected] + [replacement_marker]
            else:
                selected_lines = [line for _, line in selected]
    else:
        selected_lines = [line for _, line in selected]

    text = "\n".join([_BLOCK_LABEL, *selected_lines])
    if len(text) > max_chars:  # defensive invariant; lines are never truncated.
        return None
    digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return EvidenceSight(
        text=text,
        digest=digest,
        record_ids=tuple(record.id for record, _ in selected),
    )
