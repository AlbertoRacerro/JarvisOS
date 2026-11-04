# 234 — Geometry feature history

Status: ready
Depends on: none

## Goal

A Design component version can record a versioned, deterministic sequence of parametric geometry features and scalar
parameters that compiles into a canonical `GeometrySpec` v0.1, builds and validates a `bluecad_candidates` record
through the existing deterministic BlueCAD pipeline, and attaches that validated candidate through the existing
`design_component_geometry` owner. Editing a parameter or the feature sequence and regenerating replaces the attached
candidate atomically on pass, leaves the existing attached geometry untouched on build or validation failure, and
propagates downstream CFD and reduced-model staleness automatically through the existing `geometry_digest` contract.

## Why

Item W2 of the COMSOL-direction roadmap (`docs/strategy/ROADMAP_COMSOL_DIRECTION_2026-10-30.md`, sections 3 and 7),
queue item 4 of maintainer directive `L20261103141101-7c11`. Before direct 3D viewport editing (Spec 235) can write back
parametric edits or undo steps, each component version needs a single deterministic feature-history record that compiles
to the existing `GeometrySpec` v0.1 authority without introducing a second geometry storage or rendering path.

## Scope

In scope:
- Component-scoped feature-history persistence in `design_component_feature_history` (one row per `component_id`,
  forked across Design versions in `design_versions._fork_content` alongside `design_component_geometry`):
  `component_id`, `sequence_json`, `sequence_digest`, `compiled_spec_id`, `candidate_id`, `revision`, `updated_at`,
  `updated_by`.
- A strict, canonical JSON schema (`feature_sequence_v1`) containing:
  - `parameters`: ordered named finite positive scalar parameters in millimetres or degrees (`name`, `value`, `unit`
    in `{"mm", "deg"}`), validated before evaluation;
  - `features`: non-empty ordered list of deterministic feature steps (`feature_id`, `enabled`, `kind`, `params`,
    optional `connect_to`), where each parameter value may be a finite number or a `{"$param": "<name>"}` reference to a
    declared parameter.
  - First useful feature catalogue (compiled deterministically into `GeometrySpec` v0.1 `parts` and `connections`):
    - `tube_run` (`outer_d_mm`, `wall_t_mm`, `length_mm`, optional `frame` or `connect_to`);
    - `bend` (`outer_d_mm`, `wall_t_mm`, `bend_radius_mm`, `angle_deg`, optional `frame` or `connect_to`);
    - `manifold` (`outer_d_mm`, `wall_t_mm`, `length_mm`, `branch_count`, `branch_outer_d_mm`, optional `frame` or
      `connect_to`);
    - `serpentine` macro feature (`outer_d_mm`, `wall_t_mm`, `segment_length_mm`, `segment_count`, `bend_radius_mm`),
      expanding deterministically into chained `tube_run` and `bend` parts as in `template.py`.
- Pure compiler `compile_feature_sequence(sequence) -> dict` in `backend/app/modules/bluecad/feature_history.py` that
  resolves `$param` references, drops disabled features (refusing an empty enabled set or a broken connection target
  with structured 422), and returns a canonical `GeometrySpec` v0.1 with its `spec_id` (`sha256:...`) and
  `sequence_digest`.
- Typed routes on `design_components.py`:
  - `GET /workspaces/{project_id}/design-versions/{version_id}/components/{component_id}/geometry/feature-history`:
    returns the stored feature history for the component, or a synthesized read-only view when the component has an
    attached candidate whose `GeometrySpec` is single-part or serpentine but no stored history row yet (with
    `persisted: false`), or `persisted: false` with empty sequence when no geometry is attached.
  - `PUT /workspaces/{project_id}/design-versions/{version_id}/components/{component_id}/geometry/feature-history`:
    validates optimistic concurrency (`expected_revision`, `expected_active_version_id`, and `expected_draft_revision`
    when the component has a Process unit source), compiles `sequence` to a canonical `GeometrySpec`, builds and
    validates a new `bluecad_candidates` record (`origin='feature_history'`, `parent_candidate_id` set to the previously
    attached `candidate_id` if any, `route_class='deterministic:feature_history'`), and **only if validation passes**
    updates `design_component_feature_history` and attaches the new candidate through the guarded
    `design_component_geometry` owner in one transaction.
- Failure atomicity: if schema/parameter validation fails (422) or if the BlueCAD kernel build/validation fails (409,
  with the failed candidate parked in `bluecad_candidates` for audit), neither `design_component_geometry` nor
  `design_component_feature_history` is modified and the component's `revision` is unchanged.

Out of scope (binding non-goals):
- Direct 3D viewport gizmos, mouse primitive placement, interactive plane picking, or 2D sketch constraint solvers
  (owned by Spec 235).
