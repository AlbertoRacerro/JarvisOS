# 343 — JARVIS-KNOWLEDGE-SQL-READ-1: Bounded read-only Second Brain, literature, and named SQL views for Jarvis chat

Status: ready
Depends on: 342

## Goal

Jarvis chat (through the existing Hermes MCP broker) can inspect structured workspace knowledge — Second Brain memory records, literature sources and extracted claims/data, project revisions, calculation runs, recent AI job metadata, and safe domain table counts — via a single bounded read-only tool `jarvis_knowledge_read` backed by named, typed read-only views over `jarvisos.db`.

## Why

Directive `L20261120222744-75d9` (item 3, moved forward from step 3 of `L20261120133717-6f0b`): semantic retrieval snippets alone cannot answer structured or aggregate questions about the user's Second Brain, literature library, calculation history, or workspace state (e.g., "which literature claims are accepted?", "list my recent calculation runs", "how many promoted memory records exist in this workspace?"). Named, typed read-only views give Jarvis deterministic, structured access to `jarvisos.db` without exposing arbitrary SQL execution, write authority, or secret/credential tables.

## Code facts checked at `01f170ab`

- `backend/app/modules/agents/hermes/broker_mcp.py::_reply` exposes `_TOOLS` over stdio JSON-RPC and forwards `tools/call` requests to `/v1/jarvis/tool`.
- `backend/app/modules/agents/hermes/supervisor.py::HERMES_TOOL_ALLOWLIST` and `_TOOL_CAPABILITIES` govern which MCP tools Hermes may invoke and map each tool to a registered `JarvisCapabilityDescriptor`.
- `backend/app/core/database.py::open_sqlite_connection` opens connections to `jarvisos.db` resolved via `get_database_path`.
- `backend/app/modules/memory/literature_service.py::list_literature_sources` reads workspace-scoped literature sources and their backing state.
- `backend/app/modules/memory/service.py::list_proposals` lists workspace-scoped memory proposals and promoted/rejected records.
- `backend/app/modules/project_knowledge/service.py::list_revisions` lists workspace-scoped project knowledge revisions.
- `backend/app/modules/ai/thread_service.py::submit_interaction` orchestrates Hermes chat turns and issues per-turn read-only capability grants.

## Scope

In scope:
- **New read-only MCP tool `jarvis_knowledge_read`** in `backend/app/modules/agents/hermes/broker_mcp.py`, allowlisted in `backend/app/modules/agents/hermes/supervisor.py` (`HERMES_TOOL_ALLOWLIST`, `_TOOL_CAPABILITIES`, `_HERMES_KNOWLEDGE_READ_CAPABILITY` with `action_class="READ"`), and dispatched to a dedicated module `backend/app/modules/agents/hermes/knowledge_read_tool.py`:
  - **Named, typed views (`view` parameter; no arbitrary SQL)**:
    1. `workspaces`: lists workspaces (`id`, `name`, `description`, `created_at`, `updated_at`), bounded `limit` 1..100 (default 50).
    2. `memory_records`: lists workspace-scoped memory records (`id`, `record_type`, `title`, `summary`, `status`, `confidence`, `tags`, `updated_at`), optional filters `status`, `record_type`, `query` (case-insensitive substring), bounded `limit` 1..100 (default 30).
    3. `literature_sources`: lists workspace-scoped literature sources (`id`, `title`, `authors`, `year`, `source_type`, `locator`, `backing_state`, `entry_count`), optional `query` filter, bounded `limit` 1..100 (default 30).
    4. `literature_entries`: lists workspace-scoped literature claims/data (`id`, `source_id`, `entry_type`, `claim_text`, `value`, `unit`, `status`, `locator`, `updated_at`), optional filters `source_id`, `status`, `query`, bounded `limit` 1..100 (default 30).
    5. `project_revisions`: lists workspace-scoped project knowledge revisions (`id`, `status`, `summary`, `created_at`) and current revalidation status summary, bounded `limit` 1..50 (default 20).
    6. `calc_runs`: lists workspace-scoped calculation runs (`id`, `calculator_id`, `status`, `created_at`, `inputs_summary`, `outputs_summary`), optional `calculator_id` filter, bounded `limit` 1..50 (default 20).
    7. `ai_jobs_recent`: lists workspace-scoped `ai_jobs` metadata (`id`, `task_type`, `route_class`, `provider_id`, `model`, `status`, `created_at`, `completed_at`, `error_code` — never prompt/response/secret bodies), bounded `limit` 1..50 (default 20).
    8. `table_counts`: returns row counts for an explicit allowlist of non-secret domain tables (`workspaces`, `memory_records`, `literature_sources`, `literature_entries`, `ai_threads`, `ai_jobs`, `calc_runs`, `artifacts`, `project_revisions`, `project_validations`), scoped to the active `workspace_id` where applicable.
  - **Workspace scoping**: defaults to the active turn's `workspace_id`; rejects cross-workspace queries when a turn is bound to a specific workspace unless the view is `workspaces`.
  - **Read-only SQLite enforcement**: every query executes with `PRAGMA query_only = ON` (never mutating `jarvisos.db`), uses parameterized queries only, and rejects unknown `view` names with a clean structured error (`unknown_view`).
