# 311 — LITERATURE-FACT-TO-INPUT-1: Accept a checked datum as a named fact and use it as a parameter, from the Literature page

Status: merged
Depends on: 293, 297, 303

## Goal

On the Literature page the engineer can, for one datum of a source: give it the name of the quantity it reports,
accept it as a fact once its source check passes, and with one more action create a draft parameter that carries the
datum's number and unit and points back to it. Those are the records the readiness board's "Dati" and "Ingressi legati"
cells read, so for the first time both cells can be turned green from the interface, without typing an internal
reference.

## Why

Directives `L20261116155611-930c` point 2 and `L20261116160140-7608` point 2: the missing part for every literature case
is the path from a paper to a decision, and "numbers as located and validated facts" comes first. Spec 303 made the board
read accepted, named datums and parameters bound to them, and spec 304 links its red cells to the Literature page; but
on that page a datum can only be saved as `raw`, without a quantity name, and no route accepts it later. Binding a
parameter needs the internal string `literature_entry:<id>` typed in the Engineering data editor. A capability reachable
only by typing an internal id is not usable (directive `L20261116201429-63ee` point 6). This slice closes that gap with
the existing records and routes; it adds no table.

## Code facts checked at `81ed27af`

- `backend/app/modules/engineering/readiness_board.py::_compute_case` makes "facts" green when the case's source has a
  `literature_entries` row with `entry_kind = 'datum'`, `status = 'accepted'`, the case's `quantity`, and a
  `backend/app/modules/memory/literature_checks.py::check_datum` verdict `usable` or `usable_with_warnings`; "bound" is
  green when a `parameters` row with the input's `name` has `source_ref = 'literature_entry:<id>'` of an accepted entry
  of that source.
- `backend/app/modules/memory/literature_models.py::LiteratureEntryCreate` has no `quantity`; `status` defaults to `raw`
  and accepts `raw`, `review`, `accepted` without any check.
- `backend/app/modules/memory/literature_service.py::create_literature_entry` inserts no `quantity`; only
  `backend/app/modules/memory/literature_ingest.py` writes `quantity` (spec 297 candidates). No code path updates the
  `status` or `quantity` of an existing `literature_entries` row.
- `backend/app/modules/memory/literature_service.py::_entry_read` returns `checks`, `verdict`, `quantity`,
  `ingestion_status`, `eligible_for_later_admission` and `used_by` (parameters, assumptions, artifacts whose
  `source_ref` is the entry ref).
- `backend/app/modules/modeling/routes.py::create_parameter_endpoint` (`POST /workspaces/{id}/parameters`) accepts
  `ParameterCreate` with `name`, `value`, `unit`, `value_status` (`literature` allowed), `source_ref` and `status`
  (`draft` default); `frontend/src/api/client.ts` already wraps it.
- `frontend/src/pages/LiteratureKnowledge.tsx` renders each entry with "Select finding for Jarvis" and "Used by", and an
  "Add a finding" form that always saves `raw`; the page's visible text is English.

## Scope

In scope:
- `LiteratureEntryCreate` gains optional `quantity` (stripped, 1–128 characters, allowed only for `datum`);
  `create_literature_entry` stores it, and the `request_key` idempotency comparison includes it.
- New route `POST /workspaces/{workspace_id}/literature/sources/{source_id}/entries/{entry_id}/accept` with body
  `{ "quantity": str | null }` in `literature_routes.py`, served by a new `accept_literature_entry` in
  `literature_service.py`:
  - only `datum` entries; the quantity is the body's, else the stored one; none at all is refused (`422`);
  - refused (`409`, code `literature_entry_not_usable`) unless the on-read verdict is `usable` or
    `usable_with_warnings`; an ingested entry (`ingestion_status` not null) is refused unless
    `eligible_for_later_admission` is true;
  - sets `status = 'accepted'`, `quantity` and `updated_at`; accepting an already accepted entry with the same quantity
    returns it unchanged; with a different quantity it is refused (`409`), so an accepted fact is never renamed;
  - returns the `LiteratureEntryRead`.
- `frontend/src/api/` wrapper for the accept route; regenerate `frontend/src/api/generated` with
  `scripts/generate_frontend_contracts.py`.
- `frontend/src/pages/LiteratureKnowledge.tsx`, per datum entry:
  - "Add a finding": an optional "Quantity name" field for a datum.
  - Not accepted: a "Quantity name" input prefilled from `quantity` and an "Accept as fact" button. When the verdict is
    not usable the button is disabled and the failing check hints already shown explain why.
  - Accepted, with a number and a unit: a "Parameter name" input prefilled from `quantity` and a "Use as parameter"
    button that calls the existing parameter create route with `value` (the number as text), `unit`,
    `value_status: "literature"`, `source_ref: "literature_entry:<id>"`, `status: "draft"`; on success the source is
    re-read so "Used by" lists the new parameter, and a status line names it.
  - Errors from either call are shown in the entry with `role="alert"`; no internal id or ref in visible text.
- One browser walk `e2e/literature_fact_input_walk.mjs`.

Out of scope (binding non-goals):
- No schema change: `quantity` and `status` columns already exist.
- No change to `readiness_board.py`, its screen, the parameter lifecycle or the Engineering data editor.
- No replacement of an existing parameter (`supersedes_parameter_id`) from this page; a second parameter with the same
  name is created as a separate draft, as the existing route already allows.
- No un-accept, no edit of an accepted datum's value, unit or locator; no claim acceptance.
- No model call; accepting is a user action and the only promotion step.

## Design constraints

- The server, not the page, enforces the usable-verdict and eligibility rules; the page's disabled state is a hint only.
- Accepting writes one row in one transaction; the workspace and source of the entry must match the route.
- Visible text stays English, consistent with the rest of the Literature page.

## Acceptance criteria

1. A datum created with `quantity` returns it; `quantity` on a claim is refused (`422`).
2. Accept on a located datum whose value is found at its locator gives `accepted` with the quantity; on a datum whose
   check fails, or an ingested entry not eligible for admission, the route returns `409` and the row is unchanged.
3. Accept without a quantity in body or row is refused; re-accepting with the same quantity is idempotent; with a
   different quantity it is refused.
4. An entry id from another source or workspace gives `404`.
5. With a pack case declaring that quantity as a fact and an input, after accepting the datum and creating the
   parameter through the page's calls, `compute_readiness_board` reports "facts" and "bound" green for the case
   (backend test with the fake pack `backend/tests/fixtures/packs/zephyr` or a temporary case file).
6. The page shows the quantity field, "Accept as fact" (disabled with the check hint when not usable), and after
   acceptance "Use as parameter"; its `.mjs` test asserts these strings and is in the `build` chain.
7. Browser walk from `/` with a temporary data root: open Literature, register a text source with a file, record a
   located datum with a quantity name, accept it, use it as a parameter, see the parameter under "Used by"; then open
   Development → Prontezza and, for a seeded case naming that quantity, see "Dati" and "Ingressi legati" green; no
   console or page errors. Evidence directory attached to the PR.

## Required tests

- Backend: entry create with quantity; accept route cases of criteria 2–4; the board case of criterion 5. Existing
  literature tests stay green.
- Frontend `.mjs` static test for the new controls, added to the `build` chain in `frontend/package.json`.
- Browser walk in `e2e/` with evidence directory.

## Risk and evidence

Medium (feature code with tests, a new write route on existing records, no schema). Evidence: tests and lint, gates,
CI green on the exact head, one severe review of the diff, and the walk before the maintainer is asked to look.
