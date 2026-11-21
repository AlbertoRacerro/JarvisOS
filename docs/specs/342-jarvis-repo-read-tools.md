# 342 — JARVIS-REPO-READ-TOOLS-1: Bounded read-only repository and private-pack code/doc read tools for Jarvis chat

Status: ready
Depends on: 341

## Goal

Jarvis chat (through the existing Hermes MCP broker) can search, list, and read files from both the public JarvisOS repository and any loaded private pack repository via a single bounded read-only tool `jarvis_repo_read`. Every response carries explicit scope and confidentiality metadata (`scope: "public"` vs `scope: "pack"` with `pack_id` and `confidentiality: "private_pack"`), enforces strict path/symlink/secret containment, and fails closed against private-pack reads on non-local cloud routes.

## Why

Directive `L20261120222744-75d9` (item 2, moved forward from step 3 of `L20261120133717-6f0b`): Jarvis chat needs direct, accurate read-only access to JarvisOS code/docs and the loaded private pack repository so it can answer code and documentation questions (e.g. where a coefficient or rule is implemented) with exact file paths and line numbers. Repository files are intentionally excluded from workspace-scoped Second Brain retrieval (`WORKSPACE_SCOPED_OWNERS`), so a dedicated bounded read-only tool with explicit scope separation is required. Write, worker, and deployment authority remain unchanged.

## Code facts checked at `6e66e25d`

- `backend/app/modules/agents/hermes/broker_mcp.py::_reply` exposes `_TOOLS` over stdio JSON-RPC and forwards `tools/call` requests to `/v1/jarvis/tool`.
- `backend/app/modules/agents/hermes/supervisor.py::HERMES_TOOL_ALLOWLIST` and `_TOOL_CAPABILITIES` govern which MCP tools Hermes may invoke and map each tool to a registered `JarvisCapabilityDescriptor`.
- `backend/app/core/packs.py::load_packs` loads optional project packs from `JARVISOS_PACK_PATH`, returning `Pack(id, root, ...)` entries (empty tuple when no pack is configured).
- `backend/app/modules/ai/retrieval_query.py::query_context` restricts workspace retrieval to `WORKSPACE_SCOPED_OWNERS` and excludes repository documents.
- `backend/app/modules/ai/thread_service.py::submit_interaction` orchestrates Hermes chat turns and issues per-turn read-only capability grants.

## Scope

In scope:
- **New read-only MCP tool `jarvis_repo_read`** in `backend/app/modules/agents/hermes/broker_mcp.py`, allowlisted in `backend/app/modules/agents/hermes/supervisor.py` (`HERMES_TOOL_ALLOWLIST`, `_TOOL_CAPABILITIES`, `_HERMES_REPO_READ_CAPABILITY` with `action_class="READ"`), and dispatched to a dedicated module `backend/app/modules/agents/hermes/repo_read_tool.py`:
  - **Scope selection**:
    - `scope="public"` (default): resolves the JarvisOS repository root (with test override support). Every response includes `scope="public"`, `pack_id=None`, `confidentiality="public"`.
    - `scope="pack"`: resolves the loaded pack root via `app.core.packs.load_packs()` (optional `pack_id` required when multiple packs are loaded; defaults to the single loaded pack otherwise). When no pack is loaded, returns a clean structured error (`pack_not_configured`) without failing the server. Every successful response includes `scope="pack"`, `pack_id=<id>`, `confidentiality="private_pack"`.
    - **Fail-closed cloud boundary**: when the active turn route is not local (e.g., an external/cloud provider route), `scope="pack"` is refused with `error_code="pack_cloud_egress_blocked"` so private pack contents never reach cloud models before item 5 introduces explicit IP egress modes.
  - **Operations (`op`)**:
    - `op="list"`: lists relative file paths under an optional relative `path` prefix (bounded `limit` 1..200, default 100), skipping `.git`, `node_modules`, `.venv`, `__pycache__`, build caches, and denylisted secret/binary paths.
    - `op="search"`: bounded case-insensitive literal substring search across UTF-8 text files in the selected scope (optional relative `path` prefix, `query` 1..200 chars, `limit` 1..50, default 20), returning `{path, line, snippet}` with snippets capped at 240 characters.
    - `op="read"`: exact file read by relative `path` with optional `start_line` (1-based, default 1) and `max_lines` (1..300, default 150), capped at 16,000 characters total, returning `{path, start_line, end_line, total_lines, truncated, content}`.
