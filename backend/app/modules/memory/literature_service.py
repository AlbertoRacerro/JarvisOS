from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.modules.events.service import utc_now
from app.modules.memory.literature_models import (
    LiteratureBackingRead,
    LiteratureEntryCreate,
    LiteratureEntryRead,
    LiteratureSourceCreate,
    LiteratureSourcePage,
    LiteratureSourceRead,
    LiteratureUsedByRead,
)

MAX_LITERATURE_PAGE_SIZE = 100
MAX_ENTRIES_PER_SOURCE = 100
MAX_LOCATOR_TEXT_BYTES = 2_000_000
ALLOWED_PREVIEW_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/markdown",
    }
)
ALLOWED_LITERATURE_ARTIFACT_TYPES = frozenset({"literature_source"})
CURRENT_ARTIFACT_STATUS = "registered"


@dataclass(frozen=True)
class LiteratureContent:
    path: Path
    media_type: str
    filename: str


class LiteratureError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _require_workspace(connection, workspace_id: str) -> None:
    row = connection.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if row is None:
        raise LiteratureError("literature_workspace_not_found", "Workspace not found.", status_code=404)


def _source_row(connection, workspace_id: str, source_id: str):
    row = connection.execute(
        "SELECT * FROM literature_sources WHERE id = ? AND workspace_id = ?",
        (source_id, workspace_id),
    ).fetchone()
    if row is None:
        raise LiteratureError("literature_source_not_found", "Literature source not found.", status_code=404)
    return row


def _artifact_row_or_none(connection, workspace_id: str, artifact_id: str):
    return connection.execute(
        """
        SELECT id, workspace_id, filename, stored_path, artifact_type, mime_type, sha256, status
        FROM artifacts
        WHERE id = ? AND workspace_id = ?
        """,
        (artifact_id, workspace_id),
    ).fetchone()


def _artifact_row(connection, workspace_id: str, artifact_id: str):
    row = _artifact_row_or_none(connection, workspace_id, artifact_id)
    if row is None:
        raise LiteratureError("literature_artifact_not_found", "Backing artifact not found.", status_code=404)
    return row


def _artifact_is_literature_eligible(row) -> bool:
    return (
        str(row["artifact_type"] or "") in ALLOWED_LITERATURE_ARTIFACT_TYPES
        and str(row["status"] or "") == CURRENT_ARTIFACT_STATUS
    )


def _content_state(row) -> tuple[str, Path | None]:
    if not _artifact_is_literature_eligible(row):
        return "ineligible", None
    if str(row["mime_type"] or "") not in ALLOWED_PREVIEW_MIME_TYPES:
        return "unsupported", None
    try:
        paths = build_paths()
        data_root = paths.data_root.resolve()
        secrets_root = paths.secrets_dir.resolve()
        path = Path(str(row["stored_path"])).resolve()
        path.relative_to(data_root)
        try:
            path.relative_to(secrets_root)
        except ValueError:
            pass
        else:
            return "unsafe", None
    except (OSError, RuntimeError, ValueError):
        return "unsafe", None
    if not path.exists() or not path.is_file():
        return "missing", None
    return "available", path


def _backing_read(workspace_id: str, source_id: str, artifact_id: str, artifact_row) -> LiteratureBackingRead:
    if artifact_row is None:
        return LiteratureBackingRead(
            artifact_id=artifact_id,
            filename=None,
            mime_type=None,
            sha256=None,
            availability="missing",
            content_available=False,
            content_url=None,
        )
    availability, path = _content_state(artifact_row)
    return LiteratureBackingRead(
        artifact_id=str(artifact_row["id"]),
        filename=str(artifact_row["filename"]),
        mime_type=str(artifact_row["mime_type"]) if artifact_row["mime_type"] is not None else None,
        sha256=str(artifact_row["sha256"]) if artifact_row["sha256"] is not None else None,
        availability=availability,
        content_available=path is not None,
        content_url=(
            f"/workspaces/{workspace_id}/literature/sources/{source_id}/content"
            if path is not None
            else None
        ),
    )


