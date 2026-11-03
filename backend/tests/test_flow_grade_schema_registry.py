from __future__ import annotations

from app.core.cad_link_schema import CAD_LINK_SCHEMA_MIGRATION_RECORD
from app.core.database import (
    count_schema_migrations,
    get_current_schema_migration,
    initialize_database,
    open_sqlite_connection,
)
from app.core.grade_schema import GRADE_SCHEMA_MIGRATION_ID
from app.core.schema import CURRENT_SCHEMA_MIGRATION_ID


def test_grade_schema_remains_registered_after_cad_link_migration() -> None:
    first = initialize_database()
    second = initialize_database()

    assert first.ready is True
    assert second.ready is True
    assert get_current_schema_migration().migration_id == CURRENT_SCHEMA_MIGRATION_ID
    assert count_schema_migrations() >= 19

    with open_sqlite_connection() as connection:
        migrations = {
            row["migration_id"]: row["status"]
            for row in connection.execute(
                "SELECT migration_id, status FROM schema_migrations"
            ).fetchall()
        }
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert migrations[GRADE_SCHEMA_MIGRATION_ID] == "applied"
    assert migrations[CAD_LINK_SCHEMA_MIGRATION_RECORD["migration_id"]] == "applied"
    assert migrations[CURRENT_SCHEMA_MIGRATION_ID] == "applied"
    assert {
        "ai_flow_grade_subjects",
        "ai_flow_grade_events",
        "bluecad_cad_links",
    } <= tables
