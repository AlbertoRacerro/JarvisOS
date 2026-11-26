"""Append-only consult ledger (spec 380, migration 0038).

Every consult attempt is recorded, sent or not. Rows are never updated except an attempt's ``state``,
``ai_job_id`` and ``updated_at``. A refused S4 attempt stores only the packet hash and the reason, never the packet.
"""

CONSULT_SCHEMA_MIGRATION_RECORD = {
    "migration_id": "0038_consult_ledger",
    "name": "Expert consult attempts and answers ledger",
    "checksum": None,
}

CONSULT_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS consult_attempts (
        id TEXT PRIMARY KEY,
        workspace_id TEXT,
        thread_id TEXT,
        turn_id TEXT,
        kind TEXT NOT NULL,
        local_only INTEGER NOT NULL DEFAULT 0,
        packet_text TEXT,
        packet_sha256 TEXT NOT NULL,
        brief_sha256 TEXT,
        modules_json TEXT NOT NULL,
        gate_steps_json TEXT NOT NULL,
        result_code TEXT,
        rewrite_count INTEGER NOT NULL DEFAULT 0,
        mode TEXT NOT NULL CHECK (mode IN ('off', 'ask', 'auto')),
        account_id TEXT,
        model_id TEXT,
        sanitizer_kind TEXT NOT NULL DEFAULT 'model_local' CHECK (sanitizer_kind = 'model_local'),
        approval TEXT CHECK (approval IS NULL OR approval IN ('operator', 'policy_auto')),
        audit INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL CHECK (state IN ('refused', 'rewrite_required', 'awaiting_approval', 'approved',
                                             'rejected', 'sent', 'failed')),
        ai_job_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS consult_answers (
        id TEXT PRIMARY KEY,
        attempt_id TEXT NOT NULL UNIQUE,
        answer_text TEXT NOT NULL,
        answer_sha256 TEXT NOT NULL,
        model_id TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (attempt_id) REFERENCES consult_attempts(id)
    )
    """,
)

CONSULT_SCHEMA_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_consult_attempts_turn ON consult_attempts(turn_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_consult_attempts_state ON consult_attempts(state, created_at)",
)