- **Per-turn grant and prompt guidance**:
  - Issue a `jarvis.repo_read` grant alongside `jarvis.retrieval_query` and `jarvis.dev_read` on every Hermes chat turn in `thread_service.py`.
  - Advertise `jarvis_repo_read` (with its `grant_id`, scopes `public`/`pack`, and ops `list`/`search`/`read`) in the turn prompt alongside `jarvis_dev_read`.

Out of scope (binding non-goals):
- Any file write, patch, git mutation, or command execution.
- Second-brain / literature / read-only SQL views (item 3 of `L20261120222744-75d9`).
- Full sanitized/full cloud IP egress modes for private packs (item 5 of `L20261120222744-75d9`; this spec fails closed on non-local routes).
- Any UI changes.

## Files likely touched

- `backend/app/modules/agents/hermes/repo_read_tool.py` (new)
- `backend/app/modules/agents/hermes/broker_mcp.py`
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/ai/thread_service.py`
- `backend/app/modules/ai/context_builder.py`
- `backend/tests/test_hermes_repo_read_342.py` (new)
- `backend/tests/test_hermes_runtime.py` (if allowlist/tool count assertions need updating)

## Design constraints

- Strictly read-only: opens files in read-only mode only; never writes, creates, or mutates anything on disk.
- Containment and secret safety:
  - Rejects absolute paths, `..` traversal, null bytes, symlinks resolving outside the scope root, `.git` internals, and secret-like filenames (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `*secret*`, `*credential*`, `*.sqlite*`).
  - Never returns absolute host filesystem paths (always repo-relative or pack-relative POSIX paths).
  - Skips binary files (NUL byte detection in initial bytes) cleanly without crashing.
- Core runs without a pack: all tests use temporary fixture directories or the fake pack in `backend/tests/fixtures/packs/zephyr`; never references any real private pack.
- Bounded payloads: deterministic caps on file count, search hits, line count, snippet length, and character count.
- High risk (extends Hermes tool allowlist and reads filesystem/private-pack scopes): requires independent second-session full-diff review.

## Acceptance criteria

1. `jarvis_repo_read` is listed in `broker_mcp.py::_TOOLS`, allowlisted in `HERMES_TOOL_ALLOWLIST`, registered as `jarvis.repo_read` (`action_class="READ"`), and granted on every Hermes chat turn.
2. `list`, `search`, and `read` work deterministically on both `scope="public"` and `scope="pack"` (using a temporary/fake pack fixture), returning relative paths, line numbers, bounded content/snippets, and accurate `scope`/`pack_id`/`confidentiality` labels.
3. Path traversal (`../`), absolute paths, escaping symlinks, `.git/` paths, and secret-like files (`.env`, `*.pem`, `*.key`) are refused.
4. `scope="pack"` returns a clean `pack_not_configured` response when no pack is loaded, and is refused with `pack_cloud_egress_blocked` when called on a non-local route.
5. Turn prompt guidance advertises `jarvis_repo_read` with its `grant_id`, scopes, and operations.

## Required tests

- Offline unit tests in `backend/tests/test_hermes_repo_read_342.py` covering `list`, `search`, and `read` across both `public` and fake `pack` scopes, line slicing and truncation, binary file handling, path-traversal/symlink/secret-file refusals, missing-pack behavior, cloud-route fail-closed refusal, and grant enforcement.
- Existing `test_hermes_runtime.py` and `test_ai_threads.py` pass without weakening assertions.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, independent second-session full-diff review, spec status updated.
