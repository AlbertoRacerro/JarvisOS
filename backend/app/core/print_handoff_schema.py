PRINT_HANDOFF_MIGRATION_RECORD = {
    "migration_id": "0025_print_handoff_events",
    "name": "BLUECAD print handoff audit events",
    "checksum": None,
}

PRINT_HANDOFF_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS print_handoff_events (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        handoff_path TEXT NOT NULL,
        windows_path TEXT NOT NULL,
        outcome TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_print_handoff_events_artifact ON print_handoff_events(workspace_id, artifact_id, created_at)",
)
