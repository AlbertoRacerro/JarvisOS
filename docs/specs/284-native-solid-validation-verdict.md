# 284 — Preserve native solid validation failures in the bench verdict

Status: ready
Depends on: 281

## Goal

A solid that was built but failed any native validation check remains measurable, while its shared `EvaluationResult`
has a failed check and a `not_usable` verdict. The bench command and agent tool show the same verdict.

## Why

H7 inspection found that a native declared bounding box failure is dropped by the solid adapter. A reproduced
`BuildResult` with `verdict="fail"` and `T1_BBOX_DECL=fail` currently becomes `status="succeeded"`, with only
passing topology checks and `verdict="usable_with_warnings"`. This lets an agent trust a geometry the native
validator rejected. Repair this loss of evidence before extending code-driven geometry.

## Code facts checked at `7d6c9310`

- `backend/app/modules/bluecad/validate.py::_tier0_artifacts` and
  `backend/app/modules/bluecad/validate.py::_tier1_declared` emit artifact, declared volume and bounding box
  checks in the native report; `backend/app/modules/bluecad/validate.py::_verdict` makes any failed check fail
  that report.
- `backend/app/modules/bluecad/result_adapters.py::solid_build_result_to_evaluation_result` maps topology and
  declared volume, but does not propagate every native failure from a built solid.
- `backend/app/modules/engineering/evaluator_contracts.py::result_verdict` returns `not_usable` when any shared
  check fails, regardless of `status`.
- `backend/app/modules/engineering/bench.py::_run_bluecad_solid` and
  `backend/app/modules/engineering/bench.py::run_evaluation` use that adapter and verdict for the command and
  agent tool.

## Scope

In scope:

1. Preserve the failure of every native validation check with status `fail` or `error` in the shared checks for
   a built solid. The failed native check ids must be identifiable to a reader, with the native report retained as
   an artifact. A numeric declared bounding box mismatch carries its measured error and tolerance in millimetres.
2. A native solid `verdict="fail"` must never become `usable`, `usable_with_warnings` or `unchecked` in the
   shared envelope, even if the report has an unknown failed check id. The solid's computed outputs stay intact;
   `status="succeeded"` continues to mean the kernel returned a built shape.
3. Keep the existing mapping of topology and declared volume; avoid duplicate shared check ids.

Out of scope:

- Changing the CAD kernel, geometry schema, native validation policy or the verdict rule for other evaluators.
- Adding a project-specific geometry, tolerance or plausibility range.
- Changing old result files in place.

## Files likely touched

- `backend/app/modules/bluecad/result_adapters.py`
- `backend/tests/bluecad/test_result_adapters.py`
- `backend/tests/test_engineering_bench.py` if needed for the command projection
- `docs/specs/STATUS.md`

## Acceptance criteria

1. A built solid whose only native failure is `T1_BBOX_DECL` returns `status="succeeded"`, retains measured
   outputs, exposes a failed bounding box check with `actual`, `limit`, `unit="mm"`, and derives `not_usable`.
2. A different or unrecognized native failed check also prevents a usable verdict and names the native check in
   the shared result. No existing native failure is silently dropped.
3. A passing native report still produces the same outputs and verdict as before.
4. A headless bench run with a real, deliberately wrong declared bounding box reports `not_usable` and keeps its
   `result.json`, native manifest and validation report.

## Required tests

- Offline adapter tests for criteria 1–3 and the existing adapter tests.
- One local real-kernel command proof for criterion 4, using a temporary root and no project data.
- Focused bench tests, Ruff, exact-head CI and one severe review of the final diff.

## Definition of done

The native and shared verdicts agree on failure, computed values are preserved, the evidence above is recorded,
and the registry row is truthful.
