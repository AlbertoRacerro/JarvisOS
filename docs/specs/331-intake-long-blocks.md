# 331 — INTAKE-LONG-BLOCKS-1: Folder intake splits a long block on line breaks instead of skipping the file

Status: merged
Depends on: 316, 328

## Goal

A text file in a registered document folder whose paragraphs are separated by single line breaks (one paragraph per
line, no blank lines, the usual shape of text extracted from a paper) is included in the memory index as passages of
at most `MAX_PASSAGE_CHARS`. Today the whole file is one blank-line block; when that block is longer than 8000
characters every passage is dropped and the file is reported as `passage_too_large` ("passaggio troppo lungo").

## Why

The spec-328 case walk, run privately on a real literature case, reached six green cells of seven and stopped at
`answer`: the paper text attached to the case (one paragraph per line, about 300 lines) was skipped by folder intake,
so the retrieval check had nothing to find. Blank-line splitting stays the first rule; line breaks are only a
fallback for a block that would otherwise be lost.

## Code facts checked at `ea2557ed`

- `backend/app/modules/ai/document_intake.py::_passages` splits each heading section into blocks at blank lines and
  `continue`s past any block longer than `MAX_PASSAGE_CHARS` (8000), then packs the remaining blocks into passages
  up to that size; each passage records its first line number.
- `backend/app/modules/ai/document_intake.py::_file_documents` adds a `passage_too_large` skip when any blank-line
  block is longer than `MAX_PASSAGE_CHARS`, and another when no passage results from non-empty content.
- `backend/app/modules/ai/document_intake.py::extract_passages` is also used by
  `backend/app/modules/ai/retrieval_bench.py` to build retrieval cases.
- `backend/tests/test_document_intake.py::test_bounds_symlinks_and_invalid_files` expects a single
  8001-character line (`long.txt`) to be skipped as `passage_too_large`.

## Scope

In scope:
- In `_passages`, a blank-line block longer than `MAX_PASSAGE_CHARS` is split into its lines, and those lines are
  packed into passages exactly as blocks are (joined with a single line break, first line number kept). Only a
  single line longer than `MAX_PASSAGE_CHARS` is dropped.
- `_file_documents` reports `passage_too_large` only for a single line longer than `MAX_PASSAGE_CHARS` (or when no
  passage results from non-empty content), so the report says what was actually dropped.
- `e2e/case_walk.mjs` answer step: the refresh report counts passages, not files, so it waits for
  `Inclusi: N, Saltati: 0` with N at least 1 instead of the literal `Inclusi: 1`.

Out of scope (binding non-goals):
- No change to `MAX_PASSAGE_CHARS`, the other limits, passage ids, or headings.
- Files whose blocks all fit today produce exactly the same passages and digests as before (no reindex churn).
- No sentence splitting inside a long line.

## Files likely touched

- `backend/app/modules/ai/document_intake.py`
- `backend/tests/test_document_intake.py`
- `e2e/case_walk.mjs`

## Design constraints

- Smallest change inside `_passages`; no second splitter.
- The first line number of a passage stays the real line of its first text, so line citations remain correct.

## Acceptance criteria

1. A `.txt` file of 300 non-blank lines of 60 characters each (no blank lines) yields passages each at most 8000
   characters, covering every line in order, and no `passage_too_large` skip.
2. A passage built from that file records as its first line the file line where its text starts.
3. A file whose blank-line blocks all fit yields the same `extract_passages` output as on `ea2557ed` (test with a
   fixed sample and its expected passages).
4. A single line of 8001 characters is still skipped as `passage_too_large`; the existing intake tests pass.

## Required tests

- Unit tests in `backend/tests/test_document_intake.py` for criteria 1–4.

## Definition of done

Test gate green (see `AGENTS.md`), criteria 1–4 met, spec status updated. Risk: low (deterministic text splitting
covered by tests; only files that are skipped today change). After merge the coordinator reruns the case walk
privately and records the result in the ledger.
