import sqlite3

import pytest

from app.core.database import (
    count_schema_migrations,
    get_current_schema_migration,
    initialize_database,
    open_sqlite_connection,
)
from app.core.egress_schema import EGRESS_SCHEMA_MIGRATION_ID
from app.core.schema import (
    CURRENT_SCHEMA_MIGRATION_ID,
    SCHEMA_STATEMENTS,
)
from app.core.sensitivity_schema import SENSITIVITY_SCHEMA_STATEMENTS
from app.core.token_flow_schema import TOKEN_FLOW_SCHEMA_MIGRATION_ID

EXPECTED_EGRESS_TABLES = {
    "egress_prompt_derivatives",
    "egress_packets",
    "egress_decisions",
    "egress_budget_reservations",
    "egress_confirmation_tickets",
    "egress_attempts",
    "sanitizer_audit_items",
    "workspace_egress_policy",
}
EXPECTED_AI_JOB_USAGE_SOURCE_COLUMN = "usage_source"
EXPECTED_SANITIZER_PROVENANCE_COLUMNS = {
    "sanitizer_kind",
    "sanitizer_version",
    "sanitizer_config_digest",
    "sanitizer_ai_job_id",
    "approval_source",
    "auto_approved",
}


def test_egress_schema_remains_recorded_after_token_flow_migration():
    first = initialize_database()
    second = initialize_database()

    assert first.ready is True
    assert second.ready is True
    assert get_current_schema_migration().migration_id == CURRENT_SCHEMA_MIGRATION_ID
    assert count_schema_migrations() >= 19

    with open_sqlite_connection() as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        token_flow_migration = connection.execute(
            "SELECT status FROM schema_migrations WHERE migration_id = ?",
            (TOKEN_FLOW_SCHEMA_MIGRATION_ID,),
        ).fetchone()
        egress_migration = connection.execute(
            """
            SELECT status FROM schema_migrations WHERE migration_id = ?
            """,
            (EGRESS_SCHEMA_MIGRATION_ID,),
        ).fetchone()
    tables = {row["name"] for row in rows}
    assert EXPECTED_EGRESS_TABLES.issubset(tables)
    assert token_flow_migration is not None
    assert token_flow_migration["status"] == "applied"
    assert egress_migration is not None
    assert egress_migration["status"] == "applied"


def test_ai_job_usage_source_column_exists_and_rejects_invalid_values():
    initialize_database()
    initialize_database()

    with open_sqlite_connection() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(ai_jobs)").fetchall()}
        assert EXPECTED_AI_JOB_USAGE_SOURCE_COLUMN in columns
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO ai_jobs (
                    id, created_at, status, task_kind, route_reason_json, usage_source
                ) VALUES ('invalid-usage-source', '2026-07-15T00:00:00Z',
                          'success', 'general', '{}', 'invented')
                """
            )


def test_ai_job_usage_source_column_upgrades_predecessor_table():
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            CREATE TABLE ai_jobs (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL,
                task_kind TEXT NOT NULL, requested_route_class TEXT,
                selected_route_class TEXT, provider_id TEXT, model_id TEXT,
                route_reason_json TEXT NOT NULL, prompt_digest TEXT,
                context_digest TEXT, context_sources_json TEXT, output_digest TEXT,
                input_tokens INTEGER, output_tokens INTEGER, cost_estimate REAL,
                latency_ms INTEGER, error_type TEXT
            )
            """
        )
        connection.commit()

    initialize_database()

    with open_sqlite_connection() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(ai_jobs)").fetchall()}
    assert EXPECTED_AI_JOB_USAGE_SOURCE_COLUMN in columns


def test_sanitizer_provenance_columns_exist_on_fresh_database():
    initialize_database()
    initialize_database()

    with open_sqlite_connection() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(sanitized_derivatives)").fetchall()}

    assert EXPECTED_SANITIZER_PROVENANCE_COLUMNS.issubset(columns)


