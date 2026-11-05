# 235 — Viewport geometry editing, slice 3: freely placed serpentines

Status: merged
Depends on: 234

Slices 1 (feature inspector, PR #17) and 2 (feature placement, PR #22) are in Git history. This file is the accepted
contract for slice 3.

## Goal

A serpentine can be "Placed freely" like any other feature: the user sets its Position and Heading (typed or with
the slice 2 viewport handle), rebuilds, and the whole serpentine (every run and bend) appears at that place, beside
the main run instead of chained to it.

## Why

W3 of `docs/strategy/ROADMAP_COMSOL_DIRECTION_2026-10-30.md`, queue item 4 of maintainer directive
`L20261103141101-7c11`. Slice 2 deferred it by name: `assembly.py` `assemble_parts` walks connections only from the
first part of the whole spec. Every other part is placed by its own `frame` (default origin, heading +X), so the inner
runs and bends of a serpentine that is not the first feature ignore the serpentine's frame. The 234 compiler already
writes the feature `frame` on the serpentine's first part (`{feature_id}_run_1`). This means a heat-exchanger coil
next to a feed line cannot be modelled today without Studio.

## Scope

In scope:
- Backend, `backend/app/modules/bluecad/assembly.py` only: once the walk from the first part is done, take the first
  unplaced part in spec order, place it by its own `frame`, and walk its connections the same way. Repeat until every
  part is placed. Each connected group is then placed from the frame of its first part in spec order. Placement of
  the group that holds the first part, and of single unconnected parts, is unchanged. The existing consistency and
  port checks stay as they are.
- Frontend, `DesignFeatureInspector.tsx`: offer "Placed freely" for a `serpentine` that is not the first feature,
  with the same Position and Heading fields and "Place in viewport" handle as other kinds. Enable "Add serpentine"
  when the last enabled feature is not connectable (for example a manifold); the new serpentine starts "Placed
  freely" at the default frame.
- Backend unit tests and the extended frontend static test (already in the `build` chain).
- The slice 2 e2e walk extended, or a new walk, from `/` in a clean profile on a temporary data-root copy: tube, then a
  serpentine set to "Placed freely" at a typed position, rebuild, and the whole serpentine visibly away from the tube.

Out of scope (binding non-goals):
- Any change to `GeometrySpec`, `feature_sequence_v1`, the 234 compiler or route, the schema, or a migration.
- A frame on a part that is placed by a connection still has no effect (as today); no warning UI for it.
- Changing the placement of any spec that assembles today. This is not a re-placement of stored candidates: nothing
  is rebuilt automatically, and existing candidates and digests are untouched.
- Mesh-click selection, scale, tilt or roll, snapping, sketches, constraints, booleans, new primitives, a command
  line or Bubu proposals: later slices.

## Files likely touched

- `backend/app/modules/bluecad/assembly.py`
- `backend/tests/bluecad/test_assembly_groups.py` (new) or an existing bluecad assembly test
- `frontend/src/pages/DesignFeatureInspector.tsx`
- `frontend/tests/235b_feature_placement.test.mjs`
- `e2e/design_feature_placement_walk.mjs`

## Design constraints

- `design_component_geometry.candidate_id` stays the only geometry authority; nothing attaches geometry except the
  234 `PUT`.
- Deterministic: the result depends only on spec order and content, not on dictionary or set iteration order.
- A spec that assembled before assembles to the same placements (within the existing `ABS_TOL`/`REL_TOL`). A spec
  that failed before with `PORT_MISMATCH` only because a detached group ignored its frame now succeeds. Every other
  refusal stays a refusal.
- No new dependency; no client-side geometry beyond the slice 2 marker.

## Acceptance criteria

1. Backend: a spec with a tube run, then a serpentine whose `_run_1` frame is origin (0, 300, 0) with heading +Y and
   no connection to the tube, assembles. Every serpentine part is placed relative to that frame (the `_run_1` `port_a`
   sits at (0, 300, 0) facing −Y, so the first run extends along +Y), and every internal connection passes the coincident and
   opposed check.
2. Backend: the existing bluecad, feature-history and template tests pass unchanged. A regression test shows that a
   spec with a single connected chain and frames on non-root parts gives the same placements as before.
3. Frontend: a non-first serpentine offers "Placed freely" and switching to it writes `frame` and removes
   `connect_to`. Switching back restores the connection. "Add serpentine" is enabled after a manifold.
4. Frontend build (static tests, `tsc`, `vite build`) passes; the e2e walk passes from a clean profile at `/` using
   visible controls only, with its evidence directory attached to the PR.

## Required tests

- Backend unit tests for criteria 1–2, including determinism (two detached groups in either spec order are each
  placed from their own first part).
- Static test: serpentine "Placed freely" option present and wired to `frame`; add-serpentine enable rule.
- E2E walk covering criterion 3 against the real backend on a temporary data root.

## Definition of done

Test gate green (`AGENTS.md`), UI walk evidence attached, CI green on the exact head, one severe diff review, and
`STATUS.md` updated.
