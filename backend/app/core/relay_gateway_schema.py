RELAY_GATEWAY_MIGRATION_RECORD = {
    "migration_id": "0022_relay_safe_workspace",
    "name": "Relay safe workspace, context releases and Sidecar relay runs",
    "checksum": None,
}

RELAY_GATEWAY_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS relay_context_releases (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        derivative_id TEXT NOT NULL,
        derivative_digest TEXT NOT NULL,
        scope TEXT NOT NULL CHECK (scope IN ('persistent', 'thread')),
        thread_id TEXT,
        purpose TEXT NOT NULL,
        released_by TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('active', 'revoked')),
        expires_at TEXT,
        revocation_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK ((scope = 'thread') = (thread_id IS NOT NULL)),
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (derivative_id) REFERENCES sanitized_derivatives(id),
        FOREIGN KEY (thread_id) REFERENCES ai_threads(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS relay_workspaces (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        access_mode TEXT NOT NULL CHECK (access_mode IN ('repository', 'derivative')),
        agent TEXT NOT NULL,
        path TEXT NOT NULL,
        base_commit TEXT,
        relay_session_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (thread_id) REFERENCES ai_threads(id),
        UNIQUE(thread_id, access_mode, agent)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS relay_runs (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        source_interaction_id TEXT,
        model TEXT,
        relay_workspace_id TEXT,
        agent TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed', 'denied')),
        reason_code TEXT,
        prompt_digest TEXT NOT NULL,
        prompt_source TEXT CHECK (prompt_source IN ('operator_attested', 'approved_derivative')),
        prompt_derivative_id TEXT,
        repository_level TEXT NOT NULL,
        access_mode TEXT NOT NULL CHECK (access_mode IN ('repository', 'derivative')),
        released_derivatives_json TEXT NOT NULL DEFAULT '[]',
        relay_session_id TEXT,
        continued_from_session_id TEXT,
        turn_index INTEGER NOT NULL DEFAULT 0,
        pid INTEGER,
        stop_reason TEXT,
        exit_code INTEGER,
        result_text TEXT,
        technical_details TEXT,
        action_context_json TEXT,
        actions_ingested INTEGER NOT NULL DEFAULT 0,
        head_commit TEXT,
        change_summary TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (thread_id) REFERENCES ai_threads(id),
        FOREIGN KEY (relay_workspace_id) REFERENCES relay_workspaces(id)
    )
    """,
]

# Spec 161: a Relay escalation is linked to its source turn and records the explicit model.
RELAY_GATEWAY_MIGRATION_STATEMENTS = [
    # Keep these for databases created before the columns were added to fresh installs.
    "ALTER TABLE relay_runs ADD COLUMN source_interaction_id TEXT",
    "ALTER TABLE relay_runs ADD COLUMN model TEXT",
    "ALTER TABLE relay_runs ADD COLUMN technical_details TEXT",
    "ALTER TABLE relay_runs ADD COLUMN action_context_json TEXT",
    "ALTER TABLE relay_runs ADD COLUMN actions_ingested INTEGER NOT NULL DEFAULT 0",
]

RELAY_GATEWAY_INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_relay_runs_thread ON relay_runs(thread_id, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_relay_runs_one_active ON relay_runs(thread_id) WHERE state IN ('queued', 'running')",
    "CREATE INDEX IF NOT EXISTS idx_relay_context_releases_scope ON relay_context_releases(workspace_id, state, scope, thread_id)",
]