def test_sanitizer_provenance_columns_upgrade_immediate_predecessor_database():
    with open_sqlite_connection() as connection:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in SENSITIVITY_SCHEMA_STATEMENTS:
            connection.execute(statement)
        before = {row["name"] for row in connection.execute("PRAGMA table_info(sanitized_derivatives)").fetchall()}
        connection.commit()
    assert EXPECTED_SANITIZER_PROVENANCE_COLUMNS.isdisjoint(before)

    initialize_database()

    with open_sqlite_connection() as connection:
        after = {row["name"] for row in connection.execute("PRAGMA table_info(sanitized_derivatives)").fetchall()}
    assert EXPECTED_SANITIZER_PROVENANCE_COLUMNS.issubset(after)


def test_packet_schema_rejects_noneligible_final_level():
    initialize_database()
    with open_sqlite_connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO egress_packets (
                    id, packet_digest, operation, task_kind, route_class,
                    provider_id, model_id, fallback_index, prompt_digest,
                    packet_json, included_manifest_json, withheld_manifest_json,
                    sanitizer_failed_manifest_json, policy_capped_manifest_json,
                    budget_dropped_manifest_json, final_level, max_output_tokens,
                    policy_version, trigger_version, config_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, '[]', '[]', '[]', '[]', '[]',
                          'S2', 10, ?, ?, ?, ?)
                """,
                (
                    "packet-1",
                    "digest-1",
                    "external_provider_call",
                    "general",
                    "external:cheap",
                    "deepseek",
                    "deepseek-v4-pro",
                    "prompt-digest",
                    "{}",
                    "policy-v1",
                    "triggers-v1",
                    "config-digest",
                    "2026-07-13T00:00:00Z",
                ),
            )


def test_reservation_schema_has_unique_decision_and_bounded_state():
    initialize_database()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO egress_decisions (
                id, created_at, result, reason_code, operation, fallback_index,
                safe_input_digest, prompt_level, context_level, final_level,
                trigger_ids_json, confirmation_required, policy_version,
                trigger_version, config_digest
            ) VALUES (?, ?, 'deny', 'test-deny', 'external_provider_call', 0,
                      ?, 'S2', 'unknown', 'S2', '[]', 0, ?, ?, ?)
            """,
            (
                "decision-1",
                "2026-07-13T00:00:00Z",
                "safe-input-digest",
                "policy-v1",
                "triggers-v1",
                "config-digest",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO egress_budget_reservations (
                    id, decision_id, packet_digest, provider_id, model_id,
                    projected_input_tokens, projected_output_tokens,
                    projected_cost_upper_usd, state, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 1, 1, 0.1, 'invalid', ?, ?)
                """,
                (
                    "reservation-invalid",
                    "decision-1",
                    "packet-digest",
                    "deepseek",
                    "deepseek-v4-pro",
                    "2026-07-13T00:00:00Z",
                    "2026-07-13T00:05:00Z",
                ),
            )

        connection.execute(
            """
            INSERT INTO egress_budget_reservations (
                id, decision_id, packet_digest, provider_id, model_id,
                projected_input_tokens, projected_output_tokens,
                projected_cost_upper_usd, state, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, 1, 1, 0.1, 'active', ?, ?)
            """,
            (
                "reservation-1",
                "decision-1",
                "packet-digest",
                "deepseek",
                "deepseek-v4-pro",
                "2026-07-13T00:00:00Z",
                "2026-07-13T00:05:00Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO egress_budget_reservations (
                    id, decision_id, packet_digest, provider_id, model_id,
                    projected_input_tokens, projected_output_tokens,
                    projected_cost_upper_usd, state, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 1, 1, 0.1, 'active', ?, ?)
                """,
                (
                    "reservation-2",
                    "decision-1",
                    "packet-digest-2",
                    "deepseek",
                    "deepseek-v4-pro",
                    "2026-07-13T00:00:01Z",
                    "2026-07-13T00:05:01Z",
                ),
            )
