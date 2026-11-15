# 265 — OWNER-SPLIT-1: split the mission owner into modules, behaviour unchanged

Status: ready
Depends on: 264

## Goal

`scripts/relay_continuity.py` (about 6 250 lines) becomes a small entry point plus sibling modules, one per
responsibility that the file already marks with its own section comments. Behaviour, command line, journal and state
formats are unchanged; the existing tests are the safety net.

## Why

Maintainer directives `L20261113011123-36ed` and `L20261113093830-1603`. Every later supervisor change (session
continuity, bounded turns, coordinator choice) edits this file, and a coordinator cannot read 250 KB before changing
it. A split along the existing section boundaries lets a session read only the part it changes.

## Code facts (rechecked at `9b1f4b39`)

- `scripts/relay_continuity.py::classify_turn`, `scripts/relay_continuity.py::Store`,
  `scripts/relay_continuity.py::Notifier`, `scripts/relay_continuity.py::Supervisor` and
  `scripts/relay_continuity.py::serve_step` sit under section comments that mark the existing boundaries for turn
  classification, state, notification, supervision and missions. The file also marks probes, adapters, watch, CLI and
  roadmap continuation.
- `scripts/host_ops.py` `UNIT_TEMPLATES` starts the owner through the sandbox without `-I` and the watcher with `-I`.
  `scripts/relay_continuity.py` already loads
  `worker_routing.py` by file path, and `scripts/relay_continuity.py::load_observe` loads `relay_observe.py` that way.
  New siblings must work in both execution modes.
- `scripts/host_ops.py::_preflight_release` requires a fixed list of script files.
- `backend/tests/test_relay_continuity.py` loads the module by file path as `rc` and patches 71 module attributes.
  A patch on `rc` does not reach a function moved to a sibling that reads the name from its own globals.

## Scope

In scope:

1. New sibling modules under `scripts/` (names are the implementer's, prefixed `relay_continuity_` or a
   `relay_continuity_*.py` family) holding whole sections moved verbatim; `relay_continuity.py` keeps the entry point
   and re-exports every public and test-used name so `rc.<name>` still resolves.
2. Siblings load by file path in the existing `worker_routing` pattern, so `python3 -I scripts/relay_continuity.py`
   and `relay_observe.py`'s loader keep working.
3. Where a test patches `rc.<name>` and the name is read inside a moved section, the test's patch target is changed to
   the module that reads it. No assertion changes.
4. `_preflight_release` requires the new files too.

Out of scope (binding non-goals):

- Any behaviour, wording, journal, state, CLI or prompt change. Any rename of a public function.
- Splitting `host_ops.py` or the test file (a later change may split the tests along the same lines).
- New abstractions, classes or indirection beyond moving code and loading modules.

## Files likely touched

- `scripts/relay_continuity.py` and new `scripts/relay_continuity_*.py`
- `scripts/host_ops.py` (`_preflight_release` list only)
- `backend/tests/test_relay_continuity.py` (patch targets only), `backend/tests/test_host_ops.py` if its fixtures list
  release files

## Design constraints

- Moved code is byte-identical except imports; review with `git diff --color-moved`.
- No import cycle between siblings; the entry point imports the siblings, not the reverse, except through
  `sys.modules` registration that already exists.
- The entry point stays under about 800 lines.

## Acceptance criteria

1. `backend/tests/test_relay_continuity.py`, `test_relay_observe.py`, `test_host_ops.py`, `test_relay_jobs_recovery.py`
   pass with only patch-target edits; no test is removed, skipped or loosened.
2. `python3 -I scripts/relay_continuity.py --help` and each subcommand's `--help` print exactly as before.
3. `git diff --color-moved=zebra` shows the sections as moved, not rewritten.
4. A test fails if any `relay_continuity_*.py` file is missing from `_preflight_release`.

## Required tests

- The existing suites above, plus the preflight-list test (criterion 4) and a subprocess test running the entry point
  with `-I` from another working directory.

## Definition of done

Test gate green, full diff reviewed by a second session (risk high: supervisor code, mechanical), row `merged`.
Live after the next release install.
