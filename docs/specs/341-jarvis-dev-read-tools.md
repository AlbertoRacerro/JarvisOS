# 341 — JARVIS-DEV-READ-TOOLS-1: Read-only development, supervisor, quota, and runtime tools for Jarvis chat

Status: merged
Depends on: none

## Goal

Jarvis chat (through the existing Hermes MCP broker) can read the live development and runtime state of JarvisOS via a single bounded read-only tool `jarvis_dev_read`: the spec registry and individual spec texts, recent merged/squashed work with spec numbers, the coordinator's Up next plan, supervisor status in plain words, active directives and recent ledger notes, provider quotas and free pools, and local/Hermes runtime health. The capability envelope and thread authority instructions advertise this tool so Jarvis checks live state before claiming it has no access.

## Why

Directive `L20261120222744-75d9` (item 1, moved forward from step 3 of `L20261120133717-6f0b`): when asked in chat what was merged recently, what status a spec has, why the supervisor is stopped, or how much quota remains, Jarvis answered that it had no access to logs or GitHub. This amends the "no repository authority" wording of specs 200/260 for **read-only** access only; write, worker, and deployment authority stay unchanged.

## Code facts checked at `6b91f10e`

- `backend/app/modules/agents/hermes/broker_mcp.py::_reply` exposes the fixed `_TOOLS` list over stdio JSON-RPC and forwards `tools/call` requests to the loopback Jarvis broker (`/v1/jarvis/tool`).
- `backend/app/modules/agents/hermes/supervisor.py::dispatch_tool` validates the live grant (`CapabilityGrantRef`), checks `PRODUCTION_CAPABILITY_REGISTRY`, and dispatches each admitted capability (`jarvis.retrieval_query`, `jarvis.bench_read`, etc.).
- `backend/app/modules/ai/thread_service.py::_install_hermes_retrieval_grant` issues per-turn `jarvis.retrieval_query` and `jarvis.decide` grants on every Hermes chat turn regardless of active workspace surface.
- `backend/app/modules/ai/thread_service.py::_jarvis_system_envelope` builds the system envelope attached to chat turns.
- `backend/app/modules/coding/control_room.py::parse_registry`, `backend/app/modules/coding/control_room.py::plan_projection`, and `backend/app/modules/coding/control_room.py::recent_work_items` already parse `docs/specs/STATUS.md`, `<control_root>/plan.json`, and git merge/squash subjects with spec numbers.
- `backend/app/modules/coding/relay_monitor.py::snapshot` already produces a read-only semantic snapshot of the continuity supervisor (running, waiting, held and why, current mission and task).
- `backend/app/modules/coding/provider_quota.py::read_all` and `backend/app/modules/coding/provider_quota.py::normalize` already read and normalize provider quotas without exposing credentials.

## Scope

In scope:
- **New read-only MCP tool `jarvis_dev_read`** in `backend/app/modules/agents/hermes/broker_mcp.py`, allowlisted in `backend/app/modules/agents/hermes/supervisor.py` (`HERMES_TOOL_ALLOWLIST`, `_TOOL_CAPABILITIES`, `_HERMES_DEV_READ_CAPABILITY` with `action_class="READ"`), and dispatched in `dispatch_tool` to a dedicated module `backend/app/modules/agents/hermes/dev_read_tool.py`:
  - `op="specs"`: reads `docs/specs/STATUS.md` via `parse_registry`, optionally filtered by `spec_id` or `status`, bounded (`limit` 1..50, default 20).
  - `op="spec_text"`: reads `docs/specs/<spec_id>-*.md` where `spec_id` matches `^[0-9]{1,4}[a-z]?$` and the resolved path stays strictly inside `docs/specs/` (no path traversal, no symlinks escaping the repo), bounded to 16,000 characters with a `truncated` flag.
  - `op="recent_work"`: returns recent merged/squashed PR items with spec numbers via `control_room.py` (`limit` 1..30, default 15).
  - `op="plan"`: returns the coordinator's Up next plan from `<control_root>/plan.json` via `plan_projection`.
  - `op="supervisor"`: returns the read-only supervisor snapshot via `relay_monitor.snapshot()` (state, reason, current mission/task, scrubbed of host paths/secrets).
  - `op="directives"`: returns active directives and recent ledger notes (summaries only, bounded `limit` 1..30, default 15) from `.agent-relay/devctx/ledger.jsonl` without host filesystem paths or private mission brief content.
  - `op="quotas"`: returns normalized provider quotas (cached `refresh=False` by default) and a summary of admitted free pools (`id`, `label`, `tier`, `status` only — never API keys, env var values, or tokens).
  - `op="runtime_health"`: returns local AI runtime status, Hermes supervisor status, and repository head/version summary.
