"""Persistence helpers for the BLUECAD candidate and attempt ledger."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.core.repository import row_to_model, rows_to_models
from app.modules.bluecad.export import sha256_file
from app.modules.bluecad.models import BluecadAttemptRead, BluecadCandidateRead, BluecadLoopConfig
from app.modules.events.service import utc_now


def brief_digest(brief_text: str) -> str:
    return "sha256:" + hashlib.sha256(brief_text.encode("utf-8")).hexdigest()


def create_candidate_record(workspace_id: str, brief_text: str, loop_config: BluecadLoopConfig) -> BluecadCandidateRead:
    now = utc_now()
    candidate_id = str(uuid4())
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        connection.execute(
            """
            INSERT INTO bluecad_candidates (
                id, workspace_id, brief_text, brief_digest, status, parked_reason,
                spec_artifact_id, glb_artifact_id, report_artifact_id,
                promoted_decision_id, origin, parent_candidate_id,
                loop_config_json, created_at, updated_at, notes
            ) VALUES (?, ?, ?, ?, 'generating', NULL, NULL, NULL, NULL, NULL,
                'ai', NULL, ?, ?, ?, NULL)
            """,
            (candidate_id, workspace_id, brief_text, brief_digest(brief_text), loop_config.model_dump_json(), now, now),
        )
        connection.commit()
    return get_candidate(workspace_id, candidate_id)  # type: ignore[return-value]


def start_attempt(candidate_id: str, attempt_no: int, route_class: str, *, prompt_version: str) -> BluecadAttemptRead:
    now = utc_now()
    attempt_id = str(uuid4())
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO bluecad_attempts (
                id, candidate_id, attempt_no, route_class, proposal_outcome,
                started_at, error_detail_json
            ) VALUES (?, ?, ?, ?, 'blocked', ?, ?)
            """,
            (attempt_id, candidate_id, attempt_no, route_class, now, json.dumps({"prompt_version": prompt_version}, sort_keys=True)),
        )
        connection.execute("UPDATE bluecad_candidates SET status = 'generating', updated_at = ? WHERE id = ?", (now, candidate_id))
        connection.commit()
    return get_attempt(attempt_id)  # type: ignore[return-value]


def finish_attempt(
    attempt_id: str,
    *,
    proposal_ai_job_id: str | None = None,
    proposal_outcome: str | None = None,
    build_outcome: str | None = None,
    validation_verdict: str | None = None,
    spec_artifact_id: str | None = None,
    report_artifact_id: str | None = None,
    manifest_artifact_id: str | None = None,
    error_detail: dict[str, object] | None = None,
) -> BluecadAttemptRead:
    existing = get_attempt(attempt_id)
    details: dict[str, object] = {}
    if existing and existing.error_detail_json:
        try:
            loaded = json.loads(existing.error_detail_json)
            if isinstance(loaded, dict):
                details.update(loaded)
        except json.JSONDecodeError:
            details["previous_error_detail_json"] = existing.error_detail_json
    if error_detail:
        details.update(error_detail)
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE bluecad_attempts
            SET proposal_ai_job_id = COALESCE(?, proposal_ai_job_id),
                proposal_outcome = COALESCE(?, proposal_outcome),
                build_outcome = COALESCE(?, build_outcome),
                validation_verdict = COALESCE(?, validation_verdict),
                spec_artifact_id = COALESCE(?, spec_artifact_id),
                report_artifact_id = COALESCE(?, report_artifact_id),
                manifest_artifact_id = COALESCE(?, manifest_artifact_id),
                finished_at = ?,
                error_detail_json = ?
            WHERE id = ?
            """,
            (
                proposal_ai_job_id,
                proposal_outcome,
                build_outcome,
                validation_verdict,
                spec_artifact_id,
                report_artifact_id,
                manifest_artifact_id,
                utc_now(),
                json.dumps(details, sort_keys=True) if details else None,
                attempt_id,
            ),
        )
        connection.commit()
    return get_attempt(attempt_id)  # type: ignore[return-value]


def update_candidate_artifacts(candidate_id: str, *, spec_artifact_id: str | None, glb_artifact_id: str | None, report_artifact_id: str | None) -> None:
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE bluecad_candidates
            SET spec_artifact_id = COALESCE(?, spec_artifact_id),
                glb_artifact_id = COALESCE(?, glb_artifact_id),
                report_artifact_id = COALESCE(?, report_artifact_id),
                status = 'validating',
                updated_at = ?
            WHERE id = ?
            """,
            (spec_artifact_id, glb_artifact_id, report_artifact_id, utc_now(), candidate_id),
        )
        connection.commit()


