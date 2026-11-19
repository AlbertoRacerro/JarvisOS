# 330 — DATUM-NUMBER-SPACES-1: The literature datum check reads scientific notation set with thin or no-break spaces

Status: ready
Depends on: 311, 328

## Goal

A located literature datum such as a viscosity printed as `1.8 · 10\u22123 Pa s` with thin spaces around the `·` (the
way publisher HTML and many PDFs set it) is recognised by the datum check as the number 0.0018. Today the check
splits it into 1.8, 10 and 3, the datum's verdict is `not_usable`, and the `Dati` cell of a readiness case can never
turn green for such a value.

## Why

The spec-328 case walk, run privately on a real literature case from a pack, stopped at `facts`: three located data
were checked, but the apparent viscosity, printed in the paper as `1.8 · 10\u22123` with U+2009 thin spaces, failed
`LIT_VALUE_AT_LOCATOR`. Scientific notation with Unicode spaces is the common case in published text, so every case
whose facts include a small or large constant hits this. The check must keep refusing a value that is not in the
located lines; it must stop refusing one that is.

## Code facts checked at `ea2557ed`

- `backend/app/modules/memory/literature_checks.py::_NUMBER_TOKEN` is compiled with `re.VERBOSE | re.ASCII`; under
  `re.ASCII`, `\s` matches only ASCII whitespace, so the `\s*[×x·]\s*10` separator does not accept U+00A0, U+2009 or
  U+202F. Verified: `number_tokens("1.8\u2009·\u200910\u22123\xa0Pa")` returns `[1.8, 10.0, 3.0]`.
- `backend/app/modules/memory/literature_checks.py::number_tokens` feeds
  `backend/app/modules/memory/literature_checks.py::_value_check` (the `LIT_VALUE_AT_LOCATOR` required check) and
  `backend/app/modules/memory/literature_ingest.py` (proposed data from a quote), which also uses `_NUMBER_TOKEN`
  with `fullmatch` on a plain number.
- The `dsign`/`dexp` branch already accepts a U+2212 minus directly after `10`; the `cexp` branch accepts `10^-3`.

## Scope

In scope:
- Accept Unicode horizontal spaces (at least U+00A0, U+2002–U+200A, U+202F) wherever the token pattern accepts `\s`
  between mantissa, multiplication sign and `10`, keeping `re.ASCII` for `\d` and `\w` so only ASCII digits are
  numbers.
- Tests for the forms above.
- `e2e/case_walk.mjs` card-field lookup: it matches a field label with `new RegExp(`^${label}\\b`)` without the `u`
  flag, so a label ending in an accented letter (`Densità`, `Velocità`) never matches and the walk stops at `run`.
  Match the label as an escaped literal followed by a non-letter, non-digit (Unicode-aware).

Out of scope (binding non-goals):
- No change to the tolerance, the verdict rule, the required checks or the ingest proposal rules.
- No change to digits: non-ASCII digits stay non-numbers.
- No new number forms beyond the existing ones (`×`, `x`, `·`, `^`, superscripts, `e`).

## Files likely touched

- `backend/app/modules/memory/literature_checks.py`
- `backend/tests/test_literature_checks.py`
- `e2e/case_walk.mjs`

## Design constraints

- Smallest change to `_NUMBER_TOKEN`; no second tokenizer.
- A thin space must not join two separate numbers: `1.8\u2009 10` without a multiplication sign stays two tokens.

## Acceptance criteria

1. `number_tokens("1.8\u2009·\u200910\u22123\u00a0Pa s")` returns `[0.0018]` (within float tolerance) and nothing else.
2. The same with U+00A0 and U+202F in place of U+2009, and with `×`, give one token each.
3. `check_datum` with value 0.0018, a line locator on a line holding that text, returns `LIT_VALUE_AT_LOCATOR` pass
   and verdict `usable` (unit `Pa*s`).
4. The pipe card run of a case walk input that sets `Densità` and `Velocità` fills those fields (the walk passes
   `run` on such a case).
5. `number_tokens("1.8\u2009 10")` still returns two tokens; existing tests in `test_literature_checks.py` and
   the literature ingest tests pass unchanged.
6. After merge the coordinator reruns the case walk privately and records the result in the ledger.

## Required tests

- Unit tests in `backend/tests/test_literature_checks.py` for criteria 1–3 and 5.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria 1–5 met, spec status updated. Risk: low (one regular
expression of a deterministic check, covered by tests).
