from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.core.repository import row_to_model, rows_to_models
from app.modules.events.service import utc_now
from app.modules.files.models import ArtifactCreate, ArtifactRecord
from app.modules.files.registry import ArtifactRegistry


def create_artifact_registry() -> ArtifactRegistry:
    return ArtifactRegistry()


def create_artifact_record(workspace_id: str, payload: ArtifactCreate) -> ArtifactRecord:
    record_id = str(uuid4())
    created_at = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO artifacts (
                id, workspace_id, filename, stored_path, artifact_type, mime_type,
                sha256, source_ref, status, created_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                workspace_id,
                payload.filename,
                payload.stored_path,
                payload.artifact_type,
                payload.mime_type,
                payload.sha256,
                payload.source_ref,
                payload.status,
                created_at,
                payload.notes,
            ),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM artifacts WHERE id = ?", (record_id,)).fetchone()
    return row_to_model(row, ArtifactRecord)


def list_artifact_records(workspace_id: str) -> list[ArtifactRecord]:
    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM artifacts WHERE workspace_id = ? ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
    return rows_to_models(rows, ArtifactRecord)
