"""Transactional ledger helpers owned by EVIDENCE-SIGHT-0."""

from __future__ import annotations

import json
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.modules.bluecad.ledger import get_attempt
from app.modules.bluecad.models import BluecadAttemptRead
from app.modules.events.service import utc_now


def start_structural_attempt(
    candidate_id: str,
    attempt_no: int,
    route_class: str,
    *,
    prompt_version: str,
    evidence_digest: str,
) -> BluecadAttemptRead:
    """Record one speculative structural attempt without changing candidate state."""

    now = utc_now()
    attempt_id = str(uuid4())
    detail = {
        "attempt_kind": "structural_repair",
        "prompt_version": prompt_version,
        "evidence_digest": evidence_digest,
    }
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        owner = connection.execute(
            "SELECT status FROM bluecad_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if owner is None or str(owner["status"]) != "valid":
            connection.rollback()
            raise ValueError("structural attempt requires a valid candidate")
        row = connection.execute(
            """
            SELECT COALESCE(MAX(attempt_no), 0) AS max_attempt_no
            FROM bluecad_attempts
            WHERE candidate_id = ?
            """,
            (candidate_id,),
        ).fetchone()
        expected = int(row["max_attempt_no"]) + 1
        if attempt_no != expected:
            connection.rollback()
            raise ValueError(
                f"structural attempt_no must be the next candidate-wide value ({expected})"
            )
        connection.execute(
            """
            INSERT INTO bluecad_attempts (
                id, candidate_id, attempt_no, route_class, proposal_outcome,
                started_at, error_detail_json
            ) VALUES (?, ?, ?, ?, 'blocked', ?, ?)
            """,
            (
                attempt_id,
                candidate_id,
                attempt_no,
                route_class,
                now,
                json.dumps(detail, sort_keys=True),
            ),
        )
        connection.commit()
    attempt = get_attempt(attempt_id)
    if attempt is None:  # pragma: no cover - defensive persistence guard
        raise RuntimeError("structural attempt disappeared after insertion")
    return attempt


def commit_structural_candidate_artifacts(
    candidate_id: str,
    *,
    spec_artifact_id: str,
    glb_artifact_id: str,
    report_artifact_id: str,
) -> None:
    """Atomically replace public pointers for one criteria-passing repair."""

    values = {
        "bluecad_spec": spec_artifact_id,
        "bluecad_glb": glb_artifact_id,
        "bluecad_report": report_artifact_id,
    }
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise ValueError("successful structural artifacts must be non-empty ids")
    with open_sqlite_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        owner = connection.execute(
            "SELECT workspace_id, status FROM bluecad_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if owner is None or str(owner["status"]) != "valid":
            connection.rollback()
            raise ValueError("structural artifact commit requires a valid candidate")
        workspace_id = str(owner["workspace_id"])
        placeholders = ", ".join("?" for _ in values)
        rows = connection.execute(
            f"""
            SELECT id, artifact_type, workspace_id
            FROM artifacts
            WHERE id IN ({placeholders})
            """,
            tuple(values.values()),
        ).fetchall()
        actual = {
            str(row["artifact_type"]): str(row["id"])
            for row in rows
            if str(row["workspace_id"]) == workspace_id
        }
        if actual != values:
            connection.rollback()
            raise ValueError(
                "structural artifacts do not match candidate ownership and roles"
            )
        cursor = connection.execute(
            """
            UPDATE bluecad_candidates
            SET spec_artifact_id = ?, glb_artifact_id = ?, report_artifact_id = ?,
                updated_at = ?
            WHERE id = ? AND status = 'valid'
            """,
            (
                spec_artifact_id,
                glb_artifact_id,
                report_artifact_id,
                utc_now(),
                candidate_id,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise ValueError("candidate lost structural artifact commit authority")
        connection.commit()
