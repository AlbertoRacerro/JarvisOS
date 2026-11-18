# 312 — GUIDED-FLOW-1: Guided question flow (question list, seven-step path bar, and deterministic "Next" card) in Design

Status: merged
PR: #181
Depends on: none

## Goal

In Design, the engineer sees a **Guidato / Libero** mode toggle (default **Guidato**; switching changes no data). In
Guidato mode, the top of Design shows:
1. a **question list** ("Domande") to create, select, close or reopen engineering questions (with an optional one-line
   "Cosa cambia");
2. a **seven-step path bar** (`Inquadra`, `Fonti`, `Modello`, `Calcolo`, `Esplora`, `Confronta`, `Decidi`), each step
   evaluated deterministically from existing workspace records as `done`, `blocked` (with named items), `in_progress`,
   or `not_started`, and clickable to open its screen;
3. a **"Prossimo passo" card** computed by a fixed rule order (never by a model) with one reason and one action button.

## Why

Directives `L20261116201429-63ee` (points 1–4 and 7) and `L20261116160140-7608` (Wave 0): the unit of guidance is a
question, not a one-pass project wizard. A single data-driven step definition drives the path bar and the "Next" card
over existing screens (`Project Basis`, `Literature`, `Design`, `Runs`, `Studies`, `Prontezza`) without locking step
order.

## Code facts checked at `02e41306`

- `backend/app/core/schema.py::SCHEMA_STATEMENTS` and `backend/app/core/database.py::initialize_database` manage
  additive SQLite tables and migrations (`CURRENT_SCHEMA_MIGRATION_ID = "0033_literature_ingestion"`).
- `backend/tests/test_migration_discipline.py::BASE_CREATE_ONLY_SHA256` pins the `CREATE`-only column baseline and
  verifies fresh == upgraded.
- `backend/app/modules/engineering/readiness_board.py::STEPS` defines the seven literature-case steps and
  `backend/app/modules/engineering/readiness_board.py::compute_readiness_board` evaluates case readiness on existing
  records.
- `backend/app/modules/engineering/operator_routes.py::router` mounts workspace-scoped engineering routes under
  `/workspaces/{workspace_id}/engineering`.
- `backend/app/modules/engineering/bench_runs_view.py::list_runs` lists bench runs for a workspace with `verdict`.
- `frontend/src/pages/DesignProject.tsx::DesignProject` renders the Design screen with `projectId`, `versions`, and
  `navigate`.
- `frontend/src/app/routes.ts::PRODUCTION_ROUTES` defines `/design/project`, `/design/studies`,
  `/memory/project-basis`, `/memory/literature`, `/runs`, and `/development/readiness`.

## Scope

In scope:
- Additive SQLite table `design_questions` in `backend/app/core/schema.py` (`id`, `workspace_id`, `title`, `status` in
  `('open', 'closed')`, `what_changes`, `sensitivity`, `explore_skip_reason`, `linked_decision_id`, `created_at`,
  `updated_at`) and index `idx_design_questions_workspace`; migration `0034_design_questions` and updated baseline in
  `backend/tests/test_migration_discipline.py`.
- `backend/app/modules/engineering/guided_flow.py`:
  - Data-driven `GUIDED_STEPS` tuple of the 7 steps (`frame`, `sources`, `model`, `run`, `explore`, `compare`, `decide`)
    with `order`, `label_it`, `route`, `description_it`.
  - Deterministic step evaluation on workspace records:
    1. `frame` (`/memory/project-basis`): `done` when `title` is non-empty and at least one `requirements` row exists in
       the workspace; `in_progress` otherwise.
    2. `sources` (`/memory/literature`): `blocked` (naming flagged entries) if any `literature_entries` row has
       `ingestion_status = 'flagged'`; `done` when at least one `literature_sources` row exists and none are flagged;
       `not_started` otherwise.
    3. `model` (`/design/project`): `blocked` (naming unbound parameters) if any active `parameters` row has empty/null
       `source_ref`; `done` when at least one `design_components`, `model_specs` or `studio_drafts` row exists and no
       active parameter lacks `source_ref`; `not_started` otherwise.
    4. `run` (`/runs`): `done` when at least one bench run (`bench_runs_view.list_runs`) has `verdict` in
       `("usable", "usable_with_warnings")` or at least one `simulation_runs` row has `status = 'completed'`;
       `in_progress` if a `simulation_runs` row is `running` or `queued`; `blocked` if runs exist and none are usable;
       `not_started` otherwise.
    5. `explore` (`/design/studies`): `done` when `explore_skip_reason` is non-empty, or at least one
       `design_component_study_templates` row exists, or >1 `design_project_versions` row exists in the workspace;
       `not_started` otherwise.
    6. `compare` (`/development/readiness`): `done` when at least one `simulation_result_resolutions` row exists in the
       workspace or at least one readiness board case for the workspace has `compare.status == 'green'`; `not_started`
       otherwise.
    7. `decide` (`/memory/project-basis`): `done` when `linked_decision_id` is set or at least one `decisions` row
       exists in the workspace; `not_started` otherwise.
  - Deterministic `next_action`: earliest step whose state is not `done`, returning `{ step, title_it, reason_it,
    action_label_it, route }` (or `null` when all 7 steps are `done`).
