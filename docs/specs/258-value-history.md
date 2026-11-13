# 258 — Automatic value history: which value versions a run used, and comparing two versions (248 slice B)

Status: ready
Depends on: none (the approved [248](248-design-value-path.md) contract binds through Authority; slice A landed as
253)

## Goal

Complete slice B of the approved design-value path, point 6 of the 248 contract: history is automatic and git-like,
per project. Every version of a value, a run and a calculation is recorded when it is created. Any run shows which value
versions it used, and two versions of a value can be compared. A new version marks dependent checks stale through the
existing freshness graph. There is no snapshot step and no snapshot UI.

Risk: high (requires an additive migration; see Schema). Review confirms that existing records overwrite intermediate
result-value corrections in place (`simulation_result_resolutions` has `UNIQUE(value_id)`), so an additive migration is
strictly required to preserve every version.

## Authority

Maintainer answer `L20261106093156-1712` approves the binding contract in [248](248-design-value-path.md), with the slice
order A, then B and C. Slice A is merged (253) and slice C is merged (257). This spec is reviewed against the current
codebase and is ready for implementation.

## Why

Most of the history already exists across owners, but cannot be read as one:

- `parameters` keep a version chain through `supersedes_parameter_id`, and `freshness_invalidations` and
  `freshness_marks` (`backend/app/modules/flowsheet/freshness.py`) mark dependents stale when a parameter is replaced.
- A runner run with a schema-v2/v3 input contract names its linked parameters by `source_parameter_id` in the
  normalized input payload (`backend/app/modules/runner/linked_parameters.py` `linked_parameter_items`).
- Simulation result values carry their payload digest and extractor version (`simulation_result_values`). However,
  resolving an exception in `backend/app/modules/project_knowledge/sim_results.py` mutates `simulation_result_values` in
  place and updates `simulation_result_resolutions` where `UNIQUE(value_id)`, losing intermediate corrected versions.
- A requirement check names the run and scalar it used (`project_knowledge_validation.source_run_id`,
  `source_scalar_id`, `source_payload_digest`, `observed_json`, `supersedes_validation_id`), and its admissibility in
  `revalidation_status_from_connection` detects value changes dynamically.

No read gives "the value versions this run used", and no read compares two versions of the same value.

## Scope

1. **Run inputs manifest (read).** For a run, a read-only API returns every value version the run used: each linked
   parameter version, with its id, name, value, unit, status and position in its `supersedes` chain, plus whether a newer
   version exists (checked against current active parameters). It is derived from the stored input payload
   (`linked_parameter_items`), not from current parameter rows, so a later edit never rewrites what the run used. A run
   whose stored contract has no canonical linked parameters (historical, schema v1) gives `history_not_recorded` with
   that reason. It never guesses.
2. **Value version chain (read).** For a parameter or a simulation result value, a read-only API returns its ordered
   versions:
   - For a parameter: the ordered `supersedes_parameter_id` chain.
   - For a simulation result value: the ordered version list (the initial ingested version 0, plus all subsequent
     corrections/resolutions 1..N).
   Each version shows its source, value, unit, conditions, validation state (passed, exception/red dot, corrected, or
   confirmed), actor, reason, and creation timestamp.
3. **Compare two versions (read).** Given two versions of the same value (two parameter versions in a line or two result
   versions of the same output), the API returns value, unit, conditions, source and validation state side by side, and
   lists the runs and requirement checks that used each one. Versions of different values are refused with
   `not_same_value`.
4. **Staleness reuses existing freshness and revalidation paths.**
   - When a parameter is superseded: existing `prepare_freshness_invalidation`/`persist_freshness_invalidation` marks
     downstream flowsheet nodes/parameters stale.
   - When a result value is corrected: `_invalidate_dependent_evidence` invalidates any parameter derived from the run
     through existing `prepare_freshness_invalidation`. Concurrently, requirement checks (`project_knowledge_validation`)
     that observed the previous value fail admissibility in `_validation_row_admissible` (`observed_json["value"] !=
     current_val`), surfacing as `validation_stale` in `revalidation_status_from_connection`. No second invalidation
     engine is added.
5. **Surfaces.** The run detail view gains a read-only "Values used" section, and the value views gain "History" with a
   compare action. No snapshot button and no snapshot page (248 non-goal).

## Non-goals

- Snapshot steps, snapshot UI, Consist, AiiDA, or any new provenance framework (248 point 6 and its non-goals).
- Rewriting historical runs to add a manifest they never recorded.
- Literature and property-library values (slices D–F). The chain API is shaped so that they can join later.
- Restoring an old version. A user who wants it back enters it again as a new version.
- Changing validation, revalidation or reconcile semantics.

## Schema

An additive migration is required. Derivation from existing records alone cannot preserve every version because
`backend/app/modules/project_knowledge/sim_results.py` mutates `simulation_result_values` in place, and
`simulation_result_resolutions` enforces `UNIQUE(value_id)`. Under `docs/MIGRATIONS.md`, SQLite cannot alter or drop
unique constraints in place.

The minimal additive schema addition is a new version history table in `PROJECT_KNOWLEDGE_SCHEMA_STATEMENTS`:

