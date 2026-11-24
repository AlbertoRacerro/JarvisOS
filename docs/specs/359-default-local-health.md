# 359 — DEFAULT-LOCAL-HEALTH-1: Default local model startup fallback, Settings/AI health visibility, and Hermes sampling preservation

Status: merged
Depends on: 351, 352

## Goal

When the configured default local model is served via Strata, JarvisOS keeps local inference healthy across startup, reboot, and runtime failure by automatically falling back to a qualified `llama_cpp` model when Strata fails (after verifying the failed Strata port is silent), surfacing the fallback state and retry action in Settings/AI, and ensuring Hermes preserves the active local model entry's configured sampling (`temperature`, `top_p`, `top_k`, `min_p`) and phase-aware reasoning effort (`high` for analysis, `low` for final answer and simple routing).

## Why

Directive `L20261124060857-cd99` item 2 requires verifying default local model health: startup, reboot, fallback when Strata fails (shown in Settings/AI), and confirming Hermes uses the active entry's sampling and high/low reasoning effort. Direct code inspection at `4690ebd4` revealed two concrete gaps:

1. `run_governed_inference` in `backend/app/modules/agents/hermes/supervisor.py` hardcodes `temperature = 0.3` for `task_kind == "general"`, which sets `AIRequest.temperature` to `0.3` and overrides `active_entry.temperature` (e.g. `1.0`) in `LocalLlamaCppAdapter`.
2. When a Strata default entry fails to start (host-ops receipt `outcome != "ok"`, startup timeout, or crash), `StrataRuntimeController.get_state` marks the entry `"failed"`, but nothing automatically falls back to a qualified `llama_cpp` entry or reports an explicit fallback state in Settings/AI.

## Code facts checked at `4690ebd4`

- `backend/app/modules/agents/hermes/supervisor.py::run_governed_inference` computes `temperature = 0.3 if envelope.task_kind == "general" else None` and passes it to `run_ai_task_kwargs`, overriding `active_entry.temperature`.
- `backend/app/modules/ai/providers/local_llamacpp_adapter.py::LocalLlamaCppAdapter` prefers `request.temperature` over `active_entry.temperature` when `request.temperature is not None`, and sets `chat_template_kwargs.reasoning_effort` from `active_entry.reasoning_effort_final` when `request.metadata["reasoning_phase"] == "final"` or `active_entry.reasoning_effort_analysis` otherwise.
- `backend/app/modules/local_ai/runtime/model_registry.py::determine_initial_model` falls back to a qualified `llama_cpp` entry only when `get_spool_path() is None`, not when a host-started Strata unit fails or times out.
- `backend/app/modules/local_ai/runtime/model_registry.py::list_models` returns `{"registry_present", "models", "active_id"}` without fallback metadata.
- `backend/app/modules/local_ai/runtime/host_start.py::StrataRuntimeController` tracks `"stopped"`, `"loading"`, `"ready"`, `"failed"`, and `"requires host start"` states.
- `frontend/src/components/coding/LocalModelPanel.tsx` and `frontend/src/api/llamaRuntime.ts` render registered local models and switch actions in Settings/AI.

## Scope

In scope:
- **Hermes sampling & reasoning effort**: Update `run_governed_inference` (and callers) so that when the active local model entry specifies an explicit `temperature`, Hermes passes `temperature=None` so `LocalLlamaCppAdapter` applies `active_entry.temperature`, `top_p`, `top_k`, and `min_p`; preserve `0.3` only when the active entry leaves `temperature` unset. Verify `reasoning_phase="final"` is passed on the final answer turn and analysis/tool turns use `reasoning_effort_analysis`.
- **Automatic Strata-to-`llama_cpp` fallback**: When the default/selected Strata entry is in `"failed"` state (failed host-ops start receipt or startup timeout) and its loopback port is confirmed silent (single hot model on GPU invariant), automatically activate the first qualified installed `llama_cpp` entry with verified (or non-mismatched) digest, without overwriting the user's preferred default choice on disk so reboot/retry still targets the configured default.
- **Fallback visibility in API & Settings/AI**: Extend `list_models()` (and `LocalModels` in `frontend/src/api/llamaRuntime.ts`) with optional `fallback_active: bool`, `fallback_from_id: str | None`, and `fallback_reason: str | None`. Render a concise Italian fallback notice in `LocalModelPanel.tsx` showing why fallback is active and allowing the user to retry the default model with `"Torna al modello standard"`.
- **Offline tests & browser walk**: Backend offline unit tests for Hermes sampling/effort preservation, Strata failure/timeout fallback, silent-port guard (never start `llama_cpp` while Strata still answers), and `list_models()` fallback payload; frontend `.mjs` test update and browser walk covering Settings/AI.

Out of scope (binding non-goals):
- Automatic Strata tuning, fine-tuning, or changing the default model weights/pack.
- Running two GPU models simultaneously.
- Modifying host systemd units outside existing host-ops policy.

## Files likely touched

- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/local_ai/runtime/model_registry.py`
- `backend/app/modules/local_ai/runtime/host_start.py`
- `backend/app/modules/local_ai/runtime/routes.py`
- `frontend/src/api/llamaRuntime.ts`
- `frontend/src/components/coding/LocalModelPanel.tsx`
- `backend/tests/test_default_local_health_359.py`
- `frontend/tests/` (relevant local model panel `.mjs` test)
- `e2e/` browser walk covering the Settings/AI local model panel

## Design constraints

- Medium risk (runtime fallback + Hermes sampling + Settings/AI UI): requires coordinator full-diff review, CI green, and an `e2e/` browser walk proving Settings/AI visibility.
- Single hot model invariant: never spawn `llama_cpp` while the Strata loopback probe still responds.
- Safe defaults stay safe; all unit tests run 100% offline.

## Acceptance criteria

1. When the active local model entry defines `temperature` (e.g. `1.0`), `run_governed_inference` on a `general` Hermes turn does not override it with `0.3`; `LocalLlamaCppAdapter` sends the entry's `temperature`, `top_p`, `top_k`, `min_p`, and phase-appropriate `reasoning_effort` (`high` for analysis, `low` for final).
2. When the default Strata entry fails to start (receipt error or startup timeout) and its port is silent, JarvisOS automatically falls back to the first qualified installed `llama_cpp` entry without overwriting the stored default choice.
3. If the Strata port still answers, fallback refuses to spawn `llama_cpp` (no double GPU load).
4. `GET /local-ai/models` exposes `fallback_active`, `fallback_from_id`, and `fallback_reason`, and `LocalModelPanel.tsx` displays the fallback notice and retry button in Settings/AI.
5. Existing local model registry, Hermes runtime, and frontend build tests remain green.

## Required tests

- Offline backend tests in `backend/tests/test_default_local_health_359.py` covering criteria 1–4.
- Frontend `.mjs` test + `npm run build` green.
- `e2e/` browser walk proving the Settings/AI local model panel renders cleanly from `/` via visible controls.

## Definition of done

CI green on exact head, coordinator full-diff review clean, `e2e/` browser walk passed with evidence attached, STATUS reconciled.