def mark_candidate_valid(candidate_id: str) -> None:
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE bluecad_candidates SET status = 'valid', parked_reason = NULL, updated_at = ? WHERE id = ?",
            (utc_now(), candidate_id),
        )
        connection.commit()


def park_candidate(candidate_id: str, reason: str, *, notes: str | None = None) -> None:
    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE bluecad_candidates
            SET status = 'parked', parked_reason = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (reason, notes, now, candidate_id),
        )
        connection.commit()


def archive_candidate(workspace_id: str, candidate_id: str) -> BluecadCandidateRead | None:
    now = utc_now()
    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT status
            FROM bluecad_candidates
            WHERE workspace_id = ? AND id = ?
            """,
            (workspace_id, candidate_id),
        ).fetchone()
        if row is None:
            return None
        if str(row["status"]) in {"generating", "validating"}:
            raise ValueError("Active BLUECAD candidates may not be archived.")
        cursor = connection.execute(
            """
            UPDATE bluecad_candidates
            SET status = 'archived', updated_at = ?
            WHERE workspace_id = ? AND id = ?
              AND status NOT IN ('generating', 'validating')
            """,
            (now, workspace_id, candidate_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise ValueError("Active BLUECAD candidates may not be archived.")
        connection.commit()
    return get_candidate(workspace_id, candidate_id)


def mark_promoted(workspace_id: str, candidate_id: str, decision_id: str) -> BluecadCandidateRead:
    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE bluecad_candidates SET promoted_decision_id = ?, updated_at = ? WHERE workspace_id = ? AND id = ?",
            (decision_id, now, workspace_id, candidate_id),
        )
        connection.commit()
    return get_candidate(workspace_id, candidate_id)  # type: ignore[return-value]


def register_artifact(
    workspace_id: str,
    source_path: Path,
    *,
    role: str,
    source_ref: str,
    producer_notes: str = "Generated by BLUECAD AI loop v0.",
) -> str:
    artifact_id = str(uuid4())
    stored_path = _artifact_storage_path(workspace_id, artifact_id, source_path.name)
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, stored_path)
    mime_type = mimetypes.guess_type(stored_path.name)[0] or "application/octet-stream"
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO artifacts (
                id, workspace_id, filename, stored_path, artifact_type, mime_type,
                sha256, source_ref, status, created_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'registered', ?, ?)
            """,
            (
                artifact_id,
                workspace_id,
                stored_path.name,
                str(stored_path),
                role,
                mime_type,
                sha256_file(stored_path),
                source_ref,
                utc_now(),
                producer_notes,
            ),
        )
        connection.commit()
    return artifact_id


# Print-handoff exports written next to model.glb by export.py; registered so
# an inspected candidate can offer STL (mm) and STEP downloads.
EXPORT_ARTIFACT_ROLES = (
    ("model.stl", "bluecad_stl"),
    ("model.step", "bluecad_step"),
    ("model.3mf", "bluecad_3mf"),
)


def register_export_artifacts(
    workspace_id: str,
    out_dir: Path,
    *,
    source_ref: str,
    producer_notes: str = "Generated by BLUECAD AI loop v0.",
) -> dict[str, str]:
    registered: dict[str, str] = {}
    for filename, role in EXPORT_ARTIFACT_ROLES:
        path = out_dir / filename
        if path.is_file() and path.stat().st_size > 0:
            registered[role] = register_artifact(
                workspace_id,
                path,
                role=role,
                source_ref=source_ref,
                producer_notes=producer_notes,
            )
    return registered


