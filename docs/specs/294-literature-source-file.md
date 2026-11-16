# 294 — LITERATURE-SOURCE-FILE-1: Attach a paper file to a literature source from the Literature page

Status: merged through PR #147
Depends on: 293

## Goal

On the Literature page a person can attach a local text, markdown or PDF file to a literature source (new or existing
without a file), record a datum with its number and line range, and see the datum's checks and verdict from spec 293.
Today no route or control creates a `literature_source` artifact, so no source can have a file and no datum can be
checked from the interface.

## Why

"Load the paper" and "numbers as located facts" are the first steps of every literature acceptance case (directives
`L20261116154657-88a1` point 2, `L20261116160140-7608` point 2, `L20261116201429-63ee` rule 6: a capability reachable
only by code does not count). Spec 293 made the check; this slice makes it reachable by a person. PDF text extraction
and page locators stay for a later slice: a PDF is stored, previewed and its datums read `unchecked`.

## Code facts checked at `be9ea957`

- `backend/app/modules/memory/literature_service.py::create_literature_source` accepts an `artifact_id` and requires an
  artifact of type `literature_source` with status `registered` (`ALLOWED_LITERATURE_ARTIFACT_TYPES`,
  `CURRENT_ARTIFACT_STATUS`).
- `backend/app/modules/memory/literature_service.py::_content_state` serves a backing only when its `stored_path` is under
  the data root, outside the secrets directory, and its mime type is in `ALLOWED_PREVIEW_MIME_TYPES`
  (`application/pdf`, `text/plain`, `text/markdown`).
- `backend/app/modules/memory/literature_service.py::_validate_locator` accepts line locators on text/markdown backings.
- `backend/app/modules/memory/literature_routes.py::create_literature_entry_endpoint` and
  `create_literature_source_endpoint` are the existing write routes under `/workspaces/{workspace_id}/literature`.
- `backend/app/modules/files/service.py::create_artifact_record` inserts an `artifacts` row.
- `backend/app/modules/environment/routes.py::upload` streams a raw request body to a file with a size bound and a
  sanitised filename; follow its pattern.
- `frontend/src/pages/LiteratureKnowledge.tsx::SourceDisclosure` renders a source, its entries and the "Add a finding"
  form, which today sends only `value_text`, `unit` and `context_text` for a datum.
- `frontend/src/api/literature.ts::createLiteratureEntry` posts an entry.

## Scope

In scope:
- Backend: `POST /workspaces/{workspace_id}/literature/files?filename=<name>` with the raw file as body. Accepts suffixes
  `.txt`, `.md`, `.markdown`, `.pdf` only; mime from the suffix (`text/plain`, `text/markdown`, `application/pdf`); a PDF
  must start with `%PDF-`, text must decode as UTF-8; at most 20 MiB. Stores the bytes under
  `build_paths().artifacts_dir / "literature" / <workspace_id> / <sha256><suffix>` (written to a temporary file in that
  directory, then renamed; an identical existing file is reused), registers one `artifacts` row of type
  `literature_source`, status `registered`, and returns `{artifact_id, filename, mime_type, sha256, size}`.
- Backend: `POST /workspaces/{workspace_id}/literature/sources/{source_id}/backing` with `{artifact_id}`. Sets the
  backing only when the source has none (`409 literature_backing_exists` otherwise); the artifact must be an eligible
  `literature_source` artifact of the same workspace. Returns the `LiteratureSourceRead`.
- Frontend, Literature page:
  - The register-source form gets an optional "Source file" input; the file is uploaded first and its `artifact_id`
    sent with the source.
  - A source without a file shows an "Attach source file" control that uploads and attaches.
  - The datum form gets optional "Number" (numeric input) and "Lines" from/to fields, shown only when the source has an
    available text or markdown backing; the entry is sent with `value_number`, `unit`, `locator_kind: "line"`,
    `locator_start`, `locator_end`. A server refusal (for example a range outside the file) is shown as the form error.
  - Each datum shows its verdict in plain words (`usable` → "Checked against the source", `usable_with_warnings` →
    "Checked, with warnings", `unchecked` → "Not checked", `not_usable` → "Does not match the source") and, for every
    check that is not `pass`, its hint.
- Update `frontend/tests/114-literature-knowledge.mjs` for the new strings and calls.
- New walk `e2e/literature_datum_walk.mjs` (fresh temporary data root, `JARVISOS_PROOF_PORT`), listed in
  `e2e/README.md`: from `/` with visible controls only, reach Memory → Literature, register a source with a small
  markdown file, add a datum whose number is on the stated line and see "Checked against the source", add one with a
  wrong number and see "Does not match the source" with its hint; screenshots and a JSON evidence file.

Out of scope (binding non-goals):
- PDF text extraction, page locators, Docling or any new dependency.
- Replacing or removing a source's file, deleting artifacts, or editing existing entries.
- Any schema change.
- Agent tools, requirement checks, or the value store.

## Files likely touched

- `backend/app/modules/memory/literature_service.py`
- `backend/app/modules/memory/literature_routes.py`
- `backend/app/modules/memory/literature_models.py`
- `backend/tests/test_literature_source_file.py` (new)
- `frontend/src/api/literature.ts`
- `frontend/src/pages/LiteratureKnowledge.tsx`
- `frontend/tests/114-literature-knowledge.mjs`
- `e2e/literature_datum_walk.mjs` (new), `e2e/README.md`
- `frontend/src/api/generated/*`, `docs/MODULES.md` (generated only)

## Design constraints

- The stored path is built only from the workspace id (checked to exist), the sha256 hex and the allowed suffix; the
  client filename is kept only as the artifact `filename` after `Path(...).name` and a 255-character bound. Nothing is
  written outside `artifacts_dir`.
- The upload never follows symlinks for the target directory or file; a partial file is removed on any error.
- A refused upload returns a structured error (`code`, `message`) like the other literature routes; no path appears in
  any response.
- The frontend calls only the backend routes (Never 3) and keeps the existing request-key idempotency for entries.
- The existing literature routes and responses do not change.

## Acceptance criteria

1. Uploading a markdown file returns an artifact id; creating a source with it shows `backing.availability ==
   "available"` and the preview route serves the same bytes.
2. Uploading `.exe`, a `.pdf` without `%PDF-`, non-UTF-8 `.txt`, or more than 20 MiB is refused with no file left behind.
3. Uploading the same bytes twice stores one file.
4. Attaching a backing to a source that has one returns 409; attaching another workspace's artifact is refused.
5. Through the routes only, a datum with a number on its stated line reads verdict `usable`; a wrong number reads
   `not_usable`.
6. The walk passes from a clean browser profile and its evidence directory is attached to the PR.
7. `npm run build`, ruff, focused backend tests, contracts check, architecture enforcement and the typecheck ratchet
   pass.

## Required tests

- `backend/tests/test_literature_source_file.py`: criteria 1–5 through the API with the test client.
- `frontend/tests/114-literature-knowledge.mjs` asserts the new controls and verdict wording.
- `e2e/literature_datum_walk.mjs` for criterion 6.

## Definition of done

Test gate green (see `AGENTS.md`), walk evidence attached, acceptance criteria met, STATUS row updated.
