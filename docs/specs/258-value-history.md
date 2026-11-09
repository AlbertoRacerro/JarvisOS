# 258 — Automatic value history: which value versions a run used, and comparing two versions (248 slice B)

Status: planned (draft for review; not ready)
Depends on: none (the approved [248](248-design-value-path.md) contract binds through Authority; slice A landed as
[253](253-sim-results-requirement-verification.md))

## Goal

Complete slice B of the approved design-value path, point 6 of the 248 contract: history is automatic and git-like,
per project. Every version of a value, a run and a calculation is recorded when it is created. Any run shows which value
versions it used, and two versions of a value can be compared. A new version marks dependent checks stale through the
existing freshness graph. There is no snapshot step and no snapshot UI.

Risk: high (an additive migration may be needed; see Schema). If review shows that existing records are enough, the
risk drops to medium, and the spec records that decision before it becomes `ready`.

## Authority

Maintainer answer `L20261106093156-1712` approves the binding contract in [248](248-design-value-path.md), with the slice
order A, then B and C. Slice A is merged (253) and slice C is drafted ([257](257-precheck-dof.md)). This spec is a draft
written as readiness work. It becomes `ready` only after its own review, as 248 requires.

## Why

Most of the history already exists, but it is split across owners and cannot be read as one:

- `parameters` keep a version chain through `supersedes_parameter_id`, and `freshness_invalidations` and
  `freshness_marks` (`backend/app/modules/flowsheet/freshness.py`) mark dependents stale when a parameter is replaced.
- A runner run with a schema-v2/v3 input contract names its linked parameters by `source_parameter_id` in the
  normalized input payload (`backend/app/modules/runner/linked_parameters.py` `linked_parameter_items`).
- Simulation result values from 253 carry their payload digest and extractor version (`simulation_result_values`), and a
  correction keeps the original next to the corrected value (`simulation_result_resolutions`).
- A requirement check names the run and scalar it used (`project_knowledge_validation.source_run_id`,
  `source_scalar_id`, `supersedes_validation_id`).

No read gives "the value versions this run used", and no read compares two versions of the same value.

## Scope

1. **Run inputs manifest (read).** For a run, a read-only API returns every value version the run used: each linked
   parameter version, with its id, value, unit, status and position in its `supersedes` chain, plus whether a newer
   version exists. It is derived from the stored input payload, not from current parameter rows, so a later edit never
   rewrites what the run used. A run whose stored contract has no canonical linked parameters (historical, schema v1)
   gives `history_not_recorded` with that reason. It never guesses.
2. **Value version chain (read).** For a parameter or a simulation result value, a read-only API returns its ordered
   versions: the parameter `supersedes` chain, or the original and corrected forms of a result value. Each version shows
   its source, its validation state (passed or red dot) and its creation time.
3. **Compare two versions (read).** Given two versions of the same value, the API returns value, unit, conditions,
   source and validation state side by side, and the runs and requirement checks that used each one. Versions of
   different values are refused with `not_same_value`.
4. **Staleness reuses freshness.** A new version of a value marks the checks and runs that used the old one stale through
   the existing `prepare_freshness_invalidation`/`persist_freshness_invalidation` path. Only result-value corrections
   are new here; parameter replacement already does this. No second invalidation engine.
5. **Surfaces.** The run detail view gains a read-only "Values used" section, and the value views gain "History" with a
   compare action. No snapshot button and no snapshot page (248 non-goal).

## Non-goals

- Snapshot steps, snapshot UI, Consist, AiiDA, or any new provenance framework (248 point 6 and its non-goals).
- Rewriting historical runs to add a manifest they never recorded.
- Literature and property-library values (slices D–F). The chain API is shaped so that they can join later.
- Restoring an old version. A user who wants it back enters it again as a new version.
- Changing validation, revalidation or reconcile semantics.

## Schema

The first choice is to derive everything from existing records. An additive migration is allowed only where derivation is
impossible. Two gaps are expected and must be confirmed during review:

- `simulation_result_resolutions` is unique per value, so a second correction of the same value has nowhere to go. If
  more than one correction must be kept, add the smallest additive link (for example a `supersedes` reference on
  resolutions or values) instead of overwriting.
- Result-value corrections have no row in `freshness_invalidations`, which today references `parameters` only. Reusing
  the freshness graph for them may need an additive, nullable source reference.

Any migration follows `docs/MIGRATIONS.md`: a new `CURRENT_SCHEMA_MIGRATION_ID`, equivalent fresh and upgrade shapes,
the discipline digest and baseline in `backend/tests/test_migration_discipline.py`, and the proof on a copy of a real data
root.

## Acceptance criteria

1. For a run with linked parameters, the manifest lists exactly the parameter versions in its stored input payload. It
   is unchanged after one of those parameters is superseded, and it then shows that a newer version exists.
2. A historical run without canonical linked parameters returns `history_not_recorded` with a reason, never a guessed
   manifest.
3. The version chain of a superseded parameter and of a corrected result value lists every version in order, with
   source and validation state. A red-dot version is never shown as validated.
4. Comparing two versions of the same value returns both sides and their users (runs and requirement checks). Comparing
   versions of different values is refused with `not_same_value`.
5. Correcting a result value marks the requirement checks that used it stale through the existing freshness path. A
   test proves that no other invalidation path was added.
6. No read endpoint writes anything. A migration, if added, passes the fresh/upgrade discipline test and is proven on a
   copy of a real data root.
7. Browser walk: from `/`, open a run, see "Values used" with one superseded parameter marked "newer version exists",
   open its History and compare the two versions.

## Tests

Focused backend tests for the manifest, the chains, the comparison and the freshness reuse; the migration discipline
test if the schema changes; a frontend `.mjs` test for the new labels; the e2e walk above.

## Open questions for the review

- Is one correction per result value enough (no migration for the chain), or must every correction be kept?
- Do calculations outside runner runs (for example Pre-Run checks or requirement checks themselves) need their own
  "values used" manifest in this slice, or is the run plus `project_knowledge_validation` enough?

## Definition of done

Acceptance criteria 1–7 hold with exact-head CI green. High risk if a migration lands: one severe review, a full diff
review by a second qualified session, the migration proven on a copy of a real data root, and the browser walk evidence
attached.
