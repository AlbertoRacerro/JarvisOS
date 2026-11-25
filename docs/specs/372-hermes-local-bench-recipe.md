# 372 — HERMES-LOCAL-BENCH-RECIPE-1: Run local Hermes turns with the bench loop's prompt, continuation and history rules

Status: in_review
Depends on: 370, 371

## Goal

On `local:` routes a Hermes chat turn behaves like the private agent bench loop on which the default local model
scored 10.75/16 on the hard set: a short rule-based system prompt with a fixed answer format, an 8,192-token output
cap per step, a plain continuation request when a step ends with neither a tool call nor an answer, a repeated call
answered from the stored result, old tool results trimmed rather than dropped, and a research budget and step cap that
end in a forced final answer. Cloud routes are unchanged.

## Why

Directive `L20261125074257-c57d` items 2–8 (item 1 is spec 371). The same model scores about 5/16 through the chat;
the maintainer's grading (`L20261125073601-7388`) puts the remaining losses where Hermes differs from the bench loop:
evidence lost to compaction (a patent read compacted away and then blocked as an identical call), answers that end in
"let me…" or "need to recompute" and still count as final, and arithmetic in prose. The directive asks for the loop's
behaviour, rule for rule, written for JarvisOS; the reference loop is private and is not copied into the repository.

## Code facts checked at `513abe87`

- `backend/app/modules/agents/hermes/worker_shim.py`: `COMPACT_METHODOLOGY` (five sentences) is the local
  methodology; `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor.__init__` builds `compact_system_prompt` from
  `PRESENTATION_INSTRUCTIONS` and it.
- `backend/app/modules/agents/hermes/supervisor.py::turn_guard_kind` detects an empty step or an announcement (`_ANNOUNCE_STARTS`, under 400 chars, no
  digit); the inference loop in `supervisor.py` (the `while True` around `runner(**runner_kwargs)`) nudges with
  `DEAD_TURN_NUDGE` up to `max_nudges`, then forces a final step (`reasoning_phase = "final"`, no tools).
- Output cap: `HermesSupervisor.turn` takes `active_entry.max_output_tokens or 2048`; the shim forwards
  `general_output_cap` (default 2048, clamped to 8192); one `length` retry doubles the cap up to 8192.
- Identical-call guard exists twice: `worker_shim.py` (`/v1/jarvis/tool`, `seen_tool_calls`) and
  `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor._handle_tool` (`turn_tool_hashes`); both answer "Tool call blocked: identical call was already
  executed in this turn" without the result. Research budget (8) is also enforced in both places.
- Step cap: the shim sets `tools = []` and `reasoning_phase = "final"` at `turn_cap` (10).
- `backend/app/modules/agents/hermes/message_budget.py::compact_local_messages` digests older assistant steps (spec 369), shortens earlier tool results
  to 180 chars oldest first only over budget (spec 370), then drops whole steps.
- `local_ai/runtime/model_registry.py` entries carry `reasoning_effort_analysis` and `reasoning_effort_final`; the
  sampling fields come from the entry.

## Scope

1. **System prompt for local routes.** Replace `COMPACT_METHODOLOGY` with a generic port of the bench rules, in
   JarvisOS's tool names, with no project or domain names: list the quantities needed; never compute numbers in your
   head, every number comes from a tool (`jarvis_python`, or a bench evaluator when one fits); physics, unit
   conversions and plausibility checks are computed, not searched; with an uncertain input state an assumption with a
   range and compute for the range; facts from the world come from search and reading tools, read before citing, never
   invent a DOI, URL or number; at most about 8 searches and reads; do not repeat an identical call; check units and
   magnitude; when you decide to use a tool call it in the same message, never announce it; several independent calls
   may go in one message; the final message has no tool call and uses the format `Answer:` (1–3 sentences),
   `Numbers:`, `Sources:`, `Caveats:`. Keep `PRESENTATION_INSTRUCTIONS` and any governance sentence Jarvis requires
   (provenance, proposals); drop the rest from local turns. The base prompt plus tool schemas stays under the spec
   368 first-step bound.
2. **Output cap.** On `local:` routes each step's cap is 8,192 tokens unless the active entry sets a lower
   `max_output_tokens`; the shim's `general_output_cap` default becomes 8,192 for local routes. The step deadline
   keeps its measured-speed derivation (spec 368) and its existing clamp.
