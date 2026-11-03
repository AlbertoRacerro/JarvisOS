# Visual Identity 100 — maintainer design authority pack

Status: **binding maintainer design input for the definition/readiness of spec 100; not implementation authority by itself**  
Owner: repository maintainer  
Prepared: 2026-08-25  
Target queue slice: `100 VISUAL-IDENTITY-1`

`docs/specs/STATUS.md` remains the sole live authority for queue order, status and implementation authorization. This directory freezes the maintainer's aesthetic/product decisions so the 100 lifecycle does not have to infer visual direction from the provisional UI.

## Core thesis

JarvisOS is a **living engineering instrument**: a serious human-made technical system presented as a continuation of natural evolution rather than as cyberpunk machinery or generic SaaS. The functional grammar stays engineering-first; the visual grammar is humanistic, natural, material and quietly advanced.

Atmospheric shorthand:

**Pandora × Olympus × mature solarpunk × modern engineering workstation**

This means:

- ANSYS/Fusion/Siemens discipline for information architecture, density and legibility;
- nature/biology for chromatic and material cues;
- classical monumentality for proportion, permanence, mineral light and quiet gravity;
- modern product craft for interaction polish;
- no literal leaves, vines, Greek columns, sci-fi HUDs, neon or decorative futurism.

## Maintainer decisions already closed

1. **Light is canonical/default.** Dark remains a complete optional appearance, not the identity-defining mode.
2. **Density is medium-high engineering density.** More compact than modern SaaS, less cramped than legacy HYSYS/Aspen-style panels; information must remain scan-readable.
3. **Content is rational/engineering; UI expression is humanistic/natural.**
4. **Geometry language is bio-machined:** structural engineering surfaces stay precise and comparatively architectural; floating/transient/Jarvis surfaces may be softer. Avoid universal pill/large-radius SaaS styling.
5. **Depth is subtle but real.** Structural panels may cast small shadows to separate them from the mineral canvas. Glass/translucency is restricted to floating or transient UI and must have an opaque fallback.
6. **Motion should be felt, not watched.** Ordinary interaction motion is fast and near-subliminal. Longer animation is allowed only while the user is already waiting for real AI/solver/loading work; never fake progress.
7. **Accent is living chlorophyll/microalgae color, not neon and not petroleum teal.** Accent is carried mainly by borders, focus, selection, active detail, restrained surfaces, Jarvis presence and selected chart emphasis — not by green paragraphs or large filled green regions.
8. **Accent customization is allowed but bounded.** Three curated presets plus Custom; the seed generates a safe tonal scale. User accent never replaces semantic status colors or scientific visualization scales.
9. **Icon family priority is consistency first, then preference:** Phosphor > Lucide > Tabler. The current generic UI coverage audit passes for Phosphor, so 100 should use Phosphor only for generic application icons.
10. **Jarvis is an integrated expert colleague/butler presence, not a separate purple-gradient AI feature.**
11. **Visual identity may improve hierarchy and affordance but must not change product authority, route semantics, engineering record semantics, process topology semantics or evaluator behavior.**

## Documents

- [`VISUAL_DIRECTION.md`](./VISUAL_DIRECTION.md) — aesthetic intent, reference matrix, desired/forbidden character.
- [`DESIGN_SYSTEM_DECISIONS.md`](./DESIGN_SYSTEM_DECISIONS.md) — typography, color/theming, density, geometry, surfaces, depth, motion and Jarvis treatment.
- [`ICONOGRAPHY.md`](./ICONOGRAPHY.md) — current icon inventory, Phosphor coverage decision, use rules and future engineering-symbol boundary.
- [`PROOF_AND_ACCEPTANCE.md`](./PROOF_AND_ACCEPTANCE.md) — canonical screens and visual/readability gates the 100 spec must make objectively reviewable.
- ../../audits/VISUAL_IDENTITY_REFERENCE_AUDIT_2026-08-25.md (not carried into this repository) — external reference and license/provenance notes.

## How spec 100 must consume this pack

The definition/full spec for 100 should cite this directory as maintainer design authority and translate it into the smallest sufficient implementation. It may tune exact token values when browser proof demonstrates a readability/accessibility problem, but it must not silently substitute a different aesthetic direction.

Aesthetic decisions that are explicitly proof-gated in these documents may be resolved by comparing the required canonical screens. Everything else should proceed without another maintainer style interview.

## Explicit non-goals

This pack does **not** authorize:

- process unit-operation/node/stream semantics;
- a fake Aspen/HYSYS process model in React;
- new backend authority or persistence;
- re-layout that changes workstation/product semantics;
- new AI execution paths;
- decorative nature imagery inside dense engineering surfaces;
- copying third-party brand assets, screenshots or proprietary fonts.
