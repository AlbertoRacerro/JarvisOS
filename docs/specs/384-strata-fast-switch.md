# 384 — STRATA-FAST-SWITCH-1: Measure and minimise local Strata model switch latency

Status: ready
Depends on: —

## Goal

Measure the full phase-by-phase latency breakdown of switching between two Strata-served local models (default local
model ⇄ local specialist model) on the main host, and implement every measured, safe optimisation that reduces switch
latency toward the ≤ 30 s per-direction target (or documents the measured engine floor and why), while preserving the
one-hot-model stop-first invariant, default serving settings, and maintainer-first priority.

## Why

Directive `L20261126142319-0e3b`: the spec-381 host proof measured `swap_to_sage_s = 209.3 s` and `swap_back_s = 68.5 s`
(`total_swap_s = 277.8 s`), which is too slow for interactive consults routed to the local specialist model. Making the
switch as cheap as possible on the main host (without modifying the Strata engine binary itself) is required before
heavy local-consult use.

## Code facts checked at `ed5803e5`

- `backend/app/modules/consult/sage.py::flush_sage_queue` performs one batched swap per Hermes turn after the turn
  finishes (`activate_model(..., persist_choice=False)` + `_wait_strata_ready(...)`, then restores the original model in
  `finally:`) and records `swap_to_sage_s`, `swap_back_s`, and `total_swap_s` in `consult_attempts.gate_steps_json`.
- `backend/app/modules/consult/sage.py::enqueue_sage_consult` queues a local specialist consult during a Hermes turn as
  soon as the local-only judge or fallback path routes to the local specialist, without starting any prewarm or overlap.
- `backend/app/modules/consult/sage.py::_wait_strata_ready` polls `StrataRuntimeController.get_state` every
  `STRATA_READY_POLL_S = 2.0` seconds.
- `backend/app/modules/local_ai/runtime/host_start.py::StrataRuntimeController.stop` writes a `service.control` `stop`
  request and polls `check_receipt` + `probe_strata_readiness` every `0.5` seconds;
  `backend/app/modules/local_ai/runtime/host_start.py::StrataRuntimeController.start` writes `service.control` `start`.
- `backend/app/modules/local_ai/runtime/model_registry.py::activate_model` enforces the one-hot-model stop-first
  invariant across `strata` and `llama_cpp` entries.
- `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor.turn` calls `flush_sage_queue(interaction_id)` only
  after the full Hermes turn completes.
- `scripts/benchmarks/agent_eval/runner.py::wait_for_model_idle` checks loopback `/slots` (`is_processing`) to yield to
  in-flight maintainer turns (spec 375).

## Scope

Implement in strict measured order; keep each optimisation only when measurement shows it helps without degrading
default-model decode speed:

1. **Repeatable switch-latency breakdown benchmark (`scripts/benchmarks/strata_switch_bench.py`)**:
   - Standard-library, loopback-only (refuses port 8000, respects `vault_guard`), offline-testable script that measures
     both directions (`default -> specialist` and `specialist -> default`) across `N` runs (default 3) on an idle host
     (checking `/slots` first).
   - Breaks down each switch into explicit phases:
     - `stop_old_s` (request to hostops `ok` receipt + port silent);
     - `start_dispatch_s` (start request to hostops `ok` receipt);
     - `model_ready_s` (process start to `/v1/models` reporting the served model loaded);
     - `first_token_s` (1-token loopback `/v1/chat/completions` probe after `/v1/models` ready);
     - optional Strata log/API-monitor sub-phase parsing (`weight_read_s`, `vram_upload_s`, `engine_init_s`) when log
       lines are supplied via `--strata-log` or loopback `/api-monitor` is enabled;
     - host memory snapshot (`MemTotal`, `MemAvailable`, page-cache residency via `mincore` where file paths are
       accessible) and optional short decode-speed check (`decode_tok_s`) before and after.
   - Emits machine-readable `result.json` plus a plain-Italian summary table.

2. **Safe bounded page-cache warm helper (`backend/app/modules/local_ai/runtime/page_cache.py`)**:
   - Deterministic Linux helper using `os.posix_fadvise(..., POSIX_FADV_WILLNEED)` (never `mlock`ing beyond available
     headroom; refuses to prewarm if `MemAvailable` would drop below a configurable safety floor so the default model's
     host-RAM experts are never starved).
   - Exposed as an optional step in `strata_switch_bench.py` (cold vs warm comparison) and triggered non-blockingly when
     a specialist consult is queued (`enqueue_sage_consult`) if configured and RAM headroom permits.

