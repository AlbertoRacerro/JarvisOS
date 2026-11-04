# 235 — Viewport geometry editing, slice 2: feature placement

Status: ready
Depends on: 234

Slice 1 (feature inspector, merged in PR #17) is in Git history. This file is the accepted contract for slice 2.

## Goal

In the Design Geometry node, the user can place a feature where it belongs instead of only chaining it to the
previous outlet: type its position and direction, or drag a move/rotate handle in the 3D viewport, then rebuild. The
geometry the viewport shows after a rebuild is always the server-built GLB; the handle only edits the feature's
`frame` and marks it "not applied" until the rebuild succeeds.

## Why

W3 of `docs/strategy/ROADMAP_COMSOL_DIRECTION_2026-10-30.md` ("TransformControls gizmo writing back `transform`
features"), queue item 4 of maintainer directive `L20261103141101-7c11`. Slice 1 can only build one connected chain
from the origin: a second, separate pipe run or a manifold placed beside the main run cannot be made without Studio.
The 234 backend already accepts an optional `frame` (`origin` and `direction`, millimetres, CAD Z-up) on every
feature, and the inspector today only shows it read-only. `assembly.py` `_frame_placement` uses the origin and only
the plan heading of `direction` (`atan2(y, x)`, a rotation about CAD Z; the z component is ignored) and only for parts
not placed by a connection; the default is origin (0, 0, 0), heading +X. Placement is therefore frontend-only and is
the smallest step that makes mouse modelling real.

## Scope

In scope:
- Inspector, per feature:
  - a placement choice for every feature except the first: "Connected to previous outlet" (today's behaviour, writes
    `connect_to`) or "Placed freely" (writes `frame`, removes `connect_to`); manifolds stay never auto-connected.
    "Placed freely" is offered only for single-part kinds (`tube_run`, `bend`, `manifold`): `assemble_parts` walks
    connections only from the first part of the whole spec, so the inner runs of a detached `serpentine` would not
    follow its frame; a serpentine is placed freely only as the first feature;
  - for a freely placed feature (and the first feature), editable fields Position X/Y/Z (mm) and Heading (deg, in
    plan, 0 = +X), written as `direction` (cos, sin, 0); an existing `frame` whose direction has a non-zero z is
    shown with a note that only its plan heading is used and is preserved unless the user edits it; a feature without
    a `frame` shows the default and only writes a `frame` once the user edits it;
  - client-side refusal of non-finite values, mirroring `spec.py` `_validate_frame`.
- Viewport handle: a "Place in viewport" toggle on a freely placed feature shows three.js `TransformControls`
  (from `three/examples/jsm`, already shipped with the `three` dependency) on a small placement marker (axes at the
  frame origin pointing along the heading), in move mode (three axes) and rotate mode limited to the CAD Z axis.
  Dragging updates that feature's Position and Heading fields (rounded to 0.1 mm and 0.1 deg) and marks the sequence
  not applied. The marker converts between the CAD Z-up frame and glTF Y-up exactly as the
  viewer's existing export convention; OrbitControls are paused while dragging.
- "Rebuild geometry" and session Undo work unchanged through the 234 `PUT`; a placement change is one more revision.
- Frontend `.mjs` source test extended or added, in the `build` chain.
- E2E walk from `/` in a clean profile on a temporary data-root copy: create a tube, add a second tube "Placed
  freely", type a position, rebuild, see both in the viewport; drag the handle, see the fields change and the
  not-applied mark, rebuild; undo restores the previous placement.

Out of scope (binding non-goals):
- Moving or rotating the server mesh on the client, or any client-side geometry preview; the marker is the only
  client-drawn object.
- Scale, tilt or roll (any rotation not about CAD Z), snapping, plane picking, sketches, constraints, booleans, new primitive kinds, a `transform` feature
  kind, a command line or Bubu proposals: later slices.
- Any backend, schema or migration change. If a backend gap blocks an acceptance criterion, stop and report it.
- Detached `serpentine` placement (needs the assembler to place every connected group from its own frame).
- Selecting a feature by clicking its mesh (part-to-feature mapping is not guaranteed for macro features); selection
  stays in the inspector.

## Files likely touched

- `frontend/src/pages/DesignFeatureInspector.tsx`
- `frontend/src/components/BluecadGlbViewer.tsx` (an optional placement-marker prop and change callback)
- `frontend/src/pages/DesignProject.tsx` (wiring), `frontend/src/pages/designProject.css`
- `frontend/tests/235_feature_inspector.test.mjs` or a new `frontend/tests/235b_feature_placement.test.mjs`,
  `frontend/package.json` (build chain)
- `e2e/design_feature_placement_walk.mjs`

## Design constraints

- `design_component_geometry.candidate_id` stays the only geometry authority; nothing attaches geometry except the
  234 `PUT`.
- Failure leaves the screen truthful: after a 409/422 the previous geometry stays displayed and the edited placement
  stays in the form, marked not applied.
- Features with `$param` references stay preserved unchanged; placement edits only `frame`/`connect_to`.
- The marker lives in the same scene transform as the loaded GLB (any centring or unit scaling the viewer applies to
  the model applies to the marker), so a typed position and the rebuilt geometry coincide on screen.
- No new dependency, no new state library; the viewer change is opt-in so other viewer users are unaffected.

## Acceptance criteria

1. A second `tube_run` set to "Placed freely" with Position (0, 100, 0) mm rebuilds into a candidate whose
   `GeometrySpec` part carries that frame, and the viewport shows two separate tubes.
2. Dragging the handle changes the Position/Heading fields and shows "not applied"; Rebuild makes a new revision
   whose part frame equals the fields; Undo restores the previous frame as a new revision.
3. A non-finite value is refused before sending; a server refusal leaves the previous geometry
   displayed with the server's message visible.
4. Switching a feature back to "Connected to previous outlet" removes its `frame` and restores the chained geometry.
5. Frontend build (static tests, `tsc`, `vite build`) passes; the e2e walk passes from a clean profile at `/` using
   visible controls only (the drag uses real pointer events on the handle), with its evidence directory attached to
   the PR.

## Required tests

- Static test: placement choice writes `frame` xor `connect_to`, non-finite refusals, heading ↔ `direction`
  conversion, axis conversion helper (CAD Z-up ↔ glTF Y-up) round-trips, no direct provider/filesystem calls.
- `e2e/design_feature_placement_walk.mjs` covering criteria 1–4 against the real backend on a temporary data root.

## Definition of done

Test gate green (`AGENTS.md`), UI walk evidence attached, CI green on the exact head, one severe diff review, and
`STATUS.md` updated.
