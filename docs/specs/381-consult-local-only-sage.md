# 381 — CONSULT-LOCAL-ONLY-SAGE-1: `local_only` judge, Sage batch swap, exchanges and fallback

Status: ready
Depends on: 380

## Goal

Complete step 2 of the trusted-consult design: before any packet leaves for zone A, run the pack/vault `local_only`
terms check and a fresh-context local judge (Inkling) to block anything that states or implies a `local_only` item or
specific design value; route `local_only=true`, post-rewrite-cap, and zone-A-unavailable consults to the larger local
model (Sage) using one batched model swap per turn with measured swap time shown to the user; support ordered
trusted-account fallback and bounded 2-round consult exchanges through the same gate.

## Why

Directive `L20261125114331-2bd1` (consult design v3) §9 step 2: "The `local_only` judge (§3.4) and the Sage path (§6)",
together with §4.3 (2-round exchanges) and §4.4 (ordered trusted-account fallback to Sage) which spec 380 deferred to
step 2. Implementation starts only after spec 380 (parts a and b) is merged and measured.

## Code facts checked at `50bb3018`

- `backend/app/core/vault.py::local_only_items` and `backend/app/core/vault.py::protected_propositions` return
  `tuple[tuple[str, str], ...]` of `(id, text)` pairs from `jarvis_vault.toml` under `JARVISOS_VAULT_PATH`.
- `backend/app/core/packs.py::SearchGuardConfig` holds `groups`, `min_groups`, `exact_combinations`, and
  `protected_dimensions` from `jarvis_pack.toml`.
- `backend/app/modules/consult/config.py::read_consult_config` reads `consult.toml` in `JARVISOS_VAULT_PATH` and
  currently accepts only `{"consult_mode", "brief_sha256", "trusted_accounts"}`.
- `backend/app/modules/consult/gate.py::run_gate` executes deterministic steps 1–3 (`secrets_and_identity`,
  `number_taint`, `files_and_verbatim`) over `question` and `context`, returning `GateResult`.
- `backend/app/modules/consult/service.py::request_consult` currently returns `sage_unavailable` for `local_only=True`
  or when `streak >= MAX_REWRITES`, selects only `next(iter(config.trusted_accounts), None)`, and records every attempt
  in `consult_attempts` (`backend/app/core/consult_schema.py`).
- `backend/app/modules/agents/hermes/consult_tool.py::dispatch_consult_tool` dispatches `jarvis_consult` from
  `backend/app/modules/agents/hermes/supervisor.py` (around line 748) with `turn_id=interaction_id` and
  `turn_tool_outputs`.
- `backend/app/modules/local_ai/runtime/model_registry.py::activate_model` serialises one-hot-model switching across
  `strata` (via `StrataRuntimeController.start`/`stop` in `backend/app/modules/local_ai/runtime/host_start.py`) and
  `llama_cpp` (via `LlamaCppRuntimeOwner.switch_model` in `backend/app/modules/local_ai/runtime/llama_cpp.py`), and
  unconditionally calls `save_model_choice(entry.id)`.
- `backend/app/modules/ai/execution.py::run_ai_task` is the single product AI execution boundary that writes an
  `ai_jobs` row; `backend/app/modules/ai/providers/local_llamacpp_adapter.py::LocalLlamaCppAdapter` routes local
  completions to whichever entry `get_active_model_entry()` returns.

## Scope

1. **Config extension (`consult/config.py`)**:
   - Allow optional `sage_model_id` (a string matching a qualified or default entry id in `local_models.json`) in
     `consult.toml`. Keep unknown-key rejection fail-closed.
   - No API route, tool, or model output writes `consult.toml`.
