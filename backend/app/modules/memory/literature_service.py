from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.modules.engineering.evaluator_contracts import ResultCheck, Verdict
from app.modules.events.service import utc_now
from app.modules.files.models import ArtifactCreate
from app.modules.files.service import create_artifact_record
from app.modules.memory.literature_checks import check_datum
from app.modules.memory.literature_ingest import ingestion_eligibility
from app.modules.memory.literature_models import (
    LiteratureBackingRead,
    LiteratureEntryCreate,
    LiteratureEntryRead,
    LiteratureIngestionBatchRead,
    LiteratureIngestionCheckRead,
    LiteratureSourceCreate,
    LiteratureSourcePage,
    LiteratureSourceRead,
    LiteratureUsedByRead,
)
from app.modules.memory.pdf_text import PdfTextError, cached_pdf_pages

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
MAX_LITERATURE_UPLOAD_BYTES = 20 * 1024 * 1024
LITERATURE_UPLOAD_MIME_BY_SUFFIX = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".pdf": "application/pdf",
}


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


def _backing_read(workspace_id: str, source_id: str, artifact_id: str, artifact_row, backing) -> LiteratureBackingRead:
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
        page_text=backing.page_state() if str(artifact_row["mime_type"] or "") == "application/pdf" else None,
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


class _BackingText:
    """The located-text backing of one source, read at most once and only when a datum needs it."""

    def __init__(self, connection, workspace_id: str, source_row) -> None:
        self._connection = connection
        self._workspace_id = workspace_id
        self._artifact_id = source_row["artifact_id"]
        self._loaded = False
        self._lines: list[str] | None = None
        self._reason = ""
        self._sha_loaded = False
        self._sha256: str | None = None

    def current_sha256(self) -> str | None:
        """SHA-256 of the backing bytes as stored now; None when absent, unsafe or unreadable."""
        if not self._sha_loaded:
            self._sha_loaded = True
            artifact = (_artifact_row_or_none(self._connection, self._workspace_id, str(self._artifact_id))
                        if self._artifact_id is not None else None)
            availability, path = _content_state(artifact) if artifact is not None else ("missing", None)
            if availability == "available" and path is not None and not path.is_symlink():
                try:
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1 << 20), b""):
                            digest.update(chunk)
                    self._sha256 = digest.hexdigest()
                except OSError:
                    self._sha256 = None
        return self._sha256

    def get(self) -> tuple[list[str] | None, str]:
        if not self._loaded:
            self._loaded = True
            self._lines, self._reason = self._load()
        return self._lines, self._reason

    def is_pdf(self) -> bool:
        if self._artifact_id is None:
            return False
        artifact = _artifact_row_or_none(self._connection, self._workspace_id, str(self._artifact_id))
        return artifact is not None and artifact["mime_type"] == "application/pdf"

    def page_state(self) -> str:
        pages, reason = self.get()
        return "available" if pages is not None else ("no_text" if "no_text" in reason else "unreadable")

    def _load(self) -> tuple[list[str] | None, str]:
        if self._artifact_id is None:
            return None, "The source has no backing artifact."
        artifact = _artifact_row_or_none(self._connection, self._workspace_id, str(self._artifact_id))
        if artifact is None:
            return None, "The backing artifact is missing."
        availability, path = _content_state(artifact)
        if availability != "available" or path is None:
            return None, f"The backing content is {availability}."
        if str(artifact["mime_type"] or "") == "application/pdf":
            try:
                pages = cached_pdf_pages(path)
                return (pages, "") if pages else (None, "PDF page text is no_text.")
            except PdfTextError:
                return None, "PDF page text is unreadable (extractor unavailable or PDF invalid)."
        if str(artifact["mime_type"] or "") not in {"text/plain", "text/markdown"}:
            return None, "The backing is not a text or markdown file."
        try:
            if path.stat().st_size > MAX_LOCATOR_TEXT_BYTES:
                return None, "The backing text is too large to check."
            return _read_backing_lines(path), ""
        except (OSError, UnicodeError):
            return None, "The backing text could not be read."


