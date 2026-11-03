# Maintainer decision — final operator frontend fusion priority — 2026-08-27

Status: explicit maintainer queue-direction decision; no runtime authority by itself.

## Decision

The maintainer wants the complete maintainer-approved operator workstation to become directly inspectable in the real React/Vite JarvisOS frontend **before** proceeding through the later backend/domain implementation queue.

The visible milestone is one organically unified application derived from all eleven canonical HTML references indexed by `APPROVED_OPERATOR_UI_MANIFEST_2026-08-27.md`, not eleven disconnected demos and not a new redesign.

The intended primary shell is exactly:

`Design | Memory | Development | Coding | Settings`

with:

- Design: `Process | BLUECAD`;
- Memory: `Project Basis | Models | Literature`;
- Development: `Roadmap | Brainstorm`, with Roadmap `Timeline | Calendar`;
- Coding: `Repository | Runtime`;
- Settings: `Appearance | AI | System`.

There is no normal Home destination.

## Frontend-first boundary

This decision does **not** authorize fake backend functionality.

The frontend fusion must:

1. use truthful current backend data/read/action contracts wherever those contracts already exist;
2. preserve existing working capabilities while moving them under their final contextual owner where possible;
3. render approved controls/regions whose backend owner does not yet exist as explicit disabled/read-only/unavailable/future states when the active spec permits that staged delivery;
4. never replace fixture values from canonical HTML with invented production values;
5. never create React-owned canonical engineering/project/repository/runtime state solely to make the screen look populated;
6. never implement a missing COMMIT/EXECUTE capability in the browser merely because the canonical HTML shows the affordance;
7. preserve every approved interaction class from `FINAL_OPERATOR_INTERACTION_CONTRACT_2026-08-27.md`.

The purpose of the frontend-first milestone is therefore **visual/product integration and truthful operability of already-backed actions**, not premature backend emulation.

## Organic-fusion rule

The implementation must translate the approved HTML into the production React/Vite application and shared components. It must not:

- iframe or embed the canonical HTML files as production pages;
- ship eleven isolated copies of the application shell;
- maintain duplicate design tokens or duplicate Jarvis/Properties implementations per surface;
- restore legacy Home/Runs/Engineering Data/Review/Results/Lineage/Evidence/Files/History as peer destinations;
- substitute generic dashboards, card grids, Kanban boards or an IDE clone for the canonical compositions.

Shared shell, typography, appearance tokens, primary rail, workspace headers, peer tabs, Jarvis language and common controls should be structurally shared where semantics are actually common. Surface-specific composition remains governed by each canonical HTML.

## Current-master migration evidence

At this decision point, exact `master` still exposes the older application IA in `frontend/src/app/routes.ts`:

- primary nav: Home, Design, Runs, Engineering Data, Review, Settings;
- Design stages: Model, Process, Results, Lineage.

`frontend/src/App.tsx` still renders Home, Runs, Engineering Data and other older route-owned surfaces directly.

Therefore the final frontend fusion is a real route/information-architecture migration, not a cosmetic restyle.

## Intended queue effect

After the final preservation/specification packet in PR #388 is merged, a dedicated frontend-only canonical slice should be inserted **before 100a and before the later backend/domain queue**.

That slice must pass the normal registry/full-spec/readiness/implementation lifecycle and must remain independently removable from later backend work.

After the frontend fusion is merged and manually/browser inspected, the queue resumes with 100a/100b and the later 100c authority re-derivation/backend-domain work unless the maintainer explicitly changes direction again.

## Proof expectation

The frontend-fusion implementation is not complete until the real production app has deterministic browser evidence for every canonical surface at its manifest reference viewport, including required interaction states, and the evidence is tied to one exact PR head.

The maintainer should be able to run that exact implementation and inspect the complete final shell before later backend functionality fills currently unavailable controls.
