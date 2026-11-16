# 288 — Withhold rejected bench values from agent views

Status: ready
Depends on: 282, 283, 284

## Goal

A bench result with verdict `not_usable` keeps its computed numbers for human inspection but never presents them as usable outputs in the bench command or agent tool views, comparisons, or a follow-on agent run. The agent can report a missing number with a reason.

## Why

Showing a failed check beside a number has not reliably stopped an agent from using that number downstream. The existing bench already derives the verdict and stores full envelopes; this slice changes the hand-off boundary, not the calculation.

## Code facts checked at `94ef8955`

- `backend/app/modules/engineering/bench.py::run_evaluation` serializes `EvaluationResult` into `result.json` and builds a bounded view.
- `backend/app/modules/engineering/bench.py::format_bounded_view` currently prints outputs even when the verdict is `not_usable`.
- `backend/app/modules/engineering/bench.py::compare_runs` compares output values from two stored result files.
- `backend/app/modules/engineering/bench.py::show_run` rebuilds a view from a stored result.
- `backend/app/modules/engineering/bench.py::dispatch_bench_tool` handles agent run and read calls through the private dispatcher.
- `backend/app/modules/engineering/evaluator_contracts.py::result_verdict` derives `not_usable` from failure evidence without changing computed outputs.

## Scope

In scope:
- For `not_usable`, move serialized computed outputs in `result.json` under `rejected_outputs` and make `outputs` empty. Preserve values, units, checks and artifacts in the file.
- Agent-facing and CLI bounded views show `outputs: withheld (verdict not_usable)` alongside status, verdict, failed checks and envelope reference. The bounded view must contain no rejected numeric outputs, even after truncation.
- Show and compare views never place `rejected_outputs` into usable output fields. A comparison identifies the rejected side and omits its numeric values and numeric deltas.
- The agent tool refuses to use a `not_usable` geometry or mesh run as the input to a later run.
- Agent-facing answer guidance permits numeric fields to be `null` with a reason when evidence is rejected.

Out of scope:
- Changing calculations, checks, verdict derivation, or the treatment of `unchecked` results.
- Hiding checks' measured `actual` and `limit` fields, or removing human access to the full envelope.

## Files likely touched

- `backend/app/modules/engineering/bench.py`
- `backend/tests/test_engineering_bench.py`
- `backend/tests/test_283_bench_agent_tools.py`
- Relevant agent instruction text in `backend/app/modules/ai/thread_service.py` if it currently requires a numeric answer.

## Design constraints

- The computed numbers remain losslessly available in the full envelope under `rejected_outputs`; no evaluator changes its result.
- Refusal of a rejected prior run happens before native work starts.
- `unchecked` remains visible and explicitly labelled until measured separately.
- Older stored envelopes with `verdict: not_usable` and `outputs` must also be withheld when shown or compared.

## Acceptance criteria

1. A rejected run stores its computed outputs under `rejected_outputs` in `result.json`, with no usable `outputs`; its bounded CLI and agent views contain a withheld marker and no rejected output values.
2. A stored pre-change rejected result shown or compared also withholds its output numbers. A usable result still shows numbers.
3. Compare does not compute or print a delta involving a rejected side.
4. The agent tool rejects a rejected solid or mesh predecessor before invoking mesh or FEM.
5. The agent's answer contract permits `null` with a reason for every numeric answer; an offline test covers the instruction or schema that supplies this guidance.

## Required tests

- Offline run/show/compare tests covering rejected and usable envelopes, including a legacy rejected envelope.
- Offline agent tool test proving predecessor refusal and no native invocation.
- Test of nullable numeric-answer guidance if a binding answer schema exists.

## Definition of done

Focused tests and lint pass, exact-head CI is green, coordinator severe review of the diff is complete, and STATUS records the implementation PR.
