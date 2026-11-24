# 350 — DESIGN-PHYSICS-WIRING-1: Wire marine motion and axial tube-oxygen physics into the Design stages

Status: ready
Depends on: 324, 325, 338

## Goal

Connect every existing deterministic physics capability into the Design flow consistently (`Plant → Dynamic → Motion/Mechanics → CFD/Optics → Compare`), so the engineer finds each capability directly inside the Design stage where they expect it and operating/geometry/environment inputs flow between stages instead of living only on the bench or behind a placeholder.

## Why

Directive `L20261123232512-a0c3` (item 4): temperature, carbonate/pH, and wall optics/fouling are already exposed in the Dynamic scenario editor (Spec 325), but the 3-DOF wave-motion & mooring evaluator (`marine.rigid_body_waves`, Spec 324) and the axial dissolved-oxygen tube evaluator (`photobioreactor.tube_oxygen`, Spec 338) live only on the bench, while the Design screen still shows `"Motion/Mechanics (placeholder)"`.

## Code facts checked at `b5093b37`

- `frontend/src/pages/DesignProject.tsx::DesignProject` renders the Design stage bar (`Plant`, `Dynamic`, `CAD`, `Motion/Mechanics`, `CFD`, `Optics`, `Reduced model`) and currently labels `Motion/Mechanics` as a placeholder (`"Motion/Mechanics is a placeholder; calculations are not available here yet"`).
- `backend/app/modules/engineering/marine_motion.py::_evaluate` implements the deterministic 3-DOF linear RAO & quasi-static mooring evaluator (`marine.rigid_body_waves`) registered in the engineering bench.
- `backend/app/modules/photobioreactor/axial_oxygen.py::outlet_concentration` and `backend/app/modules/photobioreactor/axial_oxygen.py::max_run_length` implement the deterministic plug-flow axial dissolved-oxygen build-up and safe run-length evaluator (`photobioreactor.tube_oxygen`) exposed on the bench.
- `frontend/src/pages/DesignDynamicPanel.tsx` hosts the Dynamic stage scenario editor and run results (including Spec 325 thermal, carbonate/pH, and flat-wall optics/fouling fields).

## Scope

In scope:
- **Stage 1 — Replace `"Motion/Mechanics (placeholder)"` with a live `DesignMotionPanel`**:
  - Remove the `(placeholder)` suffix and placeholder message on `Motion/Mechanics` in `DesignProject.tsx`.
  - Add `frontend/src/pages/DesignMotionPanel.tsx` backed by existing workspace bench evaluation endpoints for `marine.rigid_body_waves`:
    - Pre-fills wave height and wave period from the active workspace's Environment profile when available, and mass/waterplane/draft hints from the selected component's geometry/properties when available (all editable directly in the panel).
    - Runs `marine.rigid_body_waves`, displays heave/surge/pitch RAO amplitudes, max acceleration, and quasi-static mooring tension with check-sheet status and verdict badge, and lists saved motion runs for variant comparison.
- **Stage 2 — Surface axial tube-oxygen build-up inside `Plant` / `Dynamic`**:
  - Add an `"Axial O2 (tube)"` panel/drawer in `DesignDynamicPanel.tsx` (and reachable from a tubular unit in `Plant`) backed by the existing `photobioreactor.tube_oxygen` bench card:
    - Pre-fills tube length, inner diameter, liquid velocity, temperature, and volumetric O₂ production rate from the active Process draft / latest Dynamic run when available (all editable directly in the panel).
    - Runs `photobioreactor.tube_oxygen` and displays outlet O₂ concentration, saturation ratio, and maximum safe run length with verdict badge, persisted as a workspace calc run for comparison.
- **Stage 3 — Consistent cross-stage parameter handoff & variant comparison**:
  - Add a small pure helper (`frontend/src/components/design/physicsHandoff.ts`) mapping active Process draft + Environment + CAD component metadata into initial defaults for Dynamic, Motion/Mechanics, Axial O₂, and Optics, with explicit provenance labels (`"from Process"`, `"from Environment"`, `"manual"`).
  - Surface Motion/Mechanics and Axial O₂ run verdicts alongside Dynamic/CFD/Optics runs in the Design variant/version summary so variants can be compared end-to-end across all stages.

Out of scope (binding non-goals):
- No new physics equations, no schema migrations, and no private/project-specific defaults in the public repository.
- No changes to existing `marine.rigid_body_waves` or `photobioreactor.tube_oxygen` math or conformance tests.

## Files likely touched

- `frontend/src/pages/DesignProject.tsx`
- `frontend/src/pages/DesignMotionPanel.tsx` (new)
- `frontend/src/pages/DesignDynamicPanel.tsx`
- `frontend/src/components/design/physicsHandoff.ts` (new)
- `frontend/tests/designMotionPanel.test.mjs` (new, added to `frontend/package.json` build chain)
- `frontend/tests/` existing Design `.mjs` tests updated where `"Motion/Mechanics (placeholder)"` was asserted
- `e2e/` browser walk covering `Design -> Motion/Mechanics` and `Design -> Dynamic -> Axial O2`

## Design constraints

- Reuse existing backend bench evaluation routes (`marine.rigid_body_waves`, `photobioreactor.tube_oxygen`); no duplicate backend calculation routes unless a thin read-only prefill helper is needed.
- Direct editing in every screen: every prefilled value is editable by the engineer and clearly shows its source.
- UI-changing PR: requires an AI browser walk from `/` in a clean profile covering the new panels before maintainer inspection.

## Acceptance criteria

1. Opening Design shows `Motion/Mechanics` without `(placeholder)`; selecting it renders `DesignMotionPanel`, pre-fills available Environment/component parameters, executes `marine.rigid_body_waves`, and displays RAO/mooring outputs and verdict.
2. Opening `Dynamic` (or a tubular unit in `Plant`) exposes the `Axial O2 (tube)` panel, pre-fills available Process/Dynamic parameters, executes `photobioreactor.tube_oxygen`, and displays outlet O₂ and max run length with verdict.
3. Cross-stage handoff helper (`physicsHandoff.ts`) preserves user edits and clearly labels prefilled vs manual values; runs persist in the workspace and appear for comparison.
4. `frontend` build (`.mjs` tests, `tsc`, `vite`) and an AI browser walk from `/` pass with zero console/page errors.

## Required tests

- Frontend static/logic tests (`frontend/tests/designMotionPanel.test.mjs` + updated Design tests in `frontend/package.json`).
- AI browser walk from `/` verifying reachability and live execution of both `Motion/Mechanics` and `Axial O2 (tube)` panels.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, coordinator diff review, AI browser walk evidence attached, spec status updated.
