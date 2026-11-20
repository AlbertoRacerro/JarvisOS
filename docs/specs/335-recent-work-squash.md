# 335 — RECENT-WORK-SQUASH-1: Recent work lists squash-merged pull requests

Status: ready
Depends on: 164

## Goal

The Coding > Runtime "Recent work" list shows every pull request merged into the target ref, whatever the merge
method: merge commits as today, and squash commits whose subject ends with `(#N)`. A squash item names its PR number,
a kind read from the conventional-commit type and the specs its subject names, joined to the registry as today.

## Why

Directive `L20261120131328-08f6` item 1: since 16 Nov pull requests are squash-merged, so `git log --first-parent
--merges` skips them and the list ends at the last merge commit; the maintainer no longer sees what was done. The
"Up next" plan view (item 2 of that directive) is a later slice.

## Code facts checked at `5ab62e2b`

- `backend/app/modules/coding/control_room.py::ControlRoomProjection.project` runs `git log --first-parent --merges
  --max-count=RECENT_WORK_LIMIT --format=%H%x00%cI%x00%s <sha> --` with output capped at
  `rt.MAX_PROBE_OUTPUT_BYTES` (65 536) and passes the text to `recent_work_items`.
- `backend/app/modules/coding/control_room.py::recent_work_items` turns each `sha\0date\0subject` line into an item
  through `parse_merge_subject` and joins `spec_ids` to the registry with `_registry_entry`; an item with no spec ids
  is `maintenance`.
- `backend/app/modules/coding/control_room.py::parse_merge_subject` reads PR number, branch, kind (`KIND_LABELS` on
  the branch head) and spec ids (`branch_spec_ids`) from `Merge pull request #N from <owner>/<branch>`; any other
  subject gets kind `other`, no branch, no spec ids and the first `#N` as PR number.
- `backend/app/modules/coding/control_room.py::RECENT_WORK_LIMIT` is 20.
- `frontend/src/pages/CodingWorkbench.tsx::RecentWorkRow` renders `branch ?? "Not derivable"`, so an item without a
  branch already renders.

## Scope

In scope:
- Read first-parent history without `--merges`, up to `3 * RECENT_WORK_LIMIT` commits, and keep only pull-request
  commits (a merge-commit subject as today, or a subject ending with `(#N)`), newest first, at most
  `RECENT_WORK_LIMIT` items. Other first-parent commits (direct pushes, the seed commit) are not listed.
- For a squash subject: `pr_number` is the trailing `(#N)`; `branch` is `None`; `kind` from the conventional-commit
  type before `(` or `:` (`feat` → `implementation`, `fix` → `repair`, `docs` → `docs`, anything else → `other`);
  `spec_ids` are the spec tokens written after the word `spec` or `specs` anywhere in the subject (`(spec 333)`,
  `add ready spec 334`, `mark spec 333 merged`), in order, without duplicates.
- Regression tests with a squash-merge history.

Out of scope (binding non-goals):
- The "Up next" plan view, notification graphics, any new API field or frontend change.
- Calling GitHub: the list stays derived from local git history at the target ref.

## Files likely touched

- `backend/app/modules/coding/control_room.py`
- `backend/tests/test_coding_control_room_164.py`

## Design constraints

- Merge-commit subjects keep exactly today's parsing and results (existing parametrised cases unchanged).
- Only first-parent commits count; a squash subject inside a side branch never appears.
- Response shape unchanged: same item keys, `branch` may be `None` (already allowed).
- Fail closed as today: git errors give `recent_work_unavailable:<code>`; malformed lines give
  `recent_work_line_malformed`.

## Acceptance criteria

1. On a history with merge commits followed by squash commits, `project` lists both kinds newest first, with the
   squash items' PR numbers, kinds and spec ids as defined above, and registry titles/statuses joined.
2. A first-parent commit without a PR subject is not listed; with more than `RECENT_WORK_LIMIT` PR commits, exactly
   `RECENT_WORK_LIMIT` items are returned.
3. `parse_merge_subject` on `feat(supervisor): temporary worker pools (spec 333) (#228)` gives PR 228, kind
   `implementation`, spec ids `["333"]`, branch `None`; on `docs: mark spec 333 merged (#228); park X (#229)` gives
   PR 229, kind `docs`, spec ids `["333"]`.
4. The existing control-room tests pass unchanged.
5. A browser walk on a data-root copy shows the Recent work list on Coding > Runtime with items after 16 Nov (specs
   from 285 on) from a clean profile at `/`.

## Required tests

- Extend the `repo` fixture (or add one) with squash commits on `main` whose subjects end with `(#N)`, one naming a
  spec in the registry, plus a direct commit with no PR; assert criteria 1 and 2.
- Parametrised `parse_merge_subject` cases for criterion 3 and a non-conventional squash subject (`Update readme
  (#5)` → kind `other`, no spec ids).

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, spec status updated, summary written.
