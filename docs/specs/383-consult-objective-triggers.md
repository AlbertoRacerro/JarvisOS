# 383 — CONSULT-OBJECTIVE-TRIGGERS-1: Objective consult triggers and accept/reject feedback log

Status: ready
Depends on: 382

## Goal

Add deterministic, non-learned objective triggers in the local Hermes harness that prompt Inkling to invoke
`jarvis_consult` when a turn matches specific conditions (task labeled creative/physics/literature, two independent
numeric attempts disagree, the same solver fails twice, or the maintainer explicitly asks to consult the expert), and
record the maintainer's accept/reject outcome on every consult answer so a future router can be evaluated from real
evidence.

## Why

Directive `L20261125114331-2bd1` (consult design v3) §8 and §9 step 4: "Objective triggers" with "Log every consult and
the maintainer's accept or reject. A kNN router on that log can come later." Implementation starts after steps 1–3
(specs 380–382) are merged and measured.

## Code facts checked at `5fd15900`

- `backend/app/modules/ai/thread_service.py::turn_weight` classifies local turns deterministically (`light` vs `work`)
  with no model call, and `backend/app/modules/ai/thread_service.py::_turn_tools` selects up to 7 model-visible tools by
  fixed surface and word rules.
- `backend/app/modules/agents/hermes/turn_plan_tool.py::dispatch_turn_plan_tool` stores and updates the per-turn plan
  (`steps` and `findings`) via `jarvis_turn_plan`.
- `backend/app/modules/agents/hermes/supervisor.py::HermesSupervisor.turn` drives local Hermes turns, tracks per-turn
  tool calls and outcomes in `HermesSupervisor._handle_tool`, and injects continuation/nudge messages in
  `HermesSupervisor._handle_relay`.
- `backend/app/modules/agents/hermes/verification.py::verify_answer` runs deterministic numeric/unit cross-checks over
  turn tool outputs.
- `backend/app/modules/consult/service.py::request_consult` records every consult attempt in `consult_attempts`
  (`backend/app/core/consult_schema.py`), and `backend/app/modules/consult/routes.py` exposes consult endpoints.

## Scope

1. **Deterministic objective triggers (`backend/app/modules/consult/triggers.py`)** (§8):
   - Pure deterministic evaluator `evaluate_consult_triggers(...)` (no model call, no learned router) returning a typed
     list of fired trigger codes (`explicit_user_request`, `plan_label`, `numeric_disagreement`, `solver_double_failure`)
     and the recommended `kind` (`ideas`, `physics`, `literature`, `critique`):
     1. **Explicit maintainer request**: user prompt contains an explicit consult phrase (e.g. `"chiedi a opus"`,
        `"chiedi al consulente"`, `"ask opus"`, `"consult the expert"`, case-insensitive word boundary match).
     2. **Turn plan task label**: `jarvis_turn_plan` `set_plan` gains an optional `task_label` field
        (`creative | physics | literature | engineering | other`); when set to `creative`, `physics`, or `literature`,
        the corresponding trigger fires (`ideas`, `physics`, `literature`).
     3. **Numeric disagreement**: two independent numeric tool results in the same turn report the same quantity/unit
        (normalised via Pint where convertible) disagreeing by more than a deterministic threshold (default 15 %
        relative), firing `numeric_disagreement` (`critique`).
     4. **Repeated solver failure**: the same solver/calculation tool (`jarvis_python`, `jarvis_bench_run`, or a process
        run tool) fails twice in the same turn, firing `solver_double_failure` (`physics`).
   - **Harness nudge (never auto-sends bypassing Inkling)**:
     - When consults are available (`consult_mode` in `{"ask", "auto"}` or Sage configured) and at least one trigger
       fires on a local `work` turn where Inkling has not yet consulted for that trigger, the harness injects a single
       bounded system/harness hint telling Inkling which trigger fired and inviting it to call `jarvis_consult` if
       helpful.
     - Inkling still writes the outbound question and indicative context, and every call still goes through the full
       zone-A gate, mode (`ask`/`auto`), and 3-consults-per-turn loop guard.
     - Record fired triggers (`trigger_codes`) in `consult_attempts.gate_steps_json` (and as a `hermes.consult_trigger`
       event when fired).
