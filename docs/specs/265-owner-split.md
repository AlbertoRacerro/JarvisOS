# 265 — OWNER-SPLIT-1: split the mission owner into modules, behaviour unchanged

Status: ready
Depends on: 264

## Goal

`scripts/relay_continuity.py` (about 4 800 lines) becomes a small entry point plus sibling modules, one per
responsibility that the file already marks with its own section comments. Behaviour, command line, journal and state
formats are unchanged; the existing tests are the safety net.

## Why

Maintainer directives `L20261113011123-36ed` and `L20261113093830-1603`. Every later supervisor change (session
continuity, bounded turns, coordinator choice) edits this file, and a coordinator cannot read 250 KB before changing
it. A split along the existing section boundaries lets a session read only the part it changes.

## Code facts (verified at `2ef5d2b6`)

- Section comments in `scripts/relay_continuity.py`: turn classification (from line ~161), durable state, outbound
  notification, Relay and host adapters, interrupted external operations (probes), supervisor, notify-only watch, CLI,
  missions (spec 201), roadmap continuation (spec 203).
- The host unit runs the owner as `python3 -I .../scripts/relay_continuity.py` (`host_ops.py`); `-I` does not put the
  script's directory on `sys.path`, so siblings are loaded by file path. The file already does this for
  `worker_routing.py` (`importlib.util.spec_from_file_location` registered in `sys.modules`), and `relay_observe.py`
  loads `relay_continuity` the same way.
- A release is a copy of the whole tree; `host_ops._preflight_release` requires a fixed list of script files.
- `backend/tests/test_relay_continuity.py` loads the module by path as `rc` and patches about thirty module attributes
  (`monkeypatch.setattr(rc, "make_host", ...)`, `rc.WEBHOOK_FILE`, `rc.roadmap_step`, ...). A patch on `rc` does not
  reach a function that moved to another module and reads the name from its own globals.

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
