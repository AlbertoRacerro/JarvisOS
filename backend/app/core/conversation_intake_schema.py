"""Spec 223 external-conversation intake receipts.

A receipt is a metadata-only ingestion record: it never stores conversation text.
The text itself lives only in the retained source artifact and in the existing
Brainstorm/Literature owners the operator explicitly staged it into.
"""

from app.core.schema import SCHEMA_CONVERSATION_INTAKE_MIGRATION_ID, SCHEMA_CONVERSATION_INTAKE_MIGRATION_NAME

CONVERSATION_INTAKE_MIGRATION_RECORD = {
    "migration_id": SCHEMA_CONVERSATION_INTAKE_MIGRATION_ID,
    "name": SCHEMA_CONVERSATION_INTAKE_MIGRATION_NAME,
    "checksum": None,
}

CONVERSATION_INTAKE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS conversation_intake_receipts (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        format_version TEXT NOT NULL,
        conversation_digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'applied')),
        artifact_id TEXT,
        manifest_json TEXT NOT NULL,
        lease_token TEXT,
        lease_expires_at TEXT,
        supersedes_receipt_id TEXT,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        applied_at TEXT,
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (artifact_id) REFERENCES artifacts(id),
        FOREIGN KEY (supersedes_receipt_id) REFERENCES conversation_intake_receipts(id),
        UNIQUE (workspace_id, source_sha256)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_intake_items (
        workspace_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        receipt_id TEXT NOT NULL,
        message_locator TEXT NOT NULL,
        record_kind TEXT NOT NULL CHECK (record_kind IN ('user_goal', 'vendor_claim')),
        target_owner TEXT NOT NULL CHECK (target_owner IN ('brainstorm', 'literature', 'transcript')),
        target_type TEXT NOT NULL,
        target_id TEXT,
        text_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (workspace_id, item_key),
        FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
        FOREIGN KEY (receipt_id) REFERENCES conversation_intake_receipts(id)
    )
    """,
)

CONVERSATION_INTAKE_SCHEMA_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_conversation_intake_receipts_conversation "
    "ON conversation_intake_receipts(workspace_id, conversation_digest, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_conversation_intake_items_receipt "
    "ON conversation_intake_items(receipt_id)",
)
