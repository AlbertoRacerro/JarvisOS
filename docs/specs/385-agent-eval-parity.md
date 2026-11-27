# 385 — AGENT-EVAL-PARITY-1: Three-arm parity reporting, Gate Zero alarm, and per-category scoreboard

Status: ready
Depends on: —

## Goal

Extend the spec-373 agent eval lab (`scripts/benchmarks/agent_eval/runner.py`) with per-category task aggregation, a
three-arm parity comparator (`parity` subcommand comparing standalone reference Arm A, reference-in-harness Arm B, and
optional local-in-harness Arm C), automatic Gate Zero (`B < A`) regression alarming with ranked per-task diffs, and an
atomic plain-Italian scoreboard writer so progress toward reference parity is measured honestly per category with
standard errors and 95% confidence intervals.

## Why

Directive `L20261126095006-552f` sets the north-star metric for the local agent stack: the local system (Arm C) must
match a cloud reference model (Arm A) per category (`calculation`, `multi_step_design`, `literature`, `patent`,
`tool_use_navigation`) on public/indicative domain tasks, with Gate Zero requiring that the Jarvis harness improves the
reference model (`B > A`) before local-model tuning begins. Today `agent_eval` supports only two-run `compare` without
per-category grouping, three-arm parity ratios with confidence intervals, Gate Zero alarms, or a scoreboard writer.

## Code facts checked at `bc40e416`

- `scripts/benchmarks/agent_eval/runner.py::run_eval` executes `reps` repetitions of a task set (via `--reference` or
  `--base-url`) and writes atomic `results.json` with overall `summary` from `run_summary`, without per-category grouping.
- `scripts/benchmarks/agent_eval/runner.py::run_summary` computes overall `mean_points`, `mean_pass_at_1`,
  `mean_pass_at_k`, and standard errors via `scripts/benchmarks/agent_eval/runner.py::_std_error`.
- `scripts/benchmarks/agent_eval/runner.py::paired_stats` computes per-task paired differences and paired standard error
  for `points` and `pass_at_1` across two runs.
- `scripts/benchmarks/agent_eval/runner.py::compare_report` validates comparability (`set_id`, `tasks_sha256`, task ids,
  completeness) and formats a comparison table and JSON payload.
- `scripts/benchmarks/agent_eval/runner.py::build_parser` defines the `run` and `compare` subcommands;
  `scripts/benchmarks/agent_eval/runner.py::main` enforces `scripts/vault_guard.py::touches_vault` on `--data-root` and
  `--out`.

## Scope

In scope:
1. **Per-category metadata and summaries (`scripts/benchmarks/agent_eval/runner.py`)**:
   - Accept an optional `category` string on each task in a task-set JSON (defaults to `"uncategorized"` when absent;
     raises `ValueError` when present and not a non-empty string).
   - Record `category` on each task entry in `results.json` and emit `category_summary: dict[str, dict[str, Any]]`
     (computed with `run_summary` per category, ordered deterministically) alongside overall `summary`.
   - Maintain full backwards compatibility with existing `results.json` files that predate `category` and
     `category_summary`.
2. **Three-arm parity comparator (`agent_eval parity`)**:
   - Add a `parity` subcommand accepting `--arm-a` (required: standalone reference run), `--arm-b` (required:
     reference-in-harness run), optional `--arm-c` (local-in-harness run), `--allow-task-drift`, `--json <path>`,
     `--scoreboard <path>`, `--change-label <str>`, and `--date <YYYY-MM-DD>`.
   - Enforce `vault_guard.touches_vault` (exit `vault_guard.REFUSED_EXIT`) on all supplied input and output paths.
   - Validate completeness and comparability across all supplied arms (`set_id`, `tasks_sha256`, common `task_ids`
     unless `--allow-task-drift`).
   - Compute per-category and overall metrics:
     - Each arm's `mean_points`, `mean_pass_at_1`, `mean_pass_at_k`, and standard errors;
     - **Gate Zero (`B - A`)**: paired difference and paired SE (`paired_stats`), `gate_zero_alarm: bool` (`True` when
       `B` mean points is strictly below `A` mean points in any category or overall), and a list of per-task `B - A`
       diffs sorted worst-first so harness regressions are immediately actionable;
     - **Parity ratios**: `B / A`, and when Arm C is provided, `C / A` and north-star `C / max(A, B)` per category and
       overall, with delta-method standard error (`ratio * sqrt((se_num / num)^2 + (se_den / den)^2)` when both means
       are positive; `None` ratio when denominator is zero), 95% confidence interval `[ratio - 1.96 * se, ratio + 1.96 * se]`,
       `ci_includes_one: bool`, and overall `parity_met: bool` (`True` iff every category CI includes `1.0`).
   - Print a clear human-readable summary table (including Gate Zero alarm banner and worst `B - A` task diffs) and
     optionally write the structured report to `--json`.
