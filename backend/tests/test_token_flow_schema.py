import sqlite3

import pytest

from app.core.database import (
    count_schema_migrations,
    get_current_schema_migration,
    initialize_database,
    open_sqlite_connection,
)
from app.core.egress_schema import (
    EGRESS_SCHEMA_MIGRATION_STATEMENTS,
    EGRESS_SCHEMA_STATEMENTS,
)
from app.core.schema import (
    CURRENT_SCHEMA_MIGRATION_ID,
    SCHEMA_MIGRATION_STATEMENTS,
    SCHEMA_STATEMENTS,
)
from app.core.sensitivity_schema import SENSITIVITY_SCHEMA_STATEMENTS
from app.core.token_flow_schema import TOKEN_FLOW_SCHEMA_MIGRATION_ID


def test_token_flow_schema_fresh_bootstrap_is_complete_and_idempotent():
    first = initialize_database()
    first_count = count_schema_migrations()
    second = initialize_database()

    assert first.ready is True
    assert second.ready is True
    assert count_schema_migrations() == first_count
    assert get_current_schema_migration().migration_id == CURRENT_SCHEMA_MIGRATION_ID

    with open_sqlite_connection() as connection:
        tables = {
            row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(ai_jobs)").fetchall()}
        token_flow_migration = connection.execute(
            "SELECT status FROM schema_migrations WHERE migration_id = ?",
            (TOKEN_FLOW_SCHEMA_MIGRATION_ID,),
        ).fetchone()
        settings_columns = {row["name"] for row in connection.execute("PRAGMA table_info(ai_settings)").fetchall()}

    assert token_flow_migration is not None
    assert token_flow_migration["status"] == "applied"
    assert {"ai_flows", "ai_flow_segments"}.issubset(tables)
    assert {
        "flow_id",
        "flow_attempt_index",
        "execution_class",
        "adapter_invoked",
        "external_dispatch_state",
        "normalized_usage_source",
        "accounting_basis",
        "accounted_provider_spend_usd_decimal",
    }.issubset(job_columns)
    assert {
        "max_direct_continuations",
        "direct_continuation_policy_version",
    }.issubset(settings_columns)


def test_token_flow_upgrade_preserves_legacy_attempt_and_both_usage_contracts():
    _initialize_pre_token_flow_schema()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, route_reason_json, usage_source
            ) VALUES ('legacy-job', '2026-07-17T00:00:00Z', 'success', 'test', '{}', 'actual')
            """
        )
        connection.commit()

    initialize_database()

    with open_sqlite_connection() as connection:
        legacy = connection.execute(
            """
            SELECT usage_source, normalized_usage_source, execution_class, accounting_basis
            FROM ai_jobs WHERE id = 'legacy-job'
            """
        ).fetchone()
        assert legacy is not None
        assert legacy["usage_source"] == "actual"
        assert legacy["normalized_usage_source"] is None
        assert legacy["execution_class"] is None
        assert legacy["accounting_basis"] is None

        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, route_reason_json,
                normalized_usage_source, execution_class, adapter_invoked,
                external_dispatch_state, accounting_basis,
                accounted_provider_spend_usd_decimal
            ) VALUES (
                'no-execution-job', '2026-07-17T00:00:01Z', 'config_error', 'test', '{}',
                'none', 'none', 0, 'not_applicable', 'no_execution', '0'
            )
            """
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE ai_jobs SET normalized_usage_source = 'fabricated' WHERE id = 'legacy-job'")
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE ai_jobs SET usage_source = 'none' WHERE id = 'legacy-job'")
        connection.rollback()


def test_flow_attempt_order_is_unique_and_segments_remain_separate_from_ledger():
    initialize_database()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_flows (
                id, task_kind, state, created_at, updated_at
            ) VALUES ('flow-1', 'test', 'running', '2026-07-17T00:00:00Z', '2026-07-17T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, route_reason_json,
                flow_id, flow_attempt_index, normalized_usage_source,
                execution_class, adapter_invoked, external_dispatch_state,
                accounting_basis, accounted_provider_spend_usd_decimal
            ) VALUES (
                'attempt-1', '2026-07-17T00:00:01Z', 'success', 'test', '{}',
                'flow-1', 0, 'estimated', 'synthetic', 1, 'not_applicable',
                'synthetic_not_economic', '0'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO ai_flow_segments (
                id, flow_id, segment_index, originating_attempt_id, body_text,
                body_digest, byte_count, token_count, sensitivity_level,
                policy_binding_digest, continuation_guard_digest, created_at, expires_at
            ) VALUES (
                'segment-1', 'flow-1', 0, 'attempt-1', 'protected continuation body',
                'digest', 27, 3, 'S1', 'policy-digest', 'guard-digest',
                '2026-07-17T00:00:02Z', '2026-07-18T00:00:02Z'
            )
            """
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO ai_jobs (
                    id, created_at, status, task_kind, route_reason_json,
                    flow_id, flow_attempt_index
                ) VALUES (
                    'attempt-duplicate', '2026-07-17T00:00:03Z', 'failed', 'test', '{}',
                    'flow-1', 0
                )
                """
            )
        connection.rollback()

        job = connection.execute("SELECT * FROM ai_jobs WHERE id = 'attempt-1'").fetchone()
        segment = connection.execute("SELECT body_text FROM ai_flow_segments WHERE id = 'segment-1'").fetchone()

    assert job is not None
    assert "body_text" not in job.keys()
    assert segment is not None
    assert segment["body_text"] == "protected continuation body"


def _initialize_pre_token_flow_schema() -> None:
    with open_sqlite_connection() as connection:
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in SENSITIVITY_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in EGRESS_SCHEMA_STATEMENTS:
            connection.execute(statement)
        for statement in [
            *SCHEMA_MIGRATION_STATEMENTS,
            *EGRESS_SCHEMA_MIGRATION_STATEMENTS,
        ]:
            try:
                connection.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        connection.commit()