3. **Strata keep-alive / loopback unload-load probe and tighter polling (`host_start.py`, `sage.py`)**:
   - Probe whether the installed Strata server exposes loopback `/unload` / `/load` (or `/api-monitor`) controls; if
     available and authorised on loopback, support an opt-in fast switch path in `StrataRuntimeController` that still
     enforces stop/unload-first (one hot model) and falls back cleanly to `service.control` `stop`/`start` on 404/403 or
     connection error. If Strata exposes no such API on the installed build, record that fact in the benchmark report
     and do not modify the Strata binary.
   - Tighten polling intervals (`StrataRuntimeController.stop` `0.5 s -> 0.1 s`, `sage._wait_strata_ready`
     `2.0 s -> 0.25 s`, configurable for tests) so polling overhead is negligible.

4. **Overlap, batching, and maintainer-first guard (`sage.py`, `supervisor.py`)**:
   - **Maintainer-first guard**: before `flush_sage_queue` stops the default Strata model, check its loopback `/slots`
     (`is_processing`). Never stop the default model while another maintainer turn is in flight on it; wait with a
     bounded timeout until idle and record `wait_idle_s` in `consult_attempts.gate_steps_json`.
   - **Early overlap**: when `enqueue_sage_consult` queues a `local_only` consult, immediately kick off the bounded
     non-blocking page-cache prewarm (if enabled and safe) and let `HermesSupervisor` avoid extra redundant default-model
     steps after a `local_only` consult is queued when no further non-specialist work remains, while preserving per-turn
     batching of multiple specialist questions in a single swap.
   - Record full per-phase timing breakdown (`wait_idle_s`, `stop_old_s`, `start_ready_s`, `swap_to_sage_s`,
     `swap_back_s`, `total_swap_s`, `switch_mode`) in `consult_attempts.gate_steps_json`.

5. **Host measurement and honest reporting**:
   - Run `strata_switch_bench.py` on the idle main host (3 runs each direction, before and after kept optimisations),
     measure default-model decode tok/s before and after, and record the measured table (or measured engine floor if
     ≤ 30 s is blocked by the Strata binary itself) in the proof evidence.

Out of scope (binding non-goals):

- Modifying or rebuilding the Strata engine binary itself (requires a separate maintainer decision).
- Running the specialist model on a second PC (deferred as a later option only).
- Violating the one-hot-model stop-first invariant or changing default model serving settings (8.6 GB experts, 32k
  context) if decode speed regresses.

## Files likely touched

- `scripts/benchmarks/strata_switch_bench.py` (new)
- `backend/app/modules/local_ai/runtime/page_cache.py` (new)
- `backend/app/modules/local_ai/runtime/host_start.py`
- `backend/app/modules/consult/sage.py`
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/tests/test_strata_fast_switch_384.py` (new)

## Design constraints

- Main host only; one-hot-model stop-first invariant preserved at all times.
- Default serving settings unchanged unless host measurement proves zero decode-speed regression.
- Maintainer-first: never stop or swap away from the default model while a maintainer turn is processing on `/slots`.
- Offline tests only (mocked `/slots`, `/v1/models`, `/unload`, `posix_fadvise`, and hostops spool); no new third-party
  dependencies.
- High risk (local model runtime + hostops lifecycle): requires coordinator full-diff review, independent
  second-session review, and real host proof on an idle machine.

## Acceptance criteria

1. `scripts/benchmarks/strata_switch_bench.py` runs offline unit tests cleanly and on the live idle host produces a
   3-run both-directions breakdown JSON and plain-Italian table (`stop_old_s`, `start_dispatch_s`, `model_ready_s`,
   `first_token_s`, host RAM, decode tok/s).
2. `page_cache.py` respects the `MemAvailable` safety floor (refuses prewarm when headroom is insufficient) and never
   degrades default-model decode speed.
3. `StrataRuntimeController` supports tighter polling and safe opt-in loopback `/unload`/`/load` probing with fail-closed
   fallback to `service.control` `stop`/`start`.
4. `flush_sage_queue` waits for `/slots` idle before stopping the default model (maintainer-first), preserves per-turn
   batching, triggers early prewarm on enqueue, and logs phase timings in `gate_steps_json`.
5. Lint, mypy, focused tests, repo gates, CI on exact head, independent second-session review, and real host proof pass.

## Required tests

- `backend/tests/test_strata_fast_switch_384.py`:
  - `strata_switch_bench.py` phase-breakdown math, Strata log parser, loopback/port-8000 guard, and Italian table output;
  - `page_cache.py` headroom floor refusal and bounded `posix_fadvise` call;
  - `StrataRuntimeController` tight polling and loopback `/unload` fallback to `service.control`;
  - `flush_sage_queue` `/slots` busy wait (maintainer-first), batched multi-item swap, and phase-timing `gate_steps_json`
    recording.

## Definition of done

Merged with green CI on exact head after coordinator full-diff review, independent second-session review, and real idle
host measurement reporting before/after seconds per switch (both directions, 3 runs each) and default-model decode speed.
