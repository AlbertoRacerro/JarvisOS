# 313 — REACTOR-GROWTH-FORMS-1: Photobioreactor unit accepts all registered light growth forms

Status: ready
Depends on: none

## Goal

`PhotobioreactorT1` and `resolve_growth_model` accept any light-response form registered in `bio_models.forms`
(`light.monod`, `light.haldane`, `light.steele`, `light.eilers_peeters_steady`, `light.hill`), require the pinned
parameter symbols for that form, validate them with the form's domain checks, and evaluate the cylindrical cross-section
light average ⟨f_I⟩ using that form instead of hardcoding Monod.

## Why

Directive `L20261116160140-7608` point 3 (Wave 1): "the reactor unit must accept any registered growth model form, not
only Monod." Today `bio_models.forms` and `evaluate_card` already define and evaluate all five light forms, while
`resolve_growth_model` refuses anything except `light.monod` and `PhotobioreactorT1` (`Growth._monod`) hardcodes
Monod.

## Code facts checked at `02e41306`

- `backend/app/modules/bio_models/forms.py::light_monod`, `light_haldane`, `light_steele`,
  `light_eilers_peeters_steady`, and `light_hill` implement all five registered light forms and their `FormRefusal`
  domain checks.
- `backend/app/modules/bio_models/service.py::resolve_growth_model` currently restricts `factors["light"]` to
  `{"light.monod"}` and `required` to `{"K_I", ...}` instead of the form's parameter symbols.
- `backend/app/modules/process_stack/pbr_unit.py::Growth` stores `light_saturation: float` (`K_I`) and
  `Growth._monod` evaluates `light_response(irradiance, self.light_saturation, 0.0)` inside
  `cylinder.response_average_tau`.
- `backend/app/modules/process_stack/pbr_unit.py::_model_values` and `build_growth` validate only `K_I` via
  `forms.light_monod(0.0, values["K_I"])`.

## Scope

In scope:
- `backend/app/modules/bio_models/service.py::resolve_growth_model`:
  - Accept all five registered light forms (`light.monod`, `light.haldane`, `light.steele`,
    `light.eilers_peeters_steady`, `light.hill`).
  - Require the exact parameter symbols for the chosen light form (`K_I`; `K_I, K_i`; `I_opt`; `I_opt, beta`; `I_k, n`),
    emitting `PBR_MODEL_CARD_SYMBOL_MISSING` when any is absent from the pinned parameter set.
- `backend/app/modules/process_stack/pbr_unit.py`:
  - `_model_values` validates the chosen light form's parameters using `forms.light_*` (raising
    `PbrFailure("PBR_MODEL_CARD_UNAVAILABLE", ...)` on `FormRefusal`).
  - `Growth` carries `light_form: str` and `light_parameters: tuple[float, ...]` (keeping `light_saturation` optional
    or derived for backwards compatibility if needed) and evaluates the vectorized NumPy response for the chosen light
    form in `light(...)` via `cylinder.response_average_tau`.
- Update `backend/tests/test_170_plumbing_runtime.py` and `backend/tests/test_pbr_unit_170.py` where they previously
  asserted `light.haldane` / `light.eilers_peeters_steady` were refused, keeping refusal tests for genuinely
  unsupported T1 forms (e.g. `nutrient.droop`, multiple nutrients).
- Add tests in `backend/tests/test_reactor_growth_forms_313.py` verifying all five light forms resolve, validate invalid
  parameters, and run `PhotobioreactorT1` (`evaluate_pbr_t1` / `Growth.rates_at`), matching `bio_models.forms` in the
  zero-attenuation limit (`tau -> 0`) and producing distinct steady states for photoinhibition (`light.haldane` /
  `light.steele`) vs `light.monod`.

Out of scope (binding non-goals):
- Supporting `nutrient.droop` (intracellular quota `Q` state is not in `PhotobioreactorT1`'s `(X, N, O2)` state) or
  multiple nutrients.
- Adding new light forms beyond the five already in `bio_models/forms.py`.
- Frontend changes.

## Acceptance criteria

1. `resolve_growth_model` accepts `light.monod`, `light.haldane`, `light.steele`, `light.eilers_peeters_steady`, and
   `light.hill`, and rejects missing form-specific symbols with `PBR_MODEL_CARD_SYMBOL_MISSING`.
2. Invalid light parameters (e.g. `K_i <= 0`, `I_opt <= 0`, `beta < 0`, `n <= 0`) raise `PbrFailure`
   (`PBR_MODEL_CARD_UNAVAILABLE`).
3. `Growth.light` and `PhotobioreactorT1` evaluate each of the five forms through `cylinder.response_average_tau`,
   matching `bio_models.forms.light_*` when `tau <= 0`.
4. `test_reactor_growth_forms_313.py`, `test_170_plumbing_runtime.py`, and `test_pbr_unit_170.py` pass, and `ruff` is
   clean.

## Risk and evidence

Medium (physics/solver unit code with deterministic tests, no schema change). Evidence: focused pytest suite, `ruff`,
CI green on exact head, coordinator full diff review.
