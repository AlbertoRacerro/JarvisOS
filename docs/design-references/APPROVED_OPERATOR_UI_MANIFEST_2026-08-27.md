# Approved operator UI manifest — 2026-08-27

Status: maintainer-approved normative visual manifest.

This file is the single index of the HTML references approved during the final JarvisOS operator-workstation visual inspection. These HTML files are not moodboards and are not optional examples. Subject to the explicit cross-surface overlays below, they are the normative visual/composition target for future frontend implementation at their reference viewport.

The mandatory companion for user-visible interaction/state-transition semantics is `FINAL_OPERATOR_INTERACTION_CONTRACT_2026-08-27.md`. A future frontend must conform to both the byte-identified HTML composition and the preserved interaction contract; matching one while redesigning the other is not conformance.

## Normative rule

For each listed surface, production frontend implementation MUST reproduce the approved reference's visible hierarchy, proportions, spacing, typography, colors, component treatment, control placement, information density and interaction structure at the reference viewport. Fixture/demo values in the HTML are illustrative data only and MUST be replaced by truthful runtime/backend data. A missing backend capability does not authorize an implementer to delete, rename, simplify or reinterpret an approved visible capability: the missing capability must instead remain visibly truthful/unavailable until its owning backend specification is implemented.

Responsive adaptation is allowed only to make the same semantic composition usable at other viewport sizes; it does not authorize a different information architecture or primary navigation.

An intentional visual/product deviation from these references requires an explicit maintainer decision, an updated canonical HTML/reference, and a new manifest hash entry. An implementation PR may not silently establish a new design precedent.

## Final cross-surface overlays

The following later maintainer decisions apply to every individual HTML, including older references whose embedded shell predates the decision:

1. Primary rail navigation is exactly `Design | Memory | Development | Coding | Settings`. There is no normal `Home` destination. If an older HTML omits `Coding`, the final five-item rail in this paragraph supersedes that stale shell fragment while the screen-local composition remains normative.
2. Design peer modes are exactly `Process | BLUECAD`.
3. Memory peer sections are exactly `Project Basis | Models | Literature`.
4. Development peer sections are exactly `Roadmap | Brainstorm`; Roadmap peer views are exactly `Timeline | Calendar`; there is no standalone Board page. `Execution status` is embedded/collapsible under Timeline.
5. Coding peer sections are exactly `Repository | Runtime`.
6. Settings tabs are exactly `Appearance | AI | System`.
7. Standard application type is Inter / Inter Display regular or medium rather than heavy-bold. IBM Plex Mono is reserved for code/log/path/hash-style content. The narrow/tall/light condensed type treatment is reserved for the colored Roadmap workstream bars.
8. Generic icons use Phosphor. A production build may bundle the same icon assets instead of loading the CDN used by a prototype, but the glyph choice and rendered visual role must remain equivalent.
9. Shared shell/component normalization that was explicitly approved later may be applied across older references only where it does not alter the screen-local composition or semantics.

## Canonical HTML set

