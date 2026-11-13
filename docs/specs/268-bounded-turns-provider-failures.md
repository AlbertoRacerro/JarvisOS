# 268 — Bounded turns and provider failure handling

Status: draft
Depends on: 267

## Goal

Coordinator turns and provider recovery are bounded by deterministic limits and fail-closed handling. Relay-managed turns enforce a per-turn wall limit rather than waiting indefinitely in `RelayAdapter.finish`. Before launching any turn, a single bounded network reachability probe avoids burning launches and transient retry counters across configured providers during general connectivity outages. When a provider fails authentication, the supervisor records a question to the maintainer once instead of looping through exponential estimate cooldowns. When a fresh limits reading confirms quota headroom, contradictory `limits` and `message` cooldowns are cleared immediately while non-quota `estimate` and manual cooldowns are preserved.

## Why

Supervisor execution stalls when external processes hang or when network outages exhaust retry circuits across all providers. Authentication and sign-in failures require maintainer credential intervention rather than automated retry loops. Quota recovery should be recognized as soon as validated readings confirm headroom, avoiding hours of idle waiting while keeping backoff discipline for non-quota failures.

## Code facts checked on `origin/master` at `358cad3c`

- `RelayAdapter.finish` in `scripts/relay_continuity.py` uses a bare `proc.wait()` without a timeout; only native runs via `scripts/relay_argon.py` enforce `TURN_TIMEOUT` (3600s).
- `Supervisor._launch` performs conflict, permissions, and session setup before launching, but executes no reachability probe; a network outage burns coordinator launches across providers, classifying each as `transient`.
- `classify_text` and `_runner_auth_diagnostic` classify authentication errors as `outcome == "auth"`, but `Supervisor.set_cooldown` falls back to the `else` branch, setting an `estimate` cooldown with exponential backoff (`recheck * 2 ** strikes`) that retries for hours instead of asking the maintainer.
- `Supervisor.available` returns `False` immediately when an unexpired cooldown exists in state, without checking `exhausted_until(agent)` or native windows; an early quota reset is ignored until the recorded window expires or `release --clear-cooldown` is invoked manually.
- Cooldowns with source `estimate` (transient outages, crashes) and `manual` (spend limits) represent non-quota conditions and must remain unaffected by limits readings.

## Scope

In scope:

- Enforce a per-turn wall clock timeout in `RelayAdapter.finish` (default 3600 seconds, matching native coordinator turn budget). When the limit is exceeded, terminate the process hierarchy, reap surviving processes via `Host.reap()`, and return a timed-out run result settled as a failure without blocking supervisor execution.
- Add a single bounded read-only reachability probe before launching any coordinator turn in `Supervisor._launch` (host-level network check with a bounded timeout <= 10s). If the probe fails, defer launching, enter a waiting state, and avoid incrementing transient strikes or setting provider cooldowns.
- Handle `outcome == "auth"` by recording a maintainer question once in the ledger (`devctx.py note question` with numbered options adhering to `docs/PROCESS.md`), suppressing retries for that provider until credentials are updated or released.
- In `Supervisor.available`, when an active cooldown has source `limits` or `message`, take a fresh limits reading (`exhausted_until(agent)` or native limits). If the reading returns `False` (fresh reading with valid headroom and no exhausted window), clear the cooldown and treat the agent as available.
- Retain existing cooldown behavior for `source == "estimate"` and `source == "manual"`; missing, stale, or ambiguous quota readings never clear cooldowns.

Out of scope (binding non-goals):

- No automated credential refresh, token rotation, or secret modification (credential management remains exclusively with the maintainer).
- No new external network infrastructure, daemon processes, or third-party libraries.
- No changes to frozen conformance tests (`backend/tests/**/test_*_conformance.py`).
- No changes to worker routing policies, task validation contracts, or git promotion rules.

## Files likely touched

- `scripts/relay_continuity.py`
- `backend/tests/test_relay_continuity.py`

## Design constraints

- Questions to the maintainer must follow the format defined in `docs/PROCESS.md` (at least 2 numbered option lines starting with `1.` or `1)`).
- Reachability checks must be strictly read-only, fast (timeout <= 10s), mockable offline via `Host`, and fail-safe.
- Process termination on wall limit expiry must ensure clean process group teardown; surviving child processes must be stopped via `Host.reap()` before settling.
- Quota cooldown clearing requires a fresh, validated reading adhering to `reading_freshness` and `max_reset_ahead`; stale or missing readings do not alter state.
- Offline tests must use fake providers and clocks; no paid providers or external network access.

## Acceptance criteria

1. In offline tests, a mocked coordinator turn that hangs past the per-turn wall limit is terminated, reaped, and settled as a timed-out turn without blocking supervisor progress.
2. In offline tests, when the pre-launch reachability probe fails, `Supervisor._launch` suspends launch, leaves provider cooldowns and transient strike counters unchanged, and resumes once reachability returns.
3. In offline tests, a turn with `outcome == "auth"` produces a single structured maintainer question with valid numbered option lines, marks the provider unavailable without exponential estimate retries, and avoids duplicate questions on subsequent polls.
4. In offline tests, an agent with an active future cooldown of source `limits` or `message` becomes `available` immediately when a fresh limits reading confirms headroom (`exhausted_until` returns `False`), dropping the cooldown.
5. In offline tests, an agent with an active cooldown of source `estimate` or `manual` is not cleared by a fresh quota reading showing headroom.
6. A stale, missing, or unparseable limits reading leaves existing `limits` and `message` cooldowns intact.

## Required tests

- Focused offline supervisor tests in `backend/tests/test_relay_continuity.py`:
  - `test_relay_finish_enforces_per_turn_wall_limit_and_reaps`: verify process termination, timeout return result, and settled report.
  - `test_pre_launch_reachability_probe_defers_without_burning_providers`: verify that network unreachable suspends launch without incrementing transient strikes or setting cooldowns.
  - `test_auth_failure_asks_maintainer_once_and_avoids_estimate_retries`: verify ledger question formatting, provider suppression, and absence of retry loops.
  - `test_fresh_limits_reading_clears_limits_and_message_cooldown`: verify that a cooldown with source `limits` or `message` is cleared when a fresh reading returns `False`.
  - `test_fresh_limits_reading_preserves_estimate_and_manual_cooldown`: verify that `source in ("estimate", "manual")` cooldowns remain in effect despite fresh readings.
- Exact-head CI, severe diff review, and independent second-session diff review prior to implementation merge (supervisor control flow).

## Definition of done

The acceptance criteria hold and the required evidence is recorded on the implementation PR. The registry reflects the implementation state.