2. **Maintainer accept/reject outcome log**:
   - Add an append-only `consult_feedback` table (additive migration `0040_consult_feedback` in
     `backend/app/core/consult_schema.py` and `backend/app/core/schema.py`) recording `(id, attempt_id, verdict
     ['accepted' | 'rejected'], note, trigger_codes_json, created_at)` — never mutating `consult_attempts` or
     `consult_answers`.
   - Add `POST /ai/consult/{consult_id}/feedback` (`{"verdict": "accepted" | "rejected", "note": optional <= 300 chars}`)
     accepting feedback only on `sent` consults in the current workspace.
   - Surface compact **"Utile" / "Non utile"** buttons on answered consult blocks in `ConsultCard.tsx` (persisting the
     chosen verdict and showing the recorded state).

Out of scope (binding non-goals):

- Any learned classifier or kNN router (explicitly deferred until real feedback logs exist).
- Automatically sending a cloud consult without Inkling authoring the indicative packet and passing the zone-A gate.
- Changing zone-A or zone-C gate rules.

## Files likely touched

- `backend/app/modules/consult/triggers.py` (new), `backend/app/modules/consult/service.py`,
  `backend/app/modules/consult/routes.py`
- `backend/app/core/consult_schema.py`, `backend/app/core/schema.py`,
  `backend/tests/test_migration_discipline.py`
- `backend/app/modules/agents/hermes/turn_plan_tool.py`, `backend/app/modules/agents/hermes/supervisor.py`
- `frontend/src/api/consult.ts`, `frontend/src/components/ai/ConsultCard.tsx`, `frontend/tests/`
- `backend/tests/test_consult_383_triggers.py` (new), `e2e/` walk update

## Design constraints

- Deterministic triggers only: zero extra model calls to decide whether to trigger.
- Triggers only nudge Inkling; code never writes the consult question or bypasses the zone-A gate, `ask` approval, or
  loop guard.
- When `consult_mode == "off"` and Sage is unavailable, triggers emit no nudge.
- Offline tests only; no new dependencies.

## Acceptance criteria

1. Each of the 4 objective triggers (`explicit_user_request`, `plan_label`, `numeric_disagreement`,
   `solver_double_failure`) fires deterministically on matching inputs and does not fire on non-matching or `light`
   turns.
2. When a trigger fires on an eligible local turn, the harness injects at most one nudge per trigger code per turn and
   records `trigger_codes` in the consult attempt ledger and event stream.
3. `POST /ai/consult/{consult_id}/feedback` records `accepted` or `rejected` in `consult_feedback` for a `sent` consult
   and rejects unknown/pending/cross-workspace consults.
4. `ConsultCard.tsx` renders `"Utile"` / `"Non utile"` controls on answered consult blocks and persists the choice.
5. Lint, mypy, focused tests, frontend build, migration discipline, repo gates, and CI pass on exact head.

## Required tests

- `backend/tests/test_consult_383_triggers.py`: all 4 trigger rules, unit-converted numeric disagreement, double solver
  failure, no-nudge when consults are off, single-nudge-per-trigger cap, and `consult_feedback` route + persistence.
- `backend/tests/test_migration_discipline.py` digest and baseline for `0040_consult_feedback`.
- Frontend `.mjs` test for `"Utile"` / `"Non utile"` labels and `e2e/` walk update.

## Definition of done

Merged with green CI on exact head after coordinator full-diff review, independent second-session review (additive
migration + consult trigger path), and browser walk evidence showing an answered consult block recording maintainer
feedback.
