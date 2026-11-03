# Schema migrations

Status: policy document under spec 028 (not carried into this repository). It records the rules the code already follows. It is not a migration framework, and it changes no table or runtime path.

## How the schema is built

One entry point, `initialize_database()` (`backend/app/core/database.py`), runs on every start in this order:

1. Each owner's `CREATE TABLE IF NOT EXISTS` list (`SCHEMA_STATEMENTS` in `schema.py` and the per-owner `*_schema.py` modules).
2. The additive migration lists: `SCHEMA_MIGRATION_STATEMENTS`, `EGRESS_SCHEMA_MIGRATION_STATEMENTS`, `TOKEN_FLOW_SCHEMA_MIGRATION_STATEMENTS`, `RUNNER_CREATE_REQUEST_MIGRATION_STATEMENTS`, `PROJECT_KNOWLEDGE_MIGRATION_STATEMENTS`, `RELAY_GATEWAY_MIGRATION_STATEMENTS`. Only `duplicate column name` is tolerated.
3. The guarded backfill `_ensure_parameter_lifecycle_schema` (checks `PRAGMA table_info` first).
4. The `CREATE INDEX IF NOT EXISTS` lists and the FTS rebuild.
5. `_record_schema_migrations`, which upserts each `*_MIGRATION_RECORD` into `schema_migrations` by `migration_id`.

## Rules

1. **Additive only.** Allowed: `CREATE TABLE/INDEX IF NOT EXISTS` and `ALTER TABLE <t> ADD COLUMN`. A `NOT NULL` added column carries a `DEFAULT`. No `PRIMARY KEY` or `UNIQUE` added in place.
2. **Shape in two places.** A new column goes in its owner's `CREATE TABLE` (fresh shape) and in that owner's migration list (upgrade shape). See "Known drift" for columns that are only in the migration list today.
3. **Backfills are idempotent and guarded** by a shape check (as `_ensure_parameter_lifecycle_schema` does), so a second run changes nothing.
4. **Every change carries a record.** Add or reuse a `*_MIGRATION_RECORD` with a unique `migration_id` (`NNNN_snake_case`), a `name` and a `checksum`. Add it to `_record_schema_migrations`.
5. **No renames, drops or type changes** without their own accepted spec that includes a backup and restore path (see `DATA_ROOT_RECOVERY.md`).
6. **Startup order is not changed** by a schema slice.

## Where to add each piece

| Change | Add it to |
| --- | --- |
| New table | owner `*_SCHEMA_STATEMENTS`, plus its index list |
| New column | owner `CREATE TABLE` and owner migration list |
| New index | owner `*_INDEX_STATEMENTS` (after any migration that adds its column) |
| New migration list | name it `*_MIGRATION_STATEMENTS`, import it in `database.py`, and add it to the migration loop in `initialize_database()` |
| Record | owner `*_MIGRATION_RECORD`, listed in `_record_schema_migrations` |

## Enforcement

`backend/tests/test_migration_discipline.py` discovers the lists from `app.core.database` and checks:

- fresh equals upgraded: it drops migration-added columns (and the indexes and triggers that reference them), except the pinned foreign-key-constrained column below, re-runs `initialize_database()`, and compares tables, columns (type, not-null, default), indexes and triggers with the fresh database; a separate legacy-table test exercises the constrained column's upgrade;
- every migration-added column is in its owner's `CREATE TABLE`, apart from the allowlist below;
- the exact pre-existing `CREATE`-only column baseline is pinned by digest, so adding a column only to CREATE fails; adding a new table requires an explicit baseline review;
- every migration statement is additive (`ADD COLUMN`, no `DROP`, `RENAME` or `ALTER COLUMN`), and create and index lists are `IF NOT EXISTS`;
- every `migration_id` is unique;
- two consecutive initializer runs leave identical `sqlite_master` and `schema_migrations` rows.

The tests run against `tmp_path` data roots or in-memory SQLite and never touch a real data root. `test_sqlite_legacy_upgrade.py` keeps the narrow `policy_mode` upgrade check.

## Known drift (recorded, not fixed)

- **Migration-only columns.** 38 columns exist only through a migration statement and not in their owner's `CREATE TABLE`: `sanitized_derivatives` (6, egress), `requirements` (7) and `decisions.basis_lifecycle_state`, `simulation_runs.project_knowledge_revision_id` (project knowledge), `runner_jobs.request_key`, `ai_jobs` (20, token flow and egress), and `ai_settings.max_direct_continuations` and `direct_continuation_policy_version`. Fresh databases get them because the migration lists also run on fresh databases. The test keeps the exact list in `MIGRATION_ONLY_COLUMNS`; it fails if the list goes stale. Folding them into the `CREATE` statements needs a later accepted spec.
- **CREATE-only legacy columns.** The former members `scaleway_token_cap` and `scaleway_tokens_month_to_date` were repaired by catch-up migrations ("Repair (Stage 3-fix)"). `LEGACY_CREATE_ONLY_COLUMNS` is empty and is the place to record any future case.
- **Undroppable column.** `parameters.supersedes_parameter_id` is named in a foreign key, so SQLite cannot drop it to build the synthetic legacy copy. The test pins this sole exclusion and separately constructs a parameters table without the column, then verifies the initializer adds it. The comparison covers column and index shape; it does not claim that an upgraded table gains the fresh table's foreign-key constraint, which SQLite `ADD COLUMN` cannot retroactively add.
