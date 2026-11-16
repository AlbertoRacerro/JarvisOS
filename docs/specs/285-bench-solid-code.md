# 285 — Bench solid from authored geometry code

Status: in_review
Depends on: 281, 282, 284

## Goal

The headless bench gains an evaluator `bluecad.solid_code`: a harness agent or engineer writes a build123d Python
file in its work folder, the bench builds it in the existing Studio geometry sandbox, and returns the same checked
`EvaluationResult` envelope as `bluecad.solid`, with the STEP, STL and GLB files and the measured volume and
bounding box. This is the first slice of code as the action language for geometry (directive
L20261115204046-4be1, H3). The JSON geometry spec stays the safe path; this adds the expressive one.

## Why

`bluecad.solid` accepts only the bounded JSON geometry spec, so an agent cannot express a shape the spec does not
know. The sandboxed build123d runtime already exists and already returns geometry facts and verified exports, but
only Studio reaches it. Reusing it gives the bench an expressive geometry door without a new execution boundary.

## Code facts checked at `538423c1`

- `backend/app/modules/studio_runtime/runtime.py::run` with `profile="geometry"` runs submitted source in
  bubblewrap with fixed geometry limits, and admits exactly `model.step`, `model.stl` and `model.glb` into an
  `artifact_dir` that must not exist; `RunRecord.classification` is one of `ok`, `error`, `timeout`, `limit`,
  `invalid_result`, `unavailable`, `disabled`. `runtime.status()` reports `disabled` unless
  `JARVISOS_STUDIO_USER_CODE_ENABLED=1` (`backend/app/core/config.py`), and `unavailable` without bubblewrap.
- `backend/app/modules/studio_runtime/worker.py::_geometry` returns facts `valid`, `solids`, `volume`, `bbox_min`,
  `bbox_max` for the shape returned by the source's `main(inputs)`.
- `backend/app/modules/studio/geometry.py::_bounds_errors` already judges declared `volume_min`, `volume_max` and
  `max_extent` from those facts.
- `backend/app/modules/engineering/bench.py::run_evaluation` dispatches `BLUECAD_EVALUATORS`;
  `list_evaluators` lists them with descriptors from `backend/app/modules/bluecad/result_adapters.py`;
  `AGENT_RUNNABLE_EVALUATORS` is built from `BLUECAD_EVALUATORS`.
- `backend/app/modules/bluecad/mesh_adapter.py::_port` reads named ports from a native `manifest.json`; a code-built
  solid has no manifest, so mesh and FEM hand-off is not possible in this slice.

## Scope

In scope:

1. A request kind `geometry_code` for `bluecad.solid_code`: a JSON request with `source_path` (a `.py` file resolved
   against the request folder and confined to `--root`, same rule as `_resolve_geometry_paths`), `inputs` (mapping
   of names to finite numbers, passed to `main(inputs)`), and optional declared `volume_min`, `volume_max`,
   `max_extent` in model units (millimetres). Unknown keys, a missing or oversized source, or non-finite inputs are a
   bench input error with a hint that says what to change.
2. The run uses `studio_runtime.runtime.run(..., profile="geometry", artifact_dir=<out>/artifacts)` unchanged. No
   change to `runtime.py`, `worker.py`, the sandbox arguments or the limits.
3. An adapter (next to the solid adapter) maps the `RunRecord` to `EvaluationResult`:
   - `disabled` / `unavailable` → `status="refused"`, failure category `not_available`, hint naming
     `JARVISOS_STUDIO_USER_CODE_ENABLED=1` or bubblewrap respectively.
   - `error`, `timeout`, `limit`, `invalid_result` → `status="failed"` with the limit name and value when present
     and at most 240 characters of the last stderr line; never a host path.
   - `ok` → `status="succeeded"`, outputs volume (mm3) and bounding box extents (mm), the three artifacts with the
     sha256 the runtime recorded, and checks: a required topology check (`valid` and `solids >= 1`), and declared
     volume and extent checks with `actual`, `limit`, `unit` when declared. An invalid shape fails its check and so
     derives `not_usable`; numbers are never changed.
   - The descriptor declares its required checks so a missing one derives `unchecked` (spec 280 rule).
4. `bench list` shows `bluecad.solid_code` with its availability from `runtime.status()`.
5. `bluecad.solid_code` is **not** in `AGENT_RUNNABLE_EVALUATORS`: the in-product agent tool does not execute
   model-written code in this slice. Only the headless command runs it.

Out of scope:

- Named ports or a manifest from code (needed for mesh/FEM hand-off), renders as images, and the agent tool: later
  H3 slices.
- Any change to the sandbox, its limits, or Studio's routes.
- Project-specific geometry, examples or tolerances.

## Files likely touched

- `backend/app/modules/engineering/bench.py`
- `backend/app/modules/bluecad/result_adapters.py`
- `backend/tests/test_engineering_bench.py`, `backend/tests/bluecad/test_result_adapters.py`
- `docs/MODULES.md` (regenerated), `docs/specs/STATUS.md`

## Acceptance criteria

1. With the sandbox disabled, `bench run bluecad.solid_code req.json --out o` exits 2 with `status="refused"` and a
   hint naming the setting; `bench list` shows it unavailable with a reason.
2. With a fake `runtime.run` returning each classification, the adapter yields the status, failure and checks of
   scope 3; an `ok` record with `valid=false` derives `not_usable`; a declared volume outside its range fails a
   check with `actual` and `limit`.
3. A `source_path` outside `--root`, a missing file, or an unknown request key is refused with exit 2 and a hint.
4. `bluecad.solid_code` cannot be run through `dispatch_bench_tool` (refused like any non-runnable evaluator).
5. On a host with bubblewrap and build123d, a real run of a simple box source with declared volume returns
   `usable` and the three artifacts in `<out>/artifacts`; the same source with a wrong declared volume returns
   `not_usable`. Recorded as a proof, temporary root, no project data.

## Required tests

- Offline tests for criteria 1–4 with a fake runtime; existing bench and adapter tests stay green.
- One local real-sandbox proof for criterion 5 (skipped automatically where bubblewrap is unavailable).
- Ruff, exact-head CI and one severe review of the final diff.

## Definition of done

The command builds authored code in the existing sandbox and returns a checked result that agrees with the
geometry facts, the agent tool cannot reach it, the evidence above is recorded, and the registry row is truthful.
