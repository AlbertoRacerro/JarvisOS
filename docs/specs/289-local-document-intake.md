# 289 — Workspace document folder intake

Status: ready
Depends on: 287

## Goal

An operator can register a local folder for a workspace and search its text documents by heading-sized passages. Changes and deletions in that folder update the derived index, while every retrieved passage is reread from its source before it enters agent context.

## Why

Word search is now useful, but project documents are absent from the workspace index. One bounded document source gives retrieval, later literature work and the design value path the same cited passages without three separate extractors.

## Code facts checked at `63cd150b`

- `backend/app/modules/ai/retrieval_owners.py::canonical_owner_documents` projects workspace owner records into `IndexDocument` values.
- `backend/app/modules/ai/retrieval_owners.py::owner_catch_up` compares owner fingerprints and removes changed or deleted indexed documents.
- `backend/app/modules/ai/retrieval_owners.py::resolve_owner_document` rereads canonical owner records for a `SourceRef`.
- `backend/app/modules/ai/retrieval_index.py::SQLiteIndexStore.resolve_authoritative` refuses a changed digest or revision when resolving a hit.
- `backend/app/modules/ai/retrieval_query.py::WORKSPACE_SCOPED_OWNERS` limits agent retrieval to workspace-scoped owners.
- `backend/app/core/paths.py::build_paths` locates runtime data outside the repository.

## Scope

In scope:
- One operator-only local command to register, inspect, refresh and remove a folder for one existing workspace. Registration stores the canonical folder path under that workspace's data root; a model/tool cannot supply or change it.
- Read UTF-8 `.md` and `.txt` files, split Markdown at headings and long plain text at paragraph boundaries. Preserve a stable relative path and heading in the title and source reference.
- Detect changed and deleted files on refresh and before search. Index only changed passages and remove deleted passages through the existing owner sync. A search never exposes content from a stale or missing file.
- Return a bounded count of included, skipped, changed and deleted passages, with reasons for skipped files. Make the same extraction function callable by later literature and value slices.

Out of scope:
- PDF/OCR, a file watcher, cloud sync, a new embedding model, ranking changes, automatic value extraction, or a second literature intake.
- Agent control over folder registration or source paths; a UI folder picker is a later surface once the source semantics are proven.

## Files likely touched

- `backend/app/modules/ai/retrieval_owners.py`
- `backend/app/modules/ai/retrieval_index.py`
- `backend/app/modules/ai/retrieval_query.py`
- `backend/app/core/paths.py` if a named document-storage path is useful
- New small document-intake service and local command under `backend/app/modules/ai/`
- Focused tests under `backend/tests/`

## Design constraints

- Only a local operator command accepts an absolute folder path. The folder is never mounted into an agent sandbox or returned as a host absolute path in an agent result.
- Refuse a missing folder, symlink root, and symlinked files or directories. Do not follow links, special files, or paths escaping the registered root. Bound file count, file size and passage size; report limits instead of silently dropping content.
- Use workspace IDs from the canonical workspace store. No content, personal path or document title enters logs or repository files. Content and registration live under `JARVISOS_DATA_ROOT`.
- Preserve workspace isolation and existing sensitivity/egress policy. Indexed text is evidence, not permission to disclose it to a provider.
- The index is disposable: a rebuild finds registered folders and produces the same passage identities. A changed passage keeps its stable identity with a new digest; deleted source content is unavailable immediately, even before refresh.

## Acceptance criteria

1. A local operator registers a folder containing Markdown headings and text, refreshes it and finds the expected passage under the chosen workspace with relative path and heading citation.
2. A second workspace cannot retrieve it. An agent cannot register a folder or obtain the absolute host path from search results.
3. Editing a passage updates its indexed content and digest; deleting a file or removing the registration removes its hits. A stale candidate fails authoritative reread before the refresh.
4. Symlinks, binary/invalid UTF-8, oversized files and excessive file counts are skipped with bounded, useful reasons; no external target is read.
5. A clean rebuild from the same registration yields the same passage identities and searchable text.

## Required tests

- Offline temporary data-root and folder fixtures for registration, heading split, refresh, deletion, rebuild, workspace isolation, stale reread, bounds and symlink refusal.
- Focused retrieval query test proving the new owner is admitted only under the existing workspace grant.

## Definition of done

Focused tests and lint pass; CI is green on the exact implementation head; a severe review covers the filesystem and egress boundaries; STATUS records the implementation PR.