```sql
CREATE TABLE IF NOT EXISTS simulation_result_value_versions (
    id TEXT PRIMARY KEY,
    value_id TEXT NOT NULL,
    version_index INTEGER NOT NULL,
    value_text TEXT NOT NULL,
    unit TEXT NOT NULL,
    conditions_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'exception', 'corrected', 'confirmed')),
    action TEXT CHECK (action IN ('ingest', 'correct', 'confirm')),
    reason TEXT,
    actor TEXT,
    created_at TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'recorded' CHECK (origin IN ('recorded', 'backfilled')),
    FOREIGN KEY (value_id) REFERENCES simulation_result_values(id),
    UNIQUE(value_id, version_index)
);
CREATE INDEX IF NOT EXISTS idx_sim_result_value_versions_lookup
    ON simulation_result_value_versions(value_id, version_index);
```

- Ingestion inserts version 0.
- `resolve_simulation_result_exception` appends the next `version_index` instead of overwriting intermediate history,
  while still updating `simulation_result_values` to the latest head state for lookup and validation queries.
- Existing data is backfilled idempotently on startup, with `origin = 'backfilled'`, and never claims a complete
  chain. Code facts (`resolve_simulation_result_exception`): only an `exception` value can be resolved; a correction
  that still fails its checks stays `exception` and can be corrected again, overwriting the single resolution row while
  `original_*` keeps the ingested value. So a legacy value yields at most two versions: version 0 from `original_*`
  (status `exception`, the value row's `created_at`) and version 1 as the current head. The head's value, unit and
  conditions come from the current `simulation_result_values` row, not from `corrected_*`, because a `confirm` after a
  failed correction stores `corrected_value_text = NULL` while the head keeps the corrected value. A value without a
  resolution row yields only version 0 from the current row. The original `quantity` is not stored and is not
  reconstructed.
- The chain read returns `intermediate_versions_unknown: true` for any chain that holds a backfilled resolution
  version: corrections overwritten before this migration cannot be recovered, and the API says so instead of
  presenting the backfilled chain as complete.
- `freshness_invalidations` requires no schema change: flowsheet parameters use the existing parameter FKs, and
  requirement validations track result values through `project_knowledge_validation` and `_validation_row_admissible`.

Any migration follows `docs/MIGRATIONS.md`: a new `CURRENT_SCHEMA_MIGRATION_ID`, equivalent fresh and upgrade shapes,
the discipline digest and baseline in `backend/tests/test_migration_discipline.py`, and proof on a copy of a real data
root.

## Acceptance criteria

1. For a run with linked parameters, the manifest lists exactly the parameter versions in its stored input payload. It
   is unchanged after one of those parameters is superseded, and it then shows that a newer version exists.
2. A historical run without canonical linked parameters returns `history_not_recorded` with a reason, never a guessed
   manifest.
3. The version chain of a superseded parameter and of a corrected result value lists every version in order (initial
   ingestion plus all subsequent corrections/confirmations), with source, validation state, reason, actor, and
   timestamp. A red-dot version is never shown as validated. A backfilled legacy chain has at most version 0 and the
   head, and reports `intermediate_versions_unknown: true`.
4. Comparing two versions of the same value returns both sides and their users (runs and requirement checks). Comparing
   versions of different values is refused with `not_same_value`.
5. Correcting a result value appends a new version row without destroying prior versions, and marks requirement checks
   stale through `_validation_row_admissible` and derived parameters through `prepare_freshness_invalidation`. A test
   proves that no duplicate invalidation path was added.
6. No read endpoint writes anything. The additive migration passes the fresh/upgrade discipline test and is proven on a
   copy of a real data root.
7. Browser walk: from `/`, open a run, see "Values used" with one superseded parameter marked "newer version exists",
   open its History and compare the two versions.

## Tests

Focused backend tests for the manifest, the chains, the comparison and freshness/revalidation reuse; the migration
discipline test for the additive version history table; a frontend `.mjs` test for the new labels and views; the e2e
walk above.

## Decisions from review

- **Additive migration is required:** Code inspection of `sim_results.py` and `project_knowledge_schema.py` revealed
  that in-place updates and `UNIQUE(value_id)` on `simulation_result_resolutions` destroy intermediate corrections.
  Preserving every version as mandated by 248 point 6 requires an additive `simulation_result_value_versions` table.
- **Calculations in scope:** Runner runs (`simulation_runs` with linked parameters) and requirement checks
  (`project_knowledge_validation`) are the only calculations with value bindings in this slice. Pre-run checks (257) are
  on-demand reports over working revisions. No separate calculation manifest table is needed.
- **Freshness integration:** Staleness for result-value corrections is already handled cleanly by existing mechanisms:
  flowsheet parameters via `prepare_freshness_invalidation` in `_invalidate_dependent_evidence`, and requirement checks
  dynamically via admissibility in `revalidation_status_from_connection`. No alterations to `freshness_invalidations`
  are needed.

## Definition of done

Acceptance criteria 1–7 hold with exact-head CI green. High risk because an additive migration lands: one severe
review, a full diff review by a second qualified session, the migration proven on a copy of a real data root, and the
browser walk evidence attached.
