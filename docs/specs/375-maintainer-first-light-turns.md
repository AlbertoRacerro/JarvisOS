# 375 — MAINTAINER-FIRST-LIGHT-TURNS-1: Benchmarks yield the local model to the maintainer; navigation turns run light

Status: in_review
Depends on: 368, 372

## Goal

The maintainer's own chat and Bubu requests stop queueing behind benchmark work on the single-slot local model server,
and short navigation or conversational turns on the local Hermes route answer in one light step: low reasoning effort,
no tool list, a compact prompt of at most about 3k tokens. Calculation, verification and design turns are unchanged.

## Why

Directive L20261125155527-273f: "show me the CFD screen" with Inkling took minutes because the local model server runs
one request at a time and a benchmark was running, and because navigation ran as a multi-step Hermes turn at effort
`high` with a 5.6–8.5k-token prompt. The target is the arrow in at most 40 s on an idle machine.

## Code facts checked at `2f9dd03e`

- `scripts/benchmarks/agent_eval/runner.py::run_eval` runs tasks and repetitions serially against a proof backend;
  `scripts/benchmarks/agent_eval/runner.py::validate_base_url` accepts loopback hosts only and refuses port 8000, so the
  runner cannot and must not ask the live app about maintainer activity.
- The local model server (Strata, one batch slot) answers `GET /slots` on its loopback port with
  `[{"id": 0, "is_processing": <bool>, ...}]` (observed on 2026-11-25). Its port comes from the model entry in the
  data root's `settings/local_models.json` (`port`, `host`).
- `backend/app/modules/ai/providers/local_llamacpp_adapter.py::LocalLlamaCppAdapter` sends
  `chat_template_kwargs.reasoning_effort` from the active model entry: `reasoning_effort_final` for the final phase
  (`metadata.reasoning_phase == "final"`) and routing/smoke tasks, otherwise `reasoning_effort_analysis` (for the
  default local model `high` and `low`).
- `backend/app/modules/agents/hermes/supervisor.py::run_governed_inference` puts `reasoning_phase` into the relay
  request metadata; `HermesSupervisor.turn` takes `allowed_tools` and the context blocks.
- `backend/app/modules/ai/thread_service.py::_turn_tools` is the deterministic per-turn tool router (word groups, at most
  seven tools); the Hermes submit path installs the grant blocks, the system envelope and the visible-page block
  before `worker.turn(...)`.
- `backend/app/modules/ai/thread_ui_context.py::render_visible_page_block` renders Bubu's DOM targets, the navigation
  guide (`thread_app_map.py`, at most 2,600 chars) and the `[[point:<id>]]` rules; the frontend parses the pointer.
- `backend/app/modules/ai/thread_service.py::_route_accepts_images` already returns False for `hermes:agent`, so no
  screenshot reaches Inkling through Hermes; the frontend attaches images only when the user takes a shot or enables
  the vision frame (`frontend/src/components/bubu/bubuExtras.ts`).

## Scope

In scope:
1. **Benchmarks yield.** `run_eval` gains `--yield-to-model <loopback base URL>`. Before every task repetition it polls
   `GET <url>/slots` and waits until no slot has been processing for a continuous quiet window
   (`--yield-quiet-seconds`, default 120), so the gaps inside a maintainer's multi-step turn do not let a benchmark
   step in. An unreachable or malformed `/slots` means "do not wait" and is logged once. Every repetition records
   `yield_wait_s`; the run totals record the number of pauses and the total paused seconds. Wall times stay as measured
   (the wait is outside them) and the pause is reported next to them. The gate scripts pass the flag.
2. **Light turn label.** One deterministic function labels a local Hermes turn `light` when either (a) Bubu's
   `ui_context` is present and the question asks where something is or to show/open/go to something (Italian and
   English words such as dove, mostra, mostrami, apri, vai, trova, where, show, open, go to, find), with no digit and
   no calculation word; or (b) the question is a greeting or thanks of at most six words with no digit. Everything
   else stays `work`. No model call decides the label.
3. **Light turn path.** A `light` turn on a `local:` route runs with no tools offered (one step, final answer), relay
   metadata asking for low reasoning effort, which the adapter honours over `reasoning_effort_analysis`, and a compact
   prompt: a short role line, the visible-page block (when present) and the question, without grant blocks or the
   long methodology, at most about 3k estimated tokens. A `light` turn that ends without an answer falls back once to
   the normal `work` path. Cloud routes and `work` turns are unchanged.
4. **DOM-first check.** A test pins that a Hermes turn with images never forwards them to the model (current
   behaviour), so Bubu on Inkling stays DOM-first.
5. **Measure.** Time-to-answer for 5 navigation prompts and 5 short questions on a fresh root, before (master) and
   after (PR head), on an idle model and while a gate runs with `--yield-to-model`; plus one hard-set run at the PR
   head to show calculation quality does not drop. Navigation answers must still contain a valid `[[point:<id>]]`.

Out of scope (binding non-goals):
- Pausing a benchmark in the middle of a task, or per-step preemption inside the model server (PARK if task-level
  yield proves insufficient).
- A learned router or a model call to classify turns.
- Changing the model server, its slot count or the live app's port-8000 process; reading the live data root.
- Vision-model routing for screenshots.

## Files likely touched

- `scripts/benchmarks/agent_eval/runner.py`
- `backend/app/modules/ai/thread_service.py`
- `backend/app/modules/agents/hermes/supervisor.py`
- `backend/app/modules/ai/providers/local_llamacpp_adapter.py`
- tests under `backend/tests/` and the agent-eval tests

## Design constraints

- The yield probe is a read-only loopback GET to the model server; never port 8000 and never the live data root.
- The effort override can only lower effort (`low`); it never raises it and never changes the model or route.
- The light label is a pure function of the question text and whether `ui_context` is present.
- Grants are not installed for a `light` turn (no tools are offered), so no capability is widened.
- No new dependency, setting file or schema change.

## Acceptance criteria

1. With a fake `/slots` that reports processing and then idle, the runner waits until the quiet window has passed,
   records the wait for that repetition, and reports pause count and total in the results; an unreachable `/slots` does
   not block the run.
2. The label function returns `light` for the Bubu navigation examples and short greetings in both languages, and `work`
   for questions with digits, calculation words, or without `ui_context` for navigation wording.
3. A `light` local turn sends one relay request with an empty tool list, low reasoning effort in the adapter payload and
   a prompt under 3k estimated tokens containing the visible-page block; a `work` turn's request is unchanged.
4. A `light` turn without an answer is retried once on the `work` path.
5. Images on a Hermes turn never reach the model request.
6. Measurement table (before/after, idle/under gate) and the hard-set run are recorded in the PR with their result
   paths; "mostrami la schermata CFD" on an idle model answers with a valid pointer in at most 40 s, or the PR states
   the measured time and why.

## Required tests

- Offline tests with a fake `/slots` server or opener and a fake clock for the yield; unit tests for the label; fake
  worker/adapter tests for the light path and the fallback; the image check. No network, no real model.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, measurement recorded, spec status updated. Risk: medium
(feature code with tests; no egress, grants only narrowed).
