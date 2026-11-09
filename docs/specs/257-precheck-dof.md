# 257 — Pre-run coherence check and degrees-of-freedom report (248 slice C)

Status: planned (draft for review; not ready)
Depends on: none (the approved [248](248-design-value-path.md) contract binds through Authority; slice A landed as
[253](253-sim-results-requirement-verification.md))

## Goal

Complete slice C of the approved design-value path, points 8 and 9 of the 248 contract:

- before a process-stack run, a deterministic check confirms that the models, correlations and approximations used
  together are consistent and inside their validity domains, and flags the ones that are not before the run starts;
- for a plant model, a deterministic degrees-of-freedom and missing-inputs report lists the unset specifications and
  maps each one to a value on the design-value path, or reports it as missing.

Risk: medium (feature code with tests, no migration expected, no egress).

## Authority

Maintainer answer `L20261106093156-1712` approves the binding contract in [248](248-design-value-path.md), with the slice
order A, then B and C. Slice A is merged (253). This spec is a draft written as readiness work. It becomes `ready` only
after its own review, as 248 requires.

## Why

Today the solver refuses an out-of-domain correlation only during the run (`EvaluationRefusal("outside_validity_domain")`
in `process_stack/correlations.py` and `properties.py`), after compute time is spent. Missing inputs are found only as
generic `UNIT_PARAM_MISSING` blockers, with no link to values that Jarvis already holds. Point 9 also makes the
missing-inputs judgment structural, so a model never decides what is missing.

## Scope

1. **Validity-domain pre-check.** Each correlation and property method that already raises `outside_validity_domain`
   declares its domain as data: the input quantity, its bounds and the reason code it raises today. The run's pre-run
   findings evaluate these domains against the inputs known before the solve, such as feed conditions, unit parameters
   and declared operating ranges. Each violation gives one finding with severity `blocker` and the same reason code the
   runtime refusal uses. A domain that depends on a solved value, for example Reynolds number from a computed velocity,
   gives a `not_checkable_before_run` info finding instead. The check never guesses that value.
2. **Coherence rules.** A small, closed, versioned rule table covers pairs that are mutually inconsistent: an ideal-gas
   or ideal-solution package with a declared operating point outside its stated range; typed kinetics whose declared
   temperature range excludes the reactor temperature; an isothermal approximation on a unit with a declared energy
   stream. Each rule has an id, a version and a test. Rules outside the table are out of scope; there is no
   model-proposed rule.
3. **Degrees-of-freedom report.** For each unit and feed stream, the required specifications come from the existing
   `UNIT_REGISTRY`/`STREAM_SPECS` (`draft_models.py`), so this adds no new registry. Each specification is `set`, `unset`
   or `available`. An `available` specification is unset in the draft but has a design value on the 248 path for the
   same quantity and conditions, and the report shows the value's validation state (passing or red dot). The report
   counts specifications, not equations; that is enough for the units the registry supports today. It is read-only:
   it never writes a value into the draft or into a Project Basis parameter (contract point 10).
4. **Surfaces.** The findings join `validate_document`'s existing Pre-Run findings with `source: "jarvis"`. The DOF
   report is a read-only API on the draft and a panel in the existing draft editor's Pre-Run view.

## Non-goals

- Equation-level DOF analysis (IDAES/Pyomo); 248 lists that as a read-only pilot.
- Checks inside DWSIM, or replacing DWSIM's own Validate.
- Auto-filling an `available` value into the draft; the user applies it with the existing parameter edit.
- Slices B and D–H, Slack delivery, provider calls, new dependencies or egress.
- Changing runtime refusals. The pre-check adds early findings, and the runtime still refuses on its own.

## Design notes (anchors found)

- `backend/app/modules/process_stack/draft.py` `validate_document` is the single source of Pre-Run blockers
  (`draft.py:1776` gates the run on them). The new checks are called from it, so the run gate needs no change.
- `correlations.py` raises `outside_validity_domain` at three sites and `properties.py` (CoolProp EOS limits) at three.
  The domain table moves those bounds into data that both the runtime and the pre-check read, so the two cannot drift.
- Design values with validation state come from the project-knowledge persistence added by 253. The lookup matches
  quantity, unit and conditions under the same applicability rule that requirement verification uses, and its result
  is `not_evaluable` when the conditions do not match (point 7).

## Acceptance criteria

1. Moving each runtime bound into the shared domain table leaves runtime behaviour unchanged: the existing correlation
   and property tests pass unmodified.
2. A draft whose known inputs violate a domain gets a Pre-Run `blocker` finding with the runtime reason code, and the run
   is refused before any solve starts. A domain that depends on solved values gives `not_checkable_before_run`, never a
   blocker.
3. Each coherence rule has a positive and a negative test and reports its rule id and version in the finding.
4. The DOF report lists every required specification of every unit and feed as `set`, `unset` or `available`. An
   `available` entry names the design value, its source and its validation state. A red-dot value is shown as such and
   is never presented as validated.
5. No call writes to the draft, the Project Basis or the design values.
6. Browser walk: from `/`, open a draft with an out-of-domain feed, see the Pre-Run blocker, open the DOF panel and see
   one `available` and one `unset` entry.

## Tests

Focused backend tests for the domain table, coherence rules and DOF report; a frontend `.mjs` test for the panel
labels; the e2e walk above.

## Open questions for the review

- Should a violated domain be a `blocker` or a `warning` that the user can run past with the result marked? The 248
  contract says to flag before the run starts; this draft proposes `blocker`, which matches the runtime refusal.
- Do the first coherence rules match the maintainer's real cases, or should the Q1 benchmark's validity-domain cases
  (251) choose them?

## Definition of done

Acceptance criteria 1–6 hold with exact-head CI green, one severe review of the diff, and the browser walk evidence
attached.
