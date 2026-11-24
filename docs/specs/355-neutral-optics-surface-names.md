# 355 — NEUTRAL-OPTICS-SURFACE-NAMES-1: Generic names for the surface below a tube-array optics module

Status: ready
Depends on: —

## Goal

The tube-array optics inputs that describe the reflecting surface below the module use neutral names (ground or
water), not a site-specific one. Old names keep working as input aliases, so saved drafts and external packs do not
break.

## Why

The public core should carry generic, literature-based physics without site-specific defaults or naming. The T2 optics
kernel is already generic (`lower_albedo`, "sea or ground"), but its draft parameters, labels and hints name one
siting option only.

## Code facts checked at `e6b0f5bf`

- `backend/app/modules/process_stack/draft_models.py::PBR_T2_PARAMETERS` and `PBR_T2_SPECS` define
  `centre_height_above_sea` ("Lowest tube centre above sea") and `sea_albedo` ("Sea albedo").
- `backend/app/modules/process_stack/draft.py` (unit parameter write path, around the `PBR_T2_SPECS` lookup) rejects a
  key that no spec defines with `param_unsupported`.
- `backend/app/modules/process_stack/dynamic_engine.py::t2_optics_for_unit` reads both keys from the stored unit
  parameters and maps them to `optics_t2` `centre_height` and `lower_albedo`.
- `backend/app/modules/process_stack/optics_t2.py` validation message names the field `centre_height_above_sea`.
- `frontend/src/components/process/pbrLogic.ts` (`T2_OPTICS_KEYS`, field rules, height check message) and
  `frontend/src/components/process/PbrInspector.tsx` (T2 hint text) use the same names and wording.
- Tests: `backend/tests/test_optics_t2_dynamic_174.py`, `backend/tests/test_optics_t2_process_v2.py`.

## Scope

In scope:
- Rename `centre_height_above_sea` → `centre_height_above_surface` (label "Lowest tube centre above the surface
  below") and `sea_albedo` → `surface_albedo` (label "Albedo of the surface below (ground or water)") in the backend
  specs, engine mapping, kernel message, frontend keys, rules, messages and hint.
- One alias map in the process-stack draft layer: an incoming parameter with an old name is stored under the new name
  (refuse when both old and new are given with different values); a stored unit carrying an old name is read as the
  new name by `t2_optics_for_unit` and by draft reads. No database migration.
- Update the two backend tests and any frontend `.mjs` test that asserts the changed strings; add a test that an old
  key is accepted and returned under the new key.

Out of scope (binding non-goals):
- Any physics, default value or validation range change.
- The marine motion evaluator and its generic defaults.
- Editing external packs (the pack owner updates its cases to the new names separately; aliases keep them working).

## Files likely touched

- `backend/app/modules/process_stack/draft_models.py`, `draft.py`, `dynamic_engine.py`, `optics_t2.py`
- `frontend/src/components/process/pbrLogic.ts`, `frontend/src/components/process/PbrInspector.tsx`
- `backend/tests/test_optics_t2_dynamic_174.py`, `backend/tests/test_optics_t2_process_v2.py`, `frontend/tests/` if
  they assert the strings
- `frontend/src/api/generated` only if the contract generator output changes (regenerate, never hand-edit)

## Design constraints

- Smallest sufficient change; numeric results bitwise unchanged for the same inputs.
- Low-medium risk (rename with compatibility alias, covered by tests).

## Acceptance criteria

1. No `above_sea` / `sea_albedo` / "above sea" / "Sea albedo" remains in `backend/app` or `frontend/src` outside the
   alias map.
2. A draft write with the old keys succeeds and reads back under the new keys; a write with conflicting old and new
   values is refused.
3. A stored unit with the old keys runs the dynamic optics with results identical to the same unit with the new keys.
4. Existing optics tests pass with unchanged numbers.

## Required tests

- Offline backend tests for criteria 2–4; frontend build green.

## Definition of done

Test gate green (see `AGENTS.md`), criteria 1–4 met with a coordinator full-diff review, STATUS updated.
