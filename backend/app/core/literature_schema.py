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
        quantity TEXT,
        conditions_json TEXT,
        ingestion_status TEXT,
        ingestion_reasons_json TEXT,
        batch_id TEXT,
        source_sha256 TEXT,
        extractor_id TEXT,
        extractor_version TEXT,
        ai_job_id TEXT,
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
    """
    CREATE TABLE IF NOT EXISTS literature_ingestion_batches (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('running', 'done', 'failed', 'model_unavailable', 'unavailable')),
        reason TEXT,
        proposals_count INTEGER NOT NULL DEFAULT 0,
        source_checked_count INTEGER NOT NULL DEFAULT 0,
        flagged_count INTEGER NOT NULL DEFAULT 0,
        duplicate_entry_ids_json TEXT NOT NULL DEFAULT '[]',
        truncated INTEGER NOT NULL DEFAULT 0,
        notification_state TEXT NOT NULL DEFAULT 'none' CHECK (notification_state IN ('none', 'pending', 'read')),
        ai_job_ids_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (source_id) REFERENCES literature_sources(id),
        UNIQUE(source_id, source_sha256, extractor_version)
    )
    """,
)

# Spec 297: additive columns for automatic value ingestion. Hand-typed entries keep NULL in all of them.
LITERATURE_INGESTION_MIGRATION_RECORD = {
    "migration_id": "0033_literature_ingestion",
    "name": "Literature value ingestion batches and source-checked entry columns",
    "checksum": None,
}

LITERATURE_SCHEMA_MIGRATION_STATEMENTS = (
    "ALTER TABLE literature_entries ADD COLUMN quantity TEXT",
    "ALTER TABLE literature_entries ADD COLUMN conditions_json TEXT",
    "ALTER TABLE literature_entries ADD COLUMN ingestion_status TEXT",
    "ALTER TABLE literature_entries ADD COLUMN ingestion_reasons_json TEXT",
    "ALTER TABLE literature_entries ADD COLUMN batch_id TEXT",
    "ALTER TABLE literature_entries ADD COLUMN source_sha256 TEXT",
    "ALTER TABLE literature_entries ADD COLUMN extractor_id TEXT",
    "ALTER TABLE literature_entries ADD COLUMN extractor_version TEXT",
    "ALTER TABLE literature_entries ADD COLUMN ai_job_id TEXT",
)

LITERATURE_SCHEMA_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_literature_sources_workspace_created ON literature_sources(workspace_id, created_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS idx_literature_entries_source_created ON literature_entries(source_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS idx_literature_entries_workspace_status ON literature_entries(workspace_id, status, entry_kind)",
    "CREATE INDEX IF NOT EXISTS idx_literature_entries_workspace_quantity ON literature_entries(workspace_id, quantity)",
    "CREATE INDEX IF NOT EXISTS idx_literature_ingestion_batches_source ON literature_ingestion_batches(source_id, created_at)",
)
