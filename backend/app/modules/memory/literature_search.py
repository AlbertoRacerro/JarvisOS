from __future__ import annotations

from app.core.database import open_sqlite_connection
from app.core.search_text import register_search
from app.modules.memory.literature_models import LiteratureEntryRead, LiteratureSourceRead

_LITERATURE_MATCH_LIMIT = 101


def _escape_like_literal(value: str) -> str:
    return value.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _source_read(row) -> LiteratureSourceRead:
    source_id = str(row["source_id"] if "source_id" in row.keys() else row["id"])
    return LiteratureSourceRead(
        id=source_id,
        workspace_id=str(row["workspace_id"]),
        title=str(row["title"]),
        source_kind=row["source_kind"],
        state=row["state"],
        citation=row["citation"],
        publisher=row["publisher"],
        published_year=row["published_year"],
        source_ref=f"literature_source:{source_id}",
        backing=None,
        entries=[],
        created_at=str(row["source_created_at"] if "source_created_at" in row.keys() else row["created_at"]),
        updated_at=str(row["source_updated_at"] if "source_updated_at" in row.keys() else row["updated_at"]),
    )


def _entry_read(row) -> LiteratureEntryRead:
    entry_id = str(row["entry_id"])
    return LiteratureEntryRead(
        id=entry_id,
        workspace_id=str(row["workspace_id"]),
        source_id=str(row["source_id"]),
        entry_kind=row["entry_kind"],
        statement=row["statement"],
        value_text=row["value_text"],
        value_number=row["value_number"],
        unit=row["unit"],
        status=row["entry_status"],
        locator_kind=row["locator_kind"],
        locator_start=row["locator_start"],
        locator_end=row["locator_end"],
        context_text=row["context_text"],
        provenance_ref=f"literature_entry:{entry_id}",
        used_by=[],
        created_at=str(row["entry_created_at"]),
        updated_at=str(row["entry_updated_at"]),
    )


def search_literature_sources(workspace_id: str, query: str) -> list[LiteratureSourceRead]:
    """Return bounded literal source/entry matches without detail-only owner reads."""
    with open_sqlite_connection() as connection:
        register_search(connection, query)
        workspace = connection.execute("SELECT 1 FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if workspace is None:
            raise ValueError("Workspace not found.")

        source_rows = connection.execute(
            """
            SELECT *
            FROM literature_sources
            WHERE workspace_id = ?
              AND (
                JARVIS_MATCH(title) < 99
                OR JARVIS_MATCH(citation) < 99
                OR JARVIS_MATCH(publisher) < 99
              )
            ORDER BY MIN(JARVIS_MATCH(title), JARVIS_MATCH(citation), JARVIS_MATCH(publisher)), created_at DESC, id ASC
            LIMIT ?
            """,
            (workspace_id, _LITERATURE_MATCH_LIMIT),
        ).fetchall()
        entry_rows = connection.execute(
            """
            SELECT
                ls.id AS source_id,
                ls.workspace_id,
                ls.title,
                ls.source_kind,
                ls.state,
                ls.citation,
                ls.publisher,
                ls.published_year,
                ls.created_at AS source_created_at,
                ls.updated_at AS source_updated_at,
                le.id AS entry_id,
                le.entry_kind,
                le.statement,
                le.value_text,
                le.value_number,
                le.unit,
                le.status AS entry_status,
                le.locator_kind,
                le.locator_start,
                le.locator_end,
                le.context_text,
                le.created_at AS entry_created_at,
                le.updated_at AS entry_updated_at
            FROM literature_entries AS le
            JOIN literature_sources AS ls
              ON ls.id = le.source_id
             AND ls.workspace_id = le.workspace_id
            WHERE le.workspace_id = ?
              AND (
                JARVIS_MATCH(le.statement) < 99
                OR JARVIS_MATCH(le.value_text) < 99
                OR JARVIS_MATCH(CAST(le.value_number AS TEXT)) < 99
                OR JARVIS_MATCH(le.unit) < 99
                OR JARVIS_MATCH(le.context_text) < 99
              )
            ORDER BY MIN(JARVIS_MATCH(le.statement), JARVIS_MATCH(le.value_text), JARVIS_MATCH(CAST(le.value_number AS TEXT)), JARVIS_MATCH(le.unit), JARVIS_MATCH(le.context_text)), le.created_at DESC, le.id ASC
            LIMIT ?
            """,
            (workspace_id, _LITERATURE_MATCH_LIMIT),
        ).fetchall()

    sources: dict[str, LiteratureSourceRead] = {}
    for row in source_rows:
        direct_source = _source_read(row)
        sources[direct_source.id] = direct_source
    for row in entry_rows:
        source_id = str(row["source_id"])
        source = sources.get(source_id)
        if source is None:
            source = _source_read(row)
            sources[source_id] = source
        source.entries.append(_entry_read(row))
    return list(sources.values())
