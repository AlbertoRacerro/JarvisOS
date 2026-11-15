# 274 — Supervisor state written by an older release still loads

Status: ready
Depends on: none

## Goal

A supervisor release that adds a key to its durable state never reads the states written by an earlier release as
corrupt. A release whose mission queue it cannot read is not confirmed as healthy after a self-install.

## Code facts checked on `origin/master` at `93b7a26d`

- `scripts/relay_continuity.py::Store.load` parses `state.json`, checks schema and phase, then raises `StateCorrupt` for
  any problem `scripts/relay_continuity.py::validate_state` returns. `validate_state` returns `missing <key>` for every
  key of `scripts/relay_continuity.py::STATE_TYPES` absent from the file. `wait`, `resumes` and `last_checkpoint_at`
  (spec 267) are in `STATE_TYPES` and in `scripts/relay_continuity.py::default_state`; no state written before spec 267
  has them, so every such mission store loads as corrupt.
- `scripts/relay_continuity.py::mission_status` turns `StateCorrupt` into `corrupt`, and
  `scripts/relay_continuity.py::serve_step` then returns `corrupt:<id>` for the oldest mission and holds the whole queue.
- `scripts/relay_continuity.py::command_watch` and `scripts/relay_continuity.py::command_serve` replace a state that
  raises `StateCorrupt` with `default_state()` silently, which drops the watcher's sent-alert history and the owner's
  serve fields. `command_serve` writes `release_sha` and `release_updated_at` into the serve store before its first
  `serve_step`.
- `scripts/host_ops.py::Executor.maybe_confirm_release` confirms an installed release once the serve store reports the
  candidate `release_sha` with `release_updated_at` after the owner start; past the 10-minute deadline it records the
  release unhealthy and the guard returns to the previous one.

## Scope

1. `Store.load` adds each key of `ADDED_STATE_KEYS` (the keys of `default_state()` added after the first schema:
   `parked_turns`, `wait`, `resumes`, `last_checkpoint_at`, `network_outage`) that is absent from the file, with its
   default value, before `validate_state`. Any other absent key, and a present key with a bad type or value, is still
   corrupt: those keys exist in every state since the first schema, and filling one (`cooldowns`, `hold`,
   `permissions_pinned`) would drop a guard.
2. `command_serve` reports `release_sha`/`release_updated_at` only when no mission of the queue is unreadable: every
   mission definition loads, no mission store loads as corrupt, and no open mission's state mismatches its definition
   (the checks for which `serve_step` returns `corrupt`, shared through `mission_mismatch`). Until then
   it logs the first unreadable mission and retries at each loop iteration, so a fix made during the deadline still
   confirms the release.
3. `command_watch` and `command_serve` log one line naming the store and the problem when they replace a corrupt state
   with the default.
4. The runbook states the rule: a new state key always comes with its default in `default_state()` and its name in
   `ADDED_STATE_KEYS`, which `Store.load` fills on load; the release health check includes a readable mission queue.

## Non-goals

- Migrating or rewriting state files on disk, a schema version bump, changes to the host executor or the guard.
- Recovering a state that is corrupt for another reason.

## Acceptance criteria

1. A mission state file written before spec 267 (synthetic fixture matching the pre-267 schema, without `wait`, `resumes`,
   `last_checkpoint_at`, `parked_turns`, `network_outage`) loads, with those keys at their defaults, and
   `serve_step` does not return `corrupt` for it.
2. A state whose present key has a bad type still raises `StateCorrupt`.
3. With one corrupt mission store in the queue, `command_serve` does not write `release_sha`; with a readable queue it
   does (the existing test keeps passing).

## Evidence

Focused offline tests in `backend/tests/test_relay_continuity.py` and
`backend/tests/test_relay_continuity_owner_release.py`, targeted Ruff, exact-head CI. Supervisor code: a second-session
review of the full diff before merge. The live proof is the owner restarted on the merged release loading the
existing queue.
