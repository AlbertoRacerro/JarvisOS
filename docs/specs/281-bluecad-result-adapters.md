# 281 — Checked results for solids, mesh and static FEM

Status: ready
Depends on: 280

## Goal

The existing solid build, Gmsh mesh and static CalculiX run can each produce an `EvaluationResult` with checks derived from their native evidence. A caller can distinguish a successful tool run from a checked result without losing the native reports or changing the calculated values. This prepares the three installed engines for the headless bench command.

## Why

These engines already produce useful artifacts and partial diagnostics, but the engineering evaluator registry cannot compare their results with the common verdict rule. A thin adapter is enough to make their evidence usable by the next command slice. It must expose missing evidence as `unchecked` rather than infer a pass from a tool exit code.

## Code facts checked at `574428ca`

- `backend/app/modules/engineering/evaluator_contracts.py::EvaluationResult` carries outputs, artifacts, checks and numerical diagnostics; `backend/app/modules/engineering/evaluator_contracts.py::result_verdict` derives usability from a descriptor's required checks.
- `backend/app/modules/engineering/operator_service.py::evaluator_registry` currently registers six evaluators, none for solids, mesh or FEM.
- `backend/app/modules/bluecad/service.py::build_geometry_spec` returns a `BuildResult` with a manifest and validation report; `backend/app/modules/bluecad/validate.py::_tier1_connections` reports B-rep validity and watertightness, while `backend/app/modules/bluecad/validate.py::_tier1_declared` reports declared-volume error only when a declaration exists.
- `backend/app/modules/bluecad/mesh_adapter.py::mesh_analysis_spec` returns a native dict with attempts, counts, warnings, errors and artifact paths. `backend/app/modules/bluecad/mesh_adapter.py::_post_check` checks nonzero volume elements and required groups but does not calculate numeric element quality.
- `backend/app/modules/bluecad/fem_adapter.py::solve_static_analysis` returns a native dict with solver status, parsed outputs and artifacts; `backend/app/modules/bluecad/fem_adapter.py::_parse_outputs` sums parsed reaction forces. `backend/app/modules/bluecad/fem_adapter_base.py::_deck_text` supports total nodal forces and surface pressure, and prints reactions for the first fixed set only.

## Scope

In scope:

1. Add thin, deterministic conversions from each native result to the existing `EvaluationResult`, with descriptors declaring the checks each adapter can actually evaluate. Keep the native reports as artifacts. The existing build, mesh and solve entry points and their native result shapes remain usable.
2. Solids: report measured total volume and bounding box from the manifest. Map native B-rep and watertight checks to required input checks. Map declared-volume error and tolerance to a conservation check when declared; make it advisory and `not_evaluated` when no volume was declared. Preserve failures and their native codes.
3. Mesh: require nonzero volume elements and a numeric element-quality check. Read quality from Gmsh output or its mesh artifact, with a named, sourced policy for the limit. Report the measured minimum and mean quality and the number below the policy limit. If Gmsh cannot supply or the adapter cannot parse a quality value, report the quality check as `not_evaluated`; do not invent a value. Preserve the native group and high-order validity checks.
4. Static FEM: report displacement and stress quantities from parsed output, solver completion as a required numerical check, and force equilibrium as a required conservation check only where the complete applied-load resultant and all support reactions can be obtained. For other load or reaction shapes, emit `not_evaluated` and derive `unchecked`. The existing parser's first-fixed-set reaction output must not be treated as the full support reaction when multiple fixed sets exist. Keep the native result and log as artifacts.
5. The conversions use the existing result status/failure taxonomy and `result_verdict`. A failed, refused or timed-out native run never becomes `succeeded`; a succeeded run with missing required evidence may have `unchecked` verdict. Checks do not alter outputs.

Out of scope: a new result envelope, a new solver, a headless registry command or MCP tools, mesh convergence from a single mesh, image rendering, CFD, new product-specific plausibility ranges, and changing the design-screen workflow.

## Design constraints

- Use the existing tool registry for external programs. Do not launch binaries through a new path or accept untrusted artifact paths outside the current workspace/data-root boundary.
- Keep all numeric checks finite with explicit units and sources as required by `ResultCheck`. When no valid scalar measurement exists, emit `not_evaluated` rather than a guessed pass.
- A policy threshold for mesh quality must identify the metric and its source; prove with one good and one degraded mesh fixture that it separates the two. Do not set a threshold from a warning string alone.
- Force equilibrium compares the vector sum of applied loads with the vector sum of all support reactions, normalized by the applied force magnitude. Pressure loads require an integrated resultant. A missing integration or incomplete reactions yields `not_evaluated`.
- No writes to the live data root; tests and engine proofs use temporary data and fake or registered local tools. Existing native reports stay available to current callers.

## Acceptance criteria

1. The three conversions produce serializable `EvaluationResult` values whose evaluator ids match their descriptors, preserve native artifact references, and yield the expected verdict through `result_verdict`.
2. A valid solid passes required topology checks; a bad B-rep or open solid produces `not_usable`; absent declared volume is visible as advisory `not_evaluated`; a false declared volume fails without changing the measured volume.
3. A valid mesh exposes numeric quality and element counts. A degraded mesh fails the documented policy check. Missing quality is `unchecked`, including when Gmsh exits successfully. Existing group and high-order errors remain failures.
4. A completed force-loaded FEM case with full reactions reports force-equilibrium residual and tolerance in N. A deliberately wrong or incomplete reaction set cannot be `usable`. A pressure or multiple-support case without a complete resultant is `unchecked`, not passed.
5. Timeout, tool-unavailable, parse-error and solver-error fixtures retain an appropriate non-success status and native backend code. Existing BLUECAD routes and reports remain compatible.

## Required tests

- Offline adapter tests with representative native build, mesh and FEM reports, including incomplete data and failure paths.
- A focused local proof with the installed build, Gmsh and CalculiX tools on non-private sample geometry; keep outputs in a temporary data root. If a tool is unavailable, record that gap and do not claim its engine path proved.
- Existing BLUECAD and engineering evaluator tests, lint and typecheck ratchet; exact-head CI and one severe diff review before merge.

## Definition of done

The acceptance criteria are met, the spec row is truthful, exact-head CI is green and the engine proof and review are recorded.
