# 336 — UP-NEXT-PLAN-1: Up next shows the coordinator's plan in plain Italian

Status: in_review
Depends on: 335

## Goal

The Coding > Runtime "Up next" area opens with the plan in force: the steps of the maintainer's current goal in their
order, each marked done, in progress or next (or later), in plain Italian, with the specs that close it and their
registry status. The plan comes from a private file the coordinator keeps up to date; the STATUS rows stay below it.

## Why

Directive `L20261120131328-08f6` item 2 and directive `L20261120133717-6f0b` (show the research path in "Up next"): specs
are now written one at a time, so "Up next" shows at most one ready row and the maintainer cannot see the path ahead.
The plan wording is private and changes each cycle, so it lives in the control directory, not in the repository.

## Code facts checked at `432a04bc`

- `backend/app/modules/coding/runtime_routes.py::control_room` returns `{repository, target_ref, runtime,
  **projection}` where `projection` comes from `ControlRoomProjection.project` and carries `warnings`.
- `backend/app/modules/coding/control_room.py::ControlRoomProjection.project` parses the registry at the target ref
  with `parse_registry` (rows keyed by spec id, with `status` and `name`).
- `backend/app/modules/coding/relay_monitor.py::control_root` gives the private control directory
  (`relay_continuity.control_root()`), already read by the Runtime relay panels.
- `frontend/src/api/coding.ts::ControlRoom` is the response type; `frontend/src/pages/CodingWorkbench.tsx` renders the
  `Up next` section (`aria-label="Up next"`).

## Scope

In scope:
- A private file `<control_root>/plan.json`, written by the coordinator (by hand or script), never in the repository:
  `{"schema": "plan.v1", "updated_at": "<ISO-8601>", "goal": "<text>", "steps": [{"title": "<text>",
  "state": "done" | "in_progress" | "next" | "later", "note": "<text, optional>", "specs": ["<spec id>", ...]}]}`.
- Backend: `control_room` adds `plan`: `{"status": "available", "goal", "updated_at", "steps": [{title, state, note,
  specs: [{spec_id, title, status}]}]}` with spec titles/statuses joined from the registry at the same ref as `Up
  next`; `{"status": "absent"}` when the file does not exist; `{"status": "invalid"}` plus warning `plan_invalid`
  when it cannot be read or does not match the shape. Limits: file at most 64 KiB, at most 12 steps, `goal`/`title`
  at most 160 characters, `note` at most 400, at most 8 specs per step; anything over a limit makes the file invalid
  (no silent truncation).
- Frontend: inside the `Up next` section, before the STATUS rows, a list `Il percorso` with the goal, then one row per
  step: a state label in Italian (`Fatto`, `In corso`, `Prossimo`, `Dopo`), the title, the note, and spec chips with
  the registry status badge already used by Up next. `absent` shows nothing extra; `invalid` shows one line
  `Piano non leggibile.`
- `docs/PROCESS.md`: one line in the coordinator section: keep `<control_root>/plan.json` true when a directive's
  order or a step's state changes (plain Italian, no internal codes).
- The coordinator writes the first `plan.json` from directive `L20261120133717-6f0b` after merge (not part of the PR).

Out of scope (binding non-goals):
- Editing the plan from the UI; any write endpoint.
- Generating the plan from the ledger or directives automatically.
- Notification graphics (directive item 3).

## Files likely touched

- `backend/app/modules/coding/control_room.py` (plan parsing, pure function taking text and registry)
- `backend/app/modules/coding/runtime_routes.py`
- `backend/tests/test_coding_control_room_164.py` (or a new `test_coding_up_next_plan_336.py`)
- `frontend/src/api/coding.ts`, `frontend/src/pages/CodingWorkbench.tsx`, a frontend static test in `frontend/tests/`
  added to the `build` chain
- `docs/PROCESS.md`
- `e2e/recent_work_walk.mjs` (extend) or a new walk

## Design constraints

- Read-only: the route never writes the plan file; reading follows no symlink outside the control root.
- The plan text is rendered as plain text (no HTML, no Markdown).
- Plan errors never break the rest of the control-room response.
- Tests use a temporary control root; no private plan content in fixtures (generic step titles only).

## Acceptance criteria

1. With a valid `plan.json` the response carries `plan.status == "available"`, steps in file order, and each spec
   joined to the registry title and status; an unknown spec id has `title` and `status` `null`.
2. A missing file gives `plan.status == "absent"` with no warning; a malformed file, wrong schema, unknown state,
   or any limit exceeded gives `plan.status == "invalid"` and warning `plan_invalid`; recent work and Up next are
   unaffected in all three cases.
3. The Runtime page shows `Il percorso` with the four Italian state labels and the spec chips; `invalid` shows
   `Piano non leggibile.`; `absent` shows no plan block (frontend static test).
4. A browser walk on an isolated data root with a generic `plan.json` reaches Coding > Runtime from `/` with visible
   controls only and sees the plan steps in order with their state labels.

## Required tests

- Backend: parsing cases for criteria 1–2 (valid, absent, malformed JSON, wrong schema, unknown state, each limit),
  and a route test with a temporary control root.
- Frontend static test for criterion 3.
- Walk for criterion 4, with evidence.

## Definition of done

Test gate green (see `AGENTS.md`), acceptance criteria met, spec status updated, summary written.
