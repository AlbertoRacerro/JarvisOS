# 235 — Viewport geometry editing, slice 1: feature inspector

Status: ready
Depends on: 234

## Goal

In Design, a component's Geometry node shows an editable feature list next to the 3D viewport. Without Studio, the
user can create geometry for a component that has none, add, edit, enable/disable or remove `feature_sequence_v1`
features and their dimensions through visible controls, rebuild, and see the new geometry in the viewport. Undo
returns to the previous sequence by writing it back as a new revision, so the candidate lineage
(`parent_candidate_id`) stays complete and downstream CFD and reduced-model results become obsolete through the
existing digest checks.

## Why

W3 of `docs/strategy/ROADMAP_COMSOL_DIRECTION_2026-10-30.md` (sections 3 and 7), queue item 4 of maintainer directive
`L20261103141101-7c11`. Spec 234 shipped the feature-history backend with no UI. The first useful W3 step is to put
that authority on screen with ordinary form controls; gizmos, mouse placement and sketching build on it later.

## Scope

In scope:
- Frontend client functions for the existing `GET` and `PUT .../components/{component_id}/geometry/feature-history`
  routes, next to `attachDesignGeometry` in `frontend/src/api/designProject.ts`.
- A feature inspector in the Design Geometry node (`DesignProject.tsx` or a new component beside it):
  - lists features in order with kind, `feature_id`, enabled toggle and their dimension fields (mm or deg);
  - "Add" buttons for the existing catalogue (`tube_run`, `bend`, `manifold`, `serpentine`) with valid defaults; a
    new feature after the first connects to the previous feature's outlet port when the kinds allow it;
  - remove a feature; numeric dimension editing with client-side checks that mirror the 234 refusals (finite, > 0,
    wall thickness < outer radius) and show the server's structured 422/409 message when the server refuses;
  - "Rebuild geometry" sends `PUT` with `expected_revision`, `expected_active_version_id` and, for Process-linked
    components, `expected_draft_revision`; on success the page reloads the component, so the viewport shows the new
    GLB and the downstream nodes show their refreshed state;
  - an "Undo" control holding the sequences this page applied in the current session (cleared when the component or
    version changes); undo PUTs the previous sequence as a new revision. A read-only lineage line shows the current
    feature-history revision and whether it is stale against the attached candidate
    (`stale_against_attached_candidate`).
  - When `persisted` is false and a sequence was synthesized, the inspector starts from it and says so; when the
    component has no geometry, it starts empty with the Add buttons.
- Frontend `.mjs` source test for the inspector, added to the `build` chain.
- An e2e walk from `/` in a clean profile on a temporary data-root copy: open a Design component, add a tube, rebuild,
  see the 3D preview, change its length, rebuild, undo, and record the revision/candidate lineage shown.

Out of scope (binding non-goals):
- TransformControls gizmos, mouse placement, plane picking, sketches, constraints, booleans, new primitive kinds
  (box, sphere) or a command line; Bubu natural-language proposals. These are later 235 slices.
- Any backend, schema or migration change. If a backend gap blocks an acceptance criterion, stop and report it.
- A durable server-side undo stack; Design version fork remains the durable checkpoint.
- Editing Studio-model geometry or `parameters` references (`$param`) beyond showing them read-only.

## Files likely touched

- `frontend/src/api/designProject.ts`
- `frontend/src/pages/DesignProject.tsx` and a new `frontend/src/pages/DesignFeatureInspector.tsx` (or similar)
- `frontend/src/pages/designProject.css`
- `frontend/tests/235_feature_inspector.test.mjs`, `frontend/package.json` (build chain)
- `e2e/design_feature_inspector_walk.mjs`

## Design constraints

- `design_component_geometry.candidate_id` stays the only geometry authority; the inspector never attaches a candidate
  except through the 234 `PUT`.
- No client-side geometry computation or preview: the viewport shows only server-built, validated GLBs.
- Failure leaves the screen truthful: after a 409/422 the previous geometry stays displayed and the edited sequence
  stays in the form, marked not applied.
- Features using `$param` references or a `frame` are displayed and preserved unchanged on rebuild.
- Keep it small: one inspector component, no new state library, no new dependency.

## Acceptance criteria

1. On a component with no geometry, adding a `tube_run` with default dimensions and pressing "Rebuild geometry"
   attaches a `feature_history` candidate and the viewport shows its 3D preview, without opening Studio.
2. Changing a dimension and rebuilding creates a new candidate whose parent is the previous one; the inspector shows
   the incremented revision.
3. Undo rebuilds the previous sequence as a new revision; the attached geometry matches the earlier `spec_id`.
4. An invalid dimension is refused before sending, and a server refusal leaves the previous geometry attached and
   displayed with the server's message visible.
5. Process-linked components send `expected_draft_revision`; a concurrent change returns the server's conflict message,
   not a silent overwrite.
6. Frontend build (static tests, `tsc`, `vite build`) passes; the e2e walk passes from a clean profile at `/` using
   visible controls only, with its evidence directory attached to the PR.

## Required tests

- `frontend/tests/235_feature_inspector.test.mjs`: API calls and payload fields, Add/remove/enable controls,
  client-side refusals, undo stack reset on component change, no direct provider/filesystem calls.
- `e2e/design_feature_inspector_walk.mjs` covering criteria 1–4 against the real backend on a temporary data root.

## Definition of done

Test gate green (`AGENTS.md`), UI walk evidence attached, CI green on the exact head, one severe diff review, and
`STATUS.md` updated.
