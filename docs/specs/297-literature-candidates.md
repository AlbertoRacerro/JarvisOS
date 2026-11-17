# 297 — LITERATURE-CANDIDATES-1: Automatic value ingestion from one stored PDF, flagged by exception

Status: ready (accepted after coordinator critical review at `28c83e99`; see "Acceptance review")
Depends on: 295, 296

## Goal

When a PDF is stored as a literature source's backing, Jarvis ingests its numeric values by itself, with no click per
value. A local model proposes quantity, value, unit, conditions and page evidence from the page text already cached by
spec 295 (through `run_ai_task`, so every call has an `ai_jobs` row). Deterministic code re-derives each number from the
original page and validates it against one narrow pattern. A value that passes is stored as `source_checked`, which makes it
`eligible_for_later_admission`. A value that fails, or whose meaning or conditions cannot be validated deterministically, is
stored too, marked `flagged` with its reasons, and listed in the Literature screen under one batch notification.

**No calculation consumes these values in this slice.** `source_checked` is evidence about the source text, not permission
to compute. Nothing reads these values into project-knowledge scalars, requirement checks, Materials or any calculation;
that is slice D and stays open. This spec covers only the literature-side part of [contract 248](248-design-value-path.md)
(points 1 and 2 partially: proposal and source checking for one stored PDF). It does not complete points 1–5 and must not
be described as doing so.

## Why

The first draft made the operator save each candidate by hand, which contradicts the contract (automatic ingestion,
approval by exception). Specs 295 and 296 check and display what an operator selects; this slice is the first one where
Jarvis finds the values itself. Revision 2 over-claimed (`calculation_usable`, "available for calculations") while wiring no
reader; this revision removes that claim and tightens what may pass.

## Code facts checked at `28c83e99`, and the conflict they raise

- `backend/app/modules/memory/pdf_text.py::cached_pdf_pages` gives cached per-page text (pypdfium2, keyed by SHA-256);
  `backend/app/modules/memory/literature_checks.py::number_tokens` and
  `backend/app/modules/memory/literature_checks.py::check_datum` re-derive a number from page text and check locator, value and unit.
  `check_datum` sees only the current backing's page text and knows nothing about which bytes an entry was ingested from.
- `backend/app/modules/process_kernel/units.py::parse_unit_token` validates any Pint unit token, including temperature,
  rates and amounts. `backend/app/modules/process_kernel/units.py::resolve_unit` is **not** usable here: it rejects every
  dimension outside the process-kernel set (`°C`, `K` and `1/h` raise `unit_dimension_unsupported`, checked at
  `28c83e99`). Dimension comparison therefore uses Pint dimensionality from
  `backend/app/modules/process_kernel/units.py::unit_registry`.
- `backend/app/modules/ai/execution.py::run_ai_task` accepts `structured_output_schema` and `workspace_id` and writes the
  `ai_jobs` row; `backend/app/modules/studio/workshop_draft.py::draft` is the in-repo pattern (schema, parse, refuse, degrade).
- `literature_entries` (`backend/app/core/literature_schema.py`) has no quantity, conditions, extractor, batch, ingestion
  status or source-digest column; its `status` is the human lifecycle `raw|review|accepted`, which must keep that meaning.
- The optional Docling structured view (`structured_pdf.py`, `structured_pdf_worker.py`, spec 296) is an on-demand extra.
  **The candidate path does not depend on it**: it reads only `cached_pdf_pages` text, so it works with Docling absent. Any
  use of the spec-296 table view is an optional extra signal that may only add flags, never remove one (see check 5).
- **Conflict.** The only value path with automatic validation, red dot and batch notification is
  `backend/app/modules/project_knowledge/sim_results.py`. Its tables are foreign-keyed to `simulation_runs` and its source
  check re-extracts from a run's `output_payload`; a literature value cannot enter it without a schema change to those
  tables or a fake run. Nothing reads literature datums as calculation input at all. Delivering "usable in calculations"
  therefore belongs with contract slice D and a migration of the simulation tables.
- **Narrowed slice chosen.** Keep automatic ingestion, source checking, flagging and the batch notification inside the
  literature owner as additive columns plus one batch table (entries stay in `literature_entries`). The strongest status
  this slice can give is `source_checked`, with the derived read field `eligible_for_later_admission`. It is a statement
  about the source text only.

## Scope

In scope:

1. **Trigger.** Attaching or uploading a PDF backing (existing routes) enqueues one ingestion batch in the background,
   after the response. Batch identity is (source id, source SHA-256, `extractor_version`): replays and re-attaches of the
   same bytes return the existing batch and never call a model twice. No online search, no other source kind, no manual
   "extract" button as the normal path (a visible retry exists only for a batch in `model_unavailable` or `failed`).
