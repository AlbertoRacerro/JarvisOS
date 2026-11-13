# 268 — Bounded turns and provider failure handling

Status: draft
Depends on: 267

## Goal

Coordinator turns and provider recovery are bounded by deterministic limits and fail-closed handling. Relay-managed turns enforce a per-turn wall limit rather than waiting indefinitely in `RelayAdapter.finish`. Before launching any turn, a single bounded network reachability probe avoids burning launches and transient retry counters across configured providers during general connectivity outages. When a provider fails authentication, the supervisor records a question to the maintainer once instead of looping through exponential estimate cooldowns. When a fresh limits reading confirms quota headroom, contradictory `limits` and `message` cooldowns are cleared immediately while non-quota `estimate` and manual cooldowns are preserved.

## Why

Supervisor execution stalls when external processes hang or when network outages exhaust retry circuits across all providers. Authentication and sign-in failures require maintainer credential intervention rather than automated retry loops. Quota recovery should be recognized as soon as validated readings confirm headroom, avoiding hours of idle waiting while keeping backoff discipline for non-quota failures.

## Code facts checked on `origin/master` at `358cad3c`

- `RelayAdapter.finish` in `scripts/relay_continuity.py` uses a bare `proc.wait()` without a timeout; only native runs via `scripts/relay_argon.py` enforce `TURN_TIMEOUT` (3600s).
- `RelayAdapter.start` launches processes with `start_new_session=True`, placing each run in its own process group (`os.killpg`).
- `Supervisor._launch` performs conflict and permissions checks, then increments `self.attempts += 1` and persists intent to `self.state["pending"]` before starting the process. It executes no reachability probe; a network outage burns coordinator launches across providers, classifying each as `transient`.
- `Supervisor._launch` already executes `reaped = self._reap()` (which invokes `self.host.reap()`) in its `try/finally` block immediately after `self.host.relay.finish(proc)`. `RelayAdapter` has no reference to `Host` and does not reap.
- `classify_text` and `_runner_auth_diagnostic` classify authentication errors as `outcome == "auth"`, but `Supervisor.set_cooldown` falls back to the `else` branch, setting an `estimate` cooldown with exponential backoff (`recheck * 2 ** strikes`) that retries for hours instead of asking the maintainer; nor does the supervisor deduplicate maintainer questions across polling cycles.
- `Supervisor.available` returns `False` immediately when an unexpired cooldown exists in state (`if until is None or now < until: return False`) before inspecting any limits readings, rendering the subsequent cooldown-clearing logic dead code for active cooldowns.
- For external relay agents, quota readings are evaluated via `self.exhausted_until(agent)` (derived from `RelayAdapter.limits()`), where `False` indicates headroom. For native agents (`NATIVE_AGENTS`), quota readings are evaluated via `Host.relay.argon_limits(agent)` (derived from `relay_argon.read_quota`), where a valid reading with no blocked windows indicates headroom.
- Cooldowns with source `estimate` (transient outages, crashes) and `manual` (spend limits) represent non-quota conditions and must remain unaffected by limits readings.

## Scope

In scope:

- Enforce a per-turn wall clock timeout in `RelayAdapter.finish` (default 3600 seconds, matching native coordinator turn budget). When the limit is exceeded, terminate the process group created by `start_new_session=True` (SIGTERM, followed by SIGKILL if needed), collect partial outputs, and return a timed-out run result settled as a failure without blocking supervisor execution. Rely on the existing `self._reap()` in `Supervisor._launch` for host process cleanup; do not duplicate reaping in `RelayAdapter.finish`.
- In `Supervisor._launch`, execute a single bounded read-only reachability probe before recording durable `pending` intent and before incrementing `self.attempts` (host-level network check with a bounded timeout <= 10s). If the probe fails, defer launching, log the condition, set phase to `"waiting"`, and return `"waiting"` without modifying attempt counters, writing a pending launch, incrementing transient strikes, or setting provider cooldowns.
- Handle `outcome == "auth"` by recording a maintainer question once in the ledger (`devctx.py note question` with numbered options adhering to `docs/PROCESS.md` section 7), suppressing automated retries for that provider (avoiding exponential estimate backoff) until credentials are updated or released. Deduplicate the question so subsequent polling cycles while the provider remains unavailable do not record repeated questions.
- In `Supervisor.available`, allow fresh quota headroom evidence to clear an active cooldown when `cooldown.get("source") in ("limits", "message")`:
  - For external relay agents: take a fresh reading via `self.exhausted_until(agent)`. If it returns `False` (fresh reading with valid headroom and no exhausted window), clear the cooldown and treat the agent as available.
  - For native agents (`agent in NATIVE_AGENTS`): take a fresh reading via `Host.relay.argon_limits(agent)`. If it returns a valid dict with windows where none are blocked (`disabled` or `used_percent >= 100`), clear the cooldown and treat the agent as available.
- Retain existing cooldown behavior for `source == "estimate"` and `source == "manual"`; missing, stale, or ambiguous quota readings never clear cooldowns (fail-closed).

