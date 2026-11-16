# 282 — Headless bench command over the evaluators

Status: in_review
Depends on: 280, 281

## Goal

One command-line entry point, run without the HTTP server, lists the engineering evaluators with their
availability and declared checks, runs one evaluator on a JSON request into an output folder, and compares two
such folders. Its stdout is a bounded JSON view ordered for a reader that must decide whether to trust the
result: status and verdict, then failed and warning checks, then the other checks, outputs and artifact paths.
The full `EvaluationResult` and the native reports are files in the output folder.

## Why

The engines already return `EvaluationResult` with checks (280, 281), but an agent harness or an engineer at a
shell can reach them only through HTTP routes or Python. A single headless door with bounded output and honest
exit codes lets a harness build, mesh, solve and judge a result without growing its own wrappers. It is the
door that the later MCP tools and the measurement suite call.

## Code facts checked at `3a6f41cb`

- `backend/app/modules/engineering/operator_service.py::evaluator_registry` builds the six registered evaluators
  and replaces one that fails to construct with `backend/app/modules/engineering/operator_service.py::UnavailableEvaluator`.
- `backend/app/modules/engineering/operator_service.py::evaluator_reads` turns descriptors and availability into
  rows and survives a failing `availability()`.
- `backend/app/modules/engineering/evaluator_contracts.py::EngineeringEvaluator` is the protocol: `descriptor`,
  `availability`, `evaluate(EvaluationRequest) -> EvaluationResult`.
- `backend/app/modules/engineering/evaluator_contracts.py::validate_evaluation_result` and
  `backend/app/modules/engineering/evaluator_contracts.py::result_verdict` are the existing request/result
  consistency and verdict rules.
- `backend/app/modules/bluecad/result_adapters.py::solid_evaluator_descriptor`,
  `backend/app/modules/bluecad/result_adapters.py::mesh_evaluator_descriptor` and
  `backend/app/modules/bluecad/result_adapters.py::fem_evaluator_descriptor` declare the checks of the three
  BLUECAD engines; `backend/app/modules/bluecad/result_adapters.py::solid_build_result_to_evaluation_result`,
  `backend/app/modules/bluecad/result_adapters.py::mesh_result_to_evaluation_result` and
  `backend/app/modules/bluecad/result_adapters.py::fem_result_to_evaluation_result` convert native results.
  None of the three is in `evaluator_registry`.
- The native entry points are `backend/app/modules/bluecad/service.py::build_geometry_spec_file` (GeometrySpec
  JSON file to an output folder), `backend/app/modules/bluecad/mesh_adapter.py::mesh_analysis_spec` (analysis
  spec dict naming `geometry.step_path` and `geometry.manifest_path`) and
  `backend/app/modules/bluecad/fem_adapter.py::solve_static_analysis` (analysis spec plus the native mesh result).
- External programs resolve through `backend/app/modules/bluecad/registry.py::resolve_tool`;
  `backend/app/modules/bluecad/registry.py::resolve_registry_path` takes an explicit path, then the environment
  variable, then the repository default.
- `backend/app/modules/bluecad/cli.py::main` is the existing headless pattern: argparse subcommands, output
  folder, exit code from the verdict.

## Scope

In scope:

1. A module `backend/app/modules/engineering/bench.py` with `main(argv)`, run as
   `python -m app.modules.engineering.bench` from `backend/`, and three subcommands.
2. `list`: one JSON document on stdout with one row per evaluator: id, backend kind, name and version, fidelity,
   availability state and reason code, `required_checks`, `advisory_checks`, and the request kind it accepts
   (`evaluation_request` or the native BLUECAD kind). The rows are the six registry evaluators plus
   `bluecad.solid`, `bluecad.mesh` and `bluecad.fem`. BLUECAD availability comes from `resolve_tool` for its
   registered tool (`build123d` import for the solid); a resolution error is a non-available state with its
   error code, never an exception.
