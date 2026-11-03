"""Stage 3-min — Characterize SQLite policy-mode legacy upgrade behavior.

Test-only. Protects local SQLite DBs that predate the `policy_mode` column in
`ai_settings`. Characterizes the CURRENT upgrade path (re-running
`initialize_database()`); it does NOT change schema, migrations, or runtime.

Scope is intentionally narrow:
  - Test 1: initialize_database() is idempotent on a fresh DB.
  - Test 2: a legacy ai_settings without policy_mode is upgraded in place.

Out of scope (later slices): global fresh-vs-upgraded schema diff, index diff,
dead-column detection (e.g. scaleway_token_cap / scaleway_tokens_month_to_date
exist in the CREATE but in no migration), binary .db fixtures.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Legacy ai_settings = the current CREATE TABLE ai_settings, verbatim, MINUS the
# policy_mode column. Built with explicit SQL on purpose: it must NOT be derived
# from SCHEMA_STATEMENTS, so the test genuinely exercises the 0003 migration that
# adds policy_mode. A non-default sentinel value is inserted to prove the
# pre-existing row survives the upgrade.
LEGACY_AI_SETTINGS_SQL = """
CREATE TABLE ai_settings (
    id TEXT PRIMARY KEY,
    monthly_api_budget_usd REAL NOT NULL DEFAULT 0,
    api_spend_month_to_date_usd REAL NOT NULL DEFAULT 0,
    paid_ai_enabled INTEGER NOT NULL DEFAULT 0,
    default_ai_provider TEXT NOT NULL DEFAULT 'fake',
    default_ai_model TEXT NOT NULL DEFAULT 'fake-modeling-draft-v1',
    provider_mode TEXT NOT NULL DEFAULT 'fake',
    use_fake_provider_when_budget_zero INTEGER NOT NULL DEFAULT 1,
    scaleway_enabled INTEGER NOT NULL DEFAULT 0,
    scaleway_token_cap INTEGER NOT NULL DEFAULT 0,
    scaleway_tokens_month_to_date INTEGER NOT NULL DEFAULT 0,
    scaleway_smoke_test_enabled INTEGER NOT NULL DEFAULT 0,
    scaleway_live_smoke_test_enabled INTEGER NOT NULL DEFAULT 0,
    scaleway_monthly_token_cap INTEGER NOT NULL DEFAULT 500000,
    scaleway_hard_stop_token_cap INTEGER NOT NULL DEFAULT 800000,
    scaleway_free_tier_reference_tokens INTEGER NOT NULL DEFAULT 1000000,
    scaleway_input_tokens_month_to_date INTEGER NOT NULL DEFAULT 0,
    scaleway_output_tokens_month_to_date INTEGER NOT NULL DEFAULT 0,
    smoke_test_mode_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
)
"""


def _isolate_data_root(monkeypatch, tmp_path: Path) -> None:
    """Point the DB path at an isolated tmp root and clear the settings cache.

    Consistent with the existing DB-test pattern; explicit even though the
    autouse Stage 0 fixture also isolates the root.
    """
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "jarvisos-legacy"))
    from app.core.config import get_settings

    get_settings.cache_clear()


def _non_internal_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row["name"] for row in rows}


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def test_initialize_database_is_idempotent_on_fresh_db(monkeypatch, tmp_path) -> None:
    """Running initialize_database() twice on a fresh DB is a no-op for the
    table set. This anchors the whole upgrade model: the CREATE statements are
    IF NOT EXISTS and the ALTER migrations swallow duplicate-column errors."""
    _isolate_data_root(monkeypatch, tmp_path)
    from app.core.database import initialize_database, open_sqlite_connection

    initialize_database()
    with open_sqlite_connection() as connection:
        tables_after_first = _non_internal_tables(connection)

    initialize_database()
    with open_sqlite_connection() as connection:
        tables_after_second = _non_internal_tables(connection)

    assert tables_after_first == tables_after_second
    assert "ai_settings" in tables_after_first
    # Core app tables read from SCHEMA_STATEMENTS, not guessed.
    assert {"workspaces", "events", "model_specs"}.issubset(tables_after_first)


def test_legacy_ai_settings_without_policy_mode_is_upgraded(monkeypatch, tmp_path) -> None:
    """A legacy ai_settings (no policy_mode) with a pre-existing row is upgraded
    by initialize_database(): policy_mode is added, the old row survives, and the
    new column is populated with the migration default ('FAST_DEV')."""
    _isolate_data_root(monkeypatch, tmp_path)
    from app.core.database import initialize_database, open_sqlite_connection

    # Build the legacy DB with explicit SQL (NOT the current schema).
    with open_sqlite_connection() as connection:
        connection.execute(LEGACY_AI_SETTINGS_SQL)
        connection.execute(
            "INSERT INTO ai_settings (id, monthly_api_budget_usd, updated_at) VALUES (?, ?, ?)",
            ("legacy-singleton", 42.0, "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()
        assert "policy_mode" not in _column_names(connection, "ai_settings")

    # Run the real upgrade path.
    initialize_database()

    with open_sqlite_connection() as connection:
        columns = _column_names(connection, "ai_settings")
        rows = connection.execute(
            "SELECT id, monthly_api_budget_usd, policy_mode FROM ai_settings WHERE id = ?",
            ("legacy-singleton",),
        ).fetchall()

    assert "policy_mode" in columns
    assert len(rows) == 1
    row = rows[0]
    # The pre-existing row survived with its sentinel data intact.
    assert row["monthly_api_budget_usd"] == 42.0
    # The migration default populates the legacy row fail-closed (154c).
    assert row["policy_mode"] == "STRICT_IP"

    # A second initialize_database() after upgrade is still a no-op.
    initialize_database()
    with open_sqlite_connection() as connection:
        assert "policy_mode" in _column_names(connection, "ai_settings")
        count = connection.execute("SELECT COUNT(*) AS c FROM ai_settings").fetchone()["c"]
    assert count == 1


# ---------------------------------------------------------------------------
# Stage 3-full exposed a fresh-vs-legacy drift: scaleway_token_cap and
# scaleway_tokens_month_to_date shipped in CREATE TABLE ai_settings but in no
# migration, so a legacy DB could never gain them and ensure_ai_settings() failed
# on it. Stage 3-fix added the repair migrations; the tests below now protect the
# REPAIRED behavior (legacy upgrade gains the columns, bootstrap succeeds).
# ---------------------------------------------------------------------------

# Legacy ai_settings predating policy_mode AND the two scaleway token columns.
# Built with explicit SQL (never from SCHEMA_STATEMENTS) so the migration path is
# genuinely exercised.
LEGACY_AI_SETTINGS_PRE_SCALEWAY_SQL = """
CREATE TABLE ai_settings (
    id TEXT PRIMARY KEY,
    monthly_api_budget_usd REAL NOT NULL DEFAULT 0,
    api_spend_month_to_date_usd REAL NOT NULL DEFAULT 0,
    paid_ai_enabled INTEGER NOT NULL DEFAULT 0,
    default_ai_provider TEXT NOT NULL DEFAULT 'fake',
    default_ai_model TEXT NOT NULL DEFAULT 'fake-modeling-draft-v1',
    provider_mode TEXT NOT NULL DEFAULT 'fake',
    use_fake_provider_when_budget_zero INTEGER NOT NULL DEFAULT 1,
    scaleway_enabled INTEGER NOT NULL DEFAULT 0,
    smoke_test_mode_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
)
"""

# Partially-upgraded legacy: already has scaleway_token_cap, still lacks
# scaleway_tokens_month_to_date. Exercises duplicate-column swallow (present one)
# plus repair migration (missing one) in the same upgrade.
LEGACY_AI_SETTINGS_PARTIAL_SQL = """
CREATE TABLE ai_settings (
    id TEXT PRIMARY KEY,
    monthly_api_budget_usd REAL NOT NULL DEFAULT 0,
    api_spend_month_to_date_usd REAL NOT NULL DEFAULT 0,
    paid_ai_enabled INTEGER NOT NULL DEFAULT 0,
    default_ai_provider TEXT NOT NULL DEFAULT 'fake',
    default_ai_model TEXT NOT NULL DEFAULT 'fake-modeling-draft-v1',
    provider_mode TEXT NOT NULL DEFAULT 'fake',
    use_fake_provider_when_budget_zero INTEGER NOT NULL DEFAULT 1,
    scaleway_enabled INTEGER NOT NULL DEFAULT 0,
    scaleway_token_cap INTEGER NOT NULL DEFAULT 0,
    smoke_test_mode_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
)
"""

SCALEWAY_TOKEN_COLUMNS = ("scaleway_token_cap", "scaleway_tokens_month_to_date")


def _build_legacy_pre_scaleway_db(connection: sqlite3.Connection) -> None:
    connection.execute(LEGACY_AI_SETTINGS_PRE_SCALEWAY_SQL)
    connection.execute(
        "INSERT INTO ai_settings (id, monthly_api_budget_usd, updated_at) VALUES (?, ?, ?)",
        ("legacy-singleton", 7.0, "2026-01-01T00:00:00+00:00"),
    )
    connection.commit()


def test_fresh_db_ai_settings_has_scaleway_token_columns(monkeypatch, tmp_path) -> None:
    """A fresh DB built by the current CREATE has both columns."""
    _isolate_data_root(monkeypatch, tmp_path)
    from app.core.database import initialize_database, open_sqlite_connection

    initialize_database()
    with open_sqlite_connection() as connection:
        columns = _column_names(connection, "ai_settings")

    for column in SCALEWAY_TOKEN_COLUMNS:
        assert column in columns


def test_legacy_upgrade_adds_scaleway_token_columns(monkeypatch, tmp_path) -> None:
    """REPAIRED behavior (Stage 3-fix): a legacy ai_settings that predates the two
    scaleway token columns gains them after initialize_database(), with the
    migration default (0); the pre-existing row survives and re-init is idempotent.
    Stage 3-full had characterized these columns as permanently missing."""
    _isolate_data_root(monkeypatch, tmp_path)
    from app.core.database import initialize_database, open_sqlite_connection

    with open_sqlite_connection() as connection:
        _build_legacy_pre_scaleway_db(connection)
        before = _column_names(connection, "ai_settings")
    assert "policy_mode" not in before
    for column in SCALEWAY_TOKEN_COLUMNS:
        assert column not in before

    initialize_database()

    with open_sqlite_connection() as connection:
        after = _column_names(connection, "ai_settings")
        row = connection.execute(
            """
            SELECT monthly_api_budget_usd, scaleway_token_cap, scaleway_tokens_month_to_date
            FROM ai_settings WHERE id = ?
            """,
            ("legacy-singleton",),
        ).fetchone()

    assert "policy_mode" in after
    for column in SCALEWAY_TOKEN_COLUMNS:
        assert column in after
    # Pre-existing row survived; the repaired columns carry the migration default.
    assert row is not None
    assert row["monthly_api_budget_usd"] == 7.0
    assert row["scaleway_token_cap"] == 0
    assert row["scaleway_tokens_month_to_date"] == 0

    # Second init remains idempotent.
    initialize_database()
    with open_sqlite_connection() as connection:
        after_second = _column_names(connection, "ai_settings")
    for column in SCALEWAY_TOKEN_COLUMNS:
        assert column in after_second


def test_ensure_ai_settings_succeeds_on_repaired_legacy_db(monkeypatch, tmp_path) -> None:
    """REPAIRED behavior (Stage 3-fix): ensure_ai_settings() no longer raises on a
    legacy-upgraded DB, and the ai_settings row is queryable afterward. Stage 3-full
    had characterized this as raising OperationalError."""
    _isolate_data_root(monkeypatch, tmp_path)
    from app.core.database import initialize_database, open_sqlite_connection
    from app.modules.ai.settings import ensure_ai_settings

    with open_sqlite_connection() as connection:
        _build_legacy_pre_scaleway_db(connection)
    initialize_database()

    settings = ensure_ai_settings()  # must not raise

    assert settings is not None
    with open_sqlite_connection() as connection:
        columns = _column_names(connection, "ai_settings")
        default_row = connection.execute(
            "SELECT id FROM ai_settings WHERE id = 'default'"
        ).fetchone()
    for column in SCALEWAY_TOKEN_COLUMNS:
        assert column in columns
    assert default_row is not None


def test_partial_legacy_upgrade_adds_only_missing_scaleway_column(monkeypatch, tmp_path) -> None:
    """A partially-upgraded legacy DB that already has scaleway_token_cap but lacks
    scaleway_tokens_month_to_date: initialize_database() adds only the missing one
    (duplicate-column swallow handles the present one), the existing column's value
    survives, and the row remains queryable."""
    _isolate_data_root(monkeypatch, tmp_path)
    from app.core.database import initialize_database, open_sqlite_connection

    with open_sqlite_connection() as connection:
        connection.execute(LEGACY_AI_SETTINGS_PARTIAL_SQL)
        connection.execute(
            "INSERT INTO ai_settings (id, scaleway_token_cap, updated_at) VALUES (?, ?, ?)",
            ("legacy-partial", 99, "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()
        before = _column_names(connection, "ai_settings")
    assert "scaleway_token_cap" in before
    assert "scaleway_tokens_month_to_date" not in before

    initialize_database()

    with open_sqlite_connection() as connection:
        after = _column_names(connection, "ai_settings")
        row = connection.execute(
            "SELECT scaleway_token_cap, scaleway_tokens_month_to_date FROM ai_settings WHERE id = ?",
            ("legacy-partial",),
        ).fetchone()

    for column in SCALEWAY_TOKEN_COLUMNS:
        assert column in after
    assert row is not None
    assert row["scaleway_token_cap"] == 99  # pre-existing value survived
    assert row["scaleway_tokens_month_to_date"] == 0  # added with migration default


# ---------------------------------------------------------------------------
# 0004 engineering record schema freeze (spec 001): legacy parameters and
# assumptions predate unit/value_status/value_min/value_max and the text enums.
# The fixtures below are the 0003-era shapes, built with explicit SQL (never
# from SCHEMA_STATEMENTS) so the 0004 additive upgrade is genuinely exercised.
# ---------------------------------------------------------------------------

LEGACY_WORKSPACES_SQL = """
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

