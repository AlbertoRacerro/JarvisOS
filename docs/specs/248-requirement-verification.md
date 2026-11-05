# 248 — Verify requirements against simulation results in Project Basis

Status: draft
Depends on: none

## Goal

In Project Basis, the operator can take a numeric output of a completed simulation run, admit it as evidence, check a
requirement's acceptance criterion against it on the current working revision, and open the reconciled snapshot the
result lands in. Today these steps exist only as backend routes.

## Why

Option 4 of question `L20261105213009-3f9a` (decision `L20261105214933-cb50`): the most useful block for the
engineering line. The backend already has `POST /project-knowledge/scalars` (deterministic admission: the server
re-extracts value and unit from the run's `output_payload` and refuses anything it cannot prove),
`POST /project-knowledge/validation` (deterministic pass/fail/not-evaluable against the criterion) and
`GET /project-knowledge/workspaces/{id}/snapshots/{snapshot_id}`. Revalidation and Final reconcile already have UI.

## Scope

In scope:
- **Admit a result.** From a selected working revision, choose a completed simulation run of the workspace (existing
  `GET /workspaces/{id}/simulation-runs`) and one of its scalar outputs. The frontend sends run id, output name, the value
  and unit as shown in the run output, and the SHA-256 of the run's `output_payload`. The backend's refusal codes
  (`scalar_value_unproven`, `scalar_source_digest_mismatch`, …) are shown as readable reasons.
- **Check a requirement.** For a requirement with a criterion, run the deterministic check against the current working
  revision and an admitted result. Show the outcome (`pass`, `fail`, `not_evaluable`, `no_material_effect`,
  `recomputation_required`) and its reason. The revalidation summary then refreshes through the existing route.
- **Read the reconciled snapshot.** After Final reconcile, a read-only view of the current snapshot: when, graph complete
  or not, the requirements and owners it contains, and its validation set. Technical digests stay in a collapsed details
  block, as in the existing panel.
- **One small backend read addition** (the only backend change): `RevalidationRead` gains
  `applicability_set_digest`, the current value the validation route already computes. Without it the browser cannot
  build a valid `ValidationRequest`. Read-only, no schema change, no new route.

Out of scope (binding non-goals):
- Changes to admission, validation, revalidation or reconcile semantics; typing a value the run does not prove; any
  model-proposed value or outcome (Never 7).
- Editing criteria (they stay in the governed draft flow), applicability editing, snapshot comparison or rollback.
- Recomputation (stays owned by Process through the existing handoff link).

## Files likely touched

Verify against actual code before starting; report conflicts instead of guessing.

- `backend/app/modules/project_knowledge/models.py`, `service.py` (`revalidation_status`) and a focused test
- `frontend/src/api/generated` (regenerated, never by hand) and the Project Knowledge API client
- `frontend/src/components/fusion/ProjectKnowledgePanel.tsx` or focused components beside it
- `frontend/tests/`, `frontend/package.json`, `e2e/`

## Design constraints

- Every number shown as admitted comes from the backend response, never from the form.
- Stale inputs (`validation_basis_stale`, `applicability_stale`, `criterion_stale`, digest mismatch) show a visible
  conflict with a way to reload; nothing retries on its own.
- Read-only mode shows results and snapshots but no admit or check controls.

## Acceptance criteria

1. From a clean profile at `/`, an operator with a seeded completed run and a requirement with a numeric criterion admits
   the run's output, checks the requirement, sees `pass` (and, with a second seeded run outside the criterion, `fail`),
   reconciles with the existing control and opens the snapshot that lists the requirement and its validation.
2. A value or unit the run does not prove is refused with a readable reason and nothing is stored.
3. `GET .../revalidation` returns `applicability_set_digest` equal to what the validation route checks; existing
   revalidation fields and tests are unchanged.

## Required tests

- Backend: the new field matches the digest the validation route accepts, and a changed applicability changes it.
- Offline frontend tests: payload construction (digest of the payload, run id, output name), refusal messages, outcome
  display, read-only mode.
- A clean-profile browser walk on a fresh temporary data root seeded through the API, covering criterion 1 and 2. Keep
  its evidence directory.

## Definition of done

Acceptance criteria pass, along with the relevant tests and lint. Required CI, review and the UI browser walk pass on the
exact head. The registry is updated as the work advances. Draft under maintainer directive `L20261105144536-3af0` item 3
and decision `L20261105214933-cb50`; becomes `ready` only after the maintainer approves this contract.
