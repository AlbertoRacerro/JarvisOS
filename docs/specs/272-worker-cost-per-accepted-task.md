# 272 — Workers ranked by measured quota per accepted task

Status: ready
Depends on: none

## Goal

Every agy worker run records what it spent: the token usage of the run and the Gemini quota fractions read before and
after it, with a flag when another agy worker run overlapped it. `worker_routing.py record` copies that spend into the
outcome record, and `rank` prefers, among adequate candidates of the same pool, the model with the lowest measured
quota spent per accepted task of the requested kind once it has enough records; without enough records today's order
stands.

## Why

Point 4 of maintainer directive `L20261113192444-38eb` ("i worker devono essere modelli con migliore cost per task").
Today `Model.cost` is a hand-written number the ordering does not use, and the outcome file (23 records on 2026-11-13,
18 of them `flash-high`) holds no spend. Hydrogen joined the gemini pool after Flash at equal tier only because nothing
was measured (point 2); this spec supplies the measurement that may reorder them.

## Code facts checked on `origin/master` at `afdeffbc`

- `scripts/agy_worker.py::_run` takes one no-model-turn `/quota` reading before the run
  (`scripts/agy_worker.py::quota_preflight`, target buckets from `scripts/agy_worker.py::target_buckets`) and stores it
  as `result["quota"]`; it writes the agy JSON stream to `transcript.json` but keeps nothing of its `usage` object.
- `scripts/agy_worker.py::execute` sets `started_at` and `finished_at` in `result.json`.
- The agy stream document has `usage` with `input_tokens`, `output_tokens`, `thinking_tokens`, `cache_read_tokens` and
  `total_tokens` (observed in a `transcript.json` of 2026-11-13, agy 1.5.8).
- `scripts/worker_routing.py::record` appends one JSON line per outcome with `kind`, `worker`, `model`, `tier`,
  `ci_first_pass`, `findings`, `rework_rounds`, `escalated`, `salvage` and `failure`; `scripts/worker_routing.py::cmd_record`
  is its CLI.
- `scripts/worker_routing.py::rank` sorts eligible candidates by (other family than `review_of`, capability surplus,
  gemini/argon pool first, expiring quota, free quota, table order) and skips a model as `measured_inadequate` through
  its nested first-pass check over `outcomes` of the same kind with at least 5 judged records.
- `scripts/worker_routing.py::Model` carries `cost`, documented as informational.

## Scope

In scope:

1. **Spend in the job result.** `agy_worker.py` stores in `result.json`:
   - `usage`: the stream document's `usage` object, keys limited to the five integer token counts above (absent or
     malformed → `null`);
   - `quota_after`: a second no-model-turn `/quota` reading of the same target buckets after the run (refusal or
     unreadable → `null` and the reason in `quota_after_error`; never changes the run status);
   - `overlap`: `true` when any other `agy_worker.py` run was active at any moment between this run's `started_at`
     and `finished_at`, `false` when none was, `null` when it cannot be told. The mechanism is the implementer's
     choice (for example a small append-only start/end log under the evidence root); it must survive a crashed run
     whose end was never written (treated as active until its `MAX_TIMEOUT` plus `KILL_MARGIN` has passed).
2. **Spend in the outcome record.** `worker_routing.py record` accepts `--evidence DIR` one or more times (the
   `result.json` directories of the runs that made the task: the first run and each rework run). It stores
   `spend`: `{"runs": N, "tokens": <sum of total_tokens>, "quota": <sum of the gemini-weekly fraction drop over runs
   that have both readings and overlap false>, "quota_runs": <how many runs contributed>, "overlap_runs": <count>}`.
   A missing or unreadable result is an input error (refused, nothing appended). Records without `--evidence` stay
   valid and have no `spend`.
3. **Measured ordering.** For a candidate model and the requested `kind`, its measured cost per accepted task is the
   median over its records of that kind (failure not `harness`, with `spend`) of: the record's quota spent when every
   run was clean, else its tokens times the model's quota-per-token ratio (sum of clean quota over sum of clean tokens
   across all its records with `spend`; no clean run → that record has no cost). A record whose task was not accepted
   (`failure == "model"` or `escalated`) costs infinity, so rework and failures count. A model is *measured* when it
   has at least 5 costed records of the kind. `rank` first builds today's order; then, per pool,
   the slots held by that pool's measured candidates are refilled with the same candidates sorted by measured cost
   (lower first, ties by today's order). Unmeasured candidates and other pools keep their slots; review-family
   separation is applied before this (a measured candidate never jumps the `review_of` split), and reserve,
   escalation and `measured_inadequate` skips are unchanged. The decision output shows each candidate's measured cost and record count.
4. **`Model.cost`** is deleted (and its column in any output) once nothing reads it; the table comment states that
   order comes from capability, pools and measured spend.

Out of scope (binding non-goals):

- Comparing spend across pools or plans (gemini, argon, agy3p, codex, claude, copilot fractions are different units);
  non-agy workers keep today's order.
- Native agy coordinator turns (`relay_argon.py`) in the overlap check; their spend on the gemini buckets is a known
  source of noise, mitigated by the token fallback and the median.
- Any change to coordinator choice, reserves, spend-down or the `measured_inadequate` rule.
- Backfilling the existing 23 records.

## Files likely touched

- `scripts/agy_worker.py`, `scripts/worker_routing.py`
- `backend/tests/test_agy_worker_221.py`, `backend/tests/test_worker_routing.py`
- `docs/PROCESS.md` section 2 (one line: record with `--evidence` for agy workers)

## Design constraints

- The second `/quota` reading is a no-model-turn read like the preflight; a failure to read it never fails, retries or
  lengthens the worker run beyond `QUOTA_TIMEOUT`.
- `rank` stays pure over its inputs (`pools`, `outcomes`); it reads no evidence files.
- Records stay append-only JSON lines; old records without `spend` load and rank as before.
- No new dependency.

## Acceptance criteria

1. A faked agy run whose stream has `usage` writes `usage`, `quota_after` and `overlap: false` into `result.json`; a
   run whose after-reading fails still succeeds with `quota_after: null` and `quota_after_error` set.
2. Two runs whose active intervals intersect both report `overlap: true`; a run whose start/end entry is missing for a
   peer older than `MAX_TIMEOUT + KILL_MARGIN` is not counted as overlapping.
3. `record --evidence A --evidence B` stores `spend` with the summed tokens, the summed clean quota drop and the run
   counts; an overlapped run adds tokens but no quota; a missing `result.json` is refused and appends nothing.
4. For a tier-2 request of kind `feature`, with 5 or more costed `feature` records for `hydrogen-medium` cheaper than
   5 or more for `flash-medium`, `rank` puts `hydrogen-medium` first; with 4 records for either the order is today's
   (Flash first). (The Hydrogen rows come from PR #103; if it is not merged, use any two gemini-pool models.)
5. A model with 3 accepted cheap records and 3 `failure: model` records of the kind has an infinite median and ranks
   after a measured model with finite median.
6. Candidates in different pools are never reordered against each other by measured cost.
7. `Model.cost` no longer exists, and the test suites for `agy_worker.py` and `worker_routing.py` pass.

## Required tests

- Offline tests with a fake agy binary and fixture `result.json` files covering criteria 1–7; no real `/quota` call.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, STATUS row updated by the implementation PR.
