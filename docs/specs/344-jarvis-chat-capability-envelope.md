# 344 — JARVIS-CHAT-CAPABILITY-ENVELOPE-1: Unified read-tool capability envelope, prompt guidance, and larger retrieval budget

Status: ready
Depends on: 343

## Goal

Give Jarvis chat an accurate, bounded capability envelope at the start of every turn so the assistant knows exactly which read-only tools are granted (`jarvis_dev_read`, `jarvis_repo_read`, `jarvis_knowledge_read`, `jarvis_retrieval_query`), when to call each one, what pack/route boundaries apply, and provide a larger deterministic context/retrieval budget for multi-source engineering answers.

## Why

Directive `L20261120222744-75d9` (item 4, moved forward from step 3 of `L20261120133717-6f0b`): adding read-only tools in Specs 341–343 is not enough if the assistant's system envelope and prompt instructions still tell it that it lacks access, or fail to distinguish when to use dev/runtime reads vs repo/pack reads vs structured Second Brain SQL views vs semantic retrieval, or truncate context prematurely at 32,000 characters / 20 blocks.

## Code facts checked at `13aca738`

- `backend/app/modules/ai/thread_service.py::_jarvis_system_envelope` builds the bounded `JARVIS_OPERATOR_REFERENCE` text injected into chat turns.
- `backend/app/modules/ai/thread_service.py::_turn_tools` returns the active turn's tool list and grant bindings.
- `backend/app/modules/ai/context_builder.py::assemble_prompt` combines `SYSTEM_INSTRUCTIONS`, `THREAD_AUTHORITY_INSTRUCTIONS`, `JARVIS_OPERATOR_REFERENCE`, `PROJECT_CONTEXT`, and `USER_REQUEST`.
- `backend/app/modules/ai/context_builder.py::build_workspace_context_bundle` enforces `DEFAULT_CONTEXT_BUDGET_CHARS` and `MAX_CONTEXT_BLOCKS`.
- `backend/app/modules/ai/retrieval_query.py::query_context` executes workspace-scoped retrieval queries.

## Scope

In scope:
- **Unified capability envelope in `thread_service.py::_jarvis_system_envelope`**:
  - Include a bounded `Read-only tools granted:` section listing the active read tools (`jarvis_dev_read`, `jarvis_repo_read`, `jarvis_knowledge_read`, `jarvis_retrieval_query`) and concise one-line routing cues:
    - `jarvis_dev_read`: supervisor/relay status, worker jobs, provider quotas, runtime health, recent read-only git/PR summary.
    - `jarvis_repo_read`: search/list/read files in `scope="public"` (JarvisOS repo) or `scope="pack"` (loaded private pack, local routes only).
    - `jarvis_knowledge_read`: structured read-only SQL views over `jarvisos.db` (workspaces, memory records, literature sources/entries, project revisions, calc runs, recent AI jobs, table counts).
    - `jarvis_retrieval_query`: semantic/text retrieval over workspace documents and artifacts.
  - Include `Loaded packs:` (pack IDs only, or `none`; never host paths or private pack contents) and `Route boundary:` (`local` vs `cloud/external`, noting whether private-pack reads are permitted on the active route).
  - Keep every section deterministically character-bounded so the envelope stays compact (`<= 2,400` chars total).
- **Prompt guidance in `context_builder.py::THREAD_AUTHORITY_INSTRUCTIONS` and Hermes turn instructions**:
  - Clarify that while Jarvis chat cannot mutate files, databases, or run solvers directly, it CAN inspect system state, code/docs, Second Brain memory, literature, and calculations through its granted read-only tools.
  - Instruct the assistant to call the appropriate read-only tool before claiming something is unavailable, cite exact paths/line numbers or record IDs returned by tools, and never invent tool outputs.
- **Larger deterministic context & retrieval budget**:
  - Raise `DEFAULT_CONTEXT_BUDGET_CHARS` in `backend/app/modules/ai/context_builder.py` from `32_000` to `48_000` and `MAX_CONTEXT_BLOCKS` from `20` to `32` (preserving existing priority-based drop order and deterministic budget enforcement).
  - Ensure retrieval tool results (`jarvis_retrieval_query`) allow up to `12_000` characters per call (bounded, with explicit `truncated` flag) so multi-source literature and workspace queries are not prematurely clipped.

Out of scope (binding non-goals):
- Adding new write tools or mutating authority.
- Changing cloud IP egress policies for private packs (item 5 of `L20261120222744-75d9`).
- Any UI changes.

## Files likely touched

- `backend/app/modules/ai/thread_service.py`
- `backend/app/modules/ai/context_builder.py`
- `backend/app/modules/ai/retrieval_query.py`
- `backend/tests/test_jarvis_capability_envelope_344.py` (new)
- `backend/tests/test_ai_threads.py` (update only if literal envelope/budget assertions change)
- `backend/tests/test_ai_context_builder.py` (update only if literal budget constant assertions change)

## Design constraints

- Strictly read-only: no new mutation capabilities, no secret or host-path leakage in the capability envelope.
- Core runs without a pack: `Loaded packs: none` when `JARVISOS_PACK_PATH` is unset.
- Deterministic hard bounds: envelope `<= 2,400` chars, context budget `48_000` chars, max blocks `32`, retrieval result `<= 12_000` chars.
- Medium risk (prompt/envelope & budget changes covered by offline tests; does not widen tool allowlist or egress): requires coordinator full-diff review and green CI.

## Acceptance criteria

1. `_jarvis_system_envelope` includes active workspace, loaded pack IDs (or `none`), route boundary (`local` vs `external`), granted read-only tools with routing cues, capabilities, and conversation routes, bounded to `<= 2,400` chars and free of host paths or secrets.
2. `THREAD_AUTHORITY_INSTRUCTIONS` clearly separates read-only inspection capability (available via granted tools) from write/execution authority (still human-governed).
3. `DEFAULT_CONTEXT_BUDGET_CHARS == 48_000` and `MAX_CONTEXT_BLOCKS == 32`, with existing deterministic drop-priority behavior preserved.
4. Existing `test_ai_threads.py`, `test_ai_context_builder.py`, and `test_hermes_runtime.py` pass alongside new unit tests in `test_jarvis_capability_envelope_344.py`.

## Required tests

- Offline unit tests in `backend/tests/test_jarvis_capability_envelope_344.py` verifying envelope contents with/without a fake pack, local vs external route boundary note, size bound (`<= 2,400` chars), absence of host paths/secrets, updated `THREAD_AUTHORITY_INSTRUCTIONS`, and new `48_000` / `32` context budget behavior.
- Existing `test_ai_threads.py`, `test_ai_context_builder.py`, and `test_hermes_runtime.py` pass without weakening security assertions.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, coordinator full-diff review, spec status updated.