Out of scope (binding non-goals):

- No automated credential refresh, token rotation, or secret modification (credential management remains exclusively with the maintainer).
- No new external network infrastructure, daemon processes, or third-party libraries.
- No duplicate process reaping inside `RelayAdapter` (host-level reaping remains solely with `Supervisor._reap` via `Host.reap()`).
- No changes to frozen conformance tests (`backend/tests/**/test_*_conformance.py`).
- No changes to worker routing policies, task validation contracts, or git promotion rules.

## Files likely touched

- `scripts/relay_continuity.py`
- `backend/tests/test_relay_continuity.py`

## Design constraints

- Questions to the maintainer must adhere strictly to `docs/PROCESS.md` section 7: Italian, plain language, no internal acronyms, 2 to 4 options with the recommended one first and marked `(consigliata)`, one line per option on its consequence, `altro` always allowed, and numbered option lines (starting with `1.` or `1)`).
- Reachability checks must be strictly read-only, fast (timeout <= 10s), mockable offline via `Host` (with offline-safe test defaults), and execute prior to pending intent persistence or attempt counter increments. Fail semantics defer to phase `"waiting"` without mutating retry strikes or setting cooldowns.
- Process termination on wall limit expiry must cleanly signal the process group (`os.killpg`) established by `start_new_session=True` without calling `Host.reap()`, allowing `Supervisor._launch`'s existing post-finish `_reap()` to perform clean host-level reaping.
- Quota cooldown clearing requires fresh, validated readings adhering to freshness rules (`reading_freshness` for external, valid unblocked windows for native); stale, unparseable, or missing readings leave existing cooldowns intact. Non-quota cooldowns (`source in ("estimate", "manual")`) are never cleared by quota readings.
- Offline tests must use fake providers and clocks; no paid providers or external network access.

## Acceptance criteria

1. In offline tests, a mocked coordinator turn that hangs past the per-turn wall limit in `RelayAdapter.finish` has its process group terminated and returns a timed-out `RunResult` settled as a failure, after which `Supervisor._launch` runs its existing `_reap` without duplicate reaping or blocking supervisor progress.
2. In offline tests, when the pre-launch reachability probe fails (or times out), `Supervisor._launch` defers launch, enters phase `"waiting"`, leaves attempt counters and `pending` state unwritten, and leaves provider cooldowns and transient strike counters unchanged.
3. In offline tests, a turn with `outcome == "auth"` produces a single structured maintainer question in the ledger adhering to `docs/PROCESS.md` (Italian, numbered options with `(consigliata)` and `altro`), marks the provider unavailable without exponential estimate retries, and avoids duplicate question notes on subsequent supervisor poll cycles.
4. In offline tests, an external relay agent with an active future cooldown of source `limits` or `message` becomes `available` immediately when a fresh limits reading confirms headroom (`exhausted_until(agent) == False`), dropping the cooldown.
5. In offline tests, a native agent with an active future cooldown of source `limits` or `message` becomes `available` immediately when a fresh native limits reading (`Host.relay.argon_limits(agent)`) confirms headroom (valid windows with none blocked), dropping the cooldown.
6. In offline tests, an agent with an active cooldown of source `estimate` or `manual` is not cleared by a fresh quota reading showing headroom.
7. A stale, missing, or unparseable limits reading leaves existing `limits` and `message` cooldowns intact (fail-closed).

## Required tests

- Focused offline supervisor tests in `backend/tests/test_relay_continuity.py`:
  - `test_relay_finish_enforces_per_turn_wall_limit_and_terminates_group`: verify process group termination on timeout, timed-out return result, and post-finish reaping in `_launch`.
  - `test_pre_launch_reachability_probe_defers_without_burning_providers`: verify that network unreachable suspends launch before pending recording without incrementing attempts, transient strikes, or setting cooldowns.
  - `test_auth_failure_asks_maintainer_once_and_avoids_estimate_retries`: verify ledger question formatting adhering to `docs/PROCESS.md`, provider suppression without exponential estimate retries, and deduplication across subsequent polls.
  - `test_fresh_limits_reading_clears_limits_and_message_cooldown`: verify that an external agent cooldown with source `limits` or `message` is cleared when a fresh reading returns `False`.
  - `test_fresh_native_limits_reading_clears_native_cooldown`: verify that a native agent cooldown with source `limits` or `message` is cleared when a fresh `argon_limits` reading shows unblocked headroom.
  - `test_fresh_limits_reading_preserves_estimate_and_manual_cooldown`: verify that `source in ("estimate", "manual")` cooldowns remain in effect despite fresh quota readings showing headroom.
  - `test_unusable_or_stale_limits_reading_leaves_cooldown_intact`: verify that missing, unparseable, or stale readings leave active `limits` and `message` cooldowns in place.
- Exact-head CI, severe diff review, and independent second-session diff review prior to implementation merge (supervisor control flow).

## Definition of done

The acceptance criteria hold and the required evidence is recorded on the implementation PR. The registry reflects the implementation state.
