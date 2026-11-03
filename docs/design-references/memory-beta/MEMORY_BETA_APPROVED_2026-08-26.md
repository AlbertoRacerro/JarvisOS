# Memory beta — approved visual and interaction reference — 2026-08-26

Status: maintainer-approved visual/product reference; not runtime implementation authority.

This file freezes the approved `Memory` workspace composition reached during the 2026-08-26 maintainer design session. It supplements `docs/product-direction/03-project-memory-and-development-contract.md` (not carried into this repository) and `docs/product-direction/07-model-change-validation-and-reconciliation.md` (not carried into this repository) where applicable. `docs/specs/STATUS.md` remains the source of live implementation authority.

## Shared Memory shell

Primary navigation remains exactly:

`Design | Memory | Development | Coding | Settings`

`Memory` owns exactly:

`Project Basis | Models | Literature`

Visual language follows the approved warm limestone / near-white operator shell used by Process and BLUECAD. The common right-side Jarvis interaction language is reused, but semantics are Memory-specific. Memory is project/engineering knowledge only; JarvisOS software-development knowledge remains in Coding.

## Models

Approved local HTML identity:

- `memory_models_beta_mockup_v1.html`
- SHA-256: `2e76a9d740bb07adacc85379e71912b50015af5706995367ed1b1593d921f627`

Approved rendered reference identity:

- `memory_models_beta_mockup_v1.png`
- SHA-256: `9b86f095da93abaca8766485164aad8d1e09fdec90d94d9728ca957254e43b64`

The exact approved HTML is preserved beside this file as `memory-models-beta-approved-2026-08-26.html`.

Composition:

- contextual model/version navigator on the left;
- selected version dossier dominates the center;
- Jarvis remains contextual on the right;
- validation, results, artifacts, runs and lineage remain owned by the exact selected model/version rather than becoming peer application destinations;
- version state such as Process FAIL and BLUECAD PASS is explicitly version-scoped.

The later approved scalable-disclosure rule in PD-03 overrides the static first-frame expansion state of the v1 HTML: each major dossier section is a clickable disclosure header with compact summary/count/status when collapsed and bounded detail when expanded. Large sections may scroll/paginate/filter internally. The page must remain overview-first rather than grow without bound.

## Project Basis

Final approved local HTML identity:

- `memory_project_basis_beta_mockup_v3.html`
- SHA-256: `f901f977979d8389bec74a65510d1841ea12aa1e706f19dce0816a01d852b324`

The v3 change relative to the approved v2 composition is structural only: `Project search` is on the left, `Project Basis` is in the center, and Jarvis remains on the right.

Approved composition and interaction rules:

- three content columns after the global rail: `Project search | Project Basis | Jarvis`;
- Project Basis sections are disclosure controls, not indefinitely growing cards;
- engineering rows are horizontally compact so semantic chips remain close to the item they describe;
- each evaluable row exposes `Value`, `Rule/Target`, and a truthful validation state;
- `Approved`, `Proposed`, `Critical`, `Working` and similar semantic chips remain adjacent to the item title rather than floating at the far edge of the row;
- validation is per item. If an accepted specification/criterion can be checked from exact stored outputs, it is re-evaluated deterministically immediately and is not left generically stale;
- `STALE` / recalculation-required state is reserved for changes that require authoritative Process/BLUECAD/multi-domain outputs to be recomputed;
- recalculation-required items expose a contextual action such as `Validate in Process` or `Validate in BLUECAD`; a future batch `Validate` may orchestrate the entire deterministic chain;
- Jarvis may present a bounded multi-change proposal with `Approve all`;
- after `Approve all`, the proposal box disappears, the accepted values/numbers update in-place, and a working revision such as `v13.01` becomes inspectable;
- subsequent accepted edits may advance `v13.02`, `v13.03`, etc. until explicit reconciliation produces the updated current model while preserving old immutable snapshots and provenance;
- the left search pane searches Project Basis, Models, Literature, runs and artifacts but never becomes a second truth store.

The product-direction semantics for deterministic re-evaluation, working revisions, validation and reconciliation are frozen in PD-07 and take precedence over any fixture wording in the mockup.

## Literature

Final approved local HTML identity:

- `memory_literature_beta_mockup_v3.html`
- SHA-256: `c9e4225c5969ae86d9da6936b20eebfa6edbbc8a05b82a11d1a09d5a2231d150`

Approved rendered identities:

- single expanded source: `03745a012c1c7aff64a1886bb0e9635b4d09d04287da39b4aed91d294b207e6c`
- multiple expanded sources: `de0005fba9ae4dfd47f25f149adc992f27fa0211bc0d42a7705d29902257ce0c`

Approved composition and interaction rules:

- three content columns after the global rail: `Project search | Literature | Jarvis`;
- the normal Literature surface is a compact vertical list of source/file names, for example `L-011 Hydraulic limits for tubular photobioreactors.pdf`, rather than a wall of permanently expanded cards;
- the UI must not force the user to think continuously in the backend taxonomy `Source -> Document -> Claim`. That provenance distinction may remain in the data model while the normal UI uses a compact human-facing literature list and detail disclosure;
- clicking a literature row expands that same row inline instead of replacing the entire Literature view;
- after an expanded source ends, the remaining compact Literature rows continue immediately below it;
- multiple literature entries may be expanded at the same time, allowing top-to-bottom page scrolling and cross-source comparison;
- inside an expanded row, source metadata/claims/values/usages are on the left and the file preview is on the right;
- the preview height is justified to the textual detail block rather than becoming a dominant near-full-page viewer;
- meaningful previews are derived where possible: image thumbnail for images, relevant/first PDF page for PDFs, and a restrained icon/snippet fallback for non-previewable formats;
- the preview or `Open file` action opens the full file in a separate browser/viewer context (for example the browser PDF viewer) without replacing the Memory navigation state;
- source claims/values retain exact provenance and links to the Project Basis/model records that use them;
- Jarvis may research/extract/propose, but external findings do not silently become authoritative project memory.

## Authority and implementation boundary

These references freeze approved composition and interaction intent only. They do not:

- modify `docs/specs/STATUS.md`;
- release the post-100 visual-inspection hold;
- authorize frontend/backend runtime implementation;
- create a new source/proposal/model truth store;
- override later accepted ADR/spec authority.

When an approved HTML artifact and an incorrectly rendered screenshot disagree, the approved HTML/composition contract is authoritative, consistent with the product-direction visual-reference rule.