- **Per-turn grant and prompt guidance**:
  - Issue a `jarvis.dev_read` grant alongside `jarvis.retrieval_query` in `thread_service.py::_install_hermes_retrieval_grant` so every Hermes chat turn can call `jarvis_dev_read`.
  - Advertise `jarvis_dev_read` (with its `grant_id` and supported `op` values) in the turn prompt and update `context_builder.py::THREAD_AUTHORITY_INSTRUCTIONS` so the model uses available read-only tools before stating it has no access.

Out of scope (binding non-goals):
- General repository code search / arbitrary file read and private pack repository read (next slice, item 2 of `L20261120222744-75d9`).
- Second-brain / SQLite read views (item 3 of `L20261120222744-75d9`).
- Any write, git mutation, supervisor control, worker launch, or deployment authority.
- Any UI changes (backend/Hermes broker only; chat answers verified end-to-end).

## Files likely touched

- `backend/app/modules/agents/hermes/dev_read_tool.py` (new)
- `backend/app/modules/agents/hermes/broker_mcp.py`
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/ai/thread_service.py`
- `backend/app/modules/ai/context_builder.py`
- `backend/tests/test_hermes_dev_read_341.py` (new)
- `backend/tests/test_hermes_runtime.py` (if allowlist/tool count assertions need updating)

## Design constraints

- Strictly read-only: never writes files, never executes git mutations, never triggers provider refreshes that launch model turns, never calls paid providers.
- Secret-safe and path-safe: never returns OAuth tokens, API keys, credential paths, private mission brief content, or raw host home paths; refuses invalid `spec_id` or path traversal.
- Reuses existing owners (`control_room.py`, `relay_monitor.py`, `provider_quota.py`) rather than duplicating parsers.
- Bounded payloads: every `op` caps list length and text length deterministically.
- High risk (extends Hermes tool allowlist and reads supervisor/control/quota state): requires independent second-session full-diff review.

## Acceptance criteria

1. `jarvis_dev_read` is listed in `broker_mcp.py::_TOOLS`, allowlisted in `HERMES_TOOL_ALLOWLIST`, registered as `jarvis.dev_read` (`action_class="READ"`), and granted on every Hermes chat turn.
2. Calling `dispatch_tool` with a valid `jarvis.dev_read` grant succeeds for all 8 operations (`specs`, `spec_text`, `recent_work`, `plan`, `supervisor`, `directives`, `quotas`, `runtime_health`) against fixture repos/control dirs and refuses invalid `op`, invalid `spec_id` (including `../` traversal), or missing/mismatched grant.
3. `quotas` and `directives` never leak tokens, API keys, or host home paths in their returned dicts.
4. Thread system instructions / prompt guidance list `jarvis_dev_read` and instruct the assistant to use available read-only tools before claiming it lacks access.

## Required tests

- Offline unit tests in `backend/tests/test_hermes_dev_read_341.py` covering all 8 `op` values with temporary fixture `STATUS.md`, spec markdown, git repo, `plan.json`, supervisor snapshot, `ledger.jsonl`, `temp-pools.json`, and fake quota reader; path-traversal refusal; grant enforcement; and secret-leak assertions.
- Existing `test_hermes_runtime.py` and `test_ai_threads.py` pass without weakening assertions.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, independent second-session full-diff review, spec status updated.