def _read_backing_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _entry_read(connection, row, backing: _BackingText | None = None) -> LiteratureEntryRead:
    entry_id = str(row["id"])
    checks: list[ResultCheck] = []
    verdict: Verdict | None = None
    if row["entry_kind"] == "datum":
        has_line_locator = row["locator_kind"] in {"line", "page"} and row["locator_start"] is not None
        if has_line_locator:
            if backing is None:
                source_row = _source_row(connection, str(row["workspace_id"]), str(row["source_id"]))
                backing = _BackingText(connection, str(row["workspace_id"]), source_row)
            lines, unavailable = backing.get()
            if row["locator_kind"] == "line" and backing.is_pdf():
                lines, unavailable = None, "The backing is a PDF."
            if row["locator_kind"] == "page" and not backing.is_pdf():
                lines, unavailable = None, "The backing is not a PDF."
        else:
            lines, unavailable = None, ""
        checks, verdict = check_datum(
            value_number=row["value_number"],
            value_text=row["value_text"],
            unit=row["unit"],
            locator_kind=row["locator_kind"],
            locator_start=row["locator_start"],
            locator_end=row["locator_end"],
            lines=lines,
            unavailable=unavailable,
        )
    ingestion: dict[str, Any] = {}
    if row["ingestion_status"] is not None:
        if backing is None:
            source_row = _source_row(connection, str(row["workspace_id"]), str(row["source_id"]))
            backing = _BackingText(connection, str(row["workspace_id"]), source_row)
        eligible, stale, reasons = ingestion_eligibility(row, backing.current_sha256(), verdict)
        ingestion = {
            "quantity": row["quantity"],
            "conditions": json.loads(row["conditions_json"] or "[]"),
            "ingestion_status": row["ingestion_status"],
            "ingestion_checks": [LiteratureIngestionCheckRead(**item)
                                 for item in json.loads(row["ingestion_reasons_json"] or "[]")],
            "batch_id": row["batch_id"],
            "stale_source": stale,
            "eligible_for_later_admission": eligible,
            "eligibility_reasons": reasons,
        }
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
        checks=checks,
        verdict=verdict,
        **ingestion,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _latest_batch(connection, source_id: str, backing: _BackingText) -> LiteratureIngestionBatchRead | None:
    batch = connection.execute(
        "SELECT * FROM literature_ingestion_batches WHERE source_id = ? ORDER BY created_at DESC, id LIMIT 1",
        (source_id,),
    ).fetchone()
    if batch is None:
        return None
    return LiteratureIngestionBatchRead(
        id=str(batch["id"]),
        state=batch["state"],
        reason=batch["reason"],
        proposals_count=int(batch["proposals_count"]),
        source_checked_count=int(batch["source_checked_count"]),
        flagged_count=int(batch["flagged_count"]),
        duplicate_entry_ids=json.loads(batch["duplicate_entry_ids_json"] or "[]"),
        truncated=bool(batch["truncated"]),
        notification_state=batch["notification_state"],
        ai_job_ids=json.loads(batch["ai_job_ids_json"] or "[]"),
        current=batch["source_sha256"] == backing.current_sha256(),
        created_at=str(batch["created_at"]),
        updated_at=str(batch["updated_at"]),
    )


def _source_read(connection, row) -> LiteratureSourceRead:
    workspace_id = str(row["workspace_id"])
    source_id = str(row["id"])
    artifact = None
    if row["artifact_id"] is not None:
        artifact_id = str(row["artifact_id"])
        artifact_row = _artifact_row_or_none(connection, workspace_id, artifact_id)
        backing = _BackingText(connection, workspace_id, row)
        artifact = _backing_read(workspace_id, source_id, artifact_id, artifact_row, backing)
    else:
        backing = _BackingText(connection, workspace_id, row)
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
        entries=[_entry_read(connection, entry_row, backing) for entry_row in entry_rows],
        ingestion_batch=_latest_batch(connection, source_id, backing),
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
    if payload.locator_kind == "page" and mime_type == "application/pdf":
        try:
            pages = cached_pdf_pages(path)
        except PdfTextError:
            pages = []
        if not pages:
            raise LiteratureError("literature_locator_unsupported", "PDF page text is unavailable.", status_code=422)
        start = int(payload.locator_start or 0)
        end = int(payload.locator_end if payload.locator_end is not None else start)
        if start < 1 or end > len(pages):
            raise LiteratureError("literature_locator_out_of_range", "Literature locator is outside the backing document range.", status_code=422)
        return
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
        lines = _read_backing_lines(path)
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


