# 370 — HERMES-LOCAL-TOOL-HISTORY-BUDGET-1: Keep earlier tool results whole while a local step fits its budget

Status: in_review
Depends on: 369

## Goal

On a `local:` route an earlier tool result in the same Hermes turn stays whole (as already bounded by
`compact_tool_payload`) for as long as the step fits the spec 369 message budget. Only when the step would exceed the
budget are earlier tool results shortened, oldest first, before whole steps are dropped. Cloud routes keep today's
transcript handling.

## Why

Directive `L20261124193224-8c59` (the harness, not the model, is the bottleneck), measured on the chat gate after
spec 369 (job `j-20261125T014640Z-82ef20`, head `a89da8b5`, hard set 4.9/16, all 8 turns completed). In the failing
research tasks the model said it could no longer see the evidence it had fetched: in one task it reported that the
proprietor and grant date were "not confirmed from the results I received" after a successful patent read, and
refused to repeat the identical read; in another it ended with "the prior tool outputs were compacted". The
`hermes.step_size` events of the patent task show `compacted_steps: 0` on every step and at most about 2.8k
estimated tokens against the 6k budget, with all earlier tool results together at 0.7–2.5k chars for up to 9 calls.
The loss comes from the fixed shrink of every earlier tool result to 180 chars, not from the budget: the turn had
room for the evidence, and the identical-call guard then blocks fetching it again.

## Code facts checked at `6f368031`

- `backend/app/modules/agents/hermes/worker_shim.py::shrink_tool_history` cuts every `tool` message except the last
  one to 180 chars (`keep_recent=1`, `max_shrunk_len=180`).
- `backend/app/modules/agents/hermes/worker_shim.py::Worker._handler` (relay chat completion) applies
  `shrink_tool_history`, then `compact_turn_messages(max_chars=45_500)`, then `inject_saved_plan`, before sending the
  `relay_request` frame. The shim does not know the selected route.
- Each tool result is already bounded by `compact_tool_payload` (2,000 bytes) in the same handler.
- `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor._handle_relay` knows `selected_route`; for
  `local:` routes it re-injects the plan, compacts tool schemas and calls
  `message_budget.compact_local_messages` (spec 369), then `infer_envelope`.
- `backend/app/modules/agents/hermes/supervisor.py::infer_envelope` applies `shrink_tool_history` again for every
  route.
- `backend/app/modules/agents/hermes/message_budget.py::_prompt_chars` and `size_breakdown` mirror the envelope by
  calling `shrink_tool_history`.
- The identical-call guard (spec 352) blocks a repeated identical tool call within the turn.

## Scope

1. **Shim forwards tool results unshrunk.** `Worker._handler` no longer calls `shrink_tool_history` or
   `compact_turn_messages`; it still injects the saved plan. Each result stays bounded by `compact_tool_payload`.
2. **Cloud routes unchanged in effect.** In `_handle_relay`, for a non-`local:` route the supervisor applies
   `shrink_tool_history` and then `compact_turn_messages(max_chars=45_500)` to the frame messages before
   `infer_envelope`, so the cloud prompt equals today's for the same transcript. `infer_envelope` keeps its
   `shrink_tool_history` pass for non-`local:` routes and skips it for `local:` routes.
3. **Budgeted tool history on local routes.** `compact_local_messages` keeps earlier tool results whole. After the
   spec 369 digests (items 1–2 there), while the estimated prompt plus the measured overhead exceeds
   `LOCAL_STEP_TOKEN_BUDGET`, it first shortens earlier tool results one at a time, oldest first, to the existing
   180-char form with its marker; only when all earlier results are shortened and the step still does not fit does it
   drop the oldest whole steps as today. The latest step's tool messages are never shortened. `_prompt_chars` and
   `size_breakdown` measure the messages actually sent (no second shrink).
4. **Event.** `hermes.step_size` adds one integer, `shrunk_tool_results`, the number of earlier tool results
   shortened on that step. Counts only.

Out of scope (binding non-goals):

- The budget value, the overhead rule, the digest limits, the turn cap, the tool router, the identical-call guard,
  the research budget, context size or Strata flags.
- `compact_tool_payload` limits.
- Summarising with a model; every reduction is deterministic.

## Constraints

- Never change the latest assistant step, its tool messages, the first user message or the saved plan.
- Never drop or shorten a `tool` message so that a `tool_call_id` loses its matching call, or the reverse.
- Delete the shim's now-unused calls; do not keep two shrink paths for the same route.

## Acceptance criteria

1. A local transcript of 6 steps with 1.5 kB tool results that fits the budget reaches the envelope with every earlier
   tool result byte-identical to its input, and `shrunk_tool_results: 0`.
2. The spec 369 12-step transcript (2 kB tool results) still ends at estimated plus default overhead ≤ 6,000 at
   every step; earlier tool results are shortened oldest first before any step is dropped, and the newest earlier
   results are the last to be shortened.
3. For a cloud route below the 45.5k guard, the envelope prompt built from a shim-forwarded transcript equals the
   prompt built by the pre-change pipeline (shim shrink and guard, then `infer_envelope`). Above the guard the same
   anchors are kept (system messages, first user message, latest step) and the omitted-message count is stated; the
   saved plan now counts toward the guard, which is accepted.
4. `hermes.step_size` payloads contain only integer and boolean fields, including `shrunk_tool_results`.
5. Lint, mypy ratchet and the focused Hermes tests (352, 362, 368, 369 and the new one) pass offline.

## Required tests

- Pure-function tests for budgeted shortening order and pairing (criteria 1, 2).
- Relay test through `HermesSupervisor._handle_relay` with a fake runner on a local and a cloud route (criteria 1,
  3, 4).
- Shim handler test that the forwarded `relay_request` messages carry unshrunk tool results and the injected plan.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, CI green on the exact head, one severe diff review,
spec status updated. Then the coordinator reruns the chat gate (hard and example sets) on a proof backend and
compares with the spec 369 run (`j-20261125T014640Z-82ef20`): maximum actual input tokens should stay at or under
about 6.5k, completed turns should not fall, and the research tasks should no longer report fetched evidence as
lost. The answers go to the maintainer's chat session for grading.
