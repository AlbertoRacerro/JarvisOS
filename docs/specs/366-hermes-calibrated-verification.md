# 366 — HERMES-CALIBRATED-VERIFICATION-1: Calibrated answers, deterministic verification pass, and mid-turn context compaction

Status: ready
Depends on: 362

## Goal

Strengthen the Jarvis Hermes chat harness so local models give calibrated engineering estimates instead of refusing when a source cannot be confirmed, run a fast deterministic offline verification pass over units, order-of-magnitude discrepancies against tool outputs, and physical bounds before persisting an answer, compact long mid-turn message histories without losing the saved plan, and see clearer tool descriptions that separate computation from lookup.

## Why

Directive `L20261124094804-a929` (items 3 and 4) and `L20261124095650-7ca4` (items 4 and 6): agent-bench testing showed that local and hosted models inside Jarvis abstained on partially sourced questions instead of giving calibrated estimates, risked silent order-of-magnitude or unit slips when summarizing tool outputs, and struggled with tool selection and long mid-turn transcripts. Directive `L20261124095650-7ca4` item 4 explicitly specifies that runtime verification must be deterministic (never calling a frontier model at runtime).

## Code facts checked at `5060a233`

- `backend/app/modules/agents/hermes/supervisor.py::_assemble_turn_prompt` appends `ENGINEERING_METHODOLOGY_RULES` to every Hermes turn prompt.
- `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor.turn` executes a Hermes turn and returns the completed `final_response`.
- `backend/app/modules/agents/hermes/worker_shim.py::shrink_tool_history` compacts earlier tool-result messages before relaying `/v1/chat/completions`.
- `backend/app/modules/agents/hermes/worker_shim.py::inject_saved_plan` re-injects the saved plan summary after `shrink_tool_history`.
- `backend/app/modules/agents/hermes/worker_shim.py::Worker._handler` prepares the message list on every `/v1/chat/completions` relay step.
- `backend/app/modules/agents/hermes/broker_mcp.py::main` serves the MCP tool definitions and descriptions to the Hermes worker.
- `backend/app/modules/process_kernel/units.py::unit_registry` is the single Pint unit registry owner for engineering unit validation.
- `backend/app/modules/ai/thread_models.py::AIThreadInteractionRead` is the interaction read contract consumed by the frontend.
- `backend/app/modules/ai/thread_service.py::_submit_hermes_interaction` persists Hermes interaction completion and turn events.
- `backend/app/modules/ai/thread_service.py::_interaction_from_row` projects persisted turn events onto `AIThreadInteractionRead`.

## Scope

In scope:
- **Calibrated answer methodology (`supervisor.py`)**:
  - Extend `ENGINEERING_METHODOLOGY_RULES` so that when an exact literature value or source cannot be confirmed, the model provides its best engineering estimate with an explicit confidence level (`high`, `medium`, or `low`) and names what remains unconfirmed, rather than refusing to answer.
- **Deterministic verification pass (`backend/app/modules/agents/hermes/verification.py`, new; wired into `supervisor.py`, `thread_models.py`, `thread_service.py`)**:
  - Pure, offline, deterministic verifier run on turn completion over `(final_response, turn_tool_outputs)`:
    - **Unit check**: parse explicit quantity-with-unit tokens from the final answer and validate units against `process_kernel.units.unit_registry()`; flag malformed or unrecognised engineering unit tokens.
    - **Magnitude check**: when the answer states a numeric value alongside a known metric or unit that also appears in a succeeded tool output from the same turn, flag a >= 10x order-of-magnitude discrepancy as a warning.
    - **Physical-bounds / balance check**: flag obvious physical impossibilities in the final answer (negative Kelvin temperatures, negative densities/volumes/pressures stated as absolute, or explicit component/percentage totals exceeding 100% where bounded).
  - Persist a bounded `hermes.verification_pass` event (`status`: `"passed" | "warnings"`, `warning_count`, up to 20 structured checks `{kind, severity, summary}` with no secrets or filesystem paths) in SQLite `events` (no schema migration).
  - Add optional `verification_summary` to `AIThreadInteractionRead` (default `None`), project it in `_interaction_from_row`, regenerate `frontend/src/api/generated/`, and show a compact warning badge in `frontend/src/components/ai/useJarvisSidecar.tsx` when `warning_count > 0` (with static `.mjs` test).
