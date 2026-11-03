WORKSPACE_ACTIONS_MIGRATION_RECORD = {
    "migration_id": "0023_workspace_actions",
    "name": "Governed workspace action outcomes",
    "checksum": None,
}

WORKSPACE_ACTIONS_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS workspace_actions (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        surface TEXT NOT NULL,
        state TEXT NOT NULL,
        tier TEXT NOT NULL,
        origin_json TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        interaction_id TEXT,
        relay_run_id TEXT,
        request_json TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        outcome_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_workspace_actions_thread ON workspace_actions(workspace_id, thread_id, created_at)",
)
