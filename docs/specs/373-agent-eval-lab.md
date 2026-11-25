# 373 — AGENT-EVAL-LAB-1: Repeated, paired and resumable agent evaluation with held-out tasks and artifact checks

Status: ready
Depends on: none

## Goal

`scripts/benchmarks/agent_eval` becomes the measuring instrument for the local agent: every run of a task set with
k ≥ 3 repetitions reports pass^1 and pass^k with standard errors, two runs on the same tasks are compared pair by pair
with a standard error on the difference, part of a task set can be frozen as held-out, a task can require that the
agent produced a reviewable artifact through a Jarvis tool (a Studio draft or a typed change request) rather than only
an answer in chat, and a long run survives an interruption without losing the finished repetitions.

## Why

Directive `L20261125082433-0dec` step 1 ("the lab"): a private task set of about 40 tasks growing towards 150–200,
deterministic checks where possible, a frozen held-out part, k ≥ 3 repetitions, pass^1 and pass^k with standard
errors, paired comparison of configurations, and tasks where the agent acts through Jarvis tools. Every later step of
that directive (context ceiling, reliability techniques, prompt optimization) is accepted or rejected on this
instrument. Today a gate is one repetition per task, compared by a fixed point threshold, and the results file is
written only at the end: the spec 372 gate job lost its unfinished set when its sandbox was replaced. The task sets
themselves are pack content and stay outside this repository.

## Code facts checked at `0dc903bf`

- `scripts/benchmarks/agent_eval/runner.py::run_eval` loops tasks × `reps` (default 3), submits each prompt to
  `/ai/threads/{id}/interactions`, polls to a terminal flow state, scores with `score_task`, and writes
  `results.json` and `<label>.json` once, after the last task.
- `score_task` gives `passed / len(checks) * max_points` and a hard zero when the flow state is not completed or the
  answer is empty; `check_passes` knows only `number` (relative tolerance and unit `scale`) and `contains_any`, both
  on the answer text.
- `compare_runs` requires equal `set_id`, task ids and repetition counts and fails on a total drop > 0.5 points, a
  task drop ≥ 1 point, or a new uncompleted repetition; it reports no uncertainty.
- `count_thread_tool_calls` reads `events` rows of type `hermes.agent_event` with `kind == "tool.call"` and
  `session_ref.jarvis_thread_id` from the proof database, read-only.
- `backend/app/modules/agents/hermes/broker_mcp.py` exposes `jarvis_studio_act` (`op: studio_draft`, `mode:
  create|update`) and the typed Process change request tool; `backend/app/core/schema.py` defines `studio_drafts`
  (`workspace_id`, `source`, `created_at`, `updated_at`).
- `example_tasks.json` (`set_id`, `cloud_ok`, `tasks[]` with `id`, `prompt`, `max_points`, `checks[]`) is the
  public example set.

## Scope

1. **Task fields.** A task may carry `split: "dev" | "heldout"` (default `dev`) and `tags: [str]`. The run subcommand
   gains `--split dev|heldout|all` (default `dev`); `heldout` and `all` require `--allow-heldout` so an optimisation
   loop cannot read held-out tasks by accident. The results record the split, the selected task ids and the
   task-file SHA-256.
2. **Pass definition and statistics.** A repetition passes when every check passes and the flow completed. Per task
   report the pass count c of n repetitions, pass^1 = c/n and pass^k = 1 if c = n else 0 for the run's k = n; per
   run report mean pass^1, mean pass^k and mean points, each with the standard error over tasks
   (sample standard deviation / √tasks; 0 for one task). The existing point totals stay, so earlier results remain
   comparable.
3. **Paired comparison.** `compare` matches tasks by id (refuse a different `set_id`, task-file SHA-256 or task id
   set unless `--allow-task-drift` is given, which is then printed) and reports, for points and pass^1, the mean of
   per-task differences, its paired standard error, and the count of tasks better / worse / equal. The existing
   regression rules and exit code are unchanged and the new lines are added to the table output; `--json` writes
   the same numbers as JSON.
4. **Artifact checks.** Two new check kinds, evaluated on the isolated proof database after the repetition, both
   requiring `--data-root`: `tool_called` (`tool`: a tool name; optional `min`: count, default 1) counts that
   thread's `tool.call` events for the tool; `studio_draft` (optional `terms`: list) passes when a `studio_drafts`
   row of the run's workspace was created or updated between the repetition's start and end and its `source`
   contains every term (case-insensitive). Without `--data-root` these checks fail with the reason recorded, never
   pass.
5. **Resumable runs.** After each repetition the runner rewrites `results.json` atomically (write to a temporary
   file, then rename) with the repetitions so far and `complete: false`; the final write sets `complete: true`.
   `run --resume` on an existing output directory with the same label, task-file SHA-256, split and reps skips the
   finished repetitions and continues; any mismatch refuses. `compare` refuses an incomplete run.
6. **Reporting.** A run prints one summary line: set id, split, tasks, k, mean points ± SE, pass^1 ± SE, pass^k ± SE,
   completed repetitions, tool calls and wall time.

Out of scope (binding non-goals):

- Writing the private task sets (pack content, done by the coordinator in the pack).
- Logging the maintainer's accept/reject of agent changes in the product (its own spec, next in this step).
- Model-graded checks, prompt optimization, parallel repetitions, any provider call.

## Files likely touched

- `scripts/benchmarks/agent_eval/runner.py`
- `scripts/benchmarks/agent_eval/example_tasks.json` (one `heldout` task and one `tool_called` check as examples)
- `scripts/benchmarks/agent_eval/module.toml`
- `backend/tests/test_agent_eval_373.py` (new; `test_agent_eval_354.py` keeps passing)

## Design constraints

- The proof database is opened read-only, as today; the runner never writes to the data root.
- The base-URL rules (loopback only, port 8000 refused) are unchanged.
- Results stay backward-readable: `load_run_results` and `compare` accept earlier files that lack the new fields
  (treated as split `dev`, complete, no SHA) and say so in the output.
- Standard errors are computed with the standard library; no new dependency.

## Acceptance criteria

- A three-repetition run on a stubbed backend reports pass^1, pass^k and their standard errors equal to hand-computed
  values for a task set with mixed outcomes.
- `run --split heldout` without `--allow-heldout` refuses; with it only held-out tasks run.
- `compare` of two stubbed runs prints the paired mean difference, its standard error and better/worse/equal counts
  that match hand-computed values; a set or SHA mismatch refuses.
- `tool_called` and `studio_draft` pass and fail correctly against a fixture SQLite database and fail without
  `--data-root`.
- A run interrupted after some repetitions and resumed with `--resume` produces the same `results.json` content
  (except timings) as an uninterrupted run and does not repeat finished repetitions; a mismatched resume refuses.
- Lint, the focused tests and CI pass.

## Required tests

- Offline tests with a stubbed HTTP backend (no provider, no model) and a temporary SQLite fixture for the event and
  `studio_drafts` rows; pin the statistics, the held-out guard, the artifact checks, atomic partial writes, resume
  and backward reading of an earlier results file.

## Definition of done

Merged with green CI on the exact head and one severe coordinator review (medium risk: tooling with tests). The
coordinator then runs the private set through it with k = 3 on the merged head and reports pass^1 and pass^k with
standard errors to the maintainer's chat session.