3. `run <evaluator_id> <request.json> --out DIR [--from DIR] [--root DIR] [--tool-registry PATH] [--timeout-s S]`:
   - a registry evaluator reads an `EvaluationRequest` JSON, calls `evaluate`, and checks the pair with
     `validate_evaluation_result`;
   - `bluecad.solid` reads a GeometrySpec JSON and calls `build_geometry_spec_file`;
   - `bluecad.mesh` reads an analysis spec JSON whose geometry paths may be relative to the request file;
   - `bluecad.fem` reads an analysis spec JSON and `--from` names the output folder of an earlier
     `run bluecad.mesh`, whose native mesh result it reads;
   - each converts the native result with the 281 adapter, writes `DIR/result.json` (the full
     `EvaluationResult` and its `verdict` from `result_verdict` against the evaluator's descriptor) and keeps the
     native reports under `DIR`, then prints the bounded view.
4. `compare <dirA> <dirB>`: reads both `result.json` files and prints evaluator, status and verdict of each,
   outputs by name side by side with absolute and relative difference when both values exist in the same unit
   (otherwise a `unit_mismatch` or `missing` mark, no conversion), and checks by id side by side with status and
   actual value.
5. Exit codes: `run` returns 0 for `usable` and `usable_with_warnings`, 1 for `unchecked` and `not_usable`, 2 for
   a result that is not `succeeded` and for any usage or input error. `list` and `compare` return 0, or 2 when an
   input cannot be read.
6. Errors are JSON on stdout with a code and one line saying what to change (for a request that fails
   validation: the field locations from the validation error, bounded).

Out of scope (binding non-goals):

- MCP tools, a design-screen control or any frontend change; HTTP routes.
- Registering the BLUECAD engines in `evaluator_registry` or changing what the HTTP evaluator routes list.
- New checks, new adapters for other engines, unit conversion in `compare`, renders, CFD runs.
- A new result format: the files and the view are projections of `EvaluationResult`.
- A console-script entry, packaging or a new dependency.

## Files likely touched

- `backend/app/modules/engineering/bench.py` (new)
- `backend/app/modules/engineering/module.toml` (entrypoint line)
- `backend/tests/test_engineering_bench.py` (new)
- `docs/MODULES.md` (regenerated)

## Design constraints

- **Bounded view.** The stdout view of `run` and `compare` is at most 8000 characters. When the full view is
  larger, lower-priority sections (all-pass checks, then outputs, then artifact paths) are cut first and the view
  says `"truncated": true` with the path of `result.json`. Failed and warning checks, status, verdict and the
  failure are never cut.
- **Paths stay in the working root.** `--out`, `--from`, the request file and every path a request names are
  resolved and must lie under one root: the current working directory unless `--root` is given. A path outside
  it is an input error before any tool runs. The command never writes outside `--out` and never reads or writes
  the live data root; it does not start the HTTP server.
- **No new execution path.** External programs run only through the existing BLUECAD tool registry calls inside
  the native entry points; registry evaluators run through their own `evaluate`.
- **Checks never change numbers.** The command adds no check and alters no output; the verdict is always
  `result_verdict` against the evaluator's own descriptor. An unavailable evaluator or a tool that cannot be
  resolved yields a non-succeeded result with the existing failure taxonomy, not a crash.
- **`--out` is fresh or empty.** An existing non-empty `--out` is an input error, so a folder never mixes two
  runs.
- Code stays generic: no project geometry, units or ranges; sample requests in tests are neutral shapes.

## Acceptance criteria

1. `list` prints valid JSON with nine rows, each with the declared checks and an availability state; with a
   tool registry whose tools are absent the BLUECAD rows are not available with a reason code, and the command
   still exits 0.
2. `run` on a registry evaluator with a valid `EvaluationRequest` writes `result.json` whose `verdict` equals
   `result_verdict` for that result, and the exit code follows criterion 5 of the scope; an invalid request
   exits 2 with the failing field named. The six registry evaluators declare no required checks today, so a
   succeeded run of one of them is `unchecked` and exits 1; the command does not hide that.
3. `run bluecad.solid`, then `run bluecad.mesh` on the solid's folder, then `run bluecad.fem --from` the mesh
   folder, produces three `result.json` files with the verdicts of the 281 adapters, using fake native results in
   the offline tests.
4. A view larger than the bound keeps every failed and warning check, status and verdict, marks
   `truncated`, and fits in 8000 characters.
5. A request, `--from` or geometry path outside the root, or a non-empty `--out`, exits 2 before any engine
   runs.
6. `compare` on two result folders reports differences for shared outputs in the same unit, marks a unit
   mismatch without converting, and lists checks of both sides by id.
7. With the installed solid kernel, Gmsh and CalculiX on the coordinator's machine, the solid → mesh → FEM chain
   runs through the command on a neutral sample in a temporary folder; a missing tool is reported as a gap, not
   claimed.

## Required tests

- Offline tests in `backend/tests/test_engineering_bench.py` calling `main(argv)` with `tmp_path` as root:
  `list` with an absent-tool registry; a registry evaluator run (fluids pressure drop) with valid and invalid
  requests; the BLUECAD chain with the native entry points monkeypatched to return recorded native results;
  truncation; path-boundary and non-empty `--out` refusals; `compare` with matching, mismatched and missing
  outputs.
- The local engine proof of criterion 7, reported in the PR; skipped tests are not a proof.
- Existing engineering and BLUECAD tests, lint, typecheck ratchet; exact-head CI and one severe diff review.

## Definition of done

The acceptance criteria are met, `docs/MODULES.md` is regenerated, the STATUS row is truthful, exact-head CI is
green, and the engine proof and review are recorded.
