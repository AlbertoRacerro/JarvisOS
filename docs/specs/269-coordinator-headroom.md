# 269 — Coordinator choice by headroom, not rotation

Status: ready
Depends on: —

## Goal

The persistent owner keeps a new mission with the coordinator that finished the previous one while that coordinator
is available and has quota room, instead of handing every new mission to the next agent of a fixed ring. When the
current coordinator cannot continue, the replacement is the available preferred agent with the most measured quota
room, not simply the next one in the list.

## Why

Supervisor item S10 of the maintainer's current directive order (after S3, F1, S6 and S7). Roadmap cycles are one
mission each, so the ring in `serve_step` changes coordinator on every cycle even when the last one has plenty of
quota left. Each change costs a cold start (recovery packet, worktree and PR re-reading) and breaks the "one work item
stays with one agent while it has quota" intent of S6. The ring was introduced with the owner itself (`99164198`); no
spec or decision gives a reason for it beyond spreading load, which the headroom checks in `_choose` already do.

## Code facts checked on `origin/master` at `73e1b8a9`

- `serve_step` (`scripts/relay_continuity.py`) remembers `last_completed_agent` from the latest `complete` mission
  and, for a mission with no state yet, seeds `state["agent"]` with the agent after it in
  `PREFERRED_AGENTS = ("claude", "argon", "agy_opus")`, or `PREFERRED` (`"claude"`) when the last agent is not in
  that tuple (for example `codex`).
- `Supervisor.run` starts a mission with a synthetic `success` report whose agent is `None`, so the first launch goes
  through `self._choose(failed_agent)` with `failed_agent = state["agent"]`.
- `_choose(current)` tries `current` first, then the other `PREFERRED_AGENTS` in tuple order, then `codex`; it takes
  the first agent that is `available` and not `low_headroom`, else the first available one. A change of agent records
  a switch, a `provider_switched` notification and a ledger `decision`.
- `low_headroom(agent)` returns `True` only when a usable reading (Relay: `reading`, fresh and with plausible resets;
  native: `argon_limits`) shows less than `Limits.min_free_percent` (25 %) free in some window; a missing or
  unusable reading returns `False`.
- `backend/tests/test_relay_argon_223.py::test_completed_missions_rotate_preference_without_a_new_state_store` pins
  the ring (`claude, argon, agy_opus, claude` over four missions).

## Scope

In scope:

1. **Sticky seed.** In `serve_step`, a mission with no state is seeded with `last_completed_agent` when it is in
   `PREFERRED_AGENTS`, else `PREFERRED`. The ring expression is deleted. `_choose` then keeps that agent while it is
   available and not low on headroom, and moves away only by its existing rules.
2. **Replacement by headroom.** In `_choose`, after `current`, the remaining `PREFERRED_AGENTS` are ordered by their
   measured free room (the smallest `100 - used_percent` over the windows of a usable reading, read through the same
   paths as `low_headroom`), largest first. Agents without a usable reading follow those with one, and ties and the
   no-reading group keep tuple order. `codex` stays the last resort. The "first available if all are low" fallback and
   the switch bookkeeping are unchanged.
3. One shared helper returns the free room (or `None`) so `low_headroom` and the ordering read the same numbers;
   `low_headroom` keeps its current results.

Out of scope (binding non-goals):

- No change to `available`, cooldowns, `retry_same`, the wait/hold path, `PREFERRED_AGENTS` membership, worker routing
  (`scripts/worker_routing.py`) or the mission/task contracts.
- No new reading source, cache, dependency or setting; readings stay at most one per agent per decision as today.
- No change to frozen conformance tests (`backend/tests/**/test_*_conformance.py`).

## Files likely touched

- `scripts/relay_continuity.py`
- `backend/tests/test_relay_argon_223.py` (the ring test is replaced by the sticky-seed tests)
- `backend/tests/test_relay_continuity.py`

## Design constraints

- Rebase on the merged implementations of 267 and 268 if they land first (same file); keep their behaviour intact.
- Readings are the existing injectable `host.relay.limits` and `host.relay.argon_limits`; tests use fakes only.
- A missing, stale, unparseable or implausible reading is never treated as more room than a usable one.

## Acceptance criteria

1. Four completed missions in a row, every agent available with room, all run on the same agent as the first
   (`claude`), and no `provider_switched` notification is sent.
2. A last completed agent outside `PREFERRED_AGENTS` (`codex`) seeds the next mission with `PREFERRED`.
3. When the seeded agent is low on headroom and both others are available with usable readings, the one with more
   free room is chosen, whatever its position in the tuple; with equal room the tuple order decides.
4. An agent with no usable reading is chosen only after every available agent with a usable reading and enough room;
   when no agent has a usable reading the tuple order applies as today.
5. When every available agent is low, the first available one in the headroom ordering is returned, and `codex` is
   chosen only when no preferred agent is available.
6. `low_headroom` returns the same results as before for the existing tests.

## Required tests

Focused offline tests with fake readings and fake clocks: the ring test in `test_relay_argon_223.py` becomes the
criterion 1 test, plus one or more tests per remaining criterion in `test_relay_continuity.py`. The existing relay
suites must stay green in CI.

## Definition of done

The acceptance criteria hold; exact-head CI is green; a severe review and an independent second-session full-diff
review (supervisor code, high risk) pass on the implementation PR; the registry row is set accordingly.
