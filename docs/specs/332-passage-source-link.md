# 332 — PASSAGE-SOURCE-LINK-1: The retrieval check credits a folder passage to the literature source whose file it is

Status: ready
Depends on: 316, 328, 331

## Goal

When the Memory › Ricerca retrieval check names a literature source (its title or id) as the expected answer, a
document-folder passage counts as that source when the folder file holding the passage has exactly the same bytes
(SHA-256) as the source's attached backing file. Today such a passage only matches its own file path or heading, so a
question answered by the paper's own text is reported as a miss.

## Why

The spec-328 case walk, run privately on a real literature case, reached six green cells of seven: the retrieval check
over the folder holding the paper text gave "Utilizzabile con avvertenze" (top-3 0) because the expected source title
ranked sixth, behind the paper's own folder passages, which carry only the file name and a heading. The passages were
right; the check could not tell they belong to the source. Identical bytes is a deterministic, explainable link; it
does not tune the question or the ranking.

## Code facts checked at `6ea31cb0`

- `backend/app/modules/ai/memory_routes.py::retrieval_eval` builds the `retrieval.eval` request from the UI questions
  (`query`, `expected_ids`) and runs it over the live index (`index_db_path`), workspace-scoped.
- `backend/app/modules/ai/retrieval_bench.py::_matches` accepts, for a `workspace_documents` hit, the object id, the
  title, the file path (`object_id` before `::`), the fragment and the heading; `evaluate_retrieval_request` reports
  per question `expected_ids` and `first_matching_rank`.
- `backend/app/modules/ai/document_intake.py::registered_folder` gives the workspace's registered folder;
  `_file_documents` names a passage by the folder-relative path of its file.
- `literature_sources.artifact_id` points to the backing artifact; `backend/app/modules/memory/literature_service.py::_BackingText.current_sha256`
  returns the SHA-256 of the stored backing bytes (None when absent, linked or unreadable).

## Scope

In scope:
- `retrieval_bench.evaluate_retrieval_request` accepts an optional per-question `accepted_ids` list (non-empty
  strings, bounded like `expected_ids`). A hit matching an accepted id counts for `first_matching_rank` exactly as an
  expected id does. The per-question result records `accepted_ids` next to `expected_ids` so the report shows why a
  hit counted.
- `memory_routes.retrieval_eval`: for each expected item that equals the id or the exact title of a literature source
  in the workspace whose backing SHA-256 is known, add to `accepted_ids` the folder-relative path of every `.md`/`.txt`
  file in the registered folder whose bytes have that SHA-256. Use the existing helpers for the backing digest and the
  registered folder; respect the existing intake limits (file count, file size, no symlinks).
- The retrieval check panel in Memory › Ricerca shows, when a question counted through a source file, one line such as
  "Contato come fonte: <titolo> (file <percorso>)"; the frontend `.mjs` test is updated for it.

Out of scope (binding non-goals):
- No change to ranking, indexing, passage ids, intake, or the literature tables; no migration.
- No fuzzy or partial matching: only byte-identical files link. A PDF backing never links to a text file.
- Matching by `expected_ids` alone behaves exactly as today.

## Files likely touched

- `backend/app/modules/ai/retrieval_bench.py`
- `backend/app/modules/ai/memory_routes.py`
- `backend/tests/` (retrieval bench and memory route tests)
- `frontend/src/pages/MemorySearch.tsx`, `frontend/src/api/memorySearch.ts` and the matching test in `frontend/tests/`

## Design constraints

- The digest link is computed by deterministic code in the route at request time; nothing is stored.
- A source with no backing, a linked or unreadable backing, or no identical folder file adds nothing.
- Folder-relative paths only in requests and reports; never an absolute path.

## Acceptance criteria

1. With a literature source whose backing is a text file and the same bytes copied into the registered folder, a
   retrieval check expecting the source title ranks the first passage of that file as a match (top-3 when it is among
   the first three hits) and reports the file path in `accepted_ids`.
2. The same with the folder file changed by one byte: no accepted id is added and the result equals today's.
3. An expected value that is not a literature source id or title produces no `accepted_ids`; existing retrieval bench
   and route tests pass unchanged.
4. `accepted_ids` given directly to the bench are validated like `expected_ids` (non-empty strings) and rejected
   otherwise.
5. The panel shows the "Contato come fonte" line only when a question counted through an accepted id.

## Required tests

- Backend unit tests for criteria 1–4 (bench and route), using the isolated data root and a temporary folder.
- Frontend `.mjs` test for criterion 5.

## Definition of done

Test gate green (see `AGENTS.md`), criteria 1–5 met, spec status updated. Risk: medium (feature code with tests,
read-only over existing records). After merge the coordinator reruns the case walk privately and records the result.
