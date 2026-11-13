# 268 — Bounded turns and provider failure handling

Status: ready
Depends on: 267

## Goal

Coordinator turns and provider recovery are bounded by deterministic limits and fail-closed handling. A Relay-managed
turn has a wall limit instead of an unbounded wait in `RelayAdapter.finish`. Before a launch, one bounded reachability
probe keeps a general network outage from burning a launch and a transient strike on every provider. An
authentication failure excludes that provider and asks the maintainer once, instead of backing off and retrying for
hours. A fresh limits reading that shows quota headroom ends a recorded `limits` or `message` cooldown early (F3),
once, while `estimate`, `manual` and `auth` cooldowns are untouched by readings.

## Why

Supervisor item S7 (with defect F3) of the maintainer's current directive order. A hung external process stalls the
owner indefinitely; a network outage costs one launch and one cooldown per provider; a sign-in failure is a
credential problem only the maintainer can fix, so retrying it wastes hours without telling him; a quota reset the
maintainer made early is ignored until the recorded window ends or he stops the owner to run
`release --clear-cooldown`.

## Code facts checked on `origin/master` at `4637bce1`

- `RelayAdapter.finish` (`scripts/relay_continuity.py`) is a bare `proc.wait()`. `RelayAdapter.start` launches with
  `start_new_session=True`, so each turn is its own process group. Only native turns
  (`scripts/relay_argon.py`, `TURN_TIMEOUT = 3600`) have a limit, and the native prompt states it.
- Measured on this repository's 270 retained Relay turns (`.agent-relay/sessions/*/turns/*`, prompt mtime to last
  file mtime): p95 1380 s, p99 1810 s, max 2552 s. None exceeded 2700 s.
- `Supervisor._launch` checks `host.conflicts` (returns `"waiting"` without writing `pending`) and
  `host.permissions_digest`, then increments `attempts`, writes `pending` and starts the turn. It runs no
  reachability check. `"waiting"` ends `run`, and the owner decides again on its next step.
- `Host` is the injectable boundary (`reap`, `probe`, `conflicts`, `ledger`, ...); tests replace its callables.
- `classify_text` returns `"auth"` on HTTP 401/403 or `AUTH_RE`; `no_terminal_evidence` and the native preflight
  classify (`login_state_missing`, `login_path_conflict`, `agy_missing`, `not_sandbox`) also produce `"auth"`.
  `set_cooldown` sends `"auth"` to its `else` branch: an `estimate` cooldown of `recheck * 2 ** strikes` (30 min up to
  `recheck_cap` 4 h), with a `provider_exhausted` notification and a ledger `finding`, never a question. The hold
  reason `auth_failure` exists in `HOLD_REASONS` but nothing raises it.
- The `retry_same` path keeps short same-agent retries for `transient` outcomes with `harness_cause` in
  `SAME_AGENT_CAUSES` (`provider_network`, `provider_auth_blip`); it never sees `"auth"`.
- `Supervisor.available` returns `False` for any cooldown whose `until` is `None` or in the future, before any
  reading. Only after expiry does it consult `exhausted_until` (Relay readings, `reading_freshness` 15 min,
  `max_reset_ahead`) or `argon_limits` (native, 30 s cache). `exhausted_until` returns `False` only when every window
  is usable and below 99.5 % used.
- `set_cooldown` already keeps a cooldown whose `until` is `None` (a manual cap) until `release --clear-cooldown`.
- Maintainer answers are ledger `decision` entries written by `relay_observe.record_answer` with text
  `Maintainer answer to question <id> (via <source>): <answer>` and `supersedes=[<id>]`.

## Scope

In scope:

1. **Turn wall limit.** `RelayAdapter.finish` waits at most `Limits.turn_wall` (default 3600 s, comfortably above
   the measured maximum). On expiry it sends SIGTERM to the turn's process group, waits up to 30 s, then SIGKILL, and
   returns a `RunResult` whose diagnostics name the wall limit. The turn then goes through the normal `_reap` and
   `_settle` path: it is never `success`, its in-flight effects are listed as INTERRUPTED like any interrupted turn,
   and it sets no provider cooldown (a hung turn is not evidence about the provider). The task text of a Relay turn
   states the limit in one sentence, as the native prompt does.
2. **Pre-launch reachability probe.** A new `Host.reachable` callable (default: TCP connect to `github.com:443`
   with a 5 s timeout; the coordinator needs GitHub for any work, so this adds no new destination) is called in
   `_launch` after the conflict check and before `attempts`, `pending` or any launch. When it fails, `_launch` logs,
   sets phase `waiting`, saves and returns `"waiting"`: no attempt, no strike, no cooldown, no notification per
   probe. A ledger `finding` is written once per outage (on the first failed probe after a successful one), not per
   probe. The existing `max_wait` bound and owner scheduling stay the outer limit.
