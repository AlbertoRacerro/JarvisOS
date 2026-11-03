from pathlib import Path


def test_development_schema_is_initialized_and_recorded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))

    from app.core.config import get_settings
    from app.core.database import initialize_database, open_sqlite_connection
    from app.core.schema import CURRENT_SCHEMA_MIGRATION_ID

    get_settings.cache_clear()
    info = initialize_database()

    assert info.ready is True
    assert info.schema_current.migration_id == CURRENT_SCHEMA_MIGRATION_ID

    with open_sqlite_connection() as connection:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        migration = connection.execute(
            "SELECT status FROM schema_migrations WHERE migration_id = ?",
            ("0019_roadmap_calendar",),
        ).fetchone()

    assert {
        "roadmap_items",
        "roadmap_dependencies",
        "roadmap_object_links",
        "calendar_allocations",
    }.issubset(tables)
    assert migration is not None
    assert migration["status"] == "applied"
