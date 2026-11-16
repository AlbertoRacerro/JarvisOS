# 290 — Monthly worker pool pacing

Status: ready
Depends on: none

## Goal

Worker routing knows when a monthly quota pool resets and prefers that pool, among workers that qualify for the task, while its use is behind the elapsed share of the month, so monthly quota is spent before it expires instead of being lost at the reset.

## Why

The Copilot pool is read but never wins the use-it-or-lose-it ordering: its reset is unknown to the router and counts as a week away, and the family preference puts another pool first. Unused monthly quota is lost at the reset while the short-window pools are below reserve.

## Code facts checked at `b9e2f1ee`

- `scripts/worker_routing.py::parse_copilot` returns a `Pool` with a `monthly` window and no `resets` entry; the entitlement reading carries `quota_reset_date_utc`.
- `scripts/worker_routing.py::expiry_score` counts a window without a known reset as `UNKNOWN_RESET_HOURS` away.
- `scripts/worker_routing.py::rank` orders eligible workers by family difference, capability surplus, `FIRST_POOLS`, `expiry_score`, free quota and table order.
- `scripts/worker_routing.py::below_reserve` already uses `Pool.resets` for the spend-down rule.

## Scope

In scope:
- `parse_copilot` records the `monthly` reset from `quota_reset_date_utc`; when that is missing or invalid, the first instant of the next calendar month in UTC.
- A pacing rule in `rank`: a pool with a `monthly` window and a known reset is "behind pace" when its used fraction (1 − free) is below the elapsed fraction of the month (month start = the reset minus one calendar month). Among eligible workers with the same family-difference and capability-surplus keys, a behind-pace pool sorts before the `FIRST_POOLS` preference.
- The pick output names the pacing reason when it decided the choice.

Out of scope (binding non-goals):
- Changing any model's capability tier, reserves, launch line or the Copilot sandbox limits (no `copilot login`, public code only, no commit by the worker).
- Slack status wording; a measured tier change for the Copilot model.

## Files likely touched

- `scripts/worker_routing.py`
- `backend/tests/test_worker_routing.py`

## Design constraints

- Pure read-and-decide: no new reading call, file or state. Use the reading `read_copilot` already makes.
- Capability is never relaxed: pacing only reorders workers that already qualify.
- Deterministic under an injected `now`.

## Acceptance criteria

1. A Copilot reading with `quota_reset_date_utc` yields `resets["monthly"]` at that instant; without it, the first day of next month UTC.
2. With Copilot 70 % free on day 16 of a 30-day month and a gemini worker also eligible at tier 1, `rank` puts `copilot-auto` first and the output says why; with Copilot 30 % free on the same day (ahead of pace) the existing order is unchanged.
3. At tier 2, where no Copilot model qualifies, the order is unchanged.
4. Existing routing tests pass unchanged.

## Required tests

- Offline unit tests in `backend/tests/test_worker_routing.py` for criteria 1–3 with fixed `now` and readings shaped like the real entitlement response.

## Definition of done

Focused tests and lint pass; CI green on the exact head; one coordinator review of the diff; STATUS records the PR.
