# BLUECAD workspace — beta approved visual reference

**Maintainer decision:** 2026-08-26  
**Target route:** `/design/bluecad`  
**Approved desktop render SHA-256:** `02bddfbea49e9632f8ca0a4c6cb3c2de533a50f8e96a83fbe4fc4ed67ca238f5`  
**Approved deterministic HTML SHA-256:** `da4ddfb1ebf6e0c7d39c133bf9b7c0da82e14428bad87748b0f7aa37a7e17bd9`

## Status and authority

This file freezes the maintainer-approved **beta BLUECAD composition** after the 2026-08-26 shell update. It is documentation/reference material only. It does not release the post-100 visual-inspection hold, alter `docs/specs/STATUS.md`, authorize runtime work, or grant any CAD/solver/backend authority.

`docs/product-direction/01-operator-information-architecture.md` (not carried into this repository) and `02-design-workbench-contract.md` (not carried into this repository) are the governing future-product direction where this file is silent.

## Global shell — approved

The normal primary rail is exactly:

- `Design`
- `Memory`
- `Development`
- `Coding`
- `Settings`

Do **not** restore `Home`, `Runs`, `Engineering Data`, `Review`, `Results`, `Evidence`, `Files`, `History`, or `Lineage` as peer primary destinations.

## Design navigation — approved

Inside Design, the normal peer work modes are exactly:

- `Process`
- `BLUECAD`

Do not keep `Model`, `Results`, or `Lineage` as permanent Design tabs.

A thin contextual anchor strip carries non-workspace context such as:

`Model v0.14 · Current ✓ · Last run #184 ✓ · 3 proposals · 6 sources`

The values above are visual fixture data only. Runtime implementation must populate equivalent anchors from authoritative backend state and deep-link/open the owning records.

## BLUECAD composition — hard visual requirements

- reuse the approved Process shell rather than rebuilding shared application chrome;
- warm light-first limestone/ivory visual system with chlorophyll accent;
- narrow persistent primary rail;
- only `Process | BLUECAD` as Design tabs;
- compact contextual anchor strip beneath/alongside the workspace heading;
- CAD-specific icon-first toolbar using official Phosphor generic icons;
- left model/feature navigator rather than Process equipment;
- dominant central 3D viewport;
- right inspector preserving the same Jarvis-above / Properties-below component structure as Process;
- selected CAD object drives geometry/material/dimension-compatible Properties;
- low/collapsible technical dock may expose constraints, validation, measurements, analysis and messages;
- versions, exports and studies remain subordinate concepts rather than new primary application pages;
- no broad green canvas/background wash;
- nearly square controls/surfaces with small radii;
- no overlapping labels, clipped controls, or inefficient whitespace.

## Shared-component preservation

The Process Jarvis/Properties inspector is the canonical Design-side shared inspector. BLUECAD must reuse its visual language, spacing and typography. Only CAD semantics and fields should differ. Recreating a second visually similar but structurally unrelated inspector is explicitly rejected.

## Runtime truth boundary

The approved mockup may show CAD tools, selection states, validation/results anchors, exports or analysis affordances. These are visual/product intent only unless already backed by accepted runtime authority. A later implementation must bind controls to real BLUECAD/service contracts and must not manufacture fake CAD, solver, validation, run or evidence state to match the reference.

## Future implementation acceptance

When BLUECAD visual/runtime implementation is separately authorized, the builder must reproduce this composition and compare the real screen against the approved local reference identified above. Hard acceptance includes:

- final primary rail and Design-tab information architecture;
- contextual-anchor placement;
- dominant 3D viewport;
- Process-compatible shared inspector;
- CAD-specific navigator/toolbar semantics;
- warm light-stone palette and restrained chlorophyll accent;
- near-square geometry;
- technical-dock containment;
- no reintroduction of deprecated primary destinations.

Backend-driven state differences are acceptable only when required by authoritative contracts; they do not justify changing the approved shell without a maintainer product-direction change.
