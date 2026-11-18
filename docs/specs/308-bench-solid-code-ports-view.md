# 308 — BENCH-SOLID-CODE-PORTS-VIEW-1: Authored geometry solid code and named boundary ports on the Runs bench view

Status: merged
Depends on: 285, 286, 299

## Goal

On the Runs screen ("Banco di prova", spec 299), bench runs produced by `bluecad.solid_code` (specs 285 and 286) display their readable Italian name ("Solido da codice (BlueCAD)") and, when opened, show a "Porte" section listing the named boundary ports (`name`, `interface`, and tube/pad diameter) read from `<run_dir>/artifacts/manifest.json`.

## Why

Item 5 of the front-end plan in spec 298 ("Runs: bench results and solids from authored geometry code with their named ports", under directive `L20261117071737-1216`). Spec 299 added the "Banco di prova" section on the Runs screen, and specs 285 and 286 added `bluecad.solid_code` and `artifacts/manifest.json` emission with `resolved_ports`, but `bench_runs_view.py::show` did not expose the resolved ports from `manifest.json` and `BenchRunsPanel.tsx` had no Italian label for `bluecad.solid_code` or port list in `BenchRunDetailView`. This slice was also executed as the live multi-file tier-2 trial of `copilot-auto` under directive `L20261118164933-7605` point 2.

## Code facts checked at `f0f18162`

- `backend/app/modules/engineering/bench_runs_view.py::show` returns the path-free detail view of one bench run folder inside `backend/app/modules/engineering/bench_runs_view.py::bench_root`.
- `backend/app/modules/engineering/bench.py::_run_bluecad_solid_code` writes `<out_dir>/artifacts/manifest.json` with `resolved_ports` (`{"body": {<port_name>: {"interface": "tube"|"pad", "origin": [...], "direction": [...], "outer_d": ..., "wall_t": ..., "pad_d": ...}}}`).
- `frontend/src/components/BenchRunsPanel.tsx::BenchRunDetailView` renders the detail of a selected bench run, and `frontend/src/components/BenchRunsPanel.tsx::evaluatorName` maps evaluator ids to Italian labels.

## Scope

In scope:
1. `backend/app/modules/engineering/bench_runs_view.py`:
   - `_read_ports(run_dir: Path) -> list[dict[str, Any]]` reads `run_dir / "artifacts" / "manifest.json"` (refusing symlinks or paths escaping `run_dir`), extracts `resolved_ports` into a sorted, path-free list of port objects (`name`, `interface`, `origin`, `direction`, optional `outer_d`/`wall_t`/`pad_d`), and returns `[]` when absent or malformed.
   - `show(workspace_id, run_id)` includes `"ports": _read_ports(run_dir)`.
2. `frontend/src/api/benchRuns.ts` and `frontend/src/components/BenchRunsPanel.tsx`:
   - Add `BenchPort` and `ports?: BenchPort[]` to `BenchRunDetail`.
   - Map `"bluecad.solid_code"` to `"Solido da codice (BlueCAD)"` in `EVALUATOR_NAMES`.
   - Render `<h4>Porte</h4>` and `<ul className="bench-ports">` in `BenchRunDetailView` when `run.ports` is non-empty.
3. Extend `backend/tests/test_bench_runs_view.py`, `frontend/tests/299-bench-runs-view.test.mjs`, and `e2e/bench_runs_walk.mjs` to cover `bluecad.solid_code` and its named ports.

Out of scope (binding non-goals):
- Starting a `bluecad.solid_code` run from the browser or exposing filesystem paths.

## Files likely touched

- `backend/app/modules/engineering/bench_runs_view.py`
- `backend/tests/test_bench_runs_view.py`
- `frontend/src/api/benchRuns.ts`
- `frontend/src/components/BenchRunsPanel.tsx`
- `frontend/tests/299-bench-runs-view.test.mjs`
- `e2e/bench_runs_walk.mjs`
- `docs/specs/308-bench-solid-code-ports-view.md`, `docs/specs/STATUS.md`, `docs/MODULES.md`

## Acceptance criteria

1. `GET /workspaces/{workspace_id}/engineering/bench/runs/{run_id}` returns `"ports": []` when `artifacts/manifest.json` is absent and returns the sorted, path-free list of ports when `artifacts/manifest.json` defines `resolved_ports`.
2. `evaluatorName("bluecad.solid_code")` returns `"Solido da codice (BlueCAD)"` and `BenchRunDetailView` renders the "Porte" list with interface and diameter in mm.
3. `e2e/bench_runs_walk.mjs` verifies the `"Solido da codice (BlueCAD)"` row and `"Porte"` section in headless Chromium from `/`.

## Required tests

- `backend/tests/test_bench_runs_view.py`
- `frontend/tests/299-bench-runs-view.test.mjs` and `npm run build`
- `e2e/bench_runs_walk.mjs`