2. **Proposal (model).** The cached pages are sent in bounded windows with a structured-output schema. Each proposal has
   `quantity` (name as the paper words it), `value_text` (the literal as written), `unit`, `conditions`
   (name, literal value, unit) and `page` plus `quote` (the verbatim sentence or row). Route class is local only; `auto` never
   reaches an external provider (Never 1). With no local model the batch ends `model_unavailable`, ingests nothing, and says
   so. Model output is a proposal (Never 7); a malformed reply is a recorded batch failure, never a value.
3. **Source digest on every entry.** Each ingested entry stores `source_sha256`, the digest of the backing bytes the
   proposal was read from. On every read the entry's `source_sha256` must equal the current backing's SHA-256 exactly; a
   mismatch (backing replaced, removed or unreadable) makes the entry `stale_source`, never eligible, whatever the current
   page text says. The digest comparison is an additional, independent check: it is not inferred from `check_datum`, because
   the same number can appear on the same page of a replacement PDF.
4. **Validation (deterministic, once per value and extractor version).** Each result is a separate check with a reason code:
   - `source_occurrence`: `quote` occurs verbatim (whitespace-normalised) on the cited page, and `value_text` is a number
     `number_tokens` extracts from that quote. The stored number is the one re-derived from the page text, never the model's.
   - `unit_dimension`: `parse_unit_token(unit)` succeeds and the unit's Pint dimensionality equals that of the matched
     label's reference unit (the model states no dimension; the table decides it). Where the workspace has an active
     accepted parameter or requirement for the same quantity name, its unit's dimensionality must match too.
   - `plausible_range`: checked only when a sourced bound exists (accepted workspace parameter with `value_min`/`value_max`
     in a compatible unit); `not_applicable` otherwise, which does not flag.
   - `duplicate_contradiction`: among existing entries of the same workspace with the same quantity and canonical
     conditions: same number and unit is an exact duplicate (not stored again, the existing entry is linked); a different
     number in the same unit is a contradiction and flags. Cross-quantity inference is not attempted.
   - `meaning_and_conditions`: see the pattern below. This is the only check that decides meaning, and it is deliberately
     narrow.
5. **The minimal accepted pattern (`meaning_and_conditions`).** Co-occurrence of a label and a number in a sentence does
   not establish meaning, so co-occurrence never passes. A value passes only if the quote matches exactly one bounded
   shape, evaluated on the cited page text and not on the model's restatement:
   - **Label–value–unit:** a quantity label from a small fixed table (name and aliases, each tied to one physical
     dimension; the table lives in the module, is tested, and starts with a handful of entries, not an open vocabulary),
     then at most a fixed copula or separator (`is`, `=`, `:`, `of`, `was`, `were`), then one number, then one unit token,
     with only whitespace between them. The label must be in the table, the unit's dimensionality must equal that of the
     label's reference unit, and the number must be the single numeric token directly after the separator.
   - **Condition clause:** required only when the matched label's table row says `requires_conditions` (for example
     solubility, density or viscosity, which depend on temperature; not a pipe diameter or a molar mass). When required,
     at least one condition, located by explicit syntax after the value: `at <number> <unit>`,
     `under <number> <unit>`, `@ <number> <unit>`, or a parenthesised `(<name> = <number> <unit>)`, where the condition
     name is also in a small fixed table with a dimension and the unit's dimension matches. Each stated condition's
     literal and unit must occur in the clause in that position, not merely elsewhere in the sentence.
   Everything else is flagged, never passed: free-form sentences, a label not in the table, a unit whose dimension does not
   match the label, more than one candidate number after the separator, no condition clause where the label requires one
   (`conditions_missing`), a
   condition outside the located syntax (`condition_not_in_source`), a quote line carrying three or more numbers or lying
   in a table cell or row (`table_meaning_unverified`: a column or row header decides the meaning and is not re-derived
   here), and ranges, ratios, lists and negations. The grammar is not extended inside this spec; widening it is a later
   spec with its own tests. The optional spec-296 table view may add the table flag but can never clear any flag.
6. **Storage (additive migration, high risk; boundaries pending review).** New nullable columns on `literature_entries`:
   `quantity`, `conditions_json`, `ingestion_status` (`source_checked|flagged`, NULL for hand-typed entries),
   `ingestion_reasons_json`, `batch_id`, `source_sha256`, `extractor_id`, `extractor_version`, `ai_job_id`; one table
   `literature_ingestion_batches` (source, SHA-256, extractor version, state
   `running|done|failed|model_unavailable|unavailable`, counts, `notification_state` `none|pending|read`, `ai_job_ids`).
   Entries are written `status='raw'` with the page locator and the quote as `context_text`; `status` is never
   auto-promoted. New `CURRENT_SCHEMA_MIGRATION_ID`, digest and baseline per `docs/MIGRATIONS.md`.
