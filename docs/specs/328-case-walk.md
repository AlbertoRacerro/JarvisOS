# 328 — CASE-WALK-1: One rerunnable browser walk takes a literature case from seven red cells to seven green

Status: ready
Depends on: 303, 304, 305, 311, 314, 315, 316, 318

## Goal

One script, `e2e/case_walk.mjs`, walks a single readiness case end to end in the browser: from `/` in a clean
profile, using visible controls only, it goes from the paper to the sourced answer and leaves all seven cells of the
case on the Prontezza board green. It is rerunnable: the same script runs on the fake pack in this repository and,
given a pack path and a case id, on any other pack's case against a temporary copy of a data root.

## Why

The capability map counts a capability for the board only when it is walked end to end on a case, and states that
no case is walked yet; it asks for "the walk as a rerunnable script". Each of the seven steps already has its own
screen and its own walk (311 facts and inputs, 315 correlation card, 314 and 318 verdict and decision, 316 answer
check, 304/305 board), but every one of those walks starts from state another walk would have made, often seeded
through the API. Nothing proves that one person, in one session, can take one case from nothing to a decision
through the interface. This spec composes the existing screens; it adds no capability. Where a step cannot be done
through visible controls the walk fails at that step and names it: that is the finding this walk exists to produce.

## Code facts checked at `3ef5d6e9`

- `backend/app/modules/engineering/readiness_board.py::_compute_case` evaluates the seven cells in order `paper`,
  `facts`, `bound`, `run`, `compare`, `decide`, `answer`: a literature source of the case title with a readable
  backing file; for each `facts` quantity an accepted datum of that source whose `check_datum` verdict is usable;
  for each `inputs` name a parameter whose `source_ref` is `literature_entry:<id>` of an accepted entry of that
  source; a usable bench run or converged process run; `_compare_step` within `rel_tol`; an accepted decision
  joined to an accepted `case_verdicts` row of the case; a `retrieval.eval` bench run with verdict `usable`.
- `backend/tests/fixtures/packs/zephyr/readiness_cases.toml` defines `generic-literature-1` (workspace `zephyr`,
  source "Benchmark Validation Study", facts `density`, `thermal_conductivity`, inputs `material_density`,
  `inlet_conductivity`, compare `density` 1000 kg/m^3 at 5 %).
- Visible controls exist for each step: `frontend/src/components/WorkspaceBootstrap.tsx` ("Create workspace");
  `frontend/src/pages/LiteratureKnowledge.tsx` (source with file, located datum, accept as fact, use as parameter:
  spec 311, walked by `e2e/literature_fact_input_walk.mjs`); `frontend/src/components/BenchRunsPanel.tsx`
  ("Nuova esecuzione" card form, spec 315, `e2e/correlation_cards_walk.mjs`); `frontend/src/pages/ReadinessBoard.tsx`
  ("Registra verdetto", "Accetta verdetto", "Registra decisione", specs 314 and 318, `e2e/case_verdict_walk.mjs`);
  the Memory "Ricerca" retrieval check of spec 316 (`e2e/retrieval_eval_ui_walk.mjs`).
- `bench_cards.py` `coolprop.pure_fluid_properties` with fluid `Water` at 298.15 K and 101325 Pa gives a density
  near 997 kg/m^3, inside the fake case's 5 % band (not run for this spec; the implementer confirms the output name
  the board reads for `density`, inferred from `e2e/readiness_board_walk.mjs` criterion 6).
- Existing walks start an isolated backend on `JARVISOS_PROOF_PORT` with a temporary data root and
  `JARVISOS_PACK_PATH`; `e2e/readiness_process_compare_walk.mjs` is a current example of that harness.

## Scope

In scope:
- **`e2e/case_walk.mjs <evidence-dir> [--pack <pack-dir>] [--case <case-id>] [--data-root-copy <dir>]`.**
  Defaults: the fake pack and `generic-literature-1` with a fresh empty data root. With `--data-root-copy` it copies
  that directory to a temporary location and runs against the copy, never the original.
