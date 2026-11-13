import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.core.ai_thread_schema import (
    AI_THREAD_SCHEMA_INDEX_STATEMENTS,
    AI_THREAD_SCHEMA_MIGRATION_RECORD,
    AI_THREAD_SCHEMA_STATEMENTS,
    AI_THREAD_TURN_EXTRAS_MIGRATION_RECORD,
)
from app.core.bluecad_studio_schema import (
    BLUECAD_STUDIO_SCHEMA_INDEX_STATEMENTS,
    BLUECAD_STUDIO_SCHEMA_MIGRATION_RECORD,
    BLUECAD_STUDIO_SCHEMA_STATEMENTS,
)
from app.core.brainstorm_schema import (
    BRAINSTORM_SCHEMA_INDEX_STATEMENTS,
    BRAINSTORM_SCHEMA_MIGRATION_RECORD,
    BRAINSTORM_SCHEMA_STATEMENTS,
)
from app.core.cad_link_schema import (
    CAD_LINK_SCHEMA_INDEX_STATEMENTS,
    CAD_LINK_SCHEMA_MIGRATION_RECORD,
    CAD_LINK_SCHEMA_STATEMENTS,
)
from app.core.cloud_escalation_schema import (
    CLOUD_ESCALATION_INDEX_STATEMENTS,
    CLOUD_ESCALATION_MIGRATION_RECORD,
    CLOUD_ESCALATION_SCHEMA_STATEMENTS,
)
from app.core.conversation_intake_schema import (
    CONVERSATION_INTAKE_MIGRATION_RECORD,
    CONVERSATION_INTAKE_SCHEMA_INDEX_STATEMENTS,
    CONVERSATION_INTAKE_SCHEMA_STATEMENTS,
)
from app.core.development_schema import (
    DEVELOPMENT_SCHEMA_INDEX_STATEMENTS,
    DEVELOPMENT_SCHEMA_MIGRATION_RECORD,
    DEVELOPMENT_SCHEMA_STATEMENTS,
)
from app.core.egress_schema import (
    EGRESS_SCHEMA_INDEX_STATEMENTS,
    EGRESS_SCHEMA_MIGRATION_RECORD,
    EGRESS_SCHEMA_MIGRATION_STATEMENTS,
    EGRESS_SCHEMA_STATEMENTS,
)
from app.core.grade_schema import (
    GRADE_SCHEMA_INDEX_STATEMENTS,
    GRADE_SCHEMA_MIGRATION_RECORD,
    GRADE_SCHEMA_STATEMENTS,
)
from app.core.literature_schema import (
    LITERATURE_SCHEMA_INDEX_STATEMENTS,
    LITERATURE_SCHEMA_MIGRATION_RECORD,
    LITERATURE_SCHEMA_STATEMENTS,
)
from app.core.paths import build_paths
from app.core.print_handoff_schema import (
    PRINT_HANDOFF_MIGRATION_RECORD,
    PRINT_HANDOFF_SCHEMA_STATEMENTS,
)
from app.core.project_knowledge_schema import (
    PROJECT_KNOWLEDGE_INDEX_STATEMENTS,
    PROJECT_KNOWLEDGE_MIGRATION_RECORD,
    PROJECT_KNOWLEDGE_MIGRATION_STATEMENTS,
    PROJECT_KNOWLEDGE_SCHEMA_STATEMENTS,
    SIM_RESULT_INGESTION_MIGRATION_RECORD,
    SIM_RESULT_VALUE_VERSIONS_MIGRATION_RECORD,
)
from app.core.relay_gateway_schema import (
    RELAY_GATEWAY_INDEX_STATEMENTS,
    RELAY_GATEWAY_MIGRATION_RECORD,
    RELAY_GATEWAY_MIGRATION_STATEMENTS,
    RELAY_GATEWAY_SCHEMA_STATEMENTS,
)
from app.core.schema import (
    CONTEXT_RECORDS_FTS_BACKFILL_STATEMENT,
    SCHEMA_FTS_STATEMENTS,
    SCHEMA_INDEX_STATEMENTS,
    SCHEMA_MIGRATION_RECORDS,
    SCHEMA_MIGRATION_STATEMENTS,
    SCHEMA_STATEMENTS,
)
from app.core.sensitivity_schema import (
    SENSITIVITY_SCHEMA_INDEX_STATEMENTS,
    SENSITIVITY_SCHEMA_STATEMENTS,
)
from app.core.token_flow_schema import (
    TOKEN_FLOW_SCHEMA_INDEX_STATEMENTS,
    TOKEN_FLOW_SCHEMA_MIGRATION_RECORD,
    TOKEN_FLOW_SCHEMA_MIGRATION_STATEMENTS,
    TOKEN_FLOW_SCHEMA_STATEMENTS,
)
from app.core.workspace_actions_schema import (
    WORKSPACE_ACTIONS_MIGRATION_RECORD,
    WORKSPACE_ACTIONS_SCHEMA_STATEMENTS,
)
from app.modules.events.service import utc_now
from app.modules.project_knowledge.sim_result_versions import backfill_simulation_result_value_versions

