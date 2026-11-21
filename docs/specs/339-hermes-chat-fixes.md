# 339 — HERMES-CHAT-FIXES-1: Fix Hermes chat truncation visibility, stale self-status, repetition, and per-reply surface notice

Status: ready
Depends on: none

## Goal

Hermes chat answers carry a truthful turn-time capability status, propagate truncation (`finish_reason == "length"`) visibly with a continuation affordance instead of silently cutting off, use non-zero sampling temperature on local llama.cpp chat turns so repeated questions do not echo word for word, and stop stamping `"No engineering workspace surface is active."` onto every reply outside design pages.

## Why

Directive `L20261120235337-0377` recorded four defects observed live after Hermes v0.23.0 came back: answers cut off without indication, the capability envelope telling Hermes `hermes: unavailable` while Hermes itself was running, verbatim repeated answers when the user asked again, and the inactive-surface notice glued to every message outside design screens. Fixing these before adding development read tools keeps every chat turn trustworthy.

## Code facts checked at `ce29b08c`

- `backend/app/modules/agents/hermes/supervisor.py::run_governed_inference` returns `{"status": "success", "text": outcome.response.text}` without forwarding `outcome.response.finish_reason`.
- `backend/app/modules/agents/hermes/worker_shim.py::Worker._handler` hardcodes `"finish_reason": "tool_calls" if "tool_calls" in message else "stop"` for both streaming and non-streaming relay replies, hiding `"length"` truncation from Hermes.
- `backend/app/modules/agents/hermes/worker_shim.py::Worker.run_turn` returns `final_response` and `completed` without reporting whether the final relay response hit a length cap.
- `backend/app/modules/ai/thread_service.py::_submit_hermes_interaction` rejects `relay_flow.get("state") != "complete"` (raising `AIThreadConflictError` when a final relay flow is `partial_terminal`) and receives `context_blocks` built before the lazy Hermes worker has started.
- `backend/app/modules/ai/thread_service.py::_jarvis_system_envelope` calls `capability_reads` before `_submit_hermes_interaction` starts the Hermes worker, while `backend/app/modules/engineering/operator_service.py::capability_reads` reports `hermes: unavailable (HERMES_WORKER_STOPPED)` whenever `worker_pid` is `None`.
- `backend/app/modules/ai/providers/local_llamacpp_adapter.py::LocalLlamaCppAdapter.complete` hardcodes `"temperature": 0` in the OpenAI-compatible payload, and `backend/app/modules/ai/contracts.py::AIRequest` has no `temperature` field.
- `backend/app/modules/workspace_actions/service.py::surface_brief` returns `summary="No engineering workspace surface is active."` when `surface == "none"`, and `backend/app/modules/ai/thread_service.py::_save_surface_brief` stores `brief.summary` on every interaction, which `frontend/src/components/ai/useJarvisSidecar.tsx` renders on every reply bubble even though the page header already shows `"No workspace context on this page"`.

## Scope

In scope:
- **Truncation propagation and continuation**:
  - `run_governed_inference` includes `"finish_reason": outcome.response.finish_reason` when present.
  - `Worker._handler` forwards `"length"` as `finish_reason` when the relayed result has `finish_reason == "length"` and no `tool_calls`.
  - `_submit_hermes_interaction` accepts `relay_flow.get("state") in {"complete", "partial_terminal"}` so a length-capped Hermes answer is captured cleanly with `flow_state="partial_terminal"` rather than failing capture.
  - In `frontend/src/components/ai/useJarvisSidecar.tsx`, when `interaction.flow_state === "partial_terminal"`, render a visible truncation notice alongside the existing `"Continue answer"` button so the user knows the answer was cut off and can continue.
  - Ensure Hermes `general` chat turns use at least a sensible default output budget (binding `max_output_tokens`, e.g. 2048) when Hermes does not pass an explicit `max_tokens`.
- **Turn-time Hermes status in capability envelope**:
  - In `_submit_hermes_interaction`, refresh the `jarvis:system-envelope` block in `agent_blocks` after `_ensure_hermes_thread_session` has started the Hermes worker, so Hermes never sees a stale `hermes: unavailable (HERMES_WORKER_STOPPED)` while it is actively serving the turn.