def candidate_work_dir(workspace_id: str, candidate_id: str, attempt_no: int) -> Path:
    return build_paths().workspaces_dir / workspace_id / "bluecad" / candidate_id / f"attempt_{attempt_no:02d}"


def list_candidates(workspace_id: str) -> list[BluecadCandidateRead]:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        rows = connection.execute(
            "SELECT * FROM bluecad_candidates WHERE workspace_id = ? ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
    return [_with_attempts(row_to_model(row, BluecadCandidateRead)) for row in rows]


def get_candidate(workspace_id: str, candidate_id: str) -> BluecadCandidateRead | None:
    with open_sqlite_connection() as connection:
        return get_candidate_from_connection(connection, workspace_id, candidate_id)


def get_candidate_from_connection(
    connection: sqlite3.Connection,
    workspace_id: str,
    candidate_id: str,
) -> BluecadCandidateRead | None:
    row = connection.execute(
        "SELECT * FROM bluecad_candidates WHERE workspace_id = ? AND id = ?",
        (workspace_id, candidate_id),
    ).fetchone()
    if row is None:
        return None
    return _with_attempts_from_connection(connection, row_to_model(row, BluecadCandidateRead))


def get_attempt(attempt_id: str) -> BluecadAttemptRead | None:
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT * FROM bluecad_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if row is None:
        return None
    return row_to_model(row, BluecadAttemptRead)


def list_attempts(candidate_id: str) -> list[BluecadAttemptRead]:
    with open_sqlite_connection() as connection:
        return list_attempts_from_connection(connection, candidate_id)


def list_attempts_from_connection(
    connection: sqlite3.Connection,
    candidate_id: str,
) -> list[BluecadAttemptRead]:
    rows = connection.execute(
        "SELECT * FROM bluecad_attempts WHERE candidate_id = ? ORDER BY attempt_no ASC, id ASC",
        (candidate_id,),
    ).fetchall()
    return rows_to_models(rows, BluecadAttemptRead)


def _with_attempts(candidate: BluecadCandidateRead) -> BluecadCandidateRead:
    data = candidate.model_dump()
    data["attempts"] = list_attempts(candidate.id)
    return BluecadCandidateRead(**data)


def _with_attempts_from_connection(
    connection: sqlite3.Connection,
    candidate: BluecadCandidateRead,
) -> BluecadCandidateRead:
    data = candidate.model_dump()
    data["attempts"] = list_attempts_from_connection(connection, candidate.id)
    return BluecadCandidateRead(**data)


def _artifact_storage_path(workspace_id: str, artifact_id: str, filename: str) -> Path:
    safe_name = Path(filename).name
    return build_paths().artifacts_dir / workspace_id / "bluecad" / artifact_id / safe_name


def _require_workspace(connection: sqlite3.Connection, workspace_id: str) -> None:
    if connection.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone() is None:
        raise ValueError("Workspace not found.")


class ScriptedFakeBluecadAdapter:
    """Offline fake AIProviderAdapter for BLUECAD loop tests."""

    provider_id = "scaleway"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def health(self):  # pragma: no cover - not used by run_ai_task
        return None

    def list_models(self):  # pragma: no cover - not used by run_ai_task
        return []

    def complete(self, request):
        from app.modules.ai.contracts import AIResponse, AIUsage

        prompt = request.prompt or ""
        self.prompts.append(prompt)
        if not self.responses:
            raise RuntimeError("scripted fake BLUECAD adapter exhausted")
        text = self.responses.pop(0)
        return AIResponse(
            provider_id="scaleway",
            model_id=request.model_preference or "scripted-bluecad-fake",
            request_id=request.request_id,
            text=text,
            content=text,
            usage=AIUsage(provider_id="scaleway", model_id=request.model_preference or "scripted-bluecad-fake", input_tokens=1, output_tokens=1),
            safety_status="allowed",
        )

    def stream(self, request):  # pragma: no cover - not used
        raise NotImplementedError
