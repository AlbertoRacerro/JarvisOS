# 257 — Pre-run coherence check and degrees-of-freedom report (248 slice C)

Status: ready (coordinator-accepted after review; see "Review outcome")
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
order A, then B and C. Slice A is merged (253). The coordinator reviewed this draft against the code and accepted it
with the changes recorded under "Review outcome" (`docs/PROCESS.md` section 3, rule 1).

## Why

Today the PBR unit's loop hydraulics find an out-of-domain input only during the run: CoolProp refuses water properties
outside its EOS range, the pipe correlation answers `transitional_regime`, and a pinned CFD reduced model refuses or warns
outside its fitted range (`pbr_unit.hydraulics`), after the periodic solve has been set up. Missing inputs are found only
as generic `UNIT_PARAM_MISSING` blockers, with no link to values that Jarvis already holds. Point 9 also makes the
missing-inputs judgment structural, so a model never decides what is missing.

## Review outcome (coordinator acceptance, 2026-11-09)

The draft's anchors were checked against the code before acceptance:

- `validate_document` (the DWSIM draft path) does not call `correlations.py`; the correlation and property refusals are
  reached in a draft run only through the `PhotobioreactorT1` unit's `pbr_unit.hydraulics`. Scope 1 is bound to that path.
- The proposed coherence rules are already Pre-Run blockers: typed-kinetics parameter domains (`KINETICS_PARAMETER_DOMAIN`),
  reactor mode and temperature-profile coherence (`KINETICS_MODE_UNSUPPORTED`, `KINETICS_TEMPERATURE_PROFILE_UNSUPPORTED`)
  and energy-stream requirements. No property package declares an operating range today. The coherence table is
  therefore dropped (absorbed); adding one would be new infrastructure with no case to check.
- Open question 1 is settled by "do not change runtime behaviour": the pre-check uses the runtime's own severity. A
  runtime refusal becomes a `blocker`; a runtime soft result (`transitional_regime`, a pinned CFD model out
  of its range) becomes a `warning`. Open question 2 lapses with the coherence table.

## Scope

1. **PBR hydraulics domain pre-check.** `pbr_validation.pbr_findings` gains the hydraulics domains that are knowable
   before the solve, computed with the same functions and constants the runtime uses (no copied bounds):
   - water properties at `temperature_mean` and 1 atm through the CoolProp property evaluator; a refusal gives a
     `blocker` `PBR_HYDRAULICS_OUTSIDE_DOMAIN` with the runtime backend code (`coolprop_eos_range` and so on);
   - for an unpinned loop, the Reynolds number from `liquid_velocity`, `tube_inner_diameter` and those properties; the
     transitional band gives a `warning` with code `transitional_regime`, matching the runtime's soft result;
   - for a pinned CFD reduced model, the model's own range check (`reduced_model.evaluate` with the pin's `out_of_range`
     policy). The runtime reports a refused or warned evaluation as a caveat and still runs, so both give a `warning`
     `PBR_HYDRAULICS_CFD_OUT_OF_RANGE` listing the violations; an unavailable model is left to the runtime.
   The check runs only when every input it needs is set; a missing input is already reported by `UNIT_PARAM_MISSING`.
2. **Degrees-of-freedom report.** For each unit and feed stream, the required specifications come from the existing
   `UNIT_REGISTRY`/`STREAM_SPECS` and `spec.params_for(mode)` (`draft_models.py`), so this adds no new registry. Each
   specification is `set`, `unset` or `available`. An `available` specification is unset in the draft but has a design
   value on the 248 path for the same quantity, unit and conditions in the project-knowledge records added by 253
   (`simulation_result_values` with their checks), and the report shows the value's validation state (passing or red
   dot). Conditions that do not match give no `available` entry (contract point 7). The report counts specifications,
   not equations. It is read-only: it never writes a value into the draft or into a Project Basis parameter (point 10).
3. **Surfaces.** The new findings appear through `validate_document`'s existing Pre-Run findings. The DOF report is a
   read-only API on the draft and a panel in the existing draft editor's Pre-Run view.

## Non-goals

- A coherence rule table (absorbed by existing Pre-Run blockers, see Review outcome).
- Moving correlation bounds into a shared table; the pre-check calls the runtime functions instead.
- Equation-level DOF analysis (IDAES/Pyomo); 248 lists that as a read-only pilot.
- Checks inside DWSIM, or replacing DWSIM's own Validate.
- Auto-filling an `available` value into the draft; the user applies it with the existing parameter edit.
- Slices B and D–H, Slack delivery, provider calls, new dependencies, migrations or egress.
- Changing runtime refusals or soft results.

## Design notes (anchors found)

- `backend/app/modules/process_stack/draft.py` `validate_document` is the single source of Pre-Run blockers and already
  extends them with `pbr_validation.pbr_findings`, so the run gate needs no change.
- `pbr_unit.hydraulics` is the runtime reference: CoolProp water at `temperature_mean`, `PipePressureDropEvaluator`
  (`transitional_regime` returned as a soft result), `_cfd_hydraulics` with the pin's `out_of_range` policy.
- Design values with validation state come from the project-knowledge persistence added by 253
  (`backend/app/core/project_knowledge_schema.py`, `backend/app/modules/project_knowledge/service.py`).

## Acceptance criteria

1. Runtime behaviour is unchanged: the existing `pbr_unit`, correlation and property tests pass unmodified.
2. A PBR draft whose `temperature_mean` is outside the water EOS range gets a Pre-Run `blocker` with the runtime backend
   code, and the run is refused before any solve starts. A draft in the transitional Reynolds band gets a `warning`
   `transitional_regime` and can still run. A pinned CFD model out of range gives a `warning`.
3. A draft with an unset hydraulics input gets no hydraulics domain finding, only the existing `UNIT_PARAM_MISSING`.
4. The DOF report lists every required specification of every unit and feed as `set`, `unset` or `available`. An
   `available` entry names the design value, its source and its validation state. A red-dot value is shown as such and
   is never presented as validated.
5. No call writes to the draft, the Project Basis or the design values.
6. Browser walk: from `/`, open a draft with an out-of-domain PBR input, see the Pre-Run blocker, open the DOF panel and
   see one `available` and one `unset` entry.

## Tests

Focused backend tests for the hydraulics pre-check and the DOF report; a frontend `.mjs` test for the panel labels; the
e2e walk above.

## Definition of done

Acceptance criteria 1–6 hold with exact-head CI green, one severe review of the diff, and the browser walk evidence
attached.