7. **Read-time eligibility.** On each entry read, `eligible_for_later_admission` is true only when `ingestion_status` is
   `source_checked`, `source_sha256` equals the current backing SHA-256, and the existing `check_datum` verdict is not
   `not_usable`. Flagged, stale, hand-typed and unchecked entries are false, with reasons returned. One function owns this.
   The field name states its limit: it grants no calculation use, and no code path in this slice reads it to compute.
8. **One notification per batch.** A batch with flagged values gets `notification_state='pending'`; the Literature screen
   shows one banner for the source ("N of M values need attention") linking to each flagged value, and one batch-level
   acknowledge sets `read`. The simulation path has no Slack line in code (verified), so none is added here.
9. **Literature UI.** In the existing source panel: the batch state (running, done, `model_unavailable`, failed, unavailable
   with reason such as no text layer or too many pages), counts of `source_checked` and flagged, the flagged list with page,
   quote, quantity, number, unit, conditions and each failed check with its reason, a `stale_source` marker where the digest
   no longer matches, and the label "source-checked only, not used in calculations". No per-value save, confirm or approve
   control exists.

10. **The fixed tables.** Each quantity row is `(name, aliases, reference unit, requires_conditions)`; each condition row is
   `(name, aliases, reference unit)`. They live in core and hold **generic physical quantities only** (for example
   temperature, pressure, density, dynamic viscosity, solubility, diffusivity, molar mass, diameter, length, specific
   growth rate). No project, organism, apparatus or customer term enters the core table; a pack-supplied extension is a
   later spec (PARK), not part of this slice.

Out of scope (binding non-goals):

- Any calculation reading these values; wiring into project-knowledge scalars, requirement checks, Materials,
  revalidation or freshness (slice D), the red dot on those surfaces, history (slice B), and Slack. Contract 248 points 1–5
  are not claimed complete.
- Resolving a flagged value (correct or confirm). A flagged value stays flagged; one-action resolution is the next slice.
  Hand-typed datum behaviour is unchanged.
- A general natural-language grammar for meaning, table-structure inference, OCR, scans, non-PDF backings, multi-source
  batches, online search, PaperQA2, unit conversion, changes to the Project Basis, and any new dependency.
- Cross-source or Project Basis contradiction checks beyond same quantity and conditions within the workspace's literature.

## Files likely touched

Verify against actual code before starting; report conflicts instead of guessing.

- `backend/app/modules/memory/literature_ingest.py` (new: window, propose, validate, store; fixed label/condition tables)
- `backend/app/modules/memory/literature_service.py`, `literature_routes.py`, `literature_models.py`, `module.toml`
- `backend/app/core/literature_schema.py`, `backend/app/core/schema.py`, `backend/tests/test_migration_discipline.py`
- `backend/tests/test_literature_ingest.py` (new)
- `frontend/src/api/literature.ts`, `frontend/src/api/generated` (generated only), `frontend/src/pages/LiteratureKnowledge.tsx`
- `frontend/tests/114-literature-knowledge.mjs`, `e2e/literature_datum_walk.mjs`, `docs/MODULES.md` (generated only)

## Design constraints

- Offline tests with a fake provider returning scripted proposals; no paid provider; every model call is `run_ai_task` with
  an `ai_jobs` row, workspace-scoped. Page text sent to the model is bounded and carries no data root path or secret.
- The source path and SHA-256 come from the registered artifact after the workspace check, never from the client; symlinked
  backings are refused as in spec 296. Quotes and reasons are bounded and escaped in API and UI.
- Bounds: at most 40 pages, 12 model calls and 200 proposals per batch; a hit sets `truncated` and says so.
- The candidate path uses only `cached_pdf_pages`; it must pass its tests with Docling not installed.
- Ingestion never edits the source or any Project Basis parameter (contract point 10), and never changes the verdict meaning.
- Frozen conformance tests and existing check semantics do not change. Existing hand-typed entries migrate unchanged.

## Acceptance criteria

1. Attaching a generated two-page PDF whose page 2 contains "Solubility of CO2 = 0.8 g/L at 25 °C" (inside the accepted
   pattern) and, in a three-number table row, a further value, with a scripted fake model proposing both: one batch is
   created without any value-level action; the first value is `source_checked` with the number re-derived from the page and
   `eligible_for_later_admission` true; the table row value is `flagged` with `table_meaning_unverified` and eligibility
   false; one notification is pending.