3. **Missing final answer.** On `local:` routes the announcement detector is replaced: a step with no tool call
   whose answer text has no `Answer:` gets the continuation request "Continue. If you need a tool, call it now in this
   message; otherwise give the final answer in the required format." at most 2 times per turn; then the text is
   accepted as the answer. Empty steps keep today's handling. Cloud routes keep `turn_guard_kind`.
4. **Repeated call.** An identical call (same tool, canonical arguments) is never run again and never blocked
   silently: the reply is "You already made this exact call. Its result was: <first 600 chars of the stored result>
   Do not repeat it: change the input or answer." Results are stored per turn under the call key. One place owns the
   rule: the shim, which sees both the call and its result; delete the supervisor's duplicate guard if every relay
   tool call passes through the shim (prove it with a test), otherwise make both return the same text. `read_plan`
   stays exempt.
5. **Context.** On `local:` routes keep the system message, the first user message and the saved plan whole; keep
   the last 4 tool results whole and cut every older one longer than 700 chars to its first 700 chars plus
   " …[trimmed]". The spec 369 assistant-step digests are not applied on local routes. The spec 369/370 budget
   reductions (180-char shortening, then whole-step drop) remain only as the fallback when a step still exceeds
   `LOCAL_STEP_TOKEN_BUDGET`, and `hermes.step_size` adds the integer `trimmed_tool_results`.
6. **End of turn.** The research budget reply becomes "Research budget used up (8 searches/reads). Do not search
   again: answer now with what you have, and say clearly what is unconfirmed." At the step cap the forced final step
   sends "Stop using tools now and give your final answer in the format." with no tools and the final reasoning
   phase. The entry's `reasoning_effort_final` decides the effort; the coordinator sets it to `low` for the default
   local entry as an operator setting and reports it.
7. **Sampling.** Unchanged: the entry's sampling is used (for the default local model temperature 1.0, top_p 1,
   top_k 0, min_p 0, effort high in analysis). A test pins that a local step forwards the entry's values.

Out of scope (binding non-goals):

- The calculation tool (spec 371), the tool router's selections, the budget value, context size, Strata flags.
- Cloud routes' prompt and loop.
- Copying the private bench files or proprietary harness code; patterns are reimplemented.

## Constraints

- Every reduction is deterministic; no summarising with a model.
- Never break `tool_call_id` pairing; never change the latest step, the first user message or the saved plan.
- Events carry counts and booleans only.

## Acceptance criteria

1. A local step's system prompt contains the answer format and the compute-not-estimate rule, and none of the
   removed methodology sentences; a cloud step's prompt is unchanged.
2. A local step forwards `max_output_tokens` 8,192 with an entry that sets none, and the entry value when lower.
3. Local: a step ending "Let me pull the gathered evidence before answering" and one ending "Need to recompute
   carefully" with formulas but no `Answer:` both get the continuation request; after 2 requests the third such text
   is accepted. A text with `Answer:` is final at once.
4. A repeated identical call returns the stored-result text with the first 600 chars of the earlier result and runs
   nothing; `read_plan` is not affected.
5. A local transcript of 8 tool results of 2 kB that fits the budget reaches the envelope with the last 4 whole and
   the older 4 cut to 700 chars plus the marker, and `trimmed_tool_results: 4`; an over-budget transcript still ends
   within the budget through the fallback.
6. The research budget and step-cap texts match item 6; the step-cap step has no tools and the final phase.
7. Lint, mypy ratchet and the focused Hermes tests (352, 362, 366, 368, 369, 370, 371 and the new one) pass offline;
   existing tests that pinned replaced texts are updated, not deleted.

## Required tests

- Pure-function tests for trimming and the fallback (criterion 5).
- Inference-loop tests with a fake runner on local and cloud routes (criteria 1–3, 6).
- Shim tool-call tests for the repeated call and the single guard (criterion 4).

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, CI green on the exact head, one severe diff review, spec
status updated. Then the coordinator reruns the chat gate (hard and example sets) with Inkling on its default
service: target hard ≥ 9/16, examples ≥ 10/12, every turn completed, peak input per step reported; the answers go to
the maintainer's chat session for grading. If the chat stays well below the bench loop, the coordinator reports the
remaining differences (the directive's fallback is running the bench loop itself as the local chat engine).