- **Case-driven, not hard-coded.** The walk reads the chosen case from the pack's `readiness_cases.toml` (source
  title, facts, inputs, compare quantity, value and unit) and a per-case walk input file
  `<pack-dir>/case_walks/<case-id>.toml` that supplies what the case file does not: the paper text file to attach,
  for each fact the located value (value, unit, locator), which input each fact binds to, and which card or process
  run produces the compared quantity with its field values, and the question and expected answer for the
  retrieval check. The fake pack gets
  `backend/tests/fixtures/packs/zephyr/case_walks/generic-literature-1.toml` and its generic paper text. A missing
  walk input file stops the walk with a message naming the file.
- **Visible controls only.** After the isolated backend starts, every record is made by clicking and typing in the
  app, starting from `/`: workspace, literature source with paper file, located datum per fact, accept, use as
  parameter, card run, verdict, accept verdict, decision, answer check. No `fetch`/`request` to `/api` creates or
  changes data; reads for evidence are allowed.
- **Board as the oracle.** Before the first step and after each step the walk reads the board (screen and
  `GET` of the board) and records the seven cell states. The step's own cell must turn green and no earlier green
  cell may turn red. The walk passes only when all seven are green after a reload.
- **Evidence.** `<evidence-dir>/board.jsonl` (one line per checkpoint: step, seven states, missing items),
  one screenshot per step, `summary.json` (pack id, case id, pass/fail, first failing step with the board's
  `missing` list and the control that was not found), console and page errors. No absolute paths of the data root
  in `summary.json`.
- **Failure is a finding.** If a step cannot be done through visible controls the walk fails there, names the step
  and the missing control, and exits non-zero. It must not fall back to the API.
- **README.** One entry in `e2e/README.md` with the command and the walk input file format.

Out of scope (binding non-goals):
- No product code change. If the walk shows a step unreachable, the fix is a separate spec; this PR stops at the
  failing walk and its evidence.
- No new pack format consumed by the product: `case_walks/` is read by the walk script only.
- No real project case, paper or value in this repository; the private pack's walk input files live in the pack.
- No model call; the answer step uses the deterministic retrieval check.

## Files likely touched

- `e2e/case_walk.mjs` (new), `e2e/README.md`
- `backend/tests/fixtures/packs/zephyr/case_walks/generic-literature-1.toml` and a generic paper text file (new)
- `e2e/module.toml` entrypoints if it lists walks

## Design constraints

- Reuse the harness pattern of the existing walks (isolated backend, `JARVISOS_PROOF_PORT`, temporary data root,
  `PLAYWRIGHT_MODULE`); do not add a dependency. A TOML reader: use the backend venv Python with `tomllib` through
  `spawnSync` to convert the two TOML files to JSON, as other walks call the venv.
- Selectors by role, label and visible text, as in the existing walks, so the walk fails when a control disappears.
- Never start anything on port 8000; never read or write outside the temporary data root and the evidence directory.

## Acceptance criteria

1. On the fake pack with defaults the walk passes: the first checkpoint shows seven red cells, each step turns its
   cell green in order, and after a reload all seven are green.
2. `board.jsonl` has one checkpoint before the first step and one after each step; `summary.json` states pass.
3. The script makes no data-changing `/api` request itself (checked by a static test on the script source).
4. Removing a required fact line from the walk input file makes the walk fail at `facts` with that quantity named
   in `summary.json`, exit non-zero, and no API fallback.
5. With `--data-root-copy` the original directory is unchanged after the run (digest of its file list and sizes
   before and after).
6. No console errors or page errors during a passing run.

## Required tests

- A frontend static `.mjs` test (added to the `build` chain) asserting the script has no `method: "POST"`,
  `"PATCH"`, `"PUT"` or `"DELETE"` request and reads its case from the pack files.
- The walk itself, run by the implementer with evidence attached; criteria 4 and 5 run once each and recorded in
  the PR.

## Definition of done

Test gate green (see `AGENTS.md`), the walk passing on the fake pack with its evidence directory, criteria 4 and 5
shown, spec status updated. If the walk finds a step that visible controls cannot do, the PR is not merged as
passing: the coordinator records the gap and writes the product fix as its own spec, then this walk is rerun.
Risk: low (test script and fixture only, no product code). The coordinator then runs the
walk privately on the pack's third case and records the result in the ledger, not in the repository.
