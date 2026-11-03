"""Spec 028 — hold the additive migration policy written in docs/MIGRATIONS.md.

Test-only. Changes no schema, migration or runtime path. Every test isolates the
data root under pytest's tmp_path (or uses an in-memory SQLite connection).

Covers:
  - fresh == upgraded: drop migration-added columns except the pinned FK case, re-run
    initialize_database(), compare table/column/index shape with the fresh DB;
  - migration-only drift is exact and no new CREATE-only drift is introduced;
  - additive-only lint over every migration statement list;
  - unique migration ids across every migration record;
  - whole-initializer idempotence;
  - the explicit legacy allowlist stays honest.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pytest

# Columns present in an owner's CREATE TABLE and in NO migration statement, for
# which a pre-existing database could never gain the column on upgrade. Each entry
# is (table, column) -> reason. Empty today: the former members scaleway_token_cap
# and scaleway_tokens_month_to_date were repaired by catch-up migrations in
# SCHEMA_MIGRATION_STATEMENTS ("Repair (Stage 3-fix)").
LEGACY_CREATE_ONLY_COLUMNS: dict[tuple[str, str], str] = {}

# Baseline of columns in CREATE statements that predate the additive migrations.
# Pinning their complete set catches a new CREATE-only column: the synthetic
# legacy database starts from a fresh schema, so it cannot catch that case.
# An intentional new table changes this digest too and must be reviewed here.
# Reviewed for spec 194: bluecad_studio_references is a new table, not a new column on an old one.
# Reviewed for spec 165: print_handoff_events is a new table, not a new column on an old one.
# Reviewed for spec 214: design_project_versions, design_components, design_component_units,
# design_component_geometry and design_component_models are new tables, not new columns on old ones.
# Reviewed for spec 216: design_component_study_templates is a new CREATE IF NOT EXISTS
# table, created by the schema initializer for both fresh and existing databases.
# Reviewed for spec 223: conversation_intake_receipts and conversation_intake_items are new
# CREATE IF NOT EXISTS tables (migration 0027), not new columns on old ones.
# Reviewed for BUBU-2: ai_thread_turn_extras is a new CREATE IF NOT EXISTS table
# (migration 0028), not new columns on an old one.
BASE_CREATE_ONLY_SHA256 = "aa675b9a5976b6a9734184f27978f69cf1598e0d9733e529cf6ff6fb0b966778"

# KNOWN DRIFT (spec 028, recorded not fixed): columns added by a migration
# statement but absent from their owner's CREATE TABLE. A fresh database only has
# them because the migration lists also run on fresh databases, so the "column in
# both CREATE and migration" rule in docs/MIGRATIONS.md is not yet true for them.
# No schema change is made by spec 028; a later accepted spec may fold them into
# the CREATE statements, after which the stale-entry test forces removal here.
_MIGRATION_ONLY_BY_TABLE: dict[str, str] = {
    "sanitized_derivatives": (
        "sanitizer_kind sanitizer_version sanitizer_config_digest sanitizer_ai_job_id approval_source auto_approved"
    ),
    "requirements": (
        "basis_kind reconciliation_gate criterion_output_name criterion_operator criterion_expected_value criterion_expected_unit criterion_rule_version"
    ),
    "decisions": (
        "basis_lifecycle_state"
    ),
    "simulation_runs": (
        "project_knowledge_revision_id"
    ),
    "runner_jobs": (
        "request_key"
    ),
    "ai_jobs": (
        "flow_id flow_attempt_index parent_attempt_id fallback_index continuation_index execution_class adapter_invoked external_dispatch_state requested_output_ceiling effective_output_ceiling normalized_finish_reason normalized_usage_source cache_read_tokens reasoning_tokens accounting_basis accounted_provider_spend_usd_decimal outcome_reason capability_version pricing_version accounting_version"
    ),
    "ai_settings": (
        "max_direct_continuations direct_continuation_policy_version"
    ),
}
MIGRATION_ONLY_COLUMNS = {
    (table, column): "added only by a migration statement; absent from owner CREATE"
    for table, columns in _MIGRATION_ONLY_BY_TABLE.items()
    for column in columns.split()
}

# Migration-added columns SQLite cannot DROP in place (named in a FOREIGN KEY).
# The legacy-copy builder skips them; the fresh==upgraded test pins this set.
UNDROPPABLE_ADDED_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {("parameters", "supersedes_parameter_id")}
)

ADD_COLUMN_RE = re.compile(
    r"^ALTER\s+TABLE\s+(?P<table>\w+)\s+ADD\s+COLUMN\s+(?P<column>\w+)\s+(?P<definition>.+)$",
    re.IGNORECASE | re.DOTALL,
)
FORBIDDEN_TOKEN_RE = re.compile(r"\b(DROP|RENAME|ALTER\s+COLUMN)\b", re.IGNORECASE)
CREATE_IF_NOT_EXISTS_RE = re.compile(
    r"^CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+IF\s+NOT\s+EXISTS\b", re.IGNORECASE
)
# Triggers are recreated on every run: DROP TRIGGER IF EXISTS + CREATE TRIGGER.
# Guarded idempotent UPDATE backfills also live in the egress CREATE list.
TRIGGER_RE = re.compile(
    r"^(?:DROP\s+TRIGGER\s+IF\s+EXISTS|CREATE\s+TRIGGER|UPDATE)\b", re.IGNORECASE
)


def _isolate_data_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "jarvisos-discipline"))
    from app.core.config import get_settings

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Statement-list discovery (read from the real modules, not hand-copied)
# ---------------------------------------------------------------------------


def _database_module_lists() -> dict[str, list[str]]:
    import app.core.database as database

    found: dict[str, list[str]] = {}
    for name in dir(database):
        value = getattr(database, name)
        if name.endswith("_STATEMENTS") and isinstance(value, (list, tuple)):
            found[name] = list(value)
    return found


def _migration_lists() -> dict[str, list[str]]:
    return {n: s for n, s in _database_module_lists().items() if "MIGRATION" in n}


def _create_lists() -> dict[str, list[str]]:
    return {
        n: s
        for n, s in _database_module_lists().items()
        if "MIGRATION" not in n and "INDEX" not in n and "FTS" not in n
    }


def _index_lists() -> dict[str, list[str]]:
    return {
        n: s
        for n, s in _database_module_lists().items()
        if "INDEX" in n and "MIGRATION" not in n
    }


def _migration_records() -> list[dict]:
    import app.core.database as database

    records: list[dict] = []
    for name in dir(database):
        value = getattr(database, name)
        if name.endswith("_MIGRATION_RECORD") and isinstance(value, dict):
            records.append(value)
        elif name.endswith("_MIGRATION_RECORDS") and isinstance(value, (list, tuple)):
            records.extend(value)
    return records


def _added_columns(statements: Iterable[str]) -> list[tuple[str, str]]:
    columns = []
    for statement in statements:
        match = ADD_COLUMN_RE.match(" ".join(statement.split()))
        if match:
            columns.append((match["table"], match["column"]))
    return columns


def _all_added_columns() -> list[tuple[str, str]]:
    columns: list[tuple[str, str]] = []
    for statements in _migration_lists().values():
        columns.extend(_added_columns(statements))
    # The guarded backfill _ensure_parameter_lifecycle_schema adds this column
    # outside the lists; it is part of the upgrade shape too.
    columns.append(("parameters", "lifecycle_state"))
    return sorted(set(columns))


# ---------------------------------------------------------------------------
# Pure checks (also driven by the negative-case tests below)
# ---------------------------------------------------------------------------


def lint_migration_statements(statements: Iterable[str]) -> list[str]:
    """Return a violation message per statement that is not an additive ADD COLUMN."""
    violations = []
    for statement in statements:
        normalized = " ".join(statement.split())
        match = ADD_COLUMN_RE.match(normalized)
        if match is None:
            violations.append(f"not ALTER TABLE ... ADD COLUMN: {normalized}")
            continue
        if FORBIDDEN_TOKEN_RE.search(normalized):
            violations.append(f"contains DROP/RENAME/ALTER COLUMN: {normalized}")
        definition = match["definition"].upper()
        if "NOT NULL" in definition and "DEFAULT" not in definition:
            violations.append(f"NOT NULL column without DEFAULT: {normalized}")
        if "PRIMARY KEY" in definition or "UNIQUE" in definition:
            violations.append(f"PRIMARY KEY/UNIQUE cannot be added in place: {normalized}")
    return violations


def lint_create_statements(statements: Iterable[str]) -> list[str]:
    violations = []
    for statement in statements:
        normalized = " ".join(statement.split())
        if TRIGGER_RE.match(normalized):
            continue
        if not CREATE_IF_NOT_EXISTS_RE.match(normalized):
            violations.append(f"not CREATE ... IF NOT EXISTS: {normalized[:120]}")
        if re.search(r"\b(DROP|RENAME)\b", normalized, re.IGNORECASE):
            violations.append(f"contains DROP/RENAME: {normalized[:120]}")
    return violations


def _execute_create_statements(
    connection: sqlite3.Connection, statements: Iterable[str]
) -> None:
    """Run CREATE statements in dependency-tolerant order (owner lists are unordered)."""
    pending = list(statements)
    while pending:
        deferred, last_error = [], None
        for statement in pending:
            try:
                connection.execute(statement)
            except sqlite3.OperationalError as exc:
                deferred.append(statement)
                last_error = exc
        if len(deferred) == len(pending):
            raise last_error  # type: ignore[misc]
        pending = deferred


def columns_missing_from_create(
    create_statements: Iterable[str], migration_statements: Iterable[str]
) -> list[tuple[str, str]]:
    """Migration-added columns absent from a CREATE-only database."""
    connection = sqlite3.connect(":memory:")
    try:
        _execute_create_statements(connection, create_statements)
        missing = []
        for table, column in _added_columns(migration_statements):
            present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if column not in present:
                missing.append((table, column))
        return missing
    finally:
        connection.close()


def _create_only_columns(create_statements: Iterable[str]) -> set[tuple[str, str]]:
    connection = sqlite3.connect(":memory:")
    try:
        _execute_create_statements(connection, create_statements)
        migrated = set(_all_added_columns())
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {
            (table, row[1])
            for (table,) in tables
            for row in connection.execute(f"PRAGMA table_info({table})")
        } - migrated
    finally:
        connection.close()


def _column_set_digest(columns: set[tuple[str, str]]) -> str:
    encoded = "\n".join(f"{table}.{column}" for table, column in sorted(columns)).encode()
    return hashlib.sha256(encoded).hexdigest()


def _schema_shape(connection: sqlite3.Connection) -> dict:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    columns = {
        table: {
            row[1]: (row[2], row[3], row[4])  # type, notnull, dflt_value
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for table in tables
    }
    indexes = {}
    for name, table, sql in connection.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master "
        "WHERE type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY name"
    ):
        indexes[name] = (table, " ".join(sql.split()))
    return {"columns": columns, "indexes": indexes}


def _drop_added_columns(connection: sqlite3.Connection) -> int:
    added = [c for c in _all_added_columns() if c not in UNDROPPABLE_ADDED_COLUMNS]
    tables = {table for table, _ in added}
    # Indexes and triggers may reference the columns; the initializer recreates them.
    for kind, name, table in connection.execute(
        "SELECT type, name, tbl_name FROM sqlite_master "
        "WHERE type IN ('index', 'trigger') AND sql IS NOT NULL"
    ).fetchall():
        if kind == "trigger" or table in tables:
            connection.execute(f'DROP {kind.upper()} "{name}"')
    for table, column in added:
        connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    connection.commit()
    return len(added)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_discovery_finds_every_owner_list() -> None:
    assert len(_migration_lists()) >= 6
    assert len(_create_lists()) >= 10
    assert len(_all_added_columns()) >= 40
    assert UNDROPPABLE_ADDED_COLUMNS <= set(_all_added_columns())


def test_fresh_database_equals_upgraded_database(monkeypatch, tmp_path) -> None:
    _isolate_data_root(monkeypatch, tmp_path)
    from app.core.database import initialize_database, open_sqlite_connection

    initialize_database()
    with open_sqlite_connection() as connection:
        fresh = _schema_shape(connection)
        dropped = _drop_added_columns(connection)
        legacy = _schema_shape(connection)

    assert dropped >= 40
    assert legacy["columns"] != fresh["columns"], "legacy copy must actually differ"

    initialize_database()
    with open_sqlite_connection() as connection:
        upgraded = _schema_shape(connection)

    assert upgraded["columns"] == fresh["columns"]
    assert upgraded["indexes"] == fresh["indexes"]


def test_fk_constrained_parameter_column_upgrades_from_legacy_table(monkeypatch, tmp_path) -> None:
    """Exercise the one column that cannot be DROPped from a fresh SQLite table."""
    _isolate_data_root(monkeypatch, tmp_path)
    from app.core.database import initialize_database, open_sqlite_connection
    from app.core.schema import SCHEMA_STATEMENTS

    current = next(s for s in SCHEMA_STATEMENTS if "CREATE TABLE IF NOT EXISTS parameters" in s)
    legacy = current.replace("        supersedes_parameter_id TEXT,\n", "")
    legacy = legacy.replace(
        "FOREIGN KEY (workspace_id) REFERENCES workspaces(id),\n"
        "        FOREIGN KEY (supersedes_parameter_id) REFERENCES parameters(id)",
        "FOREIGN KEY (workspace_id) REFERENCES workspaces(id)",
    )
    assert legacy != current
    with open_sqlite_connection() as connection:
        connection.execute(legacy)
        assert "supersedes_parameter_id" not in {
            row["name"] for row in connection.execute("PRAGMA table_info(parameters)")
        }
        connection.commit()

    initialize_database()
    with open_sqlite_connection() as connection:
        assert "supersedes_parameter_id" in {
            row["name"] for row in connection.execute("PRAGMA table_info(parameters)")
        }


def test_every_migration_added_column_is_in_owner_create() -> None:
    create_all = [s for statements in _create_lists().values() for s in statements]
    migration_all = [s for statements in _migration_lists().values() for s in statements]
    missing = columns_missing_from_create(create_all, migration_all)
    unexpected = [c for c in missing if c not in MIGRATION_ONLY_COLUMNS]
    assert unexpected == [], f"migration-added columns missing from CREATE TABLE: {unexpected}"


def test_migration_lists_are_additive_only() -> None:
    for name, statements in _migration_lists().items():
        assert statements, f"{name} is empty"
        assert lint_migration_statements(statements) == [], name


def test_create_and_index_lists_are_idempotent_and_non_destructive() -> None:
    for name, statements in {**_create_lists(), **_index_lists()}.items():
        assert lint_create_statements(statements) == [], name


def test_migration_ids_are_unique() -> None:
    ids = [record["migration_id"] for record in _migration_records()]
    assert len(ids) >= 14
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert duplicates == []
    assert all(re.fullmatch(r"\d{4}_[a-z0-9_]+", i) for i in ids), ids


def test_initializer_is_idempotent_over_schema_and_migration_rows(monkeypatch, tmp_path) -> None:
    _isolate_data_root(monkeypatch, tmp_path)
    from app.core.database import initialize_database, open_sqlite_connection

    def snapshot() -> tuple:
        with open_sqlite_connection() as connection:
            master = connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
            ids = connection.execute(
                "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
            ).fetchall()
        return [tuple(r) for r in master], [r[0] for r in ids]

    initialize_database()
    first = snapshot()
    initialize_database()
    second = snapshot()
    assert first == second
    assert len(first[1]) == len(set(first[1])) == len(_migration_records())


def test_migration_only_allowlist_is_exact() -> None:
    """Every allowlisted drift entry is still drift, and every drift entry is listed."""
    create_all = [s for statements in _create_lists().values() for s in statements]
    migration_all = [s for statements in _migration_lists().values() for s in statements]
    missing = set(columns_missing_from_create(create_all, migration_all))
    assert missing == set(MIGRATION_ONLY_COLUMNS)


def test_no_new_create_only_columns() -> None:
    create_all = [s for statements in _create_lists().values() for s in statements]
    create_only = _create_only_columns(create_all)
    assert all(LEGACY_CREATE_ONLY_COLUMNS.values())
    assert set(LEGACY_CREATE_ONLY_COLUMNS) <= create_only
    assert _column_set_digest(create_only - set(LEGACY_CREATE_ONLY_COLUMNS)) == (
        BASE_CREATE_ONLY_SHA256
    ), "CREATE-only shape changed; add the matching migration or review the new table"


# ---------------------------------------------------------------------------
# Negative cases (spec 028 acceptance)
# ---------------------------------------------------------------------------


def test_negative_drop_statement_in_migration_list_is_rejected() -> None:
    from app.core.schema import SCHEMA_MIGRATION_STATEMENTS

    tainted = [*SCHEMA_MIGRATION_STATEMENTS, "ALTER TABLE ai_settings DROP COLUMN policy_mode"]
    assert lint_migration_statements(SCHEMA_MIGRATION_STATEMENTS) == []
    assert lint_migration_statements(tainted)
    for bad in (
        "ALTER TABLE ai_settings RENAME COLUMN policy_mode TO mode",
        "DROP TABLE ai_settings",
        "ALTER TABLE ai_settings ADD COLUMN x TEXT NOT NULL",
    ):
        assert lint_migration_statements([bad]), bad


def test_negative_migration_list_with_drop_fails_real_lint(monkeypatch) -> None:
    import app.core.database as database

    monkeypatch.setattr(
        database,
        "RUNNER_CREATE_REQUEST_MIGRATION_STATEMENTS",
        (*database.RUNNER_CREATE_REQUEST_MIGRATION_STATEMENTS, "DROP TABLE runner_jobs"),
    )
    with pytest.raises(AssertionError):
        test_migration_lists_are_additive_only()


def test_negative_column_missing_from_create_is_detected(monkeypatch) -> None:
    import app.core.database as database

    mutated = [
        statement.replace("policy_mode TEXT NOT NULL DEFAULT 'STRICT_IP',", "")
        for statement in database.SCHEMA_STATEMENTS
    ]
    assert mutated != list(database.SCHEMA_STATEMENTS), "mutation must hit the CREATE"
    monkeypatch.setattr(database, "SCHEMA_STATEMENTS", mutated)
    with pytest.raises(AssertionError, match="policy_mode"):
        test_every_migration_added_column_is_in_owner_create()
    with pytest.raises(AssertionError):
        test_migration_only_allowlist_is_exact()


def test_negative_column_added_only_to_create_is_detected(monkeypatch) -> None:
    import app.core.database as database

    mutated = [
        statement.replace(
            "policy_mode TEXT NOT NULL DEFAULT 'STRICT_IP',",
            "policy_mode TEXT NOT NULL DEFAULT 'STRICT_IP', create_only_probe TEXT,",
        )
        for statement in database.SCHEMA_STATEMENTS
    ]
    assert mutated != list(database.SCHEMA_STATEMENTS), "mutation must hit the CREATE"
    monkeypatch.setattr(database, "SCHEMA_STATEMENTS", mutated)
    with pytest.raises(AssertionError, match="CREATE-only shape changed"):
        test_no_new_create_only_columns()


def test_negative_non_reapplicable_migration_breaks_upgrade(monkeypatch, tmp_path) -> None:
    """A migration that references a column absent from the legacy shape fails loudly."""
    _isolate_data_root(monkeypatch, tmp_path)
    import app.core.database as database

    database.initialize_database()
    monkeypatch.setattr(
        database,
        "RUNNER_CREATE_REQUEST_MIGRATION_STATEMENTS",
        ("ALTER TABLE no_such_table ADD COLUMN request_key TEXT",),
    )
    with pytest.raises(sqlite3.OperationalError):
        database.initialize_database()
