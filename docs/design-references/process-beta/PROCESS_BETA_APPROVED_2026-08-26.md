# Process workspace — beta approved visual reference

**Maintainer decision:** 2026-08-26  
**Target route:** `/design/process`  
**Approved desktop render SHA-256:** `496e3e55973f92ad5d20a29646172cce1f265c8ded79c85162fcfe029c7e9430`  
**Approved deterministic HTML SHA-256:** `d3bb06d9a7c761699a21b9b6b0a1901214a799f06dd31570c2fbfedc018cc475`

## Status and authority

This file freezes the maintainer-approved **beta Process composition** after the 2026-08-26 shell update. It is documentation/reference material only. It does not release the post-100 visual-inspection hold, alter `docs/specs/STATUS.md`, authorize runtime work, or create process-engine authority.

The previously committed JPEG in this reference PR was corrupt and has been removed. The hashes above identify the approved local render and deterministic HTML used during maintainer review. A later binary-capable repository update may add the exact decodable image without changing this composition contract.

`docs/product-direction/01-operator-information-architecture.md` (not carried into this repository) and `02-design-workbench-contract.md` (not carried into this repository) are the governing future-product direction where this file is silent.

## Global shell — approved

The normal primary rail is exactly:

- `Design`
- `Memory`
- `Development`
- `Coding`
- `Settings`

Do **not** restore `Home`, `Runs`, `Engineering Data`, `Review`, `Results`, `Evidence`, `Files`, `History`, or `Lineage` as peer primary destinations.

`JARVIS OS` remains at the upper-left with a compact runtime-health indicator beside the brand. Detailed diagnostics belong in `Settings > System`.

## Design navigation — approved

Inside Design, the normal peer work modes are exactly:

- `Process`
- `BLUECAD`

Do not keep `Model`, `Results`, or `Lineage` as permanent Design tabs.

A thin contextual anchor strip carries non-workspace context such as:

`Model v0.14 · Current ✓ · Last run #184 ✓ · 3 proposals · 6 sources`

The values above are visual fixture data only. Runtime implementation must populate equivalent anchors from authoritative backend state and deep-link/open the owning records; the strip must not become a duplicate store.

## Process composition — hard visual requirements

- warm light-first limestone/ivory visual system;
- narrow persistent primary rail;
- workspace header with `DESIGN`, `Process workspace`, and Appearance control;
- only `Process | BLUECAD` as Design tabs;
- compact contextual anchor strip rather than Model/Results/Lineage pages;
- compact icon-first process toolbar spanning the workspace below the tabs;
- three-column workbench: process-equipment navigator, dominant warm-grid canvas, right inspector;
- right inspector split vertically into **Jarvis** above and **Properties** below;
- Properties values visibly editable where the selected engineering object permits editing;
- central canvas near-white with faint warm-gray grid lines;
- no green ambient wash behind the engineering canvas;
- chlorophyll green used for active/focus/status emphasis rather than broad background fill;
- predominantly square/nearly square geometry with small radii;
- official Phosphor generic icons when implementation is authorized;
- no overlapping labels, clipped controls, or wasted utility-bar regions.

## Toolbar/toolbox intent

The approved visual scaffold includes future affordances such as Select, Pan, Add equipment, Connect, Disconnect, Multi-select, Duplicate, Delete, Fit view, Zoom, Undo, Redo, Auto-layout, Validate and Solve, plus representative process equipment.

These are product/visual intent only until a separate implementation spec binds each action to real server-owned process/evaluator contracts. Do not fabricate backend semantics merely to match the reference.

## Shared-component preservation

The Process Jarvis/Properties inspector is a stable approved component. BLUECAD and adjacent Design workspaces should reuse that component and its spacing/typography rather than rebuilding it from scratch unless a separately approved product reason requires a semantic difference.

## Future implementation acceptance

When Process runtime implementation is separately authorized, the builder must reproduce this composition and compare the real screen against the approved local reference identified above. Hard acceptance includes:

- rail and Design-tab information architecture;
- contextual-anchor placement;
- overall panel proportions;
- central-canvas dominance;
- warm light-stone palette and chlorophyll accent role;
- near-square geometry;
- Jarvis/Properties split;
- editable Properties presentation;
- toolbar density and equipment-navigator organization;
- no reintroduction of deprecated primary destinations.

Small copy differences and backend-driven state differences are acceptable only where required by an authoritative runtime/accessibility contract.
