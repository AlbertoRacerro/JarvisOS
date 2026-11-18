# 301 — CAD-VERDICT-LIGHTS-1: Shared verdict lights on solid, mesh and static FEM evidence in the CAD screen

Status: merged
Depends on: 280, 281, 284

## Goal

In the CAD screen, every solid validation, mesh quality and static FEM evidence row of a candidate shows the shared
verdict (`usable`, `usable_with_warnings`, `unchecked`, `not_usable`) as a status light, and opens the checks that
produced it. The verdict is computed on read from the stored native report with the existing result adapters; nothing
new is stored and no calculation changes.

## Why

Second item of the front-end plan in spec 298. The adapters of spec 281 and the `not_usable` rule of spec 284 are
reachable today only through the headless bench and the Runs screen (spec 299). The CAD screen, where the owner builds
and checks a part, lists the same evidence with the native tool verdict only, so a mesh that "passed" but has no quality
figure reads as good. One light per result, the same everywhere, is what the guided path will read.

## Code facts checked at `79d71b4b`

- `backend/app/modules/engineering/evaluator_contracts.py::result_verdict` derives the shared verdict from a
  descriptor's required checks; `backend/app/modules/engineering/evaluator_contracts.py::Verdict` lists the four values.
- `backend/app/modules/bluecad/result_adapters.py::solid_build_result_to_evaluation_result`,
  `backend/app/modules/bluecad/result_adapters.py::mesh_result_to_evaluation_result` and
  `backend/app/modules/bluecad/result_adapters.py::fem_result_to_evaluation_result` convert native results; today only
  `backend/app/modules/engineering/bench.py` calls them.
- `backend/app/modules/bluecad/loop.py::_run_simulation_stage` writes the native mesh result and the FEM payload
  (`result_summary`, `report`) as report artifacts and records evidence through
  `backend/app/modules/bluecad/evidence.py::record_mesh_quality_evidence` and
  `backend/app/modules/bluecad/evidence.py::record_fem_static_evidence`.
- `backend/app/modules/bluecad/evidence.py::EvidenceRecord` stores kind (`validation_v0`, `mesh_quality_v0`,
  `fem_static_v0`), the native verdict, metrics and `report_artifact_id`.
- `backend/app/modules/bluecad/read_model.py::get_bluecad_candidate_aggregate` builds the candidate aggregate;
  `backend/app/modules/bluecad/read_model.py::_build_evidence` maps evidence records to
  `BluecadEvidenceRefRead` rows that carry the native status only.
- `frontend/src/components/bluecad/BluecadWorkbench.tsx` renders those rows under "Evidence references" as plain text.

- `backend/app/modules/bluecad/loop.py::_create_simulation_run` stores the analysis spec as
  `simulation_runs.parameter_payload`; mesh and FEM evidence records point to that row through `source_run_id`. The
  FEM adapter's force-equilibrium check reads `bcs` and `loads` from that spec, the mesh adapter only its id.
- `backend/app/modules/bluecad/ledger.py::finish_attempt` keeps `report_artifact_id` and `manifest_artifact_id` on the
  attempt; `backend/app/modules/bluecad/models.py::BuildResult` (manifest, report, verdict, errors) can be rebuilt from
  those two artifacts for `solid_build_result_to_evaluation_result`.

Readiness decisions (coordinator review): the analysis spec is read from the evidence's simulation run, never from the
client; a missing run or manifest gives `unchecked` with a reason; the adapters' artifact `SourceRef`s are not returned
(path-free response, only checks and verdict).

## Scope

In scope:
1. `BluecadEvidenceRefRead` gains an optional `shared_verdict` and a bounded `checks` list (name, family, status,
   required, one-line detail), computed on read by loading the record's report artifact and calling the matching
   adapter. A report that cannot be read or adapted gives `unchecked` with a reason, never `usable`.
2. The CAD screen shows one light per evidence row with the verdict in plain Italian and opens its checks table.
   Paths and raw error payloads are not shown.
3. A static `.mjs` test and an AI browser walk over a candidate with a mesh whose quality check is missing (`unchecked`)
   and a FEM result with a failed required check (`not_usable`).

Out of scope: running the bench from the CAD screen; Design result views of the process flowsheet (its loop energy
findings and diagnosis already show in the process editor); storing verdicts; changing adapters or thresholds.

## Acceptance criteria

1. Backend tests: each evidence kind returns the adapter's verdict; an unreadable report returns `unchecked`; the
   native verdict is unchanged.
2. Frontend static test asserts the light and the Italian labels; generated contracts current.
3. The browser walk passes from a clean profile at `/` using visible controls only, evidence attached to the PR.
4. Risk medium: CI green on the exact head and one severe review of the diff.