2. A proposal whose quote is not on the cited page, whose number differs from the page, or whose unit does not parse or
   mismatches the label's dimension, is stored flagged with the exact check and reason, never `source_checked` and never
   silently dropped.
3. Ambiguity stays flagged: a label and condition tokens co-occurring in free-form prose, a label absent from the fixed
   table, a unit dimension that does not match the label, two candidate numbers after the separator, a range or ratio, a
   condition outside the located syntax, and no condition for a label whose row requires one each give their distinct
   reason and never pass. A table label that does not require conditions (for example a diameter) passes without one.
4. An exact duplicate of an existing same-quantity same-condition value is not stored again and links the existing entry; a
   different number for the same quantity, conditions and unit flags the new value as a contradiction.
5. A plausible-range bound from an accepted parameter flags an out-of-range value; without a bound the check is
   `not_applicable` and does not flag.
6. Re-attaching the same bytes creates no second batch and no second `ai_jobs` row. Replacing the backing with a different
   PDF whose page text contains the same label, number and unit on the same page yields a new batch, and the old entries
   report `stale_source` and `eligible_for_later_admission` false because their `source_sha256` no longer equals the
   backing digest, even though `check_datum` alone would still pass on that page text.
7. No local model, malformed model output, a PDF without text layer, an unreadable PDF, a non-PDF backing and a
   cross-workspace source each give a distinct batch state; none stores a value; unavailable states state the reason.
8. Every model call has an `ai_jobs` row; `route_class="auto"` never reaches an external provider; no network in tests.
   The ingestion tests pass with Docling absent.
9. No code reads `eligible_for_later_admission` or `ingestion_status` as calculation input, and no API field or UI text
   says "usable", "validated" or "available for calculations" for these values; a test asserts the strings and the absence
   of a reader.
10. The migration passes the discipline test, and old databases open unchanged.
11. From `/` with a clean profile and visible controls only, the browser walk uploads the PDF, sees the batch complete with
    source-checked and flagged counts and the single banner, opens a flagged value and sees the reason and "not used in
    calculations", and finds no per-value save or confirm control. Its evidence directory is attached before the
    maintainer is asked to look.
12. Lint, focused backend and frontend tests, build, contract, module, architecture and typecheck gates pass; exact-head CI green.

## Required tests

- Backend, offline: each check pass and fail path with scripted proposals; every accepted-pattern pass and each listed
  ambiguity flag; table-row and condition flags; duplicate and contradiction; bound present and absent; idempotence;
  replacement with identical page text (digest mismatch); each unavailable state; eligibility derivation and revocation;
  absence of any calculation reader; workspace isolation; `ai_jobs` rows; migration digest; Docling-absent run.
- Frontend `.mjs` source tests for batch states, counts, banner, flagged reasons, the "not used in calculations" wording and
  the absence of per-value controls; add to the `build` chain only if a new file is created. Extend
  `e2e/literature_datum_walk.mjs` for criterion 11.

## Definition of done

Attaching a PDF ingests it automatically; values passing the narrow pattern are `source_checked` and bound to the exact
source digest, failures are retained flagged with reasons under one batch notification, and `eligible_for_later_admission`
is the single read-time definition. No calculation consumes any of it. The STATUS row is reconciled with the merged PR.
**Contract 248 stays incomplete**: slice D wiring into project-knowledge, requirement checks and Materials, the red dot
there, one-action resolution, history, table-structure meaning, other source kinds and slice H remain open.

## Acceptance review (coordinator, 2026-11-17, at `28c83e99`)

The three open points of the planning draft are settled as follows; all are reversible technical choices.

- **Batch store.** The narrow literature-side batch table stays. Migrating `simulation_result_*` to accept literature
  sources has a larger blast radius, and slice D, which would justify one shared store, has no reader yet. Revisit when
  slice D is specified.
- **Fixed tables.** In core, generic physical quantities only (scope item 10); a pack extension is PARK.
- **`conditions_missing`.** Flagging every value without a condition would bury plain constants in flags and defeat
  "approval by exception". The flag now applies only where the label's row says `requires_conditions`.
- **Correction found in review.** `resolve_unit` cannot check temperature or rate units, so acceptance criterion 1 could
  never pass; dimension checks use Pint dimensionality instead, and the model no longer proposes a dimension.
- **Risk.** High (migration). It needs a second-session full-diff review and the migration proven on a copy of a real
  data root before merge.