- **Per-turn grant and prompt guidance**:
  - Issue a `jarvis.knowledge_read` grant alongside `jarvis.retrieval_query`, `jarvis.dev_read`, and `jarvis.repo_read` on every Hermes chat turn in `thread_service.py`.
  - Advertise `jarvis_knowledge_read` (with its `grant_id` and available `view` names) in the turn prompt in `context_builder.py`.

Out of scope (binding non-goals):
- Arbitrary user- or model-supplied SQL strings.
- Any write, promotion, deletion, or mutation of memory, literature, calculations, or database rows.
- Capability envelope / larger retrieval budget tuning (item 4 of `L20261120222744-75d9`).
- Full cloud IP egress modes for private packs (item 5 of `L20261120222744-75d9`).
- Any UI changes.

## Files likely touched

- `backend/app/modules/agents/hermes/knowledge_read_tool.py` (new)
- `backend/app/modules/agents/hermes/broker_mcp.py`
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/ai/thread_service.py`
- `backend/app/modules/ai/context_builder.py`
- `backend/tests/test_hermes_knowledge_read_343.py` (new)
- `backend/tests/test_hermes_runtime.py` (if allowlist/tool count assertions need updating)

## Design constraints

- Strictly read-only: enforces `PRAGMA query_only = ON` on the SQLite connection and never runs DDL/DML.
- No secret/credential leakage: explicit column allowlists per view; never queries credential, secret, API-key, or token tables; never returns raw host filesystem paths.
- Graceful schema resilience: handles empty workspaces cleanly (returning empty lists / zero counts without 500 errors).
- Bounded payloads: caps string fields (e.g., 300 chars per summary/claim field), row limits, and total serialized payload size (`<= 16,000` chars) with `truncated` metadata when trimmed.
- High risk (extends Hermes tool allowlist and reads `jarvisos.db` views): requires independent second-session full-diff review.

## Acceptance criteria

1. `jarvis_knowledge_read` is listed in `broker_mcp.py::_TOOLS`, allowlisted in `HERMES_TOOL_ALLOWLIST`, registered as `jarvis.knowledge_read` (`action_class="READ"`), and granted on every Hermes chat turn.
2. All eight named views (`workspaces`, `memory_records`, `literature_sources`, `literature_entries`, `project_revisions`, `calc_runs`, `ai_jobs_recent`, `table_counts`) return structured, bounded, workspace-scoped results from `jarvisos.db`.
3. Unknown view names return a structured `unknown_view` error; no raw SQL execution path exists; `PRAGMA query_only = ON` is enforced.
4. No secret, credential, token, or absolute host filesystem path is ever included in any view response; cross-workspace leakage is prevented.
5. Turn prompt guidance advertises `jarvis_knowledge_read` with its `grant_id` and supported views.

## Required tests

- Offline unit tests in `backend/tests/test_hermes_knowledge_read_343.py` covering all eight views against an isolated test SQLite database, workspace isolation, filter parameters (`status`, `record_type`, `source_id`, `calculator_id`, `query`), payload truncation, unknown view refusal, read-only enforcement, and grant verification.
- Existing `test_hermes_runtime.py` and `test_ai_threads.py` pass without weakening assertions.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, independent second-session full-diff review, spec status updated.
