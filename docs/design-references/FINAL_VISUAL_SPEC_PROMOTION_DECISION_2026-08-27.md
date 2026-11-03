# Maintainer decision — final visual direction implementation promotion — 2026-08-27

Status: explicit maintainer planning decision; no runtime implementation authority by itself.

## Decision

The maintainer considers the visual/product design inspection complete and wants the approved operator-product direction preserved so it can be implemented through normal specifications rather than remaining only in mockups/chat context.

The repository therefore records:

- PD-08 as the final product-direction reconciliation after the visual pass;
- `docs/design-references/APPROVED_OPERATOR_UI_MANIFEST_2026-08-27.md` as the canonical index and precedence record for the eleven byte-identified approved HTML surfaces;
- `docs/design-references/FRONTEND_CONFORMANCE_CONTRACT_2026-08-27.md` as the frontend visual-conformance rule;
- `docs/design-references/FINAL_OPERATOR_INTERACTION_CONTRACT_2026-08-27.md` as the binding preservation record for approved user-visible states, action classes, state transitions, explicit-context behavior and superseded interaction interpretations;
- `docs/spec-drafts/FINAL_OPERATOR_CAPABILITY_MATRIX_2026-08-27.md` (not carried into this repository) as the row-level map from approved operator behavior to candidate backend/frontend ownership and no-fake requirements;
- `docs/spec-drafts/FINAL_VISUAL_IMPLEMENTATION_PACK_2026-08-27.md` (not carried into this repository) as the complete planning draft set for backend/domain/frontend work discovered during that pass;
- `docs/specs/100c-final-product-direction-authority-0.md` as the definition-only queue-rederivation kernel that must reconcile those drafts against exact post-100a/100b master and every semantically overlapping live/planned registry row before runtime implementation;
- `docs/design-references/coding-beta/CODING_RUNTIME_TERMINAL_FUTURE_2026-08-27.md` as the future approved integrated-terminal direction.

The preservation packet is intentionally layered rather than interpretive: canonical HTML owns visual composition; the interaction contract owns approved user-visible behavior/state transitions; the capability matrix owns capability preservation and candidate ownership; the pseudo-spec pack owns draft decomposition; 100c owns later exact-master authority re-derivation. None of these planning/reference layers independently grants runtime implementation authority.

## Queue insertion intent

`100c FINAL-PRODUCT-DIRECTION-AUTHORITY-0` is the maintainer's intended future insertion **after 100b and before 101**.

This document records that intent but is not live queue authority. `docs/specs/STATUS.md` remains the sole authority for whether 100c actually occupies that position and when it may start.

The intended sequence, once separately reconciled into `STATUS.md`, is:

1. complete 100a through its normal lifecycle;
2. complete 100b through its normal lifecycle/disposition;
3. perform a separately authorized docs-only registry reconciliation that inserts/activates 100c if the maintainer still chooses this direction after exact post-100b inspection;
4. execute 100c as documentation/authority re-derivation only after `STATUS.md` authorizes it;
5. only then proceed through the queue produced by that re-derivation.

100c must not mechanically promote every pseudo-spec. It must classify overlap, retain one canonical owner, eliminate unnecessary slices, and decide which existing/planned rows remain valid, require re-derivation, are merged/reordered, or are superseded/deferred.

## Builder behavior

External builders must continue to follow live `STATUS.md`. This planning decision alone does **not** authorize them to insert 100c, divert from the current live front, or implement any FV draft. If 100b finishes while 100c is absent from `STATUS.md`, a separate maintainer/coordinator-authorized registry reconciliation is required before 100c can become the next queue item.

When a later authorized builder implements one of the frozen operator surfaces, it must read the relevant canonical HTML together with the manifest, frontend conformance contract, final interaction contract, most-specific approved surface reference and the canonical spec/readiness produced by 100c. It may not use chat memory, screenshots, current implementation convenience or missing backend support as permission to reinterpret or silently remove approved behavior.

The draft pack is planning evidence, not a parallel queue.
