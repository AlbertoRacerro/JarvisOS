# 304 — READINESS-BOARD-SCREEN-1: The readiness board as a screen under Development

Status: ready
Depends on: 303

## Goal

The readiness board of spec 303 gets a screen, "Prontezza", under Development. It shows one row per literature case
declared by the installed pack and one cell per step (paper, facts, inputs bound, run, comparison, decision, answer),
each green or red. A red cell says in plain Italian what is missing, lists the named missing items as stored, and offers
one link to the screen where that item is made, with the case's workspace selected. A summary line gives `n/m` green
cells and the leftmost red cell. The screen reads; it creates, changes and runs nothing.

## Why

Directive L20261116155611-930c point 2: the board is first of all the map of what is missing per case, and the owner
must be able to read it in the product, not only through a command. Directive L20261117071737-1216: a capability
reachable only by command is not counted as usable. The links from each red cell to the screen that fixes it are the
first, deterministic piece of the guided path of directive L20261116201429-63ee; the path bar and the next card stay
for a later spec.

## Code facts checked at `3365a6a7`

- `backend/app/modules/engineering/readiness_board.py::get_readiness` serves `GET /readiness` with
  `ReadinessBoardRead` (`cases`, `errors`, `summary`); `STEPS` fixes the seven step ids; `StepResult` carries `status`
  (`green`/`red`), `reason` (`workspace_missing`, `missing_record`, `capability_missing` or none), `missing` and an
  English `detail`.
- `backend/app/modules/engineering/readiness_board.py::_compute_case` resolves the case workspace by slug or id but
  does not return the resolved id; `CaseResult` has only `workspace` as written in the case file.
- `frontend/src/app/routes.ts` declares `PRODUCTION_ROUTES`, `PEER_NAV_ITEMS` (Development has only Roadmap) and
  `ROADMAP_STAGE_ITEMS`; `frontend/src/components/fusion/FinalWorkspaceHeader.tsx` holds the Development tabs;
  `frontend/src/App.tsx` maps route ids to pages and owns `setWorkspaceId`.
- Screens where each item is made: Literature `/memory/literature` (source, paper file, datum acceptance and the
  "used by" binding), Project Basis `/memory/project-basis` (parameters and decisions), Runs `/runs` (bench runs,
  spec 299), Ricerca `/memory/search` (spec 300).

## Scope

In scope:
- Backend: add `workspace_id: str | None` to `CaseResult` (the resolved id; `null` when the workspace is missing).
  No other change to the board's rules.
- A route `development-readiness` at `/development/readiness`, title "Prontezza", reachable as a Development tab next
  to Roadmap in both `PEER_NAV_ITEMS` and `FinalWorkspaceHeader`.
- A page `frontend/src/pages/ReadinessBoard.tsx` with an API helper: summary line; a table with one row per case
  (title, workspace) and seven cells with a light and a short Italian label; selecting a cell shows its detail.
- Italian step labels: "Articolo", "Dati", "Ingressi legati", "Calcolo", "Confronto", "Decisione", "Risposta".
- Italian reason text from the reason code and step, never from `detail`: `workspace_missing` → "Il workspace del
  caso non esiste"; `capability_missing` → "Funzione non ancora disponibile nel prodotto"; `missing_record` → one
  sentence per step (for example facts: "Mancano dati accettati e verificati per:"). Missing items shown as stored.
- One link per red cell to the screen that makes the item: paper, facts, bound → Literature; run → Runs; decide →
  Project Basis; answer → Ricerca; compare and workspace_missing → no link. Following a link selects the case's
  `workspace_id` first.
- Empty states: no pack or no cases ("Nessun caso di letteratura installato: i casi arrivano dal pacchetto di
  progetto"); board errors listed under the table in plain words with the case id.
- A browser walk `e2e/readiness_board_walk.mjs` against a temporary data root with `JARVISOS_PACK_PATH` set to the
  fake pack `backend/tests/fixtures/packs/zephyr`.

Out of scope (binding non-goals): any write or action from the screen; the comparison object; the path bar, next card
or tour; changing cell rules; showing filesystem paths; the "twice from a clean install" rule.

## Files likely touched

- `backend/app/modules/engineering/readiness_board.py`, `backend/tests/test_readiness_board_303.py` (the new field)
- `frontend/src/app/routes.ts`, `frontend/src/components/fusion/FinalWorkspaceHeader.tsx`, `frontend/src/App.tsx`
- `frontend/src/pages/ReadinessBoard.tsx` (new), `frontend/src/api/readinessBoard.ts` (new)
- `frontend/tests/304-readiness-board-screen.test.mjs` (new) and the `build` chain in `frontend/package.json`;
  existing frontend tests that assert the Development tab list or route list, if any
- `e2e/readiness_board_walk.mjs` (new); `frontend/src/api/generated` and `docs/MODULES.md` if regenerated

## Design constraints

- Read only: one GET; no new table, column or migration.
- The light comes from the stored `status`; the UI never recomputes a cell.
- Visible text in Italian; no ids, codes or English `detail` in visible text except case ids in the error list and the
  missing names as stored (these are the engineer's quantity and parameter names).
- No case content in the repository: tests and walk use the fake pack only.
- Smallest sufficient change: reuse existing page, light and table styles; no new dependency.

## Acceptance criteria

1. `GET /readiness` returns `workspace_id` for a case whose workspace exists and `null` for one that does not.
2. From `/` in a clean browser profile, Development → Prontezza opens the board using visible controls only.
3. With the fake pack and a fresh data root, the case row shows seven red cells; the summary reads `0/7`, and the
   leftmost red cell names the case and "Articolo".
4. After creating the case workspace and its literature source with a readable paper file through the existing
   routes, a reload shows "Articolo" green and "Dati" red listing the missing quantity names.
5. The red "Dati" cell's link opens Literature with the case workspace selected.
6. "Confronto" shows "Funzione non ancora disponibile nel prodotto" and no link.
7. Without a pack the screen shows the empty-state sentence.

## Required tests

- `backend/tests/test_readiness_board_303.py` extended for criterion 1.
- `frontend/tests/304-readiness-board-screen.test.mjs` for the route, tab, labels, reason sentences and link targets.
- `e2e/readiness_board_walk.mjs` covering criteria 2–6, evidence directory attached to the PR.

## Definition of done

Lint, focused backend tests and the frontend build pass; generated files current; CI green on the exact head; one
severe review of the diff (medium risk); the browser walk passes at the exact head before the owner is asked to look;
STATUS row 304 moved to merged by the implementation PR.
