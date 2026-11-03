GRADE_SCHEMA_MIGRATION_ID = "0014_grade_0"
GRADE_SCHEMA_MIGRATION_NAME = "Flow outcome subjects and append-only human grades"
GRADE_SCHEMA_MIGRATION_RECORD = {
    "migration_id": GRADE_SCHEMA_MIGRATION_ID,
    "name": GRADE_SCHEMA_MIGRATION_NAME,
    "checksum": None,
}

GRADE_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS ai_flow_grade_subjects (
        id TEXT PRIMARY KEY,
        flow_id TEXT NOT NULL,
        terminal_attempt_id TEXT,
        subject_version INTEGER NOT NULL CHECK (subject_version > 0),
        flow_outcome_digest TEXT NOT NULL,
        final_accounting_digest TEXT NOT NULL,
        final_output_digest TEXT,
        subject_payload_json TEXT NOT NULL,
        valid INTEGER NOT NULL DEFAULT 1 CHECK (valid IN (0, 1)),
        invalidated_at TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (flow_id) REFERENCES ai_flows(id),
        FOREIGN KEY (terminal_attempt_id) REFERENCES ai_jobs(id),
        UNIQUE(flow_id, subject_version),
        UNIQUE(flow_id, flow_outcome_digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_flow_grade_events (
        id TEXT PRIMARY KEY,
        flow_id TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        subject_version INTEGER NOT NULL CHECK (subject_version > 0),
        flow_outcome_digest TEXT NOT NULL,
        event_index INTEGER NOT NULL CHECK (event_index > 0),
        action TEXT NOT NULL CHECK (action IN ('set', 'withdraw')),
        grade TEXT CHECK (grade IN ('useful', 'partly', 'rework', 'failed')),
        reason_codes_json TEXT NOT NULL DEFAULT '[]',
        note_text TEXT,
        actor TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('operator_ui', 'operator_api')),
        supersedes_event_id TEXT,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        schema_version TEXT NOT NULL DEFAULT 'grade-v0',
        policy_version TEXT NOT NULL DEFAULT 'grade-policy-v0',
        CHECK (
            (action = 'set' AND grade IS NOT NULL)
            OR (action = 'withdraw' AND grade IS NULL)
        ),
        FOREIGN KEY (flow_id) REFERENCES ai_flows(id),
        FOREIGN KEY (subject_id) REFERENCES ai_flow_grade_subjects(id),
        FOREIGN KEY (supersedes_event_id) REFERENCES ai_flow_grade_events(id),
        UNIQUE(subject_id, event_index),
        UNIQUE(subject_id, idempotency_key)
    )
    """,
]

GRADE_SCHEMA_INDEX_STATEMENTS = [
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_flow_grade_subjects_current
    ON ai_flow_grade_subjects(flow_id)
    WHERE valid = 1
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ai_flow_grade_subjects_flow_version
    ON ai_flow_grade_subjects(flow_id, subject_version DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ai_flow_grade_events_subject_index
    ON ai_flow_grade_events(subject_id, event_index DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ai_flow_grade_events_flow_created
    ON ai_flow_grade_events(flow_id, created_at)
    """,
]
