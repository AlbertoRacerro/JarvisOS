# 299 — BENCH-RUNS-VIEW-1: Bench results with verdict lights and checks on the Runs screen

Status: ready
Depends on: 280, 281, 282, 283

## Goal

The Runs screen gets a "Banco di prova" section for the selected workspace. It lists the workspace's bench runs,
newest first, each with a verdict light (usable, usable with warnings, unchecked, not usable), the evaluator in plain
words, the status and the time. A row opens the run: its checks (family, status light, actual value, limit, unit, the
one-line hint and the source), its outputs with units, and its failure when it did not succeed. Two runs can be
selected and compared. Nothing is run from this screen in this slice.

## Why

Checks and an honest verdict on evaluation results (solids, mesh, static structure, retrieval quality) exist only
behind the bench command and the agent's bench tools; the owner cannot see them. This is the second slice of the
front-end plan in spec 298 and makes the bench results visible where the guided path would use them (Runs). A later
slice adds starting a run from a form, so the capability stops being reachable by command only.

## Code facts checked at `c7141d41`

- `backend/app/modules/engineering/evaluator_contracts.py::ResultCheck` carries `id`, `family`, `status`
  (`pass`, `warn`, `fail`, `not_evaluated`), `actual`, `limit`, `unit`, a one-line `hint` and `source`.
- `backend/app/modules/engineering/evaluator_contracts.py::result_verdict` derives `usable`, `usable_with_warnings`,
  `unchecked` or `not_usable` from a descriptor and a result.
- `backend/app/modules/engineering/bench.py::run_evaluation` writes `result.json` (with `verdict`, `checks`,
  `outputs`, `failure`) into one run folder; `backend/app/modules/engineering/bench.py::show_run` returns the bounded
  view of one folder and `backend/app/modules/engineering/bench.py::compare_runs` compares two.
- `backend/app/modules/engineering/bench.py::_dispatch_bench_tool` keeps the agent's runs under
  `<workspace>/engineering/bench`, next to the studies folder given by
  `backend/app/modules/engineering/operator_service.py::workspace_dir`, and confines every run id to that root.
- `backend/app/modules/engineering/bench.py::_agent_view` reduces every absolute path to its last component.
- `backend/app/modules/engineering/operator_routes.py` declares the workspace engineering router.
- `frontend/src/pages/RunsWorkbench.tsx::RunsWorkbench` is the Runs screen at `/runs` and already receives the
  selected workspace.

## Scope

In scope:
- Backend read routes under the workspace: list bench runs (folders under the workspace bench root that hold a
  `result.json`, newest first, at most 100), show one run, compare two. They reuse `show_run` and `compare_runs` with
  the bench root as `root`, and return path-free views as `_agent_view` does.
- A "Banco di prova" section in the Runs screen: list with verdict lights, run detail with the check table, outputs
  and failure, and a compare action for two selected runs.
- Plain names for the evaluators the agent may run (`AGENT_RUNNABLE_EVALUATORS`) and the retrieval evaluator; an
  unknown evaluator id is shown as is.
- An empty state that says how runs appear today ("i risultati compaiono qui quando l'assistente usa il banco di
  prova").

Out of scope (binding non-goals):
- Starting a run, deleting or editing a run, or any write to the bench root.
- Showing bench runs outside the workspace bench root, or any filesystem path.
- Changing the verdict rule, the checks or the evaluators.

## Files likely touched

- `backend/app/modules/engineering/operator_routes.py` (its router already has the prefix
  `/workspaces/{workspace_id}/engineering`; add `/bench/runs`, `/bench/runs/{run_id}` and `/bench/compare`)
- `frontend/src/pages/RunsWorkbench.tsx`, `frontend/src/components/BenchRunsPanel.tsx` (new), `frontend/src/api/`
- `backend/tests/test_bench_runs_view.py`, `frontend/tests/299-bench-runs-view.test.mjs`, `frontend/package.json`
- `e2e/bench_runs_walk.mjs`

## Design constraints

- Read only; a run folder with a missing or invalid `result.json` is skipped from the list and reported as one count.
- A run id is the folder name and must match a strict pattern; it is resolved inside the bench root or refused.
- The verdict light comes from the stored `verdict`; the UI never recomputes it. A stored verdict that is not one of
  the four values shows as unchecked.
- `not_evaluated` checks are shown as such, never as passed.
- Visible text in Italian; check ids and units are shown as stored.

## Acceptance criteria

1. With fixture run folders, the list route returns them newest first with verdict, evaluator and status; a folder
   without `result.json` and one with invalid JSON are skipped and counted.
2. The show and compare routes return checks, outputs and failure with no absolute path anywhere in the payload
   (failure messages and artifact refs included).
3. A run id containing `..`, a slash or an absolute path is refused.
4. The compare route returns the `compare_runs` document for two runs of the same workspace and refuses a run of
   another workspace.
5. The Runs screen shows the four verdict lights with distinct labels and the `not_evaluated` state; the browser walk
   from `/` opens Runs, opens a run, compares two and sees the check table.

## Required tests

- Backend: fixture `result.json` folders under a temporary workspace bench root covering the four verdicts, a
  `not_evaluated` check, a failed run, a broken folder, refused ids, cross-workspace compare.
- Frontend `.mjs`: the four verdict labels, the `not_evaluated` label, the empty-state text.
- Browser walk in `e2e/` from a clean profile at `/` with fixture runs in a temporary data root, attached as evidence.
