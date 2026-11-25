# 369 — HERMES-LOCAL-MESSAGE-BUDGET-1: Keep every local Hermes step under a message budget

Status: ready
Depends on: 362, 366, 368

## Goal

On a `local:` route every model step of a Hermes turn sends at most about 6,000 estimated input tokens, from the
first step to the turn cap, instead of growing from about 4k to over 10k. The saved plan, the question, the latest
assistant step and its tool results stay whole; older steps are reduced to short digests. Each relay step records a
size breakdown by message category, so the gate shows where input goes.

## Why

Directive `L20261124193224-8c59` items 1 and 5, after spec 368. Measured on the chat gate after PR #312 (hard set,
8 tasks, one repetition): first step 3.4k–4.4k actual input tokens, maximum per task 7.9k on average and 10.5k at
worst, growing by about 300–1,400 tokens per step. The 32k baseline on the local default (ledger
`L20261125010040-f7ad`) decodes about 31 tok/s at 3.9k prompt tokens, 17 tok/s at 8k and 8 tok/s at 16k. Keeping each
step under about 6k holds decode near its fast range for the whole turn. A larger context does not help until this
holds, because the extra KV cache sits in host RAM.

## Code facts checked at `224ced86`

- `backend/app/modules/agents/hermes/worker_shim.py::shrink_tool_history` cuts every `tool` message except the last
  one to 180 chars. Assistant messages are never reduced.
- `backend/app/modules/agents/hermes/worker_shim.py::compact_turn_messages` drops middle messages only above
  `max_chars`. `Worker._handler` calls it with `max_chars=45_500` (about 11k tokens), so on the local default it
  never fires before the turn cap.
- `backend/app/modules/agents/hermes/worker_shim.py::completion_message` turns a model tool proposal into an
  assistant message with up to 4 `tool_calls`; their `arguments` (for `jarvis_turn_plan` the whole step and finding
  lists) stay in the transcript at full length on every later step.
- `backend/app/modules/agents/hermes/worker_shim.py::Worker._handler` passes each tool result through
  `compact_tool_payload` (2,000 bytes). When the tool is `jarvis_turn_plan` it also stores the plan, which
  `inject_saved_plan` re-injects into the system message on every step (up to 2,400 chars). The same plan also
  stays in the transcript as that tool's result and call arguments.
- `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor._handle_relay` knows the selected route. For
  `local:` routes it replaces the system message (`compact_system_message`), re-injects the plan and compacts tool
  schemas, then calls `infer_envelope`, which applies `shrink_tool_history` again and serialises messages plus the
  tool summaries (`_local_tool_prompt`) and `RELAY_TOOL_PROTOCOL` into the envelope prompt.
- `ai_jobs.input_tokens` holds the actual prompt tokens reported by the local runtime (PR #312);
  `scripts/benchmarks/agent_eval/runner.py` reports `first_actual_input_tokens` and `max_actual_input_tokens` per
  repetition.

## Scope

In scope, `local:` routes only, applied in `HermesSupervisor._handle_relay` before `infer_envelope`:

1. **Step digests.** In every assistant message older than the latest assistant message:
   - `content` longer than 300 chars is cut to 300 chars with a fixed marker;
   - each tool call keeps its `id` and function name; its `arguments` JSON longer than 200 chars is replaced by a
     string of at most 200 chars that starts with the call's first argument keys and ends with a fixed marker.
   Tool-call ids stay paired with their `tool` messages, so the upstream transcript shape stays valid.
2. **Plan not repeated.** Older `jarvis_turn_plan` tool results and call arguments are cut to their digest even
   when short, because the saved plan in the system message already carries them. The latest step is unchanged.
3. **Message budget.** After 1–2, if the estimated size of the envelope prompt (messages, tool summaries and
   `RELAY_TOOL_PROTOCOL`, `ceil(chars/3.5)`) exceeds `LOCAL_STEP_TOKEN_BUDGET = 6000`, the oldest complete steps
   (an assistant message with its tool messages) after the first user message are removed one at a time, oldest
   first, and replaced by one system line stating how many steps were compacted and pointing to the saved plan.
   The system message, the saved plan, the first user message and the latest step are never removed. If the
   budget still cannot be met, the request is sent as it is and the overflow is recorded (item 4); it is never
   refused for size.
4. **Size breakdown.** Each local relay step writes one `hermes.step_size` event, through the same event writer as
   `hermes.turn_guard`, with integer char counts only: `system`, `plan`, `user`, `assistant`, `tool_args`, `tool_results`, `tools`,
   `protocol`, `estimated_tokens`, `compacted_steps`, `over_budget` (bool), and the step number. No text.
5. **Gate report.** `agent_eval` results add per repetition the number of steps over 6,000 actual input tokens.

Out of scope (binding non-goals):

- Cloud routes, which keep today's transcript handling.
- The context size, Strata flags, the tool router, the turn cap, the research budget or tool allowlists.
- Summarising with a model; every reduction here is deterministic.
- `worker_shim.py` behaviour for any route (its 45.5k guard stays as the cloud backstop).
- Editing `backend/tests/**/test_*_conformance.py`.

## Files likely touched

- `backend/app/modules/agents/hermes/supervisor.py` (or a small helper module beside it)
- `scripts/benchmarks/agent_eval/runner.py`
- `backend/tests/test_hermes_local_message_budget_369.py`, `backend/tests/test_agent_eval*.py`

## Design constraints

- Pure functions over the message list; no network, clock or model call.
- Never change the latest assistant step, its tool messages or the first user message.
- Never drop a `tool` message while keeping its assistant tool call, or the reverse.
- Events carry counts only: no question text, arguments, tool output or secrets.
- Reuse `compact_system_message`, `inject_saved_plan` and the existing estimate rule; delete rather than duplicate.

## Acceptance criteria

1. A synthetic 12-step local transcript with realistic sizes (2 kB tool results, 1.5 kB `jarvis_turn_plan` arguments,
   400-char assistant text, a 2.4 kB saved plan) gives an estimated envelope prompt ≤ 6,000 tokens at every step,
   while the same transcript on a cloud route is unchanged byte for byte.
2. In that transcript the first user message, the system message with the saved plan, and the latest assistant step
   with all its tool messages are identical to the input; every remaining `tool_call_id` has its matching call.
3. A transcript already under budget is changed only by items 1–2; a transcript that cannot fit is sent with
   `over_budget: true` in its `hermes.step_size` event and is not refused.
4. `hermes.step_size` payloads contain only the listed integer and boolean fields.
5. Lint, mypy ratchet and the focused Hermes and agent-eval tests pass offline.

## Required tests

- Pure-function tests for digests, plan de-duplication, budget compaction and pairing (criteria 1–3).
- Relay test through `HermesSupervisor._handle_relay` with a fake runner: local route compacted, cloud route
  untouched, event written (criteria 1, 3, 4).
- Agent-eval result field test (item 5).

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, CI green on the exact head, one severe diff review,
spec status updated. Then the coordinator reruns the chat gate (hard and example sets) on a proof backend and
compares with the PR #312 run: maximum actual input tokens per task should stay at or under about 6.5k, and the
score and completed turns should not fall. The answers go to the maintainer's chat session for grading.
