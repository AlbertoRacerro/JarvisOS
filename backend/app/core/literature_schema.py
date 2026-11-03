LITERATURE_SCHEMA_MIGRATION_RECORD = {
    "migration_id": "0018_literature_knowledge",
    "name": "Literature knowledge source and provenance records",
    "checksum": None,
}

LITERATURE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS literature_sources (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        title TEXT NOT NULL,
        source_kind TEXT NOT NULL CHECK (source_kind IN ('paper', 'book', 'report', 'standard', 'dataset', 'web', 'other')),
        state TEXT NOT NULL DEFAULT 'raw' CHECK (state IN ('raw', 'review', 'accepted')),
        artifact_id TEXT,
        citation TEXT,
        publisher TEXT,
        published_year INTEGER,
        request_key TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (artifact_id) REFERENCES artifacts(id),
        UNIQUE(workspace_id, artifact_id),
        UNIQUE(workspace_id, request_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS literature_entries (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        entry_kind TEXT NOT NULL CHECK (entry_kind IN ('claim', 'datum')),
        statement TEXT,
        value_text TEXT,
        value_number REAL,
        unit TEXT,
        status TEXT NOT NULL DEFAULT 'raw' CHECK (status IN ('raw', 'review', 'accepted')),
        locator_kind TEXT CHECK (locator_kind IN ('page', 'line', 'section')),
        locator_start INTEGER,
        locator_end INTEGER,
        context_text TEXT,
        request_key TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (source_id) REFERENCES literature_sources(id),
        UNIQUE(source_id, request_key),
        CHECK (locator_start IS NULL OR locator_start >= 0),
        CHECK (locator_end IS NULL OR locator_end >= 0),
        CHECK (locator_end IS NULL OR locator_start IS NULL OR locator_end >= locator_start),
        CHECK (
            (entry_kind = 'claim' AND statement IS NOT NULL AND length(trim(statement)) > 0)
            OR
            (entry_kind = 'datum' AND (
                value_number IS NOT NULL
                OR (value_text IS NOT NULL AND length(trim(value_text)) > 0)
            ))
        )
    )
    """,
)

LITERATURE_SCHEMA_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_literature_sources_workspace_created ON literature_sources(workspace_id, created_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS idx_literature_entries_source_created ON literature_entries(source_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS idx_literature_entries_workspace_status ON literature_entries(workspace_id, status, entry_kind)",
)