- **Sampling temperature on local llama.cpp chat turns**:
  - Add optional `temperature: float | None = Field(default=None, ge=0.0, le=2.0)` to `AIRequest` and `run_ai_task` (default `None` keeps `0` for deterministic/classification callers).
  - Pass a modest non-zero chat temperature (e.g. `0.3`) on Hermes `general` relay turns (`supervisor.py::run_governed_inference` / `run_ai_task_kwargs`), and honor `request.temperature if request.temperature is not None else 0` in `LocalLlamaCppAdapter.complete`.
- **Stop repeating inactive-surface notice on every reply**:
  - In `_save_surface_brief`, store `None` (or omit `summary` from per-reply display) when `brief.surface == "none"`, while keeping `brief_json` and `digest` intact, so only active engineering surfaces (`process`, `bluecad`, `studio`) show a per-reply surface summary; the sidecar header already displays page context once.

Out of scope (binding non-goals):
- Development read tools for Hermes (`L20261120222744-75d9`, separate spec).
- Local vs free cloud model quality comparison (directive item 5, after read tools land).
- Changes to `ollama_generate.py`'s `temperature == 0.0` policy guard for Ollama classification/responder paths.
- Schema migrations (no new tables or columns).

## Files likely touched

- `backend/app/modules/ai/contracts.py`
- `backend/app/modules/ai/execution.py`
- `backend/app/modules/ai/agent_contracts.py`
- `backend/app/modules/ai/providers/local_llamacpp_adapter.py`
- `backend/app/modules/ai/thread_service.py`
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/agents/hermes/worker_shim.py`
- `frontend/src/components/ai/useJarvisSidecar.tsx`
- `backend/tests/test_hermes_146.py`, `backend/tests/test_ai_threads.py`, `backend/tests/test_local_llamacpp_adapter.py`
- `frontend/tests/` (matching `.mjs` assertions)

## Design constraints

- Deterministic callers and classification routes keep `temperature = 0` by default (`AIRequest.temperature is None`).
- No schema migration; `ai_thread_surface_context.summary` is already nullable or can store `""`/be mapped to `None` on read when `brief.surface == "none"` (verify `NOT NULL` constraint in `schema.py` before writing `None`).
- Never relax `route_class="auto"` or local-only Hermes route enforcement.
- UI change requires an `e2e/` browser walk before asking the maintainer to look.

## Acceptance criteria

1. When a relayed Hermes `general` inference returns `finish_reason == "length"` (flow state `partial_terminal`), `run_governed_inference` returns `finish_reason: "length"`, `Worker._handler` forwards `"length"` instead of `"stop"`, `_submit_hermes_interaction` captures the answer cleanly without raising `AIThreadConflictError`, and `useJarvisSidecar.tsx` renders a visible truncation notice and `"Continue answer"` control.
2. When a Hermes turn starts from an idle/stopped worker, the `jarvis:system-envelope` block delivered to `worker.turn(...)` is refreshed after `_ensure_hermes_thread_session` starts the worker and reports `hermes: available` rather than `hermes: unavailable (HERMES_WORKER_STOPPED)`.
3. `LocalLlamaCppAdapter.complete` sends `temperature: 0` when `request.temperature is None`, and sends `request.temperature` when set; Hermes `general` relay calls pass a non-zero sampling temperature (`0.3`).
4. For a chat interaction submitted outside engineering surfaces (`surface == "none"`), the returned interaction has `surface_summary is None` (no `"No engineering workspace surface is active."` stamped on each reply), while active `process`/`bluecad`/`studio` turns still carry their `surface_summary`.

## Required tests

- Backend unit tests covering criteria 1–4 offline with fake providers/workers.
- Frontend `.mjs` test update verifying the truncation notice and `"Continue answer"` rendering on `partial_terminal` and `frontend` build green.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, `e2e/` browser walk covering the chat sidecar surface, spec status updated.