2. **`local_only` deterministic pre-check and local judge (`consult/judge.py`, wired into `consult/gate.py` / `consult/service.py`)** (§3.4):
   - Runs as gate step 4 after `files_and_verbatim` (and on `edit_consult` as well).
   - **Vault fail-closed rule**: if `JARVISOS_VAULT_PATH` is unset or `load_vault()` fails, zone-A consults refuse
     (`vault_absent` / `gate_error`).
   - **Deterministic pre-check**: check the outbound text (`question + context`) against pack `search_guard` exact
     combinations / `min_groups` and against `vault.local_only_items()` + `vault.protected_propositions()` normalized
     literals. Record hit IDs without echoing secret/proposition text into responses or logs.
   - **Local judge**: invoke the active local model (Inkling) in a **fresh context** through `run_ai_task` (writing an
     `ai_jobs` row, invariant 2, local route only) with the prompt:
     `"Does this text state or allow inferring any of these local-only items, or any specific design value? JSON {hits:[ids], reason}"`.
   - Parse strict JSON `{hits: [str], reason: str}`. Any parse error, timeout, provider error, or non-empty `hits` fails
     closed: parse/provider errors refuse (`judge_error`); non-empty `hits` return `rewrite_required` naming only the
     hit IDs (and count toward `MAX_REWRITES = 2`).
   - Record the judge verdict (`hits`, `reason`, `ai_job_id`) in `consult_attempts.gate_steps_json`.
