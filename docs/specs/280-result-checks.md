# 280 — Evaluation results carry checks and an honest verdict

Status: ready
Depends on: none

## Goal

Every engineering evaluation result can say which checks it ran on its own numbers, with the number, the bound and
what to change, and a derived verdict says whether the result is usable. A check that an evaluator must produce and
did not is reported as "unchecked", never as passed. This is the first slice of the engineering bench: the type and
the rule only. Adapters for more engines and a headless command over the evaluator registry are later specs.

## Code facts checked on `origin/master` at `74b6adbd`

- `backend/app/modules/engineering/evaluator_contracts.py::EvaluationResult` is the shared envelope
  (`status`, `failure`, `outputs`, `output_artifacts`, `numerical: NumericalDiagnostics`, `validity`,
  `evidence_refs`); `backend/app/modules/engineering/evaluator_contracts.py::EvaluatorDescriptor` is the static
  identity; there is also the `EngineeringEvaluator` protocol. Its module docstring
  says numerical convergence is a diagnostic, never validation. Contracts are frozen pydantic models
  (`FrozenContract`) with `MAX_ITEMS = 64` bounds.
- Evaluators that build `EvaluatorDescriptor` today: `bluecad/cfd_adapter.py`, `engineering/operator_service.py`,
  `photobioreactor/pbr_evaluator.py`, `process_stack/correlations.py` (two), `process_stack/dwsim.py`,
  `process_stack/pbr_adapter.py`, `process_stack/properties.py`.
- `backend/app/modules/bluecad/validate.py::_tier1_declared` already emits checks as dicts `{"id", "tier", "status", "detail",
  "hint"}`, for example a declared-volume check with its relative error and tolerance.
- `EvaluationResult` is not part of the generated frontend contracts (`frontend/src/api/generated`).

## Scope

1. Add `ResultCheck` (frozen) to `evaluator_contracts.py` with: `id` (upper case, `^[A-Z][A-Z0-9_]{0,63}$`),
   `family` (`input`, `numerical`, `conservation`, `discretisation`, `plausibility`, `reference`), `status`
   (`pass`, `warn`, `fail`, `not_evaluated`), `actual`, `limit` (finite floats or null), `unit` (string or null),
   `hint` (one line, at most 240 characters), `source` (`physical_limit`, `policy:<name>`, `constant:<name>` or a
   value-store reference; at most 128 characters). `actual` and `limit` are null when the status is
   `not_evaluated` or the check is not numeric (for example a topology check); a `unit` requires `actual`.
2. Add `checks: tuple[ResultCheck, ...] = ()` to `EvaluationResult`, bounded by `MAX_ITEMS`, ids unique. Results
   that are not `succeeded` may carry checks (for example the input check that caused a refusal).
3. Add `required_checks` and `advisory_checks` (tuples of check ids, default empty, disjoint, bounded) to
   `EvaluatorDescriptor`.
4. Add a pure function `result_verdict(descriptor, result) -> Verdict` with
   `Verdict = Literal["usable", "usable_with_warnings", "unchecked", "not_usable"]`:
   - status not `succeeded`, or any check `fail`: `not_usable`;
   - otherwise a required check absent or `not_evaluated`: `unchecked`;
   - otherwise a descriptor with no required checks: `unchecked` (a missing declaration is not a pass);
   - otherwise any `warn`, or an advisory check absent or `not_evaluated`: `usable_with_warnings`;
   - otherwise `usable`.
   The descriptor and the result must name the same evaluator; otherwise raise `EvaluatorContractError`.
5. Checks never change outputs. Nothing in this slice clamps, corrects or drops an output because of a check; the
   envelope validator does not reject a `succeeded` result for a failed check (the verdict carries it).
6. Existing evaluators keep working unchanged: they declare no required checks and emit no checks, so their
   `succeeded` results have verdict `unchecked`. No adapter is changed in this slice.
7. Update the module docstring of `evaluator_contracts.py` with one sentence on checks and verdict.

## Acceptance criteria

1. Tests in `backend/tests/test_engineering_evaluator_contracts.py` cover each verdict branch of scope point 4,
   including: a required check missing gives `unchecked`; a required check `not_evaluated` gives `unchecked`; a
   `fail` on a `succeeded` result gives `not_usable` and the outputs are unchanged; an advisory check missing gives
   `usable_with_warnings`; a descriptor with no required checks gives `unchecked`; a mismatched evaluator raises.
2. Validation tests reject: duplicate check ids, a non-finite `actual` or `limit`, a `unit` without `actual`, a
   `not_evaluated` check with an `actual`, and overlapping required and advisory lists.
3. A result serialised with `model_dump(mode="json")` and loaded again keeps its checks; a result without the
   `checks` key (as written before this change) still loads.
4. The existing engineering, process-stack and CFD-adapter tests pass unchanged.

## Definition of done

Focused tests, ruff and the typecheck ratchet pass; CI is green on the exact head; one severe review of the diff.
