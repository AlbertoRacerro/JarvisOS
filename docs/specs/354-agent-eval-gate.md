# 354 — AGENT-EVAL-GATE-1: Repeatable agent regression eval for the local default and the Hermes harness

Status: merged
Depends on: 352

## Goal

A repeatable eval drives the real Jarvis chat → Hermes path with a task set, three repetitions per task, scores the
answers, compares them with a stored baseline and a reference model, and fails when the candidate regresses. It runs
before any change of the local default model or of the Hermes harness.

## Why

Directive `L20261124012919-7d88` item 6. The local default and the harness rules were chosen on a one-shot private
bench (one run per task, judged by hand, outside Jarvis). Nothing re-checks them when the model, sampling or harness
changes, so a regression would be found only in use.

## Code facts checked at `e6b0f5bf`

- `backend/app/modules/ai/thread_routes.py::submit_ai_thread_interaction` (`POST /ai/threads/{thread_id}/interactions`:
  router prefix `/threads` under `backend/app/modules/ai/routes.py` prefix `/ai`) submits one chat turn; `thread_service._submit_hermes_interaction`
  sends it to Hermes when the `hermes:agent` route is reachable.
- `backend/app/modules/ai/thread_models.py::AIThreadInteractionRead` returns `assistant_text`, `flow_state`,
  `terminal_reason`, `route_class`, `model_id`, `role_id` and `flow_id` of the turn.
- `backend/app/modules/agents/hermes/supervisor.py::project_event` stores each Hermes agent event (contract
  `backend/app/modules/ai/agent_contracts.py`, with `kind` and `session_ref`) as an `events` row of type
  `hermes.agent_event`; no HTTP route reads them.
- `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor.turn` resets the per-turn research count,
  tool-hash set and step count that spec 352 guardrails use.
- `e2e/README.md`: proof processes run on a temporary copy of a data root at `JARVISOS_PROOF_PORT`, never on port 8000.
- `scripts/relay_jobs.py start` runs bounded long work as a registered job.

## Scope

In scope:
- **Runner** (new, for example `scripts/agent_eval.py`, stdlib only): `run --tasks <file> --label <name> --reps 3
  --base-url http://127.0.0.1:<port> --out <dir>`. For each task and repetition it creates a fresh thread, submits the
  prompt through the interaction API, waits for the terminal state with a per-task wall timeout, and records
  answer, `flow_state`, `terminal_reason`, `model_id`, `role_id`, wall time and, when `--data-root` names the proof data-root
  copy, the tool-call count read from its `hermes.agent_event` rows for that thread (SQLite opened read-only;
  no new route). Refuses a base URL
  whose port is 8000 or whose host is not loopback.
- **Task file format** (JSON): `{"set_id", "cloud_ok": bool, "tasks": [{"id", "prompt", "checks": [...], "reference",
  "max_points": 2}]}`. `checks` are deterministic: `{"kind": "number", "value", "rel_tol"}` passes when any number in
  the answer is within tolerance (unit-normalised only by explicit `scale` aliases given in the check); `{"kind":
  "contains_any", "terms"}`. Points = share of
  checks passed × `max_points`, plus a hard zero when `flow_state` is not completed or the answer is empty.
- **Public example set**: `scripts/benchmarks/agent_eval/example_tasks.json` with at least six generic, literature-free
  engineering tasks (pipe flow, heat transfer, a small curve fit, a unit conversion chain, a tool-chain of two
  calculations, one "uncertain input → stated range" task) with hand-checked reference values. Private task sets are
  passed by path and never enter the repository.
- **Reference model** (optional, `--reference haiku`): runs the same tasks through the Claude CLI on the maintainer's
  subscription, with the same scorer. Refused unless the task file has `cloud_ok: true`. Never via a paid API key.
- **Compare**: `compare --baseline <run dir> --candidate <run dir>` prints a per-task table (mean and min over reps,
  tool calls, wall time) and exits 1 when the candidate's total mean is more than 0.5 points below the baseline, or
  any task's mean drops by 1 point or more, or any repetition ends without a completed answer where the baseline had
  none such. Baseline and reference results are plain JSON in `--out`, outside the repository.
- **Module docs**: the new directory gets a `module.toml`; `docs/MODULES.md` regenerated.

Out of scope (binding non-goals):
- A model judge for free-text quality (later item; the brief keeps judgement to models, but the gate must be
  deterministic).
- Starting or configuring the backend, the local model server or Hermes: the operator starts a proof backend on a
  data-root copy first (documented in the runner's `--help`).
- DOI verification, fine-tuning, ensembles, or any item the directive defers.
- Wiring the gate into CI (it needs a live local model).

## Files likely touched

- `scripts/agent_eval.py` (new) or `scripts/benchmarks/agent_eval/` (new, with `module.toml`)
- `scripts/benchmarks/agent_eval/example_tasks.json` (new)
- `backend/tests/test_agent_eval_354.py` (new)
- `docs/MODULES.md` (regenerated)

## Design constraints

- Loopback only for the backend; no new dependency; no provider called directly (the backend owns the model call).
- No private task text, answers or pack terms in the repository, logs or test fixtures.
- Medium risk (supervisor tooling, no product path change); the Claude CLI reference path is egress of task text and is
  gated by `cloud_ok`.

## Acceptance criteria

1. With a stub HTTP server standing in for the backend, `run` creates one thread per task and repetition, records all
   listed fields, and marks a timeout or non-completed turn as zero points.
2. Scorer: number checks pass within tolerance and fail outside it; `contains_any` is case-insensitive; empty answer
   scores zero.
3. `compare` exits 1 on each of the three regression conditions and 0 when within bounds.
4. `run` refuses port 8000 and non-loopback hosts; `--reference haiku` refuses a task file without `cloud_ok: true`.
5. The example task set loads and every reference value is reproduced by an independent calculation in the test.
6. Live proof, after merge: a three-repetition run of the example set and of the private set against the current local
   default on a proof backend, plus one Haiku reference run of the example set, recorded as the first baseline.

## Required tests

- Offline tests for criteria 1–5 with a stub server and an injected CLI runner; no live model, no network.

## Definition of done

Test gate green (see `AGENTS.md`), criteria 1–5 met with a coordinator full-diff review, STATUS updated; criterion 6
recorded as live proof or as owed live activation.