3. **Atomic scoreboard writer (`--scoreboard`)**:
   - When `--scoreboard <path>` is passed, atomically create or append a row to a plain-Italian Markdown table
     recording date, change label, split, Gate Zero (`B/A`) status, per-category `C/max(A,B)` ratios (or `n/d` when
     Arm C is omitted), and overall ratio.
4. **Offline tests (`backend/tests/test_agent_eval_parity_385.py`)**:
   - Cover per-category `run_eval` output, backwards compatibility with legacy `results.json`, Gate Zero alarm and
     worst-first per-task diff ranking, three-arm parity ratio / delta-method SE / 95% CI / `parity_met` edge cases
     (including zero-denominator handling), `--scoreboard` atomic creation and append, and `vault_guard` refusal.

Out of scope (binding non-goals):
- Running any local model on the host GPU or modifying Strata runtime services.
- Adding private domain tasks to the public repository (private/indicative task packs live outside this repo).
- Changing backend Hermes routing or adding paid API dependencies.

## Files likely touched

- `scripts/benchmarks/agent_eval/runner.py`
- `backend/tests/test_agent_eval_parity_385.py` (new)
- `docs/MODULES.md` (regenerated via `scripts/gen_modules.py` if line counts shift)

## Design constraints

- Standard-library only in `scripts/benchmarks/agent_eval/runner.py`; no new dependencies.
- Vault boundary preserved fail-closed on every CLI path argument via `scripts/vault_guard.py`.
- Never fabricate metrics: zero denominators produce `None` ratios and `ci_includes_one = False`; incomplete runs are
  refused.
- Existing `agent_eval run` and `agent_eval compare` CLI contracts and tests remain untouched and green.

## Acceptance criteria

1. `run_eval` validates optional task `category`, stores `category` per task, and writes `category_summary` in
   `results.json` without breaking existing `run` or `compare` tests.
2. `agent_eval parity --arm-a <A> --arm-b <B> [--arm-c <C>]` computes per-category and overall Gate Zero (`B - A`,
   `gate_zero_alarm`, worst-first per-task diffs) and parity ratios (`B/A`, `C/A`, `C/max(A,B)` with delta-method SE,
   95% CI, and `parity_met`).
3. `--scoreboard <path>` atomically creates/appends a plain-Italian Markdown table row and refuses any vault path with
   `vault_guard.REFUSED_EXIT`.
4. All offline unit tests, ruff, architecture enforcement, doc-paths, spec-status, and CI checks pass.

## Required tests

- `backend/tests/test_agent_eval_parity_385.py`:
  - per-category `run_eval` summary and invalid category rejection;
  - legacy `results.json` without `category` handled as `"uncategorized"`;
  - Gate Zero `B < A` alarm triggers and ranks worst per-task `B - A` diffs first;
  - 3-arm parity ratios, delta-method SE, 95% CI `ci_includes_one`, `parity_met`, and zero-denominator safety;
  - `--scoreboard` atomic create + append and `vault_guard` refusal on all `parity` path flags.

## Definition of done

Merged with green CI on exact head after coordinator full-diff review and all offline tests passing.
