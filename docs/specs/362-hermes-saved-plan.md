# 362 — HERMES-SAVED-PLAN-1: Saved turn plan, step records, and live progress in Jarvis chat

Status: ready
Depends on: 352

## Goal

For multi-step questions, Hermes writes and updates a bounded turn plan (`steps` with statuses and compact intermediate `findings` carrying source references) through a dedicated Jarvis broker tool. The harness persists each plan revision and tool step as structured events for the interaction, re-injects the current saved plan and findings into every model step so the work survives small context and tool-output shrinking, and exposes the live plan and tool steps in the sidecar chat while the turn runs (and inspectable once finished).

## Why

Directives `L20261124095650-7ca4` (item 2, top priority after the chat bench) and `L20261124094804-a929` (item 5): local models take 1–3 minutes on multi-step engineering questions and lose track of intermediate values when old tool outputs are shrunk to protect the KV cache. A saved, re-injected plan with compact step records keeps multi-step work coherent and lets the engineer watch the plan and tool calls live in the chat.

## Code facts checked at `9d2bddcb`

- `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor.turn` resets per-turn counters and sends the turn frame to the worker.
- `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor._handle_tool` dispatches broker tool calls and logs `hermes.tool_started` and `hermes.tool_result` events with `interaction_id`.
- `backend/app/modules/agents/hermes/supervisor.py::_assemble_turn_prompt` injects `ENGINEERING_METHODOLOGY_RULES` and bounded context blocks into the turn prompt.
- `backend/app/modules/agents/hermes/worker_shim.py::Worker._handler` handles `/v1/chat/completions` and `/v1/jarvis/tool`, calls `shrink_tool_history`, and forwards inference requests to the supervisor.
- `backend/app/modules/agents/hermes/broker_mcp.py::main` serves the MCP tool schemas to the Hermes worker.
- `backend/app/modules/ai/thread_service.py::interaction_activity` derives a single activity string for a running interaction from the latest `hermes.tool_started`/`hermes.tool_result` event.
- `backend/app/modules/ai/thread_models.py::AIThreadInteractionRead` is the interaction read contract returned by thread endpoints and consumed by the frontend.
- `frontend/src/components/ai/useJarvisSidecar.tsx` renders `interaction.activity` while a turn is running.

## Scope

In scope:
- **Broker tool `jarvis_turn_plan`** (`broker_mcp.py`, `supervisor.py`):
  - Bounded per-turn operations: `set_plan` (up to 10 steps, each `{id, title, status}` with status in `pending|in_progress|done|blocked`), `update_step` (`id`, `status`, optional short `note`), `record_finding` (up to 24 compact records `{key, value, unit?, source_ref}`), and `read_plan`.
  - Persists each validated plan snapshot as a `hermes.turn_plan` event bound to `interaction_id` and `workspace_id` (no SQLite schema migration required).
  - Exempt from the research-call budget and never emits secrets or private pack paths.
- **Per-step plan re-injection** (`supervisor.py`, `worker_shim.py`):
  - Whenever a saved plan exists for the active turn, inject a compact, bounded summary of the current plan (steps + statuses + recorded findings) into the messages sent on each `/v1/chat/completions` step (after `shrink_tool_history`), so the model always sees its current step and recorded intermediate numbers without keeping raw tool dumps in context.
  - Extend `ENGINEERING_METHODOLOGY_RULES` in `_assemble_turn_prompt` with one concise rule instructing multi-step questions to create a plan with `jarvis_turn_plan` and record intermediate findings with source refs.
- **Interaction read projection** (`thread_models.py`, `thread_service.py`, regenerated frontend contracts):
  - Add optional `turn_plan` (steps + findings) and `live_steps` (ordered bounded list of tool steps `{call_id, tool_name, label, status}` from `hermes.tool_started`/`hermes.tool_result` events for that `interaction_id`) to `AIThreadInteractionRead`.
  - Keep existing `activity` field intact for backward compatibility.
- **Sidecar live steps UI** (`frontend/src/components/ai/useJarvisSidecar.tsx` and styles/tests):
  - While an interaction is running, render the live plan checklist (when present) and the recent tool steps below the working indicator; once finished, keep them collapsible under a compact summary toggle so the chat stays readable.
  - Add a frontend static `.mjs` test (registered in `frontend/package.json`) and an `e2e/` browser walk check covering live/completed plan and tool steps visibility.

Out of scope (binding non-goals):
- No SQLite schema migration (use existing `events` table with `hermes.turn_plan`, `hermes.tool_started`, `hermes.tool_result`).
- No automatic number-provenance checker or source-link rendering on final prose (separate follow-up slice for `L20261124094804-a929` item 2).
- No research sub-agent, two-run comparison, or context-length (`64k`/`128k`) Strata change.
- No changes to tool permissions, egress policy, or sandbox boundaries.

## Files likely touched

- `backend/app/modules/agents/hermes/broker_mcp.py`
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/agents/hermes/worker_shim.py`
- `backend/app/modules/ai/thread_models.py`
- `backend/app/modules/ai/thread_service.py`
- `frontend/src/api/generated/` (regenerated via `scripts/generate_frontend_contracts.py`)
- `frontend/src/api/threads.ts`
- `frontend/src/components/ai/useJarvisSidecar.tsx`
- `frontend/src/components/ai/JarvisSidecar.css`
- `backend/tests/test_hermes_saved_plan_362.py` (new)
- `frontend/tests/` static test + `frontend/package.json`
- `e2e/` browser walk covering the sidecar live plan/steps surface

## Design constraints

- Additive, backward-compatible API contract: `turn_plan` and `live_steps` default to `None` / `[]` when absent.
- Strict bounds: max 10 plan steps (title <= 160 chars, note <= 200 chars), max 24 findings (each <= 240 chars total), max 30 projected `live_steps`, re-injected plan summary <= 2400 chars.
- Identical-call blocking must not block legitimate `update_step` or `record_finding` calls with new payloads; `read_plan` always returns the latest state.
- Medium risk (feature code + UI, no egress/migration/auth change): coordinator severe review, CI green on exact head, and passing `e2e/` browser walk before merge.

## Acceptance criteria

1. `jarvis_turn_plan` validates bounds and enums, persists `hermes.turn_plan` events for the active `interaction_id`, updates steps and findings deterministically, and does not consume the research-call budget.
2. When a plan exists for the active turn, each subsequent model step in `Worker._handler` receives the compact saved plan and findings even after `shrink_tool_history` compacts earlier tool outputs.
3. `AIThreadInteractionRead` projects `turn_plan` and ordered `live_steps` from persisted events for both in-flight and completed interactions without breaking existing thread responses.
4. The sidecar UI renders the live plan checklist and tool steps during a running turn and keeps them inspectable after completion; static frontend test and `e2e/` browser walk pass.
5. Existing Hermes runtime, harness (`test_hermes_inkling_harness_352.py`), and thread tests remain green; frontend contracts check passes.

## Required tests

- Offline backend tests in `backend/tests/test_hermes_saved_plan_362.py` covering criteria 1–3 (fake inference, no network).
- Frontend static `.mjs` test covering live plan and tool steps rendering.
- `e2e/` browser walk proving visible plan and tool steps in the sidecar from `/` using visible controls only.

## Definition of done

Test gate green (see `AGENTS.md`), browser walk evidence recorded, coordinator severe review complete, CI green on exact head, STATUS row reconciled.
