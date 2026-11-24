# 351 — INKLING-SQ10-DEFAULT-1: Per-entry local sampling, reasoning effort, and Strata SQ-10 default readiness

Status: ready
Depends on: 302

## Goal

Extend the local model registry and local OpenAI-compatible inference adapter (built in spec 302) so a qualified Strata-served model entry (such as Inkling 2 Mini SQ-10 at 32k context) can declare its validated sampling parameters (`temperature`, `top_p`, `top_k`, `min_p`), context length (`ctx_size`), and phase-aware chat-template reasoning effort (`high` for analysis/tool-use turns, `low` for final answers and simple routing), and serve as the default local model for Jarvis chat and Hermes with Gemma kept as the one-click fallback.

## Why

Directive `L20261124012919-7d88` (item 1): benchmark measurements showed Inkling 2 Mini on Strata SQ-10 fits in 12 GB VRAM (~48–51 tok/s, 10.9 GB VRAM, 32k context) and outperforms the previous default on engineering tool-use tasks when paired with its recommended sampling (`temperature=1.0`, `top_p=1.0`, `top_k=0`, `min_p=0.0`) and phase-aware reasoning effort (`high` during analysis, `low` for final answer and simple routing). Today `ModelEntry` and `LocalLlamaCppAdapter.complete` only support `thinking: on|off` and `temperature`, with no per-entry `top_p`, `top_k`, `min_p`, or `reasoning_effort` fields.

## Code facts checked at `2bac0b80`

- `backend/app/modules/local_ai/runtime/model_registry.py::ModelEntry` defines frozen registry fields (`id`, `label`, `runtime`, `qualification`, `model_path`, `model_sha256`, `ctx_size`, `n_gpu_layers`, `extra_args`, `thinking`, `unit`, `port`, `served_model`, `host`) without sampling or reasoning-effort fields.
- `backend/app/modules/local_ai/runtime/model_registry.py::read_registry` parses and validates `local_models.json` entries for `llama_cpp` and `strata` runtimes, enforcing exactly one `default` entry.
- `backend/app/modules/local_ai/runtime/model_registry.py::determine_initial_model` selects the stored choice or registry default, falling back to a qualified `llama_cpp` entry when `JARVISOS_HOSTOPS_SPOOL` is unset.
- `backend/app/modules/local_ai/runtime/model_registry.py::activate_model` switches between registered `strata` and `llama_cpp` entries with one hot model at a time and persists the choice.
- `backend/app/modules/ai/providers/local_llamacpp_adapter.py::LocalLlamaCppAdapter.complete` builds the `/chat/completions` JSON payload using `request.temperature` (defaulting to `0`) and `chat_template_kwargs.enable_thinking` from `llama_cpp_runtime_config().thinking`.

## Scope

In scope:
- **Registry schema extension (`model_registry.py`)**:
  - Add optional validated fields to `ModelEntry` and `read_registry`:
    - `temperature: float | None = None` (`0.0 <= v <= 2.0`)
    - `top_p: float | None = None` (`0.0 < v <= 1.0`)
    - `top_k: int | None = None` (`v >= 0`)
    - `min_p: float | None = None` (`0.0 <= v <= 1.0`)
    - `reasoning_effort_analysis: str | None = None` (`"low" | "medium" | "high"`)
    - `reasoning_effort_final: str | None = None` (`"low" | "medium" | "high"`)
  - Validate `ctx_size` when provided on `strata` entries (positive integer, e.g. `32768`), keeping existing registries without these optional fields 100% backward-compatible.
- **Adapter payload wiring (`local_llamacpp_adapter.py`)**:
  - In `LocalLlamaCppAdapter.complete`, read the active `ModelEntry`:
    - Apply `active_entry.temperature` when `request.temperature is None` (preserving explicit caller overrides).
    - Include `top_p`, `top_k`, and `min_p` in the `/chat/completions` payload whenever configured on `active_entry`.
    - Select `reasoning_effort` from `active_entry.reasoning_effort_final` when the request signals a final-answer or simple-routing phase (via `request.metadata.get("reasoning_phase") == "final"` or `request.task_type` in simple-routing tasks), and `active_entry.reasoning_effort_analysis` otherwise; merge into `payload["chat_template_kwargs"]["reasoning_effort"]` without clobbering `enable_thinking`.
- **Host activation steps documented in PR**:
  - Document the host-side unit registration and `local_models.json` entry (Strata default with `temperature: 1.0`, `top_p: 1.0`, `top_k: 0`, `min_p: 0.0`, `ctx_size: 32768`, `reasoning_effort_analysis: "high"`, `reasoning_effort_final: "low"`, and Gemma kept as `qualified` fallback) without hardcoding any host paths in the repository.

Out of scope (binding non-goals):
- No changes to the host-ops executor or direct spawning of Strata from the backend.
- No fine-tuning, LoRA, secondary verifier model, RAM-offload pack, or translation layer (explicitly deferred by `L20261124012919-7d88`).
- No schema migration.

## Files likely touched

- `backend/app/modules/local_ai/runtime/model_registry.py`
- `backend/app/modules/ai/providers/local_llamacpp_adapter.py`
- `backend/tests/test_inkling_sq10_default_351.py` (new)
- `backend/tests/test_local_model_registry.py` (if needed for compatibility checks)

## Design constraints

- Backward compatible: registries omitting the new optional fields behave identically to today.
- Invalid sampling values (out-of-range floats, negative `top_k`, unknown reasoning effort string, boolean values) are rejected with 503 on `read_registry` just like other malformed registry fields.
- All endpoints stay loopback; no new external dependencies or secrets.
- Medium risk: requires coordinator full-diff review and green CI; switching the live host default follows the regression gate (item 6 of `L20261124012919-7d88`).

## Acceptance criteria

1. `read_registry` accepts a `strata` default entry with `temperature=1.0`, `top_p=1.0`, `top_k=0`, `min_p=0.0`, `ctx_size=32768`, `reasoning_effort_analysis="high"`, `reasoning_effort_final="low"` alongside a `qualified` `llama_cpp` fallback, and rejects out-of-range or malformed values with 503.
2. `LocalLlamaCppAdapter.complete` emits the entry's sampling parameters in `/chat/completions` when active, preserves explicit `request.temperature`, and omits unconfigured sampling keys for legacy entries.
3. `LocalLlamaCppAdapter.complete` sets `chat_template_kwargs["reasoning_effort"]` to `"high"` on analysis turns and `"low"` on final-answer / simple-routing turns without removing `enable_thinking`.
4. Existing `strata`/`llama_cpp` switching and fallback tests remain green.

## Required tests

- Offline unit tests in `backend/tests/test_inkling_sq10_default_351.py` covering criteria 1–3 with fake HTTP clients and temporary registry files.
- Existing local model registry and `LocalLlamaCppAdapter` test suites remain green.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, coordinator full-diff review, spec status updated.
