# 296 — LITERATURE-STRUCTURED-PDF-1: Read PDF tables beside the source

Status: draft; technical readiness probe pending
Depends on: 295

## Goal

The Literature screen can show locally extracted page text and tables for an attached PDF. A person can select a number
from that view to prefill a raw datum with its page locator, then use the existing deterministic source check. The
extracted document is a rebuildable view, not an accepted engineering value.

## Why

Spec 295 checks a number after the operator has typed it, using the PDF text layer. Tables and reading order are still
hard to inspect from that layer. This is the first bounded part of [contract 248](248-design-value-path.md) slice F:
Docling supplies a structured local view; a later slice will propose and ingest values automatically. Keeping the
selection and checking distinct prevents an extracted or OCR number from silently becoming a usable value. The full
Docling pipeline needs local model artifacts and must never download them during a product request.

## Code facts checked at `24739ae7`

- `backend/app/modules/memory/literature_service.py::_BackingText` reads a PDF's cached page text when a datum needs it.
- `backend/app/modules/memory/literature_service.py::_validate_locator` accepts page ranges only on readable PDF text.
- `backend/app/modules/memory/literature_service.py::create_literature_entry` stores a raw datum with a page locator and
  request-key idempotence.
- `backend/app/modules/memory/literature_checks.py::check_datum` derives source and unit checks on read; it does not
  promote a value into a calculation.
- `backend/app/modules/memory/literature_routes.py::get_literature_source_content_endpoint` serves the registered PDF
  after the workspace and backing checks in `resolve_literature_content`.
- `frontend/src/pages/LiteratureKnowledge.tsx::SourceDisclosure` owns the source preview and raw datum form.

## Scope

In scope:

- Add a pinned, locally run Docling installation and record its exact version, licence and local model artifacts in the
  PR. Conversion uses only an already stored literature PDF, never a URL. Layout and table extraction run with network
  access disabled and with an explicit artifacts directory; missing artifacts give a clear unavailable state.
- Produce a bounded, derived structured view keyed by source SHA-256 and extractor/model version: ordered page blocks,
  table captions where available, table cells and page numbers. Preserve the original PDF and the spec-295 page cache.
  Cache writes are atomic and never follow a symlink. A changed extractor or source rebuilds the derived view.
- Add a workspace-scoped read endpoint for one source's structured view. Return bounded page and table content with
  explicit `available`, `unavailable` or `failed` state and a reason. Do not expose local paths or model artifact paths.
- In the Literature screen, let the operator open the extracted view next to the original PDF and select a numeric
  passage or cell. Selection prefills the existing raw datum form with the exact visible token and page locator; it does
  not save or accept the datum. After save, display the existing source checks and verdict. A token seen only through OCR
  or inferred table structure remains `unchecked` unless the existing PDF text-layer check independently finds it.
- Use the existing stored source and entry routes; no second literature registry or source upload path.

Out of scope (binding non-goals):

- Automatic candidate creation, automatic admission into calculations or Materials, and batch notifications: these are
  later contract-248 slices and are not claimed by this spec.
- OCR-only scans, cloud conversion, PaperQA2, a new model download at request time, or any online source search.
- Changing the semantics of the existing datum checks or treating Docling's inferred output as proof of a number.

## Files likely touched

- `backend/requirements.txt`
- `backend/app/modules/memory/literature_service.py`
- `backend/app/modules/memory/literature_routes.py`
- `backend/app/modules/memory/literature_models.py`
- `backend/app/modules/memory/pdf_text.py` or a sibling structured PDF module
- `backend/tests/test_literature_pdf_pages.py` or a focused new test
- `frontend/src/api/literature.ts`
- `frontend/src/pages/LiteratureKnowledge.tsx`
- `frontend/tests/114-literature-knowledge.mjs`
- `e2e/literature_datum_walk.mjs`
- `docs/MODULES.md` (generated only)

## Design constraints

- The conversion must be bounded by input size, pages, output size and a wall limit. Conversion cannot run while
  listing sources or rendering a source page; it must be an explicit asynchronous task with a visible progress/error
  state. A repeated request for the same source and extractor version reuses the result or joins the same task.
- Installation and model prefetch are explicit setup steps; ordinary operation is offline. Tests use a small generated
  PDF and fake converter output, so CI never downloads a model or needs a paid provider.
- Every displayed block retains its PDF page. If Docling cannot establish a page for a table cell, show it as
  unlocated; it cannot prefill a page locator. Bound and escape all extracted text in the API and UI.
- The source SHA-256 comes from the registered artifact after the workspace check. Client-provided paths, hashes and
  cache filenames are never trusted.
- A returned `available` view is evidence of conversion only, not evidence that all tables or values were extracted.

## Acceptance criteria

1. From the Literature screen, a stored two-page PDF with a numeric table on page 2 shows its original preview and an
   extracted table with page 2 provenance. Selecting a cell prefills the existing datum form; saving it yields the
   spec-295 check against page 2. No datum is created merely by extracting or selecting.
2. An OCR-only or unreadable PDF, a missing local model bundle and a failed conversion have distinct readable states;
   none grants a usable datum.
3. Conversion is not performed on source list/read; a second extraction request on the same SHA and version does not
   reconvert. Replacing the model version or source bytes invalidates the derived cache.
4. Cross-workspace access, symlinked cache or backing, oversized input/output, timeout and concurrent duplicate request
   are refused or handled without exposing paths or modifying the original source.
5. The browser walk reaches extraction and selection from `/` through visible controls with a clean profile. Its
   evidence directory is attached before maintainer inspection.
6. Focused backend and frontend tests, lint, build, contract and architecture gates pass; exact-head CI is green.

## Required tests

- Offline backend tests for the endpoint, caching, workspace isolation, local-only failure states, bounds and duplicate
  requests; the converter is faked except for a local generated-PDF smoke proof.
- Frontend source tests for extraction states and prefill.
- Extend the literature browser walk for criterion 5.

## Definition of done

The extraction and selection path is usable in the Literature screen, the accepted checks still gate datum usability,
the browser walk and test gate pass, and the STATUS row is reconciled.

## Before acceptance

The first local proof used Docling 2.150.1 and an explicit offline bundle of layout and table models. A generated
two-page PDF with a ruled table produced one table, its cells and page 2 provenance in 17.9 s, with 2.6 GB peak
resident memory. The isolated environment occupied 6.2 GB and the model bundle 669 MB. A simpler unruled text sample
converted but produced no table. These are one-run observations on generated inputs, not a claim about arbitrary papers.

Before acceptance, decide how to package this optional large dependency and run a bounded conversion without blocking
source reads or adding a second job framework. Verify a real public paper's table and page attribution using an
offline-only conversion. Narrow this spec if those checks do not support a reliable page-located table view. No
implementation begins from this draft.