3. **Auth failure asks once.** For `outcome == "auth"`, `set_cooldown` records a cooldown with `until: None`,
   `source: "auth"`, and the id of one ledger `question` it writes at that moment, in the format of
   `docs/PROCESS.md` "Question format" (Italian, 2 numbered options, recommended first, each on its own line):
   `1.` the maintainer has restored access for that agent, retry it; `2.` keep the agent excluded until he releases
   it. The question names the agent and the classified detail, never credential values or paths. While that
   cooldown exists no further question is written for that agent (restarts and repeated decisions included).
4. **Clearing an auth cooldown.** In `available`, a cooldown with `source: "auth"` is cleared only by (a) an
   accepted `decision` superseding its question id whose answer, after the `: ` separator, begins with `1`, or
   (b) `release --clear-cooldown <agent>` (unchanged). Any other answer leaves the agent excluded. A limits reading
   never clears it.
5. **Fresh reading ends a quota cooldown (F3).** In `available`, when an unexpired cooldown has source `limits` or
   `message`, take a reading (`exhausted_until` for Relay agents, `argon_limits` for native ones). Clear the cooldown
   and return `True` only when the reading shows room (`exhausted_until(agent) is False`, or a native reading with no
   disabled window and every window below 100 %), the reading is newer than the cooldown's `since`, and no earlier
   reading-clear happened for this agent since its last successful turn. Record the clear in state
   (`counters["reading_clear_<agent>"]`, reset on success like the existing strike counters) and with one ledger
   `finding`. A second quota failure after a reading-clear therefore keeps its cooldown to the recorded reset: a
   reading that disagrees with the provider's own failure costs at most one launch.

Out of scope (binding non-goals):

- No credential refresh, token rotation, login automation or secret handling: credentials remain the maintainer's.
- No new dependency, daemon, endpoint or destination; the probe uses the standard library.
- No change to `estimate` or `manual` cooldown behaviour, `retry_same`, the native runner's own `TURN_TIMEOUT`,
  worker routing, task contracts or promotion rules.
- No change to frozen conformance tests (`backend/tests/**/test_*_conformance.py`).
- No change to what a stop request does: `stop` still never kills a running turn; only the wall limit does.

## Files likely touched

- `scripts/relay_continuity.py`
- `backend/tests/test_relay_continuity.py`

## Design constraints

- Rebase on the merged implementation of 267 (it edits the same file); keep 267's wait/resume path intact.
- The wall limit is measured with a monotonic clock in the adapter and is injectable for tests; tests never sleep
  for real minutes.
- Process-group termination uses the group created by `start_new_session=True`; a group that is already gone is not
  an error. `_reap` still runs afterwards and a surviving process still blocks the next launch as today.
- The probe is read-only, at most 5 s, injectable through `Host`, and an exception counts as unreachable.
- The auth question goes through `Supervisor.record` (the ledger callable), so a ledger failure is logged and never
  crashes supervision; if no question id could be recorded, the cooldown still has `until: None` and
  `release --clear-cooldown` remains the way out.
- Cooldown clearing by reading requires the existing freshness and plausibility checks; a missing, stale,
  unparseable or ambiguous reading changes nothing.

## Acceptance criteria

1. A fake Relay turn that runs past an injected wall limit is terminated with its process group, reaped, settled as
   not successful with the wall limit named in the report, and leaves `state["cooldowns"]` unchanged.
2. A failing `Host.reachable` makes `_launch` return `"waiting"` with `attempts`, `pending`, every `transient_*` and
   `estimated_*` counter and `state["cooldowns"]` unchanged and nothing passed to `relay.start`; repeated failing
   probes write one ledger finding; once the probe succeeds the launch proceeds.
3. An `"auth"` outcome writes exactly one ledger `question` whose text `relay_observe.parse_open_questions` accepts
   with two options, records a cooldown with `until: None` and `source: "auth"`, and no further question or retry
   follows on later decisions, restarts or polls.
4. A maintainer answer `1` to that question makes the agent available again; answer `2` or any other text leaves it
   excluded; `release --clear-cooldown` still clears it; a limits reading with room does not.
5. A future `limits` or `message` cooldown with a fresh reading showing room and newer than `since` makes the agent
   available and drops the cooldown, for a Relay agent and for a native agent.
6. After such a clear, a new quota failure records a cooldown that a further reading with room does not clear until
   a successful turn resets the counter.
7. `estimate` and `manual` cooldowns are not cleared by a reading with room; stale, missing, unparseable and
   `exhausted` readings leave `limits` and `message` cooldowns intact.

## Required tests

Focused offline tests in `backend/tests/test_relay_continuity.py`, one or more per acceptance criterion, with fake
processes, fake clocks and fake readings only. The existing `test_relay_continuity.py` suite must stay green in CI.

## Definition of done

The acceptance criteria hold; exact-head CI is green; a severe review and an independent second-session full-diff
review (supervisor code, high risk) pass on the implementation PR; the registry row is set accordingly.
