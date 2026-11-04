# 242 — Bubu guided tour

State: **merged**, subject to the live registry in [STATUS.md](STATUS.md). Accepted by the coordinator under maintainer
directive `L20261103141101-7c11` item 5 ("visita guidata bubu aggiornate su linea comsol") and the acceptance rule of
`L20261103142125-d682`. It does not authorize later roadmap items.

## Goal

From any page, the operator can start a guided tour from Bubu that walks them, one stop at a time, through every main
area of the app and the COMSOL-line paths (CFD results with the 3D symmetry view, Geometry with the feature inspector).
Each stop navigates to the right page, points at a real visible control with the existing Bubu pointer, and says in one
or two plain sentences what it is for. Bubu's navigation guide (`thread_app_map.py`) also describes the COMSOL-line
surfaces, so a free-form "where is…" question about them gets a correct path.

## Why

The maintainer asked for Bubu's navigation and a guided tour, both covering what the COMSOL line adds (233, 234, 235).
A tour is the deterministic complement to model pointing (231): it needs no model, cannot point at the wrong component,
and shows a new user every path that exists.

## Scope

In scope:

1. A short, ordered, hand-maintained list of tour stops in the frontend. Each stop names a route, an existing
   `data-jarvis-target` id from `designTargets.ts` (or the rail), a title and one or two sentences. Stops cover: rail and
   project picker; Design stages; Components tree and nodes; Geometry node with the feature inspector (235); CFD stage →
   Results → Compare runs and the 3D symmetry view (233); Plant; Dynamic; More Design tools; Memory, Development,
   Coding, Settings. Keep the list to roughly 12–18 stops.
2. A visible "Guided tour" entry in Bubu (launcher or bubble) that starts it. The tour bubble shows the stop text,
   "n / N", Back, Next and Exit; Escape exits; keyboard reachable.
3. Each stop navigates with the app router to its route and points with the existing pointer at its target. Stops that
   need a selection (component, node) use the C1 case study when it exists, opened through the same visible control a
   user would use. A target that is not present (no case study, empty project, feature not available) shows a truthful
   one-line "not available here: <why>" and the tour continues; it never points at a different element or invents one.
4. Update `thread_app_map.py` area text for CAD/Geometry (feature inspector: add, edit, enable, rebuild, undo) and CFD
   Results (3D symmetry view), within the existing character budgets, from what the merged code actually shows.

Out of scope (binding non-goals):

- No model call, `ai_jobs` row, provider or backend endpoint for the tour; no persisted tour state or schema change.
- The tour never clicks Run, Rebuild, Save, Attach or any mutating control; it only navigates and selects, as a user
  browsing would. No data is created.
- No new pointer, overlay or animation framework; reuse `BubuPointer` and the bubble.
- No per-user progress tracking, analytics or onboarding gating.
- Spec 231's live pointing evaluation is not part of this spec.

## Files likely touched

- `frontend/src/components/bubu/` (tour stops, tour bubble controls, `useBubuIntegration.tsx`)
- `frontend/src/components/shell/BubuLauncher.tsx`
- `frontend/src/app/designTargets.ts` (only if a COMSOL-line control lacks a target id)
- `backend/app/modules/ai/thread_app_map.py` and its tests
- `frontend/tests/242_bubu_guided_tour.test.mjs`, `frontend/package.json`
- `e2e/bubu_guided_tour_walk.mjs`, `e2e/README.md`

## Design constraints

- Every stop target id must exist in `DESIGN_TARGETS` or be rendered with `data-jarvis-target`; a static test checks it.
- Tour text is literal UI copy in English like the rest of the app; it contains no instructions to the model.
- App-map text stays reference data within `MAX_AREA_CHARS` / `MAX_GUIDE_CHARS`.

## Acceptance criteria

1. From `/` in a clean browser profile, using visible controls only, the operator starts the tour and reaches the last
   stop with Next; each stop's route is shown and its target is pointed at, or a truthful "not available here" line.
2. With the C1 case study present, the Geometry stop points at the feature inspector and the CFD stop at Compare runs
   / the 3D view of a C1 component; with no case study, those stops say so and the tour still completes.
3. Back returns to the previous stop; Exit and Escape close the tour and the pointer; no mutating request is sent
   during the tour (walk records network requests and asserts no POST/PUT/DELETE).
4. `thread_app_map.py` mentions the feature inspector and the 3D symmetry view with their real target ids; its tests
   pass and budgets hold.

## Required tests

- Static `.mjs` test: stop list order, every target id known, no mutating control ids among stops, build chain entry.
- Backend: app-map test for the new text and budgets.
- `e2e/bubu_guided_tour_walk.mjs` on a temporary data-root copy with the C1 case study and on an empty one.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met with walk evidence, STATUS row updated.