- Arbitrary Python script features or combining Studio scripts inside `feature_sequence_v1` (Studio geometry models
  remain on their existing `design_component_models` path).
- Replacing `GeometrySpec` v0.1, `bluecad_candidates`, or `design_component_geometry` as the geometry authority.
- New CAD kernel dependencies (uses the existing in-process `build123d` builder pipeline).

## Files likely touched

- `backend/app/core/schema.py` and `backend/tests/test_migration_discipline.py` (additive migration for
  `design_component_feature_history`)
- `backend/app/modules/bluecad/feature_history.py` (schema, `$param` resolution, deterministic compiler to
  `GeometrySpec` v0.1, candidate build helper)
- `backend/app/modules/workspaces/design_components.py` (`GET`/`PUT .../geometry/feature-history` routes)
- `backend/app/modules/workspaces/design_versions.py` (copy `design_component_feature_history` rows on version fork)
- `frontend/src/api/generated` (regenerated contracts)
- `backend/tests/test_234_geometry_feature_history.py`

## Design constraints

- Single geometry authority: `design_component_geometry.candidate_id` remains the sole pointer to a component's active
  3D geometry. `design_component_feature_history` records the parametric recipe that produced `candidate_id`. If a
  component's geometry is replaced later by a direct candidate attachment (`PUT .../geometry`) or Process handoff
  (`POST .../geometry/from-process`), `GET .../geometry/feature-history` reports `stale_against_attached_candidate: true`
  whenever `feature_history.candidate_id != design_component_geometry.candidate_id`.
- Deterministic compilation: identical `feature_sequence_v1` inputs (modulo key ordering) produce byte-identical
  canonical `GeometrySpec` JSON and identical `spec_id` and `sequence_digest`.
- Downstream staleness requires no bespoke invalidation table: when a parameter edit changes tube bore diameter or
  length, the compiled `GeometrySpec` gets a new `spec_id`, which changes `FluidDomain.geometry_digest` and marks
  existing CFD study templates, runs, and published reduced models obsolete through the existing digest checks.
- Optimistic concurrency: `PUT .../geometry/feature-history` checks `expected_active_version_id`, component
  `expected_revision`, and (when linked to a Process unit) `expected_draft_revision` before building, and re-verifies
  inside `BEGIN IMMEDIATE` before committing the attachment and bumping `design_components.revision`.
- Additive migration discipline (`docs/MIGRATIONS.md`): new `CURRENT_SCHEMA_MIGRATION_ID` (`0029_geometry_feature_history`),
  `CREATE TABLE IF NOT EXISTS design_component_feature_history` in `SCHEMA_STATEMENTS`, and updated
  `BASE_CREATE_ONLY_SHA256` in `backend/tests/test_migration_discipline.py`.

## Acceptance criteria

1. `compile_feature_sequence` resolves scalar parameters, skips disabled features, connects sequential features via ports
   when `connect_to` is given, and produces a valid canonical `GeometrySpec` v0.1 for `tube_run`, `bend`, `manifold`,
   and `serpentine` features; unknown parameter names, non-positive dimensions, wall thickness ≥ radius, or empty
   enabled features raise a structured validation error (HTTP 422 on the route).
2. `PUT .../geometry/feature-history` on a Design component builds a validated `bluecad_candidates` record with
   `origin == "feature_history"`, attaches it to `design_component_geometry`, stores the sequence in
   `design_component_feature_history`, and increments `design_components.revision`.
3. Editing a parameter (e.g. `length_mm` from `1000` to `1500`) and calling `PUT .../geometry/feature-history` creates a
   new candidate whose `parent_candidate_id` is the previous candidate, updates `design_component_geometry.candidate_id`,
   changes `GET .../fluid-domain` `geometry_digest`, and causes an attached CFD reduced model on that component to
   report `status == "obsolete"` with `"geometry_changed"` in `obsolete_reasons`.
4. Failure atomicity: when a feature sequence fails validation or when candidate build/validation fails, the previously
   attached `candidate_id`, stored `feature_history`, and component `revision` remain unchanged.
5. Forking a Design version copies `design_component_feature_history` to the forked component so the new version
   retains the editable feature sequence and lineage.

## Required tests

- `backend/tests/test_234_geometry_feature_history.py`: compiler determinism and validation refusals; `GET`/`PUT`
  feature-history lifecycle; parameter edit regeneration and parent candidate lineage; downstream `fluid-domain` digest
  change and reduced-model obsolescence; failure atomicity preserving prior candidate; version fork preservation.
- `backend/tests/test_migration_discipline.py`: passes with migration `0029_geometry_feature_history`.

## Definition of done

Test gate green (`AGENTS.md`), acceptance criteria covered by offline tests in `test_234_geometry_feature_history.py`,
frontend contracts regenerated cleanly, and `STATUS.md` updated.
