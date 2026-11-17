# 300 — MEMORY-SEARCH-1: Search the workspace memory and manage its document folder from the Memory screen

Status: ready
Depends on: 287, 289, 291

## Goal

Memory gets a "Ricerca" page for the selected workspace. A search box returns the current, owner-validated passages
of the workspace memory (project knowledge, literature, models, process, engineering, chats, the document folder),
each with what kind of record it is, its readable title, where it sits in its source and a short excerpt. On the same
page a "Cartella documenti" box shows whether a document folder is registered, lets the owner register one by typing
its path, refresh it (with included, skipped, changed and deleted counts and the skip reasons) or remove it. The
operator command stays; it is no longer the only way.

## Why

Any-word search, credible-leg fusion with readable record summaries, and the document folder intake exist only behind
the agent and an operator command (directive L20261117071737-1216, point 2). The owner cannot ask the memory a question
or add his documents without a terminal; a capability reachable only by command is not counted as usable. This is
item 4 of the front-end plan in spec 298. Item 3 of that plan (source chips on numeric inputs) waits: facts with
locators exist only on the literature check today, so the chips would have almost nothing to show.

## Code facts checked at `6da44110`

- `backend/app/modules/ai/retrieval_query.py::query_context` returns current evidence only (authoritative reread and
  digest match), bounded by `MAX_LIMIT` 32 and `MAX_TOKEN_BUDGET` 8192, with `source_ref`, `content_digest` and
  `excerpt` per item; `WORKSPACE_SCOPED_OWNERS` lists the owners indexed with a workspace identity.
- `backend/app/modules/ai/jarvis_context_models.py::SourceRef` carries `authority_owner`, `object_type`, `object_id`,
  `workspace_id` and an optional `location` (line range or fragment).
- `backend/app/modules/ai/document_intake.py::register_folder`, `remove_folder`, `refresh_folder` and
  `registered_folder` implement the folder registration (absolute, existing, non-symlink directory, stored under the
  workspace data root with mode 0600); `refresh_folder` returns `included`, `skipped`, `changed`, `deleted` and
  `reasons`. `registered_folder` is documented "never serialize this path into retrieval output".
- `backend/app/modules/ai/document_intake.py::main` is the operator command; spec 289 left "a UI folder picker" as a
  later surface.
- No HTTP route exposes `query_context` or the folder functions (searched `backend/app/modules/ai/*routes*.py`).
- No HTTP route checks for an owner; agent tools are in-process (`backend/app/modules/engineering/bench.py::dispatch_bench_tool`
  is the pattern) and never call HTTP routes.
- `frontend/src/app/routes.ts` declares the Memory sub-navigation (Project Basis, Models, Literature, Review).

## Scope

In scope:
- Backend routes on a new router with prefix `/workspaces/{workspace_id}/memory`: `GET /search?q=` (calls
  `query_context` with the workspace id and `source_scope=WORKSPACE_SCOPED_OWNERS`, limit 12, token budget 2048);
  `GET /documents` (registered or not, and the folder's last path component only); `PUT /documents` with
  `{"folder": "<absolute path>"}`; `POST /documents/refresh`; `DELETE /documents`.
- A "Ricerca" entry in the Memory sub-navigation at `/memory/search` with the search box, the result list and the
  "Cartella documenti" box.
- Plain Italian names for the owners (`project_knowledge` "Base di progetto", `literature` "Letteratura", `modeling`
  "Modelli", `process_stack` "Processo", `engineering` "Ingegneria", `bluecad` "CAD", `ai_threads` "Conversazioni",
  `development` "Sviluppo", `workspace_documents` "Documenti"); an unknown owner is shown as stored.

Out of scope (binding non-goals):
- An agent tool, broker method or MCP entry for registering a folder; any change to search ranking, the index or
  the intake rules; PDF intake into the folder; a native file dialog or a directory browser served by the backend.
- The retrieval quality figure: a retrieval bench run already appears on the Runs screen (spec 299).
- Repository documents (no workspace identity) in the results.

## Files likely touched

- `backend/app/modules/ai/memory_routes.py` (new), `backend/app/main.py` (include the router)
- `frontend/src/pages/MemorySearch.tsx` (new), `frontend/src/api/memorySearch.ts` (new), `frontend/src/app/routes.ts`
- `backend/tests/test_memory_routes.py`, `frontend/tests/300-memory-search.test.mjs`, `frontend/package.json`
- `e2e/memory_search_walk.mjs`

## Design constraints

- No absolute host path in any response, including errors and skip reasons: the folder is shown by its last
  component; skip reasons keep their relative path. The registration path appears only in the owner's own input box.
- Registration keeps every refusal of `register_folder` and adds: the route answers 422 with a plain code
  (`folder_invalid`) and never echoes the path; the folder may not be inside the data root (`backend/app/core/paths.py::build_paths`) or the repository checkout
  the backend runs from.
- Search shows only `validation == "current"` items; the excerpt is shown as text, never as HTML or Markdown.
- Read routes write nothing; refresh and register run only on an explicit click.
- Visible text in Italian; owner ids, object ids and digests are not shown, except the relative document path and
  heading for `workspace_documents`.

## Acceptance criteria

1. With fixture records in two workspaces, search in one returns its matching items with owner name, title and
   excerpt and nothing from the other workspace or the repository.
2. Register, refresh and remove a temporary folder through the routes: counts match `refresh_folder`, a removed
   folder's passages are no longer found, and no response contains the absolute path.
3. A relative path, a missing folder, a symlink, a file, a folder inside the data root and one inside the repository
   are refused with `folder_invalid`.
4. No agent tool, broker method or MCP descriptor references the registration route or `register_folder`
   (a test asserts it over the tool registries).
5. The browser walk from `/` opens Memory, then Ricerca, registers a fixture folder by typing its path, refreshes it,
   searches a word from one of its files and sees the passage with its relative path and heading.

## Required tests

- Backend: temporary data root, two workspaces, fixture folder with Markdown headings and a symlink; search scope,
  path-free responses, every refusal, register/refresh/remove, the agent-unreachability check.
- Frontend `.mjs`: the page title, the empty-state text, the owner names, the "Cartella documenti" labels.
- Browser walk in `e2e/` from a clean profile at `/` with a temporary data root and fixture folder, attached as
  evidence.

## Definition of done

Risk is high: the registration route lets an HTTP request choose a host folder whose text enters the workspace
memory. Test gate green, CI green on the exact head, the browser walk passed and attached, a full diff review by a
second qualified session, acceptance criteria met, STATUS row updated.
