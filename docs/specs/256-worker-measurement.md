# 256 — Worker outcomes decide escalation and capability

Status: ready
Depends on: 255

## Goal

Every delegated worker task leaves one outcome record. The router uses those records: it escalates a task away from a
family whose attempt on it failed its checks, and after enough evidence it stops routing a task kind to a model that is
measurably worse than the alternative. A short report in plain Italian tells the maintainer how much Claude and GPT
worker spend Gemini saved and where it needed escalation.

## Why

Maintainer directive `L20261109084653-6274`, rule 4, with conditions (b) and (c) of rule 1, plus `L20261109085335-06cf`
(one Flash attempt per task, honest net cost, wait rather than loop) and `L20261109090114-5e79` item 3 (salvage before
restart). Spec 255 made Gemini the first worker at every tier. Escalation and the measured record are what keep that
honest.

## Scope

In scope (`scripts/worker_routing.py`):
- **`record`** appends one JSON line to `worker-outcomes.jsonl` beside the quota snapshot (main checkout
  `.agent-relay/`). The line holds: `at`, `task` (an id the coordinator chooses, e.g. `255-review`), `kind` (one of
  `docs`, `tests`, `ui`, `feature`, `review`, `fix`), `worker`, `model`, `tier`, `ci_first_pass` (`pass`, `fail` or
  `n/a`), `findings` (counts P0 to P3), `rework_rounds`, `escalated` (bool), `salvage` (`salvaged`, `restarted` or
  `n/a`, with a `reason` when it is `restarted`), `failure` (`none`, `harness` or `model`) and `coordinator_tokens`
  (optional int). Atomic appends; invalid input is refused with a message.
- **`pick --task ID --kind K`**
  - (c) Escalation: if the record has a `failure: model` line for the same task, entries of that line's family are
    excluded (never a second attempt by that family on that task). If nothing else qualifies, the result is
    `no_worker` with reason `wait_for_escalation_pool` (wait, do not loop).
  - (b) Measured record: a model is skipped for kind K, with reason `measured_inadequate`, when it has at least 5 records
    of kind K, a first-pass rate under 50%, and a mean of rework rounds plus P0/P1 findings above the mean of the next
    eligible family's records for K. With fewer records, the default Gemini-first order holds.
  - `harness` failures never count toward (b) or (c).
- **`report`**: weekly by default (`--days N`). For each model: tasks, first-pass rate, findings by severity, rework,
  escalations. Then the net Claude and GPT worker tasks avoided (Gemini tasks not escalated), minus the coordinator
  tokens recorded on them. Plain Italian, a few lines.
- `docs/PROCESS.md` delegating paragraph: record every worker outcome; on a model failure, escalate with salvage (the
  next worker starts from the failed branch or diff plus the findings, unless the coordinator records why the approach
  is wrong).

Out of scope (binding non-goals):
- The agy harness classification and `agy_worker.py` failure handling: that is the maintainer chat session's PR #66
  (`L20261109090457-68e4`). This slice only reads the `failure` the coordinator records.
- Coordinators, supervisor and relay code. No new service, schema, dependency or UI.

## Files likely touched

- `scripts/worker_routing.py`, `backend/tests/test_worker_routing.py`, `docs/PROCESS.md`

## Design constraints

- Stdlib only; the record is append-only and the router only reads it. An unreadable or malformed line is skipped
  and counted, never fatal.
- No secrets or task text in the record: ids, enums and numbers only.

## Acceptance criteria

1. `record` writes a valid line and refuses an unknown `kind`, `failure`, `salvage` or a `restarted` without a `reason`.
2. After a `failure: model` record by `flash-high` on task T, `pick --task T` returns no Gemini entry. When only Gemini
   has room it returns `no_worker` with reason `wait_for_escalation_pool`.
3. A `failure: harness` record on task T leaves `pick --task T` unchanged.
4. With 5 `feature` records for `flash-medium` at 40% first pass and worse rework than `terra`'s, `pick --kind feature`
   at tier 2 skips `flash-medium` as `measured_inadequate`. With 4 records it does not.
5. `report --days 7` prints the per-model lines and the net saving in Italian from a fixture record.
6. Existing routing tests pass unchanged.

## Required tests

- Offline tests in `backend/tests/test_worker_routing.py` with a temporary record file.

## Definition of done

Medium risk (routing tooling): relevant tests and lint, CI green on the exact head, and one severe review of the diff.