- Routes in `backend/app/modules/engineering/operator_routes.py`:
  - `GET /workspaces/{workspace_id}/engineering/guided-questions`
  - `POST /workspaces/{workspace_id}/engineering/guided-questions`
  - `PATCH /workspaces/{workspace_id}/engineering/guided-questions/{question_id}`
- `frontend/src/api/guidedFlow.ts` and regenerated `frontend/src/api/generated` (`scripts/generate_frontend_contracts.py`).
- `frontend/src/components/design/GuidedFlowBar.tsx` rendered at the top of `DesignProject.tsx`:
  - `Guidato` / `Libero` toggle (persisted in `localStorage`);
  - Question list (open & closed), create form (`title` + optional `what_changes`), close/reopen action, and optional
    skip-explore reason;
  - Seven-step clickable path bar;
  - "Prossimo passo" card with one action button navigating to `next_action.route`.
- Tests: `backend/tests/test_guided_flow_312.py`, `frontend/tests/312-guided-flow.test.mjs` (in `frontend/package.json`
  `build` chain), and `e2e/guided_flow_walk.mjs`.

Out of scope (binding non-goals):
- Replacing `frontend/src/components/bubu/guidedTour.ts` or `readiness_board.py` (later unification slice).
- Model-computed step states or next actions.
- Locking step order (the engineer can click any step at any time).

## Design constraints

- High risk for schema migration (`test_migration_discipline.py` fresh == upgraded).
- Workspace isolation: every query filters by `workspace_id`; cross-workspace question `PATCH` returns `404`.
- Visible guided-flow text is Italian (`Inquadra`, `Fonti`, `Modello`, `Calcolo`, `Esplora`, `Confronta`, `Decidi`,
  `Prossimo passo`, `Guidato`, `Libero`); no internal IDs or paths in visible UI text.

## Acceptance criteria

1. `GET /workspaces/{id}/engineering/guided-questions` returns `steps_definition` (7 steps) and `questions` for that
   workspace; `404` for unknown workspace.
2. `POST` creates a question (`201`); `PATCH` updates `title`, `status`, `what_changes`, `explore_skip_reason`,
   `linked_decision_id` (`200`), or `404` if not in workspace.
3. Step states transition deterministically across `not_started`, `in_progress`, `blocked` (e.g. flagged literature
   entry or unbound parameter), and `done` as workspace records are added; `next_action` always picks the earliest
   non-`done` step.
4. `test_migration_discipline.py` passes with `0034_design_questions`.
5. `DesignProject` renders the `Guidato` / `Libero` toggle, question list, 7-step path bar, and `Prossimo passo` card;
   `frontend/tests/312-guided-flow.test.mjs` passes in `npm run build`.
6. Browser walk `e2e/guided_flow_walk.mjs` from `/` opens Design, creates a question, verifies the 7 steps and
   `Prossimo passo` card, toggles `Libero` / `Guidato`, clicks a step to navigate, with no console or page errors.

## Required tests

- `backend/tests/test_guided_flow_312.py`
- `backend/tests/test_migration_discipline.py`
- `frontend/tests/312-guided-flow.test.mjs`
- `e2e/guided_flow_walk.mjs`

## Risk and evidence

High (additive schema migration `0034_design_questions`). Evidence: backend tests and `test_migration_discipline.py`,
`ruff`, `npm run build`, gates (`check_spec_status.py`, `check_code_facts.py`, `generate_frontend_contracts.py --check`,
`gen_modules.py`, `check_doc_paths.py`, `check_architecture_enforcement.py`), CI green on exact head, second-session
review of the diff, and browser walk `e2e/guided_flow_walk.mjs`.