RETRIEVAL_INDEX_MAX_PAGE_COUNT = 262_144  # 1 GiB at SQLite's default 4 KiB page size.

RUNNER_CREATE_REQUEST_MIGRATION_STATEMENTS = (
    "ALTER TABLE runner_jobs ADD COLUMN request_key TEXT",
)
RUNNER_CREATE_REQUEST_INDEX_STATEMENTS = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_runner_jobs_workspace_request_key "
    "ON runner_jobs(workspace_id, request_key) WHERE request_key IS NOT NULL",
)
PARAMETER_LIFECYCLE_MIGRATION_RECORD = {
    "migration_id": "0016_parameter_lifecycle",
    "name": "Parameter lifecycle state",
    "checksum": None,
}
PARAMETER_LIFECYCLE_INDEX_STATEMENT = (
    "CREATE INDEX IF NOT EXISTS idx_parameters_workspace_lifecycle "
    "ON parameters(workspace_id, lifecycle_state)"
)


@dataclass(frozen=True)
class SchemaMigrationInfo:
    migration_id: str | None
    name: str | None
    applied_at: str | None
    checksum: str | None
    status: str | None


@dataclass(frozen=True)
class DatabaseInfo:
    engine: str
    database_file: str
    configured: bool
    ready: bool
    initialized: bool
    bootstrap_required: bool
    bootstrap_action: str | None
    schema_current: SchemaMigrationInfo
    applied_migration_count: int


def get_database_path() -> Path:
    return build_paths().database_file


