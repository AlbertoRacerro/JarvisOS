BRAINSTORM_SCHEMA_MIGRATION_RECORD = {
    "migration_id": "0020_brainstorm",
    "name": "Development brainstorm state",
    "checksum": None,
}

BRAINSTORM_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS brainstorm_raw_records (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        content TEXT NOT NULL,
        attachment_refs_json TEXT NOT NULL DEFAULT '[]',
        lineage_state TEXT NOT NULL DEFAULT 'NEW' CHECK (lineage_state IN ('NEW', 'DISCUSSED', 'RECONCILED', 'SUPERSEDED')),
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        CHECK (length(trim(content)) > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brainstorm_ideas (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        current_revision INTEGER NOT NULL CHECK (current_revision >= 1),
        lineage_state TEXT NOT NULL DEFAULT 'RECONCILED' CHECK (lineage_state IN ('NEW', 'DISCUSSED', 'RECONCILED', 'SUPERSEDED')),
        successor_idea_id TEXT,
        successor_revision INTEGER CHECK (successor_revision IS NULL OR successor_revision >= 1),
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (successor_idea_id) REFERENCES brainstorm_ideas(id),
        CHECK ((successor_idea_id IS NULL) = (successor_revision IS NULL)),
        CHECK (successor_idea_id IS NULL OR successor_idea_id <> id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brainstorm_revisions (
        idea_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        title TEXT NOT NULL,
        takeaway TEXT NOT NULL,
        synthesis TEXT NOT NULL,
        source_refs_json TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (idea_id, revision),
        FOREIGN KEY (idea_id) REFERENCES brainstorm_ideas(id),
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        CHECK (length(trim(title)) > 0),
        CHECK (length(trim(takeaway)) > 0),
        CHECK (length(trim(synthesis)) > 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brainstorm_discussions (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        target_type TEXT NOT NULL CHECK (target_type IN ('raw', 'idea')),
        target_id TEXT NOT NULL,
        target_revision INTEGER CHECK (target_revision IS NULL OR target_revision >= 1),
        source_refs_json TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brainstorm_revision_discussions (
        idea_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        discussion_id TEXT NOT NULL,
        PRIMARY KEY (idea_id, revision, discussion_id),
        FOREIGN KEY (idea_id, revision) REFERENCES brainstorm_revisions(idea_id, revision),
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (discussion_id) REFERENCES brainstorm_discussions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brainstorm_promotions (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        idea_id TEXT NOT NULL,
        source_revision INTEGER NOT NULL CHECK (source_revision >= 1),
        target TEXT NOT NULL CHECK (target IN ('roadmap', 'design', 'coding')),
        payload_json TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'accepted', 'rejected')),
        downstream_handoff_id TEXT,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (idea_id) REFERENCES brainstorm_ideas(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brainstorm_idempotency (
        workspace_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        operation TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        result_type TEXT NOT NULL,
        result_id TEXT NOT NULL,
        result_revision INTEGER,
        created_at TEXT NOT NULL,
        PRIMARY KEY (workspace_id, idempotency_key),
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
    )
    """,
)

BRAINSTORM_SCHEMA_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_brainstorm_raw_workspace_created ON brainstorm_raw_records(workspace_id, created_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS idx_brainstorm_ideas_workspace_updated ON brainstorm_ideas(workspace_id, updated_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS idx_brainstorm_revisions_workspace_idea ON brainstorm_revisions(workspace_id, idea_id, revision DESC)",
    "CREATE INDEX IF NOT EXISTS idx_brainstorm_discussions_target ON brainstorm_discussions(workspace_id, target_type, target_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS idx_brainstorm_revision_discussions_idea ON brainstorm_revision_discussions(workspace_id, idea_id, revision, discussion_id)",
    "CREATE INDEX IF NOT EXISTS idx_brainstorm_promotions_source ON brainstorm_promotions(workspace_id, idea_id, source_revision, created_at, id)",
)