3. **Sage path with single batched swap per turn (`consult/sage.py`, wired into `consult/service.py`, `consult_tool.py`, `supervisor.py`)** (§6):
   - **Triggers**:
     - `local_only=true`;
     - zone-A gate exceeds `MAX_REWRITES` (2 rewrites);
     - zone A is unavailable (all trusted accounts failed/exhausted, or `consult_mode == "off"` with no zone-A route
       while `sage_model_id` is configured).
   - **Unavailable fallback**: if `sage_model_id` is absent, unknown in `local_models.json`, or not host-startable,
     return `sage_unavailable` (preserving spec 380 behaviour).
   - **Non-persistent swap**: add an optional `persist_choice: bool = True` parameter to `activate_model` in
     `model_registry.py` so a temporary Sage swap (`persist_choice=False`) never overwrites `local_model_choice.json`
     (leaving the maintainer's configured default intact if the backend restarts).
   - **One swap per turn**:
     - During a Hermes turn, Sage consult calls record a `consult_attempts` row (`local_only=1`,
       `account_id="sage_local"`, `model_id=sage_entry.id`, `state="approved"`, `approval="policy_auto"`) and enqueue
       the full local-context question in a per-turn queue, returning `status="queued_for_sage"` with `consult_id` so
       Inkling places `{{consult:<id>}}`.
     - At the end of the Hermes turn (single flush in `supervisor.py`), if the queue is non-empty:
       1. Emit a thread status event (`"Sage sta ragionando in locale"`) visible in the chat UI.
       2. Measure `t0`, switch from the active local model (Inkling) to `sage_model_id` (`persist_choice=False`), wait
          for readiness, record `swap_to_sage_s`.
       3. Answer every queued question sequentially with full local context via `run_ai_task` (each writing an
          `ai_jobs` row, storing `consult_answers`, setting `consult_attempts.state = "sent"` and `ai_job_id`).
       4. In a `finally:` block, always switch back to the original local model (`persist_choice=False`), wait for
          readiness, and record `swap_back_s`.
       5. Store measured swap timings (`swap_to_sage_s`, `swap_back_s`, `total_swap_s`) in `gate_steps_json` and
          emit a completion status event showing the measured swap time in plain Italian.
4. **Ordered trusted-account fallback (§4.4)**:
   - On `auto` send or `ask` approval, iterate through valid `config.trusted_accounts` in order when a transport error
     indicates quota exhaustion or account unavailability (recording each tried account in `gate_steps_json`).
   - If all trusted accounts fail/exhaust, fall back to the Sage queue/runner when `sage_model_id` is configured;
     otherwise mark the attempt `failed` with `zone_a_unavailable`.
5. **Bounded 2-round exchanges (§4.3)**:
   - Extend `jarvis_consult` with optional `reply_to: str | None = None` (referencing a `sent` round-1 consult in the
     same workspace/thread).
   - Round 2 runs the **exact same zone-A gate** (steps 1–4, including `local_only` judge) and mode/approval rules
     (`ask`/`auto`), includes a bounded summary of round 1 in the outbound packet, and records `round=2` and
     `reply_to` in `gate_steps_json`.
   - Any attempt to start round 3 (`reply_to` pointing to a round-2 attempt) refuses with `exchange_limit_reached`.
6. **UI status line**:
   - Surface the `"Sage sta ragionando in locale"` progress indicator and measured swap time on the sidecar chat
     while a Sage batch runs and on the resulting consult block.

Out of scope (binding non-goals):

- Zone-C search/fetch inference judge, mosaic check, and leak benchmark (§5): step 3.
- Objective automatic consult triggers (§8): step 4.
- Any learned router or classifier deciding whether to consult.
- Schema migration unless strictly required (existing `consult_attempts` / `consult_answers` columns suffice).

## Files likely touched

- `backend/app/modules/consult/` (`config.py`, `gate.py`, `service.py`, `routes.py`, new `judge.py`, new `sage.py`)
- `backend/app/modules/local_ai/runtime/model_registry.py` (`persist_choice` flag on `activate_model`)
- `backend/app/modules/agents/hermes/{consult_tool,broker_mcp,supervisor}.py` (`reply_to` argument, per-turn Sage queue flush)
- `frontend/src/components/ai/` (Sage reasoning status indicator and swap-time display), `frontend/tests/`
- `backend/tests/test_consult_381_judge.py`, `backend/tests/test_consult_381_sage.py`, `e2e/` walk update

## Design constraints

- Code never writes consult text or answers; deterministic code only gates, queues, swaps, sends, stores, and renders.
- Every judge/swap/fallback check fails closed; an unreadable vault or invalid judge JSON never passes zone A.
- Raw `local_only` / `protected_propositions` text never appears in error messages, logs, UI labels, or cloud packets.
- One hot local model on the GPU at all times: Sage swap always unloads Inkling before loading Sage and restores
  Inkling in `finally:`.
- Every local judge call and every Sage answer goes through `run_ai_task` and writes an `ai_jobs` row (invariant 2).
- Tests run offline with fake `zephyr` pack, fake vault, fake local models/hostops controller, and fake Relay transport.

## Acceptance criteria

1. A packet that states or implies a vault `local_only` item (via deterministic check or local judge) returns
   `rewrite_required` with hit IDs only (never echoing the secret text) and records the judge verdict and `ai_job_id`
   in `consult_attempts.gate_steps_json`.
2. Unparseable judge JSON, judge timeout, or missing/broken vault fails closed (`judge_error` / `vault_absent`), never
   passes.
3. `local_only=true`, a third attempt after 2 rewrites, or exhausted zone-A accounts enqueue to Sage; two Sage
   questions in one turn perform **exactly one** unload-Inkling → load-Sage → answer-both → unload-Sage → reload-Inkling
   swap, do not overwrite `local_model_choice.json`, restore Inkling on error, and record `swap_to_sage_s` and
   `swap_back_s`.
4. With two trusted accounts configured, quota failure on account 1 automatically tries account 2; if both fail,
   falls back to Sage (or `zone_a_unavailable` when Sage is unconfigured).
5. `reply_to` allows round 2 through the full zone-A gate and refuses round 3 with `exchange_limit_reached`.
6. Sidecar chat displays `"Sage sta ragionando in locale"` during a Sage batch and shows the answered block with
   measured swap time.
7. Lint, mypy, focused tests, frontend build, repo gates, and CI pass on exact head.

## Required tests

- `backend/tests/test_consult_381_judge.py`: deterministic `local_only` pre-check, fresh-context judge via
  `run_ai_task` (`ai_jobs` row created), hit IDs without secret echo, malformed JSON / provider error fail-closed,
  `edit_consult` re-running the judge.
- `backend/tests/test_consult_381_sage.py`: single batched swap for multiple queued questions, `persist_choice=False`,
  `finally:` reload of Inkling on Sage error, swap-time recording, ordered trusted-account fallback to Sage, 2-round
  exchange pass and round-3 `exchange_limit_reached` refusal.
- Frontend `.mjs` test asserting `"Sage sta ragionando in locale"` and swap-time display; `e2e/` walk update.

## Definition of done

Merged with green CI on exact head after coordinator full-diff review, independent second-session full-diff review
(high risk: egress + local model swap), browser walk evidence attached, and a host proof on a temporary data root and
fake vault demonstrating a real local judge call and a real Sage swap with measured swap timings.