| Surface | Canonical repository path | Reference viewport | SHA-256 | Git blob |
| --- | --- | ---: | --- | --- |
| Design / Process | `docs/design-references/process-beta/process-beta-approved-2026-08-26.html` | 1365×768 | `d3bb06d9a7c761699a21b9b6b0a1901214a799f06dd31570c2fbfedc018cc475` | `175140bf429f4a47b23364f9d1dd8a6381cd039b` |
| Design / BLUECAD | `docs/design-references/bluecad-beta/bluecad-beta-approved-2026-08-26.html` | 1365×768 | `da4ddfb1ebf6e0c7d39c133bf9b7c0da82e14428bad87748b0f7aa37a7e17bd9` | `001b9c9511d565905a0b1549008180c74e52ff14` |
| Memory / Project Basis | `docs/design-references/memory-beta/memory-project-basis-beta-approved-2026-08-26.html` | 1600×1000 | `f901f977979d8389bec74a65510d1841ea12aa1e706f19dce0816a01d852b324` | `fb7b63e89d0cefee1b04ab3785590f573738fdb2` |
| Memory / Models | `docs/design-references/memory-beta/memory-models-beta-approved-2026-08-26.html` | 1600×1000 | `2e76a9d740bb07adacc85379e71912b50015af5706995367ed1b1593d921f627` | `a52f1e331a1c981388e7cbd8293bfef147c2a314` |
| Memory / Literature | `docs/design-references/memory-beta/memory-literature-beta-approved-2026-08-26.html` | 1600×1000 | `c9e4225c5969ae86d9da6936b20eebfa6edbbc8a05b82a11d1a09d5a2231d150` | `43b61e679f11478eaf8a5c8b80e724148db170dc` |
| Development / Roadmap Timeline | `docs/design-references/development-beta/development-roadmap-timeline-beta-approved-2026-08-27.html` | 1600×1000 | `eb71ddab8cd829d041319fca7a6b08d4e7f60ae57a51f96310ed9ea11d20dc70` | `19012b2754a99eb32312c257ba50dcea4c01c6a9` |
| Development / Calendar | `docs/design-references/development-beta/development-calendar-beta-approved-2026-08-27.html` | 1600×1000 | `3dc94b861478007080a3d9658ec81bc3c46394d0bf73f9a5aa4aa669024b2737` | `16fc42860aee545ac25f1b43ceaee49b4738d9ef` |
| Development / Brainstorm | `docs/design-references/development-beta/development-brainstorm-beta-approved-2026-08-27.html` | 1600×1000 | `2b30f8d558045becf3c79b7d9a7bfcfd186a42a6278d92f53a0150be61f82631` | `3aa529fa459919b4001091f4eb5c7bbcc59359a0` |
| Coding / Repository | `docs/design-references/coding-beta/coding-repository-beta-approved-2026-08-27.html` | 1600×1000 | `afe3bf43eebc3da65e38aadcb27dcaac6f55b61959077cad82cbc51979b1d11f` | `e333d1c54e090ff178a6c0f823f93dbfb92668c2` |
| Coding / Runtime | `docs/design-references/coding-beta/coding-runtime-beta-approved-2026-08-27.html` | 1600×1000 | `041b2f8974a1ad866ac5fad700c920c3a4816e6a0d6263a185e20c0ca421893e` | `ad7f7fa813dbde700682331b89aa1d3f7958e83b` |
| Settings | `docs/design-references/settings-beta/settings-beta-approved-2026-08-26.html` | 1365×768 | `f30a0937f9e8cb1a189ade226a004ac4206597d1130433748b87d4c61043e5de` | `195fd06309ee0755588fb53f486431cdae8540c9` |

All eleven entries above were preserved as repository files. The materialization step that reconstructed previously missing references verified their SHA-256 before writing them and removed its temporary payload/workflow afterward.

## Visual and interaction precedence

When two sources appear to disagree, use this order for future product/frontend behavior:

1. the final cross-surface overlays in this manifest;
2. the canonical HTML for the selected surface for exact visual/composition target;
3. `FINAL_OPERATOR_INTERACTION_CONTRACT_2026-08-27.md` for exact preserved user-visible action classes, state transitions and interaction semantics;
4. the most-specific approved reference document under `docs/design-references/`;
5. `docs/product-direction/08-final-visual-product-contract.md` (not carried into this repository) for final product semantics and ownership placement;
6. earlier product-direction documents only where not superseded above.

This precedence does not replace implementation authority. `docs/specs/STATUS.md`, the active canonical spec/readiness record and exact current code remain authoritative for what work is currently allowed and what functionality exists today.

## Frontend proof requirement

A frontend implementation PR that materially changes one of these surfaces must include deterministic browser evidence at the reference viewport and compare the result against the corresponding canonical HTML. The proof must cover at minimum:

- primary/secondary navigation;
- major panel geometry and ordering;
- typography roles and density;
- key component styling;
- the visible interaction states and action classes required by the owning spec and final interaction contract;
- absence of fabricated backend/runtime state.

Pixel-for-pixel identity is not required for unavoidable browser/font rasterization differences, but composition-changing drift is a defect unless the maintainer has explicitly approved a new reference.

## Functional preservation rule

The HTML references define the intended operator surface; they do not grant backend authority. Every visible action and state must be backed by a truthful backend/domain/read-model contract before it is presented as functional. `FINAL_OPERATOR_INTERACTION_CONTRACT_2026-08-27.md` preserves exact operator behavior, `docs/spec-drafts/FINAL_OPERATOR_CAPABILITY_MATRIX_2026-08-27.md` (not carried into this repository) maps the approved surfaces to the required capability families, and `docs/spec-drafts/FINAL_VISUAL_IMPLEMENTATION_PACK_2026-08-27.md` (not carried into this repository) records the pseudo-spec decomposition pending 100c re-derivation.

No implementer may treat absence of a current backend route/table/service as evidence that the approved frontend capability or interaction should disappear.
