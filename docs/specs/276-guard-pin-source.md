# 276 — A self-install pins a guard that can run

Status: ready
Depends on: none

## Goal

The first guarded self-install from a release that predates the guard starts the new executor instead of failing at
its pre-start step. An owner that cannot read its mission queue after an install tells the maintainer once.

## Code facts checked on `origin/master` at `e4ae9e5a`

- `scripts/host_ops.py::install_release` pins the guard with
  `source_dir = resolved if installed and resolved and resolved.is_dir() else release`, then
  `scripts/host_ops.py::_pin_guard(paths, source_dir, source_sha)`: the guard copy always comes from the installed
  previous release when there is one.
- The new executor unit runs `ExecStartPre=… guard/scripts/host_ops.py guard`. A previous release whose
  `scripts/host_ops.py` has no `guard` subcommand (every release before spec 262) exits 2 there
  (`invalid choice: 'guard'`), so the unit never starts. This happened on the host on 2026-11-15: the guarded install
  of a green master commit failed at start and the executor came back on the previous release with the old unit.
- The spec 262 criterion-8 proof script installs a BASE that already contains the guard, so it could not see this.
- `scripts/relay_continuity_roadmap.py::command_serve` → `maybe_report_release` (spec 274) withholds `release_sha`
  while `scripts/relay_continuity_missions.py::unreadable_mission` names a mission that `serve_step` would report
  `corrupt`, and logs that once per mission id. Nothing is sent to the maintainer, so a release held for this reason
  waits silently until `Executor.maybe_confirm_release` returns it at the owner-report timeout.

## Scope

1. `install_release` pins the guard from the previous release only when that release can run the guard: running
   `<python> -I <previous>/scripts/host_ops.py guard --help` exits 0 (no other code of the previous release runs). Otherwise
   it pins from the candidate release, writes the candidate sha into the guard's `RELEASE`, and records the reason
   (`previous release has no guard`) in the pending marker and the install log. A first install keeps pinning from the
   candidate.
2. When `maybe_report_release` withholds the release because a mission is unreadable, the owner sends one alert
   through its existing `Notifier` (one per release and mission id, kept across restarts in the serve state).
3. `docs/PROCESS.md` section 5 gains the rule from the maintainer's directive: a supervisor change that reads a file
   written by another program or by another release has one test whose fixture is copied from the real file, and its
   "done" includes one real run in the live owner.

Out of scope: the other findings of the same audit (native turn cut at the wall limit; resume context cap), which
get their own specs.

## Acceptance criteria

1. With a previous release whose `scripts/host_ops.py` has no `guard` subcommand, `install_release` pins the guard
   from the candidate, and running the pinned guard (`guard/scripts/host_ops.py guard`) exits 0 (test).
2. With a previous release that has the guard, the pin still comes from the previous release (test; the existing
   spec 262 tests stay green unchanged).
3. An unreadable mission at owner start produces exactly one alert for that release and mission, also after an owner
   restart (test).
4. **Real-environment proof (high risk).** The spec 262 criterion-8 proof script, adjusted only so that its BASE may
   predate the guard, passes on a temporary home with BASE = the release the host runs today (`5159b8e1`) and a green
   candidate containing this change: the candidate's executor starts and is confirmed. Only after that is the
   maintainer asked once more for the host install, with the exact command.

## Required tests

Offline, in the existing `host_ops` and owner test files, with a previous-release fixture whose `host_ops.py` is the
real pre-262 file reduced to its argument parser (subcommands as in `5159b8e1`, no `guard`).

## Definition of done

Test gate green, CI green on the exact head, full diff review by a second qualified session, and criterion 4 proven.
