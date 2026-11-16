# 293 — LITERATURE-DATUM-CHECKS-1: A literature number is checked against its located source text

Status: ready
Depends on: 248

## Goal

Every literature `datum` entry is returned with deterministic checks and a derived verdict: the entry has a locator, the
number (or literal value text) really appears in the located lines of its backing text, and its unit is a known unit.
A datum whose number is not at its locator reads `not_usable`; a datum that cannot be checked (no locator, unsupported
backing format) reads `unchecked`, never `usable`.

## Why

Slice D of the approved contract [248](248-design-value-path.md) (point 3: "the value is really in the source"), and the
first of the "numbers as located and validated facts" items that every literature acceptance case needs (directives
`L20261116154657-88a1` point 4, `L20261116160140-7608` point 2). Today `_validate_locator` only proves that the line range
exists; a datum typed with a wrong number at a right locator is stored and read back as if it were sourced. The check and
verdict shape follows `ResultCheck` and the verdict rule of directive `L20261116002041-cae9` ("a missing check is not a
passed check"). No new dependency: PDF extraction (slice F) is a separate later slice.

## Code facts checked at `f953b8c8`

- `backend/app/modules/memory/literature_models.py::LiteratureEntryCreate` holds `entry_kind`, `value_number`,
  `value_text`, `unit`, `locator_kind` (`page`, `line`, `section`), `locator_start`, `locator_end`.
- `backend/app/modules/memory/literature_models.py::LiteratureEntryRead` is the read model returned for entries, nested in
  `LiteratureSourceRead.entries`.
- `backend/app/modules/memory/literature_service.py::_validate_locator` accepts a locator at creation only for
  `locator_kind == "line"` on a `text/plain` or `text/markdown` backing no larger than `MAX_LOCATOR_TEXT_BYTES`, and only
  checks the line range.
- `backend/app/modules/memory/literature_service.py::_content_state` resolves the backing artifact path and availability.
- `backend/app/modules/memory/literature_service.py::_entry_read` and `_source_read` build the read models.
- `backend/app/modules/engineering/evaluator_contracts.py::ResultCheck` (`id`, `family`, `status`, `actual`, `limit`,
  `unit`, `hint`, `source`) and `Verdict` (`usable`, `usable_with_warnings`, `unchecked`, `not_usable`) are the existing
  check and verdict shapes.
- `backend/app/modules/process_kernel/units.py::parse_unit_token` validates any unit token known to Pint or the semantic
  table and raises `ProcessKernelError` otherwise.

## Scope

In scope:
- A pure function module `backend/app/modules/memory/literature_checks.py` that, given one entry row and the backing
  lines (or the reason they are unavailable), returns the checks and the verdict for a `datum`:
  - `LIT_LOCATOR_PRESENT` (family `input`, required): `pass` when a locator is set, else `not_evaluated`.
  - `LIT_VALUE_AT_LOCATOR` (family `reference`, required): `pass` when the located lines (inclusive range) contain the
    value; `fail` when the lines were read and do not; `not_evaluated` when there is no locator or the backing is not a
    readable `line`-located text/markdown file within the size bound. With `value_number`, a match is any number token
    in the located lines whose parsed float equals `value_number` within relative tolerance `1e-9` (absolute `1e-12`
    for zero). Accepted token forms: optional sign (`-`, `+`, `−` U+2212), digits with `.` or a single `,` as decimal
    separator, optional exponent `e`/`E`, and the forms `a × 10^b`, `a x 10^b`, `a·10^b`, `a × 10-b`/`10−b` (superscripts
    `⁰¹²³⁴⁵⁶⁷⁸⁹⁻` accepted). Without `value_number`, a match is `value_text` occurring literally (whitespace-normalised).
  - `LIT_UNIT_KNOWN` (family `input`, advisory): `pass` when `unit` parses with `parse_unit_token`; `warn` when it does not
    or when a `value_number` has no unit; for `value_text`-only datums without a unit, `not_evaluated`.
  - Verdict: any required `fail` → `not_usable`; else any required `not_evaluated` → `unchecked`; else any advisory
    `warn`/`fail` → `usable_with_warnings`; else `usable`.
  - Each check carries `source="policy:literature_datum_checks"` and a one-line `hint` that names what to fix; no
    `actual`/`limit` numbers.
- `LiteratureEntryRead` gains `checks: list[...]` (empty for `claim` entries) and `verdict: Verdict | None` (`None` for
  `claim`). Computed on every read from the current backing; nothing is stored, so no schema change. The backing text is
  read at most once per source read (all entries of a source share it).
- Regenerate `frontend/src/api/generated` with `scripts/generate_frontend_contracts.py`.

Out of scope (binding non-goals):
- Any schema change, stored validation state, notification, or red-dot UI (a later slice adds the Literature page view
  with its browser walk).
- PDF or page/section locator verification, Docling or any new dependency (slice F).
- Contradiction and plausibility-range checks (they need a quantity identity and ranges from the value store or pack).
- Changing what `create_literature_entry` accepts or refuses, or changing `status`; checks never edit the value.
- Exposing literature values to agent tools or requirement checks.

## Files likely touched

- `backend/app/modules/memory/literature_checks.py` (new)
- `backend/app/modules/memory/literature_models.py`
- `backend/app/modules/memory/literature_service.py`
- `backend/tests/test_literature_checks.py` (new)
- `frontend/src/api/generated/*` (generated only)
- `docs/MODULES.md` (generated only, if the module inventory changes)

## Design constraints

- Reuse `ResultCheck` and `Verdict` from `evaluator_contracts.py` when `scripts/check_architecture_enforcement.py` allows
  the `memory` module to import it; otherwise declare a read model in `literature_models.py` with exactly the same field
  names, types and literals, and say so in the PR.
- Deterministic and offline; no model call, no network, no new dependency.
- Bounded: reuse `MAX_LOCATOR_TEXT_BYTES`; an oversize or unreadable backing gives `not_evaluated`, never an exception
  in the read path.
- A backing that changed so the located range no longer exists gives `LIT_VALUE_AT_LOCATOR` `fail` with a hint saying
  the locator is outside the current text.
- Existing literature route responses keep every current field and value; only the two new fields are added.

## Acceptance criteria

1. A datum `value_number=0.0012`, `unit="1/s"`, line locator 3–3 on a markdown backing whose line 3 contains
   `1.2 × 10⁻³ s⁻¹` reads `LIT_VALUE_AT_LOCATOR` `pass` and verdict `usable`.
2. The same datum with `value_number=0.0021` reads `LIT_VALUE_AT_LOCATOR` `fail` and verdict `not_usable`.
3. A datum without locator reads `LIT_LOCATOR_PRESENT` `not_evaluated` and verdict `unchecked`.
4. A datum on a PDF backing with a locator reads `LIT_VALUE_AT_LOCATOR` `not_evaluated` and verdict `unchecked`.
5. A datum with a correct located value and an unknown unit token reads `usable_with_warnings`.
6. Decimal comma (`0,5`), `e` notation (`5e-1`) and `−0.5` (U+2212) match `0.5`/`-0.5` as stated; `15` does not match
   `1.5`, and `1.5` inside `11.5` does not match `1.5`.
7. A `claim` entry reads `checks=[]` and `verdict=None`.
8. Reading a source with N datum entries reads the backing file once (asserted with a counting stub).
9. `scripts/generate_frontend_contracts.py --check`, `scripts/check_architecture_enforcement.py`, ruff and the
   typecheck ratchet pass.

## Required tests

- `backend/tests/test_literature_checks.py`: unit tests of the pure function for criteria 1–7 and the number-token
  matcher (criterion 6), plus one route-level test through the existing literature API creating a markdown-backed source
  and entries and asserting criteria 1–3 and 8 on the response.
- The existing literature tests stay green unchanged.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, STATUS row updated.