def _used_by(connection, workspace_id: str, entry_id: str) -> list[LiteratureUsedByRead]:
    ref = f"literature_entry:{entry_id}"
    rows = connection.execute(
        """
        SELECT 'parameter' AS kind, id, name AS title
        FROM parameters
        WHERE workspace_id = ? AND source_ref = ?
        UNION ALL
        SELECT 'assumption' AS kind, id, statement AS title
        FROM assumptions
        WHERE workspace_id = ? AND source_ref = ?
        UNION ALL
        SELECT 'artifact' AS kind, id, filename AS title
        FROM artifacts
        WHERE workspace_id = ? AND source_ref = ?
        ORDER BY kind, id
        LIMIT 100
        """,
        (workspace_id, ref, workspace_id, ref, workspace_id, ref),
    ).fetchall()
    return [
        LiteratureUsedByRead(
            kind=row["kind"],
            record_id=str(row["id"]),
            title=str(row["title"] or row["id"]),
            ref=f"{row['kind']}:{row['id']}",
        )
        for row in rows
    ]


def _entry_read(connection, row) -> LiteratureEntryRead:
    entry_id = str(row["id"])
    return LiteratureEntryRead(
        id=entry_id,
        workspace_id=str(row["workspace_id"]),
        source_id=str(row["source_id"]),
        entry_kind=row["entry_kind"],
        statement=row["statement"],
        value_text=row["value_text"],
        value_number=row["value_number"],
        unit=row["unit"],
        status=row["status"],
        locator_kind=row["locator_kind"],
        locator_start=row["locator_start"],
        locator_end=row["locator_end"],
        context_text=row["context_text"],
        provenance_ref=f"literature_entry:{entry_id}",
        used_by=_used_by(connection, str(row["workspace_id"]), entry_id),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _source_read(connection, row) -> LiteratureSourceRead:
    workspace_id = str(row["workspace_id"])
    source_id = str(row["id"])
    artifact = None
    if row["artifact_id"] is not None:
        artifact_id = str(row["artifact_id"])
        artifact_row = _artifact_row_or_none(connection, workspace_id, artifact_id)
        artifact = _backing_read(workspace_id, source_id, artifact_id, artifact_row)
    entry_rows = connection.execute(
        """
        SELECT * FROM literature_entries
        WHERE source_id = ? AND workspace_id = ?
        ORDER BY created_at, id
        LIMIT ?
        """,
        (source_id, workspace_id, MAX_ENTRIES_PER_SOURCE),
    ).fetchall()
    return LiteratureSourceRead(
        id=source_id,
        workspace_id=workspace_id,
        title=str(row["title"]),
        source_kind=row["source_kind"],
        state=row["state"],
        citation=row["citation"],
        publisher=row["publisher"],
        published_year=row["published_year"],
        source_ref=f"literature_source:{source_id}",
        backing=artifact,
        entries=[_entry_read(connection, entry_row) for entry_row in entry_rows],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def list_literature_sources(workspace_id: str, *, offset: int = 0, limit: int = 50) -> LiteratureSourcePage:
    if offset < 0:
        raise LiteratureError("literature_invalid_offset", "offset must be non-negative")
    if limit < 1 or limit > MAX_LITERATURE_PAGE_SIZE:
        raise LiteratureError(
            "literature_invalid_limit",
            f"limit must be between 1 and {MAX_LITERATURE_PAGE_SIZE}",
        )
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        total_row = connection.execute(
            "SELECT COUNT(*) AS count FROM literature_sources WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT * FROM literature_sources
            WHERE workspace_id = ?
            ORDER BY created_at DESC, id
            LIMIT ? OFFSET ?
            """,
            (workspace_id, limit, offset),
        ).fetchall()
        total = int(total_row["count"])
        items = [_source_read(connection, row) for row in rows]
    next_offset = offset + len(items) if offset + len(items) < total else None
    return LiteratureSourcePage(items=items, offset=offset, limit=limit, total=total, next_offset=next_offset)


def get_literature_source(workspace_id: str, source_id: str) -> LiteratureSourceRead:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        return _source_read(connection, _source_row(connection, workspace_id, source_id))


def get_literature_entry(workspace_id: str, entry_id: str) -> LiteratureEntryRead:
    """Read one literature claim or datum within its owning workspace."""
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        row = connection.execute(
            "SELECT * FROM literature_entries WHERE workspace_id=? AND id=?",
            (workspace_id, entry_id),
        ).fetchone()
        if row is None:
            raise LiteratureError("literature_entry_not_found", "Literature entry not found.", status_code=404)
        return _entry_read(connection, row)


def _source_payload_matches(row, payload: LiteratureSourceCreate) -> bool:
    return (
        str(row["title"]) == payload.title
        and str(row["source_kind"]) == payload.source_kind
        and str(row["state"]) == payload.state
        and row["artifact_id"] == payload.artifact_id
        and row["citation"] == payload.citation
        and row["publisher"] == payload.publisher
        and row["published_year"] == payload.published_year
    )


def create_literature_source(workspace_id: str, payload: LiteratureSourceCreate) -> LiteratureSourceRead:
    now = utc_now()
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        if payload.artifact_id is not None:
            artifact_row = _artifact_row(connection, workspace_id, payload.artifact_id)
            if not _artifact_is_literature_eligible(artifact_row):
                raise LiteratureError(
                    "literature_artifact_ineligible",
                    "Backing artifact is not an eligible current Literature source.",
                    status_code=409,
                )
            existing = connection.execute(
                "SELECT * FROM literature_sources WHERE workspace_id = ? AND artifact_id = ?",
                (workspace_id, payload.artifact_id),
            ).fetchone()
            if existing is not None:
                if not _source_payload_matches(existing, payload):
                    raise LiteratureError(
                        "literature_duplicate_artifact_conflict",
                        "Backing artifact is already registered to a different literature source payload.",
                        status_code=409,
                    )
                return _source_read(connection, existing)
        if payload.request_key is not None:
            existing = connection.execute(
                "SELECT * FROM literature_sources WHERE workspace_id = ? AND request_key = ?",
                (workspace_id, payload.request_key),
            ).fetchone()
            if existing is not None:
                if not _source_payload_matches(existing, payload):
                    raise LiteratureError(
                        "literature_request_key_conflict",
                        "request_key was already used for a different source payload.",
                        status_code=409,
                    )
                return _source_read(connection, existing)
        source_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO literature_sources (
                id, workspace_id, title, source_kind, state, artifact_id, citation,
                publisher, published_year, request_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                workspace_id,
                payload.title,
                payload.source_kind,
                payload.state,
                payload.artifact_id,
                payload.citation,
                payload.publisher,
                payload.published_year,
                payload.request_key,
                now,
                now,
            ),
        )
        connection.commit()
        row = _source_row(connection, workspace_id, source_id)
        return _source_read(connection, row)


def _validate_locator(connection, workspace_id: str, source_row, payload: LiteratureEntryCreate) -> None:
    if payload.locator_kind is None:
        return
    if source_row["artifact_id"] is None:
        raise LiteratureError(
            "literature_locator_unverifiable",
            "Exact locator cannot be verified without a registered backing artifact.",
            status_code=422,
        )
    artifact = _artifact_row_or_none(connection, workspace_id, str(source_row["artifact_id"]))
    if artifact is None or not _artifact_is_literature_eligible(artifact):
        raise LiteratureError(
            "literature_locator_unverifiable",
            "Exact locator cannot be verified against the current backing artifact.",
            status_code=422,
        )
    availability, path = _content_state(artifact)
    if availability != "available" or path is None:
        raise LiteratureError(
            "literature_locator_unverifiable",
            "Exact locator cannot be verified against unavailable backing content.",
            status_code=422,
        )
    mime_type = str(artifact["mime_type"] or "")
    if payload.locator_kind != "line" or mime_type not in {"text/plain", "text/markdown"}:
        raise LiteratureError(
            "literature_locator_unsupported",
            "Exact locator verification is unsupported for this backing format.",
            status_code=422,
        )
    try:
        if path.stat().st_size > MAX_LOCATOR_TEXT_BYTES:
            raise LiteratureError(
                "literature_locator_unsupported",
                "Backing text is too large for bounded locator verification.",
                status_code=422,
            )
        lines = path.read_text(encoding="utf-8").splitlines()
    except LiteratureError:
        raise
    except (OSError, UnicodeError) as exc:
        raise LiteratureError(
            "literature_locator_unverifiable",
            "Backing text could not be read for locator verification.",
            status_code=422,
        ) from exc
    start = int(payload.locator_start or 0)
    end = int(payload.locator_end if payload.locator_end is not None else start)
    if start < 1 or end > len(lines):
        raise LiteratureError(
            "literature_locator_out_of_range",
            "Literature locator is outside the backing document range.",
            status_code=422,
        )


def create_literature_entry(
    workspace_id: str,
    source_id: str,
    payload: LiteratureEntryCreate,
) -> LiteratureEntryRead:
    now = utc_now()
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        source = _source_row(connection, workspace_id, source_id)
        _validate_locator(connection, workspace_id, source, payload)
        if payload.request_key is not None:
            existing = connection.execute(
                "SELECT * FROM literature_entries WHERE source_id = ? AND request_key = ?",
                (source_id, payload.request_key),
            ).fetchone()
            if existing is not None:
                comparable = (
                    existing["entry_kind"], existing["statement"], existing["value_text"],
                    existing["value_number"], existing["unit"], existing["status"],
                    existing["locator_kind"], existing["locator_start"], existing["locator_end"],
                    existing["context_text"],
                )
                requested = (
                    payload.entry_kind, payload.statement, payload.value_text, payload.value_number,
                    payload.unit, payload.status, payload.locator_kind, payload.locator_start,
                    payload.locator_end, payload.context_text,
                )
                if comparable != requested:
                    raise LiteratureError(
                        "literature_entry_request_key_conflict",
                        "request_key was already used for a different literature entry payload.",
                        status_code=409,
                    )
                return _entry_read(connection, existing)
        entry_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO literature_entries (
                id, workspace_id, source_id, entry_kind, statement, value_text,
                value_number, unit, status, locator_kind, locator_start, locator_end,
                context_text, request_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id, workspace_id, source_id, payload.entry_kind, payload.statement,
                payload.value_text, payload.value_number, payload.unit, payload.status,
                payload.locator_kind, payload.locator_start, payload.locator_end,
                payload.context_text, payload.request_key, now, now,
            ),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM literature_entries WHERE id = ? AND workspace_id = ?",
            (entry_id, workspace_id),
        ).fetchone()
        return _entry_read(connection, row)


def resolve_literature_content(workspace_id: str, source_id: str) -> LiteratureContent:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        source = _source_row(connection, workspace_id, source_id)
        if source["artifact_id"] is None:
            raise LiteratureError(
                "literature_content_unavailable",
                "This literature source has no registered backing artifact.",
                status_code=404,
            )
        artifact = _artifact_row_or_none(connection, workspace_id, str(source["artifact_id"]))
        if artifact is None:
            raise LiteratureError(
                "literature_content_unavailable",
                "Backing content is unavailable for safe preview.",
                status_code=404,
            )
        availability, path = _content_state(artifact)
        if availability != "available" or path is None:
            raise LiteratureError(
                "literature_content_unavailable",
                "Backing content is unavailable for safe preview.",
                status_code=404,
            )
        return LiteratureContent(
            path=path,
            media_type=str(artifact["mime_type"]),
            filename=str(artifact["filename"]),
        )
