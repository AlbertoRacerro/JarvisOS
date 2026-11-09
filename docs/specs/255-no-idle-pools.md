# 255 — Never idle while any pool can work

Status: merged (worker part). The coordinator scope below was superseded by maintainer directive
`L20261109084653-6274` (coordinators unchanged) and reverted; the agy harness belongs to `L20261109090457-68e4`.
Depends on: none

## Goal

The supervisor keeps coordinating, and coordinators keep delegating, while any subscription pool has room. Coordinator
and worker selection consider every qualified agy model (Argon low/medium/high, Gemini 3.8 Flash low/medium/high, agy
Sonnet and agy Opus on the third-party buckets) beside Claude and Codex, rank pools by the quota that would expire unused,
and choose effort by task, not by model family. A mission turn on agy gets an hour, not 30 minutes.

## Why

Maintainer directive `L20261109084309-afcd` (top priority, supersedes conflicting routing preferences including the
spec 221 rule that kept Argon a worker only under `--coordinator-fallback`): the supervisor sat paused about two days
with Claude, Codex and Argon's own bucket exhausted while about 90% of the general Gemini quota was unused. Model
quality is guaranteed by the gates (CI, spec proofs, browser walk, independent review, risk rules), not by brand.

## Scope

In scope:
- **Coordinator agents** (`scripts/relay_argon.py`, `scripts/relay_continuity.py`). New native agents `flash`
  (`gemini-3.8-flash-{medium,high}`, buckets `gemini-5h`, `gemini-weekly`) and `agy_sonnet`
  (`claude-sonnet-5-5-{low,medium,high}`, buckets `3p-5h`, `3p-weekly`), beside `argon` and `agy_opus`. Native
  coordinators run at `high` effort by default (coordination is design, review and integration); the existing env
  overrides stay. `TURN_TIMEOUT` becomes 3600 s with the same deadline handling. The native rules text tells the agent
  to run long tests in the foreground with their own timeout and to commit and push as a separate short step early.
- **Use-it-or-lose-it coordinator choice.** `_choose` replaces the fixed `PREFERRED_AGENTS` order: among available
  agents it prefers the one whose binding window (the window with least free room) has the highest
  `free_fraction / max(hours_to_reset, 1)`; an unreadable reading ranks last but is still tried (its run decides, as
  today). `low_headroom` uses a 5% reserve, and 0% for a window that resets within 24 hours. A quota wait
  (`_wait_or_hold`) happens only when no agent is available.
- **Workers** (`scripts/worker_routing.py`, `scripts/agy_worker.py`). `MODELS` gains `flash-medium` (tier 2),
  `flash-high` (tier 3), `argon-medium` (tier 2), `argon-high` (tier 3) on the `gemini` pool (Argon entries also need
  `gemini-argon-wtu-weekly`, read as a separate `argon` pool), and `agy-sonnet` (tier 3, high) and `agy-opus` (tier 3,
  high) on a new `agy3p` pool (`3p-5h`, `3p-weekly`). `agy_worker.py` allowlists exactly these ids under aliases of the
  same names; `argon` no longer requires `--coordinator-fallback`. Effort follows the task: the existing "smallest
  capability surplus first" order picks low for tier 1, medium for tier 2, high for tier 3. Among equally adequate
  entries the gemini pool stays first; the others follow the use-it-or-lose-it score instead of the fixed
  `POOL_PRIORITY` tail. Reserves: 5%, 0% for a window resetting within 24 hours.
- **Independent review.** `pick --review-of FAMILY` (families: `claude`, `codex`, `gemini`, `copilot`) orders entries of
  another family first, then a different model of the same family; it never returns `no_worker` only because every
  pool with room is the author's family.
- **Governance.** `docs/PROCESS.md` (delegating paragraph) and a new ADR in `docs/DECISIONS.md` record the rules: no
  model-identity bias, never idle while a pool can work, use it or lose it, effort by task, review by another family
  without blocking on it. The `worker_routing.py` docstring and summary line show the new models and tiers.

Out of scope (binding non-goals):
- Rule 7 measurement (per-turn agent/model/effort, CI first pass, findings, rework, merged; weekly recompute; Italian
  report) is the next slice of this directive.
- No change to safety gates, risk rules, review requirements, sandbox flags or the lease. No new dependency or service.
- `gpt-oss-120b-medium` is not added (no directive). Gemini 3.1 Pro is added only when agy lists it.

## Files likely touched

- `scripts/relay_argon.py`, `scripts/relay_continuity.py`, `scripts/worker_routing.py`, `scripts/agy_worker.py`
- `docs/PROCESS.md`, `docs/DECISIONS.md`
- `backend/tests/test_relay_argon*.py`, `backend/tests/test_relay_continuity.py`, `backend/tests/test_worker_routing.py`,
  `backend/tests/test_agy_worker.py` (or the existing files covering them)

## Design constraints

- Every native coordinator keeps the fail-closed quota preflight, the mission-sandbox check and the coordinator lease.
- Model ids are allowlisted per agent; an env override outside the allowlist is refused, as today.
- A missing or unreadable bucket never counts as room.
- Selection is deterministic for a given reading and clock.

## Acceptance criteria

1. With Claude and Codex at 0% and `gemini-argon-wtu-weekly` at 0%, a fake-host mission tick chooses `flash` (or
   `agy_sonnet`/`agy_opus` when Gemini is also at 0%), launches it and settles a successful turn; it waits only when
   every pool is out.
2. With two agents available, `_choose` prefers the one whose quota would expire unused first (e.g. 40% free resetting
   in 6 h beats 70% free resetting in 6 days).
3. `worker_routing.py summary` lists the `agy3p` and `argon` pools; `pick --difficulty medium --risk high` returns
   `flash-high` when Gemini has room, and `argon-high` or `agy-*` when only those pools do.
4. `pick --difficulty low --risk low` still returns `flash` (low effort); medium/medium returns a tier-2 entry.
5. `pick --review-of gemini` prefers a non-Gemini entry with room, else a different Gemini model.
6. `agy_worker.py run --model argon-high` runs without `--coordinator-fallback`; an unlisted id is refused.
7. `relay_argon.TURN_TIMEOUT == 3600` and the native rules text contains the foreground-test and early-push guidance.
8. A window resetting within 24 hours at 2% free is usable; one resetting in 5 days at 4% free is below reserve.
9. PROCESS.md and DECISIONS.md state the rules; existing tests pass, updated only where they asserted the old order,
   allowlist or timeout.

## Required tests

- Offline tests with the existing fake host, fake quota readings and fake agy binary; one per acceptance criterion.

## Definition of done

High risk (routing and mission-owner selection): relevant tests and lint, CI green on the exact head, a full diff review
by a second qualified session, then live activation after merge (the maintainer's chat session reinstalls host-ops and
restarts the watcher and mission owner) and the first real mission turn on a Gemini or 3p coordinator recorded.
