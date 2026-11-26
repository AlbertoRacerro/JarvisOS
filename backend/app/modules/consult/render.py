"""Consult rendering, marker parsing, and byte-identity check (spec 380 part b).

Parses ``{{consult:<id>}}`` markers in assistant text, appends any consult attempts
for that turn_id whose marker is missing, and returns structured consult blocks.
The byte-identity check verifies ``sha256(answer_text) == answer_sha256``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from pydantic import BaseModel, Field

CONSULT_MARKER_RE = re.compile(r"\{\{consult:([a-zA-Z0-9_-]+)\}\}")


class ConsultBlockRead(BaseModel):
    consult_id: str
    state: str
    kind: str
    modules: list[str] = Field(default_factory=list)
    brief_sha256: str | None = None
    packet_text: str | None = None
    packet_sha256: str
    model_id: str | None = None
    created_at: str
    answer_text: str | None = None
    answer_sha256: str | None = None
    byte_identical: bool = False
    answered_at: str | None = None
    swap_timing: dict[str, float] | None = None
    feedback_verdict: str | None = None


def parse_consult_markers(text: str) -> list[str]:
    """Extract consult IDs from {{consult:<id>}} markers in order of appearance."""
    return CONSULT_MARKER_RE.findall(text)


def render_consult_blocks(
    connection: sqlite3.Connection,
    turn_id: str | None,
    assistant_text: str | None,
) -> list[ConsultBlockRead]:
    """Parse {{consult:<id>}} markers in assistant text, append any consult attempts

    for that turn_id whose marker is missing, and return structured consult blocks.
    byte_identical verifies sha256(answer_text) == answer_sha256.
    """
    if not turn_id:
        return []

    query = """
        SELECT
            a.id, a.state, a.kind, a.modules_json, a.brief_sha256, a.packet_text,
            a.packet_sha256, a.model_id, a.created_at,
            ans.answer_text, ans.answer_sha256, ans.created_at as answered_at,
            a.gate_steps_json
        FROM consult_attempts a
        LEFT JOIN consult_answers ans ON ans.attempt_id = a.id
        WHERE a.turn_id = ?
        ORDER BY a.created_at ASC, a.rowid ASC
    """
    rows = connection.execute(query, (turn_id,)).fetchall()
    if not rows:
        return []

    attempts_by_id: dict[str, Any] = {row[0]: row for row in rows}

    marker_ids = parse_consult_markers(assistant_text or "")
    ordered_ids: list[str] = []
    seen: set[str] = set()

    for cid in marker_ids:
        if cid in attempts_by_id and cid not in seen:
            ordered_ids.append(cid)
            seen.add(cid)

    for row in rows:
        cid = row[0]
        if cid not in seen:
            ordered_ids.append(cid)
            seen.add(cid)

    blocks: list[ConsultBlockRead] = []
    for cid in ordered_ids:
        row = attempts_by_id[cid]
        modules = []
        modules_json = row[3]
        if modules_json:
            try:
                modules = json.loads(modules_json)
            except (json.JSONDecodeError, TypeError):
                modules = []

        answer_text = row[9]
        answer_sha = row[10]
        byte_identical = False
        if answer_text is not None and answer_sha is not None:
            computed_sha = hashlib.sha256(answer_text.encode("utf-8")).hexdigest()
            byte_identical = computed_sha == answer_sha

        swap_timing = None
        if len(row) > 12 and row[12]:
            try:
                gate_steps = json.loads(row[12])
                for s in gate_steps:
                    if s.get("step") == "sage_swap":
                        swap_timing = {
                            "swap_to_sage_s": float(s.get("swap_to_sage_s", 0.0)),
                            "swap_back_s": float(s.get("swap_back_s", 0.0),),
                            "total_swap_s": float(s.get("total_swap_s", 0.0)),
                        }
            except Exception:
                swap_timing = None

        feedback_verdict = None
        try:
            fb_row = connection.execute(
                "SELECT verdict FROM consult_feedback WHERE attempt_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (row[0],),
            ).fetchone()
            if fb_row:
                feedback_verdict = fb_row[0]
        except Exception:
            feedback_verdict = None

        blocks.append(
            ConsultBlockRead(
                consult_id=row[0],
                state=row[1],
                kind=row[2],
                modules=modules,
                brief_sha256=row[4],
                packet_text=row[5],
                packet_sha256=row[6],
                model_id=row[7],
                created_at=str(row[8]),
                answer_text=answer_text,
                answer_sha256=answer_sha,
                byte_identical=byte_identical,
                answered_at=str(row[11]) if row[11] else None,
                swap_timing=swap_timing,
                feedback_verdict=feedback_verdict,
            )
        )

    return blocks