def store_literature_file(workspace_id: str, filename: str, data: bytes) -> dict[str, object]:
    """Store uploaded paper bytes under the artifacts directory and register a literature_source artifact."""
    safe = Path(filename.replace("\\", "/")).name
    if not safe or len(safe) > 255:
        raise LiteratureError("literature_upload_filename_invalid", "Filename must be 1 to 255 characters.")
    suffix = Path(safe).suffix.lower()
    mime_type = LITERATURE_UPLOAD_MIME_BY_SUFFIX.get(suffix)
    if mime_type is None:
        raise LiteratureError(
            "literature_upload_type_unsupported",
            "Only .txt, .md, .markdown and .pdf files are accepted.",
            status_code=415,
        )
    if len(data) > MAX_LITERATURE_UPLOAD_BYTES:
        raise LiteratureError("literature_upload_too_large", "Upload exceeds 20 MiB.", status_code=413)
    if not data:
        raise LiteratureError("literature_upload_empty", "The file is empty.")
    if mime_type == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise LiteratureError("literature_upload_invalid", "The file is not a PDF.", status_code=422)
    else:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LiteratureError(
                "literature_upload_invalid", "A text file must be UTF-8.", status_code=422
            ) from exc
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
    digest = hashlib.sha256(data).hexdigest()
    artifacts_dir = build_paths().artifacts_dir
    target_dir = artifacts_dir / "literature" / workspace_id
    try:
        for directory in (artifacts_dir / "literature", target_dir):
            if directory.is_symlink():
                raise LiteratureError("literature_upload_unsafe", "Upload directory is not safe.", status_code=500)
        target_dir.mkdir(parents=True, exist_ok=True)
        if target_dir.is_symlink() or target_dir.resolve().parent.parent != artifacts_dir.resolve():
            raise LiteratureError("literature_upload_unsafe", "Upload directory is not safe.", status_code=500)
        target = target_dir / f"{digest}{suffix}"
        if target.is_symlink():
            raise LiteratureError("literature_upload_unsafe", "Upload target is not safe.", status_code=500)
        if not target.exists():
            handle, temp_name = tempfile.mkstemp(dir=target_dir, prefix=".upload-", suffix=".part")
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                os.replace(temp_name, target)
            except BaseException:
                Path(temp_name).unlink(missing_ok=True)
                raise
    except OSError as exc:
        raise LiteratureError(
            "literature_upload_failed", "The file could not be stored.", status_code=500
        ) from exc
    record = create_artifact_record(
        workspace_id,
        ArtifactCreate(
            filename=safe,
            stored_path=str(target),
            artifact_type="literature_source",
            mime_type=mime_type,
            sha256=digest,
            source_ref="literature_upload",
            status=CURRENT_ARTIFACT_STATUS,
        ),
    )
    return {
        "artifact_id": record.id,
        "filename": safe,
        "mime_type": mime_type,
        "sha256": digest,
        "size": len(data),
    }


def attach_literature_backing(workspace_id: str, source_id: str, artifact_id: str) -> LiteratureSourceRead:
    with open_sqlite_connection() as connection:
        _require_workspace(connection, workspace_id)
        source = _source_row(connection, workspace_id, source_id)
        if source["artifact_id"] is not None:
            raise LiteratureError(
                "literature_backing_exists", "This source already has a file.", status_code=409
            )
        artifact = _artifact_row(connection, workspace_id, artifact_id)
        if not _artifact_is_literature_eligible(artifact):
            raise LiteratureError(
                "literature_artifact_ineligible",
                "Backing artifact is not an eligible current Literature source.",
                status_code=409,
            )
        taken = connection.execute(
            "SELECT 1 FROM literature_sources WHERE workspace_id = ? AND artifact_id = ?",
            (workspace_id, artifact_id),
        ).fetchone()
        if taken is not None:
            raise LiteratureError(
                "literature_duplicate_artifact_conflict",
                "Backing artifact is already registered to a literature source.",
                status_code=409,
            )
        updated = connection.execute(
            "UPDATE literature_sources SET artifact_id = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND artifact_id IS NULL",
            (artifact_id, utc_now(), source_id, workspace_id),
        )
        if updated.rowcount != 1:
            raise LiteratureError(
                "literature_backing_exists", "This source already has a file.", status_code=409
            )
        connection.commit()
        return _source_read(connection, _source_row(connection, workspace_id, source_id))