@contextmanager
def open_sqlite_connection(database_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    database_path = database_path or get_database_path()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def open_retrieval_index_connection() -> Iterator[sqlite3.Connection]:
    """Open the disposable retrieval database, separate from canonical migrations."""
    with open_sqlite_connection(build_paths().retrieval_index_file) as connection:
        # Retrieval is rebuildable derived state. Bound its disk footprint so a corpus
        # or vector-index regression fails explicitly instead of exhausting the data root.
        connection.execute(f"PRAGMA max_page_count = {RETRIEVAL_INDEX_MAX_PAGE_COUNT}")
        yield connection


def initialize_database() -> DatabaseInfo:
    with open_sqlite_connection() as connection:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in CAD_LINK_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in BLUECAD_STUDIO_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in SENSITIVITY_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in EGRESS_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in TOKEN_FLOW_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in GRADE_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in AI_THREAD_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in CLOUD_ESCALATION_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in RELAY_GATEWAY_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in PROJECT_KNOWLEDGE_SCHEMA_STATEMENTS:
            connection.execute(statement)
        backfill_simulation_result_value_versions(connection)
        for statement in LITERATURE_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in DEVELOPMENT_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in BRAINSTORM_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in CONVERSATION_INTAKE_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in WORKSPACE_ACTIONS_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in PRINT_HANDOFF_SCHEMA_STATEMENTS:
            connection.execute(statement)
        # Existing workspace data is the first Design project version. The
        # stable workspace id keeps every legacy Process/BlueCad/Studio ref valid.
        connection.execute("""
            INSERT INTO design_project_versions
                (id, project_id, data_workspace_id, parent_version_id, label, active, created_at)
            SELECT id, id, id, NULL, 'v1', 1, created_at FROM workspaces
            WHERE status != 'design_version'
              AND NOT EXISTS (SELECT 1 FROM design_project_versions v WHERE v.project_id = workspaces.id)
        """)
        for statement in [
            *SCHEMA_MIGRATION_STATEMENTS,
            *EGRESS_SCHEMA_MIGRATION_STATEMENTS,
            *TOKEN_FLOW_SCHEMA_MIGRATION_STATEMENTS,
            *RUNNER_CREATE_REQUEST_MIGRATION_STATEMENTS,
            *PROJECT_KNOWLEDGE_MIGRATION_STATEMENTS,
            *RELAY_GATEWAY_MIGRATION_STATEMENTS,
        ]:
            try:
                connection.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        _ensure_parameter_lifecycle_schema(connection)
        for statement in SCHEMA_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in CAD_LINK_SCHEMA_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in BLUECAD_STUDIO_SCHEMA_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in SENSITIVITY_SCHEMA_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in EGRESS_SCHEMA_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in TOKEN_FLOW_SCHEMA_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in GRADE_SCHEMA_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in AI_THREAD_SCHEMA_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in CLOUD_ESCALATION_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in RELAY_GATEWAY_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in RUNNER_CREATE_REQUEST_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in PROJECT_KNOWLEDGE_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in LITERATURE_SCHEMA_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in DEVELOPMENT_SCHEMA_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in BRAINSTORM_SCHEMA_INDEX_STATEMENTS:
            connection.execute(statement)
        for statement in CONVERSATION_INTAKE_SCHEMA_INDEX_STATEMENTS:
            connection.execute(statement)
        if _sqlite_fts5_available(connection):
            for statement in SCHEMA_FTS_STATEMENTS:
                connection.execute(statement)
            connection.execute("DELETE FROM context_records_fts")
            connection.execute(CONTEXT_RECORDS_FTS_BACKFILL_STATEMENT)
        _record_schema_migrations(connection)
        connection.commit()
    return get_database_info()


def is_database_initialized() -> bool:
    database_path = get_database_path()
    if not database_path.exists():
        return False

    required_tables = {
        "schema_migrations",
        "workspaces",
        "events",
        "model_specs",
        "assumptions",
        "parameters",
        "requirements",
        "simulation_runs",
        "freshness_invalidations",
        "freshness_marks",
        "runner_jobs",
        "run_logs",
        "run_artifacts",
        "decisions",
        "ai_jobs",
        "bluecad_cad_links",
        "ai_settings",
        "ai_flows",
        "ai_flow_segments",
        "ai_flow_record_captures",
        "ai_flow_grade_subjects",
        "ai_flow_grade_events",
        "ai_threads",
        "ai_thread_interactions",
        "cloud_escalations",
        "relay_runs",
        "sensitivity_labels",
        "sanitized_derivatives",
        "egress_prompt_derivatives",
        "egress_packets",
        "egress_decisions",
        "egress_budget_reservations",
        "egress_confirmation_tickets",
        "egress_attempts",
        "sanitizer_audit_items",
        "workspace_egress_policy",
        "project_knowledge_drafts",
        "project_knowledge_revisions",
        "project_knowledge_approval_requests",
        "requirement_applicability",
        "simulation_run_scalar_results",
        "simulation_result_batches",
        "simulation_result_values",
        "simulation_result_checks",
        "simulation_result_resolutions",
        "project_knowledge_validation",
        "project_knowledge_reconciled_snapshots",
        "project_knowledge_reconciliation_requests",
        "literature_sources",
        "literature_entries",
        "roadmap_items",
        "roadmap_dependencies",
        "roadmap_object_links",
        "calendar_allocations",
        "brainstorm_raw_records",
        "brainstorm_ideas",
        "brainstorm_revisions",
        "brainstorm_discussions",
        "brainstorm_promotions",
        "brainstorm_idempotency",
        "workspace_actions",
        "bluecad_studio_references",
        "print_handoff_events",
        "ai_thread_surface_context",
        "ai_thread_turn_extras",
    }
    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    existing_tables = {row["name"] for row in rows}
    return required_tables.issubset(existing_tables)


def get_database_info() -> DatabaseInfo:
    database_path = get_database_path()
    initialized = is_database_initialized()
    schema_current = (
        get_current_schema_migration() if initialized else _empty_schema_migration()
    )
    applied_migration_count = count_schema_migrations() if initialized else 0
    return DatabaseInfo(
        engine="sqlite",
        database_file=str(database_path),
        configured=True,
        ready=database_path.parent.exists() and initialized,
        initialized=initialized,
        bootstrap_required=not initialized,
        bootstrap_action=None if initialized else "POST /system/initialize",
        schema_current=schema_current,
        applied_migration_count=applied_migration_count,
    )


def get_current_schema_migration() -> SchemaMigrationInfo:
    database_path = get_database_path()
    if not database_path.exists():
        return _empty_schema_migration()
    with open_sqlite_connection() as connection:
        has_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        if has_table is None:
            return _empty_schema_migration()
        row = connection.execute(
            """
            SELECT migration_id, name, applied_at, checksum, status
            FROM schema_migrations
            ORDER BY migration_id DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return _empty_schema_migration()
    return SchemaMigrationInfo(**dict(row))


def count_schema_migrations() -> int:
    database_path = get_database_path()
    if not database_path.exists():
        return 0
    with open_sqlite_connection() as connection:
        has_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        if has_table is None:
            return 0
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM schema_migrations"
        ).fetchone()
    return int(row["count"])


def _record_schema_migrations(connection: sqlite3.Connection) -> None:
    now = utc_now()
    records = [
        *SCHEMA_MIGRATION_RECORDS,
        CAD_LINK_SCHEMA_MIGRATION_RECORD,
        EGRESS_SCHEMA_MIGRATION_RECORD,
        TOKEN_FLOW_SCHEMA_MIGRATION_RECORD,
        GRADE_SCHEMA_MIGRATION_RECORD,
        AI_THREAD_SCHEMA_MIGRATION_RECORD,
        PARAMETER_LIFECYCLE_MIGRATION_RECORD,
        PROJECT_KNOWLEDGE_MIGRATION_RECORD,
        SIM_RESULT_INGESTION_MIGRATION_RECORD,
        SIM_RESULT_VALUE_VERSIONS_MIGRATION_RECORD,
        LITERATURE_SCHEMA_MIGRATION_RECORD,
        DEVELOPMENT_SCHEMA_MIGRATION_RECORD,
        BRAINSTORM_SCHEMA_MIGRATION_RECORD,
        CLOUD_ESCALATION_MIGRATION_RECORD,
        RELAY_GATEWAY_MIGRATION_RECORD,
        WORKSPACE_ACTIONS_MIGRATION_RECORD,
        BLUECAD_STUDIO_SCHEMA_MIGRATION_RECORD,
        PRINT_HANDOFF_MIGRATION_RECORD,
        CONVERSATION_INTAKE_MIGRATION_RECORD,
        AI_THREAD_TURN_EXTRAS_MIGRATION_RECORD,
    ]
    for record in records:
        connection.execute(
            """
            INSERT INTO schema_migrations (migration_id, name, applied_at, checksum, status)
            VALUES (?, ?, ?, ?, 'applied')
            ON CONFLICT(migration_id) DO UPDATE SET
                name = excluded.name,
                checksum = excluded.checksum,
                status = 'applied'
            """,
            (record["migration_id"], record["name"], now, record["checksum"]),
        )


def _ensure_parameter_lifecycle_schema(connection: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(parameters)").fetchall()
    }
    if "lifecycle_state" not in columns:
        connection.execute(
            "ALTER TABLE parameters ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'active'"
        )
        connection.execute(
            """
            UPDATE parameters
            SET lifecycle_state = CASE
                WHEN status = 'superseded' THEN 'superseded'
                ELSE 'active'
            END
            """
        )
    connection.execute(PARAMETER_LIFECYCLE_INDEX_STATEMENT)


def _empty_schema_migration() -> SchemaMigrationInfo:
    return SchemaMigrationInfo(
        migration_id=None,
        name=None,
        applied_at=None,
        checksum=None,
        status=None,
    )


def _sqlite_fts5_available(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp.jarvisos_fts5_probe USING fts5(x)"
        )
        connection.execute("DROP TABLE IF EXISTS temp.jarvisos_fts5_probe")
    except sqlite3.OperationalError:
        return False
    return True