- **Mid-turn context compaction (`worker_shim.py`)**:
  - Add `compact_turn_messages(messages, max_chars=48_000)` invoked in `Worker._handler` before `inject_saved_plan`: when the serialized message transcript exceeds `max_chars`, preserve system messages, the initial user message, and the most recent assistant+tool exchange, replacing older middle assistant/tool turns with one bounded deterministic digest message so KV-cache growth stays bounded while the saved plan stays intact.
- **Clearer tool descriptions (`broker_mcp.py`)**:
  - Refine the MCP descriptions for `jarvis_bench_run`, `jarvis_bench_read`, `jarvis_knowledge_read`, `jarvis_paper_search`, `jarvis_paper_pdf_read`, `jarvis_patent_read`, `jarvis_browser_page_read`, and `jarvis_turn_plan` so each states concisely when to use it and when to prefer another tool (e.g. compute physics with `jarvis_bench_run` instead of web search).

Out of scope (binding non-goals):
- No SQLite schema migration (use existing `events` table).
- No runtime frontier-model verification or second LLM call (deterministic checks only, per `L20261124095650-7ca4` item 4).
- No Hermes sub-agent spawning (blocked on spec 261) and no two-run comparison mode (separate follow-up slice).
- No Strata context-length (`64k`/`128k`) or sampling changes.
- No changes to tool permissions, egress policy, or sandbox boundaries.

## Files likely touched

- `backend/app/modules/agents/hermes/verification.py` (new)
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/agents/hermes/worker_shim.py`
- `backend/app/modules/agents/hermes/broker_mcp.py`
- `backend/app/modules/ai/thread_models.py`
- `backend/app/modules/ai/thread_service.py`
- `frontend/src/api/generated/` (regenerated)
- `frontend/src/api/threads.ts`
- `frontend/src/components/ai/useJarvisSidecar.tsx`
- `frontend/src/components/ai/JarvisSidecar.css`
- `backend/tests/test_hermes_calibrated_verification_366.py` (new)
- `frontend/tests/` static test + `frontend/package.json`

## Design constraints

- Additive, backward-compatible API contract: `verification_summary` defaults to `None` when no `hermes.verification_pass` event exists.
- Strict bounds: max 20 verification check entries per turn, each summary <= 200 chars, never containing secrets or absolute paths.
- Verification pass must never crash or fail a completed turn; any unexpected parse edge case degrades gracefully to skipped/passed without blocking delivery.
- Medium risk (feature code + small additive UI warning indicator; no migration/auth/egress): coordinator severe review, CI green on exact head, and passing `e2e/` browser walk covering the sidecar badge before merge.

## Acceptance criteria

1. `_assemble_turn_prompt` includes calibrated-estimate guidance (explicit confidence + unconfirmed items instead of refusal).
2. `verification.py` deterministically flags (a) unrecognised/malformed engineering units via `unit_registry()`, (b) >= 10x order-of-magnitude mismatches against same-turn tool outputs, and (c) obvious physical-bound violations (negative Kelvin, >100% bounded totals), while passing clean answers without false positives.
3. `_submit_hermes_interaction` persists `hermes.verification_pass` and `_interaction_from_row` projects `verification_summary` onto `AIThreadInteractionRead`; sidecar UI displays a compact warning indicator when warnings exist.
4. `compact_turn_messages` keeps transcripts under the configured character budget while preserving system messages, the user prompt, the most recent exchange, and the re-injected `jarvis_turn_plan`.
5. Refined MCP tool descriptions in `broker_mcp.py` remain bounded and pass existing Hermes runtime/harness tests; frontend contracts check and `npm run build` pass.

## Required tests

- Offline backend tests in `backend/tests/test_hermes_calibrated_verification_366.py` covering criteria 1–5.
- Frontend static `.mjs` test covering the verification warning indicator.
- `e2e/` browser walk check verifying the sidecar renders a verification warning indicator cleanly from `/` using visible controls only.

## Definition of done

Test gate green (see `AGENTS.md`), browser walk verified, coordinator severe review complete, CI green on exact head, STATUS row reconciled.