# 0003-era parameters: nullable unit, REAL confidence, no value_status/min/max.
LEGACY_PARAMETERS_SQL = """
CREATE TABLE parameters (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    symbol TEXT,
    value TEXT,
    unit TEXT,
    source_ref TEXT,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notes TEXT,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
)
"""

# 0003-era assumptions: REAL confidence, status default 'draft'.
LEGACY_ASSUMPTIONS_SQL = """
CREATE TABLE assumptions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    scope TEXT,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'draft',
    source_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notes TEXT,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
)
"""

PARAMETER_FREEZE_COLUMNS = ("value_status", "value_min", "value_max")


def _build_legacy_engineering_db(connection: sqlite3.Connection) -> None:
    connection.execute(LEGACY_WORKSPACES_SQL)
    connection.execute(LEGACY_PARAMETERS_SQL)
    connection.execute(LEGACY_ASSUMPTIONS_SQL)
    now = "2026-01-01T00:00:00+00:00"
    connection.execute(
        "INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("legacy-ws", "Legacy workspace", "legacy-ws", now, now),
    )
    # Sentinel rows with legacy-only shapes: NULL unit, numeric confidence,
    # pre-enum status values.
    connection.execute(
        """
        INSERT INTO parameters (id, workspace_id, name, value, unit, confidence, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("legacy-param", "legacy-ws", "Seawater density", "1025", None, 0.7, "draft", now, now),
    )
    connection.execute(
        """
        INSERT INTO assumptions (id, workspace_id, statement, confidence, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("legacy-assumption", "legacy-ws", "Calm sea during tests.", 0.7, "draft", now, now),
    )
    connection.commit()


def test_legacy_engineering_records_upgrade_without_data_loss(monkeypatch, tmp_path) -> None:
    """0004 schema freeze on a 0003-era DB: parameters gain the new columns with
    migration defaults, existing row data survives verbatim in the raw table, and
    the list services normalize legacy values (unit -> 'unspecified',
    value_status -> 'candidate', numeric assumption confidence -> None,
    pre-enum status -> 'proposed') without touching stored data."""
    _isolate_data_root(monkeypatch, tmp_path)
    from app.core.database import initialize_database, open_sqlite_connection

    with open_sqlite_connection() as connection:
        _build_legacy_engineering_db(connection)
        before = _column_names(connection, "parameters")
    for column in PARAMETER_FREEZE_COLUMNS:
        assert column not in before

    # Run the real upgrade path.
    initialize_database()

    with open_sqlite_connection() as connection:
        after = _column_names(connection, "parameters")
        raw_param = connection.execute(
            "SELECT name, value, unit, confidence, status, value_status FROM parameters WHERE id = ?",
            ("legacy-param",),
        ).fetchone()
        raw_assumption = connection.execute(
            "SELECT statement, confidence, status FROM assumptions WHERE id = ?",
            ("legacy-assumption",),
        ).fetchone()

    for column in PARAMETER_FREEZE_COLUMNS:
        assert column in after
    # Raw legacy data survived verbatim; new column carries the migration default.
    assert raw_param is not None
    assert raw_param["name"] == "Seawater density"
    assert raw_param["value"] == "1025"
    assert raw_param["unit"] is None  # duplicate-column ALTER swallowed, no rewrite
    assert raw_param["confidence"] == 0.7
    assert raw_param["status"] == "draft"
    assert raw_param["value_status"] == "candidate"
    assert raw_assumption is not None
    assert raw_assumption["statement"] == "Calm sea during tests."
    assert raw_assumption["confidence"] == 0.7  # numeric legacy value kept in storage
    assert raw_assumption["status"] == "draft"

    # List services normalize legacy values for API consumers.
    from app.modules.modeling.service import list_assumptions, list_parameters

    parameters = list_parameters("legacy-ws")
    legacy_param = next(item for item in parameters if item.id == "legacy-param")
    assert legacy_param.unit == "unspecified"
    assert legacy_param.value_status == "candidate"

    assumptions = list_assumptions("legacy-ws")
    legacy_assumption = next(item for item in assumptions if item.id == "legacy-assumption")
    assert legacy_assumption.confidence is None  # numeric value masked, not coerced
    assert legacy_assumption.status == "proposed"

    # Second initialize_database() after upgrade remains idempotent.
    initialize_database()
    with open_sqlite_connection() as connection:
        assert PARAMETER_FREEZE_COLUMNS[0] in _column_names(connection, "parameters")
        count = connection.execute("SELECT COUNT(*) AS c FROM parameters").fetchone()["c"]
    assert count == 1
