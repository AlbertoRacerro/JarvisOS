# 243 — Supervisor waste fixes and host-run local evaluation

Status: ready
Depends on: none

## Goal

Coordinator turns stop wasting time and quota on avoidable failures. Recovery at the start of a turn is fast because it
no longer asks GitHub about pull requests from the previous repository. The supervisor does not hand a turn to a
coordinator whose live quota cannot cover it while another coordinator has room. A worker started by a coordinator keeps
running, and its result can be collected, after that coordinator's turn ends. The supervisor can run a registered
local-model evaluation against the live local model server by itself and get back only the scores, so the maintainer
only checks the final result.

## Why

Maintainer directive `L20261105071756-367c` item 3a, from findings `L20261104105919-5625` and `L20261103192956-92b6`.
`devctx.py recover` took 222 s at the start of cycle 327. Most of its `gh` time went on one `gh pr view` per ledger
reference to an old pull request number that is not in this repository's open or merged lists. A coordinator with
about 14 % weekly quota left was chosen, died mid-turn, and its worker had to be run again. Spec 231 acceptance 4 had to
wait days for the maintainer to run an evaluation by hand because its credential must never enter the sandbox.

## Scope

In scope:
1. **Fast recover.** `scripts/devctx.py` stops running per-number `gh pr view` lookups for pull request numbers that are not
   in this repository. A number above the highest number in the fetched open and merged lists is unknown without a
   network call. A reference that cannot be resolved keeps its existing honest "unverified" freshness verdict. It is
   never reported as merged or open.
2. **Coordinator headroom.** `scripts/relay_continuity.py` coordinator choice skips an agent whose fresh, plausible quota
   reading shows less free room than a configured minimum in any window, when another eligible coordinator has room.
   When none has room, current behaviour is unchanged: try the preferred agent, and a real quota failure sets the
   cooldown. A missing or stale reading is still no evidence either way. The minimum is a named limit with a
   conservative default (25 % free in the weekly or longest window).
3. **Durable workers by default.** The launch command that `scripts/worker_routing.py pick` prints for a coordinator runs
   the worker through `scripts/relay_jobs.py start`. The coordinator prompt's worker-routing text says so. If the real
   environment shows that a `relay_jobs` child does not outlive the coordinator's sandbox, the slice fixes that in
   `relay_jobs.py`. The fix must not add a new service.
4. **Host-run local evaluation.** A typed `scripts/host_ops.py` operation runs one registered evaluation at a given
   commit against the live loopback local model server. The first registered evaluation is
   `docs/implementation/evidence/bubu3-pointing/pointing_eval.py` with `--ids old|new` and a label. The host reads the
   server credential from its existing host-side source and passes it only to the evaluator child process. The signed
   receipt returns only the evaluator's result JSON, scrubbed by the existing receipt scrubber. The coordinator commits
   that result as evidence.

Out of scope (binding non-goals):
- No change to `release.install` / `trust_master` policy; host installs stay with the maintainer's host session.
- No new provider, network egress, paid call, product AI path or change to `run_ai_task`. The evaluation stays
  development evidence against the loopback local server.
- No arbitrary command execution through host operations. Only allowlisted evaluators with validated arguments are
  accepted.
- No new daemon, framework or dependency.

## Files likely touched

- `scripts/devctx.py`, `scripts/tests/` (or the existing devctx tests)
- `scripts/relay_continuity.py` and its tests
- `scripts/worker_routing.py`, possibly `scripts/relay_jobs.py`, and their tests
- `scripts/host_ops.py` and its tests; `docs/implementation/evidence/bubu3-pointing/README.md`

## Design constraints

- The credential never appears in a request, receipt, log, ledger entry, commit, PR, prompt or sandbox-visible file. A
  test proves that a receipt from a run whose child output echoes the credential contains no trace of it.
- The host operation fails closed with a stated reason when the evaluator is not registered, the commit is not reachable
  from `origin/master` or an open PR head, or the credential or loopback server is unavailable. It never substitutes
  a dry run or a fake score.
- The evaluator runs from a clean temporary checkout of the requested commit, not from a coordinator worktree. It is
  bounded by a timeout.
- Quota thresholds read the same live readings `worker_routing.py` already uses. No new quota source is added.

## Acceptance criteria

1. With a ledger that references pull request numbers from the previous repository, `devctx.py recover` issues no
   `gh pr view` for them (test with a fake `gh`), and freshness verdicts for those entries stay non-merged/unverified.
   In the real environment, a recover run records its wall time and `gh` call count before and after the change.
2. A test shows coordinator choice skipping an agent below the minimum free room for another agent with room. It also
   shows unchanged behaviour when every agent is below the minimum or readings are missing.
3. `worker_routing.py pick` prints a `relay_jobs.py start ... --` launch line. A real-environment proof shows a worker job
   started in one coordinator turn reported `succeeded` by `relay_jobs.py list` in a later turn.
4. Host-op tests cover allowlist refusal, argument validation, unreachable-commit refusal, missing-credential refusal and
   credential absence from the receipt. A real host run of the bubu3-pointing evaluation at master returns a verified
   receipt whose tally matches the evaluator's own output.

## Required tests

- Offline tests with fake `gh`, fake quota readings, fake host context and a fake evaluator; no live model, no network.

## Definition of done

Test gate green; high-risk evidence per `AGENTS.md` (second-session full diff review and real-environment proof for
items 2–4); STATUS row reconciled.
