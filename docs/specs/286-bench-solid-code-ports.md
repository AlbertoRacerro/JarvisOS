# 286 — Named ports and manifest from authored geometry code

Status: in_review
Depends on: 285

## Goal

Authored geometry code run through `bluecad.solid_code` can optionally declare named boundary ports alongside its
`build123d` shape, and `bench run bluecad.solid_code` writes a `manifest.json` beside `model.step` in
`<out>/artifacts/` so `bluecad.mesh` and `bluecad.fem` can mesh and solve code-built solids directly without a
separate hand-written manifest.

## Why

Spec 285 added `bluecad.solid_code` (directive L20261115204046-4be1, H3 first slice), but a code-built solid has no
`manifest.json` or `resolved_ports`, so `bluecad.mesh` and `bluecad.fem` cannot attach boundary conditions or loads to
it. Adding optional port declarations in the geometry return value and emitting the canonical `bluecad_manifest_v0_1`
manifest closes the geometry → mesh → FEM loop for authored code.

## Code facts checked at `c956e786`

- `backend/app/modules/studio_runtime/worker.py::_geometry` accepts one `build123d.Shape`, exports `model.step`,
  `model.stl`, and `model.glb` into `/work/artifacts/`, and returns `facts` (`valid`, `solids`, `volume`, `bbox_min`,
  `bbox_max`) via `/work/result.json`.
- `backend/app/modules/bluecad/models.py::PortFrame` defines the canonical port schema (`origin`, `direction`,
  `interface` `"tube"` or `"pad"`, `outer_d`/`wall_t` for `"tube"`, `pad_d` for `"pad"`).
- `backend/app/modules/bluecad/export.py::_manifest` builds `bluecad_manifest_v0_1` with `parts`,
  `resolved_ports`, `assembly`, `artifacts`, and `manifest_digest`.
- `backend/app/modules/bluecad/mesh_adapter.py::_port` resolves `"<part>.<port>"` labels against
  `manifest["resolved_ports"][part_id][port_id]` and uses `origin` and `outer_d` or `pad_d` for Gmsh surface boxes.
- `backend/app/modules/bluecad/result_adapters.py::solid_code_run_record_to_evaluation_result` maps `RunRecord` to
  `EvaluationResult` with `outputs` and `output_artifacts`.
- `backend/app/modules/engineering/bench.py::_run_bluecad_solid_code` invokes `studio_runtime.runtime.run` with
  `profile="geometry"` and `artifact_dir=out_dir / "artifacts"`.
- `backend/app/modules/engineering/bench.py::_run_bluecad_mesh` resolves `geometry.step_path` and
  `geometry.manifest_path` within `--root` and passes them to `mesh_analysis_spec`.

## Scope

In scope:

1. `studio_runtime/worker.py::_geometry` accepts either:
   - a `build123d.Shape` (existing behavior, `ports = {}`), or
   - a `dict` with `"shape"` (a `build123d.Shape`) and optional `"ports"` (a mapping of valid port names `[A-Za-z0-9_]+`
     to port dicts matching `PortFrame`: `origin` 3 finite floats, `direction` 3 finite non-zero floats, `interface`
     `"tube"` or `"pad"`, positive `outer_d` + non-negative `wall_t < outer_d / 2` for `"tube"`, or positive `pad_d` for
     `"pad"`). Invalid shape/ports raise `TypeError` or `ValueError` inside the sandbox.
   - `_geometry` normalizes validated ports into `facts["ports"]` (only when `"ports"` is provided or empty dict;
     existing shape-only returns keep working unchanged).
2. Host-side manifest emission for `bluecad.solid_code`:
   - When `solid_code` succeeds with valid geometry facts, write `<out>/artifacts/manifest.json` using the
     `bluecad_manifest_v0_1` layout with part `"body"`, `resolved_ports: {"body": <ports>}`, `assembly` volume and bbox,
     `artifacts` (`model.step`, `model.stl`, `model.glb` sha256 and byte size), and `manifest_digest`.
   - Include `manifest.json` in `EvaluationResult.output_artifacts` with its `sha256` digest when emitted.
3. No change to `runtime.py` `ARTIFACT_NAMES`, sandbox limits, bubblewrap flags, or `AGENT_RUNNABLE_EVALUATORS`
   (`bluecad.solid_code` stays out of `AGENT_RUNNABLE_EVALUATORS`).

Out of scope (binding non-goals):

- Renders / image outputs from `bluecad.solid_code` (later H3 slice).
- Exposing `bluecad.solid_code` as an in-product agent tool.
- Multi-part assemblies in `bluecad.solid_code` (single `"body"` part only).

## Files likely touched

- `backend/app/modules/studio_runtime/worker.py`
- `backend/app/modules/bluecad/result_adapters.py`
- `backend/app/modules/engineering/bench.py`
- `backend/tests/test_engineering_bench.py`, `backend/tests/bluecad/test_result_adapters.py`,
  `backend/tests/test_studio_runtime.py` (or existing studio runtime tests)
- `docs/specs/STATUS.md`

## Design constraints

- Returning a plain `build123d.Shape` from `main(inputs)` must remain 100% backward-compatible for Studio and bench.
- `manifest.json` is written on the host from validated `facts` and host-admitted artifacts; the sandboxed worker never
  writes `manifest.json` directly and `runtime.py` `ARTIFACT_NAMES` does not change.
- Port validation must reject non-finite numbers, zero-length direction vectors, invalid port names, and invalid
  tube/pad dimensions.

## Acceptance criteria

1. `worker.py::_geometry` accepts a `dict` `{"shape": shape, "ports": {...}}`, validates port fields, and includes
   normalized `ports` in `facts`, while still accepting a bare `build123d.Shape`.
2. `bench run bluecad.solid_code` writes `<out>/artifacts/manifest.json` with `manifest_version="bluecad_manifest_v0_1"`,
   `resolved_ports={"body": ...}`, `artifacts`, and `manifest_digest`, and lists `manifest.json` in
   `output_artifacts`.
3. A `bluecad.mesh` request referencing `geometry.step_path` and `geometry.manifest_path` from a `bluecad.solid_code`
   output and port labels `"body.<port>"` meshes successfully (tested offline with fake runtime/mesh and, where tools
   are installed, verified end-to-end).
4. Invalid port definitions (non-finite coordinates, zero direction, bad interface/dimensions, invalid port name) fail
   cleanly with an error, never emitting a partial `manifest.json`.

## Required tests

- Offline unit tests for `worker.py::_geometry` port validation (valid tube/pad ports, bare shape compatibility, invalid
  ports) and for `solid_code_run_record_to_evaluation_result` / `bench run bluecad.solid_code` manifest emission and
  `bluecad.mesh` hand-off.
- Ruff, `check_code_facts.py`, `check_typecheck_ratchet.py`, and exact-head CI green; coordinator full diff review.

## Definition of done

`bluecad.solid_code` can declare named ports and emit `manifest.json` usable by `bluecad.mesh` and `bluecad.fem`, all
acceptance criteria and tests pass, and `docs/specs/STATUS.md` is updated.
