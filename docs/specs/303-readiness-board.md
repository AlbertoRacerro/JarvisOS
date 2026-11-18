# 303 — READINESS-BOARD-1: A read-only readiness board of literature cases, first reading as the map of what is missing

Status: merged
Depends on: none

## Goal

One deterministic, read-only board says, for each acceptance literature case declared by the installed pack, which of
seven steps is green and which is red, and for every red cell names what is missing. Steps: paper loaded; numbers as
accepted, located facts; model inputs bound to those facts; a run with checks and a usable verdict; comparison with the
published value; a decision linked to a run; a later question answered with value and source. The board is computed on
read from records the product already stores; it creates, changes and runs nothing. A command prints it for the
supervisor's status line; a later slice gives it a screen.

## Why

Directives L20261116154657-88a1 and L20261116155611-930c: the owner starts using the product when all nine literature
cases are done end to end, and the board is first of all the map of what is missing per case, then the test to pass.
Today nobody can say, from the product, which step of which case works. The cases themselves are private pack content;
the board mechanism is generic and runs without a pack (empty board).

## Code facts checked at `be3d42e1`

- `backend/app/core/packs.py::read_pack` accepts only the keys in `_KNOWN_KEYS` (`id`, `workspace`, `sensitivity`,
  `ui_labels`, `studio`, `hermes_roles`); `backend/app/core/packs.py::studio_example_dirs` is the existing pattern for a
  pack-relative path resolved inside the pack directory.
- `backend/app/core/literature_schema.py`: `literature_sources` (`title`, `artifact_id`, `state`) and
  `literature_entries` (`entry_kind` in `claim`/`datum`, `status` in `raw`/`review`/`accepted`, `quantity`,
  `value_number`, `unit`, `locator_kind`, `locator_start`, `locator_end`).
- `backend/app/modules/memory/literature_checks.py::check_datum` returns the deterministic datum checks and verdict
  (`usable`, `usable_with_warnings`, `unchecked`, `not_usable`) given the backing lines.
- Binding by reference already exists as data: a parameter or assumption bound to a datum carries
  `source_ref = "literature_entry:<id>"` (`backend/app/modules/memory/literature_service.py::_used_by`).
- `backend/app/modules/engineering/bench_runs_view.py::list_runs` lists a workspace's bench runs with `evaluator_id`,
  `status` and `verdict`; the retrieval measure is the bench evaluator `retrieval.eval`
  (`backend/app/modules/ai/retrieval_bench.py::retrieval_eval_descriptor`).
- `decisions` has `status` and `linked_run_id`. No product object compares a result with a published value (searched
  `backend/app` for published/reference value comparison; none found).

## Scope

In scope:
- An optional pack key `readiness` with `cases = "<relative file>"`, a TOML file inside the pack directory listing
  cases: `id`, `title`, `workspace` (slug), `source_title`, `facts` (quantity names), `inputs` (parameter names that must
  be bound), `compare` (`quantity`, `value`, `unit`, `rel_tol`). Invalid entries are reported as board errors, never
  silently dropped.
- A backend read model computing, per case and step, `green` or `red` with reason codes and named missing items:
  1. `paper`: a source in the case workspace whose title equals `source_title` with a readable backing file.
  2. `facts`: every quantity in `facts` has an `accepted` datum of that source whose `check_datum` verdict is `usable`
     or `usable_with_warnings`.
  3. `bound`: every name in `inputs` is a parameter whose `source_ref` names an accepted datum of that source.
  4. `run`: a bench run in the workspace with verdict `usable` or `usable_with_warnings` (first reading: any run of the
     workspace counts; tying a run to its case is left to the walk slice).
  5. `compare`: red with `capability_missing` until a comparison object exists (later spec).
  6. `decide`: an `accepted` decision with a `linked_run_id`.
  7. `answer`: a `retrieval.eval` bench run in the workspace with verdict `usable`.
  A missing workspace makes every cell red with `workspace_missing`.
- A read-only route `GET /readiness` (no workspace prefix: the board spans the cases' workspaces) returning the board, and `scripts/readiness_board.py` printing it as a table and
  a one-line summary (`n/m green` with m = 7 × cases, leftmost red cell) for the status line, against `JARVISOS_DATA_ROOT` read-only.

Out of scope: the screen (front-end plan slice), the walk script that drives the interface, the comparison object, the
"twice from a clean install" rule, the guided path bar and next card (directive L20261116201429-63ee), any write.

## Files likely touched

- `backend/app/core/packs.py` (the `readiness` key), `backend/app/modules/engineering/readiness_board.py` (new) and its
  route registration, `scripts/readiness_board.py` (new).
- `backend/tests/test_readiness_board_303.py` (new); a case file in `backend/tests/fixtures/packs/zephyr`.
- `frontend/src/api/generated` and `docs/MODULES.md` regenerated if the route or module listing changes them.

## Design constraints

- Read-only: no table, column, migration or write; database opened the way existing read models open it.
- Deterministic: no model call; a cell is green only by the stated condition, so a model cannot turn a cell green.
- No case content in the repository: tests use the fake pack; the public code names no case.
- Bounded: at most 20 cases; facts and inputs at most 50 names per case; backing files read through
  `backend/app/modules/memory/literature_service.py::_read_backing_lines` (made public if needed), not new parsing.
- Without a pack, or with a pack without `readiness`, the board is empty and the command says so.

## Acceptance criteria

1. With the fake pack's case file and a seeded workspace, each of the seven cells turns green exactly when its condition
   holds and red otherwise, with the named missing quantity, input, or record kind.
2. A datum that is `accepted` but fails `check_datum` keeps `facts` red and names it.
3. A parameter bound to a `raw` datum, or to another source's datum, keeps `bound` red.
4. `compare` is red with `capability_missing` for every case.
5. Invalid case entries appear as board errors; valid cases are still computed.
6. No pack or no `readiness` key: empty board, route 200, command exit 0 with a plain message.
7. The route and the command change nothing in the data root (file digests and row counts unchanged in a test).

## Required tests

- `backend/tests/test_readiness_board_303.py` covering criteria 1–7, offline, fake pack only.

## Definition of done

Lint and focused tests pass; generated files current; CI green on the exact head; one severe review of the diff
(medium risk); STATUS row 303 moved to merged by the implementation PR.
