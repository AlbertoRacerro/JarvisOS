# 309 — NUMERIC-SOURCE-CHIPS-1: Source and check-state chip on every numeric input

Status: merged
Depends on: 293, 297, 298

## Goal

Every numeric input in the process editor, PBR inspector and reactor kinetics (`QuantityInput`) displays a compact,
read-only Italian chip naming its source kind (`Digitato`, `Letteratura`, `Estratto`, `Risultato`) and its check state
(`Verificato`, `Da fonte`, `Da controllare`, `Fonte modificata`, `Non verificato`). Process draft quantities preserve
optional source metadata without breaking existing drafts or callers.

## Why

Third item of the front-end plan in spec 298 (maintainer directives `L20261117071737-1216` and `L20261116201429-63ee`
point 3): Literature and Biology already show check states on datums and library values, while process numeric inputs
(`QuantityInput`) show only `stored <value>` or `not set` with no source or check state. Putting one shared chip on
`QuantityInput` makes provenance and check state visible wherever numbers are entered or reviewed.

## Code facts checked at `d6d3a653`

- `backend/app/modules/process_stack/draft_models.py::DraftQuantity` validates `{value, unit}` with no source metadata.
- `backend/app/modules/process_stack/draft.py::_si` converts a `DraftQuantity` to `{value, unit, si}`.
- `backend/app/modules/memory/literature_checks.py::check_datum` computes deterministic datum checks and verdict.
- `frontend/src/components/process/QuantityInput.tsx` renders numeric inputs for `ProcessDraftEditor.tsx`,
  `PbrInspector.tsx` and `ReactorKinetics.tsx`.
- `frontend/src/components/process/pbrLogic.ts` already defines `verificationChip` for biology model cards.
- `frontend/src/stages/ProcessDraftEditor.tsx` builds submitted quantities as `{value, unit}` from form text, and
  `backend/app/modules/workspace_actions/service.py` rebuilds untouched culture quantities as
  `DraftQuantity(value=..., unit=...)`: both would silently drop a stored `source` when a sibling field is edited.

## Scope

In scope:
- Backend (`backend/app/modules/process_stack/draft_models.py`, `backend/app/modules/process_stack/draft.py`):
  - Add optional `QuantitySource` on `DraftQuantity`: `kind` in `{"typed", "literature", "ingested", "result"}`,
    optional `check_state` in `{"unchecked", "source_checked", "verified", "flagged", "stale"}`, optional `ref_id`
    (max 128 chars) and optional `label` (max 160 chars).
  - `_si` preserves `source` (dumped with `exclude_none=True`) when present on `DraftQuantity`; omitting `source` keeps
    existing `{value, unit, si}` output unchanged.
- Frontend (`frontend/src/api/processDraft.ts`, `frontend/src/components/process/SourceChip.tsx` (new),
  `frontend/src/components/process/QuantityInput.tsx`, `frontend/src/stages/ProcessDraftEditor.css`):
  - Add `QuantitySource` to `DraftQuantity` / `StoredQuantity`.
  - Add `SourceChip` mapping `(kind, check_state)` to Italian labels (`Digitato`, `Letteratura`, `Estratto`,
    `Risultato`; `Non verificato`, `Da fonte`, `Verificato`, `Da controllare`, `Fonte modificata`), with `data-source-kind`
    and `data-check-state` attributes and an optional plain title from `label`.
  - Render `SourceChip` inside `QuantityInput` whenever a stored value exists or the user has typed a value (defaulting
    an unsourced stored/typed value to `Digitato · Non verificato`).
  - A value the user changes is submitted without `source` (it becomes typed); an entry whose text and unit still equal
    the stored quantity is submitted with the stored `source`, so editing one field does not strip its siblings.
- Backend (`backend/app/modules/workspace_actions/service.py`): untouched culture quantities copied from the stored
  draft keep their `source`.

Out of scope (binding non-goals):
- No SQLite schema migration.
- No automatic ingestion or admission of literature candidates into process parameters (owned by later 248 slices).
- No changes to `LiteratureKnowledge.tsx` or `BiologyModelLibrary.tsx` existing badges.
- `source` is a displayed claim, not evidence: no calculation, gate, verdict or readiness cell reads `check_state`, and
  no agent tool or workspace action gains a way to set it. Until later slices write sources, most inputs show
  `Digitato · Non verificato`, which is the honest state.

## Acceptance criteria

1. `DraftQuantity` accepts optional `source` metadata and `_si` preserves it on stored stream/unit quantities; existing
   drafts without `source` continue to validate and convert identically; editing one quantity (in the editor or by a
   workspace action) keeps the stored `source` of the untouched quantities of the same object.
2. `QuantityInput` renders `SourceChip` with the Italian source kind and check state for `typed`, `literature`,
   `ingested` and `result`, and defaults unsourced/edited inputs to `Digitato · Non verificato`.
3. `frontend/tests/` has a `.mjs` test added to `frontend/package.json` `build` covering all four source kinds and five
   check states.
4. A browser walk in `e2e/` from `/` opens the Process editor, inspects a numeric input and verifies the source and
   check-state chip is visible with no console/page errors.

## Required tests

- Backend pytest: `DraftQuantity` validation (valid source kinds/states, invalid kind rejected, omitted source unchanged)
  and `_si` round-trip preservation.
- Frontend `.mjs` test for `SourceChip` and `QuantityInput`.
- Browser walk in `e2e/` attached as evidence.
